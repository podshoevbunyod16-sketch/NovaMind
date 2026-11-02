"""The first-party ReAct runner must not expose internal reasoning notes."""
import json

from agent.loop import AgentContext, run_react
from agent.output import clean_final_answer, salvage_quoted_answer

GOAL = "Привет как устроен машина tesla roadster."
LEAK = (
    "We need answer Russian. Need understand question: Tesla Roadster. "
    "They supplied found data about purported official presentation.\n\n"
    "Let's formulate:\n\n"
    '"Tesla Roadster: первое поколение — 2008–2012 гг., электрический спорткар на платформе Lotus Elise."\n\n'
    "Then explanation: данные ролика не подтверждены"
)
GOOD = ("Tesla Roadster первого поколения выпускали в 2008–2012 годах на платформе Lotus Elise. "
        "Второе поколение показали в 2017 году: заявлен разгон до 100 км/ч менее чем за 2 секунды.")
REASONING = "We need think about this question, understand it, and answer in Russian without facts."


def _ndjson(response):
    return [json.loads(line) for line in response.get_data(as_text=True).splitlines() if line.strip()]


def _answer(events):
    return "".join(event["token"] for event in events if event["type"] == "token")


def test_pure_reasoning_becomes_empty():
    assert clean_final_answer(LEAK.split("\n\n", 1)[0], GOAL) == ""


def test_answer_after_reasoning_is_kept():
    text = "We need answer Russian. Let's draft.\n\n" + GOOD
    assert clean_final_answer(text, GOAL) == GOOD


def test_russian_reasoning_is_removed():
    text = "Нужно ответить пользователю по-русски.\n\n" + GOOD
    assert clean_final_answer(text, GOAL) == GOOD


def test_normal_russian_answer_is_untouched():
    assert clean_final_answer(GOOD, GOAL) == GOOD


def test_translation_request_keeps_english_answer():
    text = "The car uses an electric motor and a battery pack."
    assert clean_final_answer(text, "переведи на английский: как устроена Tesla") == text


def test_english_question_is_not_filtered():
    text = "Let me explain how a Tesla Roadster works: it uses an electric motor."
    assert clean_final_answer(text, "How does a Tesla Roadster work?") == text


def test_explicit_think_tags_are_removed():
    assert clean_final_answer("<think>private notes</think>" + GOOD, GOAL) == GOOD


def test_answer_starting_with_code_is_not_treated_as_reasoning():
    text = "```python\nprint('Tesla Roadster')\n```\n\nЭтот код просто печатает название машины."
    assert clean_final_answer(text, "напиши код") == text


def test_unmarked_english_reasoning_is_detected():
    text = ("This is a question about the Tesla Roadster and we need to answer in Russian. "
            "The user wants the names and years, so we should mention the first generation.\n\n" + GOOD)
    assert clean_final_answer(text, GOAL) == GOOD


def test_json_action_protocol_is_not_displayed_as_an_answer():
    payload = '{"action":"answer","text":"We need answer Russian."}'
    assert clean_final_answer(payload, GOAL) == ""


def test_draft_note_after_answer_is_trimmed():
    assert clean_final_answer(GOOD + " Then explanation 2 sentences: internal note", GOAL) == GOOD


def test_cut_off_answer_is_rejected():
    assert clean_final_answer("Первое поколение выпускали в 2008–2012 годах на", GOAL) == ""
    assert clean_final_answer("Короткий обрыв на запятую,", GOAL) == ""


def test_answer_without_final_period_is_kept():
    text = "Инструменты недоступны — отвечу по памяти"
    assert clean_final_answer(text, GOAL) == text


def test_answer_ending_with_balanced_quote_is_kept():
    text = 'Он сказал: "Это отличная машина."'
    assert clean_final_answer(text, GOAL) == text


def test_quoted_draft_is_salvaged_and_question_quote_is_not():
    assert salvage_quoted_answer(LEAK, GOAL).startswith("Tesla Roadster: первое поколение")
    text = 'Мы видим вопрос "Привет как устроен машина tesla roadster." и думаем.'
    assert salvage_quoted_answer(text, GOAL) == ""


def test_react_retries_once_after_reasoning_leak():
    replies = iter([(REASONING, {}), (GOOD, {})])
    seen, events = [], []

    def complete(messages, **kwargs):
        seen.append(messages)
        return next(replies)

    result = run_react(AgentContext(GOAL, complete=complete, tools=[], emit=events.append))
    assert result.answer == GOOD
    assert len(seen) == 2
    assert "Сформулируй только чистый" in str(seen[1])
    assert "We need answer" not in json.dumps(events, ensure_ascii=False)


def test_react_salvages_quote_after_retry_still_leaks():
    result = run_react(AgentContext(GOAL, complete=lambda *a, **k: (LEAK, {}), tools=[]))
    assert result.answer.strip('"').startswith("Tesla Roadster: первое поколение")
    assert "Then explanation" not in result.answer
    assert "We need" not in result.answer


def test_react_reports_failure_if_no_clean_answer_or_quote_exists():
    leaked = "We need think about this question but there is nothing useful to quote."
    replies = iter([(leaked, {}), (leaked, {})])
    result = run_react(AgentContext(GOAL, complete=lambda *a, **k: next(replies), tools=[]))
    assert "Не удалось получить чистый ответ" in result.answer
    assert "We need" not in result.answer


def test_react_http_route_does_not_stream_reasoning(admin_client, monkeypatch):
    import ai_providers

    replies = iter([(REASONING, {}), (GOOD, {})])
    monkeypatch.setattr(ai_providers, "chat_completion", lambda *a, **k: next(replies))
    events = _ndjson(admin_client.post("/api/agent/react", json={
        "message": GOAL, "tools": False,
    }))
    result = next(event for event in events if event.get("type") == "result")
    assert result["reply"] == GOOD
    assert "We need" not in _answer(events)


def test_provider_error_result_includes_run_id(admin_client, monkeypatch):
    import ai_providers
    import database

    monkeypatch.setattr(ai_providers, "chat_completion", lambda *a, **k: (None, "quota exceeded"))
    events = _ndjson(admin_client.post("/api/agent/react", json={"message": "проверка", "tools": False}))
    result = next(event for event in events if event.get("type") == "result")
    assert result.get("run_id")
    record = database.get_agent_run(result["run_id"])
    assert record and record["status"] == "provider_error"
