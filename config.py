"""
config.py — Конфигурация NovaMind: переменные окружения, провайдеры, команды, плагины.
"""
import os
import sys
import json
import time

# ---------- Загрузка .env ----------
_env_path = os.path.join(os.path.dirname(__file__), ".env")
if os.path.exists(_env_path):
    with open(_env_path, "r", encoding="utf-8") as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip())

# ---------- Google OAuth ----------
GOOGLE_CLIENT_ID     = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REDIRECT_URI  = os.getenv("GOOGLE_REDIRECT_URI", "http://localhost:5000/auth/google/callback")

# ---------- Безопасность ----------
ADMIN_CODE        = os.getenv("ADMIN_CODE", "007")
ADMIN_SESSION_KEY = os.getenv("SESSION_SECRET") or os.urandom(24).hex()
# Логин и код администратора. ADMIN_USER можно переопределить в .env,
# иначе вход идёт под именем admin с кодом ADMIN_CODE.
ADMIN_USER        = os.getenv("ADMIN_USER", "admin").strip() or "admin"
ADMIN_CREDENTIALS = {ADMIN_USER: ADMIN_CODE}

# ---------- Провайдеры AI ----------
PROVIDERS = {
    "groq": {
        "url": "https://api.groq.com/openai/v1/chat/completions",
        "max_tokens": 32768,
        "headers": {
            "Content-Type": "application/json"
        },
        "models": [
            {"id": "openai/gpt-oss-120b",          "name": "GPT-OSS 120B"},
            {"id": "llama-3.3-70b-versatile",       "name": "Llama 3.3 70B"},
            {"id": "llama-3.1-8b-instant",          "name": "Llama 3.1 8B"},
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
            "HTTP-Referer": "http://localhost:5000",
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
current_model    = "llama-3.3-70b-versatile"
system_prompt    = os.getenv("SYSTEM_PROMPT", "Ты — NovaMind, умный AI-ассистент. Отвечай чётко и по делу.")

# Выбранная медиа-модель (изображение / аудио / видео).
# Пустой словарь — генерация медиа выключена, чат работает как обычный текстовый.
# Заполняется только явным выбором пользователя в панели «Медиа».
media_selection: dict = {}

# ---------- Язык интерфейса (i18n: tg | ru | en) ----------
ui_language: str = "tg"

# ---------- Кеш моделей ----------
MODEL_CACHE: dict = {}
MODEL_CACHE_TTL = 3600  # 1 час

# ---------- runtime_settings.json ----------
RUNTIME_SETTINGS_FILE = os.path.join(os.path.dirname(__file__), "runtime_settings.json")

def load_runtime_settings():
    """Загружает сохранённый провайдер/модель между перезапусками."""
    global current_provider, current_model, system_prompt, media_selection
    if os.path.exists(RUNTIME_SETTINGS_FILE):
        try:
            with open(RUNTIME_SETTINGS_FILE, "r", encoding="utf-8") as f:
                s = json.load(f)
            current_provider = s.get("provider", current_provider)
            current_model    = s.get("model",    current_model)
            system_prompt    = s.get("system_prompt", system_prompt)
            saved_media = s.get("media_selection")
            media_selection = saved_media if isinstance(saved_media, dict) else {}
            saved_lang = s.get("ui_language")
            if saved_lang in ("tg", "ru", "en"):
                ui_language = saved_lang
        except Exception as e:
            print(f"[config] Ошибка чтения runtime_settings: {e}")

def _write_runtime_settings():
    with open(RUNTIME_SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump({
            "provider": current_provider,
            "model": current_model,
            "system_prompt": system_prompt,
            "media_selection": media_selection,
            "ui_language": ui_language,
        }, f, ensure_ascii=False, indent=2)

def save_selected_model(provider, model):
    """Сохраняет выбранный провайдер/модель в файл."""
    global current_provider, current_model
    current_provider = provider
    current_model    = model
    try:
        _write_runtime_settings()
    except Exception as e:
        print(f"[config] Ошибка сохранения: {e}")

def save_selected_media_model(selection):
    """Сохраняет выбранную медиа-модель (или очищает выбор, если selection пустое)."""
    global media_selection
    media_selection = dict(selection or {})
    try:
        _write_runtime_settings()
    except Exception as e:
        print(f"[config] Ошибка сохранения медиа-модели: {e}")
    return media_selection


def save_ui_language(language: str):
    """Сохраняет выбранный язык интерфейса (tg | ru | en)."""
    global ui_language
    if language in ("tg", "ru", "en"):
        ui_language = language
    try:
        _write_runtime_settings()
    except Exception as e:
        print(f"[config] Ошибка сохранения языка: {e}")
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


def first_available_provider() -> str:
    """Первый провайдер, который реально может ответить прямо сейчас."""
    for provider in ("groq", "google_ai_studio", "openrouter", "cerebras", "pollinations"):
        if has_credentials(provider):
            return provider
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
    print(f"[config] Провайдер '{current_provider}' не подключён — переключаюсь на '{fallback}'")
    current_provider = fallback
    models = PROVIDERS.get(fallback, {}).get("models") or []
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
            print(f"[config] Загружено {len(CUSTOM_ALIASES)} кастомных команд")
        except Exception as e:
            print(f"[config] Ошибка чтения custom_commands.json: {e}")
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
                    print(f"[config] Плагин загружен: {modname}")
            except Exception as e:
                print(f"[config] Ошибка загрузки плагина {modname}: {e}")
