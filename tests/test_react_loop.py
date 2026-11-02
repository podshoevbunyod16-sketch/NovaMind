import json
import threading

from agent.loop import AgentContext, run_react
from tool_registry import ToolSpec


def test_react_cycle_executes_tool_then_answers():
    transcript = iter([
        ('```json\n{"tool":"lookup","arguments":{"query":"x"}}\n```', {}),
        ("Найдено: X. Проверка подтверждена выводом lookup.", {"model": "fake"}),
    ])
    calls = []
    spec = ToolSpec("lookup", "Найти значение", {
        "type": "object", "properties": {"query": {"type": "string"}},
        "required": ["query"], "additionalProperties": False,
    }, lambda **kw: {"ok": True, "result": "X"})
    result = run_react(AgentContext(
        "Найди X", complete=lambda *a, **k: next(transcript),
        tools=[spec], call_tool=lambda n, **kw: calls.append((n, kw)) or {"ok": True, "result": "X"},
    ))
    assert calls == [("lookup", {"query": "x"})]
    assert result.status == "completed"
    assert result.steps == 1
    assert "X" in result.answer


def test_unknown_tool_is_observation_not_execution():
    transcript = iter([
        ('```json\n{"tool":"made_up","arguments":{}}\n```', {}),
        ("Недоступный инструмент не запускался; задача не выполнена.", {}),
    ])
    executions = []
    result = run_react(AgentContext(
        "Проверь", complete=lambda *a, **k: next(transcript), tools=[],
        call_tool=lambda *a, **k: executions.append(a),
    ))
    assert not executions
    assert result.tool_hallucinations == 1
    assert result.observations[0]["result"]["error"]["type"] == "unknown_tool"


def test_repeat_call_rejected_and_limits_return_final_answer():
    same = '```json\n{"tool":"lookup","arguments":{"query":"x"}}\n```'
    transcript = iter([(same, {}), (same, {}), ("После повторного вызова останавливаюсь и сообщаю результат честно.", {})])
    spec = ToolSpec("lookup", "Lookup", {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}, lambda **kw: {"ok": True})
    result = run_react(AgentContext(
        "Цель", complete=lambda *a, **k: next(transcript), tools=[spec],
        call_tool=lambda *a, **k: {"ok": True}, max_steps=4,
    ))
    assert result.status == "completed"
    assert result.answer
    assert result.steps == 2
    assert result.observations[1]["result"]["error"]["type"] == "repeat_call"


def test_tool_result_emits_real_sources_and_media_events():
    outputs = iter([
        {"ok": True, "results": [{"title": "Документация", "url": "https://example.com/docs"}]},
        {"ok": True, "kind": "image", "model": {"name": "Test Image", "provider_name": "Test"},
         "result": {"url": "/media/test.png", "filename": "test.png"}},
    ])
    specs = [
        ToolSpec("web_search", "search", {"type": "object", "properties": {}}, lambda **_: next(outputs)),
        ToolSpec("image", "image", {"type": "object", "properties": {}}, lambda **_: next(outputs), risk="safe"),
    ]
    transcript = iter([
        ('```json\n{"tool":"web_search","arguments":{}}\n```', {}),
        ('```json\n{\"tool\":\"image\",\"arguments\":{}}\n```', {}),
        ("Картинка создана; ссылка и файл получены от инструмента.", {}),
    ])
    events = []
    run_react(AgentContext("Найди страницу и картинку", complete=lambda *a, **k: next(transcript),
                           emit=events.append, tools=specs,
                           call_tool=lambda _name, **_kwargs: next(outputs)))
    assert next(event for event in events if event["type"] == "sources")["sources"][0]["url"] == "https://example.com/docs"
    media = next(event["media"] for event in events if event["type"] == "media")
    assert media["url"] == "/media/test.png"
    assert media["model_name"] == "Test Image"
    assert media["provider_name"] == "Test"


def test_tool_and_final_output_secrets_are_redacted_before_events():
    secret = "sk-" + "a" * 32
    transcript = iter([
        ('```json\n{"tool":"lookup","arguments":{}}\n```', {}),
        (f"Найден секрет: {secret}", {}),
    ])
    spec = ToolSpec("lookup", "Lookup", {"type": "object", "properties": {}}, lambda **_: {"ok": True})
    events = []
    result = run_react(AgentContext(
        "Найди", complete=lambda *a, **k: next(transcript), tools=[spec], emit=events.append,
        call_tool=lambda *_a, **_kw: {"ok": True, "api_key": secret, "result": "safe"}))
    serialized = json.dumps(events, ensure_ascii=False)
    assert secret not in serialized
    assert secret not in result.answer
    assert "[скрыто:" in serialized or "[скрыто:" in result.answer


def test_cancel_returns_final_answer():
    cancel = threading.Event()
    def complete(*_args, **_kwargs):
        cancel.set()
        return ('```json\n{"tool":"lookup","arguments":{"query":"x"}}\n```', {})
    spec = ToolSpec("lookup", "Lookup", {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}, lambda **kw: {"ok": True})
    result = run_react(AgentContext("Цель", complete=complete, tools=[spec], cancel=cancel))
    assert result.status == "cancelled"
    assert "остановлена" in result.answer


def test_react_system_prompt_uses_prompt_registry(monkeypatch):
    import agent.loop as loop
    import prompt_registry

    seen = []
    monkeypatch.setattr(prompt_registry, "load",
                        lambda name: seen.append(name) or "Политика ReAct из реестра.")
    assert loop._system_prompt() == "Политика ReAct из реестра."
    assert seen == ["react_agent"]


def test_resume_does_not_repeat_a_completed_tool_call():
    repeated = '```json\n{"tool":"lookup","arguments":{"query":"x"}}\n```'
    transcript = iter([(repeated, {}), ("Использую сохранённое наблюдение, без повтора инструмента.", {})])
    calls = []
    spec = ToolSpec("lookup", "Lookup", {
        "type": "object", "properties": {"query": {"type": "string"}},
        "required": ["query"], "additionalProperties": False,
    }, lambda **kwargs: {"ok": True, "result": "new"})
    prior = [{"tool": "lookup", "arguments": {"query": "x"},
              "result": {"ok": True, "result": "old evidence"}}]
    result = run_react(AgentContext(
        "Проверь X", complete=lambda *a, **k: next(transcript), tools=[spec],
        history=[{"role": "assistant", "content": repeated},
                 {"role": "user", "content": "Наблюдение инструмента lookup: old evidence"}],
        observations=prior, step_offset=1, resume=True,
        call_tool=lambda *a, **k: calls.append(a)))
    assert calls == []
    assert result.steps == 2
    assert result.observations[0]["result"]["result"] == "old evidence"
    assert result.observations[1]["result"]["error"]["type"] == "repeat_call"
