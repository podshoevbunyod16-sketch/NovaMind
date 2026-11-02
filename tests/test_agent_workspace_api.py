import io
import zipfile


def test_workspace_zip_tree_preview_and_traversal(client, monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_WORKSPACE_ROOT", str(tmp_path / "workspaces"))
    chat_id = client.post("/api/chats", json={"title": "Workspace"}).get_json()["chat_id"]
    from flask import session
    # create a file under the exact owner/session namespace used by the routes
    with client.session_transaction() as sess:
        owner = "admin:" + str(sess.get("admin_username") or "admin") if sess.get("admin_logged_in") else "anon:" + sess["nova_sid"]
    import identity
    from agent.workspace import workspace_root
    root = workspace_root(owner, chat_id)
    (root / "report.txt").write_text("Привет", encoding="utf-8")

    listing = client.get(f"/api/agent/workspace/tree?chat_id={chat_id}").get_json()
    assert listing["files"][0]["path"] == "report.txt"
    preview = client.get(f"/api/agent/workspace/file?chat_id={chat_id}&path=report.txt").get_json()
    assert preview["content"] == "Привет"
    assert client.get(f"/api/agent/workspace/file?chat_id={chat_id}&path=../secret").status_code == 400
    archive = zipfile.ZipFile(io.BytesIO(client.get(f"/api/agent/workspace/zip?chat_id={chat_id}").data))
    assert archive.namelist() == ["report.txt"]


def test_workspace_isolated_between_browsers(client, monkeypatch, tmp_path):
    from app import app
    monkeypatch.setenv("AGENT_WORKSPACE_ROOT", str(tmp_path / "workspaces"))
    first = app.test_client()
    second = app.test_client()
    first_chat = first.post("/api/chats", json={}).get_json()["chat_id"]
    first.get("/api/agent/workspace/tree?chat_id=" + first_chat)
    response = second.get("/api/agent/workspace/tree?chat_id=" + first_chat)
    assert response.status_code == 404
