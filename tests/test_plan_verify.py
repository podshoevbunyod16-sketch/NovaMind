"""Планировщик AgenticSeek: план → специалисты → итог.

Сложная задача разбивается на шаги (``{"plan": [...]}`` в блоке ```json```),
каждый шаг уходит своему агенту, прогресс виден в потоке (``plan``,
``plan_progress``), а итог собирает отдельный вызов модели по собранным
результатам. Самопроверка старого движка заменена этим шагом «итог по
результатам»: модель отвечает только по тому, что реально сделано.
"""
import json

import pytest

import routes.agent as agent_routes
from agenticseek.core.planner_agent import MAX_PLAN_TASKS, PLAN_ATTEMPTS
from tests.agent_helpers import answer_of, events_of, model_text, ndjson, script


@pytest.fixture(autouse=True)
def specialists_mode(monkeypatch):
    """Тесты планировщика и передачи хода написаны под архитектуру специалистов.

    В проде по умолчанию ``unified`` (один оркестратор), его проверяет
    ``test_orchestrator.py``. Здесь нужны planner и роли web/code/files/talk.
    """
    monkeypatch.setenv("AGENTICSEEK_EXECUTION_MODE", "specialists")


@pytest.fixture
def fake_search(monkeypatch):
    results = [{"title": "asyncio", "url": "https://docs.python.org/asyncio",
                "snippet": "библиотека", "host": "docs.python.org"}]
    calls = []

    def search(query, limit=8):
        calls.append(query)
        return list(results), {"backend": "test", "results": results}

    monkeypatch.setattr(agent_routes, "nova_search", search)
    return calls


def _plan(*tasks):
    return "```json\n" + json.dumps(
        {"plan": [{"agent": agent, "id": str(index), "need": [],
                   "task": task} for index, (agent, task) in enumerate(tasks, 1)]},
        ensure_ascii=False) + "\n```"


# ══════════════ эвристика сложности ══════════════

def test_complex_request_heuristic():
    assert agent_routes.is_complex_request(
        "найди и сравни цены на три модели, потом сделай вывод") is True
    assert agent_routes.is_complex_request("сначала найди asyncio, затем напиши файл") is True
    assert agent_routes.is_complex_request("привет") is False
    assert agent_routes.is_complex_request("как дела?") is False
    assert agent_routes.is_complex_request("") is False


# ══════════════ разбор плана ══════════════

@pytest.fixture
def planner():
    from agenticseek.core.planner_agent import PlannerAgent
    from agenticseek.core.prompt_paths import prompt_path

    return PlannerAgent("NovaMind", prompt_path("planner_agent"), provider=None)


def test_plan_is_parsed_with_aliases(planner):
    plan = planner.parse_plan(_plan(("Web", "найти источники"), ("Code", "посчитать"),
                                    ("Talk", "сформулировать вывод")))
    assert [item["agent"] for item in plan] == ["web", "coder", "casual"]
    assert [item["id"] for item in plan] == ["1", "2", "3"]


def test_plan_rejects_unknown_agents_and_empty_tasks(planner):
    text = ('```json\n{"plan": [{"agent": "hacker", "task": "взломать"},'
            ' {"agent": "web", "task": "  "}, {"agent": "web", "task": "найти"}]}\n```')
    plan = planner.parse_plan(text)
    assert [item["task"] for item in plan] == ["найти"]


def test_plan_is_bounded(planner):
    tasks = [("web", f"шаг {index}") for index in range(MAX_PLAN_TASKS + 5)]
    assert len(planner.parse_plan(_plan(*tasks))) == MAX_PLAN_TASKS


def test_broken_plan_is_ignored(planner):
    assert planner.parse_plan("я не буду планировать") == []
    assert planner.parse_plan("```json\n{не json}\n```") == []
    assert planner.parse_plan("```json\n{\"plan\": \"не список\"}\n```") == []


def test_plan_event_shows_steps(planner):
    events = []
    planner.events = events.append
    plan = planner.parse_plan(_plan(("web", "найти источники"), ("casual", "ответить")))
    planner.emit_plan(plan)
    assert events[0]["type"] == "plan"
    assert events[0]["total"] == 2 and events[0]["done"] == 0
    assert "1. [web] найти источники" in events[0]["text"]
    assert planner.tasks == plan


# ══════════════ поток агента ══════════════

def test_model_plan_is_stored_and_shown(admin_client, sandbox, fake_search, monkeypatch):
    seen = []
    script(monkeypatch, [
        _plan(("Web", "найти источники"), ("Casual", "написать вывод")),
        "```web_search\nasyncio\n```",
        "Нашёл документацию asyncio.",
        "Итог: asyncio — библиотека Python.",
    ], seen)

    events = ndjson(admin_client.post("/api/agent/stream",
                                      json={"message": "сначала найди asyncio, затем сделай вывод"}))
    plan = events_of(events, "plan")[0]
    assert plan["total"] == 2 and plan["done"] == 0
    assert [step["agent"] for step in plan["steps"]] == ["web", "casual"]
    assert "1. [web] найти источники" in plan["text"]
    assert fake_search == ["asyncio"]
    assert "asyncio — библиотека" in answer_of(events)


def test_plan_progress_is_reported(admin_client, sandbox, fake_search, monkeypatch):
    script(monkeypatch, [
        _plan(("Web", "найти asyncio"), ("Casual", "ответить")),
        "```web_search\nasyncio\n```",
        "Нашёл.",
        "Готово.",
    ])
    events = ndjson(admin_client.post("/api/agent/stream",
                                      json={"message": "сначала найди asyncio, затем ответь"}))
    progress = events_of(events, "plan_progress")
    assert [item["done"] for item in progress] == [1, 2]
    assert progress[0]["total"] == 2
    assert any("Шаг 1/2" in event["text"] for event in events_of(events, "step"))


def test_planner_answers_directly_when_plan_fails(admin_client, sandbox, monkeypatch):
    """Модель так и не дала план — честно отвечаем сами, без выдуманного плана."""
    answers = [f"ответ {index}" for index in range(PLAN_ATTEMPTS)] + ["прямой ответ"]
    seen = []
    script(monkeypatch, answers, seen)
    events = ndjson(admin_client.post("/api/agent/stream",
                                      json={"message": "сначала найди, затем напиши"}))
    assert not events_of(events, "plan")
    assert any("План не получился" in event["text"] for event in events_of(events, "step"))
    assert answer_of(events) == "прямой ответ"


def test_simple_answer_does_not_plan(admin_client, sandbox, monkeypatch):
    script(monkeypatch, ["просто ответ"])
    events = ndjson(admin_client.post("/api/agent/stream", json={"message": "привет"}))
    assert not events_of(events, "plan")
    assert answer_of(events) == "просто ответ"


def test_planner_reports_failed_step(admin_client, sandbox, monkeypatch):
    """Сбой шага не рушит план: он виден в потоке и в итоговом ответе."""
    script(monkeypatch, [
        _plan(("Code", "сломать всё")),
        "```bash\npython3 -c 'raise SystemExit(3)'\n```",
        "Шаг не удался.",
        "Итог: шаг не выполнен.",
    ])
    events = ndjson(admin_client.post("/api/agent/stream",
                                      json={"message": "сначала запусти код, затем напиши вывод"}))
    tool = events_of(events, "tool")[0]
    assert tool["name"] == "terminal" and tool["code"] == 1
    assert "Итог" in answer_of(events)


def test_planner_respects_step_budget(admin_client, sandbox, monkeypatch):
    """Бюджет шагов ограничивает число обращений к модели, а не число тел."""
    monkeypatch.setattr(agent_routes, "MAX_STEPS", 1)
    seen = []
    script(monkeypatch, [
        _plan(("Web", "найти asyncio"), ("Casual", "ответить")),
        "Нашёл.",
        "Готово.",
    ], seen)
    events = ndjson(admin_client.post("/api/agent/stream",
                                      json={"message": "сначала найди asyncio, затем ответь"}))
    result = next(event for event in events if event["type"] == "result")
    assert result["steps"] <= 1
    assert answer_of(events).strip()
