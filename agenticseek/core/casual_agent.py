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

from agenticseek.core.agent import Agent


class CasualAgent(Agent):
    type = "casual_agent"
    role = "talk"

    def __init__(self, name, prompt_path, provider, verbose=False):
        super().__init__(name, prompt_path, provider, verbose, None)
        self.tools = {}

    def process(self, goal: str, speech_module=None):
        """One turn: push the message, answer, return."""
        self.memory.push("user", goal)
        self.check_limits()
        answer = self.llm_request()
        self.last_answer = answer
        return answer.strip(), ""
