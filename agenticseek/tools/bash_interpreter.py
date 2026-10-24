"""Shell tool (block tag ``bash``).

Derived from AgenticSeek ``sources/tools/bashInterpreter.py`` (GPL-3.0, see
NOTICE.md). The tag, the feedback format and the "one command at a time" rule
come from there.

NovaMind adaptations:

* execution goes through the NovaMind sandbox (:mod:`agenticseek.tools.sandbox`),
  not ``subprocess`` — the command allow-list, path checks, environment
  scrubbing and process-group kill already exist there;
* the upstream "safe mode" prompt is replaced by a hard refusal of commands
  that look destructive (``rm -rf``, ``curl | sh``, ``git push`` …);
* comment-only lines are dropped instead of being executed. Раньше блок
  ``# в корне проекта`` падал с ``код 2: Пустая команда`` и шаг агента терялся;
* slow commands (venv, pip/npm install, pytest, builds) get a longer timeout.
  На телефоне 12 секунд заканчивались SIGKILL, и модель видела только ``код -9``;
* timeouts, SIGKILL and cancellation are reported differently, with advice —
  «не повторяй ту же команду, сократи или разбей»;
* запуск веб-сервера (``flask run``, ``python3 app.py`` с ``app.run()``) не
  выполняется: он занимает терминал до таймаута вместо проверки. Модель
  получает подсказку проверять приложение через test client.
"""

from __future__ import annotations

import os
import signal

from agenticseek.core.agent import clip
from agenticseek.core.errors import AgenticSeekError
from agenticseek.tools import sandbox
from agenticseek.tools.gui_hint import gui_hint
from agenticseek.tools.safety import is_unsafe
from agenticseek.tools.shell_text import executable_lines, is_slow, server_hint
from agenticseek.tools.tools import Tools

DEFAULT_TIMEOUT = float(os.getenv("AGENTICSEEK_BASH_TIMEOUT", "12"))
#: Сколько ждём команду, которая заведомо долгая (venv, установка пакетов, сборка).
SLOW_TIMEOUT = float(os.getenv("AGENTICSEEK_SLOW_TIMEOUT", "180"))
#: Верхняя граница, чтобы одна команда не занимала сервер навсегда.
MAX_TIMEOUT = float(os.getenv("AGENTICSEEK_MAX_TIMEOUT", "300"))
MAX_LINES = 12

#: Подсказка на случай, когда на телефоне не хватило памяти или процесс убили.
_VENV_HINT = ("На Android/Termux `python -m venv` часто убивается системой: "
              "ставь пакеты без окружения — `pip install <пакет>`. Песочница "
              "запускает тот же Python, что и NovaMind, поэтому пакет будет "
              "виден приложению.")


def _timeout_for(command: str, base: float) -> float:
    """Долгим командам — увеличенный таймаут, но не больше MAX_TIMEOUT."""
    if not is_slow(command):
        return base
    return min(MAX_TIMEOUT, max(base, SLOW_TIMEOUT))


def _signal_name(code: int) -> str:
    """-9 → ``SIGKILL`` (если платформа знает такой сигнал)."""
    try:
        return signal.Signals(-int(code)).name
    except (ValueError, TypeError):
        return f"сигнал {-int(code)}"


def _failure(line: str, result: dict, timeout: float, text: str) -> str:
    """Понятный текст ошибки: таймаут, убийство сигналом или обычный код."""
    code = int(result.get("code") or 0)
    output = text or "(нет вывода)"
    if result.get("timed_out"):
        parts = [
            f"[failure] {line[:200]} → таймаут {timeout:.0f} c: процесс остановлен (SIGKILL).",
            f"Вывод до остановки:\n{clip(output, 2000)}",
            "Что делать: не повторяй ту же команду — сократи её "
            "(`| head -20`, `-q`, `--without-pip`), разбей на несколько шагов "
            "или подними лимит AGENTICSEEK_SLOW_TIMEOUT.",
        ]
        if "venv" in line.lower():
            parts.append(_VENV_HINT)
        return "\n".join(parts)
    if code < 0:
        parts = [
            f"[failure] {line[:200]} → процесс убит сигналом {_signal_name(code)} "
            f"(код {code}).",
            f"Вывод до остановки:\n{clip(output, 2000)}",
            "Обычно это нехватка памяти или внешний лимит, а не ошибка команды. "
            "Уменьши нагрузку или разбей работу на части.",
        ]
        if "venv" in line.lower():
            parts.append(_VENV_HINT)
        return "\n".join(parts)
    if code == 2 and "пустая команда" in output.lower():
        # Так песочница отвечает на блок из одних комментариев; после разбора
        # строк их здесь быть не должно.
        return (f"[failure] {line[:200]}: песочница не увидела команду. "
                "Напиши саму команду, без строк-комментариев.")
    message = f"[failure] {line[:200]} → код {code}:\n{clip(output, 3000)}"
    hint = gui_hint(output)
    return message + ("\n" + hint if hint else "")


def _rejected(line: str, error: Exception) -> str:
    """Отказ песочницы до запуска — с подсказкой, как переписать команду."""
    reason = (getattr(error, "user_message", "") or str(error)).strip()
    parts = [f"[failure] {line[:200]} → команда не запущена: {reason}"]
    low = reason.lower()
    if "путь" in low or "path" in low:
        parts.append("Правило: рабочая папка — единственное место, где можно "
                     "работать. Никаких абсолютных путей и «..» — только "
                     "относительные (`app.py`, `venv/bin/python`, `data/db.sqlite`).")
    return "\n".join(parts)


class BashTool(Tools):
    tag = "bash"
    name = "Bash"
    description = "Выполнить одну shell-команду в рабочей папке (без sudo, без сети наружу)."
    risk = "write"

    def __init__(self, timeout: float | None = None):
        super().__init__()
        self.timeout = max(1.0, timeout or DEFAULT_TIMEOUT)

    def execute(self, payload: str) -> str:
        # Комментарии и пустые строки — не команды: песочница отвечала на них
        # «Пустая команда», и агент терял шаг на пустом месте.
        lines = executable_lines(payload)
        if not lines:
            return ("[failure] В блоке bash нет ни одной команды — только комментарии "
                    "и пустые строки. Напиши саму команду, например:\n"
                    "```bash\nls -la\n```")
        if len(lines) > MAX_LINES:
            return (f"[failure] Слишком много команд в одном блоке ({len(lines)} > {MAX_LINES}). "
                    "Выполни их по одной.")
        outputs = []
        for line in lines:
            if is_unsafe(line):
                return (f"[failure] Команда отклонена как опасная: {line[:200]}\n"
                        "Такие действия без явного разрешения пользователя не выполняются.")
            hint = server_hint(line, self.work_dir)
            if hint:
                return f"[failure] {line[:200]} → {hint}"
            timeout = _timeout_for(line, self.timeout)
            try:
                result = sandbox.run(line, timeout=timeout)
            except AgenticSeekError as error:
                return _rejected(line, error)
            except Exception as error:  # noqa: BLE001 — the model must see the reason
                return f"[failure] {line[:200]}: {error}"
            if result.get("cancelled"):
                return f"[failure] Команда отменена: {line[:200]}"
            text = ((result.get("stdout") or "") + (result.get("stderr") or "")).strip()
            if result["code"] != 0 or result.get("timed_out"):
                return _failure(line, result, timeout, text)
            outputs.append(f"[success] {line[:200]}:\n{clip(text or '(нет вывода)', 3000)}")
        return "\n".join(outputs)

    def execution_failure_check(self, output: str) -> bool:
        return str(output or "").lstrip().startswith("[failure]")
