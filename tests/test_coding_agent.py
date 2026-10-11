"""Security and parsing tests for the read-first Auto Coding Agent."""
import pytest

from routes import coding_agent


def test_coding_agent_path_allows_project_source():
    path = coding_agent._safe_path("routes/agent.py")
    assert path == coding_agent.PROJECT_ROOT / "routes" / "agent.py"


@pytest.mark.parametrize("path", [
    "../app.py",
    "routes/../../.env",
    "/etc/passwd",
    ".env",
    ".git/config",
])
def test_coding_agent_rejects_private_or_external_paths(path):
    with pytest.raises(ValueError):
        coding_agent._safe_path(path)


def test_coding_agent_parses_one_json_action():
    action = coding_agent._parse_action('prefix {"action":"read","path":"routes/agent.py"}')
    assert action == {"action": "read", "path": "routes/agent.py"}


def test_coding_agent_rejects_non_action_text():
    assert coding_agent._parse_action("I will inspect the file now.") is None


def test_coding_agent_requires_admin(client, monkeypatch):
    monkeypatch.setenv("AGENT_ENABLED", "1")
    response = client.post(
        "/api/agent/coding/stream",
        json={"message": "Найди ошибку в routes/agent.py"},
    )
    assert response.status_code == 403
    assert "администратору" in response.get_json()["error"]

def test_coding_agent_does_not_follow_symlinks_outside_project(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("SECRET = True", encoding="utf-8")
    (project / "link.py").symlink_to(outside)
    monkeypatch.setattr(coding_agent, "PROJECT_ROOT", project)
    assert list(coding_agent._iter_source_files()) == []

