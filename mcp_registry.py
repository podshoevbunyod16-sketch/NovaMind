"""MCP connection registry and bounded clients for HTTP, legacy HTTP/SSE and STDIO.

Remote MCP servers are untrusted. Discovery errors are isolated, result schemas
are checked, logs redact credentials, and tools/call is retried only when the
server advertises a read-only/idempotent tool.
"""
from __future__ import annotations

import atexit
import copy
import json
import logging
import os
import re
import select
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import requests

log = logging.getLogger("novamind.mcp")

def _env_float(name, default, low, high):
    try: return min(high, max(low, float(os.getenv(name, default))))
    except (TypeError, ValueError): return default

def _env_int(name, default, low, high):
    try: return min(high, max(low, int(os.getenv(name, default))))
    except (TypeError, ValueError): return default

TIMEOUT = _env_float("MCP_TIMEOUT", 12.0, .5, 120.0)
MAX_RETRIES = _env_int("MCP_MAX_RETRIES", 2, 0, 4)
MAX_FRAME_BYTES = 8 * 1024 * 1024
MAX_DISCOVERY_PAGES = 200
TOOL_CACHE_SECONDS = _env_float("MCP_TOOL_CACHE_SECONDS", 30.0, 0.0, 600.0)
ROOT = Path(__file__).resolve().parent
LEGACY_STORE = ROOT / "mcp_connections.json"
STORE = Path(os.getenv("MCP_CONNECTIONS_FILE", str(LEGACY_STORE)))
PROTOCOL_VERSION = "2026-07-28"
LEGACY_PROTOCOL_VERSION = "2025-06-18"
LEGACY_PROTOCOL_FALLBACKS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
CLIENT_INFO = {"name": "NovaMind", "version": "1.0"}
META_PROTOCOL_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CLIENT_INFO = "io.modelcontextprotocol/clientInfo"
META_CLIENT_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"
CATALOG = [
    {"name": "MCP Streamable HTTP", "transport": "streamable_http", "description": "Современный MCP по HTTP POST/SSE.", "auth": ["none", "api_key", "bearer", "oauth", "basic"]},
    {"name": "MCP HTTP/SSE (legacy)", "transport": "http_sse", "description": "Совместимость с legacy MCP SSE endpoint.", "auth": ["none", "api_key", "bearer", "oauth", "basic"]},
    {"name": "MCP STDIO", "transport": "stdio", "description": "Локальный MCP-процесс, запускаемый без shell.", "auth": ["env"]},
]

_STORE_LOCK = threading.RLock()
_SERVER_LOCKS_LOCK = threading.Lock()
_SERVER_LOCKS: dict[str, threading.RLock] = {}
_TOOL_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_FAILURE_UNTIL: dict[str, float] = {}
_STDIO_SESSIONS: dict[str, Any] = {}
_STDIO_SESSIONS_LOCK = threading.RLock()
_REQUEST_SEMAPHORE = threading.BoundedSemaphore(_env_int("MCP_MAX_CONCURRENT", 4, 1, 16))

class MCPError(RuntimeError):
    def __init__(self, message, *, status_code=None, retryable=False, connection_failure=False, retry_after=None):
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable
        self.connection_failure = connection_failure
        self.retry_after = retry_after

def _server_lock(server_id):
    with _SERVER_LOCKS_LOCK:
        return _SERVER_LOCKS.setdefault(server_id, threading.RLock())

def _read():
    with _STORE_LOCK:
        # Keep the preexisting root-level store readable across upgrades; the
        # data/ path is also accepted for installations from the interim build.
        paths = dict.fromkeys((STORE, LEGACY_STORE, ROOT / "data" / "mcp_connections.json"))
        for path in paths:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict) and data:
                    return data
            except (OSError, ValueError, TypeError):
                continue
        try:
            data = json.loads(os.getenv("MCP_CONNECTIONS_JSON", "{}"))
            return data if isinstance(data, dict) else {}
        except (ValueError, TypeError):
            return {}

def _write(data):
    with _STORE_LOCK:
        STORE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STORE.with_name(STORE.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        try: os.chmod(tmp, 0o600)
        except OSError: pass
        tmp.replace(STORE)
        try: os.chmod(STORE, 0o600)
        except OSError: pass

def _normalize_transport(value):
    value = str(value or "streamable_http").strip().lower().replace("-", "_")
    value = {"http": "streamable_http", "https": "streamable_http", "streamable": "streamable_http",
             "sse": "http_sse", "http/sse": "http_sse", "legacy_sse": "http_sse"}.get(value, value)
    if value not in {"streamable_http", "http_sse", "stdio"}: raise ValueError("Неизвестный транспорт MCP")
    return value

def _safe_url(url):
    p = urlsplit(url)
    if p.scheme not in {"http", "https"} or not p.hostname: raise ValueError("Для HTTP MCP укажите полный URL с http:// или https://")
    if p.username or p.password or p.fragment: raise ValueError("Уберите логин, пароль и fragment из URL MCP; задайте авторизацию отдельно")
    return url

def _safe_public_url(url):
    try:
        p = urlsplit(str(url or ""))
        query = [(k, "[скрыто]" if re.search(r"token|secret|key|password|auth", k, re.I) else v)
                 for k, v in parse_qsl(p.query, keep_blank_values=True)]
        host = p.hostname or ""
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        if p.port:
            host = f"{host}:{p.port}"
        return urlunsplit((p.scheme, host, p.path, urlencode(query), ""))
    except Exception: return ""

def _connection(server_id):
    item = _read().get(server_id)
    if not isinstance(item, dict): raise ValueError("MCP-сервер не найден")
    return item

def _safe_error(message, item=None):
    text = str(message or "Ошибка MCP")
    if item:
        for key in ("token", "api_key", "password", "client_secret"):
            secret = item.get(key)
            if isinstance(secret, str) and secret: text = text.replace(secret, "[скрыто]")
        for secret in (item.get("env") or {}).values() if isinstance(item.get("env"), dict) else []:
            if isinstance(secret, str) and len(secret) >= 4: text = text.replace(secret, "[скрыто]")
    text = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+", r"\1[скрыто]", text)
    text = re.sub(r"(?i)(token|api[_-]?key|secret|password|authorization)(\s*[:=]\s*)[^\s,;]+", r"\1\2[скрыто]", text)
    text = re.sub(r"https?://[^\s\]\[()]+", lambda m: _safe_public_url(m.group()), text)
    return text[:700]

def _set_status(server_id, **updates):
    with _STORE_LOCK:
        data = _read(); item = data.get(server_id)
        if not isinstance(item, dict): return
        item.update(updates); data[server_id] = item; _write(data)

def _mark_failure(server_id, exc):
    try:
        item = _connection(server_id); message = _safe_error(exc, item)
        _set_status(server_id, connected=False, initialized=False, last_error=message, last_checked=int(time.time()))
        _TOOL_CACHE.pop(server_id, None); _FAILURE_UNTIL[server_id] = time.monotonic() + 5.0
        _close_stdio_session(server_id)
        log.warning("MCP server=%s transport=%s status=%s error=%s", server_id,
                    item.get("transport", "streamable_http"), getattr(exc, "status_code", None), message)
    except Exception: log.warning("MCP server=%s failed (%s)", server_id, type(exc).__name__)

def _mark_success(server_id):
    _FAILURE_UNTIL.pop(server_id, None)
    _set_status(server_id, connected=True, initialized=True, last_error="", last_checked=int(time.time()))

def catalog(): return copy.deepcopy(CATALOG)

def public_connections():
    result = []
    for sid, item in _read().items():
        if not isinstance(item, dict): continue
        try: transport = _normalize_transport(item.get("transport", "streamable_http"))
        except ValueError: transport = "unknown"
        result.append({"id": sid, "name": item.get("name", sid),
            "url": _safe_public_url(item.get("url", "")) if transport != "stdio" else "",
            "transport": transport, "auth": item.get("auth", "none"),
            "connected": bool(item.get("connected")),
            "last_error": _safe_error(item.get("last_error", ""), item),
            "last_checked": item.get("last_checked")})
    return result

def _server_id(name):
    slug = re.sub(r"[^a-z0-9_-]+", "-", name.lower()).strip("-_")
    return (slug or "mcp")[:50]

def save_connection(payload):
    payload = payload if isinstance(payload, dict) else {}
    name = str(payload.get("name") or "MCP server").strip()[:80] or "MCP server"
    transport = _normalize_transport(payload.get("transport", "streamable_http"))
    auth = str(payload.get("auth", "none") or "none").strip().lower()
    if transport == "stdio":
        if auth not in {"none", "env"}: raise ValueError("Для STDIO передавайте секреты через переменные env")
        auth = "env" if payload.get("env") else "none"
        command_value = payload.get("command")
        if isinstance(command_value, (list, tuple)):
            if not command_value or not all(isinstance(x, str) and x for x in command_value): raise ValueError("Для STDIO укажите исполняемую команду")
            command, args = command_value[0], list(command_value[1:]) + list(payload.get("args") or [])
        else: command, args = str(command_value or "").strip(), payload.get("args", [])
        if not command or "\x00" in command: raise ValueError("Для STDIO укажите исполняемую команду")
        if not isinstance(args, list) or len(args) > 64 or any(not isinstance(x, str) or "\x00" in x or len(x) > 2048 for x in args): raise ValueError("Аргументы STDIO должны быть списком строк (до 64 элементов)")
        env = payload.get("env", {})
        if not isinstance(env, dict) or len(env) > 100 or any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(k)) or not isinstance(v, str) or "\x00" in v for k, v in env.items()): raise ValueError("Некорректные переменные окружения STDIO")
        cwd = str(payload.get("cwd") or "").strip()
        if cwd and not Path(cwd).is_dir(): raise ValueError("Рабочая папка STDIO не найдена")
        url = ""
    else:
        if auth not in {"none", "api_key", "bearer", "oauth", "google_oauth", "basic"}: raise ValueError("Неизвестный способ авторизации MCP")
        url = _safe_url(str(payload.get("url") or "").strip()); command, args, env, cwd = "", [], {}, ""
    with _STORE_LOCK:
        data = _read(); sid = _server_id(name); base = sid; suffix = 2
        while sid in data and data[sid].get("url") != url:
            sid = f"{base[:44]}-{suffix}"; suffix += 1
        previous = data.get(sid, {}) if isinstance(data.get(sid), dict) else {}
        item = {"id": sid, "name": name, "url": url, "transport": transport, "auth": auth,
                "connected": False, "initialized": False, "last_error": "", "last_checked": None}
        if transport == "stdio": item.update(command=command, args=list(args), env=dict(env), cwd=cwd)
        else:
            for key in ("api_key", "token", "username", "password", "client_id", "client_secret", "token_url"):
                value = payload.get(key)
                if value not in (None, ""): item[key] = str(value)
                elif previous.get("auth") == auth and previous.get(key): item[key] = previous[key]
        data[sid] = item; _write(data)
        _TOOL_CACHE.pop(sid, None); _FAILURE_UNTIL.pop(sid, None)
    _close_stdio_session(sid)
    return sid

def remove_connection(server_id):
    with _STORE_LOCK:
        data = _read(); data.pop(server_id, None); _write(data)
        _TOOL_CACHE.pop(server_id, None); _FAILURE_UNTIL.pop(server_id, None)
    _close_stdio_session(server_id)
    log.info("MCP server=%s removed", server_id)

def _header_safe(value):
    text = str(value)
    sentinel = text.startswith("=?base64?") and text.endswith("?=")
    safe = all((" " <= ch <= "~") or ch == "\t" for ch in text) and not text.startswith((" ", "\t")) and not text.endswith((" ", "\t"))
    if safe and not sentinel:
        return text
    import base64
    return "=?base64?" + base64.b64encode(text.encode("utf-8")).decode("ascii") + "?="

def _header_schema_error(schema, seen=None):
    seen = seen if seen is not None else set()
    if not isinstance(schema, dict):
        return None
    properties = schema.get("properties", {})
    if not isinstance(properties, dict):
        return None
    for child in properties.values():
        if not isinstance(child, dict):
            continue
        name = child.get("x-mcp-header")
        if name is not None:
            kind = child.get("type")
            if not isinstance(name, str) or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name):
                return "неверное имя x-mcp-header"
            if name.lower() in seen:
                return "повторяющийся x-mcp-header"
            if kind not in {"string", "integer", "boolean"}:
                return "x-mcp-header разрешён только для string/integer/boolean"
            seen.add(name.lower())
        nested = _header_schema_error(child, seen)
        if nested:
            return nested
    return None

def _walk_header_params(value, schema, result=None):
    result = result if result is not None else []
    if not isinstance(value, dict) or not isinstance(schema, dict):
        return result
    properties = schema.get("properties", {})
    if not isinstance(properties, dict):
        return result
    for key, child_schema in properties.items():
        if not isinstance(child_schema, dict) or key not in value:
            continue
        header_name = child_schema.get("x-mcp-header")
        if isinstance(header_name, str) and isinstance(value[key], (str, int, bool)) and not isinstance(value[key], float):
            text = str(value[key]).lower() if isinstance(value[key], bool) else str(value[key])
            result.append((header_name, text))
        elif isinstance(child_schema.get("properties"), dict):
            _walk_header_params(value[key], child_schema, result)
    return result

def _headers(item, payload=None, tool_schema=None):
    modern = item.get("protocol_mode") == "modern"
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
               "MCP-Protocol-Version": str(item.get("protocol_version") or (PROTOCOL_VERSION if modern else LEGACY_PROTOCOL_VERSION))}
    if item.get("session_id") and not modern:
        headers["Mcp-Session-Id"] = str(item["session_id"])
    if modern and isinstance(payload, dict):
        headers["Mcp-Method"] = str(payload.get("method") or "")
        params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
        name = params.get("name")
        if name is None and payload.get("method") == "resources/read":
            name = params.get("uri")
        if name is not None:
            headers["Mcp-Name"] = _header_safe(name)
        if tool_schema and payload.get("method") == "tools/call":
            for field, value in _walk_header_params(params.get("arguments") or {}, tool_schema):
                if re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", field):
                    headers["Mcp-Param-" + field] = _header_safe(value)
    auth = item.get("auth")
    if auth == "api_key" and item.get("api_key"):
        headers["X-API-Key"] = str(item["api_key"])
    elif auth in {"bearer", "oauth", "google_oauth"} and item.get("token"):
        headers["Authorization"] = f"Bearer {item['token']}"
    elif auth == "basic" and item.get("username") is not None:
        import base64
        headers["Authorization"] = "Basic " + base64.b64encode(f"{item.get('username','')}:{item.get('password','')}".encode()).decode()
    return headers

def _modernize(payload, version):
    message = copy.deepcopy(payload)
    params = message.get("params")
    if not isinstance(params, dict):
        params = {}
        message["params"] = params
    meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
    meta.update({META_PROTOCOL_VERSION: version, META_CLIENT_CAPABILITIES: {}, META_CLIENT_INFO: CLIENT_INFO})
    params["_meta"] = meta
    return message

def _rpc(method, params=None, request_id=None):
    obj = {"jsonrpc": "2.0", "method": method}
    if request_id is not None: obj["id"] = request_id
    if params is not None: obj["params"] = params
    return obj

def _new_id(): return time.time_ns() % 2_000_000_000

def _response_body(response, expected_id=None):
    text = getattr(response, "text", "") or ""
    if not text.strip(): return {}
    ctype = str(getattr(response, "headers", {}).get("Content-Type", "")).lower()
    if "text/event-stream" in ctype or text.lstrip().startswith(("event:", "data:")):
        messages, data_lines = [], []
        for line in text.splitlines() + [""]:
            if line.startswith("data:"): data_lines.append(line[5:].lstrip())
            elif not line.strip() and data_lines:
                try: messages.append(json.loads("\n".join(data_lines)))
                except ValueError: pass
                data_lines.clear()
        for message in messages:
            if isinstance(message, dict) and (expected_id is None or message.get("id") == expected_id): return message
        if messages and expected_id is None: return messages[-1]
        raise MCPError("MCP SSE-ответ не содержит ожидаемый JSON-RPC id", connection_failure=True)
    try: data = response.json()
    except (ValueError, AttributeError) as exc: raise MCPError("MCP вернул некорректный JSON", connection_failure=True) from exc
    if not isinstance(data, dict): raise MCPError("MCP вернул JSON не в формате объекта", connection_failure=True)
    return data

def _response_status(response, item):
    status = int(getattr(response, "status_code", 0) or 0)
    if status < 400: return None
    if status in {400, 404, 405}:
        try:
            body = response.json()
            if isinstance(body, dict) and isinstance(body.get("error"), dict):
                return body
        except (ValueError, AttributeError):
            pass
    raw = getattr(response, "text", "") or f"HTTP {status}"
    body = _safe_error(raw, item)
    limited = status == 403 and bool(re.search(r"rate.?limit|too many requests|secondary rate", raw, re.I))
    retryable = status in {429, 502, 503, 504} or limited
    try: retry_after = min(3.0, max(0.0, float(getattr(response, "headers", {}).get("Retry-After"))))
    except (TypeError, ValueError): retry_after = None
    raise MCPError(f"MCP HTTP {status}: {body}", status_code=status,
                   retryable=retryable, connection_failure=True, retry_after=retry_after)

def _http_exchange(server_id, item, payload, tool_schema=None):
    response = requests.post(item["url"], json=payload, headers=_headers(item, payload, tool_schema), timeout=TIMEOUT, allow_redirects=False)
    headers = getattr(response, "headers", {})
    if headers.get("Mcp-Session-Id") and item.get("protocol_mode") != "modern":
        _set_status(server_id, session_id=str(headers["Mcp-Session-Id"]))
    if headers.get("MCP-Protocol-Version"): _set_status(server_id, protocol_version=str(headers["MCP-Protocol-Version"]))
    rpc_error = _response_status(response, item)
    if rpc_error:
        return rpc_error
    return _response_body(response, payload.get("id"))

def _sse_lines(response):
    iterator = getattr(response, "iter_lines", None)
    if callable(iterator):
        for raw in iterator(decode_unicode=True): yield raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw or "")
    else: yield from str(getattr(response, "text", "") or "").splitlines()

def _sse_events(response):
    name, data = "message", []
    for line in _sse_lines(response):
        if not line:
            if data: yield name, "\n".join(data)
            name, data = "message", []
        elif line.startswith(":"): continue
        elif line.startswith("event:"): name = line[6:].strip() or "message"
        elif line.startswith("data:"): data.append(line[5:].lstrip())
    if data: yield name, "\n".join(data)

def _sse_exchange(server_id, item, payload):
    # Legacy HTTP/SSE sessions are bound to the live GET stream. For every RPC
    # exchange, negotiate initialize + initialized on that stream before sending
    # the requested message; never assume a previous short-lived GET survived.
    headers = _headers(item)
    headers.pop("Content-Type", None)
    headers["Accept"] = "text/event-stream"
    stream = requests.get(item["url"], headers=headers, timeout=(TIMEOUT, TIMEOUT),
                          stream=True, allow_redirects=False)
    try:
        rpc_error = _response_status(stream, item)
        if rpc_error:
            return rpc_error
        events = _sse_events(stream)
        endpoint = ""
        deadline = time.monotonic() + TIMEOUT
        while time.monotonic() < deadline:
            try:
                event_name, data = next(events)
            except StopIteration:
                break
            if event_name.lower() == "endpoint":
                endpoint = urljoin(item["url"], data.strip())
                break
        if not endpoint:
            raise MCPError("MCP HTTP/SSE не прислал событие endpoint", connection_failure=True)

        def exchange(message):
            post_headers = _headers(item)
            post_headers["Accept"] = "application/json, text/event-stream"
            post = requests.post(endpoint, json=message, headers=post_headers,
                                 timeout=TIMEOUT, allow_redirects=False)
            rpc_error = _response_status(post, item)
            if rpc_error:
                return rpc_error
            ctype = str(getattr(post, "headers", {}).get("Content-Type", "")).lower()
            text = str(getattr(post, "text", "") or "").lstrip()
            if "json" in ctype or text.startswith("{"):
                return _response_body(post, message.get("id"))
            if message.get("id") is None:
                return {}
            for event_name, data in events:
                if time.monotonic() > deadline:
                    break
                if event_name.lower() not in {"message", "error"}:
                    continue
                try:
                    response = json.loads(data)
                except ValueError:
                    continue
                if isinstance(response, dict) and response.get("id") == message.get("id"):
                    return response
            raise MCPError("MCP HTTP/SSE: тайм-аут ожидания JSON-RPC ответа",
                           retryable=True, connection_failure=True)

        if payload.get("method") != "initialize":
            init_id = _new_id()
            initialized = exchange(_rpc("initialize", {
                "protocolVersion": PROTOCOL_VERSION, "capabilities": {}, "clientInfo": CLIENT_INFO,
            }, init_id))
            result = initialized.get("result") if isinstance(initialized, dict) else None
            if not isinstance(result, dict) or not result.get("protocolVersion"):
                raise MCPError("MCP HTTP/SSE initialize не вернул protocolVersion", connection_failure=True)
            exchange(_rpc("notifications/initialized"))
        return exchange(payload)
    finally:
        close = getattr(stream, "close", None)
        if callable(close):
            close()

def _stdio_env(item):
    allowed = {"PATH", "HOME", "TMPDIR", "TMP", "TEMP", "LANG", "LC_ALL", "LC_CTYPE", "TERM",
               "SYSTEMROOT", "WINDIR", "PATHEXT", "APPDATA", "PREFIX", "ANDROID_ROOT",
               "ANDROID_DATA", "EXTERNAL_STORAGE", "LD_LIBRARY_PATH"}
    env = {k: v for k, v in os.environ.items() if k in allowed}
    env.update({str(k): str(v) for k, v in (item.get("env") or {}).items()})
    return env

def _stdio_send(proc, message):
    raw = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    proc.stdin.write(f"Content-Length: {len(raw)}\r\n\r\n".encode() + raw)
    proc.stdin.flush()

def _stdio_read(proc, timeout):
    if proc.stdout is None:
        raise MCPError("STDIO MCP: stdout недоступен", connection_failure=True)
    fd = proc.stdout.fileno()
    buf = bytearray(getattr(proc, "_mcp_buffer", b""))
    deadline = time.monotonic() + timeout
    length = None
    while time.monotonic() < deadline:
        if length is None:
            marker = buf.find(b"\r\n\r\n")
            sep = 4
            if marker < 0:
                marker = buf.find(b"\n\n")
                sep = 2
            if marker >= 0:
                headers = bytes(buf[:marker]).decode("ascii", "replace")
                del buf[:marker + sep]
                for line in headers.splitlines():
                    if line.lower().startswith("content-length:"):
                        try:
                            length = int(line.split(":", 1)[1].strip())
                        except ValueError as exc:
                            raise MCPError("STDIO MCP: неверный Content-Length", connection_failure=True) from exc
                        break
                if length is None or length < 0 or length > MAX_FRAME_BYTES:
                    raise MCPError("STDIO MCP: неверный или слишком большой frame", connection_failure=True)
            elif len(buf) > 16384:
                raise MCPError("STDIO MCP: слишком большой заголовок frame", connection_failure=True)
        if length is not None and len(buf) >= length:
            raw = bytes(buf[:length])
            proc._mcp_buffer = bytes(buf[length:])
            try:
                result = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as exc:
                raise MCPError("STDIO MCP: некорректный JSON frame", connection_failure=True) from exc
            if not isinstance(result, dict):
                raise MCPError("STDIO MCP: JSON-RPC frame должен быть объектом", connection_failure=True)
            return result
        ready, _, _ = select.select([fd], [], [], max(0.0, deadline - time.monotonic()))
        if not ready:
            break
        chunk = os.read(fd, 4096)
        if not chunk:
            raise MCPError(f"STDIO MCP: процесс завершился до ответа (код {proc.poll()})", connection_failure=True)
        buf.extend(chunk)
    raise MCPError("STDIO MCP: тайм-аут ожидания ответа", retryable=True, connection_failure=True)

def _stdio_line_send(proc, message):
    raw = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if b"\n" in raw or b"\r" in raw:
        raise MCPError("STDIO MCP: сообщение не помещается в одну строку")
    proc.stdin.write(raw + b"\n")
    proc.stdin.flush()

def _stdio_line_read(proc, timeout):
    if proc.stdout is None:
        raise MCPError("STDIO MCP: stdout недоступен", connection_failure=True)
    fd = proc.stdout.fileno()
    buffer = bytearray(getattr(proc, "_mcp_line_buffer", b""))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        marker = buffer.find(b"\n")
        if marker >= 0:
            raw = bytes(buffer[:marker]).rstrip(b"\r")
            proc._mcp_line_buffer = bytes(buffer[marker + 1:])
            try:
                message = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as exc:
                raise MCPError("STDIO MCP: неверный JSON line", connection_failure=True) from exc
            if not isinstance(message, dict):
                raise MCPError("STDIO MCP: JSON-RPC line должен быть объектом", connection_failure=True)
            return message
        if len(buffer) > MAX_FRAME_BYTES:
            raise MCPError("STDIO MCP: JSON line превышает лимит", connection_failure=True)
        ready, _, _ = select.select([fd], [], [], max(0.0, deadline - time.monotonic()))
        if not ready:
            break
        chunk = os.read(fd, 4096)
        if not chunk:
            raise MCPError(f"STDIO MCP: процесс завершился до ответа (код {proc.poll()})", connection_failure=True)
        buffer.extend(chunk)
    raise MCPError("STDIO MCP: тайм-аут JSON line", retryable=True, connection_failure=True)

def _stdio_wait_id(proc, request_id, timeout, *, modern=False):
    reader = _stdio_line_read if modern else _stdio_read
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        message = reader(proc, max(.01, deadline - time.monotonic()))
        if message.get("id") == request_id:
            return message
    raise MCPError("STDIO MCP: тайм-аут JSON-RPC", retryable=True, connection_failure=True)

class _StdioSession:
    """One persistent server process per configured STDIO server, serialized per process."""
    def __init__(self, item, mode):
        command = [str(item["command"]), *[str(x) for x in item.get("args", [])]]
        try:
            self.proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, env=_stdio_env(item), cwd=item.get("cwd") or None,
                shell=False, bufsize=0)
        except (OSError, ValueError) as exc:
            raise MCPError(f"STDIO MCP: запуск невозможен ({type(exc).__name__})", connection_failure=True) from exc
        self.mode = mode
        self.lock = threading.RLock()
        self.closed = False
        self.initialize_result = None
        self.ready = mode == "modern"
        self.last_used = time.monotonic()

    def close(self):
        self.closed = True
        proc = self.proc
        if proc.poll() is None:
            try:
                if proc.stdin:
                    proc.stdin.close()
                try:
                    proc.wait(timeout=.5)
                except subprocess.TimeoutExpired:
                    proc.terminate()
                    try:
                        proc.wait(timeout=.5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=.5)
            except (OSError, subprocess.SubprocessError):
                pass
        if proc.stdout:
            try:
                proc.stdout.close()
            except OSError:
                pass


def _close_stdio_session(server_id):
    with _STDIO_SESSIONS_LOCK:
        session = _STDIO_SESSIONS.pop(server_id, None)
    if session:
        with session.lock:
            session.close()

def _close_stdio_sessions():
    with _STDIO_SESSIONS_LOCK:
        sessions = list(_STDIO_SESSIONS.values())
        _STDIO_SESSIONS.clear()
    for session in sessions:
        with session.lock:
            session.close()

atexit.register(_close_stdio_sessions)

def _get_stdio_session(server_id, item, mode):
    idle_timeout = _env_float("MCP_STDIO_IDLE_TIMEOUT", 300.0, 10.0, 3600.0)
    with _STDIO_SESSIONS_LOCK:
        session = _STDIO_SESSIONS.get(server_id)
        expired = session and time.monotonic() - session.last_used > idle_timeout
        if session and (session.closed or session.mode != mode or session.proc.poll() is not None or expired):
            with session.lock:
                session.close()
            _STDIO_SESSIONS.pop(server_id, None)
            session = None
        if session is None:
            max_sessions = _env_int("MCP_STDIO_MAX_SESSIONS", 8, 1, 32)
            while len(_STDIO_SESSIONS) >= max_sessions:
                oldest_id = min(_STDIO_SESSIONS, key=lambda key: _STDIO_SESSIONS[key].last_used)
                oldest = _STDIO_SESSIONS.pop(oldest_id)
                with oldest.lock:
                    oldest.close()
            session = _StdioSession(item, mode)
            _STDIO_SESSIONS[server_id] = session
        return session

def _stdio_read_for(session, request_id, timeout):
    return _stdio_wait_id(session.proc, request_id, timeout, modern=session.mode == "modern")

def _stdio_exchange_session(session, item, method, params=None):
    if method == "initialize":
        if session.initialize_result is None:
            request_id = _new_id()
            _stdio_send(session.proc, _rpc("initialize", params, request_id))
            session.initialize_result = _stdio_read_for(session, request_id, TIMEOUT)
        session.last_used = time.monotonic()
        return session.initialize_result

    if not session.ready:
        if session.initialize_result is None:
            request_id = _new_id()
            _stdio_send(session.proc, _rpc("initialize", {
                "protocolVersion": str(item.get("protocol_version") or LEGACY_PROTOCOL_VERSION),
                "capabilities": {}, "clientInfo": CLIENT_INFO,
            }, request_id))
            session.initialize_result = _stdio_read_for(session, request_id, TIMEOUT)
        if isinstance(session.initialize_result.get("error"), dict):
            session.last_used = time.monotonic()
            return session.initialize_result
        _stdio_send(session.proc, _rpc("notifications/initialized"))
        session.ready = True
        if method == "notifications/initialized":
            session.last_used = time.monotonic()
            return {}

    if method.startswith("notifications/"):
        _stdio_send(session.proc, _rpc(method, params))
        session.last_used = time.monotonic()
        return {}
    request_id = _new_id()
    _stdio_send(session.proc, _rpc(method, params, request_id))
    result = _stdio_read_for(session, request_id, TIMEOUT)
    session.last_used = time.monotonic()
    return result

def _stdio_exchange(server_id, item, method, params=None):
    while True:
        session = _get_stdio_session(server_id, item, "legacy")
        with session.lock:
            if session.closed:
                continue
            return _stdio_exchange_session(session, item, method, params)

def _stdio_modern_exchange(server_id, item, payload, version, timeout=None):
    while True:
        session = _get_stdio_session(server_id, item, "modern")
        with session.lock:
            if session.closed:
                continue
            message = _modernize(payload, version)
            request_id = message.get("id")
            _stdio_line_send(session.proc, message)
            result = _stdio_read_for(session, request_id,
                                     TIMEOUT if timeout is None else max(.1, timeout))
            session.last_used = time.monotonic()
            return result

def _perform(server_id, item, payload, tool_schema=None):
    transport = _normalize_transport(item.get("transport", "streamable_http"))
    if item.get("protocol_mode") == "modern":
        payload = _modernize(payload, str(item.get("protocol_version") or PROTOCOL_VERSION))
    with _REQUEST_SEMAPHORE:
        if transport == "stdio":
            if item.get("protocol_mode") == "modern":
                probe_timeout = _env_float("MCP_STDIO_DISCOVERY_TIMEOUT", min(TIMEOUT, 4.0), .5, 30.0) if payload.get("method") == "server/discover" else TIMEOUT
                return _stdio_modern_exchange(server_id, item, payload, str(item.get("protocol_version") or PROTOCOL_VERSION), timeout=probe_timeout)
            return _stdio_exchange(server_id, item, payload["method"], payload.get("params"))
        if transport == "http_sse": return _sse_exchange(server_id, item, payload)
        return _http_exchange(server_id, item, payload, tool_schema)

SAFE_RETRY_METHODS = {"initialize", "notifications/initialized", "ping", "tools/list", "resources/list", "resources/read", "prompts/list", "prompts/get", "completion/complete"}

def _meta_fields(version):
    return {META_PROTOCOL_VERSION: version, META_CLIENT_CAPABILITIES: {}, META_CLIENT_INFO: CLIENT_INFO}

def _choose_version(versions):
    available = {str(value) for value in versions if isinstance(value, str)} if isinstance(versions, list) else set()
    for value in (PROTOCOL_VERSION, *LEGACY_PROTOCOL_FALLBACKS):
        if value in available:
            return value
    return ""

def _set_discovered(server_id, result, version, mode):
    meta = result.get("_meta") if isinstance(result.get("_meta"), dict) else {}
    server_info = meta.get("io.modelcontextprotocol/serverInfo") or result.get("serverInfo") or {}
    _set_status(server_id, protocol_mode=mode, protocol_version=version, session_id="", initialized=True,
                connected=True, server_info=server_info, capabilities=result.get("capabilities") or {},
                server_instructions=str(result.get("instructions") or "")[:1000],
                last_error="", last_checked=int(time.time()))

def _legacy_initialize(server_id, item, version):
    if _normalize_transport(item.get("transport")) == "stdio":
        _close_stdio_session(server_id)
    item = {**item, "protocol_mode": "legacy", "protocol_version": version, "session_id": ""}
    retry_count = _env_int("MCP_MAX_RETRIES", MAX_RETRIES, 0, 4)
    for attempt in range(retry_count + 1):
        try:
            body = _perform(server_id, item, _rpc("initialize", {
                "protocolVersion": version, "capabilities": {}, "clientInfo": CLIENT_INFO,
            }, _new_id()))
            if isinstance(body.get("error"), dict):
                err = body["error"]
                raise MCPError(f"MCP initialize: {_safe_error(err.get('message') or err, item)}")
            result = body.get("result")
            if not isinstance(result, dict) or not result.get("protocolVersion"):
                raise MCPError("MCP initialize не вернул protocolVersion", connection_failure=True)
            selected = str(result["protocolVersion"])
            if selected not in LEGACY_PROTOCOL_FALLBACKS and selected != PROTOCOL_VERSION:
                raise MCPError(f"MCP согласовал неизвестную версию протокола: {selected}")
            _set_status(server_id, protocol_mode="legacy", protocol_version=selected,
                        initialized=True, connected=True, server_info=result.get("serverInfo") or {},
                        capabilities=result.get("capabilities") or {}, last_error="", last_checked=int(time.time()))
            if _normalize_transport(item.get("transport", "streamable_http")) == "streamable_http":
                current = _connection(server_id)
                try:
                    _perform(server_id, current, _rpc("notifications/initialized"))
                except Exception as exc:
                    log.info("MCP server=%s initialized notification unacknowledged (%s)", server_id, type(exc).__name__)
            return result
        except MCPError as exc:
            if attempt < retry_count and exc.retryable:
                time.sleep(exc.retry_after if exc.retry_after is not None else min(2.0, .2 * (2 ** attempt)))
                continue
            raise
        except requests.RequestException as exc:
            transient = isinstance(exc, (requests.Timeout, requests.ConnectionError))
            if attempt < retry_count and transient:
                time.sleep(min(2.0, .2 * (2 ** attempt)))
                continue
            raise MCPError(f"MCP initialize сеть/тайм-аут: {type(exc).__name__}", retryable=transient,
                           connection_failure=True) from exc
    raise MCPError("MCP initialize не выполнен")

def _initialize_unlocked(server_id, item):
    transport = _normalize_transport(item.get("transport", "streamable_http"))
    if transport in {"streamable_http", "stdio"}:
        probe = _rpc("server/discover", {"_meta": _meta_fields(PROTOCOL_VERSION)}, _new_id())
        modern_item = {**item, "protocol_mode": "modern", "protocol_version": PROTOCOL_VERSION, "session_id": ""}
        try:
            body = _perform(server_id, modern_item, probe)
        except Exception as exc:
            # STDIO protocol revisions also changed framing, so a failed modern
            # probe is followed by a fresh legacy process; HTTP only falls back
            # on an explicit old-endpoint/unknown-method response.
            if transport == "stdio":
                body = {"error": {"code": -1, "message": type(exc).__name__}}
            elif isinstance(exc, MCPError) and exc.status_code in {400, 404, 405}:
                body = {"error": {"code": -1, "message": str(exc)}}
            else:
                raise
        error = body.get("error") if isinstance(body, dict) else None
        result = body.get("result") if isinstance(body, dict) else None
        if isinstance(error, dict):
            error_data = error.get("data") if isinstance(error.get("data"), dict) else {}
            if error.get("code") == -32022:
                if not isinstance(error_data.get("supported"), list):
                    raise MCPError("MCP вернул UnsupportedProtocolVersionError без списка версий")
                selected = _choose_version(error_data["supported"])
                if not selected:
                    raise MCPError("MCP не поддерживает версии протокола NovaMind")
                if selected == PROTOCOL_VERSION:
                    result = {"supportedVersions": [selected], "capabilities": {}}
                else:
                    return _legacy_initialize(server_id, item, selected)
            elif error.get("code") in {-32020, -32021}:
                raise MCPError(f"MCP server/discover: {_safe_error(error.get('message') or error, item)}")
            else:
                # Dual-era clients fall back on unrecognized discovery errors;
                # only the explicit modern protocol errors above are definitive.
                return _legacy_initialize(server_id, item, LEGACY_PROTOCOL_VERSION)
        if isinstance(result, dict) and isinstance(result.get("supportedVersions"), list):
            selected = _choose_version(result["supportedVersions"])
            if not selected:
                raise MCPError("MCP не поддерживает совместимую версию протокола")
            if selected == PROTOCOL_VERSION:
                _set_discovered(server_id, result, selected, "modern")
                return {"protocolVersion": selected, "serverInfo": (result.get("_meta") or {}).get("io.modelcontextprotocol/serverInfo", {}),
                        "capabilities": result.get("capabilities") or {}}
            return _legacy_initialize(server_id, item, selected)
        if transport == "stdio":
            return _legacy_initialize(server_id, item, LEGACY_PROTOCOL_VERSION)
        raise MCPError("MCP server/discover вернул неполный результат", connection_failure=True)
    return _legacy_initialize(server_id, item, LEGACY_PROTOCOL_VERSION)

def _initialize_session(server_id):
    with _server_lock(server_id):
        item = _connection(server_id)
        try: return {"connected": True, "result": _initialize_unlocked(server_id, item)}
        except Exception as exc:
            _mark_failure(server_id, exc); return {"connected": False, "error": _safe_error(exc, item)}

def request(server_id, method, params=None, *, retry_safe=None, tool_schema=None):
    method = str(method or "").strip()
    if not method: raise ValueError("Не указан MCP method")
    # Serialize only initialization/session creation. Independent JSON-RPC ids
    # can be outstanding concurrently, including calls to one HTTP server.
    with _server_lock(server_id):
        item = _connection(server_id)
        if method not in {"initialize", "notifications/initialized"} and not item.get("initialized"):
            init = _initialize_unlocked(server_id, item); item = _connection(server_id)
            item.setdefault("protocol_version", init.get("protocolVersion", PROTOCOL_VERSION))
        request_id = None if method.startswith("notifications/") else _new_id()
        payload = _rpc(method, params, request_id)
    safe = method in SAFE_RETRY_METHODS if retry_safe is None else bool(retry_safe)
    retries = _env_int("MCP_MAX_RETRIES", MAX_RETRIES, 0, 4) if safe else 0
    recovered_session = False
    for attempt in range(retries + 1):
        try:
            response = _perform(server_id, item, payload, tool_schema)
            if isinstance(response.get("error"), dict):
                err = response["error"]
                raise MCPError(f"MCP JSON-RPC {err.get('code')}: {_safe_error(err.get('message') or err, item)}")
            _mark_success(server_id)
            return response
        except MCPError as exc:
            if safe and not recovered_session and exc.status_code == 404 and item.get("session_id") and attempt < retries:
                with _server_lock(server_id):
                    _set_status(server_id, session_id="", initialized=False, connected=False)
                    item = _connection(server_id); _initialize_unlocked(server_id, item); item = _connection(server_id)
                recovered_session = True; continue
            if attempt < retries and exc.retryable:
                time.sleep(exc.retry_after if exc.retry_after is not None else min(2.0, .2 * 2 ** attempt)); continue
            if exc.connection_failure or exc.status_code is not None or exc.retryable: _mark_failure(server_id, exc)
            raise MCPError(_safe_error(exc, item), status_code=exc.status_code, retryable=exc.retryable,
                           connection_failure=exc.connection_failure) from exc
        except requests.RequestException as exc:
            transient = isinstance(exc, (requests.Timeout, requests.ConnectionError))
            if attempt < retries and transient: time.sleep(min(2.0, .2 * 2 ** attempt)); continue
            safe_exc = MCPError(f"MCP сеть/тайм-аут: {type(exc).__name__}", retryable=transient, connection_failure=True)
            _mark_failure(server_id, safe_exc); raise safe_exc from exc
        except Exception as exc:
            safe_exc = MCPError(f"MCP ошибка транспорта: {type(exc).__name__}", connection_failure=True)
            _mark_failure(server_id, safe_exc); raise safe_exc from exc
    raise MCPError("MCP запрос не выполнен")

def _ensure_connected(server_id):
    item = _connection(server_id)
    if item.get("initialized") and item.get("connected"): return item
    result = _initialize_session(server_id)
    if not result.get("connected"): raise MCPError(str(result.get("error") or "Не удалось подключиться к MCP"), connection_failure=True)
    return _connection(server_id)

def _matches(value, expected):
    return {"object": lambda x: isinstance(x, dict), "array": lambda x: isinstance(x, list), "string": lambda x: isinstance(x, str),
            "integer": lambda x: isinstance(x, int) and not isinstance(x, bool), "number": lambda x: isinstance(x, (int, float)) and not isinstance(x, bool),
            "boolean": lambda x: isinstance(x, bool), "null": lambda x: x is None}.get(str(expected), lambda _x: True)(value)

def _schema_error(value, schema, path="arguments", depth=0):
    if not isinstance(schema, dict) or depth > 20: return None
    expected = schema.get("type")
    if isinstance(expected, list):
        if not any(_matches(value, t) for t in expected): return f"{path}: ожидался тип {' или '.join(map(str, expected))}"
    elif expected and not _matches(value, expected): return f"{path}: ожидался тип {expected}"
    if "enum" in schema and value not in schema["enum"]: return f"{path}: значение отсутствует в enum"
    if isinstance(value, dict):
        for key in schema.get("required", []) if isinstance(schema.get("required", []), list) else []:
            if key not in value: return f"{path}.{key}: обязательный аргумент отсутствует"
        props = schema.get("properties", {})
        if isinstance(props, dict):
            for key, child in value.items():
                if key in props:
                    error = _schema_error(child, props[key], f"{path}.{key}", depth + 1)
                    if error: return error
                elif schema.get("additionalProperties") is False: return f"{path}.{key}: неизвестный аргумент"
        for key, test in (("minProperties", lambda n: len(value) < n), ("maxProperties", lambda n: len(value) > n)):
            if isinstance(schema.get(key), int) and test(schema[key]): return f"{path}: неверное число свойств"
    elif isinstance(value, list):
        for key, test in (("minItems", lambda n: len(value) < n), ("maxItems", lambda n: len(value) > n)):
            if isinstance(schema.get(key), int) and test(schema[key]): return f"{path}: неверное число элементов"
        if isinstance(schema.get("items"), dict):
            for i, child in enumerate(value):
                error = _schema_error(child, schema["items"], f"{path}[{i}]", depth + 1)
                if error: return error
    elif isinstance(value, str):
        for key, test in (("minLength", lambda n: len(value) < n), ("maxLength", lambda n: len(value) > n)):
            if isinstance(schema.get(key), int) and test(schema[key]): return f"{path}: неверная длина строки"
        try:
            if isinstance(schema.get("pattern"), str) and re.search(schema["pattern"], value) is None: return f"{path}: строка не соответствует pattern"
        except re.error: pass
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(schema.get("minimum"), (int, float)) and value < schema["minimum"]: return f"{path}: ниже minimum"
        if isinstance(schema.get("maximum"), (int, float)) and value > schema["maximum"]: return f"{path}: выше maximum"
    return None

def list_tools(server_id, *, force=False):
    cached = _TOOL_CACHE.get(server_id)
    if not force and cached and cached[0] > time.monotonic(): return copy.deepcopy(cached[1])
    with _server_lock(server_id):
        cached = _TOOL_CACHE.get(server_id)
        if not force and cached and cached[0] > time.monotonic(): return copy.deepcopy(cached[1])
        _ensure_connected(server_id); result_tools = []; cursor = None; seen = set()
        for _ in range(MAX_DISCOVERY_PAGES):
            body = request(server_id, "tools/list", {"cursor": cursor} if cursor is not None else {})
            result = body.get("result") if isinstance(body, dict) else None
            if not isinstance(result, dict) or not isinstance(result.get("tools", []), list): raise MCPError("MCP tools/list вернул некорректный формат", connection_failure=True)
            result_tools.extend(x for x in result.get("tools", []) if isinstance(x, dict) and x.get("name"))
            cursor = result.get("nextCursor")
            if cursor is None: break
            key = str(cursor)
            if key in seen: raise MCPError("MCP tools/list вернул повторяющийся nextCursor")
            seen.add(key)
        else: raise MCPError(f"MCP tools/list превысил лимит страниц ({MAX_DISCOVERY_PAGES})")
        item = _connection(server_id)
        if item.get("protocol_mode") == "modern" and _normalize_transport(item.get("transport")) == "streamable_http":
            accepted = []
            for tool in result_tools:
                error = _header_schema_error(tool.get("inputSchema") or {})
                if error:
                    log.warning("MCP server=%s tool=%s excluded: %s", server_id, tool.get("name"), error)
                else:
                    accepted.append(tool)
            result_tools = accepted
        _TOOL_CACHE[server_id] = (time.monotonic() + TOOL_CACHE_SECONDS, copy.deepcopy(result_tools)); _mark_success(server_id)
        return result_tools

def cached_tool_annotations(server_id, tool_name):
    """Return cached annotations only; risk checks must not trigger discovery."""
    try:
        sid = resolve_server_id(server_id, tool_name, catalog_list=[])
        cached = _TOOL_CACHE.get(sid)
        if not cached or cached[0] <= time.monotonic():
            return None
        tool = next((item for item in cached[1] if item.get("name") == tool_name), None)
        if tool is None:
            return None
        annotations = tool.get("annotations")
        return annotations if isinstance(annotations, dict) else {}
    except (ValueError, TypeError):
        return None

def test_connection(server_id):
    item = _connection(server_id); initialized = _initialize_session(server_id)
    if not initialized.get("connected"): return {"connected": False, "error": initialized.get("error")}
    try:
        tools = list_tools(server_id, force=True)
        return {"connected": True, "server": initialized.get("result", {}).get("serverInfo", {}), "tools": len(tools)}
    except Exception as exc:
        _mark_failure(server_id, exc); return {"connected": False, "error": _safe_error(exc, item)}

def resolve_server_id(raw_id, tool_name="", catalog_list=None):
    servers = _read(); raw = str(raw_id or "").strip()
    if raw in servers: return raw
    needle = raw.lower(); matches = [sid for sid, item in servers.items() if needle and (needle in sid.lower() or needle in str(item.get("name", "")).lower())]
    if tool_name:
        catalog_list = catalog_list if catalog_list is not None else connected_tools()
        by_tool = {str(x.get("server_id")) for x in catalog_list if str(x.get("name", "")).lower() == tool_name.lower()}
        if matches:
            narrowed = [sid for sid in matches if sid in by_tool]
            if narrowed: matches = narrowed
        elif len(by_tool) == 1: return next(iter(by_tool))
    if len(matches) == 1: return matches[0]
    raise ValueError("Не удалось однозначно определить MCP-сервер")

def connected_tools():
    servers = _read()
    def discover(sid, item):
        if _FAILURE_UNTIL.get(sid, 0) > time.monotonic(): return []
        try:
            return [{"server_id": sid, "server_name": item.get("name", sid), "transport": _normalize_transport(item.get("transport", "streamable_http")),
                     "name": t["name"], "description": str(t.get("description") or ""), "inputSchema": t.get("inputSchema") or {"type": "object"},
                     "annotations": t.get("annotations") or {}, **({"outputSchema": t["outputSchema"]} if t.get("outputSchema") is not None else {})} for t in list_tools(sid)]
        except Exception as exc: _mark_failure(sid, exc); return []
    result = []
    with ThreadPoolExecutor(max_workers=max(1, min(8, len(servers))), thread_name_prefix="mcp-discovery") as pool:
        futures = [pool.submit(discover, sid, item) for sid, item in servers.items() if isinstance(item, dict)]
        for future in as_completed(futures):
            try: result.extend(future.result())
            except Exception: pass
    result.sort(key=lambda item: (str(item.get("server_id", "")), str(item.get("name", ""))))
    return result

def server_summaries(tools=None):
    tools = connected_tools() if tools is None else tools
    result = {}
    for tool in tools:
        sid = tool["server_id"]; item = result.setdefault(sid, {"server_id": sid, "name": tool.get("server_name", sid), "transport": tool.get("transport"), "tools": 0})
        item["tools"] += 1
    for sid, server in _read().items():
        if sid not in result:
            try: transport = _normalize_transport(server.get("transport", "streamable_http"))
            except ValueError: transport = "unknown"
            result[sid] = {"server_id": sid, "name": server.get("name", sid), "transport": transport, "tools": 0,
                           "connected": bool(server.get("connected")), "error": _safe_error(server.get("last_error", ""), server)}
        else: result[sid]["connected"] = bool(server.get("connected"))
    return list(result.values())

def tools_page(server_id="", offset=0, limit=20):
    offset = max(0, int(offset or 0)); limit = min(50, max(1, int(limit or 20)))
    if not server_id:
        servers = server_summaries()
        end = offset + limit
        return {"count": len(servers), "offset": offset, "limit": limit,
                "next_offset": end if end < len(servers) else None, "servers": servers[offset:end]}
    sid = resolve_server_id(server_id); tools = list_tools(sid); item = _connection(sid); end = offset + limit
    return {"server_id": sid, "server_name": item.get("name", sid), "count": len(tools), "offset": offset,
            "limit": limit, "next_offset": end if end < len(tools) else None,
            "tools": [{"name": t["name"], "description": str(t.get("description") or "")[:800],
                       "inputSchema": t.get("inputSchema") or {"type": "object"}, "annotations": t.get("annotations") or {}} for t in tools[offset:end]]}

def call_tool(server_id, tool_name, arguments=None):
    name = str(tool_name or "").strip()
    if not name: raise ValueError("Не указано имя MCP-инструмента")
    if arguments is None: arguments = {}
    if not isinstance(arguments, dict): raise ValueError("Аргументы MCP-инструмента должны быть объектом")
    sid = resolve_server_id(server_id, name)
    tool = next((x for x in list_tools(sid) if x.get("name") == name), None)
    if tool is None: raise MCPError(f"MCP-инструмент «{name}» не найден на сервере {sid}")
    schema = tool.get("inputSchema") or {"type": "object"}; error = _schema_error(arguments, schema)
    if error: raise ValueError(f"Некорректные аргументы MCP: {error}")
    annotations = tool.get("annotations") or {}
    safe = annotations.get("readOnlyHint") is True or (annotations.get("idempotentHint") is True and annotations.get("destructiveHint") is not True)
    log.info("MCP call server=%s tool=%s retry_safe=%s", sid, name, safe)
    body = request(sid, "tools/call", {"name": name, "arguments": arguments}, retry_safe=safe,
                   tool_schema=schema)
    result = body.get("result") if isinstance(body, dict) else None
    if not isinstance(result, dict): raise MCPError("MCP tools/call вернул некорректный формат")
    if result.get("isError") is True:
        content = result.get("content", []); text = "\n".join(str(x.get("text", "")) for x in content if isinstance(x, dict) and x.get("text")) or str(result.get("error") or result)
        raise MCPError(f"MCP-инструмент завершился с ошибкой: {_safe_error(text, _connection(sid))}")
    return result

def search_tools(query, limit=5):
    needle = str(query or "").lower(); scored = []
    for tool in connected_tools():
        text = f"{tool['name']} {tool.get('description','')} {tool['server_name']}".lower()
        score = sum(word in text for word in needle.split())
        if score: scored.append((score, tool))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [t for _, t in scored[:max(1, min(20, int(limit)))]]
