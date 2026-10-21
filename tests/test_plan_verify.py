"""Планирование и самопроверка (этап 5).

Сложная задача разбивается на шаги, статус плана виден модели в каждом
запросе и пользователю в интерфейсе, а перед финальным ответом агент
сверяет результат с целью и при нехватке данных продолжает работу.
"""
import json

import pytest

import routes.agent as agent_routes


def _ndjson(response):
    return [json.loads(line) for line in response.get_data(as_text=True).splitlines() if line.strip()]


def _answer(events):
    return "".join(event["token"] for event in events if event["type"] == "token")


@pytest.fixture
def fake_search(monkeypatch):
    results = [{"title": "asyncio", "url": "https://docs.python.org/asyncio",
                "snippet": "библиотека", "host": "docs.python.org"}]
    calls = []

    def search(query, limit=8):
        calls.append(query)
        return list(results), {"backend": "test", "results": results,
                               "trace": [{"backend": "test", "ok": True, "ms": 1}]}

    monkeypatch.setattr(agent_routes, "nova_search", search)
    return calls


def _stub(monkeypatch, answers, seen=None):
    queue = list(answers)

    def completion(messages, **kwargs):
        if seen is not None:
            seen.append({"messages": list(messages), "system": kwargs.get("system", "")})
        return (queue.pop(0) if queue else "готово"), {}

    monkeypatch.setattr(agent_routes, "chat_completion", completion)
    monkeypatch.setattr(agent_routes, "chat_stream", lambda *a, **k: iter(()))
    return queue


# ══════════════ эвристика ══════════════

def test_complex_request_heuristic():
    assert agent_routes.is_complex_request("найди и сравни цены на три модели, потом сделай вывод") is True
    assert agent_routes.is_complex_request("собери справку по asyncio и напиши файл") is True
    assert agent_routes.is_complex_request("привет") is False
    assert agent_routes.is_complex_request("как дела?") is False


def test_plan_helpers_mark_items_done():
    state = {"plan": []}
    items = agent_routes.set_plan(state, ["найти источники", "прочитать", "ответить"])
    assert [item["status"] for item in items] == ["todo", "todo", "todo"]

    first = agent_routes.advance_plan(state)
    assert first["text"] == "найти источники"
    assert state["plan"][0]["status"] == "done" and state["plan"][1]["status"] == "todo"

    text = agent_routes.plan_status_text(state)
    assert "выполнено 1 из 3" in text and "Дальше: прочитать" in text

    agent_routes.advance_plan(state)
    agent_routes.advance_plan(state)
    assert "Все пункты выполнены" in agent_routes.plan_status_text(state)
    assert agent_routes.advance_plan(state) is None


# ══════════════ поток агента ══════════════

def test_model_plan_is_stored_and_shown(admin_client, sandbox, fake_search, monkeypatch):
    seen = []
    _stub(monkeypatch, [
        '{"action":"plan","steps":["найти источники","прочитать страницу","ответить"]}',
        '{"action":"search","query":"asyncio"}',
        '{"action":"answer","text":"asyncio — библиотека"}',
        '{"action":"answer","text":"asyncio — библиотека"}',
    ], seen)

    events = _ndjson(admin_client.post("/api/agent/stream",
                                       json={"message": "собери справку по asyncio и напиши вывод"}))

    plan = next(event for event in events if event["type"] == "plan")
    assert [step["text"] for step in plan["steps"]] == ["найти источники", "прочитать страницу", "ответить"]
    assert plan["total"] == 3 and plan["done"] == 0

    # статус плана уходит модели отдельным блоком
    assert "ПЛАН: выполнено" in seen[1]["system"] or any(
        "ПЛАН: выполнено" in str(item.get("content") or "") for item in seen[1]["messages"])


def test_plan_items_get_checkmarks(admin_client, sandbox, fake_search, monkeypatch):
    _stub(monkeypatch, [
        '{"action":"plan","steps":["найти источники","ответить"]}',
        '{"action":"search","query":"asyncio"}',
        '{"action":"answer","text":"готово"}',
        '{"action":"answer","text":"готово"}',
    ])

    events = _ndjson(admin_client.post("/api/agent/stream",
                                       json={"message": "исследуй asyncio по шагам"}))
    updates = [event for event in events if event["type"] == "plan" and event.get("done")]
    assert updates, "после успешного шага должен прийти обновлённый план"
    assert updates[0]["steps"][0]["status"] == "done"
    assert updates[0]["done"] == 1 and updates[0]["total"] == 2


def test_answer_is_verified_before_final(admin_client, sandbox, fake_search, monkeypatch):
    seen = []
    _stub(monkeypatch, [
        '{"action":"search","query":"asyncio"}',
        '{"action":"answer","text":"первый вариант ответа"}',
        '{"action":"answer","text":"финальный ответ"}',
    ], seen)

    events = _ndjson(admin_client.post("/api/agent/stream", json={"message": "найди asyncio"}))

    assert any(event["type"] == "step" and "Проверяю" in event["text"] for event in events)
    verify_prompt = [item for item in seen[-1]["messages"]
                     if item.get("role") == "user" and "ПРОВЕРКА ПЕРЕД ОТВЕТОМ" in str(item.get("content"))]
    assert verify_prompt, "модель должна получить запрос на самопроверку"
    assert "финальный ответ" in _answer(events)


def test_verify_sends_agent_back_to_work(admin_client, sandbox, fake_search, monkeypatch):
    """Если после проверки модель не уверена — она продолжает работу инструментами."""
    seen = []
    _stub(monkeypatch, [
        '{"action":"search","query":"asyncio"}',
        '{"action":"answer","text":"черновой ответ"}',
        '{"action":"open","url":"https://docs.python.org/asyncio"}',
        '{"action":"answer","text":"уточнённый ответ"}',
        '{"action":"answer","text":"уточнённый ответ"}',
    ], seen)

    events = _ndjson(admin_client.post("/api/agent/stream", json={"message": "разберись с asyncio"}))

    names = [event["name"] for event in events if event["type"] == "tool"]
    assert names == ["search", "open"]                 # доработка после проверки
    assert "черновой" not in _answer(events)
    assert "уточнённый ответ" in _answer(events)


def test_verification_is_limited_to_two_requests(admin_client, sandbox, fake_search, monkeypatch):
    _stub(monkeypatch, [
        '{"action":"search","query":"asyncio"}',
        '{"action":"answer","text":"ответ 1"}',
        '{"action":"search","query":"asyncio два"}',
        '{"action":"answer","text":"ответ 2"}',
        '{"action":"search","query":"asyncio три"}',
        '{"action":"answer","text":"ответ 3"}',
        '{"action":"answer","text":"ответ 4"}',
    ])

    events = _ndjson(admin_client.post("/api/agent/stream", json={"message": "найди всё про asyncio"}))
    checks = [event for event in events if event["type"] == "step" and "Проверяю" in event["text"]]

    assert len(checks) == 2                            # максимум две доработки
    assert "ответ 3" in _answer(events)


def test_simple_answer_without_tools_skips_verification(admin_client, sandbox, monkeypatch):
    _stub(monkeypatch, ["просто ответ"])
    events = _ndjson(admin_client.post("/api/agent/stream", json={"message": "найди смысл жизни"}))
    assert not [event for event in events if event["type"] == "step" and "Проверяю" in event["text"]]
    assert "просто ответ" in _answer(events)
