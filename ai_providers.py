"""
ai_providers.py — Запросы к AI провайдерам (Groq, Gemini, OpenAI-compatible)
"""
import os
import json
import time
import re
import logging
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import requests
from groq_rotation import get_groq_key, mark_groq_key_exhausted, GROQ_KEY_COOLDOWN, mark_groq_key_success

logger = logging.getLogger(__name__)

def openai_compatible_request(url, payload, headers, timeout=90, max_retries=2):
    """Запрос к OpenAI-compatible серверу; HTTP-ошибки разбираются по status_code."""
    for attempt in range(max(1, int(max_retries))):
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=timeout)
            if resp.status_code == 429:
                if attempt < max_retries - 1:
                    time.sleep(_retry_after_seconds(resp.headers, 2 ** attempt))
                    continue
                return None, f"OpenAI-compatible сервер ограничил запросы (HTTP 429): {(resp.text or '')[:500]}"
            if resp.status_code == 401:
                return None, "OpenAI-compatible сервер отклонил API ключ (401)"
            resp.raise_for_status()
            return resp.json(), None
        except requests.exceptions.Timeout:
            if attempt < max_retries - 1:
                continue
            return None, "Таймаут OpenAI-compatible сервера"
        except requests.exceptions.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else "?"
            if status == 429 and attempt < max_retries - 1:
                time.sleep(2 ** attempt)
                continue
            return None, f"OpenAI-compatible сервер вернул HTTP {status}: {str(exc)[:500]}"
        except requests.exceptions.RequestException as exc:
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
                continue
            return None, f"OpenAI-compatible сервер недоступен: {exc.__class__.__name__}"
        except ValueError:
            return None, "OpenAI-compatible сервер вернул некорректный JSON"
    return None, "OpenAI-compatible сервер недоступен"


def _retry_after_seconds(headers, default=60):
    """Разбирает Retry-After и rate-limit reset headers с верхним безопасным пределом."""
    headers = headers or {}
    value = headers.get("Retry-After")
    if value:
        try:
            return max(1, min(int(float(value)), 25 * 3600))
        except (TypeError, ValueError):
            try:
                when = parsedate_to_datetime(value)
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
                return max(1, min(int((when - datetime.now(timezone.utc)).total_seconds()), 25 * 3600))
            except (TypeError, ValueError, OverflowError):
                pass
    for name in ("X-RateLimit-Reset", "x-ratelimit-reset-requests", "x-ratelimit-reset-tokens", "x-ratelimit-reset"):
        value = headers.get(name)
        if not value:
            continue
        try:
            raw = str(value).strip().lower()
            seconds = float(raw[:-1]) if raw.endswith("s") else float(raw)
            if not raw.endswith("s") and seconds > 1_000_000_000:
                seconds -= time.time()
            if seconds > 0:
                return max(1, min(int(seconds), 25 * 3600))
        except (TypeError, ValueError, OverflowError):
            continue
    return max(1, min(int(default), 25 * 3600))


def _groq_429_cooldown(response, detail):
    headers = getattr(response, "headers", {}) or {}
    text = (detail or "").lower()
    reset_headers = " ".join(str(v).lower() for k, v in headers.items() if "reset" in k.lower())
    daily = ("per day", "per-day", "daily limit", "daily quota", "tokens per day", "requests per day", "day limit")
    if any(term in text for term in daily) or "daily" in reset_headers:
        return _retry_after_seconds(headers, GROQ_KEY_COOLDOWN)
    # The word "quota" alone is ambiguous and should not trigger a 25-hour lockout.
    return _retry_after_seconds(headers, 60)


def _format_gemini_error(status_code, response):
    """Извлекает код, статус и причину ошибки из Gemini JSON."""
    try:
        body = response.json()
    except (ValueError, AttributeError):
        body = {}
    error = body.get("error", {}) if isinstance(body, dict) else {}
    message = str(error.get("message") or getattr(response, "text", "") or "нет подробностей").strip()
    api_status = str(error.get("status") or "").upper()
    lowered = (message + " " + api_status).lower()
    if status_code == 429 or any(x in lowered for x in ("quota", "rate limit", "resource_exhausted")):
        reason = "превышена квота или частотный лимит"
    elif status_code == 404 or any(x in lowered for x in ("model not found", "unknown model", "not found for api version")):
        reason = "модель не найдена или недоступна"
    elif status_code == 400:
        reason = "ошибка запроса или параметров"
    elif status_code in (401, 403):
        reason = "ключ недействителен или доступ запрещён"
    else:
        reason = "ошибка API"
    return f"Google AI Studio HTTP {status_code} ({reason}): {message[:900]}"


def gemini_messages_from_openai(messages):
    """OpenAI-сообщения → (systemInstruction, contents) для Gemini.

    Поддерживает нативный tool calling: assistant.tool_calls превращается
    в functionCall, а role="tool" — в functionResponse (иначе Gemini
    отверг бы второй запрос цикла агента).
    """
    contents_out=[]; system_text=""; call_names={}
    for message in messages or []:
        if not isinstance(message, dict):
            continue
        role=message.get("role","user"); content=message.get("content","")
        if isinstance(content,list):
            content="\n".join(str(p.get("text","")) for p in content if isinstance(p,dict) and p.get("text"))
        content=str(content or "")
        calls = message.get("tool_calls") if role == "assistant" else None
        if not content and not calls: continue
        if role=="system": system_text=(system_text+"\n\n"+content).strip()
        elif role=="assistant":
            parts=[]
            if content: parts.append({"text":content})
            for call in calls or []:
                if not isinstance(call, dict): continue
                function = call.get("function") if isinstance(call.get("function"), dict) else {}
                name = str(function.get("name") or "")
                if not name: continue
                args, _err = _parse_tool_arguments(function.get("arguments"))
                call_names[str(call.get("id") or name)] = name
                parts.append({"functionCall": {"name": name, "args": args}})
            if parts: contents_out.append({"role":"model","parts":parts})
        elif role=="tool":
            name = call_names.get(str(message.get("tool_call_id") or "")) or str(message.get("name") or "tool")
            contents_out.append({"role":"user","parts":[{"functionResponse":{
                "name": name, "response": {"result": content}}}]})
        else: contents_out.append({"role":"user","parts":[{"text":content}]})
    return system_text,contents_out


def gemini_tools_payload(tools):
    """OpenAI-совместимые tools → Gemini functionDeclarations."""
    declarations = []
    for tool in openai_tools_payload(tools):
        function = tool["function"]
        parameters = function.get("parameters") or {"type": "object", "properties": {}}
        if isinstance(parameters, dict):
            parameters = {k: v for k, v in parameters.items()
                          if k not in ("additionalProperties", "$schema")}
        declarations.append({"name": function["name"],
                             "description": function.get("description", ""),
                             "parameters": parameters})
    return [{"functionDeclarations": declarations}] if declarations else []

def gemini_request(model,payload,timeout=90,max_retries=2):
    key=(os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_AI_STUDIO_KEY") or "").strip()
    if not key: return None,"Google AI Studio: GEMINI_API_KEY не найден в .env"
    model_id=str(model or "").strip().removeprefix("models/")
    system_text,contents_out=gemini_messages_from_openai(payload.get("messages",[]))
    if not contents_out: return None,"Google AI Studio: отсутствует сообщение"
    body={"contents":contents_out,"generationConfig":{"temperature":float(payload.get("temperature",0.7)),"maxOutputTokens":min(int(payload.get("max_tokens",65536)),65536)}}
    if system_text: body["systemInstruction"]={"parts":[{"text":system_text}]}
    gemini_tools=gemini_tools_payload(payload.get("tools"))
    if gemini_tools:
        body["tools"]=gemini_tools
        body["toolConfig"]={"functionCallingConfig":{"mode":"AUTO"}}
    url=f"https://generativelanguage.googleapis.com/v1beta/models/{model_id}:generateContent"
    headers={"Content-Type":"application/json","x-goog-api-key":key}
    for attempt in range(max_retries):
        try:
            r=requests.post(url,json=body,headers=headers,timeout=timeout)
            if r.status_code == 429 and attempt < max_retries - 1:
                time.sleep(min(_retry_after_seconds(r.headers, 2 ** attempt), 30))
                continue
            if r.status_code >= 400:
                return None, _format_gemini_error(r.status_code, r)
            data = r.json()
            if not isinstance(data, dict):
                return None, "Google AI Studio вернул ответ неожиданной структуры"
            parts=[]; tool_calls=[]
            for candidate in data.get("candidates",[]):
                for part in (candidate.get("content") or {}).get("parts",[]):
                    if part.get("text"): parts.append(part["text"])
                    call = part.get("functionCall") if isinstance(part, dict) else None
                    if isinstance(call, dict) and call.get("name"):
                        tool_calls.append({"id": f"call_{len(tool_calls)}", "type": "function",
                                           "function": {"name": call["name"],
                                                        "arguments": json.dumps(call.get("args") or {}, ensure_ascii=False)}})
            if not parts and not tool_calls: return None,"Google AI Studio не вернул текст"
            message={"role":"assistant","content":"".join(parts)}
            if tool_calls: message["tool_calls"]=tool_calls
            return {"choices":[{"message":message}]},None
        except requests.exceptions.Timeout:
            if attempt<max_retries-1: continue
            return None,"Google AI Studio: таймаут"
        except requests.exceptions.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else "?"
            return None, f"Google AI Studio HTTP {status}: {exc.__class__.__name__}"
        except requests.exceptions.RequestException as exc:
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
                continue
            return None, f"Google AI Studio: {exc.__class__.__name__}"
        except (ValueError,TypeError): return None,"Google AI Studio вернул некорректный JSON"
    return None,"Google AI Studio: запрос не выполнен"

def gemini_stream_request(model,payload,timeout=90):
    """Открывает нативный Gemini SSE-поток и возвращает подробную ошибку API."""
    key=(os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_AI_STUDIO_KEY") or "").strip()
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
            return None, _format_gemini_error(r.status_code, r)
        r.encoding="utf-8"
        return r,None
    except requests.exceptions.Timeout:
        return None,"Google AI Studio: таймаут при подключении к streamGenerateContent"
    except requests.exceptions.RequestException as exc:
        return None,f"Google AI Studio: {exc}"

def groq_request_with_rotation(url, payload, headers, timeout=90, max_retries=3):
    """
    Запрос к Groq. Ротация выполняется только при 401/429:
    ошибки модели/параметров (400/403/404) не должны портить состояние ключей.
    """
    if "generativelanguage.googleapis.com" in url:
        return gemini_request(payload.get("model"), payload, timeout=timeout, max_retries=max_retries)
    if "api.groq.com" not in url:
        return openai_compatible_request(url, payload, headers, timeout=timeout, max_retries=max_retries)

    last_error = None
    max_attempts = max(1, min(max_retries, len(GROQ_KEYS_LIST())))
    if not GROQ_KEYS_LIST():
        return None, "Groq API ключ не найден. Добавьте GROQ_API_KEY в .env."

    for attempt in range(max_attempts):
        current_key = get_groq_key()
        if not current_key:
            return None, last_error or "Все Groq API ключи временно недоступны или помечены как неисправные."

        request_headers = dict(headers or {})
        request_headers["Authorization"] = f"Bearer {current_key}"
        request_headers.setdefault("Content-Type", "application/json")

        try:
            resp = requests.post(url, json=payload, headers=request_headers, timeout=timeout)
        except requests.exceptions.Timeout:
            last_error = "Таймаут запроса к Groq"
            if attempt < max_attempts - 1:
                time.sleep(1)
                continue
            return None, last_error
        except requests.exceptions.RequestException as exc:
            last_error = f"Ошибка соединения с Groq: {exc}"
            if attempt < max_attempts - 1:
                time.sleep(1)
                continue
            return None, last_error

        if resp.status_code == 401:
            last_error = "Groq HTTP 401: API-ключ отклонён; ключ временно отложен. После 3 последовательных 401 он будет отключён до перезапуска."
            mark_groq_key_exhausted(permanent=False, cooldown_seconds=60, key=current_key, auth_failure=True)
            continue

        if resp.status_code == 429:
            detail = (resp.text or "")[:400]
            last_error = f"Groq HTTP 429: превышен лимит запросов/токенов. {detail}".strip()
            cooldown = _groq_429_cooldown(resp, detail)
            mark_groq_key_exhausted(cooldown_seconds=cooldown, key=current_key)
            if attempt < max_attempts - 1:
                continue
            return None, last_error

        if resp.status_code >= 400:
            detail = (resp.text or "")[:600]
            return None, f"Groq HTTP {resp.status_code}: {detail}"

        try:
            data = resp.json()
            mark_groq_key_success(current_key)
            return data, None
        except ValueError:
            return None, "Groq вернул некорректный JSON"

    return None, last_error or "Не удалось выполнить запрос к Groq"


# ══════════════════════════════════════════════════════════════════
# ЕДИНЫЙ СЛОЙ ДОСТУПА К МОДЕЛЯМ
# Один вход для всех провайдеров: cloud, локальный llama-server и
# встроенная офлайн-модель. Раньше каждый маршрут дёргал transport
# вручную и ломался на Gemini (в URL провайдера плейсхолдер {model}).
# ══════════════════════════════════════════════════════════════════

REASONING_MODELS_HINT = ("gpt-oss", "deepseek-r1", "qwen-3", "glm-4.5", "o1", "o3")

# ── Нативный tool calling ────────────────────────────────────────────
# Провайдеры, которые принимают OpenAI-совместимый массив `tools` и
# возвращают `message.tool_calls`. Для Gemini (google_ai_studio) аргументы
# конвертируются в functionDeclarations внутри gemini_request.
NATIVE_TOOL_PROVIDERS = {"groq", "cerebras", "openrouter", "pollinations",
                         "openai_compatible", "google_ai_studio"}


def supports_native_tools(provider: str | None = None, model: str | None = None) -> bool:
    """Умеет ли провайдер/модель нативный tool calling.

    Отключается переменной окружения AI_NATIVE_TOOLS=0 (аварийный откат
    на старый JSON-протокол без правки кода).
    """
    if os.getenv("AI_NATIVE_TOOLS", "1") != "1":
        return False
    if not provider:
        provider = resolve_target()[0]
    if provider == "local_demo":
        return False
    return provider in NATIVE_TOOL_PROVIDERS


def _parse_tool_arguments(raw):
    """Аргументы tool_call → (dict, ошибка). Битый JSON не роняет запрос."""
    if raw is None or raw == "":
        return {}, ""
    if isinstance(raw, dict):
        return raw, ""
    text = str(raw).strip()
    try:
        data = json.loads(text)
    except (ValueError, TypeError) as exc:
        return {}, f"аргументы инструмента не являются корректным JSON: {exc}"
    if isinstance(data, dict):
        return data, ""
    return {"value": data}, ""


def normalize_tool_calls(raw_calls):
    """Приводит tool_calls провайдера к единому виду.

    Возвращает список словарей:
        {"id", "name", "arguments", "raw_arguments", "arguments_error"}
    Ошибка разбора аргументов не выбрасывается наружу: агент сообщает
    модели, что аргументы битые, и просит повторить вызов.
    """
    calls = []
    for index, call in enumerate(raw_calls or []):
        if not isinstance(call, dict):
            continue
        function = call.get("function") if isinstance(call.get("function"), dict) else {}
        name = str(function.get("name") or call.get("name") or "").strip()
        if not name:
            continue
        raw_args = function.get("arguments", call.get("arguments", {}))
        arguments, arguments_error = _parse_tool_arguments(raw_args)
        calls.append({
            "id": str(call.get("id") or f"call_{index}"),
            "name": name,
            "arguments": arguments,
            "raw_arguments": raw_args if isinstance(raw_args, str)
            else json.dumps(raw_args, ensure_ascii=False),
            "arguments_error": arguments_error,
        })
    return calls


def openai_tools_payload(tools):
    """Оставляет только корректные OpenAI-совместимые описания инструментов."""
    payload = []
    for tool in tools or []:
        if isinstance(tool, dict) and tool.get("type") == "function" and isinstance(tool.get("function"), dict):
            payload.append(tool)
        elif isinstance(tool, dict) and tool.get("name"):
            # Разрешаем «плоский» формат: {name, description, parameters}.
            payload.append({"type": "function", "function": {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": tool.get("parameters") or {"type": "object", "properties": {}},
            }})
    return payload


def _clean_reasoning_tail(value):
    """Удаляет распространённые служебные маркеры, сохраняя обычный текст."""
    text = str(value or "")
    text = re.sub(r"(?is)\[/?(?:THINK|REASONING|ANALYSIS|INTERNAL)\]", "", text)
    text = re.sub(r"(?is)</?(?:thought|think|analysis|reasoning|internal)\b[^>]*>", "", text)
    text = re.sub(r"(?is)<\|(?:think|analysis|reasoning)[^|]*\|>", "", text)
    return text.strip()


def provider_headers(provider: str) -> dict:
    """Заголовки для провайдера (ключ подставляется на сервере, не в браузере)."""
    import config
    headers = dict(PROVIDERS_HEADERS.get(provider) or config.PROVIDERS.get(provider, {}).get("headers") or {})
    if provider == "groq":
        key = get_groq_key()
        if key:
            headers["Authorization"] = f"Bearer {key}"
    headers.setdefault("Content-Type", "application/json")
    return headers


# Заголовки из config.PROVIDERS собираются один раз, но ключ Groq — всегда свежий.
PROVIDERS_HEADERS: dict = {}


def supports_reasoning(model: str) -> bool:
    model_id = str(model or "").lower()
    return any(hint in model_id for hint in REASONING_MODELS_HINT)


def resolve_target(provider: str = None, model: str = None):
    """(provider, model, provider_cfg) — с откатом на рабочий провайдер."""
    import config
    provider = provider or config.current_provider
    model = model or config.current_model
    cfg = config.PROVIDERS.get(provider) or config.PROVIDERS["local_demo"]
    if not config.has_credentials(provider):
        fallback = config.first_available_provider()
        if fallback != provider:
            models = config.PROVIDERS.get(fallback, {}).get("models") or []
            return fallback, (models[0]["id"] if models else "nova-local-1"), config.PROVIDERS[fallback]
    return provider, model, cfg


def chat_completion(messages, provider=None, model=None, system=None,
                    temperature=0.7, max_tokens=None, timeout=90, extra=None,
                    tools=None, tool_choice=None):
    """
    Синхронный запрос к текущей модели.
    Возвращает (text, meta) или (None, error_string).

    `tools` — OpenAI-совместимый список инструментов. Если провайдер их
    поддерживает, ответ может не содержать текста, но содержать tool_calls:
    тогда meta["tool_calls"] — разобранные вызовы (см. normalize_tool_calls).
    """
    provider, model, cfg = resolve_target(provider, model)
    tools_payload = openai_tools_payload(tools) if (tools and supports_native_tools(provider, model)) else []

    if provider == "local_demo":
        import local_llm
        payload_messages = ([{"role": "system", "content": system}] if system else []) + list(messages or [])
        text = local_llm.complete(payload_messages)
        return text, {"provider": provider, "model": local_llm.MODEL_ID,
                      "model_name": local_llm.MODEL_NAME, "offline": True,
                      "tool_calls": [], "native_tools": False}

    payload = {
        "model": model,
        "messages": ([{"role": "system", "content": system}] if system else []) + list(messages or []),
        "temperature": temperature,
        "max_tokens": int(max_tokens or cfg.get("max_tokens", 8192)),
    }
    if tools_payload:
        payload["tools"] = tools_payload
        payload["tool_choice"] = tool_choice or "auto"
    if extra:
        payload.update(extra)

    if provider == "google_ai_studio":
        data, error = gemini_request(model, payload, timeout=timeout)
    elif provider == "groq":
        data, error = groq_request_with_rotation(cfg["url"], payload, provider_headers("groq"), timeout=timeout)
    else:
        data, error = openai_compatible_request(
            cfg["url"], payload, provider_headers(provider), timeout=timeout
        )

    if error:
        return None, error
    try:
        message = (data.get("choices") or [{}])[0].get("message") or {}
        text = message.get("content") or ""
        if isinstance(text, list):
            text = "".join(str(p.get("text", "")) for p in text if isinstance(p, dict))
        text = str(text)
        tool_calls = normalize_tool_calls(message.get("tool_calls"))
        thought = message.get("reasoning") or message.get("reasoning_content") or ""
        thought = thought if isinstance(thought, str) else ""
        if not text.strip() and thought.strip() and not tool_calls:
            text = thought
    except (AttributeError, IndexError, TypeError):
        logger.warning("Неожиданный формат ответа (provider=%s, model=%s)", provider, model, exc_info=True)
        return None, "Провайдер вернул ответ в неожиданном формате"
    if not text.strip() and not tool_calls:
        logger.warning("Пустые content и reasoning (provider=%s, model=%s)", provider, model)
        return None, "Провайдер вернул пустой ответ (content и reasoning отсутствуют)"
    return text, {"provider": provider, "model": model, "offline": False,
                  "tool_calls": tool_calls,
                  "native_tools": bool(tools_payload),
                  # usage пригодится на этапе счётчика токенов
                  "usage": data.get("usage") if isinstance(data, dict) else None}


def chat_stream(messages, provider=None, model=None, system=None, temperature=0.7,
                max_tokens=None, timeout=90, reasoning=False, reasoning_effort="low"):
    """
    Потоковый ответ. Генератор кортежей (kind, text), где kind:
      "reasoning" — ход мыслей модели (если провайдер его отдаёт),
      "token"     — текст ответа,
      "error"     — ошибка (текст),
      "done"      — конец потока.

    Если модель не умеет отдавать рассуждения, а режим включён — сначала
    выполняется короткий «план рассуждений», затем основной ответ.
    """
    provider, model, cfg = resolve_target(provider, model)
    want_reasoning = bool(reasoning)

    if provider == "local_demo":
        import local_llm
        payload_messages = ([{"role": "system", "content": system}] if system else []) + list(messages or [])
        for kind, chunk in local_llm.stream(payload_messages, reasoning=want_reasoning):
            yield kind, chunk
        yield "done", ""
        return

    payload = {
        "model": model,
        "messages": ([{"role": "system", "content": system}] if system else []) + list(messages or []),
        "temperature": temperature,
        "max_tokens": int(max_tokens or cfg.get("max_tokens", 8192)),
        "stream": True,
    }
    # Groq: параметры рассуждений по документации — reasoning_effort (строка)
    # и reasoning_format. Раньше уходило поле "reasoning": {"effort": ...} в
    # стиле OpenRouter: Groq мог ответить 400, и пользователь получал
    # «Не удалось получить ответ модели».
    if provider == "groq" and "gpt-oss" in str(model or "").lower():
        payload["reasoning_effort"] = reasoning_effort if want_reasoning else "low"
        if not want_reasoning:
            payload["reasoning_format"] = "hidden"

    # Предварительный «план рассуждений» для моделей без нативного reasoning.
    prepass_needed = want_reasoning and not (provider == "groq" and supports_reasoning(model))
    produced_reasoning = False

    if prepass_needed:
        plan_messages = list(messages or []) + [{
            "role": "user",
            "content": (
                "Перед ответом кратко (3-6 пунктов) распиши ход рассуждений: "
                "что важно учесть, какие есть варианты, что проверить. Без вступлений."
            ),
        }]
        plan_payload = dict(payload)
        plan_payload["messages"] = ([{"role": "system", "content": system}] if system else []) + plan_messages
        plan_payload["max_tokens"] = 400
        plan_payload["temperature"] = 0.4
        for kind, chunk in _raw_stream(provider, model, plan_payload, cfg, timeout):
            if kind == "token":
                produced_reasoning = True
                yield "reasoning", chunk
            elif kind == "error":
                # План не получился — не роняем основной ответ.
                logger.warning("Reasoning prepass failed: %s", chunk)
                break

    tokens_seen = False
    reasoning_tail = []
    for kind, chunk in _raw_stream(provider, model, payload, cfg, timeout):
        if kind == "reasoning":
            produced_reasoning = True
            reasoning_tail.append(chunk)
            if len(reasoning_tail) > 400:
                reasoning_tail.pop(0)
        elif kind == "token":
            tokens_seen = True
            reasoning_tail = []
        if kind == "error":
            if tokens_seen:
                yield "error", f"Ответ получен не полностью: поток оборвался после части текста. {chunk}"
            else:
                yield "error", chunk
            continue
        yield kind, chunk

    # Не показываем пользователю служебные теги из хвоста reasoning.
    if not tokens_seen and reasoning_tail:
        tail = _clean_reasoning_tail("".join(reasoning_tail))
        if tail:
            yield "token", tail

    if want_reasoning and not produced_reasoning:
        yield "reasoning", (
            "Модель не передала внутренний ход рассуждений — "
            "сразу сформировала ответ. Включите модель с поддержкой reasoning "
            "(например, gpt-oss-120b), чтобы видеть развёрнутую цепочку мыслей."
        )
    yield "done", ""


def _raw_stream(provider, model, payload, cfg, timeout):
    """Низкоуровневый поток токенов конкретного провайдера."""
    if provider == "google_ai_studio":
        upstream, error = gemini_stream_request(model, payload, timeout=timeout)
        if upstream is None:
            data, fallback_error = gemini_request(model, payload, timeout=timeout)
            if fallback_error:
                yield "error", f"{error}. Fallback: {fallback_error}"
                return
            choices = data.get("choices") if isinstance(data, dict) else None
            first = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
            message = first.get("message") if isinstance(first.get("message"), dict) else {}
            text = message.get("content") or ""
            if isinstance(text, list):
                text = "".join(str(part.get("text", "")) for part in text if isinstance(part, dict))
            if isinstance(text, str) and text.strip():
                yield "token", text
            else:
                logger.warning("Gemini fallback вернул пустой или неожиданный ответ")
            return
        try:
            for raw in upstream.iter_lines(decode_unicode=True):
                if not raw:
                    continue
                line = raw.strip()
                if line.startswith("data:"):
                    line = line[5:].strip()
                if line == "[DONE]":
                    break
                try:
                    chunk = json.loads(line)
                except (TypeError, ValueError):
                    continue
                for candidate in chunk.get("candidates", []):
                    for part in (candidate.get("content") or {}).get("parts", []):
                        if part.get("thought"):
                            if part.get("text"):
                                yield "reasoning", part["text"]
                            continue
                        if part.get("text"):
                            yield "token", part["text"]
        finally:
            upstream.close()
        return

    headers = provider_headers(provider)
    if provider == "groq":
        attempts = max(1, min(len(GROQ_KEYS_LIST()), 4))
    else:
        attempts = 1

    last_error = None
    for attempt in range(attempts):
        if provider == "groq":
            current_key = get_groq_key()
            if not current_key:
                yield "error", last_error or "Все ключи Groq временно недоступны. Проверьте GROQ_API_KEY и лимиты."
                return
            headers = dict(headers or {})
            headers["Authorization"] = f"Bearer {current_key}"

        try:
            # (подключение, ожидание очередной порции данных) — долгий первый
            # токен у reasoning-моделей не должен обрываться по общему таймауту
            response = requests.post(cfg["url"], json=payload, headers=headers,
                                     timeout=(min(30, timeout), timeout), stream=True)
        except requests.exceptions.RequestException as exc:
            last_error = f"Ошибка соединения с {provider}: {exc.__class__.__name__}"
            if attempt < attempts - 1:
                time.sleep(1)
                continue
            yield "error", last_error
            return

        if provider == "groq" and response.status_code in (401, 429):
            status_code = response.status_code
            detail = (response.text or "")[:400]
            response.close()
            if status_code == 401:
                last_error = "Groq HTTP 401: API-ключ отклонён; временная блокировка, отключение после 3 последовательных 401."
                mark_groq_key_exhausted(permanent=False, cooldown_seconds=60, key=current_key, auth_failure=True)
            else:
                last_error = f"Groq HTTP 429: превышен лимит запросов/токенов. {detail}".strip()
                cooldown = _groq_429_cooldown(response, detail)
                mark_groq_key_exhausted(cooldown_seconds=cooldown, key=current_key)
            continue

        if response.status_code >= 400:
            detail = (response.text or "")[:600]
            response.close()
            last_error = f"{provider} HTTP {response.status_code}: {detail}"
            # Ошибки модели/параметров не относятся к состоянию API-ключа.
            yield "error", last_error
            return

        if provider == "groq":
            mark_groq_key_success(current_key)
        response.encoding = "utf-8"
        try:
            for raw in response.iter_lines(decode_unicode=True):
                if not raw:
                    continue
                line = raw.strip()
                if line.startswith("data:"):
                    line = line[5:].strip()
                if line == "[DONE]":
                    break
                try:
                    chunk = json.loads(line)
                except (TypeError, ValueError):
                    continue
                choice = (chunk.get("choices") or [{}])[0]
                delta = choice.get("delta") or {}
                thought = delta.get("reasoning") or delta.get("reasoning_content") or ""
                if thought:
                    yield "reasoning", thought
                token = delta.get("content") or choice.get("text") or ""
                if token:
                    yield "token", token
        except requests.exceptions.RequestException as exc:
            # Соединение оборвалось посреди потока. Молча рвать генератор нельзя:
            # всё, что уже пришло, показываем пользователю, и помечаем поток
            # оборванным — вызывающий код сам решит, дособирать ответ или нет.
            yield "error", f"Соединение с моделью прервано: {exc.__class__.__name__}"
            return
        finally:
            response.close()
        return

    yield "error", last_error or "Не удалось получить поток от провайдера"


def GROQ_KEYS_LIST():
    from groq_rotation import GROQ_KEYS
    return GROQ_KEYS


import re

# ---------- Метки провайдеров ----------
MODEL_ERRORS: dict = {}
PROVIDER_LABELS = {
    "groq":             "Groq",
    "cerebras":         "Cerebras",
    "openrouter":       "OpenRouter",
    "google_ai_studio": "Google AI Studio",
    "openai_compatible":"OpenAI-compatible / Local",
    "pollinations":       "Pollinations.ai",
    "local_demo":         "Локальная демо-модель (офлайн)",
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
    if provider == "pollinations":
        key = (os.getenv("POLLINATIONS_API_KEY") or os.getenv("POLLINATIONS_KEY") or os.getenv("POLLINATIONS_TOKEN") or "").strip()
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        return headers
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
    """Возвращает /models endpoints для llama-server и совместимых API.

    FIX: раньше к базе вида http://host:8080/v1 добавлялся ещё и суффикс
    /v1/models, из-за чего каталог искался по несуществующему /v1/v1/models.
    """
    from config import PROVIDERS
    configured = (
        os.getenv("OPENAI_COMPATIBLE_URL") or os.getenv("OPENAI_BASE_URL")
        or PROVIDERS.get("openai_compatible", {}).get("url")
        or "http://127.0.0.1:8080/v1/chat/completions"
    ).strip().rstrip("/")
    if configured.endswith("/chat/completions"):
        base = configured[:-len("/chat/completions")].rstrip("/")
    else:
        base = configured
    lowered = base.lower()
    if ":11434" in lowered:
        # Strip optional /v1 suffix before constructing Ollama native endpoints.
        root = base[:-len("/v1")].rstrip("/") if base.lower().endswith("/v1") else base
        candidates = [root + "/api/tags", root + "/v1/models"]
    elif lowered.endswith("/api"):
        root = base[:-len("/api")].rstrip("/")
        candidates = [base + "/tags", root + "/v1/models"]
    elif base.endswith("/v1"):
        candidates = [base + "/models"]
    else:
        candidates = [base + "/models", base + "/v1/models"]
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
    elif provider == "pollinations":
        # Публичный каталог Pollinations не требует ключа.
        urls = ["https://gen.pollinations.ai/v1/models"]
    elif provider == "openai_compatible":
        # llama-server, Ollama, LM Studio, vLLM, LocalAI и другие
        # OpenAI-compatible runtimes обычно отдают каталог через /v1/models.
        urls = _local_catalog_urls()
    elif provider == "local_demo":
        import local_llm
        models = local_llm.models_catalog()
        MODEL_CACHE[provider] = models
        MODEL_ERRORS.pop(provider, None)
        return models
    else:
        return [normalize_model(m, provider) for m in PROVIDERS[provider]["models"]]

    headers = provider_api_headers(provider)
    if headers is None:
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
                raw = resp.json().get("data", resp.json().get("models", []))
                # Groq can include inactive/retired IDs in some catalog responses.
                # Do not offer them in the model picker.
                models = [
                    normalize_model(m, provider)
                    for m in raw
                    if (m.get("id") or m.get("name"))
                    and (provider != "groq" or m.get("active", True) is not False)
                ]

            if models:
                MODEL_CACHE[provider] = models
                MODEL_ERRORS.pop(provider, None)
                return models

            last_error = f"{url}: каталог пуст"
        except (requests.RequestException, ValueError, TypeError) as exc:
            last_error = f"{url}: {exc}"
            if provider != "openai_compatible":
                break

    logger.warning("Model catalog error (%s): %s", provider, last_error)
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
    """Единый каталог image/audio/video. Pollinations берётся из live API."""
    try:
        import media_generation as mg
        models = mg.media_catalog(
            media_type=media_type,
            provider=provider,
            include_paid=True,
            include_unknown=True,
            include_trial=True,
            refresh=force,
        )
        return models
    except Exception as exc:
        MODEL_ERRORS["media"] = str(exc)
        return []


def provider_status():
    """Какие провайдеры реально подключены (есть ключ или ключ не нужен)."""
    from config import PROVIDERS, has_credentials
    return {provider: has_credentials(provider) for provider in PROVIDERS}
