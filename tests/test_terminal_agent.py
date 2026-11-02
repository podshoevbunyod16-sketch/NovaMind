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

import routes.terminal as term_routes
import routes.agent as agent_routes


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


# ══════════════ first-party ReAct ══════════════

from tests.agent_helpers import answer_of, events_of, model_text, ndjson, script


def test_agent_status_reports_tools(admin_client):
    status = admin_client.get("/api/agent/status").get_json()
    assert status["enabled"] and status["tools"] and status["available"]
    assert status["max_steps"] >= 1 and status["engine"] == "react"


def test_agent_status_explains_disabled_env(google_client, monkeypatch):
    monkeypatch.setenv("AGENT_ENABLED", "0")
    status = google_client.get("/api/agent/status").get_json()
    assert not status["available"] and "AGENT_ENABLED=1" in status["hint"]


def test_agent_requires_enable_flag(google_client, monkeypatch, sandbox):
    monkeypatch.setenv("AGENT_ENABLED", "0")
    assert google_client.post("/api/agent/stream", json={"message": "привет"}).status_code == 403


def test_agent_works_for_guests_but_workspace_requires_login(client, sandbox):
    response = client.post("/api/agent/stream", json={"message": "привет"})
    response.get_data()
    assert response.status_code == 200
    status = client.get("/api/agent/status").get_json()
    assert status["enabled"] and status["available"] and not status["tools"] and status["guest"]


def test_simple_question_is_answered_in_one_call(google_client, monkeypatch, sandbox):
    queue = script(monkeypatch, ["Привет! Чем помочь?", "лишний ответ"])
    events = ndjson(google_client.post("/api/agent/stream", json={"message": "привет"}))
    assert not events_of(events, "tool") and not events_of(events, "plan")
    assert answer_of(events) == "Привет! Чем помочь?"
    assert queue == ["лишний ответ"]
    assert any(event["type"] == "done" for event in events)


def test_agent_runs_command_then_answers(admin_client, monkeypatch, sandbox):
    script(monkeypatch, ["Сначала проверю вывод.\n```bash\npython3 hello.py\n```",
                         "Скрипт отработал: привет из папки"])
    (sandbox / "hello.py").write_text("print('Привет из папки')", encoding="utf-8")
    events = ndjson(admin_client.post("/api/agent/stream", json={"message": "запусти hello.py"}))
    tool = events_of(events, "tool")[0]
    assert events[0]["type"] == "capability" and tool["name"] == "shell"
    assert any(event["type"] == "stage" for event in events)
    assert tool["result"]["exit_code"] == 0 and "Привет" in tool["result"]["stdout"]
    assert "Скрипт отработал" in answer_of(events)
    assert next(event for event in events if event["type"] == "result")["steps"] == 1


def test_agent_first_party_file_read_returns_content(admin_client, monkeypatch, sandbox):
    (sandbox / "gateway.txt").write_text("настоящий вывод", encoding="utf-8")
    script(monkeypatch, ['```json\n{"tool":"file_read","arguments":{"path":"gateway.txt"}}\n```',
                         "Файл реально прочитан."])
    events = ndjson(admin_client.post("/api/agent/stream",
                                      json={"message": "прочитай gateway.txt"}))
    tool = events_of(events, "tool")[0]
    assert tool["name"] == "file_read" and tool["status"] == "ok"
    assert tool["result"]["content"] == "настоящий вывод"
    assert answer_of(events) == "Файл реально прочитан."


def test_legacy_json_action_is_never_shown_to_user(admin_client, monkeypatch, sandbox):
    script(monkeypatch, ['```json\n{"tool":"legacy_answer","arguments":{"text":"Готово, всё работает"}}\n```',
                         "Старый action не поддерживается."])
    events = ndjson(admin_client.post("/api/agent/stream", json={"message": "проверь"}))
    assert "{" not in answer_of(events)
    assert any(event.get("error_type") == "unknown_tool" for event in events_of(events, "tool"))
    assert next(event for event in events if event["type"] == "result")["reply"]


def test_code_answer_is_not_executed_for_casual_chat(admin_client, monkeypatch, sandbox):
    reply = "```python\nprint('hi')\n```\nЭтот код печатает hi."
    script(monkeypatch, [reply])
    events = ndjson(admin_client.post("/api/agent/stream", json={"message": "привет"}))
    assert not events_of(events, "tool") and answer_of(events) == reply


def test_agent_writes_file_then_runs_it(admin_client, monkeypatch, sandbox):
    script(monkeypatch, ["```python:calc.py\nprint(6 * 7)\n```",
                         "```bash\npython3 calc.py\n```", "Ответ: 42"])
    events = ndjson(admin_client.post("/api/agent/stream",
                                      json={"message": "напиши скрипт calc.py и посчитай 6*7"}))
    tools = events_of(events, "tool")
    assert [tool["name"] for tool in tools][:2] == ["file_write", "shell"]
    assert "42" in tools[1]["result"]["stdout"]
    assert (sandbox / "calc.py").read_text(encoding="utf-8").strip() == "print(6 * 7)"
    assert answer_of(events) == "Ответ: 42"


def test_agent_creates_and_closes_task(admin_client, monkeypatch, sandbox):
    import ai_providers
    import database
    captured_ids = []
    original_create = database.create_task

    def capture_create(*args, **kwargs):
        created = original_create(*args, **kwargs)
        captured_ids.append(created["id"])
        return created

    monkeypatch.setattr(database, "create_task", capture_create)
    turns = []

    def complete(messages, **kwargs):
        turns.append(1)
        if len(turns) == 1:
            return ('```json\n{"tool":"task","arguments":{"title":"Проверить песочницу",'
                    '"detail":"создать файл"}}\n```', {})
        if len(turns) == 2:
            return (json.dumps({"tool": "task_done", "arguments": {
                "task_id": captured_ids[0], "note": "файл создан"}}, ensure_ascii=False), {})
        return "Задача выполнена", {}

    monkeypatch.setattr(ai_providers, "chat_completion", complete)
    events = ndjson(admin_client.post("/api/agent/stream",
                                      json={"message": "создай и закрой задачу проверки"}))
    tools = events_of(events, "tool")
    assert [event["name"] for event in tools] == ["task", "task_done"]
    task = next(item for item in admin_client.get("/api/tasks").get_json()["tasks"]
                if item["id"] == captured_ids[0])
    assert task["source"] == "agent" and task["status"] == "done"
    assert "Задача выполнена" in answer_of(events)


def test_agent_respects_step_limit(admin_client, monkeypatch, sandbox):
    monkeypatch.setenv("AGENT_MAX_STEPS", "2")
    script(monkeypatch, ["```bash\nls\n```", "```bash\npwd\n```",
                         "```bash\nwhoami\n```", "хватит, вот итог"])
    events = ndjson(admin_client.post("/api/agent/stream", json={"message": "покрути команды"}))
    result = next(event for event in events if event["type"] == "result")
    assert result["steps"] == 2 and len(events_of(events, "tool")) == 2
    assert result["status"] == "budget_exhausted"
    assert "лимит шагов" in answer_of(events).lower()
    assert "хватит, вот итог" not in answer_of(events)


def test_agent_failed_command_goes_back_to_model(admin_client, monkeypatch, sandbox):
    seen = []
    script(monkeypatch, ["```bash\npython3 nope.py\n```", "Файла нет — создам его"], seen)
    events = ndjson(admin_client.post("/api/agent/stream", json={"message": "запусти nope.py"}))
    tool = events_of(events, "tool")[0]
    assert tool["result"]["exit_code"] != 0
    feedback = model_text(seen)
    assert "nope.py" in feedback and ("код" in feedback or "No such file" in feedback)


def test_agent_remembers_chat_history(admin_client, monkeypatch, sandbox):
    seen = []
    script(monkeypatch, ["ответ"], seen)
    admin_client.post("/api/agent/stream", json={"message": "меня зовут Бунёд"}).get_data()
    admin_client.post("/api/agent/stream", json={"message": "как меня зовут?"}).get_data()
    assert "Бунёд" in model_text(seen, -1)


def test_model_failure_is_reported_honestly(admin_client, monkeypatch, sandbox):
    import ai_providers
    monkeypatch.setattr(ai_providers, "resolve_target",
                        lambda provider=None, model=None: ("groq", "test-model", {}))
    monkeypatch.setattr(ai_providers, "chat_completion",
                        lambda messages, **kwargs: (None, "quota exceeded"))
    events = ndjson(admin_client.post("/api/agent/stream", json={"message": "привет"}))
    result = next(event for event in events if event["type"] == "result")
    assert result["status"] == "provider_error"
    assert "Не удалось получить ответ модели" in answer_of(events)
    assert "quota exceeded" in answer_of(events)


def test_agent_can_disable_tools_per_request(admin_client, monkeypatch):
    seen = []
    script(monkeypatch, ["Инструменты недоступны — отвечу по памяти"], seen)
    events = ndjson(admin_client.post("/api/agent/stream", json={
        "message": "что у нас в папке?", "tools": False}))
    assert not events_of(events, "tool") and "по памяти" in answer_of(events)
    assert seen[0]["tools"] is None


def test_meta_dict_is_not_mistaken_for_error(admin_client, monkeypatch, sandbox):
    import ai_providers
    monkeypatch.setattr(ai_providers, "resolve_target",
                        lambda provider=None, model=None: ("groq", "test-model", {}))
    monkeypatch.setattr(ai_providers, "chat_completion",
                        lambda messages, **kwargs: ("Просто текстовый ответ модели",
                                                    {"provider": "test", "model": "m"}))
    events = ndjson(admin_client.post("/api/agent/stream", json={"message": "привет"}))
    assert "Просто текстовый ответ" in answer_of(events)


def test_web_search_sources_reach_result(admin_client, monkeypatch, sandbox):
    results = [{"title": "Список стран по ВВП", "url": "https://example.org/gdp",
                "snippet": "…", "host": "example.org"}]
    monkeypatch.setattr(agent_routes, "nova_search", lambda query, limit=6: (
        results, {"backend": "wiki", "query": query, "results": results, "count": 1}))
    script(monkeypatch, ["```web_search\nВВП стран\n```", "Самая богатая — Люксембург."])
    events = ndjson(admin_client.post("/api/agent/stream", json={"message": "найди ВВП стран"}))
    sources = events_of(events, "sources")[0]
    assert sources["sources"][0]["url"] == "https://example.org/gdp"
    result = next(event for event in events if event["type"] == "result")
    assert result["searched"] and "Люксембург" in answer_of(events)


def test_repeated_tool_call_is_rejected_and_reported(admin_client, monkeypatch, sandbox):
    seen = []
    script(monkeypatch, ["```bash\nls\n```", "```bash\nls\n```",
                         "```bash\nls\n```", "Готово: в папке пусто"], seen)
    events = ndjson(admin_client.post("/api/agent/stream", json={"message": "покажи файлы"}))
    tools = events_of(events, "tool")
    assert sum(tool["status"] == "ok" for tool in tools) == 1
    assert any(tool.get("status") == "error" and "repeat_call" in json.dumps(tool)
               for tool in tools), tools
    assert "уже выполнялся" in model_text(seen, -1).lower()
    assert "Готово" in answer_of(events)


def test_think_blocks_and_multiline_answers_are_preserved(admin_client, monkeypatch, sandbox):
    script(monkeypatch, ["<think>надо подумать</think>строка 1\nстрока 2"])
    events = ndjson(admin_client.post("/api/agent/stream", json={"message": "привет"}))
    assert answer_of(events) == "строка 1\nстрока 2"
