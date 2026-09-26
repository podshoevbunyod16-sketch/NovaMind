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
    "openai_compatible": {
        "url": os.getenv("OPENAI_COMPATIBLE_URL", "http://localhost:11434/v1/chat/completions"),
        "max_tokens": 8192,
        "headers": {
            "Authorization": f"Bearer {os.getenv('OPENAI_COMPATIBLE_KEY', 'ollama')}",
            "Content-Type": "application/json",
        },
        "models": []
    },
}

# Текущий провайдер и модель (меняются через /api/settings/select)
current_provider = "groq"
current_model    = "llama-3.3-70b-versatile"
system_prompt    = os.getenv("SYSTEM_PROMPT", "Ты — NovaMind, умный AI-ассистент. Отвечай чётко и по делу.")

# ---------- Кеш моделей ----------
MODEL_CACHE: dict = {}
MODEL_CACHE_TTL = 3600  # 1 час

# ---------- runtime_settings.json ----------
RUNTIME_SETTINGS_FILE = os.path.join(os.path.dirname(__file__), "runtime_settings.json")

def load_runtime_settings():
    """Загружает сохранённый провайдер/модель между перезапусками."""
    global current_provider, current_model, system_prompt
    if os.path.exists(RUNTIME_SETTINGS_FILE):
        try:
            with open(RUNTIME_SETTINGS_FILE, "r", encoding="utf-8") as f:
                s = json.load(f)
            current_provider = s.get("provider", current_provider)
            current_model    = s.get("model",    current_model)
            system_prompt    = s.get("system_prompt", system_prompt)
        except Exception as e:
            print(f"[config] Ошибка чтения runtime_settings: {e}")

def save_selected_model(provider, model):
    """Сохраняет выбранный провайдер/модель в файл."""
    global current_provider, current_model
    current_provider = provider
    current_model    = model
    try:
        with open(RUNTIME_SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump({
                "provider": provider,
                "model": model,
                "system_prompt": system_prompt,
            }, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[config] Ошибка сохранения: {e}")

load_runtime_settings()

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
