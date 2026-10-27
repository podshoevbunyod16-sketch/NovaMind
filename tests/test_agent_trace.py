"""Трасса шагов агента (бонус Б2): «Что делал агент» после перезагрузки."""
import json

import pytest

import database
import routes.agent as agent_routes
from tests.agent_helpers import script


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

def _client_owner(client):
    """Владелец чата для текущей сессии (identity: anon:<id> для гостя)."""
    import identity
    with client.session_transaction() as sess:
        return identity.chat_owner(sess)


def test_traces_endpoint_returns_steps(client):
    chat_id = database.create_chat("API трасса", owner=_client_owner(client))
    database.save_trace(chat_id, "run-api", [{"kind": "tool", "command": "pwd", "code": 0}])
    payload = client.get(f"/api/chats/{chat_id}/traces").get_json()
    assert payload["chat_id"] == chat_id
    assert payload["traces"][0]["steps"][0]["command"] == "pwd"


def test_chat_endpoint_includes_traces(client):
    chat_id = database.create_chat("Чат с трассой", owner=_client_owner(client))
    database.add_message(chat_id, "user", "привет")
    database.save_trace(chat_id, "run-2", [{"kind": "step", "text": "думаю"}])
    payload = client.get(f"/api/chats/{chat_id}").get_json()
    assert payload["messages"][0]["content"] == "привет"
    assert payload["traces"][0]["run_id"] == "run-2"


# ══════════════ сквозная запись из агента ══════════════

def test_agent_turn_saves_trace(admin_client, sandbox, monkeypatch):
    """Каждый шаг агента попадает в панель «Что делал агент»."""
    script(monkeypatch, ["```bash\npwd\n```", "Работаю в рабочей папке."])
    events = _ndjson(admin_client.post("/api/agent/stream", json={"message": "выполни pwd в терминале"}))
    result = next(event for event in events if event["type"] == "result")
    assert result["chat_id"]

    traces = database.get_traces(result["chat_id"])
    assert traces, "трасса хода должна сохраниться"
    steps = traces[-1]["steps"]
    assert any(step["kind"] == "tool" and step.get("command") == "pwd" for step in steps)
    assert any(step["kind"] == "step" for step in steps)


def test_simple_answer_saves_no_tool_steps(google_client, sandbox, monkeypatch):
    """Простой ответ без инструментов: панели с командами нет."""
    script(monkeypatch, ["Привет! Чем помочь?"])
    events = _ndjson(google_client.post("/api/agent/stream", json={"message": "привет"}))
    result = next(event for event in events if event["type"] == "result")
    assert result["chat_id"]
    traces = database.get_traces(result["chat_id"])
    assert not [step for trace in traces for step in trace["steps"]
                if step["kind"] == "tool"]


def test_trace_is_not_saved_for_offline_turn_without_steps(client, sandbox, monkeypatch):
    """Офлайн-ответ без инструментов не создаёт запись с командами."""
    events = _ndjson(client.post("/api/agent/stream", json={"message": "2+2"}))
    result = next(event for event in events if event["type"] == "result")
    traces = database.get_traces(result["chat_id"])
    assert not [step for trace in traces for step in trace["steps"]
                if step["kind"] == "tool"]


def test_failed_run_is_recorded_with_error(client, sandbox, monkeypatch):
    """Отказ модели всё равно виден в истории запусков."""
    monkeypatch.setattr(agent_routes, "resolve_target",
                        lambda provider=None, model=None: ("groq", "test-model", {}))
    monkeypatch.setattr(agent_routes, "chat_completion",
                        lambda messages, **kwargs: (None, "quota exceeded"))
    events = _ndjson(client.post("/api/agent/stream", json={"message": "привет"}))
    run_id = next(event for event in events if event["type"] == "result")["run_id"]
    run = database.get_agent_run(run_id)
    assert run and run["status"] == "error"
    assert "Модель не ответила" in (run.get("error") or "")
