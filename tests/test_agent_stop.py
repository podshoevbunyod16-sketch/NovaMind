"""Кнопка «Стоп» (этап 3).

Каждый ход агента получает run_id (HTTP-заголовок X-Agent-Run-Id).
POST /api/agent/stop взводит threading.Event: цикл останавливается перед
следующим шагом, а shell-цепочка снимает свои процессы.
"""
import json
import threading
import time

import pytest

import routes.agent as agent_routes
import routes.terminal as term_routes
from tests.agent_helpers import events_of


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
    answers = ['{"action":"search","query":"asyncio"}', "готово"]

    def completion(messages, **kwargs):
        return (answers.pop(0) if answers else "готово"), {}

    monkeypatch.setattr(agent_routes, "chat_completion", completion)
    monkeypatch.setattr(agent_routes, "chat_stream", lambda *a, **k: iter(()))

    response = admin_client.post("/api/agent/stream", json={"message": "найди asyncio"})
    run_id = response.headers.get("X-Agent-Run-Id")
    events = _ndjson(response)

    assert run_id and len(run_id) >= 16
    result = next(event for event in events if event["type"] == "result")
    assert result["run_id"] == run_id and result["stopped"] is False
    assert run_id not in agent_routes._ACTIVE_RUNS     # запуск не утекает


def test_stream_stops_between_steps_and_saves_partial(admin_client, sandbox, monkeypatch):
    """«Стоп» во время первого шага: второй запрос к модели уже не идёт."""
    calls = {"model": 0}

    def search(query, limit=8):
        # имитируем нажатие кнопки: взводим флаг текущего запуска
        for record in agent_routes._ACTIVE_RUNS.values():
            record["cancel"].set()
        results = [{"title": "asyncio", "url": "https://docs.python.org/asyncio",
                    "snippet": "библиотека", "host": "docs.python.org"}]
        return results, {"backend": "test", "results": results}

    def completion(messages, **kwargs):
        calls["model"] += 1
        return "```web_search\nasyncio\n```", {"provider": "test"}

    monkeypatch.setattr(agent_routes, "nova_search", search)
    monkeypatch.setattr(agent_routes, "resolve_target",
                        lambda provider=None, model=None: ("groq", "test-model", {}))
    monkeypatch.setattr(agent_routes, "chat_completion", completion)

    events = _ndjson(admin_client.post("/api/agent/stream", json={"message": "найди asyncio"}))
    result = next(event for event in events if event["type"] == "result")

    assert result["stopped"] is True and result["status"] == "stopped"
    assert "Остановлено" in _answer(events)
    assert calls["model"] == 1              # цикл вышел до второго запроса
    assert any("Остановлено" in event["text"] for event in events_of(events, "step"))


def test_stop_before_first_step_makes_no_model_or_tool_calls(admin_client, sandbox, monkeypatch):
    calls = {"model": 0}

    def completion(messages, **kwargs):
        calls["model"] += 1
        return "не должно вызываться", {"provider": "test"}

    monkeypatch.setattr(agent_routes, "resolve_target",
                        lambda provider=None, model=None: ("groq", "test-model", {}))
    monkeypatch.setattr(agent_routes, "chat_completion", completion)
    original = agent_routes.register_run

    def register_and_stop():
        run_id, cancel = original()
        cancel.set()                            # «Стоп» нажат сразу
        return run_id, cancel

    monkeypatch.setattr(agent_routes, "register_run", register_and_stop)

    events = _ndjson(admin_client.post("/api/agent/stream", json={"message": "найди asyncio"}))
    result = next(event for event in events if event["type"] == "result")

    assert calls["model"] == 0
    assert result["stopped"] is True
    assert "Остановлено" in result["reply"]


def test_cancelled_run_does_not_execute_the_block(admin_client, sandbox, monkeypatch):
    """Флаг отмены проверяется перед выполнением блока, а не после."""
    calls = {"model": 0}
    seen = []

    def completion(messages, **kwargs):
        calls["model"] += 1
        seen.append(messages)
        return "```bash\nsleep 5\n```", {"provider": "test"}

    monkeypatch.setattr(agent_routes, "resolve_target",
                        lambda provider=None, model=None: ("groq", "test-model", {}))
    monkeypatch.setattr(agent_routes, "chat_completion", completion)
    original = agent_routes.register_run

    def register_and_stop():
        run_id, cancel = original()
        cancel.set()
        return run_id, cancel

    monkeypatch.setattr(agent_routes, "register_run", register_and_stop)
    started = time.time()

    events = _ndjson(admin_client.post("/api/agent/stream", json={"message": "запусти sleep 5"}))
    assert time.time() - started < 5
    assert events_of(events, "tool") == []
    assert next(e for e in events if e["type"] == "result")["status"] == "stopped"


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
