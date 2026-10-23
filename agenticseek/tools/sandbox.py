"""Bridge between agent tools and the NovaMind sandbox.

AgenticSeek executes generated code with ``subprocess`` inside a workspace and,
when it runs as root in Docker, drops privileges to an unprivileged uid
(upstream ``sources/tools/sandbox.py``). NovaMind already has a stricter
boundary in ``routes/terminal.py``:

* a command allow-list and a forbidden-substring list;
* one command per call, pipes and redirects parsed and validated;
* path validation for every argument (no absolute paths, no ``..``);
* a scrubbed environment (no API keys);
* a process group killed on timeout or on the Stop button;
* an output cap.

So instead of vendoring a second, weaker isolation layer, the tools call the
NovaMind runner. The runner is injected by ``routes/agent.py`` (which imports
``routes.terminal``); the tools never spawn a process themselves, which also
keeps the agent testable — a test passes a fake runner.

Failure is closed: with no runner registered, the tools refuse to execute.
"""

from __future__ import annotations

from typing import Callable, Optional

from agenticseek.core.errors import AgenticSeekError

#: ``runner(command: str, timeout: float) -> dict`` with keys
#: ``code`` (int), ``stdout`` (str), ``stderr`` (str), plus the optional
#: ``duration_ms``, ``timed_out`` and ``cancelled`` flags of ``routes.terminal``.
_runner: Optional[Callable[..., dict]] = None


def set_runner(runner: Optional[Callable[..., dict]]) -> None:
    """Register (or clear) the sandbox entry point for this process."""
    global _runner
    _runner = runner


def get_runner() -> Optional[Callable[..., dict]]:
    return _runner


def have_runner() -> bool:
    return _runner is not None


def run(command: str, timeout: float = 12.0) -> dict:
    """Execute one command through the NovaMind sandbox.

    Returns:
        dict: normalised result with ``code``, ``stdout``, ``stderr``.

    Raises:
        AgenticSeekError: when no sandbox is configured or the command was
            rejected by the sandbox before execution.
    """
    command = str(command or "").strip()
    if not command:
        raise AgenticSeekError("Empty command", "Пустая команда — нечего выполнять.")
    if _runner is None:
        raise AgenticSeekError(
            "Sandbox is not available",
            "Выполнение команд недоступно: песочница не подключена.",
            causes=["agenticseek.tools.sandbox.set_runner() was never called."],
            fixes=["Запустите агента через NovaMind (routes/agent.py подключает песочницу)."])
    try:
        result = _runner(command, timeout=timeout)
    except TypeError:
        # Older callables accept only the command.
        result = _runner(command)
    except (ValueError, RuntimeError) as error:
        raise AgenticSeekError(str(error), f"Команда запрещена песочницей: {error}") from error
    if not isinstance(result, dict):
        return {"code": 0 if result else 1, "stdout": str(result or ""), "stderr": ""}
    return {
        "code": int(result.get("code") or 0),
        "stdout": str(result.get("stdout") or ""),
        "stderr": str(result.get("stderr") or ""),
        "timed_out": bool(result.get("timed_out")),
        "cancelled": bool(result.get("cancelled")),
        "duration_ms": result.get("duration_ms"),
    }
