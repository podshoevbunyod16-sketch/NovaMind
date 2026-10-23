"""
routes/tts.py — HTTP-API озвучки ответов (text-to-speech).

Маршруты:

* ``GET  /api/tts/status``   — доступна ли озвучка, какие провайдеры и голоса
  реально настроены, текущие настройки и лимиты;
* ``GET  /api/tts/voices``   — список голосов (можно фильтровать по провайдеру);
* ``GET  /api/tts/settings`` — настройки озвучки;
* ``POST /api/tts/settings`` — сохранить настройки (единственное место хранения —
  ``runtime_settings.json`` через ``config.speech_settings``);
* ``POST /api/tts/speech``   — озвучить текст: NDJSON-поток событий
  ``start`` → ``chunk`` … → ``done`` (или ``error``/``cancelled``);
* ``POST /api/tts/cancel``   — отменить запуск по ``job_id``.

Почему поток, а не один ответ: длинный ответ режется на части, и первая часть
уже играет, пока генерируются следующие. Отмена при этом останавливает работу
между частями, а запоздавшие части не проигрываются — клиент их не получает.

Ключи провайдеров остаются на сервере: наружу уходят только флаг доступности,
список голосов и ссылки на готовые файлы в ``/media``.
"""

from __future__ import annotations

import json
import logging

from flask import Blueprint, Response, jsonify, request, stream_with_context

import security
import tts_service

tts_bp = Blueprint("tts", __name__)
log = logging.getLogger("novamind.tts")


def _signed_in() -> tuple[bool, str]:
    """Вошёл ли пользователь. Импорт ленивый: routes.terminal тянет Flask-сессию."""
    try:
        from routes.terminal import signed_in, current_nick
        if signed_in():
            try:
                return True, str(current_nick() or "user")
            except Exception:  # noqa: BLE001 — имя не критично для лимита
                return True, "user"
    except Exception:  # noqa: BLE001 — гость остаётся гостем
        pass
    return False, ""


def _ndjson(event: dict) -> str:
    return json.dumps(event, ensure_ascii=False) + "\n"


@tts_bp.route("/api/tts/status")
def tts_status():
    """Реальное состояние озвучки — без выдуманных «подключено»."""
    data = tts_service.status()
    signed, _ = _signed_in()
    limit = security.tts_limiter.limit_for(signed)
    data["rate_limit"] = {"limit": limit, "window": security.tts_limiter.window()}
    return jsonify(data)


@tts_bp.route("/api/tts/voices")
def tts_voices():
    provider = (request.args.get("provider") or "").strip().lower()
    lang = (request.args.get("lang") or "").strip().lower()
    voices = tts_service.voice_catalog(provider)
    if lang:
        voices = [item for item in voices if lang in item.get("languages", [])]
    return jsonify({"voices": voices, "count": len(voices), "lang": lang or None})


@tts_bp.route("/api/tts/settings")
def tts_get_settings():
    return jsonify({"settings": tts_service.settings(), "status": tts_service.status()})


@tts_bp.route("/api/tts/settings", methods=["POST"])
def tts_save_settings():
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({"error": "Ожидался JSON-объект с настройками"}), 400
    try:
        saved = tts_service.save_settings(data)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"success": True, "settings": saved})


@tts_bp.route("/api/tts/speech", methods=["POST"])
def tts_speech():
    """Озвучивает текст ответа и отдаёт NDJSON-поток готовых частей."""
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({"error": "Ожидался JSON-объект"}), 400
    text = str(data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "Введите текст для озвучивания"}), 400
    if len(text) > tts_service.MAX_TEXT_CHARS:
        return jsonify({
            "error": f"Текст слишком длинный для озвучки: {len(text)} символов, "
                     f"максимум {tts_service.MAX_TEXT_CHARS}.",
            "limit": tts_service.MAX_TEXT_CHARS,
        }), 413

    signed, nick = _signed_in()
    allowed, retry_after, limit, remaining = security.tts_rate_check(request, signed, nick)
    if not allowed:
        response = jsonify({"error": "Слишком много запросов на озвучку. Попробуйте позже.",
                            "retry_after": retry_after})
        response.headers["Retry-After"] = str(retry_after)
        response.headers["X-RateLimit-Limit"] = str(limit)
        response.headers["X-RateLimit-Remaining"] = "0"
        return response, 429

    options = {
        "voice": str(data.get("voice") or ""),
        "provider": str(data.get("provider") or ""),
        "model_id": str(data.get("model") or ""),
        "read_code": bool(data.get("read_code")),
        "lang": str(data.get("lang") or ""),
    }
    # Уборка старых файлов не должна задерживать ответ: считаем только новые.
    try:
        tts_service.cleanup_media()
    except Exception:  # noqa: BLE001 — уборка не влияет на озвучку
        log.debug("cleanup_media failed", exc_info=True)
    tts_service._prune_jobs()

    @stream_with_context
    def generate():
        # job_id уходит первым событием ``start`` — так клиент может отменить
        # запуск ещё до готовности первой части.
        try:
            for event in tts_service.synthesize(text, **options):
                yield _ndjson(event)
        except Exception as error:  # noqa: BLE001 — TTS не должен ронять чат
            log.warning("TTS stream failed: %s", type(error).__name__, exc_info=True)
            yield _ndjson({"type": "error", "error": f"Озвучка не удалась: {error}"})

    result = Response(generate(), mimetype="application/x-ndjson")
    result.headers["Cache-Control"] = "no-store"
    result.headers["X-RateLimit-Limit"] = str(limit)
    result.headers["X-RateLimit-Remaining"] = str(remaining)
    return result


@tts_bp.route("/api/tts/cancel", methods=["POST"])
def tts_cancel():
    """Отменяет запуск: следующая часть не будет сгенерирована и отдана."""
    data = request.get_json(silent=True) or {}
    job_id = str(data.get("job_id") or "").strip()
    if not job_id:
        return jsonify({"error": "Укажите job_id запуска озвучки"}), 400
    cancelled = tts_service.cancel_job(job_id)
    return jsonify({"ok": True, "job_id": job_id, "cancelled": cancelled})
