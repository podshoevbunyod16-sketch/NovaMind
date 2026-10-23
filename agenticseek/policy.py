"""Tool policy: risk, confirmations, untrusted data, action logging.

This is the NovaMind security layer around the AgenticSeek tools. It decides
which tool calls may run unattended and which need the user's explicit "да" in
the chat, and it is the only place that answers that question — the old
``routes/agent.py`` had its own copy of this logic; it now delegates here.

Levels
------
``safe``
    Read-only: search, page reading, file lookup/reading, memory recall.
``write``
    Changes something the user can see: running a command in the sandbox,
    writing a file, generating media, creating a task. Allowed by default
    (the sandbox still enforces its own limits); ``AGENT_CONFIRM=write`` makes
    every one of them ask first.
``danger``
    Irreversible or outward-facing: destructive shell commands, ``git push``,
    package installation, writes through MCP tools, scheduling. Always asks
    first (``AGENT_CONFIRM=off`` disables the question and is documented as
    unsafe).

Nothing here is a sandbox: ``routes/terminal.py`` remains the execution
boundary. This module decides *whether a human said yes*, and it fails closed
(no answer within the timeout = refused).
"""

from __future__ import annotations

import logging
import re
import threading
import time
import uuid
from typing import Optional

log = logging.getLogger("agenticseek.policy")

RISK_SAFE = "safe"
RISK_WRITE = "write"
RISK_DANGER = "danger"

CONFIRM_MODES = ("off", "write", "danger")
CONFIRM_TIMEOUT = float(__import__("os").getenv("AGENT_CONFIRM_TIMEOUT", "120"))


def confirm_timeout() -> float:
    """How long the agent waits for the user (read on every call)."""
    import os
    try:
        return max(0.01, float(os.getenv("AGENT_CONFIRM_TIMEOUT") or CONFIRM_TIMEOUT))
    except ValueError:
        return CONFIRM_TIMEOUT

#: Commands that must never run without a human decision.
_DANGER_PATTERNS = (
    (r"\brm\b\s+(-[a-z]*\s+)*/[^/\s]", "удаление от корня"),
    (r"\brm\b\s+-[a-z]*r", "рекурсивное удаление"),
    (r"\bgit\s+push\b", "публикация в удалённый репозиторий"),
    (r"\bgit\s+reset\s+--hard\b", "потеря незакоммиченных изменений"),
    (r"\bgit\s+clean\b", "удаление неотслеживаемых файлов"),
    (r"\b(pip|pip3|npm|apt|apt-get|pkg)\b\s+(install|add|remove|uninstall|update|upgrade)\b",
     "установка или удаление пакетов"),
    (r"\b(curl|wget)\b[^\n]*(--data|-d\s|-X\s*(POST|PUT|DELETE)|--upload-file)",
     "сетевой запрос, меняющий данные"),
    (r"\bchmod\b\s+777", "открытие прав всем"),
    (r"\bdd\b|\bmkfs\b|\bshutdown\b|\breboot\b|\bhalt\b", "системная операция"),
    (r">\s*/dev/", "запись в устройство"),
    (r"\bssh-keygen\b|\bssh\b|\bscp\b", "доступ к другому хосту"),
)

#: MCP tool names that change state on the remote service.
_DANGER_TOOLS = ("delete", "remove", "destroy", "send", "post", "publish", "merge",
                 "close", "archive", "transfer", "pay")
_WRITE_TOOLS = ("create", "update", "write", "comment", "invite", "upload", "label",
                "assign", "move", "rename")

_SECRET_PATTERN = re.compile(
    r"(?i)\b(api[_-]?key|token|secret|password|passwd|authorization|bearer|session)"
    r"\s*[:=]?\s*(?:bearer\s+)?[\"']?([A-Za-z0-9_\-.:/+=]{6,})")


# ── risk ──────────────────────────────────────────────────────────────────
def risk_of(tool, payload: str = "") -> str:
    """Risk level of ``tool`` with this ``payload``."""
    level = getattr(tool, "risk", RISK_WRITE) if not isinstance(tool, str) else RISK_WRITE
    dynamic = getattr(tool, "risk_for", None) if not isinstance(tool, str) else None
    if callable(dynamic):
        try:
            level = dynamic(payload)
        except Exception:  # noqa: BLE001 — a broken risk call is a danger sign
            log.warning("risk_for failed; treating the call as dangerous")
            level = RISK_DANGER
    tag = getattr(tool, "tag", str(tool)) if not isinstance(tool, str) else tool
    text = str(payload or "")

    if tag == "bash":
        for pattern, _reason in _DANGER_PATTERNS:
            if re.search(pattern, text, re.I):
                return RISK_DANGER
    if tag == "file_finder":
        return RISK_SAFE
    if tag in ("web_search", "web_open", "recall"):
        return RISK_SAFE
    if tag == "mcp_call":
        name = str(text or "").lower()
        # Любой инструмент, меняющий состояние на сервисе, спрашивает разрешение:
        # «создать issue» так же необратимо, как «удалить» — это видит третья сторона.
        if any(hint in name for hint in _DANGER_TOOLS + _WRITE_TOOLS):
            return RISK_DANGER
        return RISK_SAFE
    if level not in (RISK_SAFE, RISK_WRITE, RISK_DANGER):
        return RISK_WRITE
    return level


def describe_action(tool, payload: str = "") -> str:
    """Short, secret-free description of a tool call for the confirmation dialog."""
    tag = getattr(tool, "tag", str(tool)) if not isinstance(tool, str) else tool
    text = redact(str(payload or "")).strip()
    first = next((line.strip() for line in text.splitlines() if line.strip()), "")
    return f"{tag}: {first[:180]}" if first else tag


def redact(text: str) -> str:
    """Remove token-like values from anything that goes to a log or the UI."""
    return _SECRET_PATTERN.sub(lambda match: f"{match.group(1)}=***", str(text or ""))


# ── confirmations ─────────────────────────────────────────────────────────
class _Pending:
    __slots__ = ("id", "tool", "description", "risk", "created", "event", "approved",
                 "resolved", "key")

    def __init__(self, confirmation_id: str, tool: str, description: str, risk: str,
                 key=("", "")):
        self.id = confirmation_id
        self.tool = tool
        self.description = description
        self.risk = risk
        self.key = key
        self.created = time.time()
        self.event = threading.Event()
        self.approved: Optional[bool] = None
        self.resolved = False


_LOCK = threading.RLock()
_PENDING: dict[str, _Pending] = {}
_ALWAYS: dict[tuple, float] = {}
_MODE = None


def confirm_mode() -> str:
    """``off`` | ``write`` | ``danger`` from ``AGENT_CONFIRM`` (default danger)."""
    global _MODE
    if _MODE is None:
        import os
        mode = str(os.getenv("AGENT_CONFIRM", "danger")).strip().lower()
        _MODE = mode if mode in CONFIRM_MODES else "danger"
    return _MODE


def reset_cache() -> None:
    """Forget cached settings (tests change the environment between cases)."""
    global _MODE
    _MODE = None


def action_key(tool, payload: str) -> tuple:
    """Key for "always allow this kind of action"."""
    tag = getattr(tool, "tag", str(tool)) if not isinstance(tool, str) else tool
    first = next((line.strip() for line in str(payload or "").splitlines() if line.strip()), "")
    # Первые три слова: «git push origin» покрывает другую ветку, но не «rm -rf build».
    words = first.split()[:3]
    return (tag, " ".join(words)[:80])


def always_allowed(tool, payload: str) -> bool:
    with _LOCK:
        return action_key(tool, payload) in _ALWAYS


def mark_always_allowed(tool, payload: str) -> None:
    with _LOCK:
        _ALWAYS[action_key(tool, payload)] = time.time()


def forget_always_allowed(tool=None, payload=None) -> None:
    """Clear one rule, or every rule when called without arguments."""
    with _LOCK:
        if tool is None:
            _ALWAYS.clear()
            return
        _ALWAYS.pop(action_key(tool, payload or ""), None)


def always_rules() -> list[dict]:
    with _LOCK:
        return [{"tool": key[0], "match": key[1]} for key in _ALWAYS]


def needs_confirmation(tool, payload: str = "") -> bool:
    """Whether this call must be approved by the user first."""
    mode = confirm_mode()
    if mode == "off":
        return False
    risk = risk_of(tool, payload)
    if risk == RISK_SAFE:
        return False
    if always_allowed(tool, payload):
        return False              # пользователь уже разрешил именно это действие
    if risk == RISK_DANGER:
        return True
    return mode == "write"


def confirmation_needed(tool, payload: str = "") -> bool:
    """Kept name from the previous engine (same semantics)."""
    return needs_confirmation(tool, payload)


def register_confirmation(tool, payload: str = "") -> dict:
    """Create a pending confirmation; the caller then awaits it."""
    tag = getattr(tool, "tag", str(tool)) if not isinstance(tool, str) else tool
    record = _Pending(uuid.uuid4().hex[:12], tag, describe_action(tool, payload),
                      risk_of(tool, payload), action_key(tool, payload))
    with _LOCK:
        _PENDING[record.id] = record
        # Drop stale entries so the dict cannot grow forever.
        for key, item in list(_PENDING.items()):
            if time.time() - item.created > confirm_timeout() and not item.resolved:
                item.approved = False
                item.resolved = True
                item.event.set()
                _PENDING.pop(key, None)
    log.info("confirmation requested: %s (%s)", record.description[:160], record.risk)
    return {"id": record.id, "tool": tag, "description": record.description,
            "risk": record.risk}


def resolve_confirmation(confirmation_id: str, approve: bool, always: bool = False):
    """Answer a pending confirmation. Returns ``(ok, reason)``."""
    with _LOCK:
        record = _PENDING.get(str(confirmation_id or ""))
        if record is None:
            return False, "not_found"
        if record.resolved:
            return False, "already_resolved"
        record.approved = bool(approve)
        record.resolved = True
        record.event.set()
        if approve and always:
            _ALWAYS[record.key] = time.time()
    return True, "ok"


def await_confirmation(record: dict, timeout: Optional[float] = None) -> bool:
    """Wait for the user's answer. Timeout or cancellation means "no"."""
    with _LOCK:
        pending = _PENDING.get(str((record or {}).get("id") or ""))
    if pending is None:
        return False
    pending.event.wait(timeout if timeout is not None else confirm_timeout())
    with _LOCK:
        approved = bool(pending.approved) if pending.resolved else False
        if not pending.resolved:
            pending.resolved = True
            pending.approved = False
        _PENDING.pop(pending.id, None)
    return approved


def pending_count() -> int:
    with _LOCK:
        return sum(1 for item in _PENDING.values() if not item.resolved)


def reset_confirmations() -> None:
    """Cancel every pending confirmation (used by tests and on shutdown)."""
    with _LOCK:
        for item in _PENDING.values():
            item.approved = False
            item.resolved = True
            item.event.set()
        _PENDING.clear()


# ── untrusted content ─────────────────────────────────────────────────────
UNTRUSTED_TOOLS = {"web_search", "web_open", "file_finder", "mcp_call", "bash", "python",
                   "image", "audio", "video", "media"}


def wrap_untrusted(tool: str, text: str) -> str:
    """Mark external text as data, so a page cannot issue instructions.

    The closing tag is neutralised inside the payload: a page cannot "escape"
    the wrapper and continue as if it were the system prompt.
    """
    tag = getattr(tool, "tag", str(tool)) if not isinstance(tool, str) else tool
    if tag not in UNTRUSTED_TOOLS:
        return str(text or "")
    body = str(text or "").replace("</untrusted>", "<\\/untrusted>")
    return f'<untrusted tool="{tag}">\n{body}\n</untrusted>'


def log_action(tool, payload: str, ok: bool, duration_ms: int = 0) -> None:
    """One structured line per executed action, without secrets or full payloads."""
    tag = getattr(tool, "tag", str(tool)) if not isinstance(tool, str) else tool
    log.info("tool=%s ok=%s ms=%s payload=%r", tag, ok, duration_ms,
             clip(redact(str(payload or "")), 160))


def clip(text: str, limit: int) -> str:
    text = str(text or "")
    return text if len(text) <= limit else text[:limit] + "…"
