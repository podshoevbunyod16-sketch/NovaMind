"""Кнопка «Стоп» (этап 3).

Каждый ход агента получает run_id (HTTP-заголовок X-Agent-Run-Id).
POST /api/agent/stop взводит threading.Event: цикл останавливается перед
следующим шагом, а shell-цепочка снимает свои процессы.
"""
import json
import threading
import time

import pytest

import database
import ai_providers
import routes.agent as agent_routes
import routes.react_agent as react_routes
import routes.terminal as term_routes
from tests.agent_helpers import events_of, script


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
        return list(results), {"backend": "test", "result_count": 1, "results": results,
                               "count": 1, "trace": [{"backend": "test", "ok": True, "ms": 1}]}

    monkeypatch.setattr(agent_routes, "nova_search", search)
    return calls


# ══════════════ реестр запусков ══════════════

def test_register_run_creates_cancel_event():
    run_id, cancel = agent_routes.register_run()
    assert run_id and cancel.is_set() is False
    assert agent_routes.cancel_agent_run(run_id) is True
    assert cancel.is_set() is True
    agent_routes.finish_run(run_id)
    assert agent_routes.cancel_agent_run(run_id) is False


def test_stop_route_validates_and_cancels(admin_client):
    assert admin_client.post("/api/agent/stop", json={}).status_code == 400
    assert admin_client.post("/api/agent/stop", json={"run_id": "нет"}).status_code == 404

    run_id, cancel = agent_routes.register_run()
    response = admin_client.post("/api/agent/stop", json={"run_id": run_id})
    assert response.status_code == 200 and response.get_json()["stopped"] is True
    assert cancel.is_set() is True
    agent_routes.finish_run(run_id)


# ══════════════ поток агента ══════════════

def test_stream_returns_run_id_and_finishes_it(admin_client, sandbox, monkeypatch, fake_search):
    script(monkeypatch, ["готово"])
    response = admin_client.post("/api/agent/stream", json={"message": "найди asyncio"})
    run_id = response.headers.get("X-Agent-Run-Id")
    events = _ndjson(response)

    assert run_id and len(run_id) >= 16
    result = next(event for event in events if event["type"] == "result")
    assert result["run_id"] == run_id and result["status"] == "completed"
    assert database.get_agent_run(run_id)["status"] == "completed"
    assert run_id not in react_routes._ACTIVE_REACT     # запуск не утекает


def test_stream_stops_between_steps_and_saves_partial(admin_client, sandbox, monkeypatch):
    """Отмена после инструмента не позволяет модели начать следующий цикл."""
    calls = {"model": 0}

    def search(query, num=8, **kwargs):
        for cancel in react_routes._ACTIVE_REACT.values():
            cancel.set()
        results = [{"title": "asyncio", "url": "https://docs.python.org/asyncio",
                    "snippet": "библиотека", "host": "docs.python.org"}]
        return results, {"backend": "test", "results": results}

    def completion(messages, **kwargs):
        calls["model"] += 1
        return '```json\n{"tool":"web_search","arguments":{"query":"asyncio"}}\n```', {}

    import routes.search as search_routes
    monkeypatch.setattr(search_routes, "search_web", search)
    monkeypatch.setattr(ai_providers, "chat_completion", completion)
    monkeypatch.setattr(ai_providers, "resolve_target",
                        lambda provider=None, model=None: ("groq", "test-model", {}))

    events = _ndjson(admin_client.post("/api/agent/stream", json={"message": "найди asyncio"}))
    result = next(event for event in events if event["type"] == "result")

    assert result["status"] == "cancelled"
    assert "останов" in _answer(events).lower()
    assert calls["model"] == 1
    assert [event["name"] for event in events_of(events, "tool")] == ["web_search"]


def test_stop_before_first_step_makes_no_model_or_tool_calls(admin_client, sandbox, monkeypatch):
    calls = {"model": 0}
    original_run = react_routes._run

    def stop_before_worker(*args, **kwargs):
        kwargs["cancel"].set()
        return original_run(*args, **kwargs)

    def completion(messages, **kwargs):
        calls["model"] += 1
        return "не должно вызываться", {}

    monkeypatch.setattr(react_routes, "_run", stop_before_worker)
    monkeypatch.setattr(ai_providers, "chat_completion", completion)
    events = _ndjson(admin_client.post("/api/agent/stream", json={"message": "найди asyncio"}))
    result = next(event for event in events if event["type"] == "result")

    assert calls["model"] == 0
    assert result["status"] == "cancelled"
    assert "останов" in result["reply"].lower()


def test_cancelled_run_does_not_execute_the_block(admin_client, sandbox, monkeypatch):
    """Отмена сразу после ответа модели блокирует опасный tool call."""
    calls = {"model": 0}
    cancel_ref = {}
    original_run = react_routes._run

    def capture_cancel(*args, **kwargs):
        cancel_ref["event"] = kwargs["cancel"]
        return original_run(*args, **kwargs)

    def completion(messages, **kwargs):
        calls["model"] += 1
        cancel_ref["event"].set()
        return '```json\n{"tool":"shell","arguments":{"command":"sleep 5"}}\n```', {}

    monkeypatch.setattr(react_routes, "_run", capture_cancel)
    monkeypatch.setattr(ai_providers, "resolve_target",
                        lambda provider=None, model=None: ("groq", "test-model", {}))
    monkeypatch.setattr(ai_providers, "chat_completion", completion)
    started = time.time()
    events = _ndjson(admin_client.post("/api/agent/stream", json={"message": "запусти sleep 5"}))
    assert time.time() - started < 5
    assert events_of(events, "tool") == []
    assert next(e for e in events if e["type"] == "result")["status"] == "cancelled"
    assert calls["model"] == 1


# ══════════════ процессы shell ══════════════

def test_cancel_kills_shell_process_chain(admin_client, sandbox):
    cancel = threading.Event()
    threading.Timer(0.3, cancel.set).start()
    started = time.time()

    result = term_routes.run_agent("sleep 5", timeout=20, cancel=cancel)

    elapsed = time.time() - started
    assert elapsed < 4, f"процесс не был снят: {elapsed:.1f} c"
    assert result["cancelled"] is True
    assert result["code"] != 0


def test_cancel_does_not_break_normal_commands(admin_client, sandbox):
    cancel = threading.Event()
    result = term_routes.run_agent("echo привет", timeout=10, cancel=cancel)
    assert result["code"] == 0 and "привет" in result["stdout"]
    assert result["cancelled"] is False
