"""
Песочница терминала, задачи и агентный цикл — на настоящем HTTP.

Фикстуры sandbox и admin_client живут в tests/conftest.py — ими пользуются
и песочница, и тесты поиска.

Проверяется ровно то, что важно для безопасности:
  * выключенный терминал и не-админ не проходят;
  * команда вне белого списка и опасные конструкции отклоняются;
  * выход за пределы рабочей папки невозможен;
  * команда реально выполняется и отдаёт вывод;
  * задачи создаются, обновляются и удаляются;
  * агент доходит до ответа и не выходит за лимит шагов.
"""
import json
import os

import pytest

import routes.agent as agent_routes
import routes.terminal as term_routes


def _run(client, command):
    return client.post("/api/terminal/run", json={"command": command})


# ══════════════ доступ ══════════════

def test_terminal_is_off_by_default(client, monkeypatch):
    monkeypatch.setenv("TERMINAL_ENABLED", "0")
    status = client.get("/api/terminal/status").get_json()
    assert status["enabled"] is False and status["available"] is False
    assert client.post("/api/terminal/run", json={"command": "ls"}).status_code == 403


def test_terminal_requires_signed_in(client, sandbox):
    response = _run(client, "ls")
    assert response.status_code == 403
    assert "войдите" in response.get_json()["error"].lower()


def test_terminal_works_for_google_user(google_client, sandbox):
    """Вошёл через Google — песочница доступна, админом быть не нужно."""
    assert google_client.get("/api/terminal/status").get_json()["available"] is True
    result = _run(google_client, "pwd").get_json()
    assert result["code"] == 0 and str(sandbox) in result["stdout"]


def test_terminal_works_for_plain_nick(nick_client, sandbox):
    """Быстрый вход по нику тоже открывает песочницу."""
    check = nick_client.get("/api/session/check").get_json()
    assert check["signed_in"] is True and check["admin"] is False
    assert nick_client.get("/api/terminal/status").get_json()["available"] is True
    assert _run(nick_client, "ls").get_json()["code"] == 0


def test_strict_admin_mode_is_optional(google_client, sandbox, monkeypatch):
    """TERMINAL_STRICT_ADMIN=1 возвращает режим «только администратор»."""
    monkeypatch.setenv("TERMINAL_STRICT_ADMIN", "1")
    assert google_client.get("/api/terminal/status").get_json()["available"] is False
    assert _run(google_client, "ls").status_code == 403


def test_logout_closes_workspace(nick_client, sandbox):
    nick_client.post("/api/session/logout")
    assert nick_client.get("/api/terminal/status").get_json()["available"] is False


def test_status_reports_workspace_and_commands(admin_client):
    status = admin_client.get("/api/terminal/status").get_json()
    assert status["available"] is True and status["user"] is True
    assert "ls" in status["commands"] and "git" in status["commands"]


# ══════════════ выполнение ══════════════

def test_allowed_command_runs_in_workspace(admin_client, sandbox):
    result = _run(admin_client, "pwd").get_json()
    assert result["code"] == 0
    assert str(sandbox) in result["stdout"]
    assert result["cwd"] == "."      # путь песочницы относительно самой себя


def test_starter_files_are_created(admin_client, sandbox):
    _run(admin_client, "ls")
    assert (sandbox / "README.md").exists()
    assert (sandbox / "hello.py").exists()


def test_command_outside_allowlist_is_rejected(admin_client):
    response = _run(admin_client, "shutdown -h now")
    assert response.status_code == 400
    assert "не в списке" in response.get_json()["error"]


@pytest.mark.parametrize("command", [
    "cat README.md | sh",              # пайп во вторую программу
    "python3 script.py > /etc/passwd",  # перенаправление
    "ls && rm -rf /",                  # цепочка команд
    "echo `whoami`",                   # подстановка
])
def test_dangerous_shell_constructs_are_rejected(admin_client, command):
    response = _run(admin_client, command)
    assert response.status_code == 400


def test_escape_from_workspace_is_impossible(admin_client):
    result = _run(admin_client, "ls ../../").get_json()
    assert result["code"] != 0 or "workspace" in result["stdout"]


def test_file_api_cannot_leave_workspace(admin_client):
    response = admin_client.post("/api/terminal/file",
                                 json={"action": "read", "path": "../../etc/passwd"})
    assert response.status_code == 400
    assert "пределы" in response.get_json()["error"]


def test_file_read_and_write_roundtrip(admin_client):
    admin_client.post("/api/terminal/file",
                      json={"action": "write", "path": "notes/t.txt", "content": "привет"})
    data = admin_client.post("/api/terminal/file",
                             json={"action": "read", "path": "notes/t.txt"}).get_json()
    assert data["content"] == "привет"


def test_timeout_kills_runaway_command(admin_client, monkeypatch):
    monkeypatch.setattr(term_routes, "COMMAND_TIMEOUT", "1")
    result = _run(admin_client, "python3 -c 'import time; time.sleep(5)'").get_json()
    assert result["timed_out"] is True


def test_files_listing_and_git_panel(admin_client):
    files = admin_client.get("/api/terminal/files").get_json()
    assert any(item["name"] == "README.md" for item in files["files"])
    git = admin_client.get("/api/terminal/git").get_json()
    assert git["repo"] is False and "git init" in git["message"]


# ══════════════ задачи ══════════════

def test_task_lifecycle(client):
    created = client.post("/api/tasks", json={"title": "Починить тесты",
                                              "detail": "прогнать смоук"}).get_json()["task"]
    assert created["status"] == "todo" and created["source"] == "user"

    client.post(f"/api/tasks/{created['id']}/steps",
                json={"title": "подготовить", "status": "doing", "log": "начал"})
    updated = client.patch(f"/api/tasks/{created['id']}", json={"status": "done"}).get_json()["task"]
    assert updated["status"] == "done"
    assert updated["steps"][0]["title"] == "подготовить"

    listed = client.get("/api/tasks").get_json()["tasks"]
    assert any(task["id"] == created["id"] for task in listed)

    assert client.delete(f"/api/tasks/{created['id']}").status_code == 200
    assert not any(task["id"] == created["id"] for task in client.get("/api/tasks").get_json()["tasks"])


def test_task_requires_title(client):
    assert client.post("/api/tasks", json={"title": "   "}).status_code == 400


# ══════════════ агент ══════════════

def _ndjson(response):
    return [json.loads(line) for line in response.get_data(as_text=True).splitlines() if line.strip()]


def _stub_model(monkeypatch, answers):
    """Модель отвечает заранее подготовленными строками по очереди."""
    queue = list(answers)

    def fake_completion(messages, **kwargs):
        return (queue.pop(0) if queue else '{"action":"answer","text":"готово"}'), {}

    def fake_stream(messages, **kwargs):
        text = queue.pop(0) if queue else "готово"
        yield "token", text
        yield "done", ""

    monkeypatch.setattr(agent_routes, "chat_completion", fake_completion)
    monkeypatch.setattr(agent_routes, "chat_stream", fake_stream)
    return queue


def test_agent_status_reports_tools(admin_client):
    status = admin_client.get("/api/agent/status").get_json()
    assert status["enabled"] is True and status["tools"] is True
    assert status["max_steps"] >= 1


def test_agent_requires_enable_flag(google_client, monkeypatch, sandbox):
    monkeypatch.setenv("AGENT_ENABLED", "0")
    assert google_client.post("/api/agent/stream", json={"message": "привет"}).status_code == 403


def test_agent_requires_signed_in(client, sandbox):
    """Агентный режим — как и песочница — только для вошедших."""
    assert client.post("/api/agent/stream", json={"message": "привет"}).status_code == 403
    assert client.get("/api/agent/status").get_json()["tools"] is False


def test_agent_runs_command_then_answers(admin_client, monkeypatch, sandbox):
    _stub_model(monkeypatch, [
        "План: 1) посмотреть файлы 2) ответить",                      # план
        '{"action":"run","command":"python3 hello.py"}',               # действие
        '{"action":"answer","text":"Скрипт отработал: привет из папки"}',  # финал
    ])
    events = _ndjson(admin_client.post("/api/agent/stream", json={"message": "запусти hello.py"}))
    kinds = [event["type"] for event in events]

    assert kinds[0] == "stage"
    assert "plan" in kinds
    assert "tool" in kinds and "step" in kinds
    tool = next(event for event in events if event["type"] == "tool")
    assert tool["code"] == 0 and "Привет" in tool["output"]
    answer = "".join(event["token"] for event in events if event["type"] == "token")
    assert "Скрипт отработал" in answer
    assert events[-1]["type"] == "done"


def test_agent_creates_and_closes_task(admin_client, monkeypatch, sandbox):
    _stub_model(monkeypatch, [
        "План: завести задачу и выполнить",
        '{"action":"task","title":"Проверить песочницу","detail":"создать файл"}',
        '{"action":"write","path":"notes/agent.md","content":"готово"}',
        '{"action":"task_done","note":"файл создан"}',
        '{"action":"answer","text":"Задача выполнена"}',
    ])
    events = _ndjson(admin_client.post("/api/agent/stream", json={"message": "создай заметку"}))
    result = next(event for event in events if event["type"] == "result")

    assert result["task_id"]
    task = admin_client.get("/api/tasks").get_json()["tasks"]
    created = next(item for item in task if item["id"] == result["task_id"])
    assert created["source"] == "agent"
    assert created["status"] == "done"
    assert (sandbox / "notes" / "agent.md").exists()
    assert any(step["title"] == "Записал notes/agent.md" for step in created["steps"])


def test_agent_respects_step_limit(admin_client, monkeypatch, sandbox):
    monkeypatch.setattr(agent_routes, "MAX_STEPS", 2)
    _stub_model(monkeypatch, [
        "План: крутим бесконечно",
        '{"action":"run","command":"ls"}',
        '{"action":"run","command":"ls"}',
        '{"action":"run","command":"ls"}',     # лишний шаг — уже не выполняется
        '{"action":"answer","text":"хватит"}',
    ])
    events = _ndjson(admin_client.post("/api/agent/stream", json={"message": "покрути"}))
    result = next(event for event in events if event["type"] == "result")
    assert result["steps"] <= 2
    assert sum(1 for event in events if event["type"] == "tool") <= 2


def test_agent_treats_plain_text_as_final_answer(admin_client, monkeypatch, sandbox):
    _stub_model(monkeypatch, ["План: сразу отвечу", "Готовый ответ без JSON"])
    events = _ndjson(admin_client.post("/api/agent/stream", json={"message": "привет"}))
    answer = "".join(event["token"] for event in events if event["type"] == "token")
    assert "Готовый ответ" in answer


def test_agent_blocked_tool_is_reported_to_model(admin_client, monkeypatch):
    monkeypatch.setattr(agent_routes, "tools_enabled", lambda: False)
    # без инструментов агент не крутит цикл, а сразу отвечает
    _stub_model(monkeypatch, [
        '{"action":"answer","text":"Инструменты недоступны — отвечу по памяти"}',
    ])
    events = _ndjson(admin_client.post("/api/agent/stream", json={"message": "что у нас в папке?"}))
    assert not [event for event in events if event["type"] == "tool"]
    answer = "".join(event["token"] for event in events if event["type"] == "token")
    assert "Инструменты недоступны" in answer or "по памяти" in answer
