"""Migration checks: code work uses the owned ReAct workspace, never server source paths."""
import json

import pytest

from agent import workspace as workspaces


def test_workspace_path_resolves_inside_owned_workspace(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    target = workspaces.safe_path(root, "src/main.py")
    assert target == (root / "src/main.py").resolve()
    assert target.is_relative_to(root.resolve())


@pytest.mark.parametrize("path", [
    "../app.py",
    "routes/../../.env",
    "/etc/passwd",
    "workspace/../outside.py",
])
def test_workspace_rejects_traversal_and_absolute_paths(path, tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    with pytest.raises(ValueError):
        workspaces.safe_path(root, path)


def test_workspace_does_not_follow_symlinks_outside_root(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("SECRET = True", encoding="utf-8")
    (root / "link.py").symlink_to(outside)
    with pytest.raises(ValueError):
        workspaces.safe_path(root, "link.py")
    assert workspaces.tree(root) == []


def test_project_coding_routes_are_removed_in_favor_of_react_workspace(client):
    assert client.get("/api/agent/coding/status").status_code == 404
    assert client.post("/api/agent/coding/stream", json={
        "message": "прочитай routes/agent.py",
    }).status_code == 404


def test_guest_cannot_enable_workspace_tools_with_a_request_flag(client, monkeypatch):
    import ai_providers

    received = {}

    def complete(*args, **kwargs):
        received.update(kwargs)
        return "Файловые инструменты доступны только вошедшим пользователям.", {"model": "mock"}

    monkeypatch.setenv("TERMINAL_ENABLED", "1")
    monkeypatch.setattr(ai_providers, "chat_completion", complete)
    response = client.post("/api/agent/react", json={
        "message": "Прочитай main.py", "workspace": True, "tools": True,
    })
    assert response.status_code == 200
    schemas = received.get("tools") or []
    assert not any(item.get("function", {}).get("name") == "file_read" for item in schemas)


def test_signed_in_non_admin_uses_the_same_react_agent(google_client):
    status = google_client.get("/api/agent/status").get_json()
    assert status["available"] is True
    assert status["user"] is True
    assert status["engine"] == "react"


def test_react_reads_only_files_from_the_owned_workspace(client, monkeypatch, tmp_path):
    import ai_providers
    import identity
    import database

    monkeypatch.setenv("AGENT_WORKSPACE_ROOT", str(tmp_path / "workspaces"))
    monkeypatch.setenv("TERMINAL_ENABLED", "1")
    with client.session_transaction() as sess:
        sess["nova_google_login"] = True
        sess["nova_user_nick"] = "Иван"
        sess["nova_user_email"] = "ivan@example.com"
        owner = identity.chat_owner(sess)
    chat_id = client.post("/api/chats", json={"title": "coding workspace"}).get_json()["chat_id"]
    root = workspaces.workspace_root(owner, chat_id)
    (root / "main.py").write_text("print('workspace-only-marker')", encoding="utf-8")
    visible = client.get(f"/api/agent/workspace/tree?chat_id={chat_id}").get_json()
    assert any(item["path"] == "main.py" for item in visible["files"])

    answers = iter([
        ('```json\n{"tool":"file_read","arguments":{"path":"main.py"}}\n```', {}),
        ("Файл main.py содержит print и выводит workspace-only-marker.", {"model": "mock"}),
    ])
    monkeypatch.setattr(ai_providers, "chat_completion", lambda *args, **kwargs: next(answers))
    response = client.post("/api/agent/react", json={
        "chat_id": chat_id,
        "message": "Прочитай main.py и скажи, что он выводит",
        "workspace": True,
        "tools": True,
    })
    events = [json.loads(line) for line in response.get_data(as_text=True).splitlines() if line]
    tool = next(event for event in events if event.get("type") == "tool")
    result = next(event for event in events if event.get("type") == "result")
    assert tool["name"] == "file_read"
    assert "workspace-only-marker" in tool["output"]
    assert "workspace-only-marker" in result["reply"]
    saved = database.list_agent_runs(chat_id)
    assert saved and saved[0]["status"] == "completed"
