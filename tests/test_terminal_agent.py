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


# ══════════════ агент (движок AgenticSeek) ══════════════

from tests.agent_helpers import answer_of, events_of, model_text, ndjson, script


def test_agent_status_reports_tools(admin_client):
    status = admin_client.get("/api/agent/status").get_json()
    assert status["enabled"] is True and status["tools"] is True
    assert status["available"] is True
    assert status["max_steps"] >= 1
    assert status["engine"] == "agenticseek"


def test_agent_status_explains_disabled_env(google_client, monkeypatch):
    monkeypatch.setenv("AGENT_ENABLED", "0")
    status = google_client.get("/api/agent/status").get_json()
    assert status["available"] is False
    assert "AGENT_ENABLED=1" in status["hint"]


def test_agent_requires_enable_flag(google_client, monkeypatch, sandbox):
    monkeypatch.setenv("AGENT_ENABLED", "0")
    assert google_client.post("/api/agent/stream", json={"message": "привет"}).status_code == 403


def test_agent_works_for_guests_but_workspace_requires_login(client, sandbox):
    """Агент доступен всем, а файлы/терминал — только вошедшим."""
    response = client.post("/api/agent/stream", json={"message": "привет"})
    response.get_data()          # поток нужно дочитать, иначе останется открытый контекст
    assert response.status_code == 200
    status = client.get("/api/agent/status").get_json()
    assert status["enabled"] is True and status["available"] is True
    assert status["tools"] is False and status["guest"] is True


def test_simple_question_is_answered_in_one_call(google_client, monkeypatch, sandbox):
    """Простой вопрос: ни плана, ни команд — один запрос к модели."""
    queue = script(monkeypatch, ["Привет! Чем помочь?", "лишний ответ"])
    events = ndjson(google_client.post("/api/agent/stream", json={"message": "привет"}))
    kinds = [event["type"] for event in events]
    assert "tool" not in kinds and "plan" not in kinds
    assert answer_of(events) == "Привет! Чем помочь?"
    assert queue == ["лишний ответ"]
    assert events[-1]["type"] == "done"


def test_agent_runs_command_then_answers(admin_client, monkeypatch, sandbox):
    script(monkeypatch, [
        "Сначала проверю вывод.\n```bash\npython3 hello.py\n```",
        "Скрипт отработал: привет из папки",
    ])
    (sandbox / "hello.py").write_text("print('Привет из папки')", encoding="utf-8")
    events = ndjson(admin_client.post("/api/agent/stream", json={"message": "запусти hello.py"}))
    assert events[0]["type"] == "stage"
    tool = events_of(events, "tool")[0]
    assert tool["code"] == 0 and "Привет" in tool["output"]
    assert "Скрипт отработал" in answer_of(events)
    result = next(event for event in events if event["type"] == "result")
    assert result["steps"] >= 1
    assert events[-1]["type"] == "done"


def test_agent_python_gateway_executes_real_tool_and_shows_command(
        admin_client, monkeypatch, sandbox):
    (sandbox / "gateway.txt").write_text("настоящий вывод", encoding="utf-8")
    command = ("python3 -m novamind_cli call file_read "
               "'{\"path\":\"gateway.txt\"}'")
    script(monkeypatch, [f"```terminal\n{command}\n```", "Файл реально прочитан."])

    events = ndjson(admin_client.post(
        "/api/agent/stream", json={"message": "прочитай gateway.txt через терминал"}))
    tool = events_of(events, "tool")[0]
    assert tool["name"] == "file_read"
    assert tool["command"] == command
    assert "настоящий вывод" in tool["output"]
    assert tool["status"] == "ok"
    assert answer_of(events) == "Файл реально прочитан."


def test_legacy_json_action_is_never_shown_to_user(admin_client, monkeypatch, sandbox):
    """Старый протокол удалён: JSON-действие не попадает в чат как ответ."""
    script(monkeypatch, ['{"action":"answer","text":"Готово, всё работает"}'])
    events = ndjson(admin_client.post("/api/agent/stream", json={"message": "проверь"}))
    assert "{" not in answer_of(events)
    result = next(event for event in events if event["type"] == "result")
    assert result["status"] in ("ok", "error")


def test_code_answer_is_not_executed_for_casual_chat(admin_client, monkeypatch, sandbox):
    """В обычном разговоре код в ответе — это текст, а не действие."""
    reply = "```python\nprint('hi')\n```\nЭтот код печатает hi."
    script(monkeypatch, [reply])
    events = ndjson(admin_client.post("/api/agent/stream", json={"message": "привет"}))
    assert not events_of(events, "tool")
    assert answer_of(events) == reply


def test_agent_writes_file_then_runs_it(admin_client, monkeypatch, sandbox):
    script(monkeypatch, [
        "```python:calc.py\nprint(6 * 7)\n```",
        "```bash\npython3 calc.py\n```",
        "Ответ: 42",
    ])
    events = ndjson(admin_client.post("/api/agent/stream",
                                      json={"message": "напиши скрипт calc.py и посчитай 6*7"}))
    tools = events_of(events, "tool")
    assert [tool["name"] for tool in tools][:2] == ["python", "terminal"]
    assert "42" in tools[1]["output"].strip()
    assert (sandbox / "calc.py").read_text(encoding="utf-8").strip() == "print(6 * 7)"
    assert answer_of(events) == "Ответ: 42"


def test_agent_creates_and_closes_task(admin_client, monkeypatch, sandbox):
    script(monkeypatch, [
        '```task\n{"title": "Проверить песочницу", "detail": "создать файл"}\n```',
        '```python:notes/agent.md\nготово\n```',
        '```task_done\n{"note": "файл создан"}\n```',
        "Задача выполнена",
    ])
    events = ndjson(admin_client.post("/api/agent/stream",
                                      json={"message": "создай заметку в рабочей папке"}))
    result = next(event for event in events if event["type"] == "result")
    assert result["task_id"]
    tasks = admin_client.get("/api/tasks").get_json()["tasks"]
    created = next(item for item in tasks if item["id"] == result["task_id"])
    assert created["source"] == "agent"
    assert created["status"] == "done"
    assert (sandbox / "notes" / "agent.md").exists()
    assert "Задача выполнена" in answer_of(events)


def test_agent_respects_step_limit(admin_client, monkeypatch, sandbox):
    """Лимит шагов движка: после него агент честно останавливается."""
    monkeypatch.setattr(agent_routes, "MAX_STEPS", 2)
    script(monkeypatch, [
        "```bash\nls\n```",
        "```bash\npwd\n```",
        "```bash\nwhoami\n```",
        "хватит, вот итог",
    ])
    events = ndjson(admin_client.post("/api/agent/stream", json={"message": "покрути команды"}))
    result = next(event for event in events if event["type"] == "result")
    assert result["steps"] == 2
    assert len(events_of(events, "tool")) == 2
    assert any("лимит шагов" in event["text"].lower() for event in events_of(events, "step"))
    # После лимита агент не бросает пользователя: просит модель собрать итог по найденному.
    assert result["status"] == "ok"
    assert "хватит, вот итог" in answer_of(events)


def test_agent_failed_command_goes_back_to_model(admin_client, monkeypatch, sandbox):
    seen = []
    script(monkeypatch, [
        "```bash\npython3 nope.py\n```",
        "Файла нет — создам его",
    ], seen)
    events = ndjson(admin_client.post("/api/agent/stream", json={"message": "запусти nope.py"}))
    tool = events_of(events, "tool")[0]
    assert tool["code"] != 0
    feedback = model_text(seen)
    assert "nope.py" in feedback and ("код" in feedback or "No such file" in feedback)


def test_agent_remembers_chat_history(admin_client, monkeypatch, sandbox):
    seen = []
    script(monkeypatch, ["ответ"], seen)
    admin_client.post("/api/agent/stream", json={"message": "меня зовут Бунёд"}).get_data()
    admin_client.post("/api/agent/stream", json={"message": "как меня зовут?"}).get_data()
    assert "Бунёд" in model_text(seen, -1)


def test_model_failure_is_reported_honestly(admin_client, monkeypatch, sandbox):
    """Модель недоступна: пользователь получает ошибку, а не зависание."""
    monkeypatch.setattr(agent_routes, "resolve_target",
                        lambda provider=None, model=None: ("groq", "test-model", {}))
    monkeypatch.setattr(agent_routes, "chat_completion",
                        lambda messages, **kwargs: (None, "quota exceeded"))
    events = ndjson(admin_client.post("/api/agent/stream", json={"message": "привет"}))
    assert events_of(events, "error")
    result = next(event for event in events if event["type"] == "result")
    assert result["status"] == "error"
    assert "Модель не ответила" in answer_of(events)
    assert events[-1]["type"] == "done"


def test_agent_blocked_tool_is_reported_to_model(admin_client, monkeypatch):
    monkeypatch.setattr(agent_routes, "tools_enabled", lambda: False)
    seen = []
    script(monkeypatch, ["Инструменты недоступны — отвечу по памяти"], seen)
    events = ndjson(admin_client.post("/api/agent/stream", json={"message": "что у нас в папке?"}))
    assert not events_of(events, "tool")
    assert "по памяти" in answer_of(events)
    assert "```bash" not in model_text(seen).split("ДОСТУПНЫЕ")[-1]


def test_meta_dict_is_not_mistaken_for_error(admin_client, monkeypatch, sandbox):
    """Регрессия: непустой meta не должен считаться ошибкой и съедать ответ."""
    monkeypatch.setattr(agent_routes, "resolve_target",
                        lambda provider=None, model=None: ("groq", "test-model", {}))
    monkeypatch.setattr(agent_routes, "chat_completion",
                        lambda messages, **kwargs: ("Просто текстовый ответ модели",
                                                    {"provider": "test", "model": "m"}))
    events = ndjson(admin_client.post("/api/agent/stream", json={"message": "привет"}))
    assert "Просто текстовый ответ" in answer_of(events)


def test_web_search_sources_reach_result(admin_client, monkeypatch, sandbox):
    results = [{"title": "Список стран по ВВП", "url": "https://example.org/gdp",
                "snippet": "…", "host": "example.org"}]
    monkeypatch.setattr(agent_routes, "nova_search", lambda query, limit=6: (
        results, {"backend": "wiki", "query": query, "results": results, "count": 1}))
    script(monkeypatch, [
        "```web_search\nВВП стран\n```",
        "Самая богатая — Люксембург.",
    ])
    events = ndjson(admin_client.post("/api/agent/stream",
                                      json={"message": "найди ВВП стран"}))
    sources = events_of(events, "sources")[0]
    assert sources["sources"][0]["url"] == "https://example.org/gdp"
    result = next(event for event in events if event["type"] == "result")
    assert result["searched"] is True and "Люксембург" in answer_of(events)


def test_repeated_block_is_not_executed_twice(admin_client, monkeypatch, sandbox):
    """Третий одинаковый блок уже не выполняется: модель получает отказ."""
    seen = []
    script(monkeypatch, [
        "```bash\nls\n```",
        "```bash\nls\n```",
        "```bash\nls\n```",
        "Готово: в папке пусто",
    ], seen)
    events = ndjson(admin_client.post("/api/agent/stream", json={"message": "покажи файлы"}))
    assert len(events_of(events, "tool")) == 2
    assert "не повторяй" in model_text(seen, -1).lower()
    assert "Готово" in answer_of(events)


def test_think_blocks_and_multiline_answers_are_preserved(admin_client, monkeypatch, sandbox):
    script(monkeypatch, ["<think>надо подумать</think>строка 1\nстрока 2"])
    events = ndjson(admin_client.post("/api/agent/stream", json={"message": "привет"}))
    assert answer_of(events) == "строка 1\nстрока 2"
