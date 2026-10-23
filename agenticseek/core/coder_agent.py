"""Coding agent.

Derived from AgenticSeek ``sources/agents/coder_agent.py`` (GPL-3.0, see
NOTICE.md): same loop (write code block → run → read feedback → fix), same
"answer with no block when done" rule, same refusal of interactive programs.

NovaMind adaptations:

* tools are ``bash``, ``python`` and ``file_finder``. Upstream also has C, Go and
  Java interpreters; NovaMind's sandbox already permits ``gcc``/``go``/``javac``
  through ``bash``, so three more subprocess wrappers would only widen the
  attack surface;
* the number of repair attempts is bounded and configurable
  (``AGENTICSEEK_CODER_ATTEMPTS``); when they are used up the agent reports the
  last real error instead of claiming success;
* secrets, ``git push`` and package installation are refused by the tool layer
  and by the sandbox, not by prompt wording only.
"""

from __future__ import annotations

import os
import re

from agenticseek.core.agent import Agent
from agenticseek.core.errors import AgentStopped
from agenticseek.tools.bash_interpreter import BashTool
from agenticseek.tools.file_finder import FileFinderTool
from agenticseek.tools.py_interpreter import PythonTool

MAX_ATTEMPTS = max(1, min(int(os.getenv("AGENTICSEEK_CODER_ATTEMPTS", "4")), 10))
_INTERACTIVE = re.compile(r"\b(input\s*\(|curses|sys\.stdin\.read|while\s+True\s*:)")


class CoderAgent(Agent):
    type = "code_agent"
    role = "code"

    def __init__(self, name, prompt_path, provider, verbose=False):
        super().__init__(name, prompt_path, provider, verbose, None)
        self.tools = {
            "bash": BashTool(),
            "python": PythonTool(),
            "file_finder": FileFinderTool(),
        }
        self.max_turns = MAX_ATTEMPTS

    def execute_blocks(self, text: str):
        """Refuse interactive code before it can eat the whole timeout."""
        for match in _INTERACTIVE.finditer(str(text or "")):
            snippet = match.group(0)
            if snippet.startswith("while"):
                continue
            return (f"[failure] Код ждёт ввода или управляет терминалом ({snippet.strip()}) — "
                    "в песочнице нет stdin. Перепишите без интерактива.", 0)
        return super().execute_blocks(text)

    def process(self, goal: str, speech_module=None):
        try:
            return super().process(goal, speech_module)
        except AgentStopped:
            raise
