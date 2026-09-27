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
import json
import os
import re
import threading
import time
import uuid

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
PRICING_VERIFIED_AT = "2026-09-26"

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
    {
        "id": "playai-tts", "name": "PlayAI TTS (Groq)",
        "provider": "groq", "media_type": "audio",
        "pricing_status": PRICING_FREE,
        "pricing_note": "Бесплатный тариф Groq с лимитами RPM/RPD; банковская карта не требуется.",
        "pricing_source": GROQ_MODELS_SOURCE, "pricing_verified_at": PRICING_VERIFIED_AT,
        "description": "Синтез речи через OpenAI-compatible endpoint /v1/audio/speech.",
        "backend": "groq_tts", "options": {"voice": "Fritz-PlayAI", "format": "wav"},
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
        "id": "openai-audio", "name": "Pollinations OpenAI Audio",
        "provider": "pollinations", "media_type": "audio",
        "pricing_status": PRICING_FREE,
        "pricing_note": "Бесплатный endpoint без ключа; анонимные запросы ограничены по частоте.",
        "pricing_source": POLLINATIONS_SOURCE, "pricing_verified_at": PRICING_VERIFIED_AT,
        "description": "Озвучка текста в mp3 без ключа API.",
        "backend": "pollinations_audio", "keyless": True,
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

    entries = [dict(item) for item in MEDIA_MODEL_REGISTRY]
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
    "audio/ogg": ".ogg", "audio/aac": ".aac", "audio/l16": ".wav",
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
    return save_media("audio", blob, mime or "audio/wav", prefix="gemini_tts"), None


def generate_groq_audio(model: dict, prompt: str, options=None, on_status=None):
    options = {**(model.get("options") or {}), **(options or {})}
    from groq_rotation import get_groq_key
    key = get_groq_key()
    if not key:
        return None, "Groq: API ключ не найден в .env"
    payload = {
        "model": model["id"],
        "input": prompt,
        "voice": options.get("voice") or "Fritz-PlayAI",
        "response_format": options.get("format") or "wav",
    }
    _report(on_status, f"Отправка запроса к Groq /audio/speech ({model['id']})")
    try:
        resp = requests.post(
            "https://api.groq.com/openai/v1/audio/speech",
            json=payload, timeout=180,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        )
    except requests.exceptions.Timeout:
        return None, "Groq: таймаут запроса синтеза речи"
    except requests.exceptions.RequestException as exc:
        return None, f"Groq: {exc}"
    if resp.status_code >= 400:
        return None, _provider_error(resp)
    if not resp.content:
        return None, "Groq вернул пустой аудиопоток."
    _report(on_status, "Groq вернул аудио, сохраняю файл")
    mime = resp.headers.get("Content-Type", "") or f"audio/{payload['response_format']}"
    return save_media("audio", resp.content, mime, prefix="groq_tts"), None


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
    token = os.getenv(options.get("token_env", ""), "").strip()
    if token:
        params += f"&token={quote(token)}"
    url = f"https://image.pollinations.ai/prompt/{quote(prompt)}?{params}"
    _report(on_status, f"Отправка запроса к Pollinations ({model['id']})")
    try:
        resp = requests.get(url, timeout=180)
    except requests.exceptions.Timeout:
        return None, "Pollinations: таймаут генерации изображения"
    except requests.exceptions.RequestException as exc:
        return None, f"Pollinations: {exc}"
    if resp.status_code >= 400:
        return None, _provider_error(resp)
    content_type = resp.headers.get("Content-Type", "")
    if not content_type.startswith("image/"):
        return None, (f"Pollinations вернул не изображение (Content-Type: {content_type or 'нет'}). "
                      f"Возможно, сервис перегружен или модель требует кредиты Pollen.")
    _report(on_status, "Pollinations вернул изображение, сохраняю файл")
    return save_media("image", resp.content, content_type, prefix="pollinations"), None


def generate_pollinations_audio(model: dict, prompt: str, options=None, on_status=None):
    from urllib.parse import quote
    options = {**(model.get("options") or {}), **(options or {})}
    voice = options.get("voice") or "nova"
    url = (f"https://text.pollinations.ai/{quote(prompt)}"
           f"?model={quote(str(model['id']))}&voice={quote(str(voice))}")
    _report(on_status, f"Отправка запроса к Pollinations audio ({model['id']})")
    try:
        resp = requests.get(url, timeout=180)
    except requests.exceptions.Timeout:
        return None, "Pollinations: таймаут синтеза речи"
    except requests.exceptions.RequestException as exc:
        return None, f"Pollinations: {exc}"
    if resp.status_code >= 400:
        return None, _provider_error(resp)
    content_type = resp.headers.get("Content-Type", "")
    if not content_type.startswith("audio/"):
        return None, f"Pollinations вернул не аудио (Content-Type: {content_type or 'нет'})."
    _report(on_status, "Pollinations вернул аудио, сохраняю файл")
    return save_media("audio", resp.content, content_type, prefix="pollinations_tts"), None


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
