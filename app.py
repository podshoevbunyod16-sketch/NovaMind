import os
import sys
import json
import re
import uuid
import mimetypes
import requests
import subprocess
import time
import sqlite3
import threading
from datetime import datetime
from flask import Flask, request, jsonify, render_template, send_file, session, send_from_directory, redirect, Response, stream_with_context

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


def get_groq_key():
    """Возвращает текущий активный Groq API ключ"""
    global groq_key_index, GROQ_KEYS

    if not GROQ_KEYS:
        return ""

    with _groq_lock:  # FIX: thread-safe
        now = time.time()
        for key_info in GROQ_KEYS:
            if key_info["exhausted_at"] and (now - key_info["exhausted_at"]) >= GROQ_KEY_COOLDOWN:
                key_info["exhausted_at"] = None
                print(f"[Groq Key] Ключ #{key_info['index']} восстановлен (cooldown истёк)")

        for i in range(len(GROQ_KEYS)):
            idx = (groq_key_index + i) % len(GROQ_KEYS)
            if GROQ_KEYS[idx]["exhausted_at"] is None:
                groq_key_index = idx
                return GROQ_KEYS[idx]["key"]

        return GROQ_KEYS[groq_key_index]["key"]


def mark_groq_key_exhausted(permanent=False):
    """Помечает текущий ключ как исчерпанный. permanent=True для 401 (неверный ключ)."""
    global groq_key_index, GROQ_KEYS

    if not GROQ_KEYS:
        return

    with _groq_lock:  # FIX: thread-safe
        current_key = GROQ_KEYS[groq_key_index]
        if permanent:
            # FIX: 401 = неверный ключ навсегда, cooldown не поможет
            current_key["exhausted_at"] = float('inf')
            print(f"[Groq Key] Ключ #{current_key['index']} неверный (401), отключён навсегда")
        else:
            current_key["exhausted_at"] = time.time()
            print(f"[Groq Key] Ключ #{current_key['index']} исчерпан, cooldown на 25 часов")

        found = False
        for i in range(1, len(GROQ_KEYS)):
            idx = (groq_key_index + i) % len(GROQ_KEYS)
            if GROQ_KEYS[idx]["exhausted_at"] is None:
                groq_key_index = idx
                found = True
                print(f"[Groq Key] Переключение на ключ #{GROQ_KEYS[idx]['index']}")
                break

        if not found:
            print("[Groq Key] ВСЕ ключи исчерпаны! Ожидание восстановления...")


def get_groq_key_status():
    """Возвращает статус всех ключей"""
    now = time.time()
    status = []
    for key_info in GROQ_KEYS:
        if key_info["exhausted_at"] is None:
            status.append({
                "index": key_info["index"],
                "active": (GROQ_KEYS.index(key_info) == groq_key_index),
                "status": "active"
            })
        else:
            remaining = max(0, GROQ_KEY_COOLDOWN - (now - key_info["exhausted_at"]))
            status.append({
                "index": key_info["index"],
                "active": False,
                "status": "cooldown",
                "cooldown_remaining_sec": int(remaining),
                "cooldown_remaining_hr": round(remaining / 3600, 2)
            })
    return status


def openai_compatible_request(url, payload, headers, timeout=90, max_retries=2):
    """Запрос к любому OpenAI-compatible серверу: Ollama, LM Studio, vLLM или облачному endpoint."""
    for attempt in range(max_retries):
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=timeout)
            if resp.status_code == 429 and attempt < max_retries - 1:
                time.sleep(2 ** attempt)
                continue
            if resp.status_code == 401:
                return None, "OpenAI-compatible сервер отклонил API ключ (401)"
            resp.raise_for_status()
            return resp.json(), None
        except requests.exceptions.Timeout:
            if attempt < max_retries - 1:
                continue
            return None, "Таймаут OpenAI-compatible сервера"
        except requests.exceptions.RequestException as e:
            if attempt < max_retries - 1 and "429" in str(e):
                time.sleep(2 ** attempt)
                continue
            return None, f"OpenAI-compatible сервер недоступен: {e}"
        except ValueError:
            return None, "OpenAI-compatible сервер вернул некорректный JSON"
    return None, "OpenAI-compatible сервер недоступен"


def gemini_messages_from_openai(messages):
    contents_out=[]; system_text=""
    for message in messages or []:
        role=message.get("role","user"); content=message.get("content","")
        if isinstance(content,list):
            content="\n".join(str(p.get("text","")) for p in content if isinstance(p,dict) and p.get("text"))
        content=str(content or "")
        if not content: continue
        if role=="system": system_text=(system_text+"\n\n"+content).strip()
        else: contents_out.append({"role":"model" if role=="assistant" else "user","parts":[{"text":content}]})
    return system_text,contents_out

def gemini_request(model,payload,timeout=90,max_retries=2):
    key=os.getenv("GEMINI_API_KEY","").strip()
    if not key: return None,"Google AI Studio: GEMINI_API_KEY не найден в .env"
    model_id=str(model or "").strip().removeprefix("models/")
    system_text,contents_out=gemini_messages_from_openai(payload.get("messages",[]))
    if not contents_out: return None,"Google AI Studio: отсутствует сообщение"
    body={"contents":contents_out,"generationConfig":{"temperature":float(payload.get("temperature",0.7)),"maxOutputTokens":min(int(payload.get("max_tokens",65536)),65536)}}
    if system_text: body["systemInstruction"]={"parts":[{"text":system_text}]}
    url=f"https://generativelanguage.googleapis.com/v1beta/models/{model_id}:generateContent"
    headers={"Content-Type":"application/json","x-goog-api-key":key}
    for attempt in range(max_retries):
        try:
            r=requests.post(url,json=body,headers=headers,timeout=timeout)
            if r.status_code==429 and attempt<max_retries-1: time.sleep(2**attempt); continue
            if r.status_code==401: return None,"Google AI Studio: API ключ отклонён (401)"
            if r.status_code==403: return None,"Google AI Studio: доступ запрещён (403)"
            r.raise_for_status(); data=r.json(); parts=[]
            for candidate in data.get("candidates",[]):
                for part in (candidate.get("content") or {}).get("parts",[]):
                    if part.get("text"): parts.append(part["text"])
            if not parts: return None,"Google AI Studio не вернул текст"
            return {"choices":[{"message":{"role":"assistant","content":"".join(parts)}}]},None
        except requests.exceptions.Timeout:
            if attempt<max_retries-1: continue
            return None,"Google AI Studio: таймаут"
        except requests.exceptions.RequestException as exc:
            if attempt<max_retries-1 and "429" in str(exc): time.sleep(2**attempt); continue
            return None,f"Google AI Studio: {exc}"
        except (ValueError,TypeError): return None,"Google AI Studio вернул некорректный JSON"
    return None,"Google AI Studio: запрос не выполнен"

def gemini_stream_request(model,payload,timeout=90):
    """Открывает нативный Gemini SSE-поток и возвращает подробную ошибку API."""
    key=os.getenv("GEMINI_API_KEY","").strip()
    if not key:
        return None,"Google AI Studio: GEMINI_API_KEY не найден в .env"

    model_id=str(model or "").strip().removeprefix("models/")
    system_text,contents_out=gemini_messages_from_openai(payload.get("messages",[]))
    if not contents_out:
        return None,"Google AI Studio: отсутствует сообщение"

    body={
        "contents": contents_out,
        "generationConfig":{
            "temperature":float(payload.get("temperature",0.7)),
            "maxOutputTokens":min(int(payload.get("max_tokens",65536)),65536)
        }
    }
    if system_text:
        body["systemInstruction"]={"parts":[{"text":system_text}]}

    url=f"https://generativelanguage.googleapis.com/v1beta/models/{model_id}:streamGenerateContent"
    headers={
        "Content-Type":"application/json",
        "Accept":"text/event-stream",
        "x-goog-api-key":key
    }

    try:
        r=requests.post(
            url,
            params={"alt":"sse"},
            json=body,
            headers=headers,
            timeout=(15, timeout),
            stream=True
        )
        if r.status_code >= 400:
            try:
                detail=r.json()
                detail_text=json.dumps(detail,ensure_ascii=False)
            except ValueError:
                detail_text=(r.text or "").strip()
            return None,f"Google AI Studio HTTP {r.status_code}: {detail_text[:1200]}"
        r.encoding="utf-8"
        return r,None
    except requests.exceptions.Timeout:
        return None,"Google AI Studio: таймаут при подключении к streamGenerateContent"
    except requests.exceptions.RequestException as exc:
        return None,f"Google AI Studio: {exc}"

def groq_request_with_rotation(url, payload, headers, timeout=90, max_retries=3):
    """
    Выполняет запрос к Groq с автоматической ротацией ключей при rate limit.
    Возвращает (response_data, None) или (None, error_message).
    """
    if "generativelanguage.googleapis.com" in url:
        return gemini_request(payload.get("model"),payload,timeout=timeout,max_retries=max_retries)
    # Cerebras, OpenRouter и локальные серверы используют обычный OpenAI-compatible протокол.
    if "api.groq.com" not in url:
        return openai_compatible_request(url, payload, headers, timeout=timeout, max_retries=max_retries)

    for attempt in range(max_retries):
        current_key = get_groq_key()
        if not current_key:
            return None, "Нет доступных Groq API ключей"

        headers["Authorization"] = f"Bearer {current_key}"

        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=timeout)

            # Rate limit / quota exceeded
            if resp.status_code == 429:
                error_text = resp.text.lower()
                if "rate limit" in error_text or "quota" in error_text or "exceeded" in error_text:
                    print(f"[Groq Key] Rate limit на ключе, ротируем...")
                    mark_groq_key_exhausted()
                    continue
                else:
                    time.sleep(2 ** attempt)
                    continue

            if resp.status_code == 401:
                print(f"[Groq Key] 401 Unauthorized, ключ неверный навсегда")
                mark_groq_key_exhausted(permanent=True)  # FIX: 401 != временный лимит
                continue

            resp.raise_for_status()
            return resp.json(), None

        except requests.exceptions.Timeout:
            if attempt < max_retries - 1:
                time.sleep(1)
                continue
            return None, "Таймаут запроса к Groq"
        except requests.exceptions.RequestException as e:
            error_str = str(e).lower()
            if "rate limit" in error_str or "quota" in error_str or "429" in error_str:
                print(f"[Groq Key] Rate limit detected in exception, ротируем...")
                mark_groq_key_exhausted()
                continue
            if attempt < max_retries - 1:
                time.sleep(1)
                continue
            return None, str(e)

    return None, "Все Groq ключи исчерпаны или недоступны"


# ============================================================
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


def model_size_billions(model_id, model_name=""):
    match = re.search(r"(?:^|[-_/ ])(\d+(?:\.\d+)?)(?:b|B)(?:$|[-_/ ])", f"{model_id} {model_name}")
    return float(match.group(1)) if match else 0


def normalize_model(model, provider):
    model = model or {}
    model_id = model.get("id", "")
    name = model.get("name") or model_id
    pricing = model.get("pricing") or {}
    prompt_price = str(pricing.get("prompt", model.get("prompt_price", "")))
    completion_price = str(pricing.get("completion", model.get("completion_price", "")))
    is_free = ":free" in model_id or (prompt_price in {"0", "0.0", "0.000000"} and completion_price in {"0", "0.0", "0.000000"})
    context = model.get("context_length") or model.get("context_window") or model.get("max_context_length") or 0
    architecture = model.get("architecture") or {}
    modality = architecture.get("modality", "text->text") if isinstance(architecture, dict) else "text->text"
    return {
        "id": model_id,
        "name": name,
        "provider": provider,
        "provider_name": PROVIDER_LABELS.get(provider, provider),
        "context_length": int(context or 0),
        "parameters_b": model.get("parameter_count") or model_size_billions(model_id, name),
        "prompt_price": prompt_price,
        "completion_price": completion_price,
        "free": is_free,
        "modality": modality,
        "created": model.get("created", 0),
        "description": model.get("description", ""),
    }


def provider_api_headers(provider):
    if provider == "groq":
        key = get_groq_key()
        return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"} if key else None
    if provider == "openrouter":
        key = os.getenv("OPENROUTER_API_KEY", "")
        return {"Authorization": f"Bearer {key}", "Content-Type": "application/json", "HTTP-Referer": "http://localhost:5000", "X-Title": "NovaMind AI"} if key else None
    if provider == "cerebras":
        key = os.getenv("CEREBRAS_API_KEY", "")
        return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"} if key else None
    if provider == "google_ai_studio":
        key=os.getenv("GEMINI_API_KEY","")
        return {"x-goog-api-key":key,"Content-Type":"application/json"} if key else None
    return compatible_headers


def fetch_available_models(provider, force=False):
    if not force and provider in MODEL_CACHE:
        return MODEL_CACHE[provider]
    if provider == "openrouter":
        url = "https://openrouter.ai/api/v1/models"
    elif provider == "google_ai_studio":
        if not os.getenv("GEMINI_API_KEY","").strip():
            MODEL_ERRORS[provider]="GEMINI_API_KEY не найден в .env"; return []
        url="https://generativelanguage.googleapis.com/v1beta/models"
    elif provider == "groq":
        url = "https://api.groq.com/openai/v1/models"
    elif provider == "cerebras":
        url = "https://api.cerebras.ai/v1/models"
    else:
        return [normalize_model(model, provider) for model in PROVIDERS[provider]["models"]]
    headers = provider_api_headers(provider)
    if not headers:
        return []
    try:
        response = requests.get(url, headers=headers, timeout=20)
        response.raise_for_status()
        if provider == "google_ai_studio":
            raw_models=response.json().get("models",[]); models=[]
            for model in raw_models:
                model_id=str(model.get("name","")).removeprefix("models/")
                if model_id and "generateContent" in (model.get("supportedGenerationMethods") or []):
                    models.append(normalize_model({"id":model_id,"name":model.get("displayName") or model_id,"description":model.get("description",""),"context_length":model.get("inputTokenLimit",0),"pricing":{"prompt":"0","completion":"0"}},provider))
        else:
            raw_models=response.json().get("data",[])
            models=[normalize_model(model,provider) for model in raw_models if model.get("id")]
        MODEL_CACHE[provider] = models
        MODEL_ERRORS.pop(provider, None)
        return models
    except (requests.RequestException, ValueError, TypeError) as exc:
        print(f"Model catalog error ({provider}): {exc}")
        MODEL_ERRORS[provider] = str(exc)
        return []


def provider_status():
    return {
        "groq": bool(GROQ_KEYS),
        "cerebras": bool(os.getenv("CEREBRAS_API_KEY")),
        "openrouter": bool(os.getenv("OPENROUTER_API_KEY")),
        "google_ai_studio": bool(os.getenv("GEMINI_API_KEY")),
        "openai_compatible": True,
    }

def save_selected_model(provider, model):
    settings_path = os.path.join(os.path.dirname(__file__), "runtime_settings.json")
    try:
        with open(settings_path, "w", encoding="utf-8") as settings_file:
            json.dump({"provider": provider, "model": model}, settings_file, ensure_ascii=False, indent=2)
    except OSError as exc:
        print(f"Could not save model settings: {exc}")

try:
    settings_path = os.path.join(os.path.dirname(__file__), "runtime_settings.json")
    with open(settings_path, "r", encoding="utf-8") as settings_file:
        saved_settings = json.load(settings_file)
    saved_provider = saved_settings.get("provider")
    saved_model = saved_settings.get("model")
    if saved_provider in PROVIDERS and saved_model and provider_status().get(saved_provider, False):
        current_provider = saved_provider
        current_model = saved_model
except (OSError, ValueError, TypeError):
    pass

system_prompt = """ Ты — продвинутый AI ассистент NovaMind, ориентированный на практическую пользу.

Твоя цель:
- Давать максимально полезные, конкретные и применимые ответы
- Минимизировать воду и общие фразы
- Работать как эксперт, а не как болтливый помощник

Правила:

1. Структура ответа:
- Краткий вывод
- Основная часть (по шагам / списком)
- Пример или применение

2. Если пользователь не уточнил задачу:
- Сам предложи 2–3 варианта интерпретации
- Выбери наиболее вероятный и продолжи

3. Всегда оптимизируй под результат:
- Код → рабочий, современный, без мусора
- Идеи → с реализацией
- Ответ → без лишней теории

4. Используй режимы:

[MODE: CODER]
- Пиши чистый, production-ready код
- Объясняй только сложные моменты

[MODE: ANALYST]
- Разбирай проблемы глубоко
- Находи слабые места

[MODE: CREATOR]
- Генерируй идеи с конкретикой

5. Если задача слабая:
- Улучши её сам
- Предложи более эффективный подход

6. Никогда:
- не пиши общие фразы
- не дублируй очевидное
- не растягивай ответ

7. Всегда:
- думай как инженер
- отвечай как эксперт. """
contents = []  # In-memory fallback (используется только если DB недоступна)

# ============================================================
# ========== СИСТЕМА ПАМЯТИ ЧАТОВ (SQLite) ===================
# ============================================================

DB_PATH = os.path.join(os.path.dirname(__file__), "novamind_chats.db")
_db_lock = threading.Lock()

def get_db():
    """Возвращает подключение к SQLite (thread-local)."""
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """Создаёт таблицы если не существуют."""
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS chats (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at REAL NOT NULL,
                FOREIGN KEY (chat_id) REFERENCES chats(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages(chat_id);
            CREATE INDEX IF NOT EXISTS idx_chats_updated ON chats(updated_at DESC);
        """)
    print("[DB] База данных чатов инициализирована:", DB_PATH)

init_db()

def create_chat(title="Новый чат"):
    """Создаёт новый чат, возвращает его id."""
    chat_id = str(uuid.uuid4())
    now = time.time()
    with _db_lock:
        with get_db() as conn:
            conn.execute(
                "INSERT INTO chats (id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (chat_id, title, now, now)
            )
    return chat_id

def get_chat_history(chat_id, limit=50):
    """Возвращает историю сообщений чата."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT role, content FROM messages WHERE chat_id=? ORDER BY created_at ASC LIMIT ?",
            (chat_id, limit)
        ).fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in rows]

def add_message(chat_id, role, content):
    """Добавляет сообщение в историю чата."""
    now = time.time()
    with _db_lock:
        with get_db() as conn:
            conn.execute(
                "INSERT INTO messages (chat_id, role, content, created_at) VALUES (?, ?, ?, ?)",
                (chat_id, role, content, now)
            )
            conn.execute(
                "UPDATE chats SET updated_at=? WHERE id=?",
                (now, chat_id)
            )
            # Авто-заголовок: первое сообщение пользователя
            row = conn.execute(
                "SELECT title FROM chats WHERE id=?", (chat_id,)
            ).fetchone()
            if row and row["title"] == "Новый чат" and role == "user":
                title = content[:60].replace("\n", " ").strip()
                conn.execute("UPDATE chats SET title=? WHERE id=?", (title, chat_id))

def list_chats(limit=50):
    """Возвращает список чатов (новые сначала)."""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT c.id, c.title, c.created_at, c.updated_at,
               COUNT(m.id) as msg_count
               FROM chats c LEFT JOIN messages m ON c.id=m.chat_id
               GROUP BY c.id ORDER BY c.updated_at DESC LIMIT ?""",
            (limit,)
        ).fetchall()
    return [dict(r) for r in rows]

def delete_chat(chat_id):
    """Удаляет чат и все его сообщения."""
    with _db_lock:
        with get_db() as conn:
            conn.execute("DELETE FROM messages WHERE chat_id=?", (chat_id,))
            conn.execute("DELETE FROM chats WHERE id=?", (chat_id,))

def get_or_create_session_chat():
    """Получает или создаёт chat_id для текущей Flask-сессии."""
    if "chat_id" not in session:
        session["chat_id"] = create_chat()
    return session["chat_id"]

def trim_messages(chat_id, max_messages=100):
    """Оставляет только последние max_messages в чате."""
    with _db_lock:
        with get_db() as conn:
            total = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE chat_id=?", (chat_id,)
            ).fetchone()[0]
            if total > max_messages:
                conn.execute("""
                    DELETE FROM messages WHERE chat_id=? AND id NOT IN (
                        SELECT id FROM messages WHERE chat_id=?
                        ORDER BY created_at DESC LIMIT ?
                    )
                """, (chat_id, chat_id, max_messages))

# ---------- Админ ----------
ADMIN_CREDENTIALS = {
    "admin": os.getenv("ADMIN_CODE", "007"),
}
ADMIN_SESSION_KEY = os.getenv("SESSION_SECRET") or os.urandom(24).hex()  # FIX: безопасный секрет

# ---------- Кастомные алиасы ----------
CUSTOM_COMMANDS_FILE = os.path.join(os.path.dirname(__file__), "custom_commands.json")
custom_commands = {}

def load_custom_commands():
    global custom_commands
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

# ---------- Плагины ----------
plugins = {}
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

# ---------- Поиск через APILayer Google Search ----------
APILAYER_KEY = os.getenv("APILAYER_KEY", "")

def search_web(query):
    try:
        url = "https://api.apilayer.com/google_search"
        headers = {"apikey": APILAYER_KEY}
        params = {"q": query, "hl": "ru", "gl": "ru", "num": 5}
        resp = requests.get(url, headers=headers, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        parts = []
        for item in data.get("organic_results", [])[:5]:
            title = item.get("title", "")
            snippet = item.get("snippet", "")
            link = item.get("link", "")
            if snippet:
                parts.append(f"📌 **{title}**\n{snippet}\n🔗 {link}")

        answer = data.get("answer_box", {})
        if answer:
            answer_text = answer.get("answer") or answer.get("snippet") or ""
            if answer_text:
                parts.insert(0, f"✅ **Быстрый ответ:** {answer_text}")

        if parts:
            return "Результаты поиска Google:\n\n" + "\n\n".join(parts)
        return None
    except Exception as e:
        print(f"search_web error: {e}")
        return None


# ============================================================
# ========== АВТО ПОИСК (Auto Search) ========================
# ============================================================

@app.route('/api/auto_search', methods=['POST'])
def auto_search():
    """
    Авто поиск: определяет, нужен ли поиск в интернете,
    ищет через APILayer Google Search, обрабатывает через Groq.
    """
    global contents

    data = request.get_json() or {}
    user_message = data.get('message', '').strip()

    if not user_message:
        return jsonify({'error': 'Пустое сообщение'})

    provider = PROVIDERS[current_provider]

    # Шаг 1: активная модель решает, нужен ли поиск
    decision_prompt = f"""Ты — интеллектуальный фильтр для AI-ассистента.

Пользователь написал: "{user_message}"

Твоя задача: определить, нужны ли актуальные данные из интернета для ответа на этот запрос.

Запросы, требующие поиска (новости, актуальные данные, события, цены, погода, спорт, технологии, политика, наука, кино, и т.д.):
- "Какая сегодня погода?"
- "Последние новости про ..."
- "Курс доллара"
- "Кто выиграл матч вчера?"
- "Новейшие технологии ..."
- "Какие фильмы вышли в 2026?"

Запросы, НЕ требующие поиска (теория, логика, код, общие знания):
- "Объясни рекурсию"
- "Напиши код на Python"
- "Как работает нейросеть"
- "Переведи текст"
- "Реши уравнение"

Ответь ТОЛЬКО одним словом: SEARCH или DIRECT."""

    decision_payload = {
        "model": current_model,
        "messages": [{"role": "user", "content": decision_prompt}],
        "temperature": 0.1,
        "max_tokens": 300,
    }
    data_decision, error = groq_request_with_rotation(
        provider["url"], decision_payload, provider["headers"].copy(), timeout=15
    )
    if error:
        print(f"Auto search decision error: {error}")
        decision = "DIRECT"
    else:
        decision = data_decision["choices"][0]["message"]["content"].strip().upper()

    if "SEARCH" not in decision:
        return jsonify({'needs_search': False, 'reply': None})

    # Шаг 2: Генерируем поисковый запрос
    search_query_prompt = f"""Пользователь написал: "{user_message}"

Сформулируй краткий поисковый запрос для Google (1-5 слов), который поможет найти актуальную информацию.

Ответь ТОЛЬКО поисковым запросом, без кавычек и пояснений."""

    query_payload = {
        "model": current_model,
        "messages": [{"role": "user", "content": search_query_prompt}],
        "temperature": 0.1,
        "max_tokens": 128000,
    }
    data_query, error = groq_request_with_rotation(
        provider["url"], query_payload, provider["headers"].copy(), timeout=15
    )
    if error:
        print(f"Auto search query generation error: {error}")
        search_query = user_message
    else:
        search_query = data_query["choices"][0]["message"]["content"].strip()
        search_query = search_query.strip('"').strip("'")

    # Шаг 3: Поиск через APILayer
    search_result = search_web(search_query)
    if not search_result:
        search_result = "Поиск не дал результатов."

    # Шаг 4: Финальный ответ через Groq
    final_prompt = f"""Пользователь спросил: "{user_message}"

Вот актуальная информация из интернета (Google Search):
{search_result}

Твоя задача:
- Дай подробный, полезный ответ на вопрос пользователя
- Используй информацию из поиска
- Добавь своё объяснение и понимание
- Отвечай на русском языке
- Используй Markdown: заголовки, списки, таблицы если нужно
- В конце напиши: "🔍 Ответ на основе поиска Google" """

    final_payload = {
        "model": current_model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": final_prompt}
        ],
        "temperature": 0.5,
        "max_tokens": 128000,
    }
    data_final, error = groq_request_with_rotation(
        provider["url"], final_payload, provider["headers"].copy(), timeout=90
    )
    if error:
        return jsonify({
            'needs_search': True,
            'search_query': search_query,
            'reply': f'🔍 Результаты по Google "{search_query}":\n\n{search_result}\n\n_(Groq недоступен: {error})_'
        })

    reply = data_final["choices"][0]["message"]["content"]
    contents.append({"role": "user", "content": f"[Авто поиск] {user_message}"})
    contents.append({"role": "assistant", "content": reply})
    if len(contents) > 20:
        contents = contents[-20:]

    return jsonify({
        'needs_search': True,
        'search_query': search_query,
        'reply': reply
    })


# ---------- Endpoint: Поиск + Groq ----------
@app.route('/api/web_search_groq', methods=['POST'])
def web_search_groq():
    """Поиск через APILayer Google + обработка через Groq"""
    global contents

    data = request.get_json() or {}
    query = data.get('query', '').strip()

    if not query:
        return jsonify({'error': 'Пустой запрос'})

    search_result = search_web(query)
    if not search_result:
        search_result = "Поиск не дал результатов. Отвечай на основе своих знаний."

    provider = PROVIDERS[current_provider]
    groq_prompt = f"""Пользователь спросил: "{query}"

Вот результаты из Google:
{search_result}

Твоя задача:
- Дай подробный, полезный ответ на вопрос пользователя
- Используй информацию из поиска
- Добавь своё объяснение и понимание
- Отвечай на русском языке
- Используй Markdown: заголовки, списки, таблицы если нужно
- В конце напиши: "🔍 *Ответ на основе поиска Google*" """

    payload = {
        "model": current_model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": groq_prompt}
        ],
        "temperature": 0.5,
        "max_tokens": 12000,
    }
    data_resp, error = groq_request_with_rotation(
        provider["url"], payload, provider["headers"].copy(), timeout=90
    )
    if error:
        return jsonify({
            'reply': f'🔍 **Результаты Google по запросу "{query}":**\n\n{search_result}\n\n*Groq недоступен: {error}*'
        })

    reply = data_resp["choices"][0]["message"]["content"]
    contents.append({"role": "user", "content": f"[Поиск Google] {query}"})
    contents.append({"role": "assistant", "content": reply})
    if len(contents) > 20:
        contents = contents[-20:]

    return jsonify({'reply': reply})


# ========== СТРАНИЦЫ ==========

@app.route('/')
def index():
    return render_template('auth.html', google_client_id=GOOGLE_CLIENT_ID)

@app.route('/chat')
def chat_page():
    return render_template('index.html')

@app.route('/settings')
def settings_page():
    return render_template('settings.html')

@app.route('/api/settings/providers')
def settings_providers():
    statuses = provider_status()
    return jsonify({
        "providers": [
            {
                "id": provider,
                "name": PROVIDER_LABELS.get(provider, provider),
                "configured": statuses.get(provider, False),
                "url": data.get("configured_url") or data.get("url", "").split("/chat/completions")[0],
            }
            for provider, data in PROVIDERS.items()
        ],
        "current_provider": current_provider,
        "current_model": current_model,
    })

@app.route('/api/settings/models')
def settings_models():
    provider = request.args.get("provider", current_provider)
    force = request.args.get("refresh", "0") == "1"
    if provider not in PROVIDERS:
        return jsonify({"error": "Неизвестный провайдер"}), 400
    if not provider_status().get(provider, False):
        return jsonify({"provider": provider, "models": [], "configured": False, "error": "API ключ провайдера не найден в .env"})
    models = fetch_available_models(provider, force=force)
    error = None if models else (MODEL_ERRORS.get(provider) or "Не удалось получить каталог моделей. Проверьте API ключ и доступ к интернету.")
    return jsonify({"provider": provider, "models": models, "configured": True, "error": error, "current_model": current_model if provider == current_provider else None})

@app.route('/api/settings/select', methods=['POST'])
def settings_select():
    global current_provider, current_model
    data = request.get_json() or {}
    provider = data.get("provider", "")
    model = data.get("model", "").strip()
    if provider not in PROVIDERS or not model:
        return jsonify({"error": "Укажите провайдера и модель"}), 400
    models = fetch_available_models(provider)
    if not any(item["id"] == model for item in models):
        return jsonify({"error": "Модель не найдена в доступном каталоге провайдера. Обновите список моделей."}), 400
    current_provider = provider
    current_model = model
    save_selected_model(current_provider, current_model)
    return jsonify({"success": True, "provider": current_provider, "model": current_model, "message": f"Выбрано: {model}"})

@app.route('/admin/login')
def admin_login_page():
    return render_template('admin.html')

# ========== GOOGLE OAUTH ==========

@app.route('/auth/google/callback')
def auth_google_callback():
    code = request.args.get('code')
    error = request.args.get('error')

    if error:
        return f'Ошибка авторизации: {error}', 400
    if not code:
        return 'Не получен код авторизации', 400

    token_url = "https://oauth2.googleapis.com/token"
    token_data = {
        "code": code,
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "redirect_uri": "http://localhost:5000/auth/google/callback",
        "grant_type": "authorization_code"
    }

    try:
        token_resp = requests.post(token_url, data=token_data, timeout=10)
        token_resp.raise_for_status()
        token_json = token_resp.json()

        id_token_jwt = token_json.get("id_token")
        if not id_token_jwt:
            return 'Не получен ID токен', 400

        import base64 as b64
        payload = id_token_jwt.split('.')[1]
        payload += '=' * (4 - len(payload) % 4)
        user_info = json.loads(b64.urlsafe_b64decode(payload).decode('utf-8'))

        nick = user_info.get('name', user_info.get('email', 'User').split('@')[0])
        email = user_info.get('email', '')
        picture = user_info.get('picture', '')

        session['nova_user_nick'] = nick
        session['nova_user_email'] = email
        session['nova_user_avatar'] = picture
        session['nova_google_login'] = True
        session['nova_is_admin'] = False

        return redirect(f'/chat?nick={nick}&email={email}')
    except Exception as e:
        return f'Ошибка авторизации: {str(e)}', 500

# ========== API АВТОРИЗАЦИИ ==========

@app.route('/api/admin/login', methods=['POST'])
def admin_login():
    data = request.get_json() or {}
    username = data.get('username', 'admin').strip()
    code = data.get('code', '').strip()

    if username not in ADMIN_CREDENTIALS:
        return jsonify({'success': False, 'error': 'Неверный логин'})
    if ADMIN_CREDENTIALS[username] != code:
        return jsonify({'success': False, 'error': 'Неверный код'})

    session['admin_logged_in'] = True
    return jsonify({'success': True, 'username': username})

@app.route('/api/admin/logout', methods=['POST'])
def admin_logout():
    session.clear()
    return jsonify({'success': True})

@app.route('/api/admin/check')
def admin_check():
    if session.get('admin_logged_in'):
        return jsonify({'logged_in': True, 'username': session.get('admin_username', 'admin')})
    return jsonify({'logged_in': False})

# ========== ЧАТ ==========
@app.route('/send', methods=['POST'])
def send():
    """Отправка сообщения к ИИ с сохранением в БД"""
    global contents
    data = request.get_json()
    message = data.get('message', '').strip()
    reasoning = data.get('reasoning', False)
    chat_id = data.get('chat_id') or get_or_create_session_chat()

    if not message:
        return jsonify({'error': 'Пустое сообщение'})

    # FIX: загружаем историю из БД
    history = get_chat_history(chat_id, limit=50)
    add_message(chat_id, "user", message)

    if reasoning and os.getenv("CEREBRAS_API_KEY"):
        provider = PROVIDERS["cerebras"]
        model = "zai-glm-4.7"
    else:
        provider = PROVIDERS[current_provider]
        model = current_model

    # Берём max_tokens из настроек провайдера (у каждого свой аппаратный лимит)
    provider_max = provider.get("max_tokens", 8192)
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system_prompt}] + history + [{"role": "user", "content": message}],
        "temperature": 0.7,
        "max_tokens": provider_max,
    }

    data_resp, error = groq_request_with_rotation(
        provider["url"], payload, provider["headers"].copy(), timeout=90
    )
    if error:
        return jsonify({'error': error})

    reply = data_resp["choices"][0]["message"]["content"]
    add_message(chat_id, "assistant", reply)
    trim_messages(chat_id, max_messages=100)

    # Совместимость: обновляем contents для других endpoint'ов
    contents = get_chat_history(chat_id, limit=20)

    return jsonify({'reply': reply, 'chat_id': chat_id})


@app.route('/send_stream', methods=['POST'])
def send_stream():
    """Потоковый чат с надёжной обработкой Groq rate-limit/auth ошибок."""
    global contents
    data = request.get_json() or {}
    message = data.get('message', '').strip()
    reasoning = data.get('reasoning', False)

    if not message:
        return jsonify({'error': 'Пустое сообщение'}), 400

    contents.append({"role": "user", "content": message})

    if reasoning and os.getenv("CEREBRAS_API_KEY"):
        provider = PROVIDERS["cerebras"]
        model = "zai-glm-4.7"
    else:
        provider = PROVIDERS[current_provider]
        model = current_model

    # FIX: загружаем историю из БД для send_stream тоже
    _chat_id_stream = get_or_create_session_chat()
    _history_stream = get_chat_history(_chat_id_stream, limit=50)
    add_message(_chat_id_stream, "user", message)

    # Берём max_tokens из настроек провайдера (у каждого свой аппаратный лимит)
    _provider_max = provider.get("max_tokens", 8192)
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system_prompt}] + _history_stream + [{"role": "user", "content": message}],
        "temperature": 0.7,
        "max_tokens": _provider_max,
        "stream": True,
    }

    # Для Groq делаем несколько попыток с ротацией ключей.
    upstream = None
    last_error = None
    is_gemini = provider.get("url","").startswith("https://generativelanguage.googleapis.com")
    if is_gemini:
        upstream, gemini_error = gemini_stream_request(model, payload, timeout=90)

        # Некоторые сети/прокси могут блокировать SSE, хотя обычный generateContent
        # работает. В таком случае не отдаём 502: выполняем обычный Gemini-запрос
        # и возвращаем его как один потоковый token.
        if upstream is None:
            print(f"[Gemini stream] {gemini_error}")
            fallback_data, fallback_error = gemini_request(model, payload, timeout=90, max_retries=2)
            if fallback_error:
                if contents and contents[-1]["role"] == "user":
                    contents.pop()
                return jsonify({
                    "error": f"{gemini_error}. Fallback generateContent: {fallback_error}"
                }), 502

            fallback_reply=((fallback_data.get("choices") or [{}])[0].get("message") or {}).get("content","")
            if contents and contents[-1]["role"] == "user":
                contents.pop()

            def generate_fallback():
                if fallback_reply:
                    yield json.dumps({"token": fallback_reply}, ensure_ascii=False) + "\n"
                    contents.append({"role":"assistant","content":fallback_reply})
                    if len(contents) > 20:
                        del contents[:-20]
                yield json.dumps({"done":True}, ensure_ascii=False) + "\n"

            return Response(
                generate_fallback(),
                mimetype="application/x-ndjson",
                headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no-cache"}
            )
    else:
        upstream = None
    max_attempts = max(1, min(len(GROQ_KEYS), 9)) if "api.groq.com" in provider["url"] else 1

    for _ in range(0 if upstream is not None else max_attempts):
        headers = provider["headers"].copy()
        if "api.groq.com" in provider["url"]:
            key = get_groq_key()
            if not key:
                last_error = "Нет доступных Groq API ключей"
                break
            headers["Authorization"] = f"Bearer {key}"

        try:
            candidate = requests.post(
                provider["url"], json=payload, headers=headers, timeout=90, stream=True
            )
            if candidate.status_code in (401, 429) and "api.groq.com" in provider["url"]:
                last_error = f"Groq HTTP {candidate.status_code}"
                mark_groq_key_exhausted()
                candidate.close()
                continue
            candidate.raise_for_status()
            candidate.encoding = "utf-8"
            upstream = candidate
            break
        except requests.exceptions.RequestException as exc:
            last_error = str(exc)
            if "api.groq.com" in provider["url"]:
                mark_groq_key_exhausted()
                continue
            break

    if upstream is None:
        if contents and contents[-1]["role"] == "user":
            contents.pop()
        return jsonify({'error': f'Ошибка подключения к AI: {last_error or "неизвестная ошибка"}'}), 502

    @stream_with_context
    def generate():
        full_reply = ""
        try:
            for raw_line in upstream.iter_lines(decode_unicode=True):
                if not raw_line:
                    continue
                line = raw_line.strip()
                if line.startswith("data:"):
                    line = line[5:].strip()
                if line == "[DONE]":
                    break
                try:
                    chunk = json.loads(line)
                except (TypeError, json.JSONDecodeError):
                    continue

                if is_gemini:
                    for candidate in chunk.get("candidates", []):
                        for part in (candidate.get("content") or {}).get("parts", []):
                            token = part.get("text") or ""
                            if token:
                                full_reply += token
                                yield json.dumps({"token": token}, ensure_ascii=False) + "\n"
                    continue

                choice = (chunk.get("choices") or [{}])[0]
                delta = choice.get("delta") or {}
                token = delta.get("content") or choice.get("text") or ""

                # GPT-OSS может отдавать reasoning отдельно; пользователю нужен content.
                if token:
                    full_reply += token
                    yield json.dumps({"token": token}, ensure_ascii=False) + "\n"

            if full_reply:
                contents.append({"role": "assistant", "content": full_reply})
                if len(contents) > 20:
                    del contents[:-20]

            yield json.dumps({"done": True}, ensure_ascii=False) + "\n"
        except Exception as exc:
            yield json.dumps({"error": f"Ошибка потокового ответа: {exc}"}, ensure_ascii=False) + "\n"
        finally:
            if upstream is not None:
                upstream.close()

    return Response(
        generate(),
        mimetype="application/x-ndjson",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )

# ========== КОМАНДЫ ==========

@app.route('/command', methods=['POST'])
def handle_command():
    """Обработка команд (/search, /code, /image, плагины, алиасы)"""
    global contents
    data = request.get_json()
    cmd_line = data.get('command', '').strip()
    if not cmd_line.startswith('/'):
        return jsonify({'error': 'Команда должна начинаться с /'})

    parts = cmd_line[1:].split(maxsplit=1)
    cmd = parts[0].lower()
    args = parts[1].split() if len(parts) > 1 else []

    # Поиск в интернете
    if cmd == "search":
        query = " ".join(args)
        if not query:
            return jsonify({'error': 'Укажите запрос'})
        results = search_web(query)
        return jsonify({'result': results or 'Ничего не найдено'})

    # Генерация изображений (Pollinations.ai)
    if cmd == "image":
        prompt = " ".join(args)
        if not prompt:
            return jsonify({'error': 'Укажите описание изображения'})

        import base64 as b64
        import urllib.parse

        encoded_prompt = urllib.parse.quote(prompt)
        img_url = f"https://image.pollinations.ai/prompt/{encoded_prompt}?width=1024&height=1024&nologo=true"

        try:
            img_resp = requests.get(img_url, timeout=30)
            img_resp.raise_for_status()

            img_dir = os.path.join(os.path.dirname(__file__), "generated_images")
            os.makedirs(img_dir, exist_ok=True)

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"image_{timestamp}.png"
            filepath = os.path.join(img_dir, filename)

            with open(filepath, "wb") as f:
                f.write(img_resp.content)

            image_url = f"/generated_image?file={filename}"
            return jsonify({
                'result': f'✅ Изображение сгенерировано:\n\n![Image]({image_url})'
            })
        except Exception as e:
            return jsonify({'error': f'Ошибка генерации: {e}'})

    # Плагины
    if cmd in plugins:
        try:
            result = plugins[cmd](args)
            return jsonify({'result': result if result else 'OK'})
        except Exception as e:
            return jsonify({'error': str(e)})

    # Кастомные алиасы
    if cmd in custom_commands:
        cc = custom_commands[cmd]
        if cc["type"] == "plugin":
            plugin_name = cc["plugin"]
            if plugin_name in plugins:
                try:
                    result = plugins[plugin_name](list(cc.get("args_template", [])) + args)
                    return jsonify({'result': str(result) if result else "OK"})
                except Exception as e:
                    return jsonify({'error': str(e)})
        elif cc["type"] == "llm":
            prompt_template = cc.get("prompt", "{query}")
            query = " ".join(args) if args else ""
            rendered_prompt = prompt_template.replace("{query}", query)
            contents.append({"role": "user", "content": rendered_prompt})
            provider = PROVIDERS[current_provider]
            payload = {
                "model": current_model,
                "messages": [{"role": "system", "content": system_prompt}] + contents,
                "temperature": 0.7,
                "max_tokens": 3000,
            }
            data_resp, error = groq_request_with_rotation(
                provider["url"], payload, provider["headers"].copy()
            )
            if error:
                return jsonify({'error': error})
            reply = data_resp["choices"][0]["message"]["content"]
            contents.append({"role": "assistant", "content": reply})
            return jsonify({'result': reply})

    # Встроенные команды
    if cmd == "clear":
        contents.clear()
        return jsonify({'result': 'История очищена'})

    if cmd == "history":
        if not contents:
            return jsonify({'result': 'История пуста'})
        hist = "\n\n".join([f"**{msg['role']}**: {msg['content']}" for msg in contents])
        return jsonify({'result': hist})

    if cmd == "code":
        query = " ".join(args)
        if not query:
            return jsonify({'error': 'Укажите, какой код создать'})

        provider = PROVIDERS[current_provider]
        payload = {
            "model": current_model,
            "messages": [
                {"role": "system", "content": "Ты программист. Пиши чистый код с комментариями."},
                {"role": "user", "content": f"Напиши код: {query}"}
            ],
            "temperature": 0.3,
            "max_tokens": 3000,
        }
        data_resp, error = groq_request_with_rotation(
            provider["url"], payload, provider["headers"].copy(), timeout=60
        )
        if error:
            return jsonify({'error': error})
        reply = data_resp["choices"][0]["message"]["content"]
        return jsonify({'result': reply})

    # Управление алиасами
    if cmd == "alias":
        if not args:
            if not custom_commands:
                return jsonify({'result': 'Нет пользовательских команд. Добавьте через /alias add <имя> plugin <плагин> или /alias add <имя> llm <промпт>'})
            info = "Ваши команды:\n"
            for name, cc in custom_commands.items():
                info += f"/{name} → {cc['type']}\n"
            return jsonify({'result': info})

        subcmd = args[0].lower()
        if subcmd == "add":
            if len(args) < 3:
                return jsonify({'error': '/alias add <имя> plugin <плагин> или /alias add <имя> llm <промпт>'})
            name = args[1]
            type_ = args[2].lower()
            if type_ == "plugin":
                if len(args) < 4:
                    return jsonify({'error': 'Укажите плагин'})
                plugin_name = args[3]
                preset_args = args[4:] if len(args) > 4 else []
                custom_commands[name] = {"type": "plugin", "plugin": plugin_name, "args_template": preset_args}
            else:
                prompt = " ".join(args[3:]) if len(args) > 3 else "{query}"
                custom_commands[name] = {"type": "llm", "prompt": prompt}
            save_custom_commands()
            return jsonify({'result': f'Команда /{name} добавлена. Перезагрузите страницу.'})
        elif subcmd == "remove":
            if len(args) < 2:
                return jsonify({'error': 'Укажите имя команды'})
            name = args[1]
            if name in custom_commands:
                del custom_commands[name]
                save_custom_commands()
                return jsonify({'result': f'Команда /{name} удалена'})
            return jsonify({'error': 'Не найдена'})

    # ========== ГЛУБОКОЕ ИССЛЕДОВАНИЕ ==========
    if cmd == "research":
        query = " ".join(args)
        if not query:
            return jsonify({'error': 'Укажите вопрос. Пример: /research Как работает нейросеть'})

        search_result = search_web(query)
        if not search_result:
            search_result = "Информация не найдена в интернете."

        provider = PROVIDERS[current_provider]
        analysis_prompt = f"""Проанализируй следующую информацию и выдели 3-5 ключевых фактов по вопросу: "{query}"

Информация из интернета:
{search_result}

Выдели только ключевые факты, коротко."""

        analysis_payload = {
            "model": current_model,
            "messages": [{"role": "user", "content": analysis_prompt}],
            "temperature": 0.3,
            "max_tokens": 1000000,
        }
        data_analysis, error = groq_request_with_rotation(
            provider["url"], analysis_payload, provider["headers"].copy(), timeout=60
        )
        if error:
            analysis = f"Анализ не удался: {error}.\n\nИспользую сырой поиск:\n{search_result[:1000]}"
        else:
            analysis = data_analysis["choices"][0]["message"]["content"]

        final_prompt = f"""На основе анализа напиши подробный, структурированный ответ на вопрос: "{query}"

Анализ:
{analysis}

Требования к ответу:
- Подробный (3-5 абзацев)
- Если есть сравнения, характеристики, данные, списки — ОБЯЗАТЕЛЬНО используй Markdown-таблицы
- Структурированный (с маркированными списками где уместно)
- На русском языке
- Укажи источники, если они есть в анализе

Пример таблицы:
| Характеристика | Значение |
|---------------|----------|
| Скорость | 100 км/ч |
| Вес | 10 кг |"""

        final_payload = {
            "model": current_model,
            "messages": [{"role": "user", "content": final_prompt}],
            "temperature": 0.5,
            "max_tokens": 1000000,
        }
        data_final, error = groq_request_with_rotation(
            provider["url"], final_payload, provider["headers"].copy(), timeout=90
        )
        if error:
            final_answer = f"**🔍 Результаты поиска:**\n\n{search_result}\n\n**📊 Анализ:**\n\n{analysis}\n\n_(Финальный ответ не удалось сгенерировать: {error})_"
        else:
            final_answer = data_final["choices"][0]["message"]["content"]

        return jsonify({'result': final_answer})

    return jsonify({'error': f'Неизвестная команда: /{cmd}'})


# ========== ОТДАЧА ИЗОБРАЖЕНИЙ ==========

@app.route('/generated_image')
def generated_image():
    filename = request.args.get("file", "")
    if not filename:
        return jsonify({"error": "No filename"}), 400
    safe_name = filename.replace("..", "").replace("/", "")
    img_dir = os.path.join(os.path.dirname(__file__), "generated_images")
    filepath = os.path.join(img_dir, safe_name)
    if not os.path.exists(filepath):
        return jsonify({"error": "File not found"}), 404
    return send_file(filepath, mimetype='image/png')


# ========== ЗАГРУЗКА ФАЙЛОВ ==========

@app.route('/upload_image', methods=['POST'])
def upload_image():
    """Загрузка и анализ изображения через Groq Vision"""
    global contents

    if 'image' not in request.files:
        return jsonify({'error': 'Нет файла'}), 400

    file = request.files['image']
    if file.filename == '':
        return jsonify({'error': 'Файл не выбран'}), 400

    user_desc = request.form.get('description', '').strip()
    if not user_desc:
        user_desc = 'Подробно опиши что изображено на картинке. Опиши объекты, цвета, текст если есть, настроение и все детали.'

    upload_dir = os.path.join(os.path.dirname(__file__), "uploads")
    os.makedirs(upload_dir, exist_ok=True)

    import base64 as b64
    filename = file.filename
    filepath = os.path.join(upload_dir, filename)
    file.save(filepath)

    try:
        with open(filepath, "rb") as f:
            image_data = b64.b64encode(f.read()).decode('utf-8')

        mime_type = "image/jpeg"
        if filename.lower().endswith(".png"):
            mime_type = "image/png"
        elif filename.lower().endswith(".webp"):
            mime_type = "image/webp"
        elif filename.lower().endswith(".gif"):
            mime_type = "image/gif"

        data_url = f"data:{mime_type};base64,{image_data}"

        groq_key = os.getenv("GROQ_API_KEY", "")
        if not groq_key:
            return jsonify({'error': 'GROQ_API_KEY не задан в .env'})

        headers = {
            "Authorization": f"Bearer {groq_key}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": "meta-llama/llama-4-scout-17b-16e-instruct",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": user_desc},
                        {"type": "image_url", "image_url": {"url": data_url}}
                    ]
                }
            ],
            "max_tokens": 1500
        }
        resp = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers=headers,
            json=payload,
            timeout=30
        )
        resp.raise_for_status()
        description = resp.json()["choices"][0]["message"]["content"]

        contents.append({"role": "user", "content": f"[Изображение: {filename}] {user_desc}"})
        contents.append({"role": "assistant", "content": description})
        if len(contents) > 20:
            contents = contents[-20:]

        return jsonify({
            'result': f'''📷 **Анализ изображения {filename} (Groq Vision):**

{description}'''
        })

    except Exception as e:
        print(f"upload_image error: {e}")
        return jsonify({'error': f'Ошибка анализа изображения: {str(e)}'})


@app.route('/upload_file', methods=['POST'])
def upload_file():
    """Загрузка файла + анализ через Groq или Cerebras"""
    global contents

    if 'file' not in request.files:
        return jsonify({'error': 'Нет файла'}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'Файл не выбран'}), 400

    user_desc = request.form.get('description', '').strip()

    upload_dir = os.path.join(os.path.dirname(__file__), "uploads")
    os.makedirs(upload_dir, exist_ok=True)

    filename = file.filename
    filepath = os.path.join(upload_dir, filename)
    file.save(filepath)

    text_extensions = ['.txt', '.json', '.csv', '.py', '.js', '.html', '.css', '.md', '.xml', '.yaml', '.yml', '.log', '.ini', '.cfg']
    ext = os.path.splitext(filename)[1].lower()

    if ext not in text_extensions:
        return jsonify({
            'result': f'''⚠️ Формат {ext} не поддерживается.

Поддерживаются: txt, json, csv, py, js, html, css, md, xml, yaml, log'''
        })

    try:
        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            file_content = f.read()[:5000]
    except Exception as e:
        return jsonify({'error': f'Ошибка чтения файла: {str(e)}'})

    if not file_content.strip():
        return jsonify({'result': f'⚠️ Файл {filename} пустой.'})

    if not user_desc:
        if ext == '.py':
            user_desc = 'Проанализируй этот Python код: объясни что он делает, найди ошибки и предложи улучшения.'
        elif ext in ['.js', '.ts']:
            user_desc = 'Проанализируй этот JavaScript код: объясни структуру, найди проблемы.'
        elif ext == '.html':
            user_desc = 'Проанализируй эту HTML страницу: опиши структуру и найди проблемы.'
        elif ext == '.css':
            user_desc = 'Проанализируй этот CSS файл: опиши стили и найди проблемы.'
        elif ext == '.json':
            user_desc = 'Опиши структуру этого JSON и объясни что в нём хранится.'
        elif ext == '.csv':
            user_desc = 'Проанализируй эти CSV данные: опиши колонки и содержимое.'
        elif ext == '.md':
            user_desc = 'Сделай резюме этого Markdown документа и выдели главное.'
        else:
            user_desc = 'Подробно проанализируй содержимое этого файла и объясни что в нём.'

    analysis_prompt = f"""Пользователь загрузил файл: {filename}

Задача: {user_desc}

Содержимое файла:
{file_content}

Отвечай на русском языке. Используй Markdown форматирование."""

    groq_error = None
    try:
        provider = PROVIDERS[current_provider]
        payload = {
            "model": current_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": analysis_prompt}
            ],
            "temperature": 0.3,
            "max_tokens": 3000,
        }
        resp = requests.post(
            provider["url"],
            json=payload,
            headers=provider["headers"],
            timeout=60
        )
        resp.raise_for_status()
        reply = resp.json()["choices"][0]["message"]["content"]

        contents.append({"role": "user", "content": f"[Файл: {filename}] {user_desc}"})
        contents.append({"role": "assistant", "content": reply})
        if len(contents) > 20:
            contents = contents[-20:]

        return jsonify({
            'result': f'''📁 **Анализ файла {filename}:**

{reply}'''
        })

    except Exception as e1:
        groq_error = str(e1)
        print(f"Groq upload_file error: {e1}")

    try:
        cerebras_key = os.getenv("CEREBRAS_API_KEY", "")
        if not cerebras_key:
            raise Exception("CEREBRAS_API_KEY не задан")

        cerebras_headers = {
            "Authorization": f"Bearer {cerebras_key}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": "qwen-3-235b-a22b-instruct-2507",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": analysis_prompt}
            ],
            "temperature": 0.3,
            "max_tokens": 3000,
        }
        resp = requests.post(
            "https://api.cerebras.ai/v1/chat/completions",
            json=payload,
            headers=cerebras_headers,
            timeout=60
        )
        resp.raise_for_status()
        reply = resp.json()["choices"][0]["message"]["content"]

        contents.append({"role": "user", "content": f"[Файл: {filename}] {user_desc}"})
        contents.append({"role": "assistant", "content": reply})
        if len(contents) > 20:
            contents = contents[-20:]

        return jsonify({
            'result': f'''📁 **Анализ файла {filename} (Cerebras):**

{reply}'''
        })

    except Exception as e2:
        print(f"Cerebras upload_file error: {e2}")
        return jsonify({
            'result': f'''⚠️ AI недоступен (Groq: {groq_error}, Cerebras: {str(e2)})

**Содержимое файла {filename}:**

```
{file_content[:2000]}
```'''
        })


# ========== МУЛЬТИМЕДИА ==========
MEDIA_DIR = os.path.join(os.path.dirname(__file__), "generated_media")
os.makedirs(MEDIA_DIR, exist_ok=True)

@app.route('/media/<path:filename>')
def media_file(filename):
    safe_name = os.path.basename(filename)
    filepath = os.path.join(MEDIA_DIR, safe_name)
    if not os.path.isfile(filepath):
        return jsonify({"error": "Медиафайл не найден"}), 404
    return send_file(filepath, mimetype=mimetypes.guess_type(filepath)[0] or "application/octet-stream")

@app.route('/api/media/upload', methods=['POST'])
def media_upload():
    media = request.files.get('file')
    if not media or not media.filename:
        return jsonify({"error": "Файл не выбран"}), 400
    content_type = media.mimetype or mimetypes.guess_type(media.filename)[0] or "application/octet-stream"
    if not (content_type.startswith("audio/") or content_type.startswith("video/") or content_type.startswith("image/")):
        return jsonify({"error": "Поддерживаются только аудио, видео и изображения"}), 415
    extension = os.path.splitext(media.filename)[1].lower() or mimetypes.guess_extension(content_type) or ""
    saved_name = f"upload_{uuid.uuid4().hex}{extension}"
    media.save(os.path.join(MEDIA_DIR, saved_name))
    return jsonify({"success": True, "filename": media.filename, "type": content_type, "url": f"/media/{saved_name}"})

@app.route('/api/media/transcribe', methods=['POST'])
def media_transcribe():
    media = request.files.get('file')
    if not media or not media.filename:
        return jsonify({"error": "Аудиофайл не выбран"}), 400
    endpoint = os.getenv("AUDIO_TRANSCRIPTION_URL", "")
    api_key = os.getenv("AUDIO_API_KEY", "")
    if not endpoint:
        if os.getenv("GROQ_API_KEY") or GROQ_KEYS:
            endpoint = "https://api.groq.com/openai/v1/audio/transcriptions"
            api_key = api_key or get_groq_key()
        elif os.getenv("OPENAI_API_KEY"):
            endpoint = "https://api.openai.com/v1/audio/transcriptions"
            api_key = api_key or os.getenv("OPENAI_API_KEY", "")
    if not endpoint:
        return jsonify({"error": "Настройте AUDIO_TRANSCRIPTION_URL и AUDIO_API_KEY либо добавьте GROQ_API_KEY/OPENAI_API_KEY в .env"}), 503
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        response = requests.post(endpoint, headers=headers, files={"file": (media.filename, media.stream, media.mimetype)}, data={"model": os.getenv("AUDIO_TRANSCRIPTION_MODEL", "whisper-large-v3-turbo"), "language": request.form.get("language", "ru")}, timeout=180)
        response.raise_for_status()
        result = response.json()
        return jsonify({"success": True, "text": result.get("text", ""), "language": result.get("language")})
    except (requests.RequestException, ValueError) as exc:
        return jsonify({"error": f"Ошибка расшифровки аудио: {exc}"}), 502

@app.route('/api/media/tts', methods=['POST'])
def media_tts():
    data = request.get_json() or {}
    text = data.get("text", "").strip()
    if not text:
        return jsonify({"error": "Введите текст для озвучивания"}), 400
    endpoint = os.getenv("AUDIO_TTS_URL", "")
    api_key = os.getenv("AUDIO_API_KEY", "")
    model = os.getenv("AUDIO_TTS_MODEL", "tts-1")
    if not endpoint and os.getenv("OPENAI_API_KEY"):
        endpoint, api_key = "https://api.openai.com/v1/audio/speech", api_key or os.getenv("OPENAI_API_KEY", "")
    if not endpoint:
        return jsonify({"error": "Настройте AUDIO_TTS_URL и AUDIO_API_KEY либо добавьте OPENAI_API_KEY в .env"}), 503
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"} if api_key else {"Content-Type": "application/json"}
    try:
        response = requests.post(endpoint, headers=headers, json={"model": model, "input": text, "voice": data.get("voice", "alloy"), "response_format": "mp3"}, timeout=180)
        response.raise_for_status()
        filename = f"speech_{uuid.uuid4().hex}.mp3"
        with open(os.path.join(MEDIA_DIR, filename), "wb") as audio_file:
            audio_file.write(response.content)
        return jsonify({"success": True, "url": f"/media/{filename}", "filename": filename})
    except requests.RequestException as exc:
        return jsonify({"error": f"Ошибка генерации аудио: {exc}"}), 502

@app.route('/api/media/image', methods=['POST'])
def media_image():
    data = request.get_json() or {}
    prompt = data.get("prompt", "").strip()
    if not prompt:
        return jsonify({"error": "Введите описание изображения"}), 400
    endpoint = os.getenv("IMAGE_GENERATION_URL", "")
    api_key = os.getenv("IMAGE_API_KEY", "") or os.getenv("OPENAI_API_KEY", "")
    if not endpoint and os.getenv("OPENAI_API_KEY"):
        endpoint = "https://api.openai.com/v1/images/generations"
    if not endpoint:
        from urllib.parse import quote
        width, height = int(data.get("width", 1024)), int(data.get("height", 1024))
        url = f"https://image.pollinations.ai/prompt/{quote(prompt)}?width={width}&height={height}&nologo=True"
        return jsonify({"success": True, "url": url, "provider": "pollinations"})
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"} if api_key else {"Content-Type": "application/json"}
    try:
        response = requests.post(endpoint, headers=headers, json={"model": os.getenv("IMAGE_MODEL", "gpt-image-1"), "prompt": prompt, "size": data.get("size", "1024x1024"), "n": 1}, timeout=240)
        response.raise_for_status()
        result = response.json().get("data", [{}])[0]
        if result.get("url"):
            return jsonify({"success": True, "url": result["url"], "provider": "configured"})
        if result.get("b64_json"):
            import base64
            filename = f"image_{uuid.uuid4().hex}.png"
            with open(os.path.join(MEDIA_DIR, filename), "wb") as image_file:
                image_file.write(base64.b64decode(result["b64_json"]))
            return jsonify({"success": True, "url": f"/media/{filename}", "provider": "configured"})
        return jsonify({"error": "Провайдер не вернул изображение"}), 502
    except (requests.RequestException, ValueError, IndexError) as exc:
        return jsonify({"error": f"Ошибка генерации изображения: {exc}"}), 502

@app.route('/api/media/video', methods=['POST'])
def media_video():
    data = request.get_json() or {}
    prompt = data.get("prompt", "").strip()
    endpoint = os.getenv("VIDEO_API_URL", "")
    if not prompt:
        return jsonify({"error": "Введите описание видео"}), 400
    if not endpoint:
        return jsonify({"error": "Для видео укажите VIDEO_API_URL, VIDEO_API_KEY и VIDEO_MODEL в .env. У видео-провайдеров разные API, поэтому endpoint настраивается явно."}), 503
    api_key = os.getenv("VIDEO_API_KEY", "") or os.getenv("OPENAI_API_KEY", "")
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"} if api_key else {"Content-Type": "application/json"}
    payload = {"model": os.getenv("VIDEO_MODEL", ""), "prompt": prompt, "duration": data.get("duration", 5), "aspect_ratio": data.get("aspect_ratio", "16:9")}
    try:
        response = requests.post(endpoint, headers=headers, json=payload, timeout=240)
        response.raise_for_status()
        result = response.json()
        return jsonify({"success": True, "url": result.get("video_url") or result.get("url"), "job_id": result.get("id") or result.get("job_id"), "status_url": result.get("status_url"), "raw": result})
    except (requests.RequestException, ValueError) as exc:
        return jsonify({"error": f"Ошибка генерации видео: {exc}"}), 502

# ========== МОДЕЛИ ==========

@app.route('/models_list')
def models_list():
    providers_list = []
    for key, data in PROVIDERS.items():
        providers_list.append({"provider": key, "list": data["models"]})
    return jsonify({"models": providers_list, "current": current_model})

@app.route('/switch_model')
def switch_model():
    global current_provider, current_model
    model_id = request.args.get('model_id', '')
    for key, data in PROVIDERS.items():
        for m in data["models"]:
            if m["id"] == model_id:
                current_provider = key
                current_model = model_id
                return jsonify({"success": True, "provider": key})
    return jsonify({'error': 'Модель не найдена'}), 400


# ========== АДМИН API ==========

@app.route('/api/admin/stats')
def admin_stats():
    return jsonify({
        "models": PROVIDERS,
        "current_provider": current_provider,
        "current_model": current_model,
        "history_messages": len(contents),
        "plugins_loaded": list(plugins.keys()),
        "custom_commands": list(custom_commands.keys()),
        "system_prompt": system_prompt,
        "voice_enabled": os.environ.get("ASSISTANT_VOICE_REPLY", "0") == "1"
    })

@app.route('/api/admin/settings', methods=['POST'])
def admin_settings():
    global system_prompt, current_provider, current_model
    data = request.get_json() or {}
    if "system_prompt" in data:
        system_prompt = data["system_prompt"]
    if "provider" in data and data["provider"] in PROVIDERS:
        current_provider = data["provider"]
    if "model" in data:
        for key, pdata in PROVIDERS.items():
            for m in pdata["models"]:
                if m["id"] == data["model"]:
                    current_provider = key
                    current_model = data["model"]
                    break
    return jsonify({"success": True})

@app.route('/api/admin/save_code', methods=['POST'])
def admin_save_code():
    data = request.get_json() or {}
    filename = data.get("filename", "script.py")
    code = data.get("code", "")
    if not code:
        return jsonify({"error": "Нет кода"}), 400
    code_dir = os.path.join(os.path.dirname(__file__), "saved_codes")
    os.makedirs(code_dir, exist_ok=True)
    filepath = os.path.join(code_dir, filename)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(code)
    return jsonify({"success": True, "filepath": filepath})

@app.route('/api/admin/run_saved_code', methods=['POST'])
def admin_run_saved_code():
    data = request.get_json() or {}
    filename = data.get("filename", "script.py")
    code_dir = os.path.join(os.path.dirname(__file__), "saved_codes")
    filepath = os.path.join(code_dir, filename)
    if not os.path.exists(filepath):
        return jsonify({"error": "Файл не найден"}), 404
    try:
        result = subprocess.run(["python3", filepath], capture_output=True, text=True, timeout=10)
        output = result.stdout
        if result.stderr:
            output += "\nSTDERR: " + result.stderr
        return jsonify({"result": output or "Нет вывода"})
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Превышено время (10 сек)"}), 408
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/admin/saved_codes')
def admin_saved_codes():
    code_dir = os.path.join(os.path.dirname(__file__), "saved_codes")
    if not os.path.exists(code_dir):
        return jsonify({"files": []})
    files = sorted([f for f in os.listdir(code_dir) if f.endswith(".py")])
    return jsonify({"files": files})

@app.route('/api/admin/load_code')
def admin_load_code():
    filename = request.args.get("file", "")
    code_dir = os.path.join(os.path.dirname(__file__), "saved_codes")
    filepath = os.path.join(code_dir, filename)
    if not os.path.exists(filepath):
        return jsonify({"error": "Файл не найден"}), 404
    with open(filepath, "r", encoding="utf-8") as f:
        code = f.read()
    return jsonify({"code": code, "filename": filename})


# ========== СТАТУС КЛЮЧЕЙ GROQ (админка) ==========

@app.route('/api/admin/groq_keys')
def admin_groq_keys():
    """Статус всех Groq API ключей"""
    return jsonify({
        "keys": get_groq_key_status(),
        "current_key_index": groq_key_index,
        "total_keys": len(GROQ_KEYS),
        "cooldown_hours": GROQ_KEY_COOLDOWN / 3600
    })

@app.route('/api/admin/groq_keys/reset', methods=['POST'])
def admin_reset_groq_keys():
    """Сбросить все cooldown'ы (для админа)"""
    global groq_key_index
    for key_info in GROQ_KEYS:
        key_info["exhausted_at"] = None
    groq_key_index = 0
    return jsonify({"success": True, "message": "Все ключи сброшены"})


# ========== СТАТИКА ==========

@app.route('/static/')
def static_files(filename):
    return send_from_directory('static', filename)

@app.route('/composio')
def composio_page():
    return render_template('composio.html')

@app.route('/api/composio/connect', methods=['POST'])
def composio_connect():
    data = request.get_json() or {}
    api_key = data.get('api_key', '')
    if not api_key:
        return jsonify({'error': 'API ключ не указан'}), 400
    try:
        headers = {'x-api-key': api_key, 'Content-Type': 'application/json'}
        resp = requests.get('https://backend.composio.dev/api/v3.1/toolkits?limit=5', headers=headers, timeout=10)
        if resp.status_code == 401:
            return jsonify({'error': 'Неверный API ключ'}), 401
        resp.raise_for_status()
        os.environ['COMPOSIO_API_KEY'] = api_key
        return jsonify({'success': True, 'message': 'Подключено к Composio'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/composio/integrations', methods=['GET'])
def composio_integrations():
    api_key = os.getenv('COMPOSIO_API_KEY', '')
    if not api_key:
        return jsonify({'error': 'Composio не подключен'}), 400
    try:
        headers = {'x-api-key': api_key, 'Content-Type': 'application/json'}
        resp = requests.get('https://backend.composio.dev/api/v3.1/toolkits?limit=50', headers=headers, timeout=10)
        resp.raise_for_status()
        return jsonify({'integrations': resp.json()})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/composio/connected_accounts', methods=['GET'])
def composio_connected_accounts():
    api_key = os.getenv('COMPOSIO_API_KEY', '')
    if not api_key:
        return jsonify({'error': 'Composio не подключен'}), 400
    try:
        headers = {'x-api-key': api_key, 'Content-Type': 'application/json'}
        resp = requests.get('https://backend.composio.dev/api/v3.1/connected_accounts', headers=headers, timeout=10)
        resp.raise_for_status()
        return jsonify(resp.json())
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/composio/connect_account', methods=['POST'])
def composio_connect_account():
    api_key = os.getenv('COMPOSIO_API_KEY', '')
    if not api_key:
        return jsonify({'error': 'Composio не подключен'}), 400
    data = request.get_json() or {}
    toolkit = data.get('toolkit', '')
    if not toolkit:
        return jsonify({'error': 'Укажи toolkit'}), 400
    try:
        headers = {'x-api-key': api_key, 'Content-Type': 'application/json'}
        session_resp = requests.post('https://backend.composio.dev/api/v3.1/tool_router/session', headers=headers, json={"user_id": "novauser"}, timeout=10)
        session_resp.raise_for_status()
        session_data = session_resp.json()
        session_id = session_data.get('session_id', '')
        if not session_id:
            return jsonify({'error': 'Не удалось создать сессию'}), 500
        link_resp = requests.post(f'https://backend.composio.dev/api/v3/tool_router/session/{session_id}/link', headers=headers, json={"toolkit": toolkit}, timeout=10)
        link_resp.raise_for_status()
        link_data = link_resp.json()
        redirect_url = link_data.get('redirect_url', '')
        return jsonify({'success': True, 'redirect_url': redirect_url, 'connected_account_id': link_data.get('connected_account_id', '')})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/composio/execute', methods=['POST'])
def composio_execute():
    api_key = os.getenv('COMPOSIO_API_KEY', '')
    if not api_key:
        return jsonify({'error': 'Composio не подключен'}), 400
    data = request.get_json() or {}
    action_name = data.get('action', '')
    params = data.get('params', {})
    connected_account_id = data.get('connected_account_id', '')
    if not action_name:
        return jsonify({'error': 'Укажи действие'}), 400
    try:
        headers = {'x-api-key': api_key, 'Content-Type': 'application/json'}
        payload = {"input": params, "allow_tracing": True}
        if connected_account_id:
            payload["connected_account_id"] = connected_account_id
        resp = requests.post(f'https://backend.composio.dev/api/v2/actions/{action_name}/execute', json=payload, headers=headers, timeout=30)
        resp.raise_for_status()
        return jsonify({'result': resp.json()})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/composio/actions', methods=['GET'])
def composio_actions():
    api_key = os.getenv('COMPOSIO_API_KEY', '')
    if not api_key:
        return jsonify({'error': 'Composio не подключен'}), 400
    toolkit = request.args.get('toolkit', '')
    try:
        headers = {'x-api-key': api_key, 'Content-Type': 'application/json'}
        url = 'https://backend.composio.dev/api/v2/actions?limit=20'
        if toolkit:
            url += f'&apps={toolkit}'
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        return jsonify(resp.json())
    except Exception as e:
        return jsonify({'error': str(e)}), 500



# ========== LEGACY HISTORY API ==========

@app.route('/api/history/clear', methods=['DELETE', 'POST'])
def api_history_clear():
    global contents
    new_chat_id = create_chat()
    session['chat_id'] = new_chat_id
    contents = []
    return jsonify({'success': True, 'new_chat_id': new_chat_id})

@app.route('/api/history', methods=['GET'])
def api_history_get():
    chat_id = get_or_create_session_chat()
    messages = get_chat_history(chat_id, limit=200)
    return jsonify({'history': messages, 'chat_id': chat_id})

# ========== API ЧАТОВ (ПАМЯТЬ) ==========

@app.route('/api/chats', methods=['GET'])
def api_list_chats():
    """Список всех чатов пользователя."""
    chats = list_chats(limit=100)
    return jsonify({'chats': chats})

@app.route('/api/chats', methods=['POST'])
def api_create_chat():
    """Создать новый чат."""
    data = request.get_json() or {}
    title = data.get('title', 'Новый чат')
    chat_id = create_chat(title)
    session['chat_id'] = chat_id
    return jsonify({'chat_id': chat_id, 'title': title})

@app.route('/api/chats/<chat_id>', methods=['GET'])
def api_get_chat(chat_id):
    """Получить историю сообщений чата."""
    messages = get_chat_history(chat_id, limit=200)
    return jsonify({'chat_id': chat_id, 'messages': messages})

@app.route('/api/chats/<chat_id>', methods=['DELETE'])
def api_delete_chat(chat_id):
    """Удалить чат."""
    delete_chat(chat_id)
    if session.get('chat_id') == chat_id:
        session.pop('chat_id', None)
    return jsonify({'success': True})

@app.route('/api/chats/<chat_id>/switch', methods=['POST'])
def api_switch_chat(chat_id):
    """Переключиться на другой чат."""
    global contents
    session['chat_id'] = chat_id
    contents = get_chat_history(chat_id, limit=20)
    return jsonify({'success': True, 'chat_id': chat_id})

@app.route('/api/chats/current', methods=['GET'])
def api_current_chat():
    """Текущий активный чат и его история."""
    chat_id = get_or_create_session_chat()
    messages = get_chat_history(chat_id, limit=200)
    return jsonify({'chat_id': chat_id, 'messages': messages})

@app.route('/api/chats/clear', methods=['POST'])
def api_clear_current_chat():
    """Очистить текущий чат (создаёт новый)."""
    global contents
    session['chat_id'] = create_chat()
    contents = []
    return jsonify({'success': True, 'new_chat_id': session['chat_id']})

# ========== ЗАПУСК ==========

if __name__ == '__main__':
    print("=" * 50)
    print("NovaMind AI Assistant запущен")
    print("http://localhost:5000")
    print("=" * 50)
    app.run(host='0.0.0.0', port=5000)
