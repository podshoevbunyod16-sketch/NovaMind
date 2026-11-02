import json

from agent.loop import AgentContext, run_react
from tool_registry import ToolSpec


def test_risky_tool_waits_for_approval_and_executes_after_resume(monkeypatch):
    import agent.policy as policy
    monkeypatch.setenv("AGENT_CONFIRM", "write")
    monkeypatch.setattr(policy, "_MODE", None)
    tool_calls = []
    answers = iter([
        ('```json\n{"tool":"write","arguments":{"path":"x.txt","content":"x"}}\n```', {}),
        ('```json\n{"tool":"write","arguments":{"path":"x.txt","content":"x"}}\n```', {}),
        ("Файл записан согласно разрешённому действию.", {}),
    ])
    spec = ToolSpec("write", "Создать файл", {"type": "object", "properties": {
        "path": {"type": "string"}, "content": {"type": "string"}},
        "required": ["path", "content"]}, lambda **kwargs: {"ok": True}, risk="write")

    def call_tool(name, **kwargs):
        tool_calls.append((name, kwargs))
        return {"ok": True, "result": "created"}

    complete = lambda *a, **k: next(answers)
    first = run_react(AgentContext("Создай файл x.txt", complete=complete,
                                   tools=[spec], call_tool=call_tool, run_id="r-1"))
    assert first.status == "paused"
    assert not tool_calls
    assert first.context["pending_confirmation"]["tool"] == "write"
    assert first.context["approvals"] == []
    resumed = run_react(AgentContext("Создай файл x.txt", complete=complete,
        tools=[spec], call_tool=call_tool, run_id="r-1", resume=True,
        history=first.context["messages"],
        approvals=[first.context["pending_confirmation"]["approval_key"]]))
    assert resumed.status == "completed"
    assert len(tool_calls) == 1


def test_confirmation_endpoint_rejects_action_without_approval(client, monkeypatch):
    import database
    chat_id = client.post("/api/chats", json={}).get_json()["chat_id"]
    record_id = database.create_agent_run("Создай файл", chat_id=chat_id,
                                          run_id="run-confirm", engine="react")
    database.update_agent_run(record_id, status="paused",
        context={"goal": "Создай файл", "messages": [], "workspace": True},
        pending_question={"type": "confirmation", "tool": "file_write"})
    response = client.post("/api/agent/answer", json={"run_id": "run-confirm", "approved": False})
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "отклонено пользователем" in body
    assert database.get_agent_run(record_id)["status"] == "abandoned"


def test_finish_cannot_confirm_a_running_or_paused_run(client):
    import database
    chat_id = client.post("/api/chats", json={}).get_json()["chat_id"]
    for status in ("running", "paused"):
        run_id = "finish-block-" + status
        record_id = database.create_agent_run("Ещё выполняется", chat_id=chat_id,
                                              run_id=run_id, engine="react")
        database.update_agent_run(record_id, status=status)
        response = client.post("/api/agent/finish", json={"run_id": run_id,
                                                           "outcome": "confirmed"})
        assert response.status_code == 409
        assert database.get_agent_run(record_id)["status"] == status
        assert database.get_agent_run(record_id)["outcome"] == ""


def test_resume_restores_observations_and_step_offset(client, monkeypatch):
    import ai_providers
    import database

    chat_id = client.post("/api/chats", json={}).get_json()["chat_id"]
    run_id = "resume-observations"
    record_id = database.create_agent_run("Объясни SQLite", chat_id=chat_id,
                                          run_id=run_id, engine="react")
    prior_observations = [{"tool": "lookup", "arguments": {"query": "sqlite"},
                           "result": {"ok": True, "result": "SQLite is embedded."}}]
    prior_messages = [
        {"role": "system", "content": "Задача: Объясни SQLite"},
        {"role": "user", "content": "Объясни SQLite"},
        {"role": "assistant", "content": '{"tool":"lookup","arguments":{"query":"sqlite"}}'},
        {"role": "user", "content": 'Наблюдение инструмента lookup: {"ok":true,"result":"SQLite is embedded."}'},
    ]
    database.update_agent_run(record_id, status="paused", steps=1,
        context={"goal": "Объясни SQLite", "messages": prior_messages,
                 "observations": prior_observations, "steps": 1, "workspace": False,
                 "tools_enabled": False, "approvals": []},
        pending_question={"type": "ask_user", "question": "Нужна деталь"})
    captured = []

    def complete(messages, **kwargs):
        captured.append(messages)
        return "SQLite — встроенная база данных.", {"model": "fixture"}

    monkeypatch.setattr(ai_providers, "chat_completion", complete)
    response = client.post("/api/agent/answer", json={"run_id": run_id, "answer": "Для приложения"})
    assert response.status_code == 200
    events = [json.loads(line) for line in response.get_data(as_text=True).splitlines() if line]
    result = next(event for event in events if event.get("type") == "result")
    assert result["steps"] == 1
    assert "SQLite is embedded" in str(captured[0])
    saved = database.get_agent_run(record_id)
    assert saved["steps"] == 1
    assert json.loads(saved["context"])["observations"] == prior_observations
