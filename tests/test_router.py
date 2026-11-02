"""The model selects among the current run's registered ReAct tools."""
from agent.loop import AgentContext, run_react
from tool_registry import ToolSpec


def _spec(name, description="fixture", schema=None):
    return ToolSpec(name, description, schema or {
        "type": "object", "properties": {"query": {"type": "string"}},
        "required": ["query"], "additionalProperties": False,
    }, lambda **kwargs: {"ok": True, "result": name + " result"})


def test_create_request_uses_only_the_tool_selected_by_the_model():
    calls = []
    replies = iter([
        ('```json\n{"tool":"file_plan","arguments":{"query":"shop"}}\n```', {}),
        ("Файловый план создан.", {}),
    ])
    result = run_react(AgentContext(
        "Создай интернет-магазин", complete=lambda *a, **k: next(replies),
        tools=[_spec("web_search"), _spec("file_plan")],
        call_tool=lambda name, **kwargs: calls.append(name)
        or {"ok": True, "result": "план"}))
    assert calls == ["file_plan"]
    assert result.steps == 1
    assert "план" in result.answer.lower()


def test_web_request_does_not_force_a_web_tool_without_model_choice():
    calls = []
    result = run_react(AgentContext(
        "Найди новости", complete=lambda *a, **k: ("Уточните регион поиска.", {}),
        tools=[_spec("web_search")], call_tool=lambda *a, **k: calls.append(a)))
    assert calls == []
    assert result.steps == 0
    assert "регион" in result.answer


def test_unavailable_tool_is_reported_with_available_names():
    replies = iter([
        ('```json\n{"tool":"hidden","arguments":{}}\n```', {}),
        ("Доступного инструмента hidden нет; ничего не запускал.", {}),
    ])
    executed = []
    result = run_react(AgentContext(
        "Найди данные", complete=lambda *a, **k: next(replies),
        tools=[_spec("web_search"), _spec("file_read")],
        call_tool=lambda *a, **k: executed.append(a)))
    assert executed == []
    assert result.tool_hallucinations == 1
    assert "web_search" in result.observations[0]["result"]["error"]["message"]


def test_invalid_arguments_are_not_executed():
    executed = []
    result = run_react(AgentContext(
        "Найди", complete=lambda *a, **k: ('```json\n{"tool":"web_search","arguments":{"extra":1}}\n```', {}),
        tools=[_spec("web_search")], call_tool=lambda *a, **k: executed.append(a)))
    assert executed == []
    assert result.observations[0]["result"]["error"]["type"] == "invalid_arguments"
