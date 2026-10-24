"""tts_service.py — настоящая озвучка ответов ИИ (text-to-speech).

Зачем отдельный модуль
----------------------
В NovaMind уже есть генерация аудио: ``media_generation`` умеет четыре TTS-бэкенда
(Google AI Studio, Groq Orpheus, Pollinations, свой ``AUDIO_TTS_URL``), а
``media_agent`` выбирает модель. Чату нужен другой сценарий: озвучить **готовый
текст ответа**, разбить длинный ответ на части, показать подготовку и дать
пользователю остановиться. Поэтому здесь только логика озвучки, а сами запросы
к провайдерам по-прежнему делает ``media_generation.generate_media`` — второго
набора API-клиентов и ключей в проекте нет.

Что умеет
---------
* :func:`speech_text` — превращает Markdown-ответ в текст для речи: заголовки,
  списки, цитаты, таблицы и ссылки читаются по-человечески, разметка не
  произносится, блоки кода по умолчанию пропускаются (``read_code=True``
  включает их чтение).
* :func:`split_text` — режет текст по границам предложений, не разрывая слов,
  с ограничением на размер части и число частей.
* :func:`model_catalog` / :func:`voice_catalog` / :func:`status` — реальный
  список провайдеров, моделей и голосов с честным признаком «доступен / нужен
  ключ / почему недоступен». Модели Pollinations берутся из живого каталога
  провайдера, поэтому интерфейс показывает только существующие модели, а у
  каждой модели известен её endpoint и лимит символов на запрос.
* :func:`synthesize` — генератор: отдаёт по одной части, поддерживает отмену
  между частями, повторяет временно неудачный запрос один раз.
* :class:`_Job` — реестр запусков, чтобы ``POST /api/tts/cancel`` мог
  остановить работу, а ``cleanup_media`` — удалить старые файлы.

Чего здесь нет
--------------
* Браузерной озвучки. Если ни один провайдер не настроен, API честно отвечает
  ``mode="browser"``/``available=false``, а клиент решает, использовать ли
  Web Speech API как резерв (и помечает это в интерфейсе).
* Ключей в браузере: наружу уходят только флаг доступности и список голосов.
"""

from __future__ import annotations

import base64
import html
import logging
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Iterator, Optional

import media_generation as mg

log = logging.getLogger("novamind.tts")

#: Сколько символов принимаем на один запрос озвучки.
MAX_TEXT_CHARS = int(os.getenv("TTS_MAX_CHARS", "8000"))
#: Размер одной части, отправляемой провайдеру (лимиты у сервисов разные).
CHUNK_CHARS = max(200, min(int(os.getenv("TTS_CHUNK_CHARS", "900")), 2000))
#: Больше частей за один запуск не генерируем: длинный ответ остаётся текстом.
MAX_CHUNKS = max(1, min(int(os.getenv("TTS_MAX_CHUNKS", "12")), 40))
#: Сколько ждём один запрос к провайдеру (секунды). Таймаут самого бэкенда — 180,
#: здесь ограничение на попытку, чтобы пользователь не ждал бесконечно.
REQUEST_TIMEOUT = float(os.getenv("TTS_TIMEOUT", "90"))
#: Сколько секунд живут готовые файлы озвучки до фоновой уборки.
MEDIA_TTL = float(os.getenv("TTS_FILE_TTL", "3600"))
#: Сколько последних файлов оставляем даже после TTL.
MEDIA_KEEP = int(os.getenv("TTS_FILE_KEEP", "200"))

VOICE_RU = "ru"
VOICE_EN = "en"

# ----------------------------------------------------------------------
# Провайдеры и голоса
# ----------------------------------------------------------------------
#: Голоса Gemini (prebuilt voices, мультиязычные — язык определяется по тексту).
GOOGLE_VOICES = (
    ("Kore", "Kore · спокойный женский", (VOICE_RU, VOICE_EN)),
    ("Puck", "Puck · живой мужской", (VOICE_RU, VOICE_EN)),
    ("Charon", "Charon · низкий мужской", (VOICE_RU, VOICE_EN)),
    ("Fenrir", "Fenrir · энергичный мужской", (VOICE_RU, VOICE_EN)),
    ("Aoede", "Aoede · мягкий женский", (VOICE_RU, VOICE_EN)),
    ("Leda", "Leda · молодой женский", (VOICE_RU, VOICE_EN)),
    ("Orus", "Orus · уверенный мужской", (VOICE_RU, VOICE_EN)),
    ("Zephyr", "Zephyr · светлый женский", (VOICE_RU, VOICE_EN)),
)

#: Голоса Groq Orpheus. playai-tts отключён Groq 31.12.2025, вместо него две
#: модели Orpheus: английская (autumn/diana/hannah/austin/daniel/troy) и
#: саудовская арабская (fahad/sultan/lulwa/noura). Русский текст обе читают
#: с акцентом — об этом честно сказано в подписи голоса.
GROQ_VOICES = (
    ("autumn", "Autumn · женский (en)", (VOICE_EN,)),
    ("diana", "Diana · женский (en)", (VOICE_EN,)),
    ("hannah", "Hannah · женский (en)", (VOICE_EN,)),
    ("austin", "Austin · мужской (en)", (VOICE_EN,)),
    ("daniel", "Daniel · мужской (en)", (VOICE_EN,)),
    ("troy", "Troy · мужской (en)", (VOICE_EN,)),
    ("fahad", "Fahad · мужской (ar)", ("ar",)),
    ("sultan", "Sultan · мужской (ar)", ("ar",)),
    ("lulwa", "Lulwa · женский (ar)", ("ar",)),
    ("noura", "Noura · женский (ar)", ("ar",)),
)

GROQ_VOICES_BY_MODEL = {
    "canopylabs/orpheus-v1-english": GROQ_VOICES[:6],
    "canopylabs/orpheus-arabic-saudi": GROQ_VOICES[6:],
}

#: Голоса Pollinations/OpenAI-совместимых endpoint'ов (язык — по тексту).
POLLINATIONS_VOICES = tuple(
    (voice, f"{voice} · голос OpenAI", (VOICE_RU, VOICE_EN))
    for voice in mg.POLLINATIONS_AUDIO_VOICES
)

#: Свой endpoint (AUDIO_TTS_URL) обычно OpenAI-совместим.
CUSTOM_VOICES = POLLINATIONS_VOICES

PROVIDER_LABELS = {
    "google_ai_studio": "Google AI Studio (Gemini TTS)",
    "groq": "Groq (Orpheus TTS)",
    "pollinations": "Pollinations Audio",
    "custom": "Свой TTS-endpoint (AUDIO_TTS_URL)",
}

_VOICES_BY_BACKEND = {
    "google_tts": GOOGLE_VOICES,
    "groq_tts": GROQ_VOICES,
    "pollinations_audio": POLLINATIONS_VOICES,
    "custom_tts": CUSTOM_VOICES,
}

_PROVIDER_REASON = {
    "google_ai_studio": "Нужен GEMINI_API_KEY (бесплатный тариф с лимитами).",
    "groq": "Нужен GROQ_API_KEY (бесплатный тариф с лимитами, карта не нужна).",
    "pollinations": "Нужен POLLINATIONS_API_KEY — без ключа /v1/audio/speech отвечает 401.",
    "custom": "Нужен AUDIO_TTS_URL в .env (OpenAI-совместимый /audio/speech).",
}

#: Куда идти пользователю, если провайдер ответил отказом по доступу.
_PROVIDER_ACTION = {
    "groq": ("Принять условия Orpheus в консоли Groq",
             mg.groq_terms_url("canopylabs/orpheus-v1-english")),
    "pollinations": ("Получить бесплатный ключ Pollinations", mg.POLLINATIONS_KEYS_URL),
    "google_ai_studio": ("Получить ключ Google AI Studio",
                         "https://aistudio.google.com/app/apikey"),
    "custom": ("Настроить AUDIO_TTS_URL", ""),
}


def provider_action(provider: str) -> dict:
    """Кнопка-действие для провайдера: подпись и ссылка (если есть)."""
    label, url = _PROVIDER_ACTION.get(provider, ("", ""))
    return {"label": label, "url": url}


_ISO_LANG = re.compile(r"^([a-z]{2})[-_][A-Z]{2}[-_]")


def _guess_languages(voice: str) -> list[str]:
    """Язык голоса по его id (``ru-RU-Masha`` → ``ru``), иначе «любой»."""
    match = _ISO_LANG.match(str(voice or ""))
    return [match.group(1)] if match else []


def _registry_audio_models() -> list[dict]:
    """TTS-модели из общего реестра медиа (без сетевых запросов)."""
    return [dict(item) for item in mg.MEDIA_MODEL_REGISTRY
            if item.get("media_type") == "audio" and item.get("backend") in _VOICES_BY_BACKEND]


def _normalize_pollinations_entries(entries: list[dict]) -> list[dict]:
    """Элементы живого каталога → записи моделей в формате реестра медиа."""
    models = []
    for entry in entries:
        voices = [str(voice) for voice in (entry.get("voices") or []) if voice]
        models.append({
            "id": entry["id"],
            "name": entry.get("name") or entry["id"],
            "provider": "pollinations",
            "media_type": "audio",
            "backend": "pollinations_audio",
            "pricing_status": mg.PRICING_PAID if entry.get("paid_only") else mg.PRICING_FREE,
            "description": entry.get("description") or "",
            "options": {
                "voice": (voices or list(mg.POLLINATIONS_AUDIO_VOICES))[0],
                "voices": voices or list(mg.POLLINATIONS_AUDIO_VOICES),
                "endpoint": entry.get("endpoint") or "speech",
                "aliases": entry.get("aliases") or [],
                "paid_only": bool(entry.get("paid_only")),
                "health": entry.get("health") or "",
            },
            "in_provider_catalog": True,
            "live": True,
        })
    return models


def _pollinations_models(refresh: bool = False, allow_fetch: bool = False) -> list[dict]:
    """Живой каталог TTS-моделей Pollinations в формате реестра.

    Каталог у провайдера меняется (модели, голоса, алиасы), поэтому список
    моделей в настройках берётся оттуда, а не из захардкоженного перечня.
    ``allow_fetch=False`` — только уже загруженный снимок, без сети: страница
    настроек не должна ждать провайдера.
    """
    try:
        entries = mg.pollinations_tts_catalog(force=refresh, allow_fetch=allow_fetch)
    except TypeError:            # старая сигнатура каталога
        entries = mg.pollinations_tts_catalog(force=refresh)
    except Exception as exc:  # noqa: BLE001 — каталог не должен ломать настройки
        log.warning("Pollinations TTS catalog unavailable: %s", exc)
        entries = []
    return _normalize_pollinations_entries(entries)


def _live_pollinations_model(model_id: str) -> Optional[dict]:
    """Одна модель Pollinations из живого каталога (если её нет в реестре)."""
    try:
        entry = mg.pollinations_tts_model(model_id)
    except Exception:  # noqa: BLE001 — сеть не должна ломать выбор модели
        return None
    if not entry:
        return None
    models = _normalize_pollinations_entries([entry])
    return models[0] if models else None


def _custom_model() -> Optional[dict]:
    """Своя TTS-модель, если задан AUDIO_TTS_URL."""
    endpoint = os.getenv("AUDIO_TTS_URL", "").strip()
    if not endpoint:
        return None
    return {
        "id": os.getenv("AUDIO_TTS_MODEL", "custom-tts") or "custom-tts",
        "name": "Своя TTS-модель (AUDIO_TTS_URL)",
        "provider": "custom", "media_type": "audio", "backend": "custom_tts",
        "pricing_status": mg.PRICING_UNKNOWN, "options": {"voice": "alloy"},
        "provider_name": PROVIDER_LABELS["custom"],
    }


def _all_models(refresh: bool = False, allow_fetch: bool = False) -> list[dict]:
    """Все TTS-модели: живой каталог Pollinations + реестр медиа + свой endpoint.

    Живые записи имеют приоритет, статический реестр остаётся страховкой, если
    каталог провайдера недоступен.
    """
    live = _pollinations_models(refresh=refresh, allow_fetch=allow_fetch)
    live_ids = {model["id"] for model in live}
    models = [model for model in _registry_audio_models()
              if model.get("provider") != "pollinations" or model["id"] not in live_ids]
    models.extend(live)
    custom = _custom_model()
    if custom:
        models.insert(0, custom)
    return models


def _provider_available(provider: str) -> bool:
    if provider == "pollinations":
        # Для синтеза речи Pollinations требует ключ (см. generate_pollinations_audio).
        try:
            return bool(mg._pollinations_key())
        except Exception:  # pragma: no cover — защита от смены внутренностей
            return False
    if provider == "custom":
        return bool(os.getenv("AUDIO_TTS_URL", "").strip())
    return bool(mg.provider_connected(provider))


def _ready_model(model: dict) -> dict:
    """Добавляет поля, которые проверяет media_generation.validate_selection."""
    ready = dict(model)
    provider = ready.get("provider") or ""
    ready["provider_name"] = PROVIDER_LABELS.get(provider, provider)
    ready["provider_connected"] = _provider_available(provider)
    ready.setdefault("in_provider_catalog", None)
    ready["pricing_label"] = mg.PRICING_LABELS.get(
        ready.get("pricing_status"), mg.PRICING_LABELS[mg.PRICING_UNKNOWN])
    return ready


def voices_for(model: dict) -> list[dict]:
    """Голоса конкретной TTS-модели в форме, удобной интерфейсу.

    Источник истины — сама модель: у живой модели каталога Pollinations голоса
    приходят от провайдера (``ru-RU-Masha``, ``af_nova`` и т. п.), у Groq —
    свой список для каждой модели Orpheus.
    """
    options = model.get("options") or {}
    backend = model.get("backend") or ""
    default = options.get("voice") or ""
    known = {voice[0]: voice for voice in _VOICES_BY_BACKEND.get(backend, ())}
    if backend == "groq_tts" and model.get("id") in GROQ_VOICES_BY_MODEL:
        known = {voice[0]: voice for voice in GROQ_VOICES_BY_MODEL[model["id"]]}
    explicit = [str(voice) for voice in (options.get("voices") or []) if voice]
    items = []
    if explicit:
        for voice_id in explicit:
            _, label, languages = known.get(
                voice_id, (voice_id, voice_id, _guess_languages(voice_id)))
            items.append({"id": voice_id, "label": label, "languages": list(languages),
                          "default": voice_id == default})
    else:
        for voice_id, label, languages in known.values():
            items.append({"id": voice_id, "label": label, "languages": list(languages),
                          "default": voice_id == default})
    return items


def voice_catalog(provider: str = "", model_id: str = "") -> list[dict]:
    """Голоса провайдера (или одной модели) с признаком доступности.

    Голоса разных моделей одного провайдера дублируются (у Gemini это одни и
    те же 8 голосов в двух моделях) — отдаём каждый один раз со списком моделей.
    """
    catalog: dict[tuple[str, str], dict] = {}
    for model in _all_models():
        item_provider = model.get("provider") or ""
        if provider and item_provider != provider:
            continue
        if model_id and model.get("id") != model_id:
            continue
        available = _provider_available(item_provider)
        for voice in voices_for(model):
            key = (item_provider, voice["id"])
            if key in catalog:
                catalog[key]["models"].append(model.get("id"))
                continue
            catalog[key] = {**voice,
                            "provider": item_provider,
                            "provider_name": PROVIDER_LABELS.get(item_provider, item_provider),
                            "model": model.get("id"),
                            "models": [model.get("id")],
                            "available": available}
    return list(catalog.values())


def model_catalog(provider: str = "", refresh: bool = False,
                  allow_fetch: bool = True) -> list[dict]:
    """Модели TTS (по провайдеру) для выбора в настройках озвучки.

    В отличие от :func:`provider_choices` здесь есть всё, что нужно панели:
    голоса модели, тариф, endpoint и лимит символов на запрос.
    """
    configured = settings()
    selected_model = str(configured.get("model") or "")
    selected_provider = str(configured.get("provider") or "")
    items = []
    for model in _all_models(refresh=refresh, allow_fetch=allow_fetch):
        item_provider = model.get("provider") or ""
        if provider and item_provider != provider:
            continue
        options = model.get("options") or {}
        voices = voices_for(model)
        paid = model.get("pricing_status") == mg.PRICING_PAID
        items.append({
            "id": model["id"],
            "name": model.get("name") or model["id"],
            "provider": item_provider,
            "provider_name": PROVIDER_LABELS.get(item_provider, item_provider),
            "description": model.get("description") or "",
            "available": _provider_available(item_provider),
            "pricing_status": model.get("pricing_status"),
            "pricing_label": mg.PRICING_LABELS.get(model.get("pricing_status"),
                                                   mg.PRICING_LABELS[mg.PRICING_UNKNOWN]),
            "paid": paid,
            "endpoint": options.get("endpoint") or "",
            "voices": voices,
            "voices_count": len(voices),
            "max_input_chars": int(options.get("max_input_chars") or CHUNK_CHARS),
            "health": options.get("health") or "",
            "in_provider_catalog": model.get("in_provider_catalog"),
            "selected": (model["id"] == selected_model
                         and (not selected_provider or selected_provider == item_provider)),
        })
    items.sort(key=lambda item: (item["paid"], not item["available"], item["name"].lower()))
    return items


def provider_choices(refresh: bool = False, allow_fetch: bool = False) -> list[dict]:
    """Провайдеры TTS: подключён или нет, почему и что сделать."""
    seen, items = set(), []
    models = _all_models(refresh=refresh, allow_fetch=allow_fetch)
    for model in models:
        provider = model.get("provider") or ""
        if provider in seen:
            continue
        seen.add(provider)
        available = _provider_available(provider)
        action = provider_action(provider)
        items.append({
            "id": provider,
            "name": PROVIDER_LABELS.get(provider, provider),
            "available": available,
            "reason": "" if available else _PROVIDER_REASON.get(provider, "Провайдер не настроен."),
            "action_label": action["label"],
            "action_url": action["url"],
            "models": [{"id": m["id"], "name": m.get("name", m["id"]),
                        "paid": m.get("pricing_status") == mg.PRICING_PAID}
                       for m in models if m.get("provider") == provider],
            "backend": model.get("backend"),
        })
    return items


# ----------------------------------------------------------------------
# Настройки
# ----------------------------------------------------------------------
def settings() -> dict:
    """Текущие настройки озвучки (тот же объект, что хранит config)."""
    import config
    return dict(config.speech_settings)


def save_settings(updates: Optional[dict] = None) -> dict:
    """Сохраняет только известные поля; провайдер, модель и голос проверяются."""
    import config
    payload = dict(updates or {})
    provider = str(payload.get("provider") or "").strip()
    if provider and provider not in {item["id"] for item in provider_choices()}:
        raise ValueError(f"Неизвестный TTS-провайдер: {provider}")
    model_id = str(payload.get("model") or "").strip()
    voice = str(payload.get("voice") or "").strip()
    if model_id:
        # Список моделей разрешаем обновить: пользователь мог выбрать модель,
        # которая появилась в каталоге провайдера только что.
        candidates = model_catalog(provider, allow_fetch=True)
        found = next((item for item in candidates if item["id"] == model_id), None)
        if found is None:
            raise ValueError(
                f"Модель «{model_id}» не найдена у провайдера "
                f"{PROVIDER_LABELS.get(provider, provider) or '—'}: обновите список моделей.")
        if found.get("paid") and not payload.get("confirm_paid"):
            raise ValueError(
                f"Модель «{model_id}» платная ({found.get('pricing_label')}): каждый запрос "
                "озвучки расходует кредиты провайдера. Подтвердите выбор (confirm_paid=true) "
                "или выберите бесплатную модель.")
        if not provider:
            provider = found.get("provider") or ""
            payload["provider"] = provider
        known_voices = {voice_item["id"] for voice_item in found.get("voices") or []}
        if voice and known_voices and voice not in known_voices:
            raise ValueError(f"Голос «{voice}» недоступен у модели «{model_id}».")
    elif voice:
        known = {item["id"] for item in voice_catalog(provider)}
        if voice not in known:
            raise ValueError(f"Голос «{voice}» недоступен у выбранного провайдера")
    lang = str(payload.get("lang") or "").strip().lower()
    if lang and lang not in {"ru", "en", "auto"}:
        raise ValueError("Язык озвучки: ru, en или auto")
    safe = {key: payload[key] for key in ("auto_speak", "read_code", "provider", "model", "voice", "lang")
            if key in payload}
    if safe.get("lang") == "auto":
        safe["lang"] = ""
    return config.save_speech_settings(safe)


def _find_model(models: list[dict], model_id: str, provider: str = "") -> Optional[dict]:
    """Ищет модель по id или алиасу провайдера."""
    for model in models:
        if model.get("id") != model_id:
            continue
        if provider and model.get("provider") != provider:
            continue
        return model
    for model in models:
        aliases = (model.get("options") or {}).get("aliases") or []
        if model_id in aliases and (not provider or model.get("provider") == provider):
            return model
    return None


def resolve_model(provider: str = "", model_id: str = "") -> tuple[Optional[dict], str]:
    """Выбирает TTS-модель: явная настройка → выбранный провайдер → любой доступный."""
    configured = settings()
    provider = (provider or configured.get("provider") or "").strip()
    model_id = (model_id or configured.get("model") or "").strip()
    models = _all_models()

    if model_id:
        model = _find_model(models, model_id, provider)
        if model is None:
            # Модель могла появиться в живом каталоге после старта процесса.
            live = _live_pollinations_model(model_id)
            if live and (not provider or live["provider"] == provider):
                model = live
        if model is None:
            hint = f" у провайдера {PROVIDER_LABELS.get(provider, provider)}" if provider else ""
            return None, (f"TTS-модель «{model_id}» не найдена{hint}. "
                          "Обновите список моделей в настройках озвучки.")
        if not _provider_available(model.get("provider") or ""):
            reason = _PROVIDER_REASON.get(model.get("provider"), "Провайдер не настроен.")
            return None, f"{PROVIDER_LABELS.get(model.get('provider'), '')}: {reason}"
        return model, ""

    order = [item for item in (provider, configured.get("provider") or "") if item]
    order += [item["id"] for item in provider_choices()]
    seen = set()
    for candidate in order:
        if candidate in seen:
            continue
        seen.add(candidate)
        for model in models:
            if model.get("provider") != candidate:
                continue
            if _provider_available(candidate):
                return model, ""
            break
    reasons = [f"{PROVIDER_LABELS.get(item['id'], item['id'])}: {item['reason']}"
               for item in provider_choices() if not item["available"]]
    return None, ("Озвучка не настроена. " + " ".join(reasons)) if reasons else "TTS-провайдер не найден."


def explain_error(message: str, model: Optional[dict] = None) -> dict:
    """Человеческое объяснение ошибки провайдера: что именно сделать.

    Возвращает ``{"hint": str, "action_url": str}``. Дословный ответ провайдера
    остаётся в тексте ошибки — интерфейс показывает и его, и подсказку.
    """
    text = str(message or "")
    low = text.lower()
    model = model or {}
    provider = model.get("provider") or ""
    hint, url = "", ""
    if "model_terms_required" in low or "requires terms acceptance" in low:
        hint = ("Модель Groq нужно один раз активировать: откройте ссылку, войдите в свою "
                "организацию, нажмите «Accept terms» и повторите озвучку.")
        url = mg.groq_terms_url(model.get("id") or "canopylabs/orpheus-v1-english")
    elif "is a text model" in low or "text endpoint" in low:
        hint = ("Эта модель синтезирует речь только через /v1/chat/completions. "
                "Обновите список моделей озвучки — правильный endpoint подставится сам.")
    elif provider and ("401" in low or "unauthorized" in low
                       or "invalid api key" in low or "нужен pollinations_api_key" in low):
        hint = ("Ключ провайдера не принят или отсутствует. "
                f"{_PROVIDER_REASON.get(provider, '')}").strip()
        url = provider_action(provider).get("url", "")
    elif "429" in low or "rate limit" in low or "too many requests" in low:
        hint = ("Провайдер ограничил частоту запросов. Groq на бесплатном тарифе даёт "
                "10 запросов в минуту — подождите минуту и повторите.")
    elif "deprecated" in low or "playai" in low or "not found" in low or "не найдена" in low:
        hint = "Обновите список моделей и выберите модель, которая есть в каталоге провайдера."
    return {"hint": hint, "action_url": url}


def status(refresh: bool = False) -> dict:
    """Полное состояние озвучки для интерфейса (без секретов).

    ``refresh=True`` — перечитать каталоги провайдеров (кнопка «Проверить»).
    """
    configured = settings()
    model, error = resolve_model(configured.get("provider") or "", configured.get("model") or "")
    provider = (model or {}).get("provider") or configured.get("provider") or ""
    voices = voice_catalog(provider)
    default_voice = ""
    if model:
        default_voice = configured.get("voice") or (model.get("options") or {}).get("voice") or ""
    explanation = explain_error(error, model) if error else {"hint": "", "action_url": ""}
    catalog_error = ""
    if provider == "pollinations":
        try:
            catalog_error = getattr(mg, "_POLL_TTS_CACHE", {}).get("error") or ""
        except Exception:  # pragma: no cover — диагностика не обязательна
            catalog_error = ""
    return {
        "available": model is not None,
        "mode": "ai" if model else "browser",
        "provider": (model or {}).get("provider", ""),
        "provider_name": PROVIDER_LABELS.get((model or {}).get("provider", ""), ""),
        "model": (model or {}).get("id", ""),
        "model_name": (model or {}).get("name", ""),
        "voice": default_voice,
        "voices": voices,
        "models": model_catalog(provider, refresh=refresh, allow_fetch=refresh),
        "providers": provider_choices(refresh=refresh, allow_fetch=refresh),
        "settings": configured,
        "limits": {"max_chars": MAX_TEXT_CHARS, "chunk_chars": CHUNK_CHARS,
                   "max_chunks": MAX_CHUNKS, "timeout": REQUEST_TIMEOUT},
        "error": error,
        "hint": explanation["hint"],
        "action_url": explanation["action_url"],
        "catalog_error": catalog_error,
        "fallback": "browser" if model is None else "",
    }


# ----------------------------------------------------------------------
# Подготовка текста
# ----------------------------------------------------------------------
_FENCE = re.compile(r"```([\w+#.-]*)[^\n]*\n(.*?)```", re.S)
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_IMAGE = re.compile(r"!\[[^\]]*\]\((?:[^)]*)\)")
_LINK = re.compile(r"\[([^\]]+)\]\((?:[^)]*)\)")
_BARE_URL = re.compile(r"https?://\S+")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*", re.M)
_BULLET = re.compile(r"^\s{0,3}[-*+•]\s+", re.M)
_NUMBERED = re.compile(r"^\s{0,3}(\d{1,2})[.)]\s+", re.M)
_QUOTE = re.compile(r"^\s{0,3}>\s?", re.M)
_TABLE_SEP = re.compile(r"^\s*\|?[\s:|-]+\|[\s:|-]*$", re.M)
_EMPHASIS = re.compile(r"(\*\*|__|\*|_|~~)")
_FOOTNOTE = re.compile(r"\[(\d{1,2})\]")
_HR = re.compile(r"^\s{0,3}([-*_])\s*(?:\1\s*){2,}$", re.M)
_HTML_TAG = re.compile(r"<[^>]{1,200}>")
_SPACES = re.compile(r"[ \t\u00a0]+")
_BLANKS = re.compile(r"\n{3,}")


def _clean_inline(text: str) -> str:
    text = _IMAGE.sub("", text)
    text = _LINK.sub(r"\1", text)
    text = _BARE_URL.sub("ссылка", text)
    text = _INLINE_CODE.sub(r"\1", text)
    text = _EMPHASIS.sub("", text)
    text = _FOOTNOTE.sub("", text)
    text = _HTML_TAG.sub("", text)
    return html.unescape(text)


def _table_to_speech(block: str) -> str:
    rows = []
    for line in block.strip().splitlines():
        if not line.strip() or _TABLE_SEP.match(line):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        cells = [cell for cell in cells if cell]
        if cells:
            rows.append(" — ".join(cells))
    return ". ".join(rows)


def speech_text(text: str, read_code: bool = False, max_chars: int = MAX_TEXT_CHARS) -> str:
    """Ответ ИИ → текст, который не стыдно прочитать вслух.

    Markdown-разметка убирается, ссылки не произносятся, таблицы читаются
    построчно, блоки кода по умолчанию пропускаются. Исходное сообщение в чате
    не меняется — это только подготовка для синтеза.
    """
    raw = str(text or "")
    if not raw.strip():
        return ""

    def fence(match: re.Match) -> str:
        code = match.group(2) or ""
        if not read_code:
            return "\n\n"
        # Код вслух — по строкам, без символов разметки.
        lines = [line.strip() for line in code.splitlines() if line.strip()]
        return "\n\n" + ". ".join(lines) + ".\n\n"

    raw = _FENCE.sub(fence, raw)
    raw = _HR.sub("\n", raw)
    raw = _QUOTE.sub("", raw)
    raw = _HEADING.sub("", raw)

    # Таблицы — до построчной обработки, иначе разделители станут текстом.
    def table(match: re.Match) -> str:
        return "\n\n" + _table_to_speech(match.group(0)) + "\n\n"
    raw = re.sub(r"(?m)(^\s*\|.*\|\s*$\n?)+", table, raw)

    raw = _BULLET.sub("", raw)
    raw = _NUMBERED.sub(r"\1) ", raw)
    raw = _clean_inline(raw)
    raw = _SPACES.sub(" ", raw)

    lines = []
    for line in raw.split("\n"):
        cleaned = line.strip(" \t-—")
        if not cleaned:
            lines.append("")
            continue
        # Пункт списка без точки на конце — добавляем, чтобы речь не слипалась.
        if cleaned[-1] not in ".!?;:…»\"')":
            cleaned += "."
        lines.append(cleaned)
    result = "\n".join(lines)
    result = _BLANKS.sub("\n\n", result)
    # Абзацы и пункты списка разделяем паузой, а не склейкой.
    result = re.sub(r"\n+", " ", result)
    result = _SPACES.sub(" ", result).strip()
    result = re.sub(r"\s+([.,!?;:])", r"\1", result)
    result = re.sub(r"([.,!?;:])(?:\s*\1)+", r"\1", result)
    return result[:max_chars].strip()


_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+")
_SOFT_BREAK = re.compile(r"(?<=[,;:—])\s+")


def split_text(text: str, limit: int = CHUNK_CHARS, max_chunks: int = MAX_CHUNKS) -> list[str]:
    """Делит текст на части по границам предложений, не разрывая слов."""
    text = _SPACES.sub(" ", str(text or "")).strip()
    if not text:
        return []
    limit = max(40, int(limit))  # ниже 40 символов голос звучит рвано
    sentences = [item for item in _SENTENCE_END.split(text) if item.strip()]
    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        parts = [sentence]
        if len(sentence) > limit:
            # Одно длинное предложение: сначала по запятым, потом по словам.
            parts, buffer = [], ""
            for piece in _SOFT_BREAK.split(sentence):
                if len(piece) > limit:
                    words, word_buffer = [], ""
                    for word in piece.split(" "):
                        if len(word_buffer) + len(word) + 1 > limit and word_buffer:
                            words.append(word_buffer)
                            word_buffer = word
                        else:
                            word_buffer = f"{word_buffer} {word}".strip()
                    if word_buffer:
                        words.append(word_buffer)
                    parts.extend(words)
                    continue
                if len(buffer) + len(piece) + 1 > limit and buffer:
                    parts.append(buffer)
                    buffer = piece
                else:
                    buffer = f"{buffer} {piece}".strip()
            if buffer:
                parts.append(buffer)
        for part in parts:
            part = part.strip()
            if not part:
                continue
            if len(current) + len(part) + 1 <= limit:
                current = f"{current} {part}".strip()
            else:
                if current:
                    chunks.append(current)
                current = part
    if current:
        chunks.append(current)
    return chunks[:max_chunks]


# ----------------------------------------------------------------------
# Запуски и отмена
# ----------------------------------------------------------------------
@dataclass
class _Job:
    """Один запуск озвучки: нужен для отмены и диагностики."""

    id: str
    cancel: threading.Event = field(default_factory=threading.Event)
    created: float = field(default_factory=time.time)
    chunks_total: int = 0
    chunks_done: int = 0
    state: str = "running"      # running | done | cancelled | error


_JOBS: dict[str, _Job] = {}
_JOBS_LOCK = threading.Lock()
JOB_TTL = 900.0


def _register_job(chunks: int = 0) -> _Job:
    job = _Job(id=uuid.uuid4().hex[:12], chunks_total=chunks)
    with _JOBS_LOCK:
        _JOBS[job.id] = job
        if len(_JOBS) > 200:                     # не копим реестр
            for key in sorted(_JOBS, key=lambda k: _JOBS[k].created)[:50]:
                _JOBS.pop(key, None)
    return job


def get_job(job_id: str) -> Optional[_Job]:
    with _JOBS_LOCK:
        return _JOBS.get(job_id or "")


def cancel_job(job_id: str) -> bool:
    """Просит остановить запуск; генератор увидит флаг между частями."""
    job = get_job(job_id)
    if not job:
        return False
    job.cancel.set()
    job.state = "cancelled"
    return True


def _prune_jobs() -> None:
    edge = time.time() - JOB_TTL
    with _JOBS_LOCK:
        for key in [k for k, job in _JOBS.items() if job.created < edge]:
            _JOBS.pop(key, None)


# ----------------------------------------------------------------------
# Синтез
# ----------------------------------------------------------------------
_TTS_PREFIXES = ("gemini_tts_", "groq_tts_", "pollinations_tts_", "custom_tts_")


def cleanup_media(max_age: float = MEDIA_TTL, keep: int = MEDIA_KEEP) -> int:
    """Удаляет старые файлы озвучки. Возвращает число удалённых файлов.

    Трогаем только файлы, созданные TTS-бэкендами: картинки, видео и загрузки
    пользователя не удаляются.
    """
    try:
        directory = mg.ensure_media_dir()
    except Exception:  # pragma: no cover — каталог недоступен, уборка не критична
        return 0
    edge = time.time() - max_age
    removed = 0
    try:
        names = sorted(
            (os.path.join(directory, name) for name in os.listdir(directory)
             if name.startswith(_TTS_PREFIXES)),
            key=lambda path: os.path.getmtime(path),
            reverse=True,
        )
    except OSError:
        return 0
    for index, path in enumerate(names):
        try:
            if index >= keep or os.path.getmtime(path) < edge:
                os.remove(path)
                removed += 1
        except OSError:
            continue
    return removed


_TRANSIENT = ("таймаут", "timeout", "timed out", "connection", "network",
              "временно", "502", "503", "504", "429")


def _is_transient(message: str) -> bool:
    low = str(message or "").lower()
    return any(marker in low for marker in _TRANSIENT)


def synthesize(text: str, *, voice: str = "", provider: str = "", model_id: str = "",
               read_code: bool = False, lang: str = "",
               on_status: Optional[Callable[[str], None]] = None,
               cancel: Optional[threading.Event] = None,
               retries: int = 1) -> Iterator[dict]:
    """Генерирует озвучку и отдаёт события по мере готовности частей.

    События (dict):
      ``{"type": "start", ...}`` — провайдер, модель, голос, число частей;
      ``{"type": "chunk", "index", "url", "mime", "size", "text"}``;
      ``{"type": "chunk_error", "index", "error"}`` — часть не озвучена;
      ``{"type": "error", "error"}`` — озвучка не удалась целиком;
      ``{"type": "cancelled"}``; ``{"type": "done", "chunks": n}``.

    Отмена проверяется перед каждой частью и после запроса: уже отправленный
    запрос к провайдеру прервать нельзя (HTTP-клиент синхронный), но его
    результат не будет отдан клиенту.
    """
    prepared = speech_text(text, read_code=read_code)
    if not prepared:
        yield {"type": "error", "error": "Нечего озвучивать: в ответе нет текста."}
        return
    if len(prepared) > MAX_TEXT_CHARS:
        prepared = prepared[:MAX_TEXT_CHARS]

    resolved, error = resolve_model(provider, model_id)
    if not resolved:
        explanation = explain_error(error)
        yield {"type": "error", "error": error, "mode": "browser",
               "hint": explanation["hint"], "action_url": explanation["action_url"]}
        return

    resolved = _ready_model(resolved)
    options = resolved.get("options") or {}
    # У провайдеров разные лимиты на один запрос: Orpheus принимает 200 символов,
    # Gemini — до 900. Режем по лимиту модели, иначе провайдер отвечает 400.
    limit = max(40, min(CHUNK_CHARS, int(options.get("max_input_chars") or CHUNK_CHARS)))
    max_chunks = MAX_CHUNKS if limit >= 600 else min(40, MAX_CHUNKS * 3)
    chunks = split_text(prepared, limit=limit, max_chunks=max_chunks)
    if not chunks:
        yield {"type": "error", "error": "Не удалось разбить текст для озвучки."}
        return
    voice_id = voice or (settings().get("voice") or "") or options.get("voice") or ""
    voices = {item["id"] for item in voices_for(resolved)}
    if voice_id and voices and voice_id not in voices:
        # Голос мог остаться от другой модели — молча подставляем голос модели.
        voice_id = options.get("voice") or next(iter(voices))
    detected = lang or detect_language(prepared)
    # Пользователь выбрал модель сам в настройках — это и есть явное подтверждение
    # тарифа (платную модель нельзя сохранить без confirm_paid).
    confirm = {"paid": True, "trial": True, "unknown": True}

    job = _register_job(len(chunks))
    if cancel is not None and cancel.is_set():
        job.cancel.set()
    yield {"type": "start", "job_id": job.id, "provider": resolved.get("provider"),
           "provider_name": PROVIDER_LABELS.get(resolved.get("provider", ""), ""),
           "model": resolved.get("id"), "model_name": resolved.get("name"),
           "voice": voice_id, "lang": detected, "chunks": len(chunks),
           "mode": "ai"}

    produced = 0
    for index, chunk in enumerate(chunks):
        if job.cancel.is_set() or (cancel is not None and cancel.is_set()):
            job.state = "cancelled"
            yield {"type": "cancelled", "index": index, "chunks": produced}
            return
        if on_status:
            try:
                on_status(f"Озвучиваю часть {index + 1} из {len(chunks)}…")
            except Exception:  # noqa: BLE001 — колбэк не должен ломать синтез
                pass

        result, error = None, ""
        attempts = 0
        while attempts <= retries:
            attempts += 1
            result, error = mg.generate_media(
                resolved, chunk, options={"voice": voice_id} if voice_id else {},
                confirm=confirm,
            )
            if result or not _is_transient(error):
                break
            log.warning("TTS chunk %s failed (attempt %s): %s", index, attempts, error)

        if job.cancel.is_set():
            job.state = "cancelled"
            yield {"type": "cancelled", "index": index, "chunks": produced}
            return
        if not result:
            if index == 0:
                job.state = "error"
                explanation = explain_error(error, resolved)
                yield {"type": "error", "error": f"Озвучка не удалась: {error}",
                       "hint": explanation["hint"],
                       "action_url": explanation["action_url"],
                       "provider": resolved.get("provider"),
                       "model": resolved.get("id")}
                return
            explanation = explain_error(error, resolved)
            yield {"type": "chunk_error", "index": index, "error": str(error),
                   "hint": explanation["hint"], "action_url": explanation["action_url"]}
            continue
        if not str(result.get("mime", "")).startswith("audio/"):
            # Провайдер вернул не аудио — не подсовываем это плееру.
            yield {"type": "chunk_error", "index": index,
                   "error": f"Провайдер вернул не аудио ({result.get('mime') or 'unknown'})"}
            continue
        produced += 1
        job.chunks_done = produced
        yield {"type": "chunk", "index": index, "url": result.get("url"),
               "filename": result.get("filename"), "mime": result.get("mime"),
               "size": result.get("size"), "text": chunk,
               "model": resolved.get("id"), "provider": resolved.get("provider")}

    job.state = "done"
    yield {"type": "done", "chunks": produced, "total": len(chunks), "job_id": job.id}


_CYR = re.compile(r"[А-Яа-яЁё]")
_LAT = re.compile(r"[A-Za-z]")


def detect_language(text: str) -> str:
    """ru/en по составу текста — только чтобы выбрать голос, не для перевода."""
    cyr, lat = len(_CYR.findall(str(text or ""))), len(_LAT.findall(str(text or "")))
    return VOICE_RU if cyr >= lat else VOICE_EN


def synthesize_all(text: str, **kwargs) -> tuple[list[dict], str]:
    """Синхронный вариант для тестов и простых клиентов: (части, ошибка)."""
    chunks, error = [], ""
    for event in synthesize(text, **kwargs):
        if event["type"] == "chunk":
            chunks.append(event)
        elif event["type"] in {"error", "cancelled"}:
            error = event.get("error") or "Озвучка отменена."
            break
    return chunks, error


def decode_data_url(value: str) -> tuple[bytes, str]:
    """Утилита для офлайн-тестов: data-URL → (bytes, mime)."""
    payload = str(value or "")
    mime = ""
    if payload.startswith("data:"):
        header, _, payload = payload.partition(",")
        mime = header[5:].split(";")[0].strip()
    return base64.b64decode(payload + "=" * ((4 - len(payload) % 4) % 4)), mime
