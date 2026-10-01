"""
media_agent.py — генерация изображений, аудио и видео по решению самого ИИ.

Кнопок «Медиа» и ручного выбора модели в чате больше нет. Когда ИИ решает,
что пользователю нужна картинка, озвучка или ролик, он вызывает действие
image / audio / video, а этот модуль сам:

  1. собирает подходящие модели подключённых провайдеров;
  2. выбирает лучшую (или ту, которую назвал ИИ);
  3. генерирует файл, а если провайдер отказал — пробует следующую модель.

Деньги пользователя не тратятся без спроса: автоматически берутся только
модели с подтверждённым бесплатным API и собственные endpoint'ы из .env
(IMAGE_GENERATION_URL, AUDIO_TTS_URL, VIDEO_API_URL). Платные модели и модели
на пробных кредитах подключаются явным флагом MEDIA_ALLOW_PAID=1.

Модуль не импортирует Flask — его удобно тестировать отдельно.
"""
import os
import re
import threading
import time

import media_generation as mg
from i18n import tr, variants

KINDS = ("image", "audio", "video")
def is_attempt(message):
    """Строка «Пробую <модель>» на любом языке интерфейса (для краткого статуса в чате)."""
    prefixes = tuple(text.split("{")[0] for text in variants("Пробую {name} ({provider})"))
    return str(message or "").startswith(prefixes)


KIND_LABELS = {"image": "изображение", "audio": "аудио", "video": "видео"}

# Какого провайдера пробовать первым для каждого типа медиа.
# Свой endpoint из .env — всегда первый: пользователь настроил его сам.
PROVIDER_ORDER = {
    "image": ("custom", "pollinations", "openrouter", "google_ai_studio"),
    "audio": ("custom", "google_ai_studio", "groq", "pollinations", "openrouter"),
    "video": ("custom", "pollinations", "google_ai_studio", "openrouter"),
}
# Внутри провайдера — любимые модели (остальные после них, по алфавиту).
MODEL_ORDER = {
    "pollinations": ("flux", "turbo", "openai-audio"),
    "google_ai_studio": ("gemini-3.8-flash-tts", "gemini-3.8-flash-lite-tts",
                         "gemini-3.1-flash-image", "veo-3.1-fast-generate-preview"),
}
MAX_ATTEMPTS = int(os.getenv("MEDIA_MAX_ATTEMPTS", "3"))
CACHE_TTL = 600

# Что подключить, если моделей нет совсем — ИИ передаст это пользователю.
SETUP_HINTS = {
    "image": ("POLLINATIONS_API_KEY (бесплатный ключ на enter.pollinations.ai), "
              "OPENROUTER_API_KEY с бесплатной image-моделью или свой IMAGE_GENERATION_URL"),
    "audio": "GEMINI_API_KEY или GROQ_API_KEY (бесплатные TTS), POLLINATIONS_API_KEY или свой AUDIO_TTS_URL",
    "video": ("свой VIDEO_API_URL, POLLINATIONS_API_KEY (если у Pollinations есть бесплатная "
              "видео-модель) или GEMINI_API_KEY + MEDIA_ALLOW_PAID=1 (Veo — платно)"),
}

_cache = {}
_cache_lock = threading.Lock()


def allow_paid() -> bool:
    return os.getenv("MEDIA_ALLOW_PAID", "0") == "1"


def reset_cache():
    with _cache_lock:
        _cache.clear()


def _signature():
    """Ключ кэша: меняется, если подключили новый ключ или флаг цены."""
    return (tuple(sorted(mg.provider_statuses().items())), allow_paid(),
            os.getenv("IMAGE_GENERATION_URL", ""), os.getenv("AUDIO_TTS_URL", ""),
            os.getenv("VIDEO_API_URL", ""))


def _usable(model: dict) -> bool:
    """Можно ли брать модель без вопроса пользователю."""
    if not model.get("provider_connected") or model.get("in_provider_catalog") is False:
        return False
    status = model.get("pricing_status")
    if status == mg.PRICING_FREE or model.get("provider") == "custom":
        return True
    return allow_paid()


def _static_fallback(kind: str) -> list:
    """Бесплатные модели Pollinations из реестра — если живой каталог не ответил."""
    if not mg.provider_connected("pollinations"):
        return []
    models = []
    for item in mg.MEDIA_MODEL_REGISTRY:
        if item.get("provider") != "pollinations" or item.get("media_type") != kind:
            continue
        if item.get("pricing_status") != mg.PRICING_FREE:
            continue
        model = dict(item)
        model.update({
            "provider_name": mg.MEDIA_PROVIDERS["pollinations"],
            "provider_connected": True,
            "in_provider_catalog": None,
            "pricing_label": mg.PRICING_LABELS[mg.PRICING_FREE],
        })
        models.append(model)
    return models


def _rank(kind: str, model: dict):
    providers = PROVIDER_ORDER.get(kind, ())
    provider = model.get("provider")
    provider_rank = providers.index(provider) if provider in providers else len(providers)
    favourites = MODEL_ORDER.get(provider, ())
    model_rank = favourites.index(model["id"]) if model["id"] in favourites else len(favourites)
    price_rank = 0 if model.get("pricing_status") == mg.PRICING_FREE or provider == "custom" else 1
    return (price_rank, provider_rank, model_rank, str(model.get("name", "")).lower())


def candidates(kind: str, refresh: bool = False) -> list:
    """Модели, которыми ИИ может сгенерировать `kind`, лучшие — первыми."""
    if kind not in KINDS:
        return []
    key = (kind, _signature())
    with _cache_lock:
        cached = _cache.get(key)
        if cached and not refresh and time.time() - cached[0] < CACHE_TTL:
            return [dict(model) for model in cached[1]]

    paid = allow_paid()
    try:
        catalog = mg.media_catalog(media_type=kind, include_paid=paid, include_unknown=True,
                                   include_trial=paid, refresh=refresh)
    except Exception as exc:          # каталог не должен ронять ответ ИИ
        print(f"[media-agent] каталог недоступен: {exc}")
        catalog = []
    models = [model for model in catalog if _usable(model)]
    if not any(model.get("provider") == "pollinations" for model in models):
        models.extend(_static_fallback(kind))
    models.sort(key=lambda model: _rank(kind, model))

    unique, seen = [], set()
    for model in models:
        ident = (model.get("provider"), model.get("id"))
        if ident not in seen:
            seen.add(ident)
            unique.append(model)
    with _cache_lock:
        _cache[key] = (time.time(), unique)
    return [dict(model) for model in unique]


def quick_available(kind: str) -> bool:
    """Быстрая проверка без сети: подключён ли хоть один провайдер для `kind`.

    Нужна для /api/agent/status — он вызывается при каждой загрузке чата,
    и ждать живые каталоги провайдеров там нельзя.
    """
    env_url = {"image": "IMAGE_GENERATION_URL", "audio": "AUDIO_TTS_URL", "video": "VIDEO_API_URL"}.get(kind)
    if env_url and os.getenv(env_url):
        return True
    free_providers = {
        "image": ("pollinations", "openrouter"),
        "audio": ("google_ai_studio", "groq", "pollinations", "openrouter"),
        "video": ("pollinations",),
    }.get(kind, ())
    if any(mg.provider_connected(provider) for provider in free_providers):
        return True
    return allow_paid() and mg.provider_connected("google_ai_studio")


def _prefer(models: list, wanted: str) -> list:
    """Модель, которую назвал ИИ, — первой (по id, имени или провайдеру)."""
    wanted = (wanted or "").strip().lower()
    if not wanted:
        return models

    def score(model):
        ident = str(model.get("id", "")).lower()
        name = str(model.get("name", "")).lower()
        provider = str(model.get("provider", "")).lower()
        if wanted in (ident, name):
            return 0
        if wanted in ident or wanted in name:
            return 1
        if wanted == provider:
            return 2
        return 3

    return sorted(models, key=score)


def describe(kind: str = "all") -> str:
    """Список доступных моделей — ответ на действие media_models."""
    kinds = KINDS if kind in ("", "all", None) else (kind,)
    lines = []
    for item in kinds:
        models = candidates(item)
        if not models:
            lines.append(f"{KIND_LABELS.get(item, item)}: моделей нет. Подключить: {SETUP_HINTS[item]}")
            continue
        lines.append(f"{KIND_LABELS.get(item, item)} (первая — по умолчанию):")
        for model in models[:8]:
            price = "свой endpoint" if model.get("provider") == "custom" else model.get("pricing_label", "")
            about = str(model.get("description") or "")[:90]
            lines.append(f"  • {model['id']} — {model.get('name')} · {model.get('provider_name')} · {price}"
                         + (f" · {about}" if about else ""))
    return "\n".join(lines)


_SIZE = re.compile(r"^\s*(\d{2,4})\s*[x×*]\s*(\d{2,4})\s*$")
_ASPECTS = {
    "1:1": (1024, 1024), "16:9": (1344, 768), "9:16": (768, 1344),
    "4:3": (1152, 864), "3:4": (864, 1152), "3:2": (1216, 832), "2:3": (832, 1216),
}


def build_options(kind: str, action: dict) -> dict:
    """Параметры генерации из действия ИИ (размер, голос, длительность…)."""
    options = {}
    if kind == "image":
        size = str(action.get("size") or "").strip()
        aspect = str(action.get("aspect") or action.get("aspect_ratio") or "").strip()
        match = _SIZE.match(size)
        if match:
            width, height = int(match.group(1)), int(match.group(2))
        elif aspect in _ASPECTS:
            width, height = _ASPECTS[aspect]
        else:
            width, height = 1024, 1024
        width = max(256, min(2048, width))
        height = max(256, min(2048, height))
        options.update({"width": width, "height": height, "size": f"{width}x{height}"})
        if aspect:
            options["aspect_ratio"] = aspect
        if action.get("seed") is not None:
            try:
                options["seed"] = int(action["seed"])
            except (TypeError, ValueError):
                pass
    elif kind == "audio":
        if action.get("voice"):
            options["voice"] = str(action["voice"])[:40]
    elif kind == "video":
        try:
            options["duration"] = max(1, min(20, int(action.get("duration") or 5)))
        except (TypeError, ValueError):
            options["duration"] = 5
        options["aspect_ratio"] = str(action.get("aspect") or action.get("aspect_ratio") or "16:9")
    return options


def generate(kind: str, prompt: str, options=None, wanted_model: str = "", on_status=None):
    """Генерирует медиа, перебирая модели. Возвращает (result, model, errors).

    result — как у media_generation.generate_media (url/filename/… или job_id
    для асинхронного видео); None, если не вышло ни у одной модели.
    """
    prompt = (prompt or "").strip()
    if kind not in KINDS:
        return None, None, [f"Неизвестный тип медиа: {kind}"]
    if not prompt:
        return None, None, ["Пустое описание: что именно создать?"]
    models = _prefer(candidates(kind), wanted_model)
    if not models:
        return None, None, [f"Нет доступных моделей ({KIND_LABELS[kind]}). Подключить: {SETUP_HINTS[kind]}"]

    errors = []
    for model in models[:max(1, MAX_ATTEMPTS)]:
        if on_status:
            on_status(tr("Пробую {name} ({provider})", name=model.get('name'),
                         provider=model.get('provider_name')))
        # Цену уже проверили при отборе кандидатов — подтверждаем за пользователя
        # только то, что разрешено политикой выше.
        result, error = mg.generate_media(
            model, prompt, options=options or {}, on_status=on_status,
            confirm={"paid": True, "trial": True, "unknown": True},
        )
        if result:
            return result, model, errors
        errors.append(f"{model.get('name')}: {error}")
    return None, None, errors
