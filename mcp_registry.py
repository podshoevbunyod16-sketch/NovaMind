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


def request(server_id: str, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    data = _read()
    item = data.get(server_id)
    if not item:
        raise ValueError("MCP-сервер не найден")
    if urlparse(item["url"]).hostname in {"localhost", "127.0.0.1", "::1"} or item["url"].startswith(("https://", "http://")):
        pass
    payload = {"jsonrpc": "2.0", "id": int(time.time() * 1000) % 1000000000, "method": method, "params": params or {}}
    response = requests.post(item["url"], headers=_headers(item), json=payload, timeout=TIMEOUT)
    response.raise_for_status()
    content_type = response.headers.get("content-type", "")
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


def test_connection(server_id: str) -> dict[str, Any]:
    data = _read()
    item = data.get(server_id)
    if not item:
        raise ValueError("MCP-сервер не найден")
    try:
        result = request(server_id, "initialize", {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "NovaMind", "version": "1.0"}})
        data[server_id]["connected"] = "error" not in result
        data[server_id]["last_error"] = None if data[server_id]["connected"] else str(result.get("error"))
        data[server_id]["updated_at"] = int(time.time())
        _write(data)
        return {"connected": data[server_id]["connected"], "server": result.get("result", result)}
    except Exception as exc:
        data[server_id]["connected"] = False
        data[server_id]["last_error"] = f"{exc.__class__.__name__}: {exc}"
        data[server_id]["updated_at"] = int(time.time())
        _write(data)
        return {"connected": False, "error": data[server_id]["last_error"]}
