"""tts_service.py — настоящая озвучка ответов ИИ (text-to-speech).

Зачем отдельный модуль
----------------------
В NovaMind уже есть генерация аудио: ``media_generation`` умеет четыре TTS-бэкенда
(Google AI Studio, Groq PlayAI, Pollinations, свой ``AUDIO_TTS_URL``), а
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
* :func:`voice_catalog` / :func:`status` — реальный список провайдеров, моделей
  и голосов с честным признаком «доступен / нужен ключ / почему недоступен».
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

#: Голоса Groq PlayAI. Официальный playai-tts синтезирует английскую речь,
#: поэтому в списке честно указано, что русский текст он не озвучит.
GROQ_VOICES = (
    ("autumn", "Autumn · женский (en)", (VOICE_EN,)),
    ("diana", "Diana · женский (en)", (VOICE_EN,)),
    ("hannah", "Hannah · женский (en)", (VOICE_EN,)),
    ("austin", "Austin · мужской (en)", (VOICE_EN,)),
    ("daniel", "Daniel · мужской (en)", (VOICE_EN,)),
    ("troy", "Troy · мужской (en)", (VOICE_EN,)),
)

#: Голоса Pollinations (OpenAI-совместимые названия; язык — по тексту).
POLLINATIONS_VOICES = (
    ("nova", "nova · женский", (VOICE_RU, VOICE_EN)),
    ("alloy", "alloy · нейтральный", (VOICE_RU, VOICE_EN)),
    ("echo", "echo · мужской", (VOICE_RU, VOICE_EN)),
    ("fable", "fable · рассказчик", (VOICE_RU, VOICE_EN)),
    ("onyx", "onyx · глубокий мужской", (VOICE_RU, VOICE_EN)),
    ("shimmer", "shimmer · мягкий женский", (VOICE_RU, VOICE_EN)),
)

#: Свой endpoint (AUDIO_TTS_URL) обычно OpenAI-совместим.
CUSTOM_VOICES = POLLINATIONS_VOICES

PROVIDER_LABELS = {
    "google_ai_studio": "Google AI Studio (Gemini TTS)",
    "groq": "Groq (PlayAI TTS)",
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
    "pollinations": "Нужен POLLINATIONS_API_KEY — без него синтез речи не работает.",
    "custom": "Нужен AUDIO_TTS_URL в .env (OpenAI-совместимый /audio/speech).",
}


def _audio_models() -> list[dict]:
    """TTS-модели из общего реестра медиа (без сетевых запросов)."""
    return [dict(item) for item in mg.MEDIA_MODEL_REGISTRY
            if item.get("media_type") == "audio" and item.get("backend") in _VOICES_BY_BACKEND]


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


def _all_models() -> list[dict]:
    models = _audio_models()
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
    """Голоса конкретной TTS-модели в форме, удобной интерфейсу."""
    backend = model.get("backend") or ""
    default = (model.get("options") or {}).get("voice") or ""
    items = []
    for voice_id, label, languages in _VOICES_BY_BACKEND.get(backend, ()):
        items.append({"id": voice_id, "label": label, "languages": list(languages),
                      "default": voice_id == default})
    return items


def voice_catalog(provider: str = "") -> list[dict]:
    """Все голоса (или только выбранного провайдера) с признаком доступности."""
    catalog = []
    for model in _all_models():
        item_provider = model.get("provider") or ""
        if provider and item_provider != provider:
            continue
        available = _provider_available(item_provider)
        for voice in voices_for(model):
            catalog.append({**voice, "provider": item_provider,
                            "provider_name": PROVIDER_LABELS.get(item_provider, item_provider),
                            "model": model.get("id"), "available": available})
    return catalog


def provider_choices() -> list[dict]:
    """Провайдеры TTS: подключён или нет и почему."""
    seen, items = set(), []
    for model in _all_models():
        provider = model.get("provider") or ""
        if provider in seen:
            continue
        seen.add(provider)
        available = _provider_available(provider)
        items.append({
            "id": provider,
            "name": PROVIDER_LABELS.get(provider, provider),
            "available": available,
            "reason": "" if available else _PROVIDER_REASON.get(provider, "Провайдер не настроен."),
            "models": [{"id": m["id"], "name": m.get("name", m["id"])}
                       for m in _all_models() if m.get("provider") == provider],
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
    """Сохраняет только известные поля; провайдер и голос проверяются."""
    import config
    payload = dict(updates or {})
    provider = str(payload.get("provider") or "").strip()
    if provider and provider not in {item["id"] for item in provider_choices()}:
        raise ValueError(f"Неизвестный TTS-провайдер: {provider}")
    voice = str(payload.get("voice") or "").strip()
    if voice:
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


def resolve_model(provider: str = "", model_id: str = "") -> tuple[Optional[dict], str]:
    """Выбирает TTS-модель: явная настройка → первый доступный провайдер."""
    models = _all_models()
    if model_id:
        for model in models:
            if model.get("id") == model_id and (not provider or model.get("provider") == provider):
                if not _provider_available(model.get("provider") or ""):
                    return None, _PROVIDER_REASON.get(model.get("provider"), "Провайдер не настроен.")
                return model, ""
        return None, f"TTS-модель «{model_id}» не найдена."

    configured = settings()
    preferred = [provider or configured.get("provider") or "",
                 configured.get("provider") or ""]
    order = [item for item in preferred if item]
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


def status() -> dict:
    """Полное состояние озвучки для интерфейса (без секретов)."""
    configured = settings()
    model, error = resolve_model(configured.get("provider") or "", configured.get("model") or "")
    voices = voice_catalog(configured.get("provider") or "")
    default_voice = ""
    if model:
        default_voice = configured.get("voice") or (model.get("options") or {}).get("voice") or ""
    return {
        "available": model is not None,
        "mode": "ai" if model else "browser",
        "provider": (model or {}).get("provider", ""),
        "provider_name": PROVIDER_LABELS.get((model or {}).get("provider", ""), ""),
        "model": (model or {}).get("id", ""),
        "model_name": (model or {}).get("name", ""),
        "voice": default_voice,
        "voices": voices,
        "providers": provider_choices(),
        "settings": configured,
        "limits": {"max_chars": MAX_TEXT_CHARS, "chunk_chars": CHUNK_CHARS,
                   "max_chunks": MAX_CHUNKS, "timeout": REQUEST_TIMEOUT},
        "error": error,
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
        yield {"type": "error", "error": error, "mode": "browser"}
        return

    resolved = _ready_model(resolved)
    chunks = split_text(prepared, limit=CHUNK_CHARS)
    if not chunks:
        yield {"type": "error", "error": "Не удалось разбить текст для озвучки."}
        return
    voice_id = voice or (settings().get("voice") or "") or (resolved.get("options") or {}).get("voice") or ""
    detected = lang or detect_language(prepared)

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
                confirm={"unknown": resolved.get("provider") == "custom"},
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
                yield {"type": "error", "error": f"Озвучка не удалась: {error}"}
                return
            yield {"type": "chunk_error", "index": index, "error": str(error)}
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
