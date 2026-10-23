"""Read-first coding agent for the NovaMind project itself.

NovaMind already shipped an admin-only "Auto Coding Agent" that explored the
repository and proposed patches (never applied them). That loop was a second,
independent implementation of an agent; it now runs on the AgenticSeek base
class with project-scoped tools (:mod:`agenticseek.tools.project_tools`), so
there is one agent loop in the product.

It differs from :class:`agenticseek.core.coder_agent.CoderAgent` on purpose:

* it works on the *repository*, not on the sandbox workspace — a bad edit would
  break the running server, so every change is a proposal the administrator
  reviews;
* it can run one test file to check a hypothesis, and it must report the real
  outcome (the prompt forbids claiming "tests pass" without a tool result);
* it is admin-only; the route enforces that before the engine is built.
"""

from __future__ import annotations

import os

from agenticseek.core.agent import Agent
from agenticseek.tools.project_tools import build_project_tools

MAX_TURNS = max(1, min(int(os.getenv("CODING_AGENT_MAX_STEPS", "8")), 12))


class ProjectAgent(Agent):
    type = "project_agent"
    role = "project"

    def __init__(self, name, prompt_path, provider, verbose=False):
        super().__init__(name, prompt_path, provider, verbose, None)
        self.tools = build_project_tools(emit=self.emit)
        self.max_turns = MAX_TURNS

    def limit_answer(self) -> str:
        patches = getattr(self.tools.get("project_patch"), "proposals", []) or []
        text = [f"Не удалось закончить анализ за {self.max_turns} шагов."]
        if patches:
            text.append("Предложенные патчи (не применены):")
            for item in patches:
                text.append(f"- {item['path']}")
        if self.last_feedback:
            text.append(f"\nПоследний вывод инструмента:\n{self.last_feedback[:800]}")
        return "\n".join(text)
