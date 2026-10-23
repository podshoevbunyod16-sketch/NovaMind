"""Shell tool (block tag ``bash``).

Derived from AgenticSeek ``sources/tools/bashInterpreter.py`` (GPL-3.0, see
NOTICE.md). The tag, the feedback format and the "one command at a time" rule
come from there.

NovaMind adaptations:

* execution goes through the NovaMind sandbox (:mod:`agenticseek.tools.sandbox`),
  not ``subprocess`` — the command allow-list, path checks, environment
  scrubbing and process-group kill already exist there;
* the upstream "safe mode" prompt is replaced by a hard refusal of commands
  that look destructive (``rm -rf``, ``curl | sh``, ``git push`` …): the model
  gets an error instead of a question nobody can answer in a CLI;
* multi-line blocks are split into separate commands, but each command is its
  own sandbox call, so ``cd`` does not leak into the next line — the prompt
  tells the model to use ``cd dir && cmd`` instead.
"""

from __future__ import annotations

import os

from agenticseek.core.agent import clip
from agenticseek.tools import sandbox
from agenticseek.tools.safety import is_unsafe
from agenticseek.tools.tools import Tools

DEFAULT_TIMEOUT = float(os.getenv("AGENTICSEEK_BASH_TIMEOUT", "12"))
MAX_LINES = 12


class BashTool(Tools):
    tag = "bash"
    name = "Bash"
    description = "Выполнить одну shell-команду в рабочей папке (без sudo, без сети наружу)."
    risk = "write"

    def __init__(self, timeout: float | None = None):
        super().__init__()
        self.timeout = max(1.0, timeout or DEFAULT_TIMEOUT)

    def execute(self, payload: str) -> str:
        lines = [line.strip() for line in str(payload or "").splitlines() if line.strip()]
        # Drop a shebang: NovaMind runs the command through its own shell wrapper.
        lines = [line for line in lines if not line.startswith("#!")]
        if not lines:
            return "[failure] Пустой блок bash."
        if len(lines) > MAX_LINES:
            return (f"[failure] Слишком много команд в одном блоке ({len(lines)} > {MAX_LINES}). "
                    "Выполни их по одной.")
        outputs = []
        for line in lines:
            if is_unsafe(line):
                return (f"[failure] Команда отклонена как опасная: {line[:200]}\n"
                        "Такие действия без явного разрешения пользователя не выполняются.")
            try:
                result = sandbox.run(line, timeout=self.timeout)
            except Exception as error:  # noqa: BLE001 — the model must see the reason
                return f"[failure] {line[:200]}: {error}"
            if result.get("cancelled"):
                return f"[failure] Команда отменена: {line[:200]}"
            text = (result.get("stdout") or "") + (result.get("stderr") or "")
            text = text.strip() or "(нет вывода)"
            if result["code"] != 0:
                return f"[failure] {line[:200]} → код {result['code']}:\n{clip(text, 3000)}"
            outputs.append(f"[success] {line[:200]}:\n{clip(text, 3000)}")
        return "\n".join(outputs)

    def execution_failure_check(self, output: str) -> bool:
        return str(output or "").lstrip().startswith("[failure]")
