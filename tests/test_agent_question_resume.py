import json


def _events(response):
    return [json.loads(line) for line in response.get_data(as_text=True).splitlines() if line.strip()]


def test_ask_user_pauses_and_resumes_from_persisted_context(client, monkeypatch):
    import tool_registry
    from agent import loop
    chat_id = client.post("/api/chats", json={"title": "Вопрос"}).get_json()["chat_id"]
    monkeypatch.setattr(tool_registry, "call_tool", lambda name, **args: {
        "ok": True, "type": "ask_user", "question": args["question"],
        "options": args.get("options", []),
    })
    responses = iter([
        ('```json\n{"tool":"ask_user","arguments":{"question":"Какой цвет?","options":["синий","красный"]}}\n```', {"model": "mock"}),
        ("Выбрано: синий. Задача выполнена согласно уточнению.", {"model": "mock"}),
    ])
    monkeypatch.setattr("ai_providers.chat_completion", lambda *a, **k: next(responses))

    first = _events(client.post("/api/agent/react", json={"message": "Создай макет", "chat_id": chat_id}))
    ask = next(event for event in first if event.get("type") == "ask_user")
    run_id = ask["run_id"]
    assert ask["question"] == "Какой цвет?"
    import database
    record = database.get_agent_run(run_id)
    assert record["status"] == "paused"
    assert json.loads(record["context"])["goal"] == "Создай макет"

    resumed = _events(client.post("/api/agent/answer", json={"run_id": run_id, "answer": "синий"}))
    assert any(event.get("type") == "result" for event in resumed)
    assert database.get_agent_run(run_id)["status"] == "completed"


def test_completion_outcomes_are_persisted(client):
    import database
    chat_id = client.post("/api/chats", json={}).get_json()["chat_id"]
    record_id = database.create_agent_run("Проверка", chat_id=chat_id, run_id="run-finish", engine="react")
    database.update_agent_run(record_id, status="completed", answer="Проверка завершена")
    assert client.post("/api/agent/finish", json={"run_id": "run-finish", "outcome": "confirmed"}).get_json()["ok"]
    record = database.get_agent_run(record_id)
    assert record["outcome"] == "confirmed"
    assert record["status"] == "confirmed"
