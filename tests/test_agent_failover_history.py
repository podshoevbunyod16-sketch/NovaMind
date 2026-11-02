from agent.loop import AgentContext, run_react
from tool_registry import ToolSpec


def test_observations_survive_provider_failover():
    seen = []
    calls = iter([
        ('```json\n{"tool":"lookup","arguments":{"query":"needle"}}\n```', {"model": "provider-a"}),
        ("Результат проверки обработан после переключения модели.",
         {"model": "provider-b", "failover_count": 1}),
    ])
    spec = ToolSpec("lookup", "Найти", {"type": "object",
        "properties": {"query": {"type": "string"}}, "required": ["query"]},
        lambda **kw: {"ok": True, "result": "needle found"})
    def completion(messages, **kwargs):
        seen.append([str(message.get("content", "")) for message in messages])
        return next(calls)
    result = run_react(AgentContext("Проверь needle", complete=completion,
        tools=[spec], call_tool=lambda *a, **k: {"ok": True, "result": "needle found"}))
    assert result.status == "completed"
    assert any("needle found" in item for item in seen[1])
