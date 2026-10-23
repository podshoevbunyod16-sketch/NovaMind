"""Agent workspace and runtime directory resolution.

Adapted from AgenticSeek ``sources/workspace.py`` (GPL-3.0, see NOTICE.md).

NovaMind already owns a sandbox: ``routes/terminal.py`` confines every command
to ``TERMINAL_DIR`` (default ``<repo>/workspace``).  Agents must work in that
same directory, otherwise the web UI file browser and the agent would see
different trees.  Instead of inventing a second workspace we resolve the same
one here, without importing Flask:

* ``AGEANTICSEEK_WORK_DIR`` / ``WORK_DIR`` — explicit override;
* ``TERMINAL_DIR`` — the NovaMind sandbox variable;
* ``<repo>/workspace`` — NovaMind default.

Runtime data (logs, sessions, screenshots) lives in ``AGENT_RUNTIME_DIR``
(default ``<repo>/.agent-data``) so it never mixes with user files.
"""

from __future__ import annotations

import os

DEFAULT_RUNTIME_DIR = ".agent-data"
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

#: Exposed for tests and for the engine: the directory agents may touch.
_WORK_DIR: str | None = None


def get_runtime_dir() -> str:
    """Directory for backend runtime files (logs, screenshots, sessions)."""
    path = os.getenv("AGENT_RUNTIME_DIR", DEFAULT_RUNTIME_DIR).strip() or DEFAULT_RUNTIME_DIR
    if not os.path.isabs(path):
        path = os.path.join(_REPO_ROOT, path)
    return os.path.realpath(os.path.abspath(path))


def runtime_subdir(name: str) -> str:
    """Create (if needed) and return a sub directory of the runtime dir."""
    path = os.path.join(get_runtime_dir(), name)
    os.makedirs(path, exist_ok=True)
    return path


def get_work_dir() -> str:
    """Directory where agents may read, write, and execute code."""
    global _WORK_DIR
    if _WORK_DIR:
        return _WORK_DIR
    path = (os.getenv("AGENTICSEEK_WORK_DIR", "").strip()
            or os.getenv("WORK_DIR", "").strip()
            or os.getenv("TERMINAL_DIR", "").strip())
    if not path:
        path = os.path.join(_REPO_ROOT, "workspace")
    return os.path.realpath(os.path.abspath(path))


def set_work_dir(path: str) -> str:
    """Pin the workspace for the current process (used by Flask setup and tests)."""
    global _WORK_DIR
    _WORK_DIR = os.path.realpath(os.path.abspath(path)) if path else None
    return get_work_dir()


def ensure_directory(path: str) -> str:
    os.makedirs(path, exist_ok=True)
    return path


def ensure_work_dir() -> str:
    return ensure_directory(get_work_dir())


def is_within_directory(path: str, directory: str) -> bool:
    """Return True when path resolves inside directory (no traversal escape)."""
    try:
        path_real = os.path.realpath(os.path.abspath(path))
        dir_real = os.path.realpath(os.path.abspath(directory))
        return os.path.commonpath([path_real, dir_real]) == dir_real
    except ValueError:
        return False


def resolve_workspace_path(path: str, work_dir: str | None = None) -> str:
    """Resolve a user or model-provided path inside the agent workspace.

    Raises:
        ValueError: empty path
        PermissionError: resolved path escapes the workspace
    """
    if path is None or not str(path).strip():
        raise ValueError("Empty path")

    base = work_dir or get_work_dir()
    candidate = str(path).strip()
    if os.path.isabs(candidate):
        resolved = os.path.realpath(candidate)
    else:
        resolved = os.path.realpath(os.path.join(base, candidate))

    if not is_within_directory(resolved, base):
        raise PermissionError(
            f"Path '{path}' is outside the agent workspace ({base})"
        )
    return resolved
