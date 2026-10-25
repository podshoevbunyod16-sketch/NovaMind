"""
config.py — Конфигурация NovaMind: переменные окружения, провайдеры, команды, плагины.
"""
import os
import re
import secrets
import sys
import json
import time
import logging

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


# ---------- Загрузка .env ----------
_ENV_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def clean_env_value(value: str) -> str:
    """Значение из .env без кавычек и хвостового комментария.

    Раньше строка разбиралась «как есть», и `ADMIN_USER="имя@example.com"` или
    `export ADMIN_CODE=…  # код` давали переменную с кавычками/комментарием
    внутри. Вход в админку после этого не работал, хотя пароль верный.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    if text[0] in "\"'":
        quote = text[0]
        end = text.find(quote, 1)
        return text[1:end] if end != -1 else text[1:]
    for marker in (" #", "\t#"):
        index = text.find(marker)
        if index != -1:
            text = text[:index]
            break
    return text.strip()


def parse_env_line(line: str):
    """Разобрать строку .env в ``(ключ, значение)``; ``None`` — не настройка.

    Поддерживаются формы, которые люди реально пишут в .env:
    ``KEY=value``, ``KEY = value``, ``export KEY=value``, ``KEY="value"``,
    ``KEY=value  # комментарий``.
    """
    text = str(line or "").strip().lstrip("\ufeff")
    if not text or text.startswith("#") or "=" not in text:
        return None
    key, _, value = text.partition("=")
    key = key.strip()
    if key.startswith("export "):
        key = key[len("export "):].strip()
    if not _ENV_KEY.fullmatch(key):
        return None
    return key, clean_env_value(value)


def load_env_file(path: str) -> dict:
    """Прочитать .env в ``os.environ`` (не перетирая уже заданные переменные)."""
    applied = {}
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            for line in handle:
                pair = parse_env_line(line)
                if pair is None:
                    continue
                key, value = pair
                if key in os.environ:
                    continue
                os.environ[key] = value
                applied[key] = value
    except OSError:
        return applied
    return applied


_env_path = os.path.join(BASE_DIR, ".env")
if os.path.exists(_env_path):
    load_env_file(_env_path)

PORT = int(os.getenv("PORT", "5000") or 5000)

# ---------- Google OAuth ----------
GOOGLE_CLIENT_ID     = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
SITE_URL             = os.getenv("SITE_URL", "http://localhost:5000").rstrip("/")
GOOGLE_REDIRECT_URI  = os.getenv("GOOGLE_REDIRECT_URI",
                                 f"{SITE_URL}/auth/google/callback")

# ---------- Безопасность ----------
# Файл с ключом подписи cookie. Раньше ключ был случайным на каждый запуск:
# после перезапуска сервера админ «терялся» — браузер считал его вошедшим
# (localStorage), а сервер уже нет, и настройки с MCP отвечали 403.
SESSION_SECRET_FILE = os.getenv("SESSION_SECRET_FILE",
                                os.path.join(BASE_DIR, ".session_secret"))


def load_session_secret(path: str | None = None, value: str | None = None) -> str:
    """Ключ подписи сессий: из `SESSION_SECRET` или из файла (переживает рестарт).

    Один ключ на все процессы: с несколькими воркерами gunicorn сессия,
    выданная одним, должна приниматься остальными.
    """
    explicit = clean_env_value(value if value is not None
                               else os.getenv("SESSION_SECRET", ""))
    if explicit:
        return explicit
    path = path or SESSION_SECRET_FILE
    try:
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as handle:
                saved = handle.read().strip()
            if saved:
                return saved
    except OSError:
        pass
    secret = secrets.token_hex(32)
    try:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        # Права 600: ключ читает только владелец сервера.
        handle = os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600),
                           "w", encoding="utf-8")
        with handle:
            handle.write(secret)
    except OSError:
        logger.warning("Не удалось сохранить %s: сессии сбросятся при перезапуске", path)
    return secret


# Пароль администратора. Предсказуемый дефолт опасен: без ADMIN_CODE
# в окружении генерируем случайный код и печатаем его в лог при старте —
# войти «по умолчанию» снаружи уже нельзя.
_admin_code_env = clean_env_value(os.getenv("ADMIN_CODE", ""))
ADMIN_CODE        = _admin_code_env or secrets.token_urlsafe(9)
ADMIN_CODE_IS_DEFAULT = not bool(_admin_code_env)
ADMIN_SESSION_KEY = load_session_secret()

# Логин администратора. Это может быть имя или e-mail (например, адрес
# Google-аккаунта): человек входит тем же логином, каким пользуется в чате.
ADMIN_USER        = (clean_env_value(os.getenv("ADMIN_USER", ""))
                     or clean_env_value(os.getenv("ADMIN_LOGIN", "")) or "admin")
# Необязательный второй логин — почта, если ADMIN_USER задан ником.
ADMIN_EMAIL       = clean_env_value(os.getenv("ADMIN_EMAIL", ""))
ADMIN_CREDENTIALS = {ADMIN_USER: ADMIN_CODE}
#: Все логины администратора без регистра (вход и Google-почта сравниваются так).
ADMIN_LOGINS      = frozenset(item.casefold() for item in (ADMIN_USER, ADMIN_EMAIL) if item)

# ---------- Провайдеры AI ----------
PROVIDERS = {
    "groq": {
        "url": "https://api.groq.com/openai/v1/chat/completions",
        "max_tokens": 32768,
        "headers": {
            "Content-Type": "application/json"
        },
        "models": [
            {"id": "openai/gpt-oss-120b", "name": "GPT-OSS 120B"},
            {"id": "openai/gpt-oss-20b", "name": "GPT-OSS 20B"},
            {"id": "qwen/qwen3.8-27b", "name": "Qwen 3.8 27B"},
        ]
    },
    "cerebras": {
        "url": "https://api.cerebras.ai/v1/chat/completions",
        "max_tokens": 16384,
        "headers": {
            "Authorization": f"Bearer {os.getenv('CEREBRAS_API_KEY', '')}",
            "Content-Type": "application/json"
        },
        "models": [
            {"id": "qwen-3-235b-a22b-instruct-2507",    "name": "Qwen 3 235B"},
            {"id": "zai-glm-4.7",                        "name": "Z.ai GLM 4.7"},
            {"id": "deepseek-r1-distill-llama-70b",      "name": "DeepSeek R1 Distill 70B"},
        ]
    },
    "openrouter": {
        "url": "https://openrouter.ai/api/v1/chat/completions",
        "max_tokens": 131072,
        "headers": {
            "Authorization": f"Bearer {os.getenv('OPENROUTER_API_KEY', '')}",
            "Content-Type": "application/json",
            "HTTP-Referer": os.getenv("SITE_URL", "http://localhost:5000").rstrip("/"),
            "X-Title": "NovaMind AI",
        },
        "models": [
            {"id": "google/gemini-2.0-flash-001",        "name": "Gemini 2.0 Flash (Free)"},
            {"id": "deepseek/deepseek-chat-v3-0324",     "name": "DeepSeek R1 (Free)"},
        ]
    },
    "google_ai_studio": {
        "url": f"https://generativelanguage.googleapis.com/v1beta/models/{{model}}:generateContent?key={os.getenv('GOOGLE_AI_STUDIO_KEY', '')}",
        "max_tokens": 8192,
        "headers": {
            "Content-Type": "application/json"
        },
        "models": [
            {"id": "gemini-2.0-flash",      "name": "Gemini 2.0 Flash"},
            {"id": "gemini-2.0-flash-lite", "name": "Gemini 2.0 Flash Lite"},
            {"id": "gemini-1.5-flash",      "name": "Gemini 1.5 Flash"},
            {"id": "gemini-1.5-pro",        "name": "Gemini 1.5 Pro"},
        ]
    },
    "pollinations": {
        "url": "https://gen.pollinations.ai/v1/chat/completions",
        "base_url": "https://gen.pollinations.ai/v1",
        "max_tokens": 131072,
        "headers": {
            "Authorization": f"Bearer {(os.getenv('POLLINATIONS_API_KEY') or os.getenv('POLLINATIONS_KEY') or os.getenv('POLLINATIONS_TOKEN') or '')}",
            "Content-Type": "application/json"
        },
        "models": []
    },
    "openai_compatible": {
        "url": (
            (os.getenv("OPENAI_COMPATIBLE_URL") or os.getenv("OPENAI_BASE_URL") or "http://127.0.0.1:8080/v1")
            .rstrip("/") + ("" if (os.getenv("OPENAI_COMPATIBLE_URL") or os.getenv("OPENAI_BASE_URL") or "").rstrip("/").endswith("/chat/completions") else "/chat/completions")
        ),
        "max_tokens": 8192,
        "headers": {
            "Authorization": f"Bearer {(os.getenv('OPENAI_COMPATIBLE_KEY') or os.getenv('OPENAI_API_KEY') or 'ollama')}",
            "Content-Type": "application/json",
        },
        "models": [
            {"id": os.getenv("LOCAL_MODEL_ID", "local-llama"),
             "name": os.getenv("LOCAL_MODEL_NAME", "Local Llama (llama.cpp)"), "context_length": 4096},
            {"id": "llama.cpp", "name": "Llama.cpp Local", "context_length": 4096}
        ]
    },
    # Встроенная офлайн-модель: не требует ни ключей, ни сети.
    # Нужна, чтобы чат отвечал всегда (демо-режим и аварийный контур).
    "local_demo": {
        "url": "local://novamind/offline",
        "max_tokens": 4096,
        "headers": {},
        "models": [{"id": "nova-local-1", "name": "Nova Local 1 (офлайн)", "context_length": 8192}],
    },
}

# Текущий провайдер и модель (меняются через /api/settings/select)
current_provider = "groq"
current_model    = "openai/gpt-oss-120b"
system_prompt    = os.getenv("SYSTEM_PROMPT", "Ты — NovaMind, умный AI-ассистент. Отвечай чётко и по делу.")

# Выбранная медиа-модель (изображение / аудио / видео).
# Пустой словарь — генерация медиа выключена, чат работает как обычный текстовый.
# Заполняется только явным выбором пользователя в панели «Медиа».
media_selection: dict = {}

# ---------- Язык интерфейса (i18n: tg | ru | en) ----------
ui_language: str = "tg"

# ---------- Озвучка ответов (TTS) ----------
# Одно место хранения: те же runtime_settings.json, что и модель/язык.
# UI хранит копию только как резерв, если сервер недоступен.
speech_settings: dict = {
    "auto_speak": False,   # озвучивать ответ ИИ автоматически
    "provider": "",        # "" — выбрать доступного провайдера автоматически
    "model": "",           # "" — модель по умолчанию у провайдера
    "voice": "",           # "" — голос по умолчанию у провайдера
    "read_code": False,    # читать вслух блоки кода (по умолчанию — нет)
    "lang": "",            # "" — язык определяется по тексту ответа
}


def normalize_speech_settings(value) -> dict:
    """Приводит настройки озвучки к известной схеме (без потери значений)."""
    base = dict(speech_settings)
    if not isinstance(value, dict):
        return base
    if "auto_speak" in value:
        base["auto_speak"] = bool(value.get("auto_speak"))
    if "read_code" in value:
        base["read_code"] = bool(value.get("read_code"))
    for key in ("provider", "model", "voice", "lang"):
        if key in value:
            base[key] = str(value.get(key) or "").strip()[:64]
    return base

# ---------- Кеш моделей ----------
MODEL_CACHE: dict = {}
MODEL_CACHE_TTL = 3600  # 1 час

# ---------- runtime_settings.json ----------
RUNTIME_SETTINGS_FILE = os.path.join(os.path.dirname(__file__), "runtime_settings.json")

def load_runtime_settings():
    """Загружает сохранённый провайдер/модель между перезапусками."""
    global current_provider, current_model, system_prompt, media_selection, speech_settings
    if os.path.exists(RUNTIME_SETTINGS_FILE):
        try:
            with open(RUNTIME_SETTINGS_FILE, "r", encoding="utf-8") as f:
                s = json.load(f)
            current_provider = s.get("provider", current_provider)
            current_model    = s.get("model",    current_model)
            # Автоматически мигрируем модели Groq, отключённые на Developer/free-тарифах.
            if current_provider == "groq" and current_model in {
                "llama-3.3-70b-versatile",
                "llama-3.1-8b-instant",
                "qwen/qwen3.6-27b",
            }:
                current_model = "openai/gpt-oss-120b"
            system_prompt    = s.get("system_prompt", system_prompt)
            saved_media = s.get("media_selection")
            media_selection = saved_media if isinstance(saved_media, dict) else {}
            saved_lang = s.get("ui_language")
            if saved_lang in ("tg", "ru", "en"):
                ui_language = saved_lang
            saved_speech = s.get("speech")
            if isinstance(saved_speech, dict):
                globals()["speech_settings"] = normalize_speech_settings(saved_speech)
        except Exception as e:
            logger.warning("Ошибка чтения runtime_settings: %s", e)

def _write_runtime_settings():
    with open(RUNTIME_SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump({
            "provider": current_provider,
            "model": current_model,
            "system_prompt": system_prompt,
            "media_selection": media_selection,
            "ui_language": ui_language,
            "speech": speech_settings,
        }, f, ensure_ascii=False, indent=2)

def save_selected_model(provider, model):
    """Сохраняет выбранный провайдер/модель в файл."""
    global current_provider, current_model
    current_provider = provider
    current_model    = model
    try:
        _write_runtime_settings()
    except Exception as e:
        logger.warning("Ошибка сохранения runtime_settings: %s", e)

def save_selected_media_model(selection):
    """Сохраняет выбранную медиа-модель (или очищает выбор, если selection пустое)."""
    global media_selection
    media_selection = dict(selection or {})
    try:
        _write_runtime_settings()
    except Exception as e:
        logger.warning("Ошибка сохранения медиа-модели: %s", e)
    return media_selection


def save_speech_settings(updates: dict | None = None) -> dict:
    """Сохраняет настройки озвучки (auto_speak, провайдер, голос, язык)."""
    global speech_settings
    speech_settings = normalize_speech_settings({**speech_settings, **(updates or {})})
    try:
        _write_runtime_settings()
    except Exception as e:
        logger.warning("Ошибка сохранения настроек озвучки: %s", e)
    return dict(speech_settings)


def save_ui_language(language: str):
    """Сохраняет выбранный язык интерфейса (tg | ru | en)."""
    global ui_language
    if language in ("tg", "ru", "en"):
        ui_language = language
    try:
        _write_runtime_settings()
    except Exception as e:
        logger.warning("Ошибка сохранения языка: %s", e)
    return ui_language


load_runtime_settings()


# ---------- Доступность провайдеров ----------
def has_credentials(provider: str) -> bool:
    """Подключён ли провайдер (есть ключ / не требует ключа вовсе)."""
    if provider == "groq":
        keys = [os.getenv("GROQ_API_KEY", "")] + [os.getenv(f"GROQ_API_KEY_{i}", "") for i in range(1, 10)]
        return any(k.strip() for k in keys)
    if provider == "cerebras":
        return bool(os.getenv("CEREBRAS_API_KEY", "").strip())
    if provider == "openrouter":
        return bool(os.getenv("OPENROUTER_API_KEY", "").strip())
    if provider == "google_ai_studio":
        return bool((os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_AI_STUDIO_KEY") or "").strip())
    if provider == "pollinations":
        # Каталог моделей публичный, но генерация текста требует ключ.
        return bool((os.getenv("POLLINATIONS_API_KEY")
                     or os.getenv("POLLINATIONS_KEY")
                     or os.getenv("POLLINATIONS_TOKEN") or "").strip())
    if provider == "openai_compatible":
        # Локальный llama-server / Ollama ключ не требуют.
        return True
    if provider == "local_demo":
        return True
    return False


# Автообнаружение локального OpenAI-compatible сервера (llama.cpp, Ollama,
# LM Studio). Выключено по умолчанию: без него первый запрос не тратит
# время на сетевой пробинг. LOCAL_LLM_AUTODETECT=1 — и чат сам подхватит
# запущенный локальный сервер, если облачных ключей нет.
LOCAL_LLM_AUTODETECT = os.getenv("LOCAL_LLM_AUTODETECT", "0") == "1"
_LOCAL_PROBE = {"at": 0.0, "ok": False}
_LOCAL_PROBE_TTL = 60.0


def local_endpoint_available(force: bool = False) -> bool:
    """Отвечает ли локальный OpenAI-compatible сервер (с кэшем на минуту)."""
    if not LOCAL_LLM_AUTODETECT:
        return False
    now = time.time()
    if not force and now - _LOCAL_PROBE["at"] < _LOCAL_PROBE_TTL:
        return _LOCAL_PROBE["ok"]
    base = (os.getenv("OPENAI_BASE_URL") or os.getenv("OPENAI_COMPATIBLE_URL")
            or "http://127.0.0.1:8080/v1").rstrip("/")
    if base.endswith("/chat/completions"):
        base = base[: -len("/chat/completions")].rstrip("/")
    url = base + "/models"
    ok = False
    try:
        import requests
        response = requests.get(url, timeout=1.5)
        ok = response.status_code < 500
    except Exception:
        ok = False
    _LOCAL_PROBE.update({"at": now, "ok": ok})
    return ok


def first_available_provider() -> str:
    """Первый провайдер, который реально может ответить прямо сейчас."""
    for provider in ("groq", "google_ai_studio", "openrouter", "cerebras", "pollinations"):
        if has_credentials(provider):
            return provider
    if local_endpoint_available():
        return "openai_compatible"
    return "local_demo"


def ensure_usable_provider() -> None:
    """
    Если выбранный провайдер не может ответить (нет ключа), переключаемся на
    тот, который может. Без этого чат молча возвращал «нет ответа от ИИ».
    """
    global current_provider, current_model
    if current_provider not in PROVIDERS:
        current_provider = "local_demo"
        current_model = "nova-local-1"
        return
    if current_provider == "local_demo" or has_credentials(current_provider):
        return
    fallback = first_available_provider()
    logger.warning("Провайдер '%s' не подключён — переключаюсь на '%s'", current_provider, fallback)
    current_provider = fallback
    models = PROVIDERS.get(fallback, {}).get("models") or []
    if fallback == "openai_compatible":
        # У локального сервера имя модели задаётся в .env — берём его.
        current_model = os.getenv("OPENAI_MODEL") or os.getenv("AI_MODEL") or (
            models[0]["id"] if models else "local-model")
    else:
        current_model = models[0]["id"] if models else "nova-local-1"


ensure_usable_provider()

# ---------- Кастомные команды ----------
CUSTOM_COMMANDS_FILE = os.path.join(os.path.dirname(__file__), "custom_commands.json")
CUSTOM_ALIASES: dict = {}

def load_custom_commands():
    """Загружает пользовательские алиасы команд из файла."""
    global CUSTOM_ALIASES
    if os.path.exists(CUSTOM_COMMANDS_FILE):
        try:
            with open(CUSTOM_COMMANDS_FILE, "r", encoding="utf-8") as f:
                CUSTOM_ALIASES = json.load(f)
            logger.info("Загружено %s кастомных команд", len(CUSTOM_ALIASES))
        except Exception as e:
            logger.warning("Ошибка чтения custom_commands.json: %s", e)
            CUSTOM_ALIASES = {}
    else:
        CUSTOM_ALIASES = {}

def save_custom_commands():
    """Сохраняет кастомные алиасы в файл."""
    with open(CUSTOM_COMMANDS_FILE, "w", encoding="utf-8") as f:
        json.dump(CUSTOM_ALIASES, f, ensure_ascii=False, indent=2)

load_custom_commands()

# ---------- Плагины ----------
PLUGINS_DIR = os.path.join(os.path.dirname(__file__), "plugins")
plugins: dict = {}

def load_plugins():
    """Загружает Python-плагины из папки commands/."""
    plugin_dir = os.path.join(os.path.dirname(__file__), "commands")
    if not os.path.isdir(plugin_dir):
        return
    sys.path.insert(0, plugin_dir)
    for fname in os.listdir(plugin_dir):
        if fname.endswith(".py") and not fname.startswith("_"):
            modname = fname[:-3]
            try:
                mod = __import__(modname)
                if hasattr(mod, "run"):
                    plugins[modname] = mod.run
                    logger.info("Плагин загружен: %s", modname)
            except Exception as e:
                logger.warning("Ошибка загрузки плагина %s: %s", modname, e)
