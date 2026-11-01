from __future__ import annotations

import mcp_registry
from tests.agent_helpers import ndjson, script


def test_agent_stream_emits_stable_lifecycle_without_chain_of_thought(admin_client, monkeypatch):
    monkeypatch.setattr(mcp_registry, "connected_tools", lambda: [])
    script(monkeypatch, ["Готовый ответ."])
    events = ndjson(admin_client.post("/api/agent/stream", json={"message": "объясни NovaMind"}))
    kinds = [event["type"] for event in events]
    assert kinds[0] == "stage"  # backward-compatible contract
    for required in ("task_started", "task_analysis", "tool_discovered", "plan_created",
                     "verification_started", "verification_completed", "task_completed"):
        assert required in kinds
    # Lifecycle contains short summaries/statuses, never hidden model reasoning.
    for event in events:
        if event["type"] in {"task_analysis", "verification_started", "verification_completed"}:
            assert "reasoning" not in event and "chain_of_thought" not in event


def test_real_tool_call_has_selection_start_completion_and_observation(admin_client, monkeypatch):
    monkeypatch.setenv("AGENT_CONFIRM", "off")
    monkeypatch.setattr(mcp_registry, "connected_tools", lambda: [{
        "server_id": "demo", "name": "lookup", "inputSchema": {"type": "object"},
    }])
    monkeypatch.setattr(mcp_registry, "call_tool", lambda *_a, **_k: {"value": "observed"})
    script(monkeypatch, [
        '```mcp_call\n{"server_id":"demo","tool":"lookup","arguments":{}}\n```',
        "Проверено.",
    ])
    events = ndjson(admin_client.post("/api/agent/stream", json={"message": "используй MCP demo"}))
    kinds = [event["type"] for event in events]
    assert "tool_selected" in kinds
    assert "tool_call_started" in kinds
    assert "tool_call_completed" in kinds
    assert "observation" in kinds
    assert kinds.index("tool_call_started") < kinds.index("tool_call_completed")
