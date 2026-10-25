"""Logging for the vendored agent code.

Derived from AgenticSeek ``sources/logger.py`` (GPL-3.0, see NOTICE.md).
Upstream writes one file per agent into ``logs/`` and installs a global
``sys.excepthook``; a Flask server already has logging configured, so this
version reuses it and only adds a per-agent file under the NovaMind runtime
directory when a log directory is available.

Log rules for this project (see docs/AGENTICSEEK_INTEGRATION.md):
  * never log API keys, cookies, session tokens or full tool payloads;
  * tool payloads are truncated to ``LOG_PAYLOAD_LIMIT`` characters;
  * errors are logged with the exception type, not its arguments.
"""

from __future__ import annotations

import logging
import os

LOG_PAYLOAD_LIMIT = 800
_handlers_ready = set()


def runtime_log_dir() -> str:
    """Directory for agent logs (``AGENT_RUNTIME_DIR/logs`` by default)."""
    base = os.getenv("AGENT_RUNTIME_DIR", "").strip()
    if not base:
        base = os.path.join(os.getcwd(), ".agent-data")
    elif not os.path.isabs(base):
        base = os.path.join(os.getcwd(), base)
    return os.path.join(base, "logs")


def get_logger(name: str = "agenticseek") -> logging.Logger:
    logger = logging.getLogger(name)
    if name in _handlers_ready:
        return logger
    try:
        directory = runtime_log_dir()
        os.makedirs(directory, exist_ok=True)
        handler = logging.FileHandler(os.path.join(directory, f"{name.split('.')[-1]}.log"),
                                      mode="a", encoding="utf-8")
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s [%(name)s] %(message)s"))
        logger.addHandler(handler)
        logger.propagate = True
    except OSError:
        # Read-only filesystem (some Termux setups): the console logger is enough.
        pass
    logger.setLevel(os.getenv("AGENTICSEEK_LOG_LEVEL", "INFO").upper())
    _handlers_ready.add(name)
    return logger


class Logger:
    """Drop-in replacement for the upstream ``Logger`` class.

    Upstream: ``Logger("agent.log").info("…")``. Here the argument names the
    logger instead of a file, and the file lives in the runtime directory.
    """

    def __init__(self, name: str = "agent.log", append: bool = True):
        self.logger = get_logger(f"agenticseek.{str(name).replace('.log', '')}")

    def info(self, message) -> None:
        self.logger.info(_clip(message, LOG_PAYLOAD_LIMIT))

    def warning(self, message) -> None:
        self.logger.warning(_clip(message, LOG_PAYLOAD_LIMIT))

    def error(self, message) -> None:
        self.logger.error(_clip(message, LOG_PAYLOAD_LIMIT))

    def debug(self, message) -> None:
        self.logger.debug(_clip(message, LOG_PAYLOAD_LIMIT))

    def success(self, message) -> None:
        self.logger.info(_clip(message, LOG_PAYLOAD_LIMIT))


def _clip(value, limit: int) -> str:
    text = "" if value is None else str(value)
    return text if len(text) <= limit else text[:limit] + "…"


def install_excepthook() -> None:
    """Kept for API compatibility: Flask already handles uncaught exceptions."""
    return None
