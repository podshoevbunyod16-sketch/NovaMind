import threading
import time

from agenticseek.core.agent import Agent
from agenticseek.state import RunState
from core.events import EventBus, LIFECYCLE_EVENTS
from core.planning import create_plan
from core.verification import verify
import tool_registry


class _Adapter:
    aliases = ()
    description = "test adapter"
    risk = "safe"

    def __init__(self, tag, delay=0):
        self.tag = self.name = tag
        self.delay = delay
        self.saved_path = ""

    def execute(self, payload):
        time.sleep(self.delay)
        return f"RESULT:{self.tag}:{payload}"

    @staticmethod
    def execution_failure_check(output):
        return False

    @staticmethod
    def interpreter_feedback(output):
        return output

    @staticmethod
    def load_blocks(text):
        return []

    @staticmethod
    def load_exec_block(text):
        return [], None


class _NativeProvider:
    max_tokens = 1000

    def __init__(self):
        self.turn = 0

    def respond_tools(self, messages, tools):
        self.turn += 1
        if self.turn == 1:
            names = {item["function"]["name"] for item in tools}
            assert {"search", "open"} <= names
            return "", [
                {"id": "a", "name": "search", "arguments": {"query": "nova"}},
                {"id": "b", "name": "open", "arguments": {"url": "https://example.test"}},
            ]
        return "Готово по двум наблюдениям.", []


def test_streamed_tool_call_fragments_are_normalized():
    from ai_providers import merge_tool_call_fragments
    calls = merge_tool_call_fragments([
        [{"index": 0, "id": "call-1", "function": {"name": "web_", "arguments": '{"q"'}}],
        [{"index": 0, "function": {"name": "search", "arguments": ':"nova"}'}}],
    ])
    assert calls == [{"id": "call-1", "name": "web_search", "arguments": {"q": "nova"},
                      "raw_arguments": '{"q":"nova"}', "arguments_error": ""}]


def test_event_bus_enriches_complete_protocol():
    captured = []
    bus = EventBus(captured.append, "task-1")
    for kind in sorted(LIFECYCLE_EVENTS):
        bus.emit({"type": kind, "summary": kind})
    assert len({item["event_id"] for item in captured}) == len(captured)
    assert all(item["task_id"] == "task-1" and item["timestamp"] > 0 for item in captured)
    assert all(isinstance(item["payload"], dict) for item in captured)
    assert [item["sequence"] for item in captured] == list(range(1, len(captured) + 1))


def test_dynamic_plan_is_goal_and_capability_specific():
    catalog = [
        {"id": "git", "source": "builtin", "available": True},
        {"id": "file_read", "source": "filesystem", "available": True},
        {"id": "file_edit", "source": "filesystem", "available": True},
        {"id": "shell", "source": "terminal", "available": True},
    ]
    plan = create_plan("Analyze my GitHub repository, fix errors and run tests", catalog)
    ids = [item["id"] for item in plan]
    assert ids == ["repository", "diagnose", "change", "tests", "verify"]
    assert "git" in plan[0]["candidate_tools"]
    assert all(item["success_criteria"] for item in plan)


def test_authority_rejects_bad_schema_and_times_out(monkeypatch):
    tool_registry.register_tool(tool_registry.ToolSpec(
        "schema_probe", "probe",
        {"type": "object", "properties": {"value": {"type": "integer"}}, "required": ["value"],
         "additionalProperties": False},
        lambda value: {"ok": True, "value": value}, timeout=1,
    ))
    bad = tool_registry.call_tool("schema_probe", value="not-int", extra=True)
    assert bad["ok"] is False and bad["error"]["type"] == "schema_validation"

    tool_registry.register_tool(tool_registry.ToolSpec(
        "timeout_probe", "probe", {"type": "object", "properties": {}},
        lambda: (time.sleep(0.08) or {"ok": True}), timeout=0.01,
    ))
    timed = tool_registry.call_tool("timeout_probe")
    assert timed["ok"] is False and timed["error"]["type"] == "timeout"


def test_native_multiple_calls_use_same_runtime_authority(tmp_path):
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("Solve tasks with tools.", encoding="utf-8")
    search, opened = _Adapter("web_search", 0.1), _Adapter("web_open", 0.1)
    agent = Agent("Nova", str(prompt), _NativeProvider())
    agent.register_tool("web_search", search)
    agent.register_tool("web_open", opened)
    state = RunState()
    registry = tool_registry.RuntimeToolRegistry(agent.tools, cancel=threading.Event())
    agent.attach(state=state, tool_registry=registry)
    started = time.monotonic()
    answer, feedback = agent.process("Research Nova")
    elapsed = time.monotonic() - started
    assert answer == "Готово по двум наблюдениям."
    assert "RESULT:web_search:nova" in feedback
    assert "RESULT:web_open:https://example.test" in feedback
    assert elapsed < 0.17  # both safe native calls ran concurrently
    assert state.tool_ok == {"web_search": 1, "web_open": 1}


def test_verification_never_completes_operation_without_evidence():
    empty = RunState()
    verdict = verify("Создай файл report.txt", "Файл создан.", empty)
    assert verdict.status == "failed" and verdict.ok is False
    assert "инструментального подтверждения" in verdict.summary

    evidenced = RunState()
    evidenced.add_tool("file_write", True)
    evidenced.add_observation("file_write", True, "saved report.txt", "write report.txt")
    done = verify("Создай файл report.txt", "Файл создан.", evidenced)
    assert done.status == "completed" and done.ok is True


def test_user_isolation_for_mcp_skills_and_task_records(client, tmp_path, monkeypatch):
    from flask import session
    from app import app as flask_app
    import database
    import mcp_registry
    import skills_registry

    monkeypatch.setattr(mcp_registry, "STORE", tmp_path / "mcp.json")
    monkeypatch.setattr(skills_registry, "SKILLS_DIR", tmp_path / "skills")

    def login(email):
        session["nova_user_email"] = email
        session["nova_google_login"] = True

    with flask_app.test_request_context("/"):
        login("a@example.test")
        server_a = mcp_registry.save_connection({"id": "private", "name": "A", "transport": "http",
                                                 "url": "https://a.example/mcp", "auth": "none"})
        skills_registry.save_text("private-skill", "---\nname: Private\ndescription: only A\n---\nInstructions")
        run_a = database.create_agent_run("private task", run_id="owner-a")
        assert any(item["id"] == server_a for item in mcp_registry.public_connections())
        assert skills_registry.get_skill("private-skill")["name"] == "Private"

    with flask_app.test_request_context("/"):
        login("b@example.test")
        assert mcp_registry.public_connections() == []
        assert skills_registry.list_skills() == []
        assert database.get_agent_run(run_a) is None


def test_failed_observation_records_recovery_attempt(tmp_path):
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("Use tools.", encoding="utf-8")

    class Provider:
        max_tokens = 1000
        turn = 0
        def respond_tools(self, messages, tools):
            self.turn += 1
            if self.turn == 1:
                return "", [{"name": "search", "arguments": {}, "arguments_error": "query missing"}]
            return "Исправил вызов и завершил.", []

    agent = Agent("Nova", str(prompt), Provider())
    agent.register_tool("web_search", _Adapter("web_search"))
    state = RunState()
    agent.attach(state=state, tool_registry=tool_registry.RuntimeToolRegistry(agent.tools))
    events = []
    agent.events = events.append
    answer, _ = agent.process("Найди данные")
    assert answer == "Исправил вызов и завершил."
    assert state.recovery_attempts
    assert any(event.get("type") == "plan_revised" for event in events)
