import json
import time


def test_stop_endpoint_cancels_react_cycle_between_provider_and_next_step(client, monkeypatch):
    import ai_providers
    chat_id = client.post("/api/chats", json={"title": "Stop ReAct"}).get_json()["chat_id"]
    def delayed(*_args, **_kwargs):
        time.sleep(0.15)
        return ("Остановленный ответ не должен объявлять задачу выполненной.", {"model": "mock"})
    monkeypatch.setattr(ai_providers, "chat_completion", delayed)
    response = client.post("/api/agent/react", json={"message": "Проверь задачу", "chat_id": chat_id,
                                                     "tools": False}, buffered=False)
    run_id = response.headers["X-Agent-Run-Id"]
    stop = client.post("/api/agent/react/stop", json={"run_id": run_id})
    assert stop.status_code == 200
    events = [json.loads(line) for chunk in response.response for line in chunk.splitlines() if line]
    result = next(event for event in events if event.get("type") == "result")
    assert result["status"] == "cancelled"
    assert "остановлена" in result["reply"]
