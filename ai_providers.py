"""
ai_providers.py — Запросы к AI провайдерам (Groq, Gemini, OpenAI-compatible)
"""
import os
import json
import time
import requests
from groq_rotation import get_groq_key, mark_groq_key_exhausted

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
    key=(os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_AI_STUDIO_KEY") or "").strip()
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


import re

# ---------- Метки провайдеров ----------
MODEL_ERRORS: dict = {}
PROVIDER_LABELS = {
    "groq":             "Groq",
    "cerebras":         "Cerebras",
    "openrouter":       "OpenRouter",
    "google_ai_studio": "Google AI Studio",
    "openai_compatible":"OpenAI-compatible / Local",
}

# ---------- Вспомогательные функции моделей ----------
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
    is_free = ":free" in model_id or (prompt_price in {"0","0.0","0.000000"} and completion_price in {"0","0.0","0.000000"})
    context = model.get("context_length") or model.get("context_window") or model.get("max_context_length") or 0
    architecture = model.get("architecture") or {}
    modality = architecture.get("modality", "text->text") if isinstance(architecture, dict) else "text->text"
    input_modalities = architecture.get("input_modalities") if isinstance(architecture, dict) else None
    output_modalities = architecture.get("output_modalities") if isinstance(architecture, dict) else None
    if not input_modalities:
        input_modalities = [str(modality).split("->")[0]] if "->" in str(modality) else ["text"]
    if not output_modalities:
        output_modalities = [str(modality).split("->")[-1]] if "->" in str(modality) else ["text"]
    in_mods = [str(x).lower() for x in (input_modalities or [])]
    out_mods = [str(x).lower() for x in (output_modalities or [])]
    media_types = []
    if "image" in out_mods: media_types.append("image")
    if "audio" in out_mods: media_types.append("audio")
    if "video" in out_mods: media_types.append("video")
    if not media_types: media_types.append("text")
    return {
        "id": model_id, "name": name, "provider": provider,
        "provider_name": PROVIDER_LABELS.get(provider, provider),
        "context_length": int(context or 0),
        "parameters_b": model.get("parameter_count") or model_size_billions(model_id, name),
        "prompt_price": prompt_price, "completion_price": completion_price,
        "free": is_free, "modality": modality,
        "input_modalities": in_mods, "output_modalities": out_mods,
        "media_types": media_types,
        "pricing_status": "free" if is_free else ("paid" if prompt_price or completion_price else "unknown"),
        "created": model.get("created", 0), "description": model.get("description", ""),
    }

def provider_api_headers(provider):
    from config import PROVIDERS
    from groq_rotation import get_groq_key
    if provider == "groq":
        key = get_groq_key()
        return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"} if key else None
    if provider == "openrouter":
        key = os.getenv("OPENROUTER_API_KEY", "")
        return {"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                "HTTP-Referer": "http://localhost:5000", "X-Title": "NovaMind AI"} if key else None
    if provider == "cerebras":
        key = os.getenv("CEREBRAS_API_KEY", "")
        return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"} if key else None
    if provider == "google_ai_studio":
        key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_AI_STUDIO_KEY") or ""
        return {"x-goog-api-key": key, "Content-Type": "application/json"} if key else None
    key = os.getenv("OPENAI_COMPATIBLE_KEY") or os.getenv("OPENAI_API_KEY") or "ollama"
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

def _local_model_fallback():
    """Резервный каталог для локального OpenAI-compatible сервера.
    Эти записи не являются отдельными облачными моделями: они представляют
    загруженную локально модель, когда /v1/models временно недоступен.
    """
    return [
        normalize_model({
            "id": os.getenv("LOCAL_MODEL_ID", "local-llama"),
            "name": os.getenv("LOCAL_MODEL_NAME", "Local Llama (llama.cpp)"),
            "description": "Локальная модель через llama-server / OpenAI-compatible API.",
            "context_length": int(os.getenv("LOCAL_MODEL_CONTEXT", "4096")),
            "pricing": {"prompt": "0", "completion": "0"},
        }, "openai_compatible"),
        normalize_model({
            "id": "llama.cpp",
            "name": "Llama.cpp Local",
            "description": "Резервная запись для локального llama-server.",
            "context_length": 4096,
            "pricing": {"prompt": "0", "completion": "0"},
        }, "openai_compatible"),
    ]

def _local_catalog_urls():
    """Возвращает возможные /models endpoints для llama-server и совместимых API."""
    configured = (
        os.getenv("OPENAI_COMPATIBLE_URL")
        or os.getenv("OPENAI_BASE_URL")
        or "http://127.0.0.1:8080/v1"
    ).rstrip("/")
    candidates = []
    for suffix in ("/models", "/v1/models"):
        if configured.endswith("/chat/completions"):
            base = configured[:-len("/chat/completions")].rstrip("/")
            candidates.append(base + suffix if suffix != "/models" else base + "/models")
        elif configured.endswith("/models"):
            candidates.append(configured)
        else:
            candidates.append(configured + suffix)
    # Deduplicate while preserving order.
    return list(dict.fromkeys(candidates))

def fetch_available_models(provider, force=False):
    from config import PROVIDERS, MODEL_CACHE
    if not force and provider in MODEL_CACHE:
        return MODEL_CACHE[provider]

    if provider == "openrouter":
        urls = ["https://openrouter.ai/api/v1/models"]
    elif provider == "google_ai_studio":
        if not (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_AI_STUDIO_KEY")):
            MODEL_ERRORS[provider] = "GEMINI_API_KEY не найден в .env"
            return []
        urls = ["https://generativelanguage.googleapis.com/v1beta/models"]
    elif provider == "groq":
        urls = ["https://api.groq.com/openai/v1/models"]
    elif provider == "cerebras":
        urls = ["https://api.cerebras.ai/v1/models"]
    elif provider == "openai_compatible":
        # llama-server, Ollama, LM Studio, vLLM, LocalAI и другие
        # OpenAI-compatible runtimes обычно отдают каталог через /v1/models.
        urls = _local_catalog_urls()
    else:
        return [normalize_model(m, provider) for m in PROVIDERS[provider]["models"]]

    headers = provider_api_headers(provider)
    if not headers:
        return []

    last_error = None
    for url in urls:
        try:
            resp = requests.get(url, headers=headers, timeout=8)
            resp.raise_for_status()
            if provider == "google_ai_studio":
                raw = resp.json().get("models", [])
                models = []
                for m in raw:
                    mid = str(m.get("name", "")).removeprefix("models/")
                    if mid and "generateContent" in (m.get("supportedGenerationMethods") or []):
                        models.append(normalize_model({
                            "id": mid,
                            "name": m.get("displayName") or mid,
                            "description": m.get("description", ""),
                            "context_length": m.get("inputTokenLimit", 0),
                            "pricing": {"prompt": "0", "completion": "0"},
                        }, provider))
            else:
                raw = resp.json().get("data", [])
                models = [normalize_model(m, provider) for m in raw if m.get("id")]

            if models:
                MODEL_CACHE[provider] = models
                MODEL_ERRORS.pop(provider, None)
                return models

            last_error = f"{url}: каталог пуст"
        except (requests.RequestException, ValueError, TypeError) as exc:
            last_error = f"{url}: {exc}"
            if provider != "openai_compatible":
                break

    print(f"[ai_providers] Model catalog error ({provider}): {last_error}")
    MODEL_ERRORS[provider] = last_error or "Каталог моделей недоступен"

    # Local provider must remain selectable even if its catalog endpoint
    # is unavailable. The selected model is sent to the same chat endpoint.
    if provider == "openai_compatible":
        fallback = _local_model_fallback()
    else:
        fallback = [
            normalize_model(m, provider)
            for m in PROVIDERS.get(provider, {}).get("models", [])
            if m.get("id")
        ]

    if fallback:
        MODEL_CACHE[provider] = fallback
        return fallback
    return []

# ---------- Генеративные media-модели ----------
GOOGLE_MEDIA_MODELS = [
    {"id":"gemini-3.1-flash-image","name":"Nano Banana 2","type":"image","free":False,"price":"paid","description":"Генерация и редактирование изображений."},
    {"id":"gemini-3-pro-image","name":"Nano Banana Pro","type":"image","free":False,"price":"paid","description":"Профессиональная генерация изображений."},
    {"id":"gemini-3.8-flash-tts","name":"Gemini 3.8 Flash TTS","type":"audio","free":True,"price":"free-tier","description":"Синтез речи из текста; есть бесплатный тариф с лимитами."},
    {"id":"gemini-3.8-flash-lite-tts","name":"Gemini 3.8 Flash-Lite TTS","type":"audio","free":True,"price":"free-tier","description":"Экономичный TTS; есть бесплатный тариф с лимитами."},
    {"id":"veo-3.1-generate-preview","name":"Veo 3.1","type":"video","free":False,"price":"paid","description":"Генерация видео с синхронизированным аудио; API — платный тариф."},
    {"id":"veo-3.1-lite-generate-preview","name":"Veo 3.1 Lite","type":"video","free":False,"price":"paid","description":"Экономичная генерация и редактирование видео."},
    {"id":"gemini-omni-1.1-flash","name":"Gemini Omni Flash","type":"video","free":False,"price":"paid","description":"Генерация и редактирование видео с нативным аудио."},
]

def media_models_catalog(media_type="all", provider="all", force=False):
    """Возвращает модели image/audio/video с понятным статусом цены."""
    models=[]
    if provider in ("all", "openrouter"):
        for m in fetch_available_models("openrouter", force=force):
            if any(t in (m.get("media_types") or []) for t in ("image","audio","video")):
                item=dict(m)
                item["source_type"]="live"
                models.append(item)
    if provider in ("all", "google_ai_studio"):
        configured=bool(os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_AI_STUDIO_KEY"))
        for m in GOOGLE_MEDIA_MODELS:
            models.append({
                "id":m["id"], "name":m["name"], "provider":"google_ai_studio",
                "provider_name":"Google AI Studio", "media_types":[m["type"]],
                "free":m["free"], "pricing_status":m["price"],
                "prompt_price":"","completion_price":"","configured":configured,
                "description":m["description"], "context_length":0,
                "parameters_b":0, "source_type":"official-catalog"
            })
    if media_type != "all":
        models=[m for m in models if media_type in (m.get("media_types") or [])]
    unique={}
    for m in models: unique[(m.get("provider"),m.get("id"))]=m
    return list(unique.values())

def provider_status():
    from groq_rotation import GROQ_KEYS
    return {
        "groq":             bool(GROQ_KEYS),
        "cerebras":         bool(os.getenv("CEREBRAS_API_KEY")),
        "openrouter":       bool(os.getenv("OPENROUTER_API_KEY")),
        "google_ai_studio": bool(os.getenv("GEMINI_API_KEY")),
        "openai_compatible": True,
    }
