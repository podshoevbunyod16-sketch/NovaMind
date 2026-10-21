"""Трасса шагов агента (бонус Б2): «Что делал агент» после перезагрузки."""
import json

import pytest

import database
import routes.agent as agent_routes


def _ndjson(response):
    return [json.loads(line) for line in response.get_data(as_text=True).splitlines() if line.strip()]


def _stub_model(monkeypatch, answers):
    queue = list(answers)

    def fake_completion(messages, **kwargs):
        return (queue.pop(0) if queue else "готово"), {}

    def fake_stream(messages, **kwargs):
        text = queue.pop(0) if queue else "готово"
        yield "token", text
        yield "done", ""

    monkeypatch.setattr(agent_routes, "chat_completion", fake_completion)
    monkeypatch.setattr(agent_routes, "chat_stream", fake_stream)
    return queue


@pytest.fixture
def db(tmp_path, monkeypatch, isolated_env):
    """Своя база на каждый тест — репозиторий не трогаем."""
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "traces.db"))
    monkeypatch.setattr(database, "_traces_ready", set())
    database.init_db()
    return database


# ══════════════ хранилище ══════════════

def test_save_and_read_trace(db):
    chat_id = db.create_chat("Проверка трассы")
    steps = [{"kind": "step", "icon": "🔎", "text": "ищу в интернете"},
             {"kind": "tool", "name": "ls", "command": "ls", "code": 0}]
    trace_id = db.save_trace(chat_id, "run-1", steps)
    assert trace_id > 0

    traces = db.get_traces(chat_id)
    assert len(traces) == 1
    assert traces[0]["run_id"] == "run-1"
    assert traces[0]["steps"] == steps


def test_empty_trace_is_not_saved(db):
    chat_id = db.create_chat("Пустая")
    assert db.save_trace(chat_id, "run-1", []) == 0
    assert db.get_traces(chat_id) == []


def test_traces_are_ordered_and_limited(db):
    chat_id = db.create_chat("Много шагов")
    for index in range(5):
        db.save_trace(chat_id, f"run-{index}", [{"kind": "step", "text": f"шаг {index}"}])
    traces = db.get_traces(chat_id, limit=3)
    assert [trace["run_id"] for trace in traces] == ["run-2", "run-3", "run-4"]
    assert traces[-1]["steps"][0]["text"] == "шаг 4"


def test_traces_do_not_grow_without_limit(db):
    chat_id = db.create_chat("Длинный чат")
    for index in range(8):
        db.save_trace(chat_id, f"run-{index}", [{"kind": "step", "text": str(index)}],
                      limit_per_chat=5)
    traces = db.get_traces(chat_id, limit=50)
    assert len(traces) == 5


def test_clear_and_delete_chat_remove_traces(db):
    chat_id = db.create_chat("Удаляемый")
    db.save_trace(chat_id, "run-1", [{"kind": "step", "text": "шаг"}])
    assert db.clear_traces(chat_id) == 1
    assert db.get_traces(chat_id) == []

    db.save_trace(chat_id, "run-2", [{"kind": "step", "text": "шаг"}])
    db.delete_chat(chat_id)
    assert db.get_traces(chat_id) == []


def test_broken_json_in_steps_does_not_crash(db):
    chat_id = db.create_chat("Битая трасса")
    db.save_trace(chat_id, "run-1", [{"kind": "step", "text": "шаг"}])
    with db.get_db() as conn:
        conn.execute("UPDATE step_traces SET steps='{не json' WHERE chat_id=?", (chat_id,))
    assert db.get_traces(chat_id)[0]["steps"] == []


# ══════════════ API ══════════════

def test_traces_endpoint_returns_steps(client):
    chat_id = database.create_chat("API трасса")
    database.save_trace(chat_id, "run-api", [{"kind": "tool", "command": "pwd", "code": 0}])
    payload = client.get(f"/api/chats/{chat_id}/traces").get_json()
    assert payload["chat_id"] == chat_id
    assert payload["traces"][0]["steps"][0]["command"] == "pwd"


def test_chat_endpoint_includes_traces(client):
    chat_id = database.create_chat("Чат с трассой")
    database.add_message(chat_id, "user", "привет")
    database.save_trace(chat_id, "run-2", [{"kind": "step", "text": "думаю"}])
    payload = client.get(f"/api/chats/{chat_id}").get_json()
    assert payload["messages"][0]["content"] == "привет"
    assert payload["traces"][0]["run_id"] == "run-2"


# ══════════════ сквозная запись из агента ══════════════

def test_agent_turn_saves_trace(admin_client, sandbox, monkeypatch):
    _stub_model(monkeypatch, [
        '{"action":"run","command":"pwd","thought":"проверю папку"}',
        "Работаю в рабочей папке.",
    ])
    events = _ndjson(admin_client.post("/api/agent/stream", json={"message": "где я нахожусь"}))
    result = next(event for event in events if event["type"] == "result")
    assert result["chat_id"]

    traces = database.get_traces(result["chat_id"])
    assert traces, "трасса хода должна сохраниться"
    kinds = {step["kind"] for step in traces[-1]["steps"]}
    assert "step" in kinds or "tool" in kinds
    assert any(step.get("command") == "pwd" for step in traces[-1]["steps"])


def test_simple_answer_saves_no_trace(google_client, sandbox, monkeypatch):
    """Простой ответ без инструментов — панели нет, лишних записей нет."""
    _stub_model(monkeypatch, ["Привет! Чем помочь?"])
    events = _ndjson(google_client.post("/api/agent/stream", json={"message": "привет"}))
    result = next(event for event in events if event["type"] == "result")
    assert result["chat_id"]
    assert database.get_traces(result["chat_id"]) == []
