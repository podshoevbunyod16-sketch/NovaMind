"""routes/ops.py — эксплуатационные маршруты для новых подсистем.

Один blueprint на то, что появилось в аудите 2026-10-28:

* ``/api/knowledge/*`` — RAG-база знаний: список документов, загрузка (текст
  или файл), поиск, удаление, статистика;
* ``/api/prompts/*`` — версии промптов: список, история, diff, закрепление;
* ``/api/agent/trace/<id>``, ``/api/agent/runs/<run_id>/trace`` — сквозная
  трассировка хода;
* ``/api/ops/metrics``, ``/api/ops/health`` — сводка по трассам и состояние
  всех новых подсистем.

Права: знания и трассы принадлежат владельцу (``identity.chat_owner``), админ
видит всё; запись знаний требует входа (гостю нечего индексировать), а
закрепление версии промпта — только админ. Никакой маршрут не отдаёт чужие
данные: фильтр по владельцу стоит в самом запросе к базе.
"""
from __future__ import annotations

import logging
import os
import tempfile

from flask import Blueprint, jsonify, request, session

import identity
from routes.terminal import is_admin, signed_in

ops_bp = Blueprint("ops", __name__)
log = logging.getLogger("novamind.ops")

MAX_UPLOAD_MB = int(os.getenv("KNOWLEDGE_MAX_FILE_MB", "25"))


# ── общие помощники ──────────────────────────────────────────────────────
def _owner() -> str:
    """Владелец данных: та же строка, что у чатов и рабочей папки."""
    try:
        return identity.chat_owner() or ""
    except Exception:  # noqa: BLE001 — вне запроса владельца нет
        return ""


def _any_owner() -> bool:
    """Админ видит документы всех пользователей."""
    return bool(is_admin())


def _knowledge():
    import knowledge
    return knowledge.default_base()


def _forbidden(message="Нужен вход"):
    return jsonify({"ok": False, "error": message}), 403


# ── база знаний ──────────────────────────────────────────────────────────
@ops_bp.get("/api/knowledge/stats")
def knowledge_stats():
    try:
        stats = _knowledge().stats(owner=_owner(), any_owner=_any_owner())
    except Exception as error:  # noqa: BLE001 — не ломаем интерфейс
        log.warning("knowledge stats failed: %s", error)
        return jsonify({"ok": False, "error": "База знаний недоступна"}), 500
    return jsonify({"ok": True, **stats, "signed_in": signed_in()})


@ops_bp.get("/api/knowledge/documents")
def knowledge_documents():
    try:
        documents = _knowledge().documents(owner=_owner(), any_owner=_any_owner())
    except Exception as error:  # noqa: BLE001
        log.warning("knowledge documents failed: %s", error)
        return jsonify({"ok": False, "error": "База знаний недоступна"}), 500
    return jsonify({"ok": True, "documents": documents, "count": len(documents)})


@ops_bp.post("/api/knowledge/add")
def knowledge_add():
    """Индексирует текст из JSON: ``{title, text, tags}``."""
    if not signed_in():
        return _forbidden("Индексировать документы может только вошедший пользователь")
    data = request.get_json(silent=True) or {}
    title = str(data.get("title") or "").strip()
    text = str(data.get("text") or "")
    if not text.strip():
        return jsonify({"ok": False, "error": "Пустой текст документа"}), 400
    try:
        result = _knowledge().ingest_text(
            title or "Без названия", text, tags=str(data.get("tags") or ""),
            owner=_owner(), source=str(data.get("source") or "api"))
    except ValueError as error:
        return jsonify({"ok": False, "error": str(error)}), 400
    except Exception as error:  # noqa: BLE001
        log.exception("knowledge add failed")
        return jsonify({"ok": False, "error": f"Не удалось проиндексировать: {error}"}), 500
    return jsonify({"ok": True, "document": result})


@ops_bp.post("/api/knowledge/upload")
def knowledge_upload():
    """Индексирует загруженный файл (PDF, DOCX, XLSX, MD, TXT, CSV, JSON)."""
    if not signed_in():
        return _forbidden("Индексировать документы может только вошедший пользователь")
    upload = request.files.get("file")
    if upload is None or not upload.filename:
        return jsonify({"ok": False, "error": "Нет файла в поле «file»"}), 400
    filename = os.path.basename(upload.filename)
    suffix = os.path.splitext(filename)[1].lower()
    size = int(request.content_length or 0)
    if size > MAX_UPLOAD_MB * 1024 * 1024:
        return jsonify({"ok": False,
                        "error": f"Файл больше {MAX_UPLOAD_MB} МБ"}), 413
    handle, path = tempfile.mkstemp(suffix=suffix or ".txt")
    try:
        with os.fdopen(handle, "wb") as target:
            upload.save(target)
        result = _knowledge().ingest_file(
            path, title=str(request.form.get("title") or filename),
            owner=_owner(), tags=str(request.form.get("tags") or ""))
        result["title"] = str(request.form.get("title") or filename)
    except ValueError as error:
        return jsonify({"ok": False, "error": str(error)}), 400
    except Exception as error:  # noqa: BLE001
        log.exception("knowledge upload failed")
        return jsonify({"ok": False, "error": f"Не удалось проиндексировать: {error}"}), 500
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    return jsonify({"ok": True, "document": result})


@ops_bp.post("/api/knowledge/search")
def knowledge_search():
    data = request.get_json(silent=True) or {}
    query = str(data.get("query") or data.get("q") or "").strip()
    if not query:
        return jsonify({"ok": False, "error": "Нужен запрос"}), 400
    try:
        limit = max(1, min(int(data.get("limit") or 5), 20))
    except (TypeError, ValueError):
        limit = 5
    hits = _knowledge().search(query, limit=limit, owner=_owner(), any_owner=_any_owner())
    return jsonify({"ok": True, "query": query, "results": [hit.as_dict() for hit in hits],
                    "count": len(hits)})


@ops_bp.delete("/api/knowledge/documents/<int:document_id>")
def knowledge_delete(document_id: int):
    if not signed_in():
        return _forbidden("Удалять документы может только вошедший пользователь")
    removed = _knowledge().delete(document_id, owner=_owner(), any_owner=_any_owner())
    if not removed:
        return jsonify({"ok": False, "error": "Документ не найден"}), 404
    return jsonify({"ok": True, "deleted": document_id})


# ── промпты ──────────────────────────────────────────────────────────────
@ops_bp.get("/api/prompts")
def prompts_list():
    import prompt_registry
    if request.args.get("snapshot") == "1" and is_admin():
        prompt_registry.ensure()
    return jsonify({"ok": True, "prompts": prompt_registry.list_prompts(),
                    "summary": prompt_registry.summary()})


@ops_bp.get("/api/prompts/<name>")
def prompts_detail(name: str):
    import prompt_registry
    if not is_admin():
        # Текст системного промпта — служебные данные. В чате их защищает
        # ограждение, значит и API не должен отдавать их анонимному гостю.
        return _forbidden("Текст промпта может смотреть только администратор")
    try:
        entry = prompt_registry.record_use(name)
        versions = prompt_registry.history(name)
        text = prompt_registry.load(name)
    except ValueError as error:
        return jsonify({"ok": False, "error": str(error)}), 400
    except FileNotFoundError:
        return jsonify({"ok": False, "error": "Промпт не найден"}), 404
    return jsonify({"ok": True, "name": name, "current": entry,
                    "versions": versions, "text": text})


@ops_bp.get("/api/prompts/<name>/diff")
def prompts_diff(name: str):
    import prompt_registry
    if not is_admin():
        return _forbidden("Диффы промптов может смотреть только администратор")
    old = str(request.args.get("a") or "").strip()
    new = str(request.args.get("b") or "").strip()
    if not old:
        return jsonify({"ok": False, "error": "Нужен параметр a (версия)"}), 400
    try:
        return jsonify({"ok": True, "diff": prompt_registry.diff(name, old, new)})
    except ValueError as error:
        return jsonify({"ok": False, "error": str(error)}), 400
    except FileNotFoundError:
        return jsonify({"ok": False, "error": "Версия не найдена в архиве"}), 404


@ops_bp.post("/api/prompts/<name>/pin")
def prompts_pin(name: str):
    import prompt_registry
    if not is_admin():
        return _forbidden("Закреплять версию промпта может только администратор")
    data = request.get_json(silent=True) or {}
    version = str(data.get("version") or "").strip()
    try:
        if not version or version in ("current", "active", "latest"):
            info = prompt_registry.snapshot(name)
            version = info["version"]
        return jsonify({"ok": True, **prompt_registry.pin(name, version)})
    except (ValueError, FileNotFoundError) as error:
        return jsonify({"ok": False, "error": str(error)}), 400


@ops_bp.delete("/api/prompts/<name>/pin")
def prompts_unpin(name: str):
    import prompt_registry
    if not is_admin():
        return _forbidden("Снимать закрепление может только администратор")
    try:
        return jsonify({"ok": True, **prompt_registry.unpin(name)})
    except ValueError as error:
        return jsonify({"ok": False, "error": str(error)}), 400


# ── трассы и метрики ─────────────────────────────────────────────────────
def _trace_allowed(record: dict) -> bool:
    """Трассу видит владелец чата или админ."""
    if is_admin():
        return True
    chat_id = str((record.get("attributes") or {}).get("chat_id") or "")
    if not chat_id:
        return False
    try:
        import database
        return bool(database.chat_allowed(chat_id))
    except Exception:  # noqa: BLE001
        return False


@ops_bp.get("/api/agent/trace/<trace_id>")
def agent_trace(trace_id: str):
    import telemetry
    record = telemetry.get_trace(trace_id)
    if not record:
        return jsonify({"ok": False, "error": "Трасса не найдена"}), 404
    if not _trace_allowed(record):
        return jsonify({"ok": False, "error": "Трасса не найдена"}), 404
    return jsonify({"ok": True, "trace": record})


@ops_bp.get("/api/agent/runs/<run_id>/trace")
def agent_run_trace(run_id: str):
    import telemetry
    record = telemetry.find_by_run(run_id)
    if not record:
        return jsonify({"ok": False, "error": "Трасса не найдена"}), 404
    if not _trace_allowed(record):
        return jsonify({"ok": False, "error": "Трасса не найдена"}), 404
    return jsonify({"ok": True, "trace": record})


@ops_bp.get("/api/ops/traces")
def ops_traces():
    import telemetry
    if not is_admin():
        return _forbidden("Список трасс доступен администратору")
    try:
        limit = max(1, min(int(request.args.get("limit") or 20), 100))
    except (TypeError, ValueError):
        limit = 20
    return jsonify({"ok": True, "traces": telemetry.recent(limit)})


@ops_bp.get("/api/ops/metrics")
def ops_metrics():
    import telemetry
    if not is_admin() and not signed_in():
        return _forbidden("Метрики доступны вошедшим пользователям")
    try:
        limit = max(1, min(int(request.args.get("limit") or 100), 500))
    except (TypeError, ValueError):
        limit = 100
    return jsonify({"ok": True, "metrics": telemetry.metrics(limit),
                    "session": {"admin": bool(is_admin())}})


@ops_bp.get("/api/ops/health")
def ops_health():
    """Состояние всех новых подсистем одним ответом."""
    import capabilities
    import guardrails
    import prompt_registry
    import telemetry
    try:
        knowledge_stats_data = _knowledge().stats(owner=_owner(),
                                                  any_owner=_any_owner())
    except Exception:  # noqa: BLE001
        knowledge_stats_data = {"ready": False, "error": "недоступна"}
    try:
        import agenticseek.orchestration as orchestration
        orchestration_data = orchestration.summary()
    except Exception:  # noqa: BLE001
        orchestration_data = {}
    return jsonify({
        "ok": True,
        "guardrails": guardrails.summary(),
        "capabilities": capabilities.summary(),
        "knowledge": knowledge_stats_data,
        "prompts": prompt_registry.summary(),
        "telemetry": telemetry.summary(),
        "orchestration": orchestration_data,
        "session": {"signed_in": signed_in(), "admin": bool(is_admin())},
    })
