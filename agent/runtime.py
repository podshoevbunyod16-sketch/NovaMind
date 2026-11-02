"""Small request helpers shared by NovaMind's first-party agent routes."""
from __future__ import annotations

import os


def is_enabled() -> bool:
    """Agent endpoints are enabled by default; only an explicit 0 disables them."""
    value = os.getenv("AGENT_ENABLED")
    return value is None or not str(value).strip() or str(value).strip() == "1"


def parse_bool(value, default=True) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "да", "вкл"}:
        return True
    if text in {"0", "false", "no", "off", "нет", "выкл", ""}:
        return False
    return default


def request_tools_allowed(data) -> bool:
    """Respect the explicit browser opt-in while keeping API clients enabled by default."""
    for key in ("tools", "auto_search", "search"):
        if key in (data or {}):
            return parse_bool(data[key], default=True)
    return True


def bind_client_chat(data) -> bool:
    """Bind an HTTP session to an owned chat; null/empty starts an unbound chat."""
    if not isinstance(data, dict) or "chat_id" not in data:
        return True
    from flask import session
    import database

    chat_id = str(data.get("chat_id") or "").strip()
    if not chat_id:
        session.pop("chat_id", None)
        return True
    if not database.chat_allowed(chat_id):
        return False
    session["chat_id"] = chat_id
    return True
