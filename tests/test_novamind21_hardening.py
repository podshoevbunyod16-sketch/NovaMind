"""Regression tests for NovaMind 2.1: unified execution, record isolation,
five final states and a plan that matches real tool observations.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

import database
import identity
import mcp_registry
import tool_registry
from agenticseek.engine import AgentEngine, EngineContext
from agenticseek.state import RunState
from core import limits
from core.events import TERMINAL_EVENTS
from core.planning import create_plan, finalize_plan, mark_tool, plan_counts
from core.verification import OUTCOMES, verify


@pytest.fixture(autouse=True)
def _restore_owner():
    token = identity.bind_owner("")
    yield
    identity._OWNER.reset(token)


# ── unified execution ───────────────────────────────────────────────────────

class _Adapter:
    tag = "web_search"
    aliases = ()
    description = "fake adapter"
    risk = "safe"

    def __init__(self):
        self.calls = []
        self.saved_path = ""

    def execute(self, payload):
        self.calls.append(payload)
        return "[success] " + payload

    @staticmethod
    def execution_failure_check(output):
        return False

    @staticmethod
    def interpreter_feedback(output):
        return output


def test_runtime_registry_is_the_single_adapter_gateway():
    adapter = _Adapter()
    registry = tool_registry.RuntimeToolRegistry({"web_search": adapter})
    result = registry.execute("web_search", "asyncio")
    assert result["ok"] is True
    assert result["result"] == "[success] asyncio"
    assert adapter.calls == ["asyncio"]

    # Unknown ids never fall through to a direct tool call.
    missing = registry.execute("not-a-tool", "x")
    assert missing["ok"] is False and missing["error"]["type"] == "unknown_tool"

    # Cancellation is checked before adapter execution.
    import threading
    cancel = threading.Event()
    cancel.set()
    cancelled = tool_registry.RuntimeToolRegistry({"web_search": adapter}, cancel=cancel)
    stopped = cancelled.execute("web_search", "x")
    assert stopped["ok"] is False and stopped["error"]["type"] == "cancelled"
    assert adapter.calls == ["asyncio"]


def test_native_arguments_are_validated_at_the_registry_boundary():
    adapter = _Adapter()
    registry = tool_registry.RuntimeToolRegistry({"web_search": adapter})
    bad = registry.execute("web_search", arguments={"limit": 3})
    assert bad["ok"] is False and bad["error"]["type"] == "schema_validation"
    good = registry.execute("web_search", arguments={"query": "asyncio"})
    assert good["ok"] is True
    assert json.loads(adapter.calls[-1])["query"] == "asyncio"


def test_mcp_search_cap_is_shared_by_registry_and_builtin_paths(monkeypatch):
    calls = []
    monkeypatch.setattr(mcp_registry, "connected_tools", lambda: [
        {"server_id": "github", "name": "search_repositories",
         "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}},
                         "required": ["query"]}},
    ])
    monkeypatch.setattr(
        mcp_registry, "call_tool",
        lambda server_id, tool, arguments=None: calls.append((server_id, tool)) or {"items": []},
    )
    limits.bind_search_budget(3)

    results = [tool_registry.execute_unified(
        "mcp:github:search_repositories", {"query": f"q{i}"}) for i in range(4)]
    assert len(calls) == 3
    assert results[0]["ok"] and results[2]["ok"]
    assert results[3]["ok"] is False
    assert "лимит поисковых вызовов" in results[3]["error"]["message"]

    # The built-in ``mcp_call`` gateway must use the same run budget.
    limits.bind_search_budget(3)
    calls.clear()
    for i in range(4):
        tool_registry.call_tool("mcp_call", server_id="github",
                                tool="search_repositories", arguments={"query": str(i)})
    assert len(calls) == 3


def test_offline_terminal_runs_through_registry_not_ctx_runner(monkeypatch):
    import agenticseek.engine as engine_mod

    class Terminal:
        tag = "terminal"
        description = "terminal"
        risk = "write"
        saved_path = ""
        calls = []

        def execute(self, payload):
            self.calls.append(payload)
            return "[success] " + payload

        @staticmethod
        def execution_failure_check(output):
            return False

        def load_exec_block(self, text):
            return [], None

        def load_blocks(self, text):
            return []

        def interpreter_feedback(self, output):
            return output

    terminal = Terminal()
    events = []
    ctx = EngineContext(goal="создай проект", complete=lambda *a, **k: ("ok", {}),
                        emit=events.append)
    ctx.runner = lambda *a, **k: (_ for _ in ()).throw(AssertionError("ctx.runner bypassed"))
    engine = AgentEngine(ctx)
    engine.runtime_registry = tool_registry.RuntimeToolRegistry({"terminal": terminal})
    monkeypatch.setattr(engine_mod, "_offline_terminal_commands", lambda goal: ["pwd"])

    engine._run_offline_terminal()

    assert terminal.calls == ["pwd"]
    assert any(event.get("name") == "terminal" for event in events)
    assert "pwd" in engine.answer


# ── old/new persisted records ───────────────────────────────────────────────

def _old_schema(path):
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE agent_runs (
        id TEXT PRIMARY KEY, chat_id TEXT DEFAULT '', run_id TEXT DEFAULT '',
        goal TEXT DEFAULT '', engine TEXT DEFAULT 'agenticseek',
        status TEXT DEFAULT 'running', route TEXT DEFAULT '',
        provider TEXT DEFAULT '', model TEXT DEFAULT '', plan TEXT DEFAULT '[]',
        steps INTEGER DEFAULT 0, answer TEXT DEFAULT '', error TEXT DEFAULT '',
        started_at REAL, finished_at REAL)""")
    conn.execute("INSERT INTO agent_runs (id, goal, status, started_at)"
                 " VALUES ('legacy-1', 'старый запуск', 'completed', 1.0)")
    conn.commit()
    conn.close()


def test_legacy_agent_rows_migrate_and_are_admin_only(tmp_path, monkeypatch):
    path = tmp_path / "novamind.db"
    _old_schema(path)
    monkeypatch.setattr(database, "DB_PATH", str(path))
    database._runs_ready.discard(str(path))
    database.init_agent_runs()

    conn = sqlite3.connect(path)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(agent_runs)")}
    conn.close()
    assert {"owner", "verification"} <= columns

    new_id = database.create_agent_run("новый запуск", owner="anon:user1")

    identity.bind_owner("anon:user1")
    assert database.get_agent_run("legacy-1") is None
    assert database.get_agent_run(new_id)["goal"] == "новый запуск"
    assert {row["id"] for row in database.list_agent_runs()} == {new_id}

    identity.bind_owner("admin:root")
    assert database.get_agent_run("legacy-1")["goal"] == "старый запуск"
    admin_ids = {row["id"] for row in database.list_agent_runs()}
    assert "legacy-1" in admin_ids and new_id not in admin_ids

    identity.bind_owner("")
    assert database.get_agent_run("legacy-1") is not None
    assert len(database.list_agent_runs()) >= 2


def test_chat_records_reject_foreign_owner(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "chats.db"))
    database.init_db()
    identity.bind_owner("anon:user1")
    chat_id = database.create_chat("свой")
    database.add_message(chat_id, "user", "привет")
    assert database.get_chat_history(chat_id)

    identity.bind_owner("anon:user2")
    assert database.get_chat_history(chat_id) == []
    assert database.add_message(chat_id, "user", "чужое") is None
    assert database.delete_chat(chat_id) is False

    identity.bind_owner("anon:user1")
    assert database.delete_chat(chat_id) is True


# ── five final states ───────────────────────────────────────────────────────

def test_all_five_outcomes_are_reachable_and_terminal_events_cover_them():
    assert set(OUTCOMES) == set(TERMINAL_EVENTS)
    assert set(TERMINAL_EVENTS) == {"completed", "partially_completed", "failed",
                                    "cancelled", "requires_user_action"}

    done = RunState()
    done.add_tool("file_write", True)
    done.add_observation("file_write", True, "saved report.txt", "write")
    assert verify("Создай файл report.txt", "Готово.", done).status == "completed"

    partial = RunState()
    partial.add_tool("web_search", False)
    partial.add_observation("web_search", False, "поиск недоступен", "search")
    partial.add_tool("web_open", True)
    partial.add_observation("web_open", True, "страница прочитана", "open")
    assert verify("Найди факт", "Нашёл частично.", partial).status == "partially_completed"

    assert verify("Создай файл", "", RunState()).status == "failed"
    assert verify("Найди", "Ответ", RunState(), cancelled=True).status == "cancelled"

    denied = RunState()
    denied.add_tool("terminal", False)
    denied.add_observation("terminal", False,
                           "[failure] Инструмент «terminal» не разрешён: нет прав", "pwd")
    verdict = verify("Запусти тест", "", denied)
    assert verdict.status == "requires_user_action"
    assert "действие пользователя" in verdict.summary.lower()


# ── plan matches real actions ───────────────────────────────────────────────

def _catalog():
    return [
        {"id": "web_search", "source": "builtin", "available": True},
        {"id": "web_open", "source": "builtin", "available": True},
        {"id": "file_read", "source": "builtin", "available": True},
        {"id": "terminal", "source": "builtin", "available": True},
    ]


def test_plan_marks_only_the_step_that_was_actually_executed():
    plan = create_plan("найди свежие новости", _catalog())
    research = next(item for item in plan if item["id"] == "research")
    crosscheck = next(item for item in plan if item["id"] == "crosscheck")

    step, changed = mark_tool(plan, "web_search", "in_progress", "поиск")
    assert changed and step is research and step["status"] == "in_progress"
    assert crosscheck["status"] == "pending"

    step, changed = mark_tool(plan, "web_search", "completed", "нашёл 3 источника")
    assert changed and step is research and step["status"] == "completed"
    assert crosscheck["status"] == "pending"

    # A successful unrelated tool must not tick another step.
    _, changed = mark_tool(plan, "file_read", "completed", "прочитал файл")
    assert changed is False
    assert plan_counts(plan)["completed"] == 1

    assert finalize_plan(plan, "completed", "готово") is True
    assert crosscheck["status"] == "skipped"
    assert next(item for item in plan if item["id"] == "verify")["status"] == "completed"


def test_engine_plan_emits_statuses_from_tool_events():
    events = []
    engine = AgentEngine(EngineContext(goal="найди свежие новости",
                                       complete=lambda *a, **k: ("Готово", {}),
                                       emit=events.append))
    engine.plan = create_plan("найди свежие новости", _catalog())

    engine._emit({"type": "tool_selected", "tool": "web_search", "summary": "поиск"})
    engine._emit({"type": "tool", "name": "web_search", "status": "ok", "code": 0,
                  "output": "нашёл"})

    research = next(item for item in engine.plan if item["id"] == "research")
    assert research["status"] == "completed"
    assert next(item for item in engine.plan if item["id"] == "verify")["status"] == "pending"
    snapshots = [event for event in events
                 if event.get("type") in {"plan_updated", "plan_revised"}]
    assert snapshots and any(
        any(item["status"] == "completed" for item in event.get("plan", [])
            if item.get("id") == "research")
        for event in snapshots)
