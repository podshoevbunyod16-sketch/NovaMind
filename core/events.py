"""Stable, backward-compatible event protocol for autonomous tasks."""
from __future__ import annotations

import time
import uuid
from typing import Callable

LIFECYCLE_EVENTS = frozenset({
    "task_started", "task_analysis", "plan_created", "plan_updated",
    "tool_discovered", "tool_selected", "permission_requested",
    "tool_call_started", "tool_call_completed", "tool_call_failed",
    "observation", "plan_revised", "verification_started",
    "verification_completed", "task_completed", "task_failed", "task_cancelled",
})


class EventBus:
    """Adds protocol metadata while retaining flat legacy event fields.

    ``payload`` is the canonical structured body. Flat copies stay present so
    older NovaMind clients continue to work during rolling deployments.
    """

    def __init__(self, sink: Callable[[dict], None], task_id: str = ""):
        self.sink = sink
        self.task_id = str(task_id or uuid.uuid4())
        self.sequence = 0

    def emit(self, event: dict) -> dict:
        item = dict(event or {})
        kind = str(item.get("type") or "event")
        self.sequence += 1
        item.setdefault("event_id", uuid.uuid4().hex)
        item.setdefault("task_id", self.task_id)
        item.setdefault("timestamp", time.time())
        item.setdefault("sequence", self.sequence)
        if kind in LIFECYCLE_EVENTS:
            item.setdefault("payload", {
                key: value for key, value in item.items()
                if key not in {"event_id", "task_id", "timestamp", "sequence", "type", "payload"}
            })
        self.sink(item)
        return item
