"""First-party ReAct exposes registered tool schemas and records actual use."""
import json

import tool_registry
import routes.agent as agent_routes
from tests.agent_helpers import answer_of, events_of, model_text, ndjson, script


def test_tool_schemas_are_registered_and_typed():
    tool_registry.bootstrap_default_tools()
    schemas = tool_registry.tool_schemas()
    assert schemas
    by_name = {item["name"]: item for item in schemas}
    assert {"shell", "file_read", "file_write", "file_edit", "web_search", "web_open"} <= set(by_name)
    assert all(item["schema"]["type"] == "object" for item in schemas)


def test_activity_events_keep_tool_identity_and_result():
    from agent.events import presentation_from_event

    event = {"type": "tool", "name": "shell", "status": "ok",
             "command": json.dumps({"command": "pwd"}), "result": {"exit_code": 0}}
    shown = presentation_from_event(event)
    assert shown["label"] == "Выполнил команду" and shown["detail"] == "pwd"


def test_agent_receives_registered_schemas_before_first_model_turn(admin_client, sandbox,
                                                                    monkeypatch):
    seen = []
    script(monkeypatch, ["Просто ответ."], seen)
    events = ndjson(admin_client.post("/api/agent/stream", json={"message": "привет"}))
    assert seen[0]["tools"]
    names = {item["function"]["name"] for item in seen[0]["tools"]}
    assert "web_search" in names and "shell" in names
    assert answer_of(events) == "Просто ответ."


def test_tools_are_absent_only_when_request_disables_them(admin_client, sandbox, monkeypatch):
    seen = []
    script(monkeypatch, ["Ответ без инструментов."], seen)
    events = ndjson(admin_client.post("/api/agent/stream", json={
        "message": "выполни pwd", "tools": False}))
    assert seen[0]["tools"] is None
    assert not events_of(events, "tool")
    assert next(event for event in events if event["type"] == "result")["steps"] == 0


def test_connected_mcp_tool_is_called_through_registry(admin_client, sandbox, monkeypatch):
    import mcp_registry
    monkeypatch.setattr(mcp_registry, "connected_tools", lambda: [{"server_id": "github"}])
    monkeypatch.setattr(mcp_registry, "call_tool", lambda server_id, tool, arguments:
                        {"ok": True, "repositories": ["NovaMind"]})
    monkeypatch.setenv("AGENT_CONFIRM", "off")
    script(monkeypatch, ['```json\n{"tool":"mcp_call","arguments":{"server_id":"github","tool":"search","arguments":{"query":"NovaMind"}}}\n```',
                         "Найден репозиторий NovaMind."])
    events = ndjson(admin_client.post("/api/agent/stream", json={"message": "найди проект через GitHub"}))
    tool = events_of(events, "tool")[0]
    assert tool["name"] == "mcp_call" and tool["status"] == "ok"
    assert "NovaMind" in tool["output"]


def test_plain_question_has_no_tool_actions(admin_client, sandbox, monkeypatch):
    seen = []
    script(monkeypatch, ["Ответ без вызовов."] , seen)
    events = ndjson(admin_client.post("/api/agent/stream", json={"message": "привет"}))
    assert not events_of(events, "tool")
    assert answer_of(events) == "Ответ без вызовов."
    assert len(seen) == 1


def test_workspace_action_records_output_and_step(admin_client, sandbox, monkeypatch):
    script(monkeypatch, ["```terminal\npwd\n```", "Готово."])
    events = ndjson(admin_client.post("/api/agent/stream", json={"message": "выполни pwd"}))
    tool = events_of(events, "tool")[0]
    result = next(event for event in events if event["type"] == "result")
    assert tool["name"] == "shell" and tool["status"] == "ok"
    assert "workspace" in tool["result"]["stdout"].lower() or tool["result"]["stdout"].strip()
    assert result["steps"] == 1 and result["linux"] is True


def test_offline_provider_performs_explicit_search_without_model(client, sandbox,
                                                                 monkeypatch):
    monkeypatch.setattr(agent_routes, "nova_search", lambda query, limit=8:
                        ([{"title": "Источник", "url": "https://example.com", "snippet": "данные"}], {}))
    events = ndjson(client.post("/api/agent/stream", json={"message": "найди источник"}))
    tool = events_of(events, "tool")[0]
    result = next(event for event in events if event["type"] == "result")
    assert tool["name"] == "web_search" and result["offline"]
    assert "https://example.com" in answer_of(events)
