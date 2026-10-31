"""MCP catalog and small Streamable HTTP client for NovaMind settings.

Secrets are kept server-side in a chmod 600 JSON file. The browser receives
only connection metadata and masked status, never API keys or OAuth tokens.
"""
from __future__ import annotations

import json
import logging
import os
import re
import secrets
import stat
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

log = logging.getLogger("novamind.mcp")

ROOT = Path(__file__).resolve().parent
STORE = Path(os.getenv("MCP_CONNECTIONS_FILE", ROOT / "mcp_connections.json"))
TIMEOUT = float(os.getenv("MCP_TIMEOUT", "12"))
RETRY_STATUS_CODES = {403, 429}
MAX_RETRIES = max(0, min(int(os.getenv("MCP_MAX_RETRIES", "2")), 5))
RETRY_BASE_DELAY = max(0.0, float(os.getenv("MCP_RETRY_DELAY", "2.0")))


class McpError(RuntimeError):
    """Ошибка MCP-вызова с сохранённым HTTP/статус-кодом и текстом сервера."""

    def __init__(self, message: str, status_code: int | None = None, logged: bool = False):
        super().__init__(message)
        self.status_code = status_code
        self._mcp_logged = logged


def _extract_error_text(value: Any) -> str:
    """Извлекает читаемый текст ошибки из MCP content / JSON-RPC error / тела HTTP."""
    if value is None:
        return ""
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("{"):
            try:
                return _extract_error_text(json.loads(text)) or text
            except ValueError:
                return text
        return text
    if isinstance(value, list):
        parts = [_extract_error_text(item) for item in value]
        return "; ".join(part for part in parts if part)
    if isinstance(value, dict):
        for key in ("text", "message", "detail", "error_description"):
            if value.get(key):
                inner = _extract_error_text(value[key])
                if inner:
                    return inner
        if value.get("error"):
            return _extract_error_text(value["error"])
        return json.dumps(value, ensure_ascii=False)
    return str(value).strip()


_HTTP_CODE_RE = re.compile(r"\b(400|401|403|404|408|409|413|422|429|500|502|503|504)\b")


def _detect_http_status(text: str) -> int | None:
    match = _HTTP_CODE_RE.search(str(text or ""))
    return int(match.group(1)) if match else None


def _retry_delay(attempt: int, retry_after: str | None = None) -> float:
    base = float(os.getenv("MCP_RETRY_DELAY", str(RETRY_BASE_DELAY)))
    if retry_after:
        try:
            return min(max(float(retry_after), 0.1), 10.0)
        except ValueError:
            pass
    return min(base * (2 ** attempt), 10.0)

CATALOG = [
    {"id": "custom", "name": "Свой MCP-сервер",
     "description": "Любой Streamable HTTP MCP endpoint", "url": "",
     "auth": ["api_key", "bearer", "none"]},
    {"id": "context7", "name": "Context7 — документация",
     "description": "Актуальная документация библиотек и примеры кода",
     "url": "https://mcp.context7.com/mcp", "auth": ["none", "api_key"]},
    {"id": "github", "name": "GitHub",
     "description": "Репозитории, issues, pull requests и код (нужен token)",
     "url": "https://api.githubcopilot.com/mcp/", "auth": ["bearer"]},
    {"id": "notion", "name": "Notion",
     "description": "Страницы и базы знаний Notion (нужен access token)",
     "url": "https://mcp.notion.com/mcp", "auth": ["bearer"]},
    {"id": "sentry", "name": "Sentry",
     "description": "Ошибки приложений, проекты и диагностика",
     "url": "https://mcp.sentry.dev/mcp", "auth": ["bearer"]},
]


def _read() -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(STORE.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError, OSError):
        data = {}
    if not data:  # запасной источник: переменная окружения (на Render диск стирается)
        raw = os.getenv("MCP_CONNECTIONS_JSON", "")
        if raw:
            try:
                data = json.loads(raw)
            except ValueError:
                data = {}
    return data


def _write(data: dict[str, dict[str, Any]]) -> None:
    STORE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STORE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
    tmp.replace(STORE)


def catalog() -> list[dict[str, Any]]:
    """Шаблоны подключения. Ничего не подключается самим фактом наличия в каталоге."""
    return [{**item, "transport": "http"} for item in CATALOG]


def public_connections() -> list[dict[str, Any]]:
    """Метаданные подключений без секретов.

    ``transport`` — реальный транспорт клиента: NovaMind говорит с сервером по
    Streamable HTTP (JSON-RPC поверх POST + SSE-ответы). STDIO-серверы не
    поддерживаются, и интерфейс не должен делать вид, что они есть.
    """
    return [{"id": key, "name": value.get("name", key), "url": value.get("url", ""),
             "auth": value.get("auth", "none"), "connected": bool(value.get("connected")),
             "transport": "http", "last_error": value.get("last_error"),
             "updated_at": value.get("updated_at"),
             "tools_count": value.get("tools_count")}
            for key, value in _read().items()]


def save_connection(payload: dict[str, Any]) -> dict[str, Any]:
    name = str(payload.get("name") or payload.get("id") or "custom").strip()[:80]
    server_id = str(payload.get("id") or secrets.token_urlsafe(8)).strip()[:80]
    url = str(payload.get("url") or "").strip()
    auth = str(payload.get("auth") or "none").strip().lower()
    if not url.startswith(("http://", "https://")):
        raise ValueError("URL MCP-сервера должен начинаться с http:// или https://")
    if auth not in {"none", "api_key", "bearer", "basic", "oauth", "google_oauth"}:
        raise ValueError("Неподдерживаемый способ авторизации")
    data = _read()
    old = data.get(server_id, {})
    item = {"name": name, "url": url, "auth": auth, "connected": False,
            "updated_at": int(time.time()), "last_error": None}
    # Preserve credentials when the UI only edits metadata.
    for key in ("api_key", "token", "username", "password", "access_token", "refresh_token"):
        if payload.get(key):
            item[key] = str(payload[key])
        elif old.get(key):
            item[key] = old[key]
    data[server_id] = item
    _write(data)
    return server_id


def remove_connection(server_id: str) -> None:
    data = _read()
    data.pop(server_id, None)
    _write(data)


def _headers(item: dict[str, Any]) -> dict[str, str]:
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    # Этот заголовок обязателен для последующих запросов сессии, но часть
    # локальных серверов отвергает его на самом первом initialize.
    if item.get("initialized"):
        headers["MCP-Protocol-Version"] = item.get("protocol_version", "2025-06-18")
    if item.get("session_id"):
        headers["MCP-Session-Id"] = item["session_id"]
    auth = item.get("auth")
    if auth == "api_key" and item.get("api_key"):
        headers["X-API-Key"] = item["api_key"]
    elif auth in {"bearer", "oauth", "google_oauth"} and item.get("access_token", item.get("token")):
        headers["Authorization"] = "Bearer " + item.get("access_token", item.get("token"))
    elif auth == "basic" and item.get("username"):
        import base64
        raw = f"{item['username']}:{item.get('password', '')}".encode()
        headers["Authorization"] = "Basic " + base64.b64encode(raw).decode()
    return headers


def request(server_id: str, method: str, params: dict[str, Any] | None = None, _retry_session: bool = True) -> dict[str, Any]:
    data = _read()
    item = data.get(server_id)
    if not item:
        raise ValueError("MCP-сервер не найден")
    if urlparse(item["url"]).hostname in {"localhost", "127.0.0.1", "::1"} or item["url"].startswith(("https://", "http://")):
        pass
    payload = {"jsonrpc": "2.0", "id": int(time.time() * 1000) % 1000000000, "method": method, "params": params or {}}
    max_retries = max(0, min(int(os.getenv("MCP_MAX_RETRIES", str(MAX_RETRIES))), 5))
    response = None
    for attempt in range(max_retries + 1):
        try:
            response = requests.post(item["url"], headers=_headers(item), json=payload, timeout=TIMEOUT)
        except requests.RequestException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            log.error("MCP ошибка сети [%s %s] status=%s: %s",
                      server_id, method, status or "network_error", exc)
            raise
        session_id = response.headers.get("mcp-session-id")
        if session_id:
            data[server_id]["session_id"] = session_id
            item = data[server_id]
            _write(data)
        if response.status_code in RETRY_STATUS_CODES and attempt < max_retries:
            delay = _retry_delay(attempt, response.headers.get("Retry-After"))
            raw_hint = _extract_error_text(response.text)[:240]
            log.warning("MCP HTTP %s [%s %s] status=%s: пауза %.1f с и повтор (%d/%d)%s",
                        response.status_code, server_id, method, response.status_code,
                        delay, attempt + 1, max_retries,
                        f" — {raw_hint}" if raw_hint else "")
            if delay > 0:
                time.sleep(delay)
            continue
        break

    assert response is not None
    if response.status_code >= 400:
        detail = _extract_error_text(response.text).replace("\n", " ")[:600]
        session_lost = any(marker in detail.lower() for marker in (
            "missing mcp-session-id",
            "session not found",
            "reinitialize",
            "invalid session",
        ))
        if _retry_session and method not in {"initialize", "notifications/initialized"} and session_lost:
            # Streamable HTTP-сервер мог перезапуститься и забыть старую
            # сессию, хотя NovaMind всё ещё хранит её id. Создаём новую один раз.
            data[server_id].pop("session_id", None)
            data[server_id].pop("initialized", None)
            _write(data)
            initialized = _initialize_session(server_id)
            if isinstance(initialized, dict) and initialized.get("error"):
                err_msg = _extract_error_text(initialized.get("error"))
                log.error("MCP reinitialize failed [%s %s] status=%s: %s",
                          server_id, method, response.status_code, err_msg)
                err = requests.HTTPError(
                    f"MCP reinitialize failed (HTTP {response.status_code}): {err_msg}",
                    response=response)
                err.status_code = response.status_code
                raise err
            try:
                request(server_id, "notifications/initialized", {}, _retry_session=False)
            except Exception:
                pass
            return request(server_id, method, params, _retry_session=False)
        log.error("MCP HTTP ошибка [%s %s] status=%s: %s",
                  server_id, method, response.status_code, detail or "сервер не сообщил причину")
        err = requests.HTTPError(
            f"MCP HTTP {response.status_code}: {detail or 'сервер не сообщил причину'}",
            response=response,
        )
        err.status_code = response.status_code
        err._mcp_logged = True
        raise err
    content_type = response.headers.get("content-type", "")
    if response.status_code in {202, 204} or not response.text.strip():
        return {}
    if "text/event-stream" in content_type:
        # Streamable HTTP servers commonly send one JSON event per data line.
        for line in response.text.splitlines():
            if line.startswith("data:"):
                try:
                    return json.loads(line[5:].strip())
                except ValueError:
                    continue
        log.error("MCP пустой SSE-ответ [%s %s] status=%s", server_id, method, response.status_code)
        raise ValueError("MCP-сервер вернул пустой SSE-ответ")
    return response.json()


def _initialize_session(server_id: str) -> dict[str, Any]:
    """Создаёт новую MCP-сессию и сохраняет session id/protocol version."""
    result = request(server_id, "initialize", {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "NovaMind", "version": "1.0"},
    }, _retry_session=False)
    data = _read()
    item = data.get(server_id)
    if item and "error" not in result:
        body = _result_body(result)
        item["connected"] = True
        item["initialized"] = True
        item["protocol_version"] = body.get("protocolVersion", "2025-06-18")
        item["last_error"] = None
        item["updated_at"] = int(time.time())
        _write(data)
    return result


def _result_body(response: dict[str, Any]) -> dict[str, Any]:
    """Возвращает result MCP, сохраняя совместимость с JSON-RPC-ответом."""
    return response.get("result", response) if isinstance(response, dict) else {}


def list_tools(server_id: str) -> list[dict[str, Any]]:
    """Получает инструменты подключённого MCP-сервера."""
    body = _result_body(request(server_id, "tools/list"))
    tools = body.get("tools", []) if isinstance(body, dict) else []
    return [tool for tool in tools if isinstance(tool, dict) and tool.get("name")]


def connected_tools() -> list[dict[str, Any]]:
    """Собирает инструменты всех подключённых MCP-серверов."""
    result = []
    for server_id, item in _read().items():
        if not item.get("connected"):
            continue
        try:
            for tool in list_tools(server_id):
                result.append({
                    "server_id": server_id,
                    "server_name": item.get("name", server_id),
                    "name": str(tool.get("name") or ""),
                    "description": tool.get("description", ""),
                    "inputSchema": tool.get("inputSchema") or {"type": "object"},
                })
        except Exception as exc:
            status = getattr(exc, "status_code", None) or getattr(getattr(exc, "response", None), "status_code", None)
            log.warning("MCP не удалось получить список инструментов [%s] status=%s: %s",
                        server_id, status or "error", exc)
            item["last_error"] = f"{exc.__class__.__name__}: {exc}"
    return [entry for entry in result if entry["name"]]


def resolve_server_id(server_id: str, tool_name: str = "",
                      tools: list[dict[str, Any]] | None = None) -> str:
    """Находит реальный ``server_id`` по ID, имени сервера, URL или уникальному имени инструмента.

    Когда пользователь подключает сервер через панель Настроек, его ``id`` может
    быть случайным (``xK9_mP2q``), а имя — «GitHub MCP». Если модель вызовет
    ``"server_id": "github"`` или опустит ``server_id`` при единственном сервере
    с этим инструментом, вызов автоматически попадёт в нужное подключение.
    """
    raw = str(server_id or "").strip()
    wanted = raw.lower()
    tname = str(tool_name or "").strip()
    catalog_list = [t for t in (tools or []) if isinstance(t, dict)]

    if catalog_list:
        by_server: dict[str, dict[str, Any]] = {}
        for entry in catalog_list:
            sid = str(entry.get("server_id") or entry.get("server") or "").strip()
            if not sid:
                continue
            info = by_server.setdefault(sid, {
                "name": str(entry.get("server_name") or sid).strip(),
                "tools": set(),
            })
            en = str(entry.get("name") or entry.get("tool") or "").strip()
            if en:
                info["tools"].add(en)

        if raw and raw in by_server:
            return raw

        if len(wanted) >= 2:
            for sid, info in by_server.items():
                sname = info["name"].lower()
                if wanted == sid.lower() or wanted == sname:
                    return sid
            for sid, info in by_server.items():
                sname = info["name"].lower()
                if wanted in sname or sname in wanted or wanted in sid.lower():
                    if not tname or not info["tools"] or tname in info["tools"]:
                        return sid

        if tname:
            owners = [sid for sid, info in by_server.items() if tname in info["tools"]]
            if len(owners) == 1:
                return owners[0]

    data = _read()
    if raw and raw in data:
        return raw
    if len(wanted) >= 2 and data:
        for sid, item in data.items():
            if not item.get("connected"):
                continue
            sname = str(item.get("name") or "").lower()
            surl = str(item.get("url") or "").lower()
            if wanted == sid.lower() or wanted == sname or wanted in sname or wanted in surl:
                return sid
    return raw


def format_tools_summary(tools: list[dict[str, Any]], max_tools: int = 40) -> str:
    """Компактное описание подключённых MCP-серверов и их инструментов для системного промпта."""
    valid = [t for t in (tools or []) if isinstance(t, dict) and (t.get("name") or t.get("tool"))]
    if not valid:
        return "пусто"
    entries: list[str] = []
    for item in valid[:max_tools]:
        sid = str(item.get("server_id") or item.get("server") or "?").strip()
        sname = str(item.get("server_name") or "").strip()
        tname = str(item.get("name") or item.get("tool") or "?").strip()
        schema = item.get("inputSchema") if isinstance(item.get("inputSchema"), dict) else {}
        props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        param_names = list(props.keys())[:6]
        desc = re.sub(r"\s+", " ", str(item.get("description") or "")).strip()[:90]
        head = f"{sid}:{tname}"
        if sname and sname.lower() != sid.lower():
            head += f" [{sname}]"
        if param_names:
            head += f"({', '.join(param_names)})"
        if desc:
            head += f" — {desc}"
        entries.append(head)
    return "; ".join(entries)


def call_tool(server_id: str, tool_name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Вызывает MCP tools/call с паузой/повтором при 403/429 и логированием ошибок."""
    server_id = resolve_server_id(server_id, tool_name)
    max_retries = max(0, min(int(os.getenv("MCP_MAX_RETRIES", str(MAX_RETRIES))), 5))
    for attempt in range(max_retries + 1):
        response = request(server_id, "tools/call", {
            "name": tool_name,
            "arguments": arguments or {},
        })
        if isinstance(response, dict) and response.get("error"):
            rpc_err = response["error"]
            rpc_code = rpc_err.get("code") if isinstance(rpc_err, dict) else None
            err_text = _extract_error_text(rpc_err) or "MCP JSON-RPC error"
            http_code = _detect_http_status(err_text)
            if http_code in RETRY_STATUS_CODES and attempt < max_retries:
                delay = _retry_delay(attempt)
                log.warning("MCP JSON-RPC ошибка [%s:%s] status=%s: пауза %.1f с и повтор (%d/%d): %s",
                            server_id, tool_name, http_code, delay, attempt + 1, max_retries, err_text[:240])
                if delay > 0:
                    time.sleep(delay)
                continue
            status_val = http_code or rpc_code or "rpc_error"
            log.error("MCP JSON-RPC ошибка [%s:%s] status=%s: %s",
                      server_id, tool_name, status_val, err_text[:500])
            raise McpError(err_text, status_code=http_code, logged=True)

        body = _result_body(response)
        if isinstance(body, dict) and body.get("isError"):
            err_text = _extract_error_text(body.get("content")) or "MCP-сервер вернул ошибку без описания"
            http_code = _detect_http_status(err_text)
            if http_code in RETRY_STATUS_CODES and attempt < max_retries:
                delay = _retry_delay(attempt)
                log.warning("MCP инструмент [%s:%s] вернул код %s (status=%s): пауза %.1f с и повтор (%d/%d): %s",
                            server_id, tool_name, http_code, http_code, delay, attempt + 1, max_retries, err_text[:240])
                if delay > 0:
                    time.sleep(delay)
                continue
            log.error("MCP ошибка инструмента [%s:%s] status=%s: %s",
                      server_id, tool_name, http_code or "tool_error", err_text[:500])
            msg = f"MCP HTTP {http_code}: {err_text}" if http_code and f"{http_code}" not in err_text else err_text
            raise McpError(msg, status_code=http_code, logged=True)
        return body if isinstance(body, dict) else {"content": body}
    return {}


def test_connection(server_id: str) -> dict[str, Any]:
    data = _read()
    item = data.get(server_id)
    if not item:
        raise ValueError("MCP-сервер не найден")
    try:
        result = _initialize_session(server_id)
        data[server_id]["connected"] = "error" not in result
        data[server_id]["last_error"] = None if data[server_id]["connected"] else str(result.get("error"))
        if data[server_id]["connected"]:
            body = _result_body(result)
            data[server_id]["initialized"] = True
            data[server_id]["protocol_version"] = body.get("protocolVersion", "2025-06-18")
        data[server_id]["updated_at"] = int(time.time())
        _write(data)
        # MCP requires this notification after a successful initialize.
        try:
            request(server_id, "notifications/initialized", {})
        except Exception:
            # Notification is not needed for stateless/simple local servers.
            pass
        return {"connected": data[server_id]["connected"], "server": result.get("result", result)}
    except Exception as exc:
        status = getattr(exc, "status_code", None) or getattr(getattr(exc, "response", None), "status_code", None)
        log.error("MCP ошибка проверки подключения [%s] status=%s: %s",
                  server_id, status or "error", exc)
        data[server_id]["connected"] = False
        data[server_id]["last_error"] = f"{exc.__class__.__name__}: {exc}"
        data[server_id]["updated_at"] = int(time.time())
        _write(data)
        return {"connected": False, "error": data[server_id]["last_error"]}
