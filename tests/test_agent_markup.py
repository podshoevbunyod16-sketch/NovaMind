"""Agent tool calls use the explicit ReAct JSON contract, never markup lookalikes."""
from agent.loop import AgentContext, run_react
from guardrails import check_output
from tool_registry import ToolSpec


LOOKUP = ToolSpec(
    "lookup", "Return a fixture value for a query.",
    {"type": "object", "properties": {"query": {"type": "string"}},
     "required": ["query"], "additionalProperties": False},
    lambda **kwargs: {"ok": True, "result": ""}, risk="safe")


def test_markup_like_command_is_plain_text_not_an_action():
    calls = []
    result = run_react(AgentContext(
        "Explain this example", complete=lambda *a, **k: ("<bash>echo should-not-run</bash>", {}),
        tools=[LOOKUP], call_tool=lambda *a, **k: calls.append((a, k))))
    assert calls == []
    assert "echo should-not-run" in result.answer
    assert result.steps == 0


def test_fenced_json_uses_only_the_declared_tool_and_arguments():
    responses = iter([
        ('```json\n{"tool":"lookup","arguments":{"query":"real"}}\n```', {}),
        ("Команда вернула реальный вывод real.", {"model": "mock"}),
    ])
    calls = []
    result = run_react(AgentContext(
        "Выполни команду printf real", complete=lambda *a, **k: next(responses),
        tools=[LOOKUP], call_tool=lambda name, **kwargs:
            calls.append((name, kwargs)) or {"ok": True, "result": "real"}))
    assert calls == [("lookup", {"query": "real"})]
    assert result.steps == 1
    assert "real" in result.answer


def test_model_cannot_forge_successful_shell_output_with_markup():
    calls = []
    result = run_react(AgentContext(
        "Создай файл report.txt", complete=lambda *a, **k:
            ("<bash>touch report.txt</bash><bash code_output>created</bash_output>", {}),
        tools=[LOOKUP], call_tool=lambda *a, **k: calls.append((a, k))))
    assert calls == []
    assert result.steps == 0
    assert "не могу честно подтвердить завершение" in result.answer.lower()


def test_output_guard_removes_dangerous_html_and_redacts_secrets():
    secret = "sk-" + "a" * 36
    verdict = check_output(f"Итог {secret} <script>alert(1)</script>")
    assert verdict.allowed
    assert secret not in verdict.text
    assert "<script>" not in verdict.text
    assert "[скрыто:" in verdict.text


def test_clean_text_answer_is_unchanged():
    text = "Работа не запускалась; это только пример команды echo hello."
    verdict = check_output(text)
    assert verdict.text == text
    assert not verdict.meta.get("changed")


def test_malformed_or_non_json_tool_text_does_not_execute():
    calls = []
    responses = iter([
        ('```json\n{"tool":"lookup","arguments": {broken}}\n```', {}),
        ("Аргументы некорректны; команда не выполнялась.", {}),
    ])
    result = run_react(AgentContext("Проверь", complete=lambda *a, **k: next(responses),
                                    tools=[LOOKUP], call_tool=lambda *a, **k: calls.append(a)))
    assert calls == []
    assert result.steps == 0
    assert "arguments" in result.answer and "broken" in result.answer
