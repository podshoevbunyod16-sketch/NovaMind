"""MCP catalog and small Streamable HTTP client for NovaMind settings.

Secrets are kept server-side in a chmod 600 JSON file. The browser receives
only connection metadata and masked status, never API keys or OAuth tokens.
"""
from __future__ import annotations

import json
import os
import secrets
import stat
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

ROOT = Path(__file__).resolve().parent
STORE = Path(os.getenv("MCP_CONNECTIONS_FILE", ROOT / "mcp_connections.json"))
TIMEOUT = float(os.getenv("MCP_TIMEOUT", "12"))

CATALOG = [
    {"id": "github", "name": "GitHub", "description": "Репозитории, issues и pull requests", "auth": ["api_key", "oauth"]},
    {"id": "google-drive", "name": "Google Drive", "description": "Поиск и чтение файлов Drive", "auth": ["google_oauth", "api_key"]},
    {"id": "filesystem", "name": "Filesystem", "description": "Локальные файлы через MCP", "auth": ["none"]},
    {"id": "custom", "name": "Свой MCP-сервер", "description": "Любой Streamable HTTP MCP endpoint", "auth": ["api_key", "bearer", "basic", "oauth", "none"]},
]


def _read() -> dict[str, dict[str, Any]]:
    try:
        return json.loads(STORE.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError, OSError):
        return {}


def _write(data: dict[str, dict[str, Any]]) -> None:
    STORE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STORE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
    tmp.replace(STORE)


def catalog() -> list[dict[str, Any]]:
    return CATALOG


def public_connections() -> list[dict[str, Any]]:
    return [{"id": key, "name": value.get("name", key), "url": value.get("url", ""),
             "auth": value.get("auth", "none"), "connected": bool(value.get("connected")),
             "last_error": value.get("last_error"), "updated_at": value.get("updated_at")}
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
    response = requests.post(item["url"], headers=_headers(item), json=payload, timeout=TIMEOUT)
    session_id = response.headers.get("mcp-session-id")
    if session_id:
        data[server_id]["session_id"] = session_id
        _write(data)
    if response.status_code >= 400:
        detail = (response.text or "").strip().replace("\n", " ")[:600]
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
                raise requests.HTTPError(
                    f"MCP reinitialize failed: {initialized.get('error')}", response=response)
            try:
                request(server_id, "notifications/initialized", {}, _retry_session=False)
            except Exception:
                pass
            return request(server_id, method, params, _retry_session=False)
        raise requests.HTTPError(
            f"MCP HTTP {response.status_code}: {detail or 'сервер не сообщил причину'}",
            response=response,
        )
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
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "inputSchema": tool.get("inputSchema") or {"type": "object"},
                })
        except Exception as exc:
            item["last_error"] = f"{exc.__class__.__name__}: {exc}"
    return result


def call_tool(server_id: str, tool_name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Вызывает MCP tools/call и возвращает структурированный результат."""
    response = request(server_id, "tools/call", {
        "name": tool_name,
        "arguments": arguments or {},
    })
    body = _result_body(response)
    if isinstance(body, dict) and body.get("isError"):
        raise RuntimeError(str(body.get("content") or "MCP tool returned an error"))
    return body if isinstance(body, dict) else {"content": body}


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
        data[server_id]["connected"] = False
        data[server_id]["last_error"] = f"{exc.__class__.__name__}: {exc}"
        data[server_id]["updated_at"] = int(time.time())
        _write(data)
        return {"connected": False, "error": data[server_id]["last_error"]}
