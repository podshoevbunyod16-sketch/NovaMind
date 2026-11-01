"""Per-run budgets shared by every execution path.

The MCP search cap used to live only on ``routes.agent.NovaToolsContext``.  A
model calling the registry's ``mcp_call`` / ``mcp:<server>:<tool>`` path, or the
engine calling ``execute_unified``, bypassed that object and could search four
or more times.  A mutable budget object bound in a :class:`ContextVar` is shared
by copied contexts and worker threads, so one run has one counter regardless of
which gateway performs the call.
"""
from __future__ import annotations

import contextvars
import os
import threading

DEFAULT_SEARCH_LIMIT = 3


class SearchLimitExceeded(RuntimeError):
    """The run-wide MCP search budget is exhausted."""

    def __init__(self, limit: int):
        super().__init__(
            f"Достигнут лимит поисковых вызовов MCP за запуск (не более {limit})")
        self.limit = limit
        self.retryable = False


class SearchBudget:
    """Thread-safe counter for search-like MCP calls in one agent run."""

    def __init__(self, limit: int | None = None):
        self.limit = max(1, min(int(limit or _env_limit()), 10))
        self.used = 0
        self._lock = threading.Lock()

    def consume(self, tool_name: str) -> bool:
        """Count a search call; return False for non-search tools."""
        if "search" not in str(tool_name or "").lower():
            return False
        with self._lock:
            if self.used >= self.limit:
                raise SearchLimitExceeded(self.limit)
            self.used += 1
        return True

    def as_dict(self) -> dict:
        return {"limit": self.limit, "used": self.used}


def _env_limit() -> int:
    try:
        return int(os.getenv("MCP_MAX_SEARCH_CALLS", str(DEFAULT_SEARCH_LIMIT)))
    except ValueError:
        return DEFAULT_SEARCH_LIMIT


_BUDGET: contextvars.ContextVar[SearchBudget | None] = contextvars.ContextVar(
    "nova_search_budget", default=None)


def bind_search_budget(limit: int | None = None) -> SearchBudget:
    """Start a fresh budget for this run (called by context/registry setup)."""
    budget = SearchBudget(limit)
    _BUDGET.set(budget)
    return budget


def current_search_budget() -> SearchBudget:
    """The run's budget; creates one for direct/CLI calls without a route."""
    budget = _BUDGET.get()
    if budget is None:
        budget = bind_search_budget()
    return budget


def consume_search(tool_name: str) -> bool:
    return current_search_budget().consume(tool_name)
