"""Длина ответа агента: коротко → полно и до конца.

Регрессия на реальный баг: один ответ модели был ограничен 1200 токенов,
поэтому длинный текст обрывался на полуслове. Фильтр ``final_answer_text``
считал такой обрыв «незаконченным черновиком» и выбрасывал ответ целиком;
движок после неудачной попытки восстановления показывал одну строку
«Не удалось получить ответ…» вместо двух тысяч символов.

Здесь проверяется вся цепочка: лимит токенов, спасение длинного обрыва,
дописывание ответа до конца, аккуратная работа с кодом и рассуждениями.
"""

import json
from pathlib import Path

import pytest

import local_llm
import routes.agent as agent_routes
from agenticseek.core.prompt_paths import prompt_path
from agenticseek.engine import (AGENT_MAX_TOKENS, CONTINUE_ANSWER_SYSTEM,
                                CLEAN_ANSWER_SYSTEM, AgentEngine, EngineContext)
from tests.agent_helpers import answer_of, events_of, ndjson, script

GOAL = "расскажи подробно про структуру проекта"
LONG = ("NovaMind устроен как тонкий слой Flask поверх единого провайдера ИИ. " * 30).strip()
TRUNCATED = LONG + " и"
TAIL = ("Кроме того, в проекте есть отдельные модули для базы данных, планировщика, "
        "MCP-серверов и генерации медиа. Вместе они образуют единое приложение, "
        "которое запускается одной командой python app.py. На этом ответ завершён.")


# ══════════════ лимит токенов ══════════════

def _engine(**kwargs):
    return AgentEngine(EngineContext(
        goal=GOAL, complete=lambda *a, **k: ("готово", {}),
        emit=lambda event: None, **kwargs))


def test_agent_answer_budget_is_much_larger_than_old_1200():
    """1200 токенов обрывали ответ; новый бюджет — не меньше 4096."""
    assert AGENT_MAX_TOKENS >= 4096
    assert _engine().provider.max_tokens >= 4096


def test_engine_respects_provider_token_cap():
    """Роут передаёт лимит провайдера — движок его не превышает."""
    assert _engine(max_tokens=3000).provider.max_tokens == 3000
    assert _engine(max_tokens=1800).provider.max_tokens == 1800


def test_route_passes_provider_cap_to_model(admin_client, sandbox, monkeypatch):
    """Настоящий вызов модели получает лимит, а не жёсткие 1200 токенов."""
    calls = []

    def fake_complete(messages, **kwargs):
        calls.append(kwargs)
        return "Готовый ответ.", {}

    monkeypatch.setattr(agent_routes, "resolve_target",
                        lambda *a, **k: ("groq", "test-model", {"max_tokens": 3000}))
    monkeypatch.setattr(agent_routes, "chat_completion", fake_complete)
    events = ndjson(admin_client.post("/api/agent/stream", json={
        "message": "привет", "tools": False, "search": False}))
    assert calls and calls[0]["max_tokens"] == 3000
    assert answer_of(events) == "Готовый ответ."


# ══════════════ спасение оборванного ответа ══════════════

def test_repair_keeps_long_truncated_answer():
    head = agent_routes.repair_truncated_answer(TRUNCATED, GOAL)
    assert len(head) >= 1800
    assert head.endswith(".")
    assert agent_routes._looks_complete_answer(head)
    assert not head.endswith(" и")


def test_repair_rejects_short_fragments_and_reasoning():
    assert agent_routes.repair_truncated_answer("Короткий обрыв на запятую,", GOAL) == ""
    assert agent_routes.repair_truncated_answer(
        "We need answer Russian. They supplied found data about the project. " * 4, GOAL) == ""


def test_final_answer_text_keeps_long_truncated_answer():
    out = agent_routes.final_answer_text(TRUNCATED, GOAL)
    assert len(out) >= 1800
    assert "Не удалось получить ответ" not in out


def test_existing_short_fragment_contract_is_preserved():
    """Короткий обрывок по-прежнему считается утечкой рассуждений."""
    assert agent_routes.final_answer_text("Короткий обрыв на запятую,", GOAL) == ""
    assert agent_routes.final_answer_text("Первое поколение выпускали в 2008–2012 годах на", GOAL) == ""


def test_normal_long_answer_is_not_changed():
    assert agent_routes.final_answer_text(LONG, GOAL) == LONG


# ══════════════ дописываем ответ до конца ══════════════

def test_truncated_answer_is_finished_instead_of_one_line_error(admin_client, sandbox, monkeypatch):
    """Главный сценарий: длинный обрыв → полный ответ с дописанным хвостом."""
    seen = []
    script(monkeypatch, [TRUNCATED, TAIL], seen)
    events = ndjson(admin_client.post("/api/agent/stream", json={
        "message": GOAL, "tools": False, "search": False}))
    reply = answer_of(events)
    assert len(reply) > 1800, reply[:200]
    assert TAIL in reply, "продолжение не приклеилось"
    assert "Не удалось получить ответ" not in reply
    assert any("дописываю до конца" in event.get("text", "")
               for event in events_of(events, "step"))
    assert len(seen) == 2, "ровно один дополнительный запрос на продолжение"
    assert CONTINUE_ANSWER_SYSTEM in str(seen[-1].get("system", ""))


def test_continuation_failure_still_keeps_long_answer(admin_client, sandbox, monkeypatch):
    """Модель повторила тот же обрывок — duplicates нет, длинный текст на месте."""
    script(monkeypatch, [TRUNCATED])
    events = ndjson(admin_client.post("/api/agent/stream", json={
        "message": GOAL, "tools": False, "search": False}))
    reply = answer_of(events)
    assert len(reply) > 1800
    assert reply.count("NovaMind устроен") == 30, "ответ не должен дублироваться"


def test_continue_system_asks_for_full_answer():
    assert "продолжение" in CONTINUE_ANSWER_SYSTEM.lower()
    assert "не повторяй" in CONTINUE_ANSWER_SYSTEM.lower()
    assert "только готовым ответом" in CLEAN_ANSWER_SYSTEM
    assert "одной короткой фразой" not in CLEAN_ANSWER_SYSTEM


# ══════════════ код и рассуждения больше не режут ответ ══════════════

ENGLISH_REASONING = (
    "This is a question about the Tesla Roadster and we need to answer in Russian. "
    "The user wants the names and years, so we should mention the first generation."
)


def test_code_block_with_odd_quotes_does_not_look_truncated():
    text = LONG + '\n\n```python\nprint("it\'s ok)\n```'
    assert agent_routes._looks_complete_answer(text)
    assert agent_routes.final_answer_text(text, GOAL) == text


def test_unclosed_code_fence_is_detected():
    assert not agent_routes._looks_complete_answer(LONG + "\n```python\nprint(1)")


def test_reasoning_is_removed_but_code_tail_is_kept():
    text = ENGLISH_REASONING + "\n\n" + LONG + "\n\n```python\nprint(1)\n```"
    out = agent_routes.strip_leaked_reasoning(text, GOAL)
    assert LONG in out
    assert "print(1)" in out
    assert not out.startswith("This is a question")


def test_pure_reasoning_still_becomes_empty():
    text = ENGLISH_REASONING + "\n\n" + "They supplied found data about the car."
    assert agent_routes.strip_leaked_reasoning(text, GOAL) == ""


# ══════════════ промпты и офлайн-контур ══════════════

def test_prompts_ask_for_full_answers():
    casual = Path(prompt_path("casual_agent")).read_text(encoding="utf-8")
    browser = Path(prompt_path("browser_agent")).read_text(encoding="utf-8")
    assert "отвечай сразу текстом, коротко" not in casual
    assert "полно и по существу" in casual
    assert "короткий ответ по задаче" not in browser
    assert "полный, законченный ответ" in browser


def test_offline_structure_answer_is_long_and_honest():
    reply = local_llm.complete([{"role": "user", "content": GOAL}])
    assert len(reply) > 1500, len(reply)
    assert "agenticseek/" in reply and "routes/" in reply and "static/" in reply
    assert "языковой модели нет" in reply.lower()
    assert "GROQ_API_KEY" in reply
    assert "токенов" not in reply.lower()
