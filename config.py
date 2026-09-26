import os
import json
import time
from flask import Flask

# ---------- Загрузка .env ----------
# ---------- Загрузка .env ----------
env_path = os.path.join(os.path.dirname(__file__), ".env")
if os.path.exists(env_path):
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                os.environ[k.strip()] = v.strip()

# ---------- Google OAuth ----------
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")

# ============================================================
# ========== GROQ API KEY ROTATION SYSTEM ====================
# ============================================================

# --- Загрузка всех Groq ключей ---
GROQ_KEYS = []
for i in range(1, 10):  # GROQ_API_KEY_1 ... GROQ_API_KEY_9
    k = os.getenv(f"GROQ_API_KEY_{i}", "")
    if k:
        GROQ_KEYS.append({"key": k, "index": i, "exhausted_at": None})

# Fallback: если нет нумерованных — берём основной
if not GROQ_KEYS:
    main_key = os.getenv("GROQ_API_KEY", "")
    if main_key:
        GROQ_KEYS.append({"key": main_key, "index": 0, "exhausted_at": None})

groq_key_index = 0  # Текущий активный ключ
_groq_lock = threading.Lock()  # FIX: защита от race condition

# Cooldown: 1 день + 1 час = 90000 секунд
GROQ_KEY_COOLDOWN = 90000


# ---------- Google OAuth ----------
GOOGLE_CLIENT_ID     = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REDIRECT_URI  = os.getenv("GOOGLE_REDIRECT_URI", "http://localhost:5000/auth/google/callback")

# ========== ПРОВАЙДЕРЫ =====================================
# ============================================================

PROVIDERS = {
    "groq": {
        "url": "https://api.groq.com/openai/v1/chat/completions",
        "max_tokens": 32768,  # Groq hardware limit
        "headers": {
            "Content-Type": "application/json"
        },
        "models": [
            {"id": "openai/gpt-oss-120b", "name": "GPT-OSS 120B"},
            {"id": "llama-3.3-70b-versatile", "name": "Llama 3.3 70B"},
            {"id": "llama-3.1-8b-instant", "name": "Llama 3.1 8B"},
        ]
    },
    "cerebras": {
        "url": "https://api.cerebras.ai/v1/chat/completions",
        "max_tokens": 16384,  # Cerebras limit
        "headers": {
            "Authorization": f"Bearer {os.getenv('CEREBRAS_API_KEY', '')}",
            "Content-Type": "application/json"
        },
        "models": [
            {"id": "qwen-3-235b-a22b-instruct-2507", "name": "Qwen 3 235B"},
            {"id": "zai-glm-4.7", "name": "Z.ai GLM 4.7"},
            {"id": "deepseek-r1-distill-llama-70b", "name": "DeepSeek R1 Distill Llama 70B"}
        ]
    },
    "openrouter": {
        "url": "https://openrouter.ai/api/v1/chat/completions",
        "max_tokens": 131072,  # OpenRouter поддерживает до 128K у многих моделей
        "headers": {
            "Authorization": f"Bearer {os.getenv('OPENROUTER_API_KEY', '')}",
            "Content-Type": "application/json",
            "HTTP-Referer": "http://localhost:5000",
            "X-Title": "NovaMind AI"
        },
        "models": [
            {"id": "google/gemini-2.0-flash-001", "name": "Gemini 2.0 Flash (Free)"},
            {"id": "deepseek/deepseek-chat-v3-0324", "name": "DeepSeek R1 (Free)"},
        ]
    },
    "google_ai_studio": {
        "url":"https://generativelanguage.googleapis.com/v1beta",
        "headers":{"Content-Type":"application/json"},
        "models":[]
    }
}

COMPATIBLE_BASE_URL = (
    os.getenv("OPENAI_BASE_URL")
    or os.getenv("OPENAI_API_BASE")
    or os.getenv("AI_BASE_URL")
    or os.getenv("LOCAL_LLM_URL")
    or ("https://api.openai.com/v1" if os.getenv("OPENAI_API_KEY") else "http://127.0.0.1:8080")
).rstrip("/")
COMPATIBLE_API_KEY = (
    os.getenv("OPENAI_API_KEY")
    or os.getenv("AI_API_KEY")
    or os.getenv("LOCAL_LLM_API_KEY")
    or ""
)
COMPATIBLE_MODEL = (
    os.getenv("OPENAI_MODEL")
    or os.getenv("AI_MODEL")
    or os.getenv("LOCAL_LLM_MODEL")
    or ("gpt-4o-mini" if os.getenv("OPENAI_API_KEY") else "llama3.2")
)
COMPATIBLE_CHAT_URL = (
    COMPATIBLE_BASE_URL
    if COMPATIBLE_BASE_URL.endswith("/chat/completions")
    else f"{COMPATIBLE_BASE_URL}/chat/completions"
)

compatible_headers = {"Content-Type": "application/json"}
if COMPATIBLE_API_KEY:
    compatible_headers["Authorization"] = f"Bearer {COMPATIBLE_API_KEY}"
PROVIDERS["openai_compatible"] = {
    "url": COMPATIBLE_CHAT_URL,
    "headers": compatible_headers,
    "models": [{"id": COMPATIBLE_MODEL, "name": f"Configured: {COMPATIBLE_MODEL}"}],
    "configured_url": COMPATIBLE_BASE_URL,
}

if GROQ_KEYS:
    current_provider = "groq"
    current_model = os.getenv("AI_MODEL") or "openai/gpt-oss-120b"
elif os.getenv("CEREBRAS_API_KEY"):
    current_provider = "cerebras"
    current_model = os.getenv("AI_MODEL") or "qwen-3-235b-a22b-instruct-2507"
elif os.getenv("OPENROUTER_API_KEY"):
    current_provider = "openrouter"
    current_model = os.getenv("AI_MODEL") or "google/gemini-2.0-flash-001"
elif os.getenv("GEMINI_API_KEY"):
    current_provider = "google_ai_studio"
    current_model = os.getenv("GEMINI_MODEL") or "gemini-3.8-flash"
else:
    current_provider = "openai_compatible"
    current_model = COMPATIBLE_MODEL

MODEL_CACHE = {}
MODEL_ERRORS = {}
PROVIDER_LABELS = {
    "groq": "Groq",
    "cerebras": "Cerebras",
    "openrouter": "OpenRouter",
    "google_ai_studio": "Google AI Studio",
    "openai_compatible": "OpenAI-compatible / Local",
}


# ---------- Админ ----------
ADMIN_CODE = os.getenv("ADMIN_CODE", "007")
ADMIN_SESSION_KEY = os.getenv("SESSION_SECRET") or os.urandom(24).hex()

# ---------- Кастомные алиасы ----------
CUSTOM_COMMANDS_FILE = os.path.join(os.path.dirname(__file__), "custom_commands.json")

def load_custom_commands():
        if os.path.exists(CUSTOM_COMMANDS_FILE):
            try:
                with open(CUSTOM_COMMANDS_FILE, "r", encoding="utf-8") as f:
                    custom_commands = json.load(f)
                print(f"Загружено {len(custom_commands)} пользовательских команд")
            except Exception as e:
                print(f"Ошибка чтения custom_commands.json: {e}")
                custom_commands = {}
        else:
            custom_commands = {}

    def save_custom_commands():
        with open(CUSTOM_COMMANDS_FILE, "w", encoding="utf-8") as f:
            json.dump(custom_commands, f, ensure_ascii=False, indent=2)

    load_custom_commands()


def save_custom_commands():
    with open(CUSTOM_COMMANDS_FILE, "w", encoding="utf-8") as f:
        json.dump(CUSTOM_ALIASES, f, ensure_ascii=False, indent=2)

# ---------- Плагины ----------
PLUGINS_DIR = os.path.join(os.path.dirname(__file__), "plugins")

def load_plugins():
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
                        print(f"Плагин загружен: {modname}")
                except Exception as e:
                    print(f"Ошибка загрузки {modname}: {e}")

    load_plugins()

    # ---------- Flask ----------
    app = Flask(__name__)
    app.secret_key = ADMIN_SESSION_KEY
