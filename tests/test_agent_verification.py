from agent.loop import AgentContext, run_react


def test_action_claim_requires_tool_evidence():
    result = run_react(AgentContext(
        "Создай файл отчёта", complete=lambda *a, **k: ("Готово, файл отчёта создан.", {}),
        tools=[],
    ))
    assert "Не могу честно подтвердить завершение" in result.answer


def test_non_action_answer_does_not_require_tools():
    result = run_react(AgentContext(
        "Что такое SQLite?", complete=lambda *a, **k: ("SQLite — встроенная база данных.", {}),
        tools=[],
    ))
    assert result.answer == "SQLite — встроенная база данных."
