"""Conversation memory of one agent.

Derived from AgenticSeek ``sources/memory.py`` (GPL-3.0, see NOTICE.md).

Upstream keeps a JSON conversation per agent and compresses long histories by
summarising them with the model. NovaMind changes two things:

* compression is replaced by *trimming* — the vendored engine may already make
  a routing call and several tool calls per turn, and summarising every 20
  messages multiplies the cost on free API keys. Trimming keeps the system
  message, the goal and the most recent turns;
* nothing is written to disk by default. The agent run is persisted in SQLite
  by the engine (``database.agent_runs``), which survives restarts and shows up
  in the UI, so a second, hidden session file would only duplicate state.

``recover_last_session`` and ``memory_compression`` arguments are accepted for
signature compatibility with the upstream agents.
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger("agenticseek.memory")

#: Rough character budget of one agent conversation. A token counter would need
#: transformers (class C on Termux), so the budget is expressed in characters.
MAX_CONTEXT_CHARS = int(os.getenv("AGENTICSEEK_CONTEXT_CHARS", "36000"))
#: Keep at least this many recent messages, even if they exceed the budget.
MIN_KEEP_MESSAGES = 4


class Memory:
    """Message list of one agent: system prompt + rolling conversation."""

    def __init__(self, system_prompt: str,
                 recover_last_session: bool = False,
                 memory_compression: bool = False,
                 model_provider: str = "",
                 session_id: str = ""):
        self.system_prompt = str(system_prompt or "")
        self.messages: list[dict] = [{"role": "system", "content": self.system_prompt}]
        self.recover_last_session = bool(recover_last_session)
        self.memory_compression = bool(memory_compression)  # kept for compatibility
        self.model_provider = model_provider
        self.session_id = session_id
        self.max_chars = MAX_CONTEXT_CHARS

    # ── contents ──────────────────────────────────────────────────────────
    def push(self, role: str, content: str) -> None:
        """Append a message; ``system`` messages replace the prompt."""
        content = "" if content is None else str(content)
        if role == "system":
            self.messages[0] = {"role": "system", "content": content}
            self.system_prompt = content
            return
        self.messages.append({"role": role, "content": content})
        self.trim()

    def get(self) -> list[dict]:
        """Return a copy of the conversation (safe to pass to a provider)."""
        return [dict(item) for item in self.messages]

    def reset(self) -> None:
        self.messages = [{"role": "system", "content": self.system_prompt}]

    def size(self) -> int:
        return sum(len(str(item.get("content") or "")) for item in self.messages)

    def trim(self) -> None:
        """Drop the oldest exchanges until the character budget is respected."""
        if self.size() <= self.max_chars:
            return
        while len(self.messages) > 1 + MIN_KEEP_MESSAGES and self.size() > self.max_chars:
            # Remove the oldest non-system message, keeping the first user goal.
            index = 1
            if index < len(self.messages) - MIN_KEEP_MESSAGES:
                self.messages.pop(index)
            else:
                break

    def snapshot(self) -> dict:
        """Serialisable view used by tests and the run record."""
        return {"session_id": self.session_id, "messages": len(self.messages),
                "chars": self.size()}

    # ── compatibility helpers ─────────────────────────────────────────────
    def save(self) -> None:
        """No-op: run state is persisted in SQLite by the engine (see module doc)."""
        log.debug("memory save skipped (persisted by the engine)")

    def load(self) -> None:
        """No-op, see :meth:`save`."""
        log.debug("memory load skipped (persisted by the engine)")
