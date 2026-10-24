"""
media_generation.py — единый каталог и движок генерации медиа (image / audio / video).

Принципы:
  * В каталоге остаются только модели подключённых провайдеров.
  * Статус цены берётся из данных провайдера (OpenRouter отдаёт pricing в /models)
    либо из проверенного источника (ссылка + дата проверки хранятся в записи).
  * pricing_status:
      "free"    — подтверждённый бесплатный API (бесплатный тариф, ключ не обязателен);
      "trial"   — доступ только за счёт пробных кредитов (обозначается отдельно);
      "paid"    — платная модель (автоматически НЕ используется);
      "unknown" — цену подтвердить не удалось (автоматически НЕ используется).
  * Никакого выдуманного прогресса: статусы — это реальные шаги запроса,
    ошибки провайдера возвращаются дословно вместе с HTTP-кодом.

Модуль не импортирует Flask, поэтому его можно тестировать напрямую.
"""
import base64
import io
import json
import os
import re
import threading
import time
import uuid
import wave
from concurrent.futures import ThreadPoolExecutor

import requests

# ---------- Пути ----------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MEDIA_DIR = os.getenv("NOVAMIND_MEDIA_DIR") or os.path.join(BASE_DIR, "generated_media")
IMAGE_DIR = os.getenv("NOVAMIND_IMAGE_DIR") or os.path.join(BASE_DIR, "generated_images")

# ---------- Статусы цены ----------
PRICING_FREE = "free"
PRICING_TRIAL = "trial"
PRICING_PAID = "paid"
PRICING_UNKNOWN = "unknown"

PRICING_LABELS = {
    PRICING_FREE: "Бесплатный API",
    PRICING_TRIAL: "Пробные кредиты",
    PRICING_PAID: "Платная модель",
    PRICING_UNKNOWN: "Цена не подтверждена",
}

# Только такие модели выбираются автоматически (без явного подтверждения).
AUTO_USABLE_STATUSES = (PRICING_FREE,)

MEDIA_TYPES = ("image", "audio", "video")

# ---------- Источники данных о ценах ----------
# Статусы ниже сверены с официальной страницей цен Gemini API 2026-09-26.
GOOGLE_PRICING_SOURCE = "https://ai.google.dev/gemini-api/docs/pricing"
GROQ_MODELS_SOURCE = "https://console.groq.com/docs/models"
POLLINATIONS_SOURCE = "https://github.com/pollinations/pollinations"
PRICING_VERIFIED_AT = "2026-10-24"

# ---------- Адреса TTS-провайдеров ----------
#: База Pollinations (можно переопределить своим прокси в .env).
POLLINATIONS_BASE_URL = (os.getenv("POLLINATIONS_BASE_URL") or "https://gen.pollinations.ai").rstrip("/")
#: Groq Orpheus: тот же OpenAI-совместимый endpoint /audio/speech.
GROQ_TTS_URL = "https://api.groq.com/openai/v1/audio/speech"
#: Orpheus принимает максимум 200 символов за запрос (документация Groq).
GROQ_TTS_MAX_INPUT = 200
#: Условия модели Groq нужно принять один раз в консоли — иначе 400 model_terms_required.
GROQ_TERMS_URL = "https://console.groq.com/playground?model={model}"
#: Ключ Pollinations (без него /v1/audio/speech отвечает 401).
POLLINATIONS_KEYS_URL = "https://enter.pollinations.ai/keys"

#: Голоса OpenAI-совместимых моделей Pollinations/своего endpoint.
POLLINATIONS_AUDIO_VOICES = ("alloy", "echo", "fable", "onyx", "nova", "shimmer")

#: Что точно не является озвучкой речи, хотя отдаёт audio на /v1/audio/speech.
_NON_SPEECH_TTS_MARKERS = (
    "music", "sfx", "sound-effect", "sound_effect", "stable-audio", "lyria",
    "stem-separation", "voice-isolator", "voice-changer", "speech-to-speech",
)

_MIME_BY_AUDIO_FORMAT = {
    "mp3": "audio/mpeg", "wav": "audio/wav", "flac": "audio/flac",
    "opus": "audio/ogg", "aac": "audio/aac", "pcm16": "audio/l16",
}

MEDIA_MODEL_REGISTRY = [
    # ── Google AI Studio: аудио (бесплатный тариф подтверждён) ──
    {
        "id": "gemini-3.8-flash-tts", "name": "Gemini 3.8 Flash TTS",
        "provider": "google_ai_studio", "media_type": "audio",
        "pricing_status": PRICING_FREE,
        "pricing_note": "Free Tier: Free of charge (input/output) на странице цен Gemini API.",
        "pricing_source": GOOGLE_PRICING_SOURCE, "pricing_verified_at": PRICING_VERIFIED_AT,
        "description": "Студийное качество речи, 130 языков, выразительные голоса.",
        "backend": "google_tts", "options": {"voice": "Kore"},
        "checks_provider_catalog": True,
    },
    {
        "id": "gemini-3.8-flash-lite-tts", "name": "Gemini 3.8 Flash-Lite TTS",
        "provider": "google_ai_studio", "media_type": "audio",
        "pricing_status": PRICING_FREE,
        "pricing_note": "Free Tier: Free of charge (input/output) на странице цен Gemini API.",
        "pricing_source": GOOGLE_PRICING_SOURCE, "pricing_verified_at": PRICING_VERIFIED_AT,
        "description": "Быстрый и дешёвый TTS, 101 язык, подходит для озвучки длинных текстов.",
        "backend": "google_tts", "options": {"voice": "Kore"},
        "checks_provider_catalog": True,
    },
    # ── Google AI Studio: изображения (бесплатного тарифа API нет) ──
    {
        "id": "gemini-3.1-flash-image", "name": "Nano Banana 2",
        "provider": "google_ai_studio", "media_type": "image",
        "pricing_status": PRICING_PAID, "trial_credits": True,
        "pricing_note": "Free Tier: Not available. $0.067 за изображение 1K. "
                        "Можно оплатить пробными кредитами нового проекта Google Cloud.",
        "pricing_source": GOOGLE_PRICING_SOURCE, "pricing_verified_at": PRICING_VERIFIED_AT,
        "description": "Генерация и редактирование изображений, до 4K.",
        "backend": "google_image", "checks_provider_catalog": True,
    },
    {
        "id": "gemini-3.1-flash-lite-image", "name": "Nano Banana 2 Lite",
        "provider": "google_ai_studio", "media_type": "image",
        "pricing_status": PRICING_PAID, "trial_credits": True,
        "pricing_note": "Free Tier: Not available. $0.0336 за изображение 1K.",
        "pricing_source": GOOGLE_PRICING_SOURCE, "pricing_verified_at": PRICING_VERIFIED_AT,
        "description": "Самая быстрая и дешёвая image-модель Gemini.",
        "backend": "google_image", "checks_provider_catalog": True,
    },
    {
        "id": "gemini-3-pro-image", "name": "Nano Banana Pro",
        "provider": "google_ai_studio", "media_type": "image",
        "pricing_status": PRICING_PAID, "trial_credits": True,
        "pricing_note": "Free Tier: Not available. Премиальная генерация изображений.",
        "pricing_source": GOOGLE_PRICING_SOURCE, "pricing_verified_at": PRICING_VERIFIED_AT,
        "description": "Точный рендер текста и сложные сцены.",
        "backend": "google_image", "checks_provider_catalog": True,
    },
    # ── Google AI Studio: видео (только платный тариф) ──
    {
        "id": "veo-3.1-generate-preview", "name": "Veo 3.1",
        "provider": "google_ai_studio", "media_type": "video",
        "pricing_status": PRICING_PAID, "trial_credits": True,
        "pricing_note": "Free Tier: Not available. Видео доступно только на платном тарифе Gemini API.",
        "pricing_source": GOOGLE_PRICING_SOURCE, "pricing_verified_at": PRICING_VERIFIED_AT,
        "description": "Генерация видео со звуком; асинхронная задача (predictLongRunning).",
        "backend": "google_video", "async": True,
    },
    {
        "id": "veo-3.1-fast-generate-preview", "name": "Veo 3.1 Fast",
        "provider": "google_ai_studio", "media_type": "video",
        "pricing_status": PRICING_PAID, "trial_credits": True,
        "pricing_note": "Free Tier: Not available. Видео доступно только на платном тарифе Gemini API.",
        "pricing_source": GOOGLE_PRICING_SOURCE, "pricing_verified_at": PRICING_VERIFIED_AT,
        "description": "Более быстрая и дешёвая генерация видео.",
        "backend": "google_video", "async": True,
    },
    # ── Groq: аудио (бесплатный тариф с лимитами, карта не нужна) ──
    #
    # playai-tts выключен Groq 31.12.2025 — вместо него Orpheus. У Orpheus две
    # модели (английская и саудовская арабская), лимит 200 символов на запрос и
    # только wav на выходе; русский текст он читает с английским акцентом, о чём
    # честно написано в описании.
    {
        "id": "canopylabs/orpheus-v1-english", "name": "Orpheus V1 English (Groq)",
        "provider": "groq", "media_type": "audio",
        "pricing_status": PRICING_FREE,
        "pricing_note": "Бесплатный тариф Groq с лимитами RPM/RPD; банковская карта не требуется.",
        "pricing_source": GROQ_MODELS_SOURCE, "pricing_verified_at": PRICING_VERIFIED_AT,
        "description": "Английская речь (русский текст читает с акцентом). Лимит 200 символов "
                       "на запрос, формат только wav. Требуется один раз принять условия модели.",
        "backend": "groq_tts",
        "options": {"voice": "autumn", "format": "wav", "max_input_chars": GROQ_TTS_MAX_INPUT,
                    "languages": ["en"]},
        "checks_provider_catalog": True,
    },
    {
        "id": "canopylabs/orpheus-arabic-saudi", "name": "Orpheus Arabic Saudi (Groq)",
        "provider": "groq", "media_type": "audio",
        "pricing_status": PRICING_FREE,
        "pricing_note": "Бесплатный тариф Groq с лимитами RPM/RPD; банковская карта не требуется.",
        "pricing_source": GROQ_MODELS_SOURCE, "pricing_verified_at": PRICING_VERIFIED_AT,
        "description": "Саудовская арабская речь (русский текст читает с акцентом). "
                       "Лимит 200 символов на запрос, формат только wav.",
        "backend": "groq_tts",
        "options": {"voice": "fahad", "format": "wav", "max_input_chars": GROQ_TTS_MAX_INPUT,
                    "languages": ["ar"]},
        "checks_provider_catalog": True,
    },
    # ── Pollinations: без ключа, бесплатно ──
    {
        "id": "flux", "name": "Pollinations Flux",
        "provider": "pollinations", "media_type": "image",
        "pricing_status": PRICING_FREE,
        "pricing_note": "Бесплатный endpoint без ключа; анонимные запросы ограничены по частоте.",
        "pricing_source": POLLINATIONS_SOURCE, "pricing_verified_at": PRICING_VERIFIED_AT,
        "description": "Генерация изображений без регистрации и ключа API.",
        "backend": "pollinations_image", "keyless": True,
    },
    {
        "id": "turbo", "name": "Pollinations Turbo",
        "provider": "pollinations", "media_type": "image",
        "pricing_status": PRICING_FREE,
        "pricing_note": "Бесплатный endpoint без ключа; быстрее Flux, качество ниже.",
        "pricing_source": POLLINATIONS_SOURCE, "pricing_verified_at": PRICING_VERIFIED_AT,
        "description": "Быстрая генерация изображений без ключа API.",
        "backend": "pollinations_image", "keyless": True,
    },
    {
        "id": "openai/tts-1", "name": "OpenAI TTS (Pollinations)",
        "provider": "pollinations", "media_type": "audio",
        "pricing_status": PRICING_FREE,
        "pricing_note": "Модель каталога Pollinations с paid_only=false; нужен ключ Pollinations.",
        "pricing_source": POLLINATIONS_SOURCE, "pricing_verified_at": PRICING_VERIFIED_AT,
        "description": "Озвучка текста через /v1/audio/speech, шесть голосов OpenAI.",
        "backend": "pollinations_audio",
        "keyless": False,
        "options": {"voice": "nova", "endpoint": "speech",
                    "voices": list(POLLINATIONS_AUDIO_VOICES)},
        "checks_provider_catalog": True,
    },
    {
        # Раньше здесь был id «openai-audio». В новом каталоге Pollinations это
        # алиас чат-модели openai/gpt-audio-mini: она НЕ принимает /v1/audio/speech
        # («is a text model»), зато отвечает аудио через /v1/chat/completions.
        "id": "openai/gpt-audio-mini", "name": "GPT Audio Mini (Pollinations)",
        "provider": "pollinations", "media_type": "audio",
        "pricing_status": PRICING_FREE,
        "pricing_note": "Модель каталога Pollinations с paid_only=false; нужен ключ Pollinations.",
        "pricing_source": POLLINATIONS_SOURCE, "pricing_verified_at": PRICING_VERIFIED_AT,
        "description": "Чат-модель с аудио-ответом: голос синтезируется через "
                       "/v1/chat/completions (modalities: text+audio).",
        "backend": "pollinations_audio",
        "keyless": False,
        "options": {"voice": "alloy", "endpoint": "chat",
                    "aliases": ["openai-audio"],
                    "voices": ["alloy", "echo", "fable", "onyx", "nova", "shimmer"]},
        "checks_provider_catalog": True,
    },
    {
        "id": "gptimage", "name": "Pollinations GPT Image (кредиты Pollen)",
        "provider": "pollinations", "media_type": "image",
        "pricing_status": PRICING_TRIAL,
        "pricing_note": "Премиум-модель Pollinations: списывает кредиты Pollen ($1 = 1 Pollen).",
        "pricing_source": POLLINATIONS_SOURCE, "pricing_verified_at": PRICING_VERIFIED_AT,
        "description": "Требует токен Pollinations и кредиты Pollen.",
        "backend": "pollinations_image", "options": {"token_env": "POLLINATIONS_TOKEN"},
    },
]

# ---------- Провайдеры медиа ----------
MEDIA_PROVIDERS = {
    "google_ai_studio": "Google AI Studio",
    "openrouter": "OpenRouter",
    "groq": "Groq",
    "pollinations": "Pollinations (без ключа)",
    "custom": "Свой endpoint (.env)",
}

_MEDIA_CACHE = {"models": None, "fetched_at": 0.0}
_MEDIA_CACHE_TTL = 3600
_MEDIA_LOCK = threading.Lock()

# ---------- Асинхронные задачи (видео) ----------
_JOBS: dict = {}
_JOBS_LOCK = threading.Lock()


# ======================================================================
# Подключение провайдеров
# ======================================================================
def provider_connected(provider: str) -> bool:
    """Подключён ли провайдер (есть ключ или ключ не нужен)."""
    if provider == "pollinations":
        return True
    if provider == "custom":
        return bool(
            os.getenv("IMAGE_GENERATION_URL")
            or os.getenv("AUDIO_TTS_URL")
            or os.getenv("VIDEO_API_URL")
        )
    if provider == "google_ai_studio":
        return bool((os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_AI_STUDIO_KEY") or "").strip())
    if provider == "openrouter":
        return bool((os.getenv("OPENROUTER_API_KEY") or "").strip())
    if provider == "groq":
        try:
            from groq_rotation import GROQ_KEYS
            return bool(GROQ_KEYS)
        except Exception:
            return bool((os.getenv("GROQ_API_KEY") or "").strip())
    return False


def provider_statuses() -> dict:
    return {pid: provider_connected(pid) for pid in MEDIA_PROVIDERS}


# ======================================================================
# Классификация цен
# ======================================================================
def _is_zero_price(value) -> bool:
    try:
        return float(str(value)) == 0.0
    except (TypeError, ValueError):
        return str(value or "") in {"", "free"}


def classify_openrouter_model(model: dict) -> dict:
    """Статус цены модели OpenRouter по данным самого провайдера."""
    model_id = str(model.get("id") or "")
    pricing = model.get("pricing") or {}
    prompt = pricing.get("prompt")
    completion = pricing.get("completion")
    image = pricing.get("image")
    audio = pricing.get("audio")
    known = [v for v in (prompt, completion, image, audio) if v not in (None, "")]

    if ":free" in model_id:
        status = PRICING_FREE
        note = "Модель помечена провайдером суффиксом :free."
    elif known and all(_is_zero_price(v) for v in known):
        status = PRICING_FREE
        note = "Провайдер отдаёт нулевые цены в /api/v1/models."
    elif known:
        status = PRICING_PAID
        note = "Провайдер отдаёт ненулевые цены в /api/v1/models."
    else:
        status = PRICING_UNKNOWN
        note = "Провайдер не вернул данные о цене."
    return {
        "pricing_status": status,
        "pricing_note": note,
        "pricing_source": "https://openrouter.ai/api/v1/models",
        "pricing_verified_at": "live",
    }


# ======================================================================
# Каталог
# ======================================================================
def _live_provider_catalog(provider: str, force: bool = False):
    """Живой каталог моделей провайдера (для проверки ID и цен)."""
    with _MEDIA_LOCK:
        if not force and _MEDIA_CACHE["models"] is not None and (time.time() - _MEDIA_CACHE["fetched_at"]) < _MEDIA_CACHE_TTL:
            return _MEDIA_CACHE["models"]

    models = {}
    if provider_connected("openrouter"):
        try:
            resp = requests.get("https://openrouter.ai/api/v1/models", timeout=10)
            resp.raise_for_status()
            for item in resp.json().get("data", []):
                if item.get("id"):
                    models[("openrouter", item["id"])] = item
        except (requests.RequestException, ValueError, TypeError) as exc:
            print(f"[media] Каталог OpenRouter недоступен: {exc}")

    if provider_connected("groq"):
        try:
            from groq_rotation import get_groq_key
            key = get_groq_key()
            resp = requests.get(
                "https://api.groq.com/openai/v1/models",
                headers={"Authorization": f"Bearer {key}"}, timeout=10,
            )
            resp.raise_for_status()
            for item in resp.json().get("data", []):
                if item.get("id"):
                    models[("groq", item["id"])] = item
        except (requests.RequestException, ValueError, TypeError) as exc:
            print(f"[media] Каталог Groq недоступен: {exc}")

    with _MEDIA_LOCK:
        _MEDIA_CACHE["models"] = models
        _MEDIA_CACHE["fetched_at"] = time.time()
    return models


def reset_catalog_cache():
    with _MEDIA_LOCK:
        _MEDIA_CACHE["models"] = None
        _MEDIA_CACHE["fetched_at"] = 0.0


def _openrouter_media_entries(live: dict) -> list:
    """Image/audio/video модели OpenRouter с ценами из ответа провайдера."""
    entries = []
    for (provider, model_id), item in live.items():
        if provider != "openrouter":
            continue
        architecture = item.get("architecture") or {}
        outputs = [str(x).lower() for x in (architecture.get("output_modalities") or [])]
        media_type = next((t for t in MEDIA_TYPES if t in outputs), None)
        if not media_type:
            continue
        entry = {
            "id": model_id,
            "name": item.get("name") or model_id,
            "provider": "openrouter",
            "media_type": media_type,
            "description": item.get("description") or "",
            "backend": "openrouter_image" if media_type == "image" else "openrouter_audio",
            "context_length": item.get("context_length") or 0,
        }
        entry.update(classify_openrouter_model(item))
        entries.append(entry)
    return entries


def _pollinations_key() -> str:
    return (os.getenv("POLLINATIONS_API_KEY")
            or os.getenv("POLLINATIONS_KEY")
            or os.getenv("POLLINATIONS_TOKEN") or "").strip()


def _pollinations_audio_endpoint(item: dict) -> str:
    """Каким endpoint'ом звать аудио-модель: ``speech`` или ``chat``.

    Pollinations разделяет два вида моделей:

    * выделенные TTS (``openai/tts-1``, ``microsoft/mai-voice-2.1``) принимают
      ``POST /v1/audio/speech``;
    * чат-модели с аудио-выходом (``openai/gpt-audio-mini``, он же алиас
      ``openai-audio``) на ``/v1/audio/speech`` отвечают 400 «is a text model»
      и синтезируют речь только через ``/v1/chat/completions``.
    """
    endpoints = [str(value) for value in (item.get("supported_endpoints") or [])]
    if "/v1/audio/speech" in endpoints:
        return "speech"
    if "/v1/chat/completions" in endpoints:
        return "chat"
    # Нет списка endpoint'ов (старый ответ каталога): решаем по категории модели.
    return "chat" if str(item.get("category") or "").lower() == "text" else "speech"


def _is_non_speech_audio(item: dict) -> bool:
    """Музыка, SFX и обработка чужого аудио — это не озвучка текста."""
    haystack = " ".join(str(item.get(key) or "") for key in ("name", "id", "title", "description"))
    haystack = haystack.lower()
    return any(marker in haystack for marker in _NON_SPEECH_TTS_MARKERS)


def _pollinations_item_matches(media_type: str, item: dict) -> bool:
    """Подходит ли модель каталога Pollinations для запрошенного типа медиа.

    Каталог ``/audio/models`` намеренно содержит и распознавание речи, и
    обработку аудиофайлов: без этой проверки в интерфейс попадали модели,
    которые физически не могут озвучить текст (провайдер отвечал 400).
    """
    outputs = [str(value).lower() for value in (item.get("output_modalities") or [])]
    inputs = [str(value).lower() for value in (item.get("input_modalities") or [])]
    if media_type == "audio":
        if outputs and "audio" not in outputs:
            return False
        if inputs and "text" not in inputs:
            return False
        endpoints = [str(value) for value in (item.get("supported_endpoints") or [])]
        if endpoints and not ({"/v1/audio/speech", "/v1/chat/completions"} & set(endpoints)):
            return False
        return not _is_non_speech_audio(item)
    if media_type == "video":
        return not outputs or "video" in outputs
    if media_type == "image":
        return not outputs or "image" in outputs
    return True


_POLL_TTS_CACHE = {"models": None, "fetched_at": 0.0, "error": ""}
_POLL_TTS_TTL = 1800
_POLL_TTS_LOCK = threading.Lock()


def _pollinations_catalog(path: str, timeout: float = 8.0) -> list:
    """Сырой ответ JSON-каталога Pollinations (список моделей)."""
    resp = requests.get(f"{POLLINATIONS_BASE_URL}{path}", timeout=timeout)
    resp.raise_for_status()
    payload = resp.json()
    if isinstance(payload, dict):
        payload = payload.get("data") or payload.get("models") or []
    return payload if isinstance(payload, list) else []


def pollinations_tts_catalog(force: bool = False, allow_fetch: bool = True) -> list[dict]:
    """Живой список TTS-моделей Pollinations (только те, что правда озвучивают текст).

    Возвращает нормализованные записи::

        {"id", "name", "endpoint", "voices", "paid_only", "health",
         "description", "aliases", "publisher", "source"}

    ``endpoint`` — ``speech`` (``/v1/audio/speech``) или ``chat``
    (``/v1/chat/completions``). Ошибка сети не бросается наружу: возвращается
    последний удачный снимок (или пустой список), а текст ошибки кладётся в
    ``_POLL_TTS_CACHE["error"]`` для честного сообщения в интерфейсе.

    ``allow_fetch=False`` — не ходить в сеть вовсе (только уже загруженный
    снимок). Так страница настроек не ждёт провайдера при каждом открытии.
    """
    with _POLL_TTS_LOCK:
        fresh = _POLL_TTS_CACHE["models"] is not None and \
            (time.time() - _POLL_TTS_CACHE["fetched_at"]) < _POLL_TTS_TTL
        if fresh and not force:
            return [dict(item) for item in _POLL_TTS_CACHE["models"]]
        if not allow_fetch and not force:
            return [dict(item) for item in (_POLL_TTS_CACHE["models"] or [])]

    errors: list[str] = []

    def fetch(path: str) -> list:
        try:
            return _pollinations_catalog(path)
        except (requests.RequestException, ValueError, TypeError) as exc:
            errors.append(f"{path}: {exc}")
            return []

    # Два каталога запрашиваем параллельно: /audio/models — выделенные TTS,
    # /text/models — чат-модели с аудио-выходом (openai/gpt-audio-*).
    # Параллельно ожидание ограничено одним таймаутом, а не двумя.
    with ThreadPoolExecutor(max_workers=2) as pool:
        audio_future = pool.submit(fetch, "/audio/models")
        text_future = pool.submit(fetch, "/text/models")
        audio_items, text_items = audio_future.result(), text_future.result()

    items: list[dict] = []
    for source, raw in (("speech", audio_items), ("chat", text_items)):
        for item in raw:
            if isinstance(item, str):
                item = {"name": item}
            if not isinstance(item, dict):
                continue
            model_id = str(item.get("name") or item.get("id") or "").strip()
            if not model_id or not _pollinations_item_matches("audio", item):
                continue
            endpoint = _pollinations_audio_endpoint(item)
            if source == "chat" and endpoint != "chat":
                continue
            items.append({
                "id": model_id,
                "name": item.get("title") or item.get("display_name") or model_id,
                "description": item.get("description") or "",
                "publisher": item.get("publisher") or "",
                "endpoint": endpoint,
                "voices": [str(voice) for voice in (item.get("voices") or []) if voice],
                "aliases": [str(alias) for alias in (item.get("aliases") or []) if alias],
                "paid_only": bool(item.get("paid_only")),
                "health": ((item.get("health") or {}).get("status") or "")
                          if isinstance(item.get("health"), dict) else "",
                "source": f"{POLLINATIONS_BASE_URL}/{'audio' if source == 'speech' else 'text'}/models",
            })

    unique: dict[str, dict] = {}
    for item in items:
        unique.setdefault(item["id"], item)
    models = sorted(
        unique.values(),
        key=lambda entry: (entry["paid_only"], entry["health"] not in {"healthy", ""},
                           entry["endpoint"] != "speech", entry["name"].lower()),
    )

    if models or not errors:
        with _POLL_TTS_LOCK:
            _POLL_TTS_CACHE.update({"models": models, "fetched_at": time.time(),
                                    "error": "; ".join(errors)})
        return [dict(item) for item in models]

    print(f"[media] Каталог TTS Pollinations недоступен: {'; '.join(errors)}")
    with _POLL_TTS_LOCK:
        cached = _POLL_TTS_CACHE["models"] or []
        _POLL_TTS_CACHE["error"] = "; ".join(errors)
    return [dict(item) for item in cached]


def pollinations_tts_model(model_id: str, force: bool = False,
                           allow_fetch: bool = True) -> dict | None:
    """Находит модель (или её алиас) в живом каталоге TTS Pollinations."""
    needle = str(model_id or "").strip()
    if not needle:
        return None
    for item in pollinations_tts_catalog(force=force, allow_fetch=allow_fetch):
        if item["id"] == needle or needle in item.get("aliases", []):
            return item
    return None


def reset_tts_catalog_cache():
    with _POLL_TTS_LOCK:
        _POLL_TTS_CACHE.update({"models": None, "fetched_at": 0.0, "error": ""})


def _pollinations_media_entries(force=False) -> list:
    """Получает актуальные image/audio/video модели из публичного каталога Pollinations."""
    endpoints = {
        "image": f"{POLLINATIONS_BASE_URL}/image/models",
        "audio": f"{POLLINATIONS_BASE_URL}/audio/models",
        "video": f"{POLLINATIONS_BASE_URL}/video/models",
    }
    entries = []
    for media_type, endpoint in endpoints.items():
        try:
            resp = requests.get(endpoint, timeout=12)
            resp.raise_for_status()
            payload = resp.json()

            # Pollinations returns the rich media catalogs as a JSON LIST.
            # Some compatible endpoints may instead return OpenAI-style
            # {"data": [...]} or {"models": [...]}. Handle all three safely.
            if isinstance(payload, list):
                raw = payload
            elif isinstance(payload, dict):
                raw = payload.get("data")
                if raw is None:
                    raw = payload.get("models")
                if raw is None:
                    raw = []
            else:
                raw = []

            if not isinstance(raw, list):
                print(f"[media] Pollinations {media_type}: unexpected catalog shape {type(payload).__name__}")
                continue
            for item in raw:
                if isinstance(item, str):
                    item = {"id": item}
                model_id = str(item.get("id") or item.get("name") or "").strip()
                if not model_id:
                    continue
                if not _pollinations_item_matches(media_type, item):
                    continue
                pricing = item.get("pricing") or {}
                values = [v for v in pricing.values() if v not in (None, "")]
                try:
                    zero_price = bool(values) and all(float(str(v)) == 0.0 for v in values)
                except (TypeError, ValueError):
                    zero_price = False
                # paid_only=True — модель доступна только на платном тарифе Pollen.
                paid_only = item.get("paid_only")
                free_flag = item.get("free") is True or item.get("free_tier") is True or paid_only is False
                if paid_only is True:
                    status = PRICING_PAID
                elif free_flag or zero_price:
                    status = PRICING_FREE
                else:
                    status = PRICING_UNKNOWN
                backend = {
                    "image": "pollinations_image",
                    "audio": "pollinations_audio",
                    "video": "pollinations_video",
                }[media_type]
                options = {}
                if media_type == "audio":
                    options = {
                        "endpoint": _pollinations_audio_endpoint(item),
                        "voices": [str(voice) for voice in (item.get("voices") or []) if voice],
                    }
                    default_voice = (options["voices"] or ["nova"])[0]
                    options["voice"] = default_voice
                entries.append({
                    "id": model_id,
                    "name": item.get("title") or item.get("display_name")
                            or item.get("name") or model_id,
                    "provider": "pollinations",
                    "media_type": media_type,
                    "pricing_status": status,
                    "pricing_note": (
                        "Бесплатная модель live-каталога Pollinations (paid_only=false)."
                        if status == PRICING_FREE else
                        "Pollinations помечает модель как платную (paid_only) — "
                        "она расходует кредиты Pollen."
                        if status == PRICING_PAID else
                        "Pollinations не сообщил тариф для этой модели; требуется проверить доступ."
                    ),
                    "pricing_source": f"{POLLINATIONS_BASE_URL}/models",
                    "pricing_verified_at": "live",
                    "description": item.get("description") or "",
                    "backend": backend,
                    "options": options,
                    "keyless": False,
                    "checks_provider_catalog": True,
                    "in_provider_catalog": True,
                })
        except (requests.RequestException, ValueError, TypeError) as exc:
            print(f"[media] Pollinations {media_type} catalog unavailable: {exc}")
    return entries


def _custom_endpoint_entries() -> list:
    """Модели, которые пользователь подключил сам через .env (оплачивает сам)."""
    entries = []
    if os.getenv("IMAGE_GENERATION_URL"):
        entries.append({
            "id": os.getenv("IMAGE_MODEL", "custom-image"),
            "name": "Своя image-модель (IMAGE_GENERATION_URL)",
            "provider": "custom", "media_type": "image", "backend": "custom_image",
            "pricing_status": PRICING_UNKNOWN,
            "pricing_note": "Endpoint из .env: стоимость определяет ваш провайдер.",
            "pricing_source": ".env", "pricing_verified_at": PRICING_VERIFIED_AT,
            "description": "OpenAI-compatible /images/generations endpoint.",
        })
    if os.getenv("AUDIO_TTS_URL"):
        entries.append({
            "id": os.getenv("AUDIO_TTS_MODEL", "custom-tts"),
            "name": "Своя TTS-модель (AUDIO_TTS_URL)",
            "provider": "custom", "media_type": "audio", "backend": "custom_tts",
            "pricing_status": PRICING_UNKNOWN,
            "pricing_note": "Endpoint из .env: стоимость определяет ваш провайдер.",
            "pricing_source": ".env", "pricing_verified_at": PRICING_VERIFIED_AT,
            "description": "OpenAI-compatible /audio/speech endpoint.",
        })
    if os.getenv("VIDEO_API_URL"):
        entries.append({
            "id": os.getenv("VIDEO_MODEL", "custom-video"),
            "name": "Своя video-модель (VIDEO_API_URL)",
            "provider": "custom", "media_type": "video", "backend": "custom_video",
            "pricing_status": PRICING_UNKNOWN,
            "pricing_note": "Endpoint из .env: стоимость определяет ваш провайдер.",
            "pricing_source": ".env", "pricing_verified_at": PRICING_VERIFIED_AT,
            "description": "Произвольный video-endpoint, результат опрашивается по status_url.",
            "async": True,
        })
    return entries


def media_catalog(media_type="all", provider="all", query="", include_paid=False,
                  include_unknown=False, include_trial=True, refresh=False,
                  skip_live=False) -> list:
    """
    Каталог медиа-моделей.

    По умолчанию возвращаются только модели с подтверждённым бесплатным API
    и модели на пробных кредитах (помечены отдельно). Платные и модели
    с неподтверждённой ценой добавляются только явными флагами.
    """
    live = {} if skip_live else _live_provider_catalog("all", force=refresh)

    # Pollinations меняет каталог динамически, поэтому live-модели имеют приоритет
    # над устаревшими статическими записями.
    entries = [
        dict(item) for item in MEDIA_MODEL_REGISTRY
        if item.get("provider") != "pollinations"
    ]
    pollinations_live = _pollinations_media_entries(force=refresh) if provider in ("all", "pollinations") else []
    # Keep the verified static entries as a safety net: the live endpoint can
    # omit premium/trial IDs or report incomplete pricing during outages.
    entries.extend(pollinations_live)
    entries.extend([dict(item) for item in MEDIA_MODEL_REGISTRY
                    if item.get("provider") == "pollinations"])
    entries.extend(_openrouter_media_entries(live))
    entries.extend(_custom_endpoint_entries())

    # Подтверждение ID живым каталогом провайдера (где каталог существует).
    catalog_providers = {p for (p, _mid) in live}
    for entry in entries:
        key = (entry["provider"], entry["id"])
        if entry["provider"] in catalog_providers:
            entry["in_provider_catalog"] = key in live
        else:
            entry["in_provider_catalog"] = None
        entry.setdefault("trial_credits", False)
        entry["provider_name"] = MEDIA_PROVIDERS.get(entry["provider"], entry["provider"])
        entry["provider_connected"] = provider_connected(entry["provider"])
        entry["pricing_label"] = PRICING_LABELS.get(entry.get("pricing_status"), PRICING_LABELS[PRICING_UNKNOWN])
        status = entry.get("pricing_status", PRICING_UNKNOWN)
        entry["auto_usable"] = (
            status in AUTO_USABLE_STATUSES
            and entry["provider_connected"]
            and entry.get("in_provider_catalog") is not False
        )
        entry["selectable"] = entry["provider_connected"] and (
            status == PRICING_FREE
            or (status == PRICING_TRIAL)
            or (status == PRICING_PAID and include_paid)
            or (status == PRICING_UNKNOWN and include_unknown)
        )

    if media_type != "all":
        entries = [e for e in entries if e.get("media_type") == media_type]
    if provider != "all":
        entries = [e for e in entries if e.get("provider") == provider]

    needle = (query or "").strip().lower()
    if needle:
        entries = [
            e for e in entries
            if needle in " ".join([
                str(e.get("id", "")), str(e.get("name", "")), str(e.get("description", "")),
                str(e.get("provider", "")), str(e.get("provider_name", "")),
            ]).lower()
        ]

    if not include_paid:
        entries = [e for e in entries if e.get("pricing_status") != PRICING_PAID]
    if not include_unknown:
        entries = [e for e in entries if e.get("pricing_status") != PRICING_UNKNOWN]
    if not include_trial:
        entries = [e for e in entries if e.get("pricing_status") != PRICING_TRIAL]

    order = {PRICING_FREE: 0, PRICING_TRIAL: 1, PRICING_UNKNOWN: 2, PRICING_PAID: 3}
    entries.sort(key=lambda e: (
        0 if e.get("auto_usable") else 1,
        order.get(e.get("pricing_status", PRICING_UNKNOWN), 9),
        not e.get("provider_connected"),
        str(e.get("name", "")).lower(),
    ))

    unique = {}
    for entry in entries:
        unique[(entry["provider"], entry["id"], entry.get("media_type"))] = entry
    return list(unique.values())


def find_media_model(provider: str, model_id: str, include_paid=False, include_unknown=False,
                     skip_live=False):
    """Ищет модель в каталоге. Возвращает (model, None) или (None, error)."""
    if not provider or not model_id:
        return None, "Укажите провайдера и модель."
    models = media_catalog(
        provider=provider, include_paid=include_paid,
        include_unknown=include_unknown, skip_live=skip_live,
    )
    for model in models:
        if model["id"] == model_id:
            return model, None
    # Модель есть в реестре, но отфильтрована (платная/не подключён провайдер).
    hidden = media_catalog(provider=provider, include_paid=True, include_unknown=True, skip_live=skip_live)
    for model in hidden:
        if model["id"] == model_id:
            if not model.get("provider_connected"):
                return None, (f"Провайдер {model.get('provider_name')} не подключён: "
                              f"добавьте ключ в .env.")
            return None, (f"Модель {model_id} — {model.get('pricing_label', '').lower()}. "
                          f"Она не используется автоматически: подтвердите выбор явно.")
    return None, f"Модель {model_id} не найдена в каталоге провайдера."


def required_confirmations(model: dict) -> dict:
    """Какие явные подтверждения нужны для выбора модели."""
    status = model.get("pricing_status")
    return {
        "paid": status == PRICING_PAID,
        "trial": status == PRICING_TRIAL,
        "unknown": status == PRICING_UNKNOWN,
    }


def validate_selection(model: dict, confirm=None) -> str | None:
    """Проверяет, можно ли использовать модель. Возвращает текст ошибки или None."""
    confirm = confirm or {}
    if not model.get("provider_connected"):
        return (f"Провайдер {model.get('provider_name')} не подключён. "
                f"Добавьте ключ в .env или выберите другую модель.")
    if model.get("in_provider_catalog") is False:
        return (f"Модели {model.get('id')} нет в актуальном каталоге провайдера. "
                f"Обновите каталог и выберите доступную модель.")
    needs = required_confirmations(model)
    if needs["paid"] and not confirm.get("paid"):
        return ("Платные модели не используются автоматически. "
                "Подтвердите, что готовы оплатить генерацию.")
    if needs["trial"] and not confirm.get("trial"):
        return ("Модель работает на пробных кредитах. "
                "Подтвердите, что кредиты могут быть израсходованы.")
    if needs["unknown"] and not confirm.get("unknown"):
        return ("Цену этой модели подтвердить не удалось. "
                "Подтвердите выбор, если готовы оплатить запрос.")
    return None


def current_selection():
    """Текущая выбранная медиа-модель (пустой dict, если генерация выключена)."""
    import config
    return dict(getattr(config, "media_selection", None) or {})


def resolve_selection(requested_provider=None, requested_model=None, confirm=None):
    """
    Разрешает модель для запроса генерации.

    Клиент может прислать provider/model явно либо опустить их — тогда берётся
    модель, выбранная в панели «Медиа». Возвращает (model, confirm, error).
    """
    import config
    confirm = dict(confirm or {})
    selection = current_selection()
    provider = (requested_provider or selection.get("provider") or "").strip()
    model_id = (requested_model or selection.get("model") or "").strip()
    if not provider or not model_id:
        return None, confirm, ("Медиа-модель не выбрана. Откройте «Медиа» в верхней панели "
                               "чата и выберите модель генерации.")
    already_selected = (provider, model_id) == (selection.get("provider"), selection.get("model"))
    model, error = find_media_model(
        provider, model_id,
        include_paid=True, include_unknown=True, skip_live=False,
    )
    if error:
        return None, confirm, error
    if already_selected:
        # Модель уже прошла проверку при выборе в панели «Медиа»,
        # поэтому повторно подтверждения не спрашиваем.
        confirm = {"paid": True, "trial": True, "unknown": True}
    return model, confirm, None


def selection_payload(model: dict) -> dict:
    """Компактное описание выбранной модели (для runtime_settings и ответов API)."""
    return {
        "provider": model["provider"],
        "provider_name": model.get("provider_name", model["provider"]),
        "model": model["id"],
        "name": model.get("name", model["id"]),
        "media_type": model.get("media_type"),
        "pricing_status": model.get("pricing_status"),
        "pricing_label": model.get("pricing_label"),
        "pricing_note": model.get("pricing_note", ""),
        "pricing_source": model.get("pricing_source", ""),
    }


# ======================================================================
# Сохранение файлов
# ======================================================================
_EXTENSION_BY_MIME = {
    "image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif",
    "audio/wav": ".wav", "audio/x-wav": ".wav", "audio/mpeg": ".mp3", "audio/mp3": ".mp3",
    "audio/ogg": ".ogg", "audio/opus": ".opus", "audio/aac": ".aac", "audio/x-aac": ".aac",
    "audio/flac": ".flac", "audio/x-flac": ".flac", "audio/l16": ".wav", "audio/webm": ".webm",
    "video/mp4": ".mp4", "video/webm": ".webm", "video/quicktime": ".mov",
}


def ensure_media_dir() -> str:
    os.makedirs(MEDIA_DIR, exist_ok=True)
    return MEDIA_DIR


def save_media(kind: str, data: bytes, mime: str = "", prefix: str = "") -> dict:
    """Сохраняет байты медиа и возвращает {filename, path, mime, url, size}."""
    ensure_media_dir()
    mime = (mime or "").split(";")[0].strip().lower()
    ext = _EXTENSION_BY_MIME.get(mime) or {
        "image": ".png", "audio": ".wav", "video": ".mp4",
    }.get(kind, ".bin")
    filename = f"{prefix or kind}_{int(time.time())}_{uuid.uuid4().hex[:10]}{ext}"
    path = os.path.join(MEDIA_DIR, filename)
    with open(path, "wb") as handle:
        handle.write(data)
    return {
        "kind": kind,
        "filename": filename,
        "path": path,
        "mime": mime or "application/octet-stream",
        "size": len(data),
        "url": f"/media/{filename}",
    }


def _decode_data_url(value: str):
    """Декодирует data-URL или чистый base64. Возвращает (bytes, mime)."""
    mime = ""
    payload = str(value or "")
    if payload.startswith("data:"):
        header, _, payload = payload.partition(",")
        mime = header[5:].split(";")[0].strip()
    payload = re.sub(r"\s+", "", payload)
    payload += "=" * ((4 - len(payload) % 4) % 4)
    return base64.b64decode(payload), mime


def _extract_inline_data(payload: dict):
    """Достаёт inlineData (изображение/аудио) из ответа generateContent."""
    for candidate in payload.get("candidates", []) or []:
        for part in (candidate.get("content") or {}).get("parts", []) or []:
            inline = part.get("inlineData") or part.get("inline_data")
            if not inline:
                continue
            data = inline.get("data")
            if not data:
                continue
            mime = inline.get("mimeType") or inline.get("mime_type") or ""
            return base64.b64decode(re.sub(r"\s+", "", data)), mime
    return None, ""


def _provider_error(resp) -> str:
    """Дословная ошибка провайдера с HTTP-кодом (без выдуманных формулировок)."""
    try:
        detail = json.dumps(resp.json(), ensure_ascii=False)
    except (ValueError, TypeError):
        detail = (resp.text or "").strip()
    return f"Провайдер вернул HTTP {resp.status_code}: {detail[:1200]}"


def _report(on_status, message: str):
    if on_status:
        on_status(message)


# ======================================================================
# Бэкенды генерации
# ======================================================================
def _google_key() -> str:
    return (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_AI_STUDIO_KEY") or "").strip()


def _google_generate_content(model_id: str, body: dict, timeout: int = 180):
    key = _google_key()
    if not key:
        return None, "Google AI Studio: GEMINI_API_KEY не найден в .env"
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_id}:generateContent"
    try:
        resp = requests.post(
            url, json=body, timeout=timeout,
            headers={"Content-Type": "application/json", "x-goog-api-key": key},
        )
    except requests.exceptions.Timeout:
        return None, f"Google AI Studio: таймаут запроса к {model_id} ({timeout} c)"
    except requests.exceptions.RequestException as exc:
        return None, f"Google AI Studio: {exc}"
    if resp.status_code >= 400:
        return None, _provider_error(resp)
    try:
        return resp.json(), None
    except ValueError:
        return None, "Google AI Studio вернул некорректный JSON"


def generate_google_image(model: dict, prompt: str, options=None, on_status=None):
    options = options or {}
    generation = {"responseModalities": ["IMAGE"]}
    size = options.get("image_size")
    if size:
        generation["imageConfig"] = {"imageSize": str(size).upper()}
    body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}], "generationConfig": generation}
    _report(on_status, f"Отправка запроса к Google AI Studio ({model['id']})")
    data, error = _google_generate_content(model["id"], body)
    if error:
        return None, error
    _report(on_status, "Google AI Studio вернул ответ, сохраняю изображение")
    blob, mime = _extract_inline_data(data)
    if not blob:
        finish = (data.get("candidates") or [{}])[0].get("finishReason")
        return None, ("Google AI Studio не вернул изображение"
                      + (f" (finishReason={finish})" if finish else "")
                      + ". Проверьте, что модель доступна для вашего ключа.")
    return save_media("image", blob, mime or "image/png", prefix="gemini_image"), None


def generate_google_audio(model: dict, prompt: str, options=None, on_status=None):
    options = {**(model.get("options") or {}), **(options or {})}
    voice = options.get("voice") or "Kore"
    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice}}},
        },
    }
    _report(on_status, f"Отправка запроса к Google AI Studio TTS ({model['id']}, голос {voice})")
    data, error = _google_generate_content(model["id"], body)
    if error:
        return None, error
    _report(on_status, "Google AI Studio вернул аудио, сохраняю файл")
    blob, mime = _extract_inline_data(data)
    if not blob:
        return None, "Google AI Studio не вернул аудиоданные (в ответе нет inlineData)."
    mime_low = (mime or "").lower()
    if blob[:4] != b"RIFF" and ("l16" in mime_low or "pcm" in mime_low or not mime_low):
        # Gemini отдаёт сырой PCM 16 бит, моно. Браузер такой не проиграет,
        # поэтому оборачиваем его в WAV-заголовок.
        import io, wave
        found = re.search(r"rate=(\d+)", mime_low)
        rate = int(found.group(1)) if found else 24000
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(rate)
            wav.writeframes(blob)
        blob, mime = buf.getvalue(), "audio/wav"
    return save_media("audio", blob, mime or "audio/wav", prefix="gemini_tts"), None


def groq_terms_url(model_id: str) -> str:
    """Ссылка на страницу принятия условий модели Groq."""
    from urllib.parse import quote
    return GROQ_TERMS_URL.format(model=quote(str(model_id or ""), safe=""))


def _groq_error(model_id: str, detail: str) -> str:
    """Переводит известные ошибки Groq в понятное действие пользователя."""
    text = str(detail or "")
    low = text.lower()
    if "model_terms_required" in low or "requires terms acceptance" in low:
        return (f"Groq: модель {model_id} не активирована — ваша организация ещё не приняла "
                f"условия модели (model_terms_required). Откройте {groq_terms_url(model_id)} "
                f"и нажмите «Accept terms», затем повторите озвучку. "
                f"Ответ провайдера: {text}")
    if "decommissioned" in low or "deprecated" in low or "does not exist" in low \
            or "model_not_found" in low:
        return (f"Groq: модели {model_id} больше нет в API. Модель playai-tts провайдер "
                f"отключил 31.12.2025; актуальные TTS-модели — canopylabs/orpheus-v1-english "
                f"и canopylabs/orpheus-arabic-saudi. Ответ провайдера: {text}")
    return text


def _split_for_limit(text: str, limit: int) -> list[str]:
    """Режет текст так, чтобы каждая часть влезла в ``limit`` символов."""
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]
    parts, current = [], ""
    for sentence in re.split(r"(?<=[.!?…;:])\s+", text):
        pieces = [sentence] if len(sentence) <= limit else []
        if not pieces:
            buffer = ""
            for word in sentence.split(" "):
                while len(word) > limit:            # «слово» длиннее лимита — режем
                    pieces.append(word[:limit])
                    word = word[limit:]
                if not word:
                    continue
                candidate = f"{buffer} {word}".strip()
                if len(candidate) > limit and buffer:
                    pieces.append(buffer)
                    buffer = word
                else:
                    buffer = candidate
            if buffer:
                pieces.append(buffer)
        for piece in pieces:
            piece = piece.strip()
            if not piece:
                continue
            candidate = f"{current} {piece}".strip()
            if len(candidate) <= limit:
                current = candidate
            else:
                if current:
                    parts.append(current)
                current = piece
    if current:
        parts.append(current)
    return parts


def _join_wav(blobs: list[bytes]) -> bytes:
    """Склеивает части wav в один файл (Orpheus отвечает только wav).

    Если части пришли в разных форматах, склеить их без потери качества нельзя —
    поднимаем ``ValueError``, чтобы вызывающий код честно сказал об этом.
    """
    if len(blobs) == 1:
        return blobs[0]
    params, frames = None, []
    for blob in blobs:
        try:
            with wave.open(io.BytesIO(blob), "rb") as source:
                current = (source.getnchannels(), source.getsampwidth(), source.getframerate())
                if params is not None and current != params:
                    raise ValueError("wav parts have different formats")
                params = current
                frames.append(source.readframes(source.getnframes()))
        except (wave.Error, EOFError) as exc:
            raise ValueError(f"broken wav part: {exc}") from exc
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as target:
        target.setnchannels(params[0])
        target.setsampwidth(params[1])
        target.setframerate(params[2])
        for frame in frames:
            target.writeframes(frame)
    return buffer.getvalue()


def generate_groq_audio(model: dict, prompt: str, options=None, on_status=None):
    """Orpheus TTS: лимит 200 символов на запрос и только wav на выходе.

    Длинный текст режем сами и склеиваем wav: иначе Groq отвечает 400 на
    первый же запрос длиннее 200 символов, хотя интерфейс обещал озвучку.
    """
    options = {**(model.get("options") or {}), **(options or {})}
    from groq_rotation import get_groq_key
    key = get_groq_key()
    if not key:
        return None, "Groq: API ключ не найден в .env (GROQ_API_KEY)"
    model_id = str(model.get("id") or "canopylabs/orpheus-v1-english")
    voice = options.get("voice") or "autumn"
    response_format = "wav"      # Orpheus поддерживает только wav
    if str(options.get("format") or "wav").lower() != "wav":
        _report(on_status, "Groq Orpheus поддерживает только wav — использую wav")
    limit = int(options.get("max_input_chars") or GROQ_TTS_MAX_INPUT)
    parts = _split_for_limit(prompt, limit)
    if not parts:
        return None, "Groq: пустой текст для озвучки."

    blobs = []
    for index, part in enumerate(parts):
        payload = {
            "model": model_id,
            "input": part,
            "voice": voice,
            "response_format": response_format,
        }
        if len(parts) > 1:
            _report(on_status, f"Groq /audio/speech ({model_id}): часть {index + 1} из {len(parts)}")
        else:
            _report(on_status, f"Отправка запроса к Groq /audio/speech ({model_id})")
        try:
            resp = requests.post(
                GROQ_TTS_URL, json=payload, timeout=180,
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            )
        except requests.exceptions.Timeout:
            return None, "Groq: таймаут запроса синтеза речи"
        except requests.exceptions.RequestException as exc:
            return None, f"Groq: {exc}"
        if resp.status_code >= 400:
            return None, _groq_error(model_id, _provider_error(resp))
        if not resp.content:
            return None, "Groq вернул пустой аудиопоток."
        blobs.append(resp.content)

    try:
        blob = _join_wav(blobs)
    except ValueError as exc:
        log_warning = f"Groq: не удалось склеить части ({exc}) — сохраняю первую"
        _report(on_status, log_warning)
        blob = blobs[0]
    _report(on_status, "Groq вернул аудио, сохраняю файл")
    return save_media("audio", blob, "audio/wav", prefix="groq_tts"), None


def _openrouter_headers() -> dict:
    key = (os.getenv("OPENROUTER_API_KEY") or "").strip()
    if not key:
        return {}
    return {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "HTTP-Referer": os.getenv("SITE_URL", "http://localhost:5000"),
        "X-Title": os.getenv("APP_TITLE", "NovaMind AI"),
    }


def _openrouter_chat(model_id: str, prompt: str, modalities: list, on_status=None, timeout=240):
    headers = _openrouter_headers()
    if not headers:
        return None, "OpenRouter: OPENROUTER_API_KEY не найден в .env"
    payload = {
        "model": model_id,
        "messages": [{"role": "user", "content": prompt}],
        "modalities": modalities,
    }
    _report(on_status, f"Отправка запроса к OpenRouter ({model_id})")
    try:
        resp = requests.post("https://openrouter.ai/api/v1/chat/completions",
                             json=payload, headers=headers, timeout=timeout)
    except requests.exceptions.Timeout:
        return None, f"OpenRouter: таймаут запроса к {model_id}"
    except requests.exceptions.RequestException as exc:
        return None, f"OpenRouter: {exc}"
    if resp.status_code >= 400:
        return None, _provider_error(resp)
    try:
        return resp.json(), None
    except ValueError:
        return None, "OpenRouter вернул некорректный JSON"


def generate_openrouter_image(model: dict, prompt: str, options=None, on_status=None):
    data, error = _openrouter_chat(model["id"], prompt, ["image", "text"], on_status)
    if error:
        return None, error
    _report(on_status, "OpenRouter вернул ответ, сохраняю изображение")
    message = ((data.get("choices") or [{}])[0].get("message") or {})
    images = message.get("images") or []
    for item in images:
        url = (item or {}).get("image_url", {}).get("url") if isinstance(item, dict) else None
        if url:
            blob, mime = _decode_data_url(url)
            return save_media("image", blob, mime or "image/png", prefix="openrouter_image"), None
    return None, "OpenRouter не вернул изображение в ответе модели."


def generate_openrouter_audio(model: dict, prompt: str, options=None, on_status=None):
    data, error = _openrouter_chat(model["id"], prompt, ["audio", "text"], on_status)
    if error:
        return None, error
    _report(on_status, "OpenRouter вернул ответ, сохраняю аудио")
    message = ((data.get("choices") or [{}])[0].get("message") or {})
    audio = message.get("audio") or {}
    payload_value = audio.get("data") if isinstance(audio, dict) else None
    if not payload_value and isinstance(audio, dict):
        payload_value = audio.get("url")
    if payload_value:
        blob, mime = _decode_data_url(payload_value)
        return save_media("audio", blob, mime or "audio/wav", prefix="openrouter_audio"), None
    for item in message.get("images") or []:
        url = (item or {}).get("image_url", {}).get("url") if isinstance(item, dict) else None
        if url and url.startswith("data:audio"):
            blob, mime = _decode_data_url(url)
            return save_media("audio", blob, mime, prefix="openrouter_audio"), None
    return None, "OpenRouter не вернул аудиоданные в ответе модели."


def generate_pollinations_image(model: dict, prompt: str, options=None, on_status=None):
    from urllib.parse import quote
    options = {**(model.get("options") or {}), **(options or {})}
    width = int(options.get("width") or 1024)
    height = int(options.get("height") or 1024)
    params = f"width={width}&height={height}&nologo=true&model={quote(str(model['id']))}"
    if options.get("seed") is not None:
        params += f"&seed={int(options['seed'])}"
    key = _pollinations_key()
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    url = f"https://image.pollinations.ai/prompt/{quote(prompt)}?{params}"
    _report(on_status, f"Отправка запроса к Pollinations ({model['id']})")
    try:
        resp = requests.get(url, headers=headers, timeout=240)
    except requests.exceptions.Timeout:
        return None, "Pollinations: таймаут генерации изображения"
    except requests.exceptions.RequestException as exc:
        return None, f"Pollinations: {exc}"
    if resp.status_code >= 400:
        return None, _provider_error(resp)
    content_type = resp.headers.get("Content-Type", "")
    if not content_type.startswith("image/"):
        return None, f"Pollinations вернул не изображение (Content-Type: {content_type or 'нет'})."
    return save_media("image", resp.content, content_type, prefix="pollinations"), None


def _pollinations_voice(model: dict, catalog: dict | None, requested: str) -> str:
    """Голос, который модель точно принимает.

    Если запрошенного голоса нет в каталоге (например, «nova» для модели
    Kokoro с голосами ``af_nova``), подставляем первый голос модели — иначе
    провайдер отвечает 400 на каждый запрос.
    """
    voices = [str(voice) for voice in (catalog or {}).get("voices") or []]
    if not voices:
        return requested or "nova"
    if requested and requested in voices:
        return requested
    return voices[0]


def _pollinations_chat_audio(model_id: str, prompt: str, voice: str, audio_format: str,
                             key: str, headers: dict, on_status=None):
    """Синтез через /v1/chat/completions для чат-моделей с аудио-выходом.

    Именно так работают ``openai/gpt-audio-mini`` (алиас ``openai-audio``) и
    ``openai/gpt-audio-1.5``: /v1/audio/speech для них отвечает
    «is a text model and cannot be used on the audio endpoint».
    """
    payload = {
        "model": model_id,
        "modalities": ["text", "audio"],
        "audio": {"voice": voice or "alloy", "format": audio_format},
        "messages": [{"role": "user", "content": prompt}],
    }
    _report(on_status, f"Pollinations /v1/chat/completions (аудио, {model_id})")
    try:
        resp = requests.post(f"{POLLINATIONS_BASE_URL}/v1/chat/completions",
                             headers=headers, json=payload, timeout=240)
    except requests.exceptions.Timeout:
        return None, "Pollinations: таймаут синтеза речи (chat endpoint)"
    except requests.exceptions.RequestException as exc:
        return None, f"Pollinations: {exc}"
    if resp.status_code >= 400:
        return None, _provider_error(resp)
    try:
        data = resp.json()
    except ValueError:
        return None, "Pollinations вернул некорректный JSON вместо аудио."
    message = (((data.get("choices") or [{}])[0] or {}).get("message") or {})
    audio = message.get("audio") or {}
    blob_value = audio.get("data") if isinstance(audio, dict) else None
    if not blob_value and isinstance(audio, dict):
        blob_value = audio.get("url")
    if not blob_value:
        # Некоторые ответы кладут аудио в content как data-URL.
        content = message.get("content")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and str(part.get("type")) == "audio":
                    blob_value = (part.get("audio") or {}).get("data") or part.get("data")
                    break
    if not blob_value:
        return None, ("Pollinations ответил без аудио: модель "
                      f"{model_id} вернула только текст.")
    blob, mime = _decode_data_url(str(blob_value))
    return save_media("audio", blob, mime or _MIME_BY_AUDIO_FORMAT.get(audio_format, "audio/mpeg"),
                      prefix="pollinations_tts"), None


def generate_pollinations_audio(model: dict, prompt: str, options=None, on_status=None):
    """Озвучка Pollinations с правильным endpoint для каждого типа модели.

    Раньше все модели отправлялись на ``/v1/audio/speech``. Для чат-моделей
    с аудио-выходом (``openai-audio`` = ``openai/gpt-audio-mini``) это давало
    HTTP 400 «is a text model and cannot be used on the audio endpoint».
    Теперь endpoint берётся из живого каталога, а если провайдер всё равно
    отвечает этой ошибкой — запрос повторяется на chat endpoint.
    """
    options = {**(model.get("options") or {}), **(options or {})}
    model_id = str(model.get("id") or "").strip()
    key = _pollinations_key()
    if not key:
        return None, ("Pollinations: нужен POLLINATIONS_API_KEY/POLLINATIONS_KEY "
                      f"(ключ бесплатный: {POLLINATIONS_KEYS_URL}).")
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    # Каталог уточняет endpoint, голоса и алиасы. Сначала смотрим уже
    # загруженный снимок (без сети): озвучка не должна ждать каталог. В сеть идём
    # только если модель неизвестна и endpoint не записан в самой модели.
    catalog = None
    try:
        catalog = pollinations_tts_model(model_id, allow_fetch=False)
        if not catalog and not options.get("endpoint"):
            catalog = pollinations_tts_model(model_id)
    except Exception:  # noqa: BLE001 — каталог не должен ломать озвучку
        catalog = None
    if catalog:
        model_id = catalog.get("id") or model_id
        endpoint = catalog.get("endpoint") or "speech"
    else:
        endpoint = options.get("endpoint") or "speech"
    # Голоса берём из каталога, иначе — из самой модели (options["voices"]).
    voice = _pollinations_voice(model, catalog or options, str(options.get("voice") or ""))
    if options.get("voice") and catalog and voice != options.get("voice"):
        _report(on_status, f"Голос {options.get('voice')} недоступен в модели "
                           f"{model_id} — использую {voice}")
    audio_format = str(options.get("format") or options.get("response_format") or "mp3").lower()
    if audio_format not in _MIME_BY_AUDIO_FORMAT:
        audio_format = "mp3"

    if endpoint == "chat":
        return _pollinations_chat_audio(model_id, prompt, voice, audio_format,
                                        key, headers, on_status)

    payload = {
        "model": model_id,
        "input": prompt,
        "voice": voice,
        "response_format": audio_format,
    }
    _report(on_status, f"Pollinations /v1/audio/speech ({model_id}, голос {voice})")
    try:
        resp = requests.post(f"{POLLINATIONS_BASE_URL}/v1/audio/speech",
                             headers=headers, json=payload, timeout=240)
    except requests.exceptions.Timeout:
        return None, "Pollinations: таймаут синтеза речи"
    except requests.exceptions.RequestException as exc:
        return None, f"Pollinations: {exc}"
    if resp.status_code >= 400:
        error = _provider_error(resp)
        low = error.lower()
        if "text model" in low or "text endpoint" in low:
            # Провайдер сам подсказывает: модель принимает только chat endpoint.
            _report(on_status, "Pollinations: это чат-модель с аудио-выходом — "
                               "повторяю запрос через /v1/chat/completions")
            return _pollinations_chat_audio(model_id, prompt, voice, audio_format,
                                            key, headers, on_status)
        if "not found" in low or "unknown model" in low or "does not exist" in low:
            return None, (f"Pollinations: модели «{model_id}» нет в актуальном каталоге. "
                          "Обновите список моделей в настройках озвучки. "
                          f"Ответ провайдера: {error}")
        return None, error
    content_type = resp.headers.get("Content-Type", "") \
        or _MIME_BY_AUDIO_FORMAT.get(audio_format, "audio/mpeg")
    if not content_type.startswith("audio/"):
        return None, f"Pollinations вернул не аудио (Content-Type: {content_type})."
    return save_media("audio", resp.content, content_type, prefix="pollinations_tts"), None


def generate_pollinations_video(model: dict, prompt: str, options=None, on_status=None):
    from urllib.parse import quote
    options = options or {}
    key = _pollinations_key()
    if not key:
        return None, "Pollinations: нужен POLLINATIONS_API_KEY/POLLINATIONS_KEY для генерации видео."
    params = [f"model={quote(str(model['id']))}"]
    if options.get("duration"):
        params.append(f"duration={int(options['duration'])}")
    if options.get("aspect_ratio"):
        params.append(f"aspect_ratio={quote(str(options['aspect_ratio']))}")
    url = f"https://gen.pollinations.ai/video/{quote(prompt)}?{'&'.join(params)}"
    _report(on_status, f"Отправка запроса к Pollinations video ({model['id']})")
    try:
        resp = requests.get(url, headers={"Authorization": f"Bearer {key}"}, timeout=600)
    except requests.exceptions.Timeout:
        return None, "Pollinations: таймаут генерации видео"
    except requests.exceptions.RequestException as exc:
        return None, f"Pollinations: {exc}"
    if resp.status_code >= 400:
        return None, _provider_error(resp)
    content_type = resp.headers.get("Content-Type", "")
    if not content_type.startswith("video/"):
        return None, f"Pollinations вернул не видео (Content-Type: {content_type or 'нет'})."
    return save_media("video", resp.content, content_type, prefix="pollinations_video"), None


def generate_google_video(model: dict, prompt: str, options=None, on_status=None):
    """Запускает асинхронную задачу Veo. Возвращает job, а не файл."""
    options = options or {}
    key = _google_key()
    if not key:
        return None, "Google AI Studio: GEMINI_API_KEY не найден в .env"
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model['id']}:predictLongRunning"
    body = {
        "instances": [{"prompt": prompt}],
        "parameters": {
            "aspectRatio": options.get("aspect_ratio") or "16:9",
        },
    }
    duration = options.get("duration")
    if duration:
        body["parameters"]["durationSeconds"] = int(duration)
    _report(on_status, f"Отправка задачи видео в Google AI Studio ({model['id']})")
    try:
        resp = requests.post(url, json=body, timeout=120,
                             headers={"Content-Type": "application/json", "x-goog-api-key": key})
    except requests.exceptions.Timeout:
        return None, "Google AI Studio: таймаут отправки видео-задачи"
    except requests.exceptions.RequestException as exc:
        return None, f"Google AI Studio: {exc}"
    if resp.status_code >= 400:
        return None, _provider_error(resp)
    try:
        data = resp.json()
    except ValueError:
        return None, "Google AI Studio вернул некорректный JSON для видео-задачи"
    operation = data.get("name")
    if not operation:
        return None, "Google AI Studio не вернул имя операции для видео-задачи."
    _report(on_status, f"Задача принята провайдером: {operation}")
    return {
        "kind": "video", "job_id": operation, "operation": operation,
        "status_url": f"https://generativelanguage.googleapis.com/v1beta/{operation}",
        "state": "queued", "provider": model["provider"], "model": model["id"],
    }, None


def generate_custom_video(model: dict, prompt: str, options=None, on_status=None):
    options = options or {}
    endpoint = os.getenv("VIDEO_API_URL", "")
    if not endpoint:
        return None, "VIDEO_API_URL не задан в .env"
    api_key = os.getenv("VIDEO_API_KEY", "") or os.getenv("OPENAI_API_KEY", "")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload = {
        "model": model["id"],
        "prompt": prompt,
        "duration": options.get("duration") or 5,
        "aspect_ratio": options.get("aspect_ratio") or "16:9",
    }
    _report(on_status, f"Отправка запроса к своему video-endpoint ({endpoint})")
    try:
        resp = requests.post(endpoint, json=payload, headers=headers, timeout=240)
    except requests.exceptions.Timeout:
        return None, "Свой video-endpoint: таймаут запроса"
    except requests.exceptions.RequestException as exc:
        return None, f"Свой video-endpoint: {exc}"
    if resp.status_code >= 400:
        return None, _provider_error(resp)
    try:
        data = resp.json()
    except ValueError:
        return None, "Свой video-endpoint вернул некорректный JSON"
    direct_url = data.get("video_url") or data.get("url")
    if direct_url and not (data.get("status_url") or data.get("id") or data.get("job_id")):
        return {"kind": "video", "kind_ready": True, "url": direct_url, "state": "succeeded",
                "provider": "custom", "model": model["id"], "raw": data}, None
    return {
        "kind": "video",
        "job_id": data.get("id") or data.get("job_id") or uuid.uuid4().hex,
        "status_url": data.get("status_url"),
        "state": data.get("status") or "queued",
        "provider": "custom", "model": model["id"], "raw": data,
    }, None


def generate_custom_image(model: dict, prompt: str, options=None, on_status=None):
    options = options or {}
    endpoint = os.getenv("IMAGE_GENERATION_URL", "")
    if not endpoint:
        return None, "IMAGE_GENERATION_URL не задан в .env"
    api_key = os.getenv("IMAGE_API_KEY", "") or os.getenv("OPENAI_API_KEY", "")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    _report(on_status, f"Отправка запроса к своему image-endpoint ({endpoint})")
    try:
        resp = requests.post(endpoint, headers=headers, timeout=240, json={
            "model": model["id"], "prompt": prompt,
            "size": options.get("size") or "1024x1024", "n": 1,
        })
    except requests.exceptions.Timeout:
        return None, "Свой image-endpoint: таймаут запроса"
    except requests.exceptions.RequestException as exc:
        return None, f"Свой image-endpoint: {exc}"
    if resp.status_code >= 400:
        return None, _provider_error(resp)
    try:
        result = (resp.json().get("data") or [{}])[0]
    except (ValueError, IndexError, TypeError):
        return None, "Свой image-endpoint вернул неожиданный формат ответа"
    if result.get("url"):
        return {"kind": "image", "kind_ready": True, "url": result["url"],
                "provider": "custom", "model": model["id"]}, None
    if result.get("b64_json"):
        blob, mime = _decode_data_url(result["b64_json"])
        return save_media("image", blob, mime or "image/png", prefix="custom_image"), None
    return None, "Свой image-endpoint не вернул изображение."


def generate_custom_audio(model: dict, prompt: str, options=None, on_status=None):
    options = options or {}
    endpoint = os.getenv("AUDIO_TTS_URL", "")
    if not endpoint:
        return None, "AUDIO_TTS_URL не задан в .env"
    api_key = os.getenv("AUDIO_API_KEY", "") or os.getenv("OPENAI_API_KEY", "")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    _report(on_status, f"Отправка запроса к своему TTS-endpoint ({endpoint})")
    try:
        resp = requests.post(endpoint, headers=headers, timeout=180, json={
            "model": model["id"], "input": prompt,
            "voice": options.get("voice") or "alloy", "response_format": "mp3",
        })
    except requests.exceptions.Timeout:
        return None, "Свой TTS-endpoint: таймаут запроса"
    except requests.exceptions.RequestException as exc:
        return None, f"Свой TTS-endpoint: {exc}"
    if resp.status_code >= 400:
        return None, _provider_error(resp)
    if not resp.content:
        return None, "Свой TTS-endpoint вернул пустой ответ."
    return save_media("audio", resp.content,
                      resp.headers.get("Content-Type", "audio/mpeg"), prefix="custom_tts"), None


BACKENDS = {
    "google_image": generate_google_image,
    "google_tts": generate_google_audio,
    "google_video": generate_google_video,
    "groq_tts": generate_groq_audio,
    "openrouter_image": generate_openrouter_image,
    "openrouter_audio": generate_openrouter_audio,
    "pollinations_image": generate_pollinations_image,
    "pollinations_audio": generate_pollinations_audio,
    "pollinations_video": generate_pollinations_video,
    "custom_image": generate_custom_image,
    "custom_tts": generate_custom_audio,
    "custom_video": generate_custom_video,
}


# ======================================================================
# Задачи (для асинхронного видео)
# ======================================================================
def register_job(job: dict) -> str:
    job_id = job.get("job_id") or uuid.uuid4().hex
    record = dict(job)
    record.update({
        "job_id": job_id,
        "state": job.get("state") or "queued",
        "created_at": time.time(),
        "updated_at": time.time(),
        "polls": 0,
        "events": [{"at": time.time(), "status": f"Задача принята провайдером: {job_id}"}],
    })
    with _JOBS_LOCK:
        _JOBS[job_id] = record
    return job_id


def _append_event(record: dict, status: str):
    record["events"] = (record.get("events") or [])[-40:]
    record["events"].append({"at": time.time(), "status": status})
    record["updated_at"] = time.time()


def poll_job(job_id: str) -> dict | None:
    """Опрашивает провайдера и возвращает реальный статус задачи."""
    with _JOBS_LOCK:
        record = dict(_JOBS.get(job_id) or {})
    if not record:
        return None
    if record.get("state") in {"succeeded", "failed", "cancelled"}:
        return record

    record["polls"] = int(record.get("polls") or 0) + 1
    provider = record.get("provider")
    status_url = record.get("status_url")

    if provider == "google_ai_studio" and record.get("operation"):
        key = _google_key()
        try:
            resp = requests.get(
                f"https://generativelanguage.googleapis.com/v1beta/{record['operation']}",
                headers={"x-goog-api-key": key}, timeout=60,
            )
        except (requests.exceptions.Timeout, requests.exceptions.RequestException) as exc:
            _append_event(record, f"Опрос задачи: {exc}")
            record["state"] = record.get("state") or "queued"
            _JOBS[job_id] = record
            return record
        if resp.status_code >= 400:
            _append_event(record, _provider_error(resp))
            record["state"] = "failed"
            record["error"] = _provider_error(resp)
            _JOBS[job_id] = record
            return record
        try:
            data = resp.json()
        except ValueError:
            _append_event(record, "Опрос задачи: провайдер вернул некорректный JSON")
            _JOBS[job_id] = record
            return record
        if data.get("error"):
            record["state"] = "failed"
            record["error"] = json.dumps(data["error"], ensure_ascii=False)[:800]
            _append_event(record, f"Провайдер сообщил об ошибке: {record['error']}")
            _JOBS[job_id] = record
            return record
        if not data.get("done"):
            record["state"] = "running"
            _append_event(record, "Провайдер выполняет задачу (done=false)")
            _JOBS[job_id] = record
            return record
        # Готово: ищем файл в response.
        response = data.get("response") or {}
        video_uri = None
        for sample in (response.get("generateVideoResponse") or {}).get("generatedSamples", []) or []:
            video_uri = ((sample.get("video") or {}).get("uri")) or sample.get("uri")
            if video_uri:
                break
        if not video_uri:
            record["state"] = "failed"
            record["error"] = "Провайдер завершил задачу, но не вернул файл видео."
            _append_event(record, record["error"])
            _JOBS[job_id] = record
            return record
        try:
            download = requests.get(
                video_uri, timeout=180,
                params={"key": key} if "generativelanguage.googleapis.com" in video_uri else None,
            )
            download.raise_for_status()
        except (requests.exceptions.Timeout, requests.exceptions.RequestException) as exc:
            record["state"] = "failed"
            record["error"] = f"Не удалось скачать видео провайдера: {exc}"
            _append_event(record, record["error"])
            _JOBS[job_id] = record
            return record
        saved = save_media("video", download.content,
                           download.headers.get("Content-Type", "video/mp4"), prefix="veo")
        record.update({"state": "succeeded", "media": saved})
        _append_event(record, "Видео получено от провайдера и сохранено")
        _JOBS[job_id] = record
        return record

    # Прочие провайдеры: опрашиваем status_url, если он есть.
    if status_url:
        headers = {}
        api_key = os.getenv("VIDEO_API_KEY", "") or os.getenv("OPENAI_API_KEY", "")
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        try:
            resp = requests.get(status_url, headers=headers, timeout=60)
            if resp.status_code >= 400:
                record["state"] = "failed"
                record["error"] = _provider_error(resp)
            else:
                data = resp.json()
                state = str(data.get("status") or data.get("state") or "running").lower()
                record["state"] = state
                record["raw"] = data
                video_url = data.get("video_url") or data.get("url")
                if state in {"succeeded", "completed", "success"} and video_url:
                    record["media"] = {"kind": "video", "url": video_url, "mime": "video/mp4",
                                       "filename": os.path.basename(video_url.split("?")[0]) or "video.mp4"}
                elif state in {"failed", "error", "cancelled"}:
                    record["error"] = data.get("error") or "Провайдер сообщил об ошибке задачи."
        except (requests.exceptions.Timeout, requests.exceptions.RequestException, ValueError) as exc:
            _append_event(record, f"Опрос задачи: {exc}")
        _append_event(record, f"Статус провайдера: {record.get('state')}")
        with _JOBS_LOCK:
            _JOBS[job_id] = record
        return record

    record["state"] = record.get("state") or "queued"
    _append_event(record, "У задачи нет status_url — статус уточнить нельзя")
    with _JOBS_LOCK:
        _JOBS[job_id] = record
    return record


def get_job(job_id: str) -> dict | None:
    with _JOBS_LOCK:
        record = _JOBS.get(job_id)
    return dict(record) if record else None


def job_public(record: dict) -> dict:
    """Поля задачи, которые можно отдавать в браузер."""
    return {
        "job_id": record.get("job_id"),
        "state": record.get("state"),
        "provider": record.get("provider"),
        "model": record.get("model"),
        "polls": record.get("polls", 0),
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at"),
        "error": record.get("error"),
        "media": record.get("media"),
        "events": record.get("events") or [],
    }


# ======================================================================
# Точка входа генерации
# ======================================================================
def generate_media(model: dict, prompt: str, options=None, on_status=None, confirm=None):
    """
    Выполняет генерацию. Возвращает (result, None) или (None, error).

    result: {"kind", "url", "filename", "mime", ...} — файл готов,
            либо {"kind": "video", "job_id", ...} — задача поставлена в очередь.
    """
    prompt = (prompt or "").strip()
    if not prompt:
        return None, "Пустой промпт: опишите, что нужно создать."
    if not isinstance(model, dict) or not model.get("id"):
        return None, "Медиа-модель не выбрана."

    error = validate_selection(model, confirm)
    if error:
        return None, error

    backend = BACKENDS.get(model.get("backend") or "")
    if backend is None:
        return None, f"Для модели {model['id']} нет реализации генерации ({model.get('backend')})."

    started = time.time()
    _report(on_status, f"Модель: {model.get('name')} ({model.get('provider_name')}) · "
                       f"{model.get('pricing_label', '').lower()}")
    try:
        result, error = backend(model, prompt, options, on_status)
    except Exception as exc:  # защищаем чат от падения на неожиданной ошибке бэкенда
        return None, f"Ошибка генерации: {exc}"
    if error:
        return None, error

    elapsed_ms = int((time.time() - started) * 1000)
    _report(on_status, f"Готово за {elapsed_ms / 1000:.1f} c")
    result = dict(result)
    result.setdefault("kind", model.get("media_type"))
    result.update({
        "model": model["id"],
        "model_name": model.get("name"),
        "provider": model["provider"],
        "provider_name": model.get("provider_name"),
        "pricing_status": model.get("pricing_status"),
        "elapsed_ms": elapsed_ms,
    })
    if result.get("job_id"):
        job_id = register_job(result)
        result["job_id"] = job_id
        result["state"] = result.get("state") or "queued"
    return result, None
