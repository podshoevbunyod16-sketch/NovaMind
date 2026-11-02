from agent.loop import AgentContext, run_react
from agent.output import clean_final_answer, salvage_quoted_answer

GOOD = ("Tesla Roadster первого поколения выпускали в 2008–2012 годах на платформе Lotus Elise. "
        "Второе поколение показали в 2017 году.")


def test_private_think_block_is_removed_without_losing_answer():
    assert clean_final_answer("<think>internal note</think>" + GOOD) == GOOD


def test_reasoning_paragraph_is_removed_and_answer_kept():
    raw = "We need answer Russian. Let's formulate.\n\n" + GOOD + "\n\nThen explanation: private note"
    assert clean_final_answer(raw, "Как устроен Tesla Roadster?") == GOOD


def test_pure_reasoning_and_json_action_protocol_are_not_user_answers():
    assert clean_final_answer("We need answer in Russian. The user asks about a car.") == ""
    assert clean_final_answer('{"action":"answer","text":"We need answer Russian."}') == ""
    notes = ("This is a question about the Tesla Roadster and we need to answer in Russian. "
             "The user wants the names and years, so we should mention the first generation.")
    assert clean_final_answer(notes) == ""


def test_incomplete_endings_and_unclosed_code_are_rejected():
    assert clean_final_answer("Первое поколение выпускали в 2008–2012 годах на") == ""
    assert clean_final_answer("Короткий обрыв на запятую,") == ""
    assert clean_final_answer("```python\\nprint(1)") == ""


def test_translation_code_and_ordinary_english_are_preserved():
    english = "The car uses an electric motor and a battery pack."
    code = "```python\nprint('Tesla Roadster')\n```"
    assert clean_final_answer(english, "переведи на английский: как устроена Tesla") == english
    assert clean_final_answer(code, "Покажи пример кода") == code


def test_quoted_answer_rescue_ignores_a_quote_of_the_question():
    leaked = "Let's formulate: \"" + GOOD + "\" Then explanation: internal note"
    assert salvage_quoted_answer(leaked, "Как устроен Tesla Roadster?") == GOOD
    question_quote = 'Вопрос: "Привет как устроен машина tesla roadster."'
    assert salvage_quoted_answer(question_quote, "Привет как устроен машина tesla roadster.") == ""


def test_react_retries_once_for_a_clean_final_answer_and_never_emits_leak():
    replies = iter([
        ("We need answer Russian. The user asks about Tesla.", {}),
        (GOOD, {"model": "fixture"}),
    ])
    seen, events = [], []

    def complete(messages, **kwargs):
        seen.append(messages)
        return next(replies)

    result = run_react(AgentContext("Как устроен Tesla Roadster?", complete=complete,
                                   tools=[], emit=events.append))
    assert result.answer == GOOD
    assert len(seen) == 2
    assert "Сформулируй только чистый" in str(seen[1])
    assert "We need answer" not in str(events)
    assert any(event.get("type") == "result" and event.get("reply") == GOOD for event in events)


def test_react_returns_honest_fallback_after_second_leaked_answer():
    leaked = "We need answer Russian. The user asks about Tesla."
    replies = iter([(leaked, {}), (leaked, {})])
    result = run_react(AgentContext("Вопрос о машине", complete=lambda *a, **k: next(replies),
                                   tools=[]))
    assert "Не удалось получить чистый ответ" in result.answer
    assert "We need answer" not in result.answer
