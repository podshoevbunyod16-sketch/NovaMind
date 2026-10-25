"""Casual agent: conversation without tools.

Derived from AgenticSeek ``sources/agents/casual_agent.py`` (GPL-3.0, see
NOTICE.md). The agent answers in a single model call and owns no tools — that
is what makes it safe to route ordinary chat to it: nothing in a chat message
can make it execute code.

NovaMind adaptation: the upstream prompt asks for "subtle sarcasm"; NovaMind's
chat is a product for everyday users, so the vendored prompt keeps the
"you only talk, do not pretend to run anything" rule and drops the persona
instructions.
"""

from __future__ import annotations

import os

from agenticseek.core.agent import Agent
from agenticseek.core.markup import normalize_answer

MAX_CASUAL_STEPS = max(1, min(int(os.getenv("AGENTICSEEK_CASUAL_STEPS", "6")), 12))


class CasualAgent(Agent):
    type = "casual_agent"
    role = "talk"

    def __init__(self, name, prompt_path, provider, verbose=False):
        super().__init__(name, prompt_path, provider, verbose, None)
        self.tools = {}
        self.max_turns = MAX_CASUAL_STEPS

    def process(self, goal: str, speech_module=None):
        """Answer directly, or autonomously invoke registered non-shell tools when needed."""
        self.memory.push("user", goal)
        self.last_feedback = ""
        if not self.tools:
            self.check_limits()
            answer = self.llm_request()
            self.last_answer = answer
            return answer.strip(), ""

        for _turn in range(self.max_turns):
            self.check_limits()
            raw = self.llm_request()
            normalized = normalize_answer(raw)
            self.memory.push("assistant", normalized)
            self.last_answer = normalized
            feedback, executed = self.execute_blocks(normalized)
            if executed:
                self.last_feedback = feedback
                self.memory.push("user", feedback)
                continue
            return (raw if not self.last_feedback else normalized).strip(), self.last_feedback
        return self.last_answer.strip(), self.last_feedback
