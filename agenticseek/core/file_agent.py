"""File agent: find, read and organise files inside the workspace.

Derived from AgenticSeek ``sources/agents/file_agent.py`` (GPL-3.0, see
NOTICE.md): same tools (``file_finder`` + ``bash``), same rule "one action at a
time", same "final answer without a block" ending.

NovaMind adaptations:

* the number of steps is bounded (``AGENTICSEEK_FILE_STEPS``);
* the workspace is the NovaMind sandbox, so ``cd`` and absolute paths outside it
  are rejected by the tool layer;
* secret-looking files are refused by ``FileFinderTool``.
"""

from __future__ import annotations

import os

from agenticseek.core.agent import Agent
from agenticseek.tools.bash_interpreter import BashTool
from agenticseek.tools.file_finder import FileFinderTool

MAX_STEPS = max(1, min(int(os.getenv("AGENTICSEEK_FILE_STEPS", "6")), 15))


class FileAgent(Agent):
    type = "file_agent"
    role = "files"

    def __init__(self, name, prompt_path, provider, verbose=False):
        super().__init__(name, prompt_path, provider, verbose, None)
        self.tools = {
            "file_find": FileFinderTool(),
            "terminal": BashTool(),
        }
        self.max_turns = MAX_STEPS

    def limit_answer(self) -> str:
        detail = str(self.last_feedback or "")[:600]
        return ("Не удалось закончить работу с файлами за отведённые шаги.\n\n"
                f"Последний вывод:\n{detail}" if detail else
                "Не удалось закончить работу с файлами за отведённые шаги.")
