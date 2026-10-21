"""Рассуждения модели без тегов не должны попадать в ответ чата."""
from unittest import mock

from routes import agent

GOAL = "Привет как устроен машина tesla roadster."

LEAK = (
    "We need answer Russian. Need understand question: \"Привет как устроен машина tesla roadster.\" "
    "They supplied found data about purported official presentation second-gen Tesla Roadster.\n\n"
    "Let's formulate:\n\n"
    "\"Tesla Roadster: первое поколение — 2008–2012 гг., электрический спорткар на платформе Lotus Elise.\"\n\n"
    "Then explanation: данные ролика don't match confirmed Tesla specs"
)

GOOD = ("Tesla Roadster первого поколения выпускали в 2008–2012 годах на платформе Lotus Elise. "
        "Второе поколение показали в 2017 году: заявлен разгон до 100 км/ч менее чем за 2 секунды.")


def test_pure_reasoning_becomes_empty():
    assert agent.strip_leaked_reasoning(LEAK, GOAL) == ""


def test_answer_after_reasoning_is_kept():
    text = "We need answer Russian. Let's draft.\n\n" + GOOD
    assert agent.strip_leaked_reasoning(text, GOAL) == GOOD


def test_normal_russian_answer_untouched():
    assert agent.strip_leaked_reasoning(GOOD, GOAL) == GOOD


def test_english_question_is_not_filtered():
    text = "Let me explain how a Tesla Roadster works: it uses an electric motor."
    assert agent.strip_leaked_reasoning(text, "How does a Tesla Roadster work?") == text


def test_think_tags_still_removed():
    assert agent.strip_leaked_reasoning("<think>x</think>" + GOOD, GOAL) == GOOD


def test_synthesize_retries_after_leak():
    replies = [(LEAK, {}), (GOOD, {})]
    with mock.patch.object(agent, "chat_completion", side_effect=replies) as chat:
        answer = agent._synthesize(GOAL, {"notes": [("поиск", "данные")], "sources": []}, "p", "m")
    assert answer == GOOD
    assert chat.call_count == 2          # второй запрос — со строгой инструкцией


def test_synthesize_gives_empty_when_both_attempts_leak():
    with mock.patch.object(agent, "chat_completion", return_value=(LEAK, {})):
        assert agent._synthesize(GOAL, {"notes": [("поиск", "данные")], "sources": []}, "p", "m") == ""
