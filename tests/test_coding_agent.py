"""Read-first Auto Coding Agent: доступ, пути, протокол блоков."""
import pytest

import agenticseek.tools.project_tools as project_tools
import routes.coding_agent as coding_agent
from tests.agent_helpers import answer_of, events_of, model_text, ndjson, script


# ══════════════ доступ к файлам проекта ══════════════

def test_coding_agent_path_allows_project_source():
    path = project_tools.safe_project_path("routes/agent.py")
    assert path == project_tools.PROJECT_ROOT / "routes" / "agent.py"


@pytest.mark.parametrize("path", [
    "../app.py",
    "routes/../../.env",
    "/etc/passwd",
    ".env",
    ".git/config",
    "workspace/notes.md",
    "novamind_chats.db",
])
def test_coding_agent_rejects_private_or_external_paths(path):
    with pytest.raises(ValueError):
        project_tools.safe_project_path(path)


def test_coding_agent_rejects_unknown_file_types():
    with pytest.raises(ValueError):
        project_tools.safe_project_path("app.pem")
    with pytest.raises(ValueError):
        project_tools.safe_project_path("")


def test_coding_agent_does_not_follow_symlinks_outside_project(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("SECRET = True", encoding="utf-8")
    (project / "link.py").symlink_to(outside)
    (project / "inside.py").write_text("OK = 1", encoding="utf-8")
    monkeypatch.setattr(project_tools, "PROJECT_ROOT", project)

    names = [path.name for path in project_tools.iter_source_files()]
    assert names == ["inside.py"]


# ══════════════ маршрут ══════════════

def test_coding_agent_requires_admin(client, monkeypatch):
    monkeypatch.setenv("AGENT_ENABLED", "1")
    response = client.post(
        "/api/agent/coding/stream",
        json={"message": "Найди ошибку в routes/agent.py"},
    )
    assert response.status_code == 403
    assert "администратору" in response.get_json()["error"]


def test_coding_agent_status_requires_admin(client, monkeypatch):
    monkeypatch.setenv("AGENT_ENABLED", "1")
    status = client.get("/api/agent/coding/status").get_json()
    assert status["admin_required"] is True and status["available"] is False
    assert status["engine"] == "agenticseek"


# ══════════════ протокол блоков ══════════════

def test_coding_agent_reads_project_file(admin_client, monkeypatch):
    seen = []
    script(monkeypatch, ["```project_read\nroutes/coding_agent.py\n```",
                         "Прочитал маршрут, замечаний нет."], seen, module=coding_agent)
    events = ndjson(admin_client.post("/api/agent/coding/stream",
                                      json={"message": "проверь routes/coding_agent.py"}))
    tool = events_of(events, "tool")[0]
    assert tool["name"] == "project_read" and tool["code"] == 0
    assert "Admin-only read-first coding agent" in tool["output"]
    assert answer_of(events) == "Прочитал маршрут, замечаний нет."
    offered = model_text(seen).split("ДОСТУПНЫЕ")[-1]
    assert "project_tree" in offered and "project_patch" in offered


def test_coding_agent_rejects_non_action_text(admin_client, monkeypatch):
    """Ответ без блока — это просто ответ, а не действие."""
    script(monkeypatch, ["Я бы посмотрел routes/agent.py, но вы не просили."], module=coding_agent)
    events = ndjson(admin_client.post("/api/agent/coding/stream",
                                      json={"message": "что бы ты проверил?"}))
    assert not events_of(events, "tool")
    assert "routes/agent.py" in answer_of(events)


def test_coding_agent_patch_is_a_proposal_not_an_edit(admin_client, monkeypatch):
    """Патч показывается пользователю и не применяется к файлу."""
    import routes.agent as agent_routes
    target = agent_routes.__file__
    original = open(target, encoding="utf-8").read()
    script(monkeypatch, [
        '```project_patch\n{"path": "routes/agent.py", "old": "MAX_STEPS", "new": "MAX_STEPS"}\n```',
        "Предложил патч.",
    ], module=coding_agent)
    events = ndjson(admin_client.post("/api/agent/coding/stream",
                                      json={"message": "предложи правку routes/agent.py"}))
    assert open(target, encoding="utf-8").read() == original
    tool = events_of(events, "tool")[0]
    assert tool["name"] == "project_patch" and "не применён" in tool["output"]
    patch = events_of(events, "patch")[0]["patch"]
    assert patch["path"] == "routes/agent.py" and patch["applied"] is False
    assert "- MAX_STEPS" in patch["patch"] and "+ MAX_STEPS" in patch["patch"]


def test_coding_agent_blocks_secret_paths_in_blocks(admin_client, monkeypatch):
    seen = []
    script(monkeypatch, ["```project_read\n.env\n```", "Файл закрыт."], seen, module=coding_agent)
    events = ndjson(admin_client.post("/api/agent/coding/stream",
                                      json={"message": "прочитай .env"}))
    tool = events_of(events, "tool")[0]
    assert tool["code"] == 1
    assert "не разрешён" in tool["output"] or "закрыт" in tool["output"].lower()
    assert answer_of(events) == "Файл закрыт."
