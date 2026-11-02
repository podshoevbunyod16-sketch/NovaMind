"""Per-owner, per-chat workspace with traversal and symlink containment checks."""
from __future__ import annotations

import hashlib
import os
import contextvars
from pathlib import Path

_ACTIVE_ROOT = contextvars.ContextVar("novamind_agent_workspace", default=None)


def bind_workspace(root: Path | None):
    return _ACTIVE_ROOT.set(root)


def reset_workspace(token):
    _ACTIVE_ROOT.reset(token)


def current_workspace() -> Path | None:
    return _ACTIVE_ROOT.get()


def workspace_root(owner: str, session_id: str) -> Path:
    base = Path(os.getenv("AGENT_WORKSPACE_ROOT", "workspace_data")).expanduser().resolve()
    owner_key = hashlib.sha256(str(owner or "anonymous").encode()).hexdigest()[:24]
    session_key = hashlib.sha256(str(session_id or "").encode()).hexdigest()[:24]
    root = (base / owner_key / session_key).resolve()
    if not root.is_relative_to(base):
        raise ValueError("Workspace path escaped its root")
    root.mkdir(parents=True, exist_ok=True)
    return root


def safe_path(root: Path, relative: str = "") -> Path:
    raw = str(relative or "")
    candidate = Path(raw)
    if candidate.is_absolute() or any(part == ".." for part in candidate.parts):
        raise ValueError("Недопустимый путь")
    resolved_root = root.resolve()
    cursor = resolved_root
    for part in candidate.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ValueError("Путь через символическую ссылку запрещён")
    target = (resolved_root / candidate).resolve()
    if not target.is_relative_to(resolved_root):
        raise ValueError("Путь выходит за пределы рабочей папки")
    return target


def tree(root: Path, *, max_items: int = 300) -> list[dict]:
    entries = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            continue
        try:
            rel = path.relative_to(root).as_posix()
        except ValueError:
            continue
        entries.append({"path": rel, "type": "directory" if path.is_dir() else "file",
                        "size": path.stat().st_size if path.is_file() else 0})
        if len(entries) >= max_items:
            break
    return entries
