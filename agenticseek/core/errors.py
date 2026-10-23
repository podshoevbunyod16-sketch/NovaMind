"""Error type for the agent engine.

Derived from AgenticSeek ``sources/errors.py`` (GPL-3.0, see NOTICE.md).
Upstream attaches a long list of troubleshooting hints and prints a colourised
report to the terminal; NovaMind keeps the structured parts and drops the
terminal formatting, because the message is shown in the web UI and written to
the log instead.

``AgenticSeekError`` deliberately carries an English ``message`` (log/diagnostic
layer) plus an optional ``user_message`` in Russian, which the route may show to
the user. Agent code should raise this only for failures the user must know
about; tool errors are returned as ``[failure]`` feedback to the model instead.
"""

from __future__ import annotations

from typing import Iterable


class AgenticSeekError(Exception):
    """A failure the engine reports instead of fabricating a result."""

    def __init__(self, message: str, user_message: str = "",
                 causes: Iterable[str] | None = None,
                 fixes: Iterable[str] | None = None):
        super().__init__(message)
        self.message = str(message or "Unknown agent error")
        self.user_message = str(user_message or "")
        self.causes = [str(item) for item in (causes or []) if str(item).strip()]
        self.fixes = [str(item) for item in (fixes or []) if str(item).strip()]

    def summary(self) -> str:
        """One-line description for logs and the ``error`` event."""
        parts = [self.message]
        if self.causes:
            parts.append("Причины: " + "; ".join(self.causes))
        if self.fixes:
            parts.append("Что сделать: " + "; ".join(self.fixes))
        return " | ".join(parts)

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.summary()


class AgentStopped(AgenticSeekError):
    """The run was cancelled, ran out of time or hit the step limit.

    NovaMind adapter: upstream would simply ``sys.exit``; a web request cannot
    do that, so the engine catches this, keeps whatever the agent already
    produced and reports the stop reason honestly.
    """

    def __init__(self, reason: str, user_message: str = ""):
        super().__init__(reason, user_message or _STOP_TEXT.get(reason, ""))
        self.reason = reason


_STOP_TEXT = {
    "cancelled": "Остановлено пользователем.",
    "timeout": "Время на выполнение вышло — показываю то, что успел.",
    "steps": "Закончился лимит шагов — показываю то, что успел.",
    "loop": "Обнаружено зацикливание — остановился, чтобы не тратить запросы.",
}


class StepLimitReached(AgentStopped):
    """The shared step budget of the run is exhausted."""

    def __init__(self):
        super().__init__("steps")


class WorkspaceError(AgenticSeekError):
    """A path escaped the agent workspace or is otherwise not allowed."""

    def __init__(self, message: str):
        super().__init__(message, "Путь вне рабочей папки — доступ запрещён.",
                         causes=[str(message)],
                         fixes=["Укажите путь внутри рабочей папки агента."])
