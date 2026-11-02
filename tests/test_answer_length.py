"""First-party ReAct answer budget, truncation recovery, and output hygiene."""
from pathlib import Path

import ai_providers
import local_llm
from agent.loop import AgentContext, run_react
from agent.output import clean_final_answer, is_truncated_answer

GOAL = "расскажи подробно про структуру проекта"
LONG = ("NovaMind устроен как тонкий слой Flask поверх единого провайдера ИИ. " * 30).strip()
TRUNCATED = LONG + " и"
TAIL = ("Кроме того, в проекте есть отдельные модули для базы данных, планировщика, "
        "MCP-серверов и генерации медиа. Вместе они образуют единое приложение, "
        "которое запускается одной командой python app.py. На этом ответ завершён.")


def test_react_passes_full_answer_budget_to_provider():
    seen = []

    def complete(messages, **kwargs):
        seen.append(kwargs)
        return "Готово.", {}

    run_react(AgentContext(GOAL, complete=complete, tools=[], max_tokens=4096))
    assert seen[0]["max_tokens"] == 4096


def test_react_respects_a_lower_configured_budget():
    seen = []
    run_react(AgentContext(GOAL, complete=lambda *a, **kw: (seen.append(kw) or "Готово.", {}),
                           tools=[], max_tokens=3000))
    assert seen[0]["max_tokens"] == 3000


def test_route_limits_budget_to_provider_cap(admin_client, monkeypatch):
    import routes.react_agent as react_routes
    captured = []
    original_context = react_routes.AgentContext

    def capture_context(*args, **kwargs):
        captured.append(kwargs.get("max_tokens"))
        return original_context(*args, **kwargs)

    monkeypatch.setattr(react_routes, "AgentContext", capture_context)
    monkeypatch.setattr(ai_providers, "resolve_target",
                        lambda *a, **k: ("groq", "test-model", {"max_tokens": 3000}))
    monkeypatch.setattr(ai_providers, "chat_completion", lambda *a, **kw: ("Готовый ответ.", {}))
    response = admin_client.post("/api/agent/react", json={"message": "привет", "tools": False})
    assert response.status_code == 200
    assert captured == [3000]


def test_clean_answer_preserves_long_complete_text():
    assert clean_final_answer(LONG, GOAL) == LONG


def test_incomplete_endings_are_detected_but_short_answers_are_not_repaired():
    assert is_truncated_answer(TRUNCATED)
    assert clean_final_answer(TRUNCATED, GOAL) == ""
    assert clean_final_answer("Короткий обрыв на запятую,", GOAL) == ""
    assert clean_final_answer("Первое поколение выпускали в 2008–2012 годах на", GOAL) == ""
    assert clean_final_answer("Инструменты недоступны — отвечу по памяти", GOAL)


def test_long_truncated_answer_is_continued_and_not_duplicated():
    replies = iter([(TRUNCATED, {}), (TAIL, {})])
    seen = []
    result = run_react(AgentContext(
        GOAL, complete=lambda messages, **kwargs: (seen.append(messages) or next(replies)),
        tools=[]))
    assert len(seen) == 2
    assert "Продолжи незавершённый ответ" in str(seen[1])
    assert result.answer.count("NovaMind устроен") == 30
    assert TAIL in result.answer
    assert "неполным" not in result.answer


def test_failed_continuation_keeps_partial_text_and_is_honest():
    replies = iter([(TRUNCATED, {}), (TRUNCATED, {})])
    result = run_react(AgentContext(
        GOAL, complete=lambda *a, **k: next(replies), tools=[]))
    assert result.answer.count("NovaMind устроен") == 30
    assert "ответ может быть неполным" in result.answer


def test_unclosed_code_fence_gets_a_continuation_attempt():
    code = "```python\n" + ("print('long')\n" * 60)
    assert is_truncated_answer(code)
    replies = iter([(code, {}), ("\n```\nКод печатает строки.", {})])
    result = run_react(AgentContext("Покажи код", complete=lambda *a, **k: next(replies), tools=[]))
    assert "```python" in result.answer
    assert "Код печатает строки" in result.answer


def test_reasoning_is_removed_while_code_tail_is_preserved():
    text = "This is a question about the project and we need to answer in Russian.\n\n" + LONG + "\n\n```python\nprint(1)\n```"
    out = clean_final_answer(text, GOAL)
    assert LONG in out and "print(1)" in out
    assert not out.startswith("This is a question")


def test_pure_reasoning_is_rejected():
    text = "We need answer Russian. They supplied found data about the project."
    assert clean_final_answer(text, GOAL) == ""


def test_code_block_with_odd_quotes_is_not_cut_off():
    text = LONG + '\n\n```python\nprint("it\'s ok")\n```'
    assert clean_final_answer(text, GOAL) == text


def test_react_prompt_requires_complete_evidence_based_answers():
    prompt = Path("prompts/base/react_agent.txt").read_text(encoding="utf-8")
    assert "проверь результат наблюдаемым доказательством" in prompt
    assert "что осталось невыполненным" in prompt
    assert "Отвечай пользователю по-русски" in prompt


def test_offline_structure_answer_is_long_and_honest():
    reply = local_llm.complete([{"role": "user", "content": GOAL}])
    assert len(reply) > 1500, len(reply)
    assert "agent/" in reply and "routes/" in reply and "static/" in reply
    assert "языковой модели нет" in reply.lower()
