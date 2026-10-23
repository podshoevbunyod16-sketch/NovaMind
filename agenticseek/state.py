"""Mutable state of one agent run.

The tools and the engine both write here: tools record the sources they read
and the media they generated, the engine tracks steps and the final status.
The route copies the result into the ``result`` event and into
``database.agent_runs``.

Keeping this object separate from the engine avoids a second, hidden channel
between tools and the UI: a tool calls ``state.add_source(...)``, and the engine
emits the collected list once, at the end.
"""

from __future__ import annotations

import threading


class RunState:
    """Thread-safe container of what happened during a run."""

    def __init__(self, max_sources: int = 12, max_tools: int = 40):
        self._lock = threading.Lock()
        self.sources: list[dict] = []
        self.tools: list[str] = []
        self.tool_ok: dict[str, int] = {}
        self.tool_fail: dict[str, int] = {}
        self.media: list = []
        self.notes: list[str] = []
        self.steps = 0
        self.status = "ok"
        self.error = ""

    def add_source(self, url: str, title: str = "", max_sources: int = 12) -> None:
        url = str(url or "").strip()
        if not url:
            return
        with self._lock:
            if any(item.get("url") == url for item in self.sources):
                return
            self.sources.append({"title": title or url, "url": url})
            del self.sources[max_sources:]

    def add_tool(self, name: str, ok: bool = True) -> None:
        name = str(name or "").strip()
        if not name:
            return
        with self._lock:
            if name not in self.tools:
                self.tools.append(name)
            target = self.tool_ok if ok else self.tool_fail
            target[name] = target.get(name, 0) + 1
            self.steps += 1

    def add_media(self, result, model=None) -> None:
        """Record generated media (result + chosen model, as media_agent returns)."""
        if not result:
            return
        with self._lock:
            self.media.append({"result": result, "model": model})

    @property
    def media_results(self) -> list:
        with self._lock:
            return [item.get("result") for item in self.media if item.get("result")]

    def add_note(self, text: str) -> None:
        text = str(text or "").strip()
        if text:
            with self._lock:
                self.notes.append(text[:2000])
                del self.notes[:-20]

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "sources": list(self.sources),
                "tools": list(self.tools),
                "media": [item.get("result") for item in self.media],
                "steps": self.steps,
                "status": self.status,
                "error": self.error,
                "tool_stats": {"ok": dict(self.tool_ok), "failed": dict(self.tool_fail)},
            }
