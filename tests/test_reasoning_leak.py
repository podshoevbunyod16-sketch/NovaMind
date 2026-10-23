"""Рассуждения модели без тегов не должны попадать в ответ чата.

Бесплатные модели (minimax, qwen и др.) иногда пишут ход мыслей прямо в
content по-английски, без <think>. Раньше этот текст уходил пользователю
вместо ответа (см. вопрос про Tesla Roadster).
"""
import json
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


def _ndjson(response):
    return [json.loads(line) for line in response.get_data(as_text=True).splitlines() if line.strip()]


def _answer(events):
    return "".join(event["token"] for event in events if event["type"] == "token")


# ══════════════ очистка текста ══════════════

def test_pure_reasoning_becomes_empty():
    assert agent.strip_leaked_reasoning(LEAK, GOAL) == ""


def test_answer_after_reasoning_is_kept():
    text = "We need answer Russian. Let's draft.\n\n" + GOOD
    assert agent.strip_leaked_reasoning(text, GOAL) == GOOD


def test_russian_reasoning_is_detected():
    text = "Нужно ответить пользователю по-русски.\n\n" + GOOD
    assert agent.strip_leaked_reasoning(text, GOAL) == GOOD


def test_normal_russian_answer_untouched():
    assert agent.strip_leaked_reasoning(GOOD, GOAL) == GOOD


def test_translation_request_keeps_english_answer():
    text = "The car uses an electric motor and a battery pack."
    assert agent.strip_leaked_reasoning(text, "переведи на английский: как устроена Tesla") == text


def test_english_question_is_not_filtered():
    text = "Let me explain how a Tesla Roadster works: it uses an electric motor."
    assert agent.strip_leaked_reasoning(text, "How does a Tesla Roadster work?") == text


def test_think_tags_still_removed():
    assert agent.strip_leaked_reasoning("<think>x</think>" + GOOD, GOAL) == GOOD


def test_answer_starting_with_code_is_not_treated_as_reasoning():
    text = "```python\nprint('Tesla Roadster')\n```\n\nЭтот код просто печатает название машины."
    assert agent.strip_leaked_reasoning(text, "напиши код") == text


def test_unmarked_english_reasoning_is_detected():
    text = ("This is a question about the Tesla Roadster and we need to answer in Russian. "
            "The user wants the names and years, so we should mention the first generation.\n\n" + GOOD)
    assert agent.strip_leaked_reasoning(text, GOAL) == GOOD


def test_final_answer_text_cleans_json_action():
    payload = '{"action":"answer","text":"We need answer Russian."}'
    assert agent.final_answer_text(payload, GOAL) == ""


def test_draft_hint_after_answer_is_trimmed():
    """Служебная пометка в конце не должна прятать уже готовый ответ."""
    text = GOOD + ' Then explanation 2 sentences: "что-то ещё"'
    assert agent.strip_leaked_reasoning(text, GOAL) == GOOD


def test_cut_off_answer_is_rejected():
    text = "Первое поколение выпускали в 2008–2012 годах на"
    assert agent.final_answer_text(text, GOAL) == ""
    assert agent.final_answer_text("Короткий обрыв на запятую,", GOAL) == ""


def test_answer_without_final_period_is_kept():
    text = "Инструменты недоступны — отвечу по памяти"
    assert agent.final_answer_text(text, GOAL) == text


def test_answer_ending_with_balanced_quote_is_kept():
    text = 'Он сказал: "Это отличная машина."'
    assert agent.final_answer_text(text, GOAL) == text


def test_quoted_draft_is_salvaged_as_last_resort():
    answer = agent.salvage_quoted_answer(LEAK, GOAL)
    assert answer.startswith("Tesla Roadster: первое поколение")
    assert "Then explanation" not in answer


def test_quote_of_the_question_is_not_salvaged():
    text = 'Мы видим вопрос "Привет как устроен машина tesla roadster." и думаем.'
    assert agent.salvage_quoted_answer(text, GOAL) == ""


# ══════════════ восстановление ответа движком ══════════════

def _engine(monkeypatch, answers, seen=None):
    """Движок без Flask: маршрут не нужен, ответ уходит в ``engine.answer``."""
    from agenticseek.engine import AgentEngine, EngineContext
    from routes.agent import final_answer_text, salvage_quoted_answer
    from tests.agent_helpers import script

    script(monkeypatch, answers, seen)
    import routes.agent as agent_routes
    return AgentEngine(EngineContext(
        goal=GOAL, complete=agent_routes.chat_completion, emit=lambda event: None,
        provider="groq", model="test-model",
        finalize=lambda text: final_answer_text(text, GOAL),
        salvage_answer=lambda text: salvage_quoted_answer(text, GOAL)))


def test_engine_retries_after_leak(monkeypatch):
    """Рассуждение в ответе → один строгий повтор и чистый ответ пользователю."""
    seen = []
    engine = _engine(monkeypatch, [LEAK, GOOD], seen)
    events = list(engine.stream())

    assert engine.answer == GOOD
    assert engine.state.status == "ok"
    assert len(seen) == 2                      # второй запрос — со строгой инструкцией
    assert "только готовым ответом" in seen[1]["system"]
    assert any("прошу чистый ответ" in str(event.get("text"))
               for event in events if event["type"] == "step")


def test_engine_salvages_quote_when_both_attempts_leak(monkeypatch):
    """Оба ответа — рассуждение: достаём готовую цитату, а не отдаём пустоту."""
    engine = _engine(monkeypatch, [LEAK])
    list(engine.stream())

    assert engine.answer.startswith("Tesla Roadster: первое поколение")
    assert "Then explanation" not in engine.answer
    assert "We need" not in engine.answer


def test_engine_reports_no_answer_when_nothing_to_salvage(monkeypatch):
    """Спасать нечего — честное сообщение вместо пустого ответа."""
    engine = _engine(monkeypatch, ["We need think about this question but there is nothing."])
    list(engine.stream())

    assert engine.state.status == "error"
    assert "Не удалось получить ответ" in engine.answer


# ══════════════ поток чата ══════════════

def test_simple_question_does_not_show_reasoning(admin_client, monkeypatch):
    """Именно этот сценарий был на скриншоте: короткий вопрос и «мыслящий» текст."""
    from tests.agent_helpers import answer_of, events_of, script

    script(monkeypatch, [LEAK, GOOD])
    events = _ndjson(admin_client.post("/api/agent/stream", json={"message": GOAL}))
    result = next(event for event in events if event["type"] == "result")

    assert result["reply"] == GOOD
    assert result["status"] == "ok"
    assert answer_of(events) == GOOD
    assert "We need" not in answer_of(events)
    assert "They supplied" not in answer_of(events)


def test_fast_path_salvages_quoted_answer_when_retry_also_leaks(admin_client, monkeypatch):
    """Даже если модель дважды ответила рассуждением, пользователь получает ответ."""
    from tests.agent_helpers import answer_of, script

    script(monkeypatch, [LEAK])
    events = _ndjson(admin_client.post("/api/agent/stream", json={"message": GOAL}))
    result = next(event for event in events if event["type"] == "result")

    assert result["reply"].startswith("Tesla Roadster: первое поколение")
    answer = answer_of(events)
    assert "Then explanation" not in answer
    assert "We need" not in answer
