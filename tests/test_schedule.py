"""Задачи по расписанию (бонус Б4): cron, интервалы, поток и API."""
from datetime import datetime
import json
import time

import pytest

import database
import routes.agent as agent_routes
from tests.agent_helpers import answer_of, events_of, script
import scheduler


@pytest.fixture
def db(tmp_path, monkeypatch, isolated_env):
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "schedule.db"))
    monkeypatch.setattr(database, "_schedule_ready", set())
    database.init_db()
    return database


def _ndjson(response):
    return [json.loads(line) for line in response.get_data(as_text=True).splitlines() if line.strip()]


# ══════════════ интервалы ══════════════

@pytest.mark.parametrize("text,seconds", [
    ("30m", 1800), ("2h", 7200), ("1d", 86400), ("90", 5400), ("5M", 300),
])
def test_parse_every(text, seconds):
    assert scheduler.parse_every(text) == seconds


@pytest.mark.parametrize("text", ["", "часто", "0m", "30s", "-5m"])
def test_parse_every_rejects_bad_values(text):
    with pytest.raises(ValueError):
        scheduler.parse_every(text)


# ══════════════ cron ══════════════

def test_cron_field_step_and_range():
    assert scheduler.parse_cron_field("*/15", 0, 59) == {0, 15, 30, 45}
    assert scheduler.parse_cron_field("1-5", 1, 31) == {1, 2, 3, 4, 5}
    assert scheduler.parse_cron_field("1,3", 0, 59) == {1, 3}


def test_cron_field_names_and_sunday_wrap():
    assert scheduler.parse_cron_field("MON", 0, 6, scheduler._WEEKDAYS) == {1}
    assert 0 in scheduler.parse_cron_field("7", 0, 6, scheduler._WEEKDAYS, wrap=7)


@pytest.mark.parametrize("expr", ["* * *", "* * * * * *", "60 * * * *", "abc * * * *",
                                  "*/0 * * * *", "1-70 * * * *"])
def test_bad_cron_is_rejected(expr):
    with pytest.raises(ValueError):
        scheduler.parse_cron(expr)
    assert scheduler.validate(expr) is False


def test_cron_matches_specific_minute():
    moment = datetime(2026, 10, 21, 9, 0)
    assert scheduler.cron_matches("0 9 * * *", moment) is True
    assert scheduler.cron_matches("5 9 * * *", moment) is False


def test_cron_matches_weekday():
    wednesday = datetime(2026, 10, 21, 9, 0)           # среда
    assert scheduler.cron_matches("0 9 * * WED", wednesday) is True
    assert scheduler.cron_matches("0 9 * * MON", wednesday) is False


def test_next_cron_time_is_in_future_and_matches():
    now = datetime(2026, 10, 21, 8, 30).timestamp()
    following = scheduler.next_cron_time("0 9 * * *", now)
    assert following > now
    assert scheduler.cron_matches("0 9 * * *", datetime.fromtimestamp(following))


def test_next_cron_time_rolls_to_next_day():
    now = datetime(2026, 10, 21, 10, 30).timestamp()
    following = datetime.fromtimestamp(scheduler.next_cron_time("0 9 * * *", now))
    assert (following.day, following.hour) == (22, 9)


def test_next_run_for_every():
    now = time.time()
    following = scheduler.next_run_for("every", "15m", now)
    assert 890 < following - now < 910


# ══════════════ хранение заданий ══════════════

def test_create_job_adds_result_chat(db):
    job = scheduler.create_job("иван", "сводка новостей", "every", "1h")
    assert job["status"] == "active" and job["runs"] == 0
    assert job["next_run_at"] > time.time()
    assert database.get_chat_history(job["result_chat_id"], limit=5) == []


def test_create_job_rejects_bad_input(db):
    with pytest.raises(ValueError):
        scheduler.create_job("иван", "", "every", "1h")
    with pytest.raises(ValueError):
        scheduler.create_job("иван", "текст", "every", "каждую минуту")
    with pytest.raises(ValueError):
        scheduler.create_job("иван", "текст", "cron", "0 9 * *")
    with pytest.raises(ValueError):
        scheduler.create_job("иван", "текст", "monthly", "1d")


def test_due_jobs_only_returns_ripe(db):
    ripe = scheduler.create_job("иван", "пора", "every", "1h")
    later = scheduler.create_job("иван", "потом", "every", "1h")
    database.update_job(ripe["id"], next_run_at=time.time() - 5)
    database.update_job(later["id"], next_run_at=time.time() + 3600)
    due = database.due_jobs(time.time())
    assert [job["id"] for job in due] == [ripe["id"]]


def test_list_and_delete_jobs(db):
    job = scheduler.create_job("мария", "напоминание", "every", "1d")
    assert [item["id"] for item in database.list_jobs("мария")] == [job["id"]]
    assert database.list_jobs("иван") == []
    assert database.delete_job(job["id"]) == 1
    assert database.get_job(job["id"]) is None


# ══════════════ выполнение ══════════════

def test_tick_runs_due_job(db):
    job = scheduler.create_job("иван", "пришли сводку", "every", "1h")
    database.update_job(job["id"], next_run_at=time.time() - 1)

    calls = []

    def runner(current):
        calls.append(current["id"])
        return {"reply": "Сводка: всё хорошо", "saved": False}

    results = scheduler.tick(runner=runner)
    assert calls == [job["id"]] and results[0]["reply"].startswith("Сводка")

    updated = database.get_job(job["id"])
    assert updated["runs"] == 1 and updated["status"] == "active"
    assert updated["next_run_at"] > time.time()

    messages = database.get_chat_history(job["result_chat_id"], limit=10)
    assert [message["role"] for message in messages] == ["user", "assistant"]
    assert messages[0]["content"] == "пришли сводку"


def test_tick_ignores_jobs_in_the_future(db):
    scheduler.create_job("иван", "потом", "every", "1h")
    assert scheduler.tick(runner=lambda job: {"reply": "не должно"}) == []


def test_broken_job_does_not_stop_others(db):
    first = scheduler.create_job("иван", "упадёт", "every", "1h")
    second = scheduler.create_job("иван", "сработает", "every", "1h")
    database.update_job(first["id"], next_run_at=time.time() - 1)
    database.update_job(second["id"], next_run_at=time.time() - 1)

    def runner(job):
        if job["id"] == first["id"]:
            raise RuntimeError("сломалось")
        return {"reply": "ок", "saved": False}

    scheduler.tick(runner=runner)
    assert database.get_job(first["id"])["status"] == "error"
    assert database.get_job(second["id"])["runs"] == 1


def test_result_chat_is_reused_per_owner(db):
    first = scheduler.create_job("иван", "а", "every", "1h")
    chat = scheduler.result_chat_for("иван")
    assert chat == scheduler.result_chat_for("иван")
    assert chat  # чат существует и не зависит от конкретного задания
    assert first["result_chat_id"]


# ══════════════ API ══════════════

def test_schedule_api_requires_login(client, db):
    assert client.get("/api/schedule").get_json()["jobs"] == []
    assert client.post("/api/schedule", json={"prompt": "привет", "every": "1h"}).status_code == 403


def test_schedule_api_creates_and_deletes(google_client, db):
    created = google_client.post("/api/schedule",
                                 json={"prompt": "сводка новостей", "cron": "0 9 * * *"})
    assert created.status_code == 201
    job = created.get_json()["job"]
    assert job["kind"] == "cron" and job["owner"]
    listed = google_client.get("/api/schedule").get_json()["jobs"]
    assert [item["id"] for item in listed] == [job["id"]]
    assert google_client.delete(f"/api/schedule/{job['id']}").status_code == 200
    assert google_client.get("/api/schedule").get_json()["jobs"] == []


def test_schedule_api_reports_bad_cron(google_client, db):
    response = google_client.post("/api/schedule", json={"prompt": "x", "cron": "0 9 * *"})
    assert response.status_code == 400
    assert "5 полей" in response.get_json()["error"]


def test_schedule_api_blocks_foreign_job(google_client, db):
    job = scheduler.create_job("кто-то другой", "чужое", "every", "1h")
    assert google_client.delete(f"/api/schedule/{job['id']}").status_code == 403
    assert google_client.post(f"/api/schedule/{job['id']}/run").status_code == 403


def test_schedule_run_now(google_client, db, monkeypatch):
    # Владелец теперь проверяемая личность (email:/anon:), а не ник.
    import identity
    with google_client.session_transaction() as sess:
        owner = identity.chat_owner(sess)
    job = scheduler.create_job(owner, "своё", "every", "1h")
    monkeypatch.setattr(scheduler, "run_job",
                        lambda current: {"id": current["id"], "reply": "готово", "error": ""})
    response = google_client.post(f"/api/schedule/{job['id']}/run")
    assert response.status_code == 200
    assert response.get_json()["result"]["reply"] == "готово"


# ══════════════ инструмент агента ══════════════

def _stub_model(monkeypatch, answers):
    queue = list(answers)

    def fake_completion(messages, **kwargs):
        return (queue.pop(0) if queue else "готово"), {}

    def fake_stream(messages, **kwargs):
        yield "token", queue.pop(0) if queue else "готово"
        yield "done", ""

    monkeypatch.setattr(agent_routes, "chat_completion", fake_completion)
    monkeypatch.setattr(agent_routes, "chat_stream", fake_stream)


def test_agent_creates_schedule_via_tool(admin_client, sandbox, db, monkeypatch):
    script(monkeypatch, [
        '```schedule\n{"prompt": "сводка новостей", "cron": "0 9 * * *"}\n```',
        "Готово: каждое утро в 9 буду присылать сводку.",
    ])
    events = _ndjson(admin_client.post("/api/agent/stream",
                                       json={"message": "каждое утро в 9 присылай сводку новостей"}))
    assert any(event["type"] == "schedule" for event in events)
    jobs = database.list_jobs(owner="admin:admin")
    assert len(jobs) == 1 and jobs[0]["expr"] == "0 9 * * *"
    assert "сводку" in answer_of(events)


def test_agent_schedule_needs_expression(admin_client, sandbox, db, monkeypatch):
    script(monkeypatch, [
        '```schedule\n{"prompt": "отчёт"}\n```',
        "Не хватает расписания.",
    ])
    events = _ndjson(admin_client.post("/api/agent/stream",
                                       json={"message": "поставь отчёт по расписанию"}))
    assert not any(event["type"] == "schedule" for event in events)
    assert database.list_jobs(owner="admin:admin") == []
    assert events_of(events, "tool")[0]["code"] == 1
