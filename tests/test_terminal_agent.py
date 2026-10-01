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
    """Модель отвечает заранее подготовленными строками по очереди (потоком)."""
    queue = list(answers)

    def fake_completion(messages, **kwargs):
        return (queue.pop(0) if queue else "готово"), {}

    def fake_stream(messages, **kwargs):
        text = queue.pop(0) if queue else "готово"
        # режем на куски, как настоящий поток
        for index in range(0, len(text), 7):
            yield "token", text[index:index + 7]
        yield "done", ""

    monkeypatch.setattr(agent_routes, "chat_completion", fake_completion)
    monkeypatch.setattr(agent_routes, "chat_stream", fake_stream)
    return queue


def _answer(events):
    return "".join(event["token"] for event in events if event["type"] == "token")


def test_agent_status_reports_tools(admin_client):
    status = admin_client.get("/api/agent/status").get_json()
    assert status["enabled"] is True and status["tools"] is True
    assert status["available"] is True
    assert status["max_steps"] >= 1


def test_agent_status_explains_disabled_env(google_client, monkeypatch):
    monkeypatch.setenv("TERMINAL_ENABLED", "0")
    monkeypatch.setenv("AGENT_ENABLED", "0")
    status = google_client.get("/api/agent/status").get_json()
    assert status["available"] is False
    assert "TERMINAL_ENABLED=1" in status["hint"]


def test_agent_requires_enable_flag(google_client, monkeypatch, sandbox):
    monkeypatch.setenv("AGENT_ENABLED", "0")
    assert google_client.post("/api/agent/stream", json={"message": "привет"}).status_code == 403


def test_agent_requires_signed_in(client, sandbox):
    """Главный чат с Linux — как и песочница — только для вошедших."""
    assert client.post("/api/agent/stream", json={"message": "привет"}).status_code == 403
    assert client.get("/api/agent/status").get_json()["tools"] is False


def test_simple_question_is_answered_in_one_streamed_call(google_client, monkeypatch, sandbox):
    """Простой вопрос: ни плана, ни команд — сразу поток текста."""
    queue = _stub_model(monkeypatch, ["Привет! Чем помочь?", "лишний ответ"])
    events = _ndjson(google_client.post("/api/agent/stream", json={"message": "привет"}))
    kinds = [event["type"] for event in events]
    assert "tool" not in kinds and "plan" not in kinds and "stage" not in kinds
    assert _answer(events) == "Привет! Чем помочь?"
    assert queue == ["лишний ответ"]           # ровно один запрос к модели
    assert events[-1]["type"] == "done"


def test_agent_runs_command_then_answers(admin_client, monkeypatch, sandbox):
    _stub_model(monkeypatch, [
        '{"action":"plan","steps":["запустить hello.py","ответить"]}',
        '{"action":"run","command":"python3 hello.py","thought":"проверю вывод"}',
        "Скрипт отработал: привет из папки",
    ])
    events = _ndjson(admin_client.post("/api/agent/stream", json={"message": "запусти hello.py"}))
    kinds = [event["type"] for event in events]

    assert kinds[0] == "stage"
    assert "plan" in kinds
    plan = next(event for event in events if event["type"] == "plan")
    assert "1. запустить hello.py" in plan["text"]
    assert any(event["type"] == "step" and event["text"] == "проверю вывод" for event in events)
    tool = next(event for event in events if event["type"] == "tool")
    assert tool["code"] == 0 and "Привет" in tool["output"]
    assert "Скрипт отработал" in _answer(events)
    result = next(event for event in events if event["type"] == "result")
    assert result["steps"] == 2
    assert events[-1]["type"] == "done"


def test_json_answer_action_is_printed_as_text(admin_client, monkeypatch, sandbox):
    _stub_model(monkeypatch, ['```json\n{"action":"answer","text":"Готово, всё работает"}\n```'])
    events = _ndjson(admin_client.post("/api/agent/stream", json={"message": "проверь"}))
    assert _answer(events) == "Готово, всё работает"


def test_code_answer_is_not_mistaken_for_action(admin_client, monkeypatch, sandbox):
    reply = "```python\nprint('hi')\n```\nЭтот код печатает hi."
    _stub_model(monkeypatch, [reply])
    events = _ndjson(admin_client.post("/api/agent/stream", json={"message": "код"}))
    assert not [event for event in events if event["type"] == "tool"]
    assert _answer(events) == reply


def test_text_then_action_is_retracted_and_executed(admin_client, monkeypatch, sandbox):
    """Модель начала с фразы, а потом выдала JSON — фразу убираем, действие выполняем."""
    _stub_model(monkeypatch, [
        'Ок: {"action":"run","command":"ls","thought":"посмотрю файлы в рабочей папке"}',
        "В папке есть hello.py",
    ])
    events = _ndjson(admin_client.post("/api/agent/stream", json={"message": "что в папке?"}))
    kinds = [event["type"] for event in events]
    assert "retract" in kinds
    assert kinds.index("retract") < kinds.index("tool")
    after = events[kinds.index("retract"):]
    assert _answer(after).endswith("В папке есть hello.py")


def test_agent_writes_file_then_runs_it(admin_client, monkeypatch, sandbox):
    _stub_model(monkeypatch, [
        '{"action":"write","path":"calc.py","content":"print(6 * 7)"}',
        '{"action":"run","command":"python3 calc.py"}',
        "Ответ: 42",
    ])
    events = _ndjson(admin_client.post("/api/agent/stream", json={"message": "посчитай 6*7 в python"}))
    tools = [event for event in events if event["type"] == "tool"]
    assert [tool["name"] for tool in tools] == ["write", "run"]
    assert tools[1]["output"].strip() == "42"
    assert (sandbox / "calc.py").read_text() == "print(6 * 7)"
    assert _answer(events) == "Ответ: 42"


def test_agent_creates_and_closes_task(admin_client, monkeypatch, sandbox):
    _stub_model(monkeypatch, [
        '{"action":"task","title":"Проверить песочницу","detail":"создать файл"}',
        '{"action":"write","path":"notes/agent.md","content":"готово"}',
        '{"action":"task_done","note":"файл создан"}',
        "Задача выполнена",
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
        '{"action":"run","command":"ls"}',
        '{"action":"run","command":"ls"}',
        "хватит, вот итог",                  # после лимита — только текст
    ])
    events = _ndjson(admin_client.post("/api/agent/stream", json={"message": "покрути"}))
    result = next(event for event in events if event["type"] == "result")
    assert result["steps"] <= 2
    assert sum(1 for event in events if event["type"] == "tool") <= 2
    assert any(event["type"] == "step" and "Шаги закончились" in event["text"] for event in events)
    assert "итог" in _answer(events)


def test_agent_failed_command_goes_back_to_model(admin_client, monkeypatch, sandbox):
    seen = []

    def fake_stream(messages, **kwargs):
        seen.append(messages[-1]["content"])
        text = '{"action":"run","command":"python3 nope.py"}' if len(seen) == 1 else "Файла нет — создам его"
        yield "token", text
        yield "done", ""

    monkeypatch.setattr(agent_routes, "chat_stream", fake_stream)
    events = _ndjson(admin_client.post("/api/agent/stream", json={"message": "запусти nope.py"}))
    tool = next(event for event in events if event["type"] == "tool")
    assert tool["code"] != 0
    assert "код выхода" in seen[1] and "nope.py" in seen[1]


def test_agent_remembers_chat_history(admin_client, monkeypatch, sandbox):
    captured = []

    def fake_stream(messages, **kwargs):
        captured.append(list(messages))
        yield "token", "ответ"
        yield "done", ""

    monkeypatch.setattr(agent_routes, "chat_stream", fake_stream)
    admin_client.post("/api/agent/stream", json={"message": "меня зовут Бунёд"}).get_data()
    admin_client.post("/api/agent/stream", json={"message": "как меня зовут?"}).get_data()
    second = captured[-1]
    assert any("Бунёд" in message["content"] for message in second[:-1])
    assert second[-1]["content"] == "как меня зовут?"


def test_agent_empty_stream_falls_back_to_completion(admin_client, monkeypatch, sandbox):
    def empty_stream(messages, **kwargs):
        yield "error", "обрыв"
        yield "done", ""

    monkeypatch.setattr(agent_routes, "chat_stream", empty_stream)
    monkeypatch.setattr(agent_routes, "chat_completion", lambda messages, **kw: ("Ответ без потока", {}))
    events = _ndjson(admin_client.post("/api/agent/stream", json={"message": "привет"}))
    assert _answer(events) == "Ответ без потока"


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


# ══════════════ агент: ответ приходит всегда ══════════════

def _stub_real_model(monkeypatch, answers):
    """Как настоящий chat_completion: при успехе второе значение — непустой словарь meta."""
    queue = list(answers)

    def fake_completion(messages, **kwargs):
        if not queue:
            return None, "нет больше ответов"
        return queue.pop(0), {"provider": "test", "model": "m", "offline": False}

    monkeypatch.setattr(agent_routes, "chat_completion", fake_completion)
    return queue


def _fake_search(monkeypatch):
    results = [{"title": "Список стран по ВВП", "url": "https://example.org/gdp", "snippet": "…", "host": "example.org"}]
    monkeypatch.setattr(agent_routes, "nova_search", lambda query, limit=6: (
        results, {"backend": "wiki", "elapsed_ms": 5, "query": query, "results": results, "count": 1, "trace": []}))


def test_agent_answer_survives_real_meta_dict(admin_client, monkeypatch, sandbox):
    """Регрессия: непустой meta не должен считаться ошибкой и съедать ответ."""
    _stub_real_model(monkeypatch, ["План: ответить", "Просто текстовый ответ модели"])
    events = _ndjson(admin_client.post("/api/agent/stream", json={"message": "привет"}))
    assert any(event["type"] == "plan" for event in events)
    answer = "".join(event["token"] for event in events if event["type"] == "token")
    assert "Просто текстовый ответ" in answer


def test_agent_step_limit_still_gives_answer_from_findings(admin_client, monkeypatch, sandbox):
    """Модель только ищет и не отвечает — ответ собирается по найденному."""
    monkeypatch.setattr(agent_routes, "MAX_STEPS", 2)
    _fake_search(monkeypatch)
    _stub_real_model(monkeypatch, [
        "План: поискать",
        '{"action":"search","query":"самая богатая страна"}',
        '{"action":"search","query":"самая бедная страна"}',
        "Самая богатая — Люксембург, самая бедная — Бурунди.",     # финальный запрос
    ])
    events = _ndjson(admin_client.post("/api/agent/stream", json={"message": "богатая и бедная страна"}))
    answer = "".join(event["token"] for event in events if event["type"] == "token")
    assert "Люксембург" in answer
    assert "лимит шагов" not in answer.lower()
    sources = next(event for event in events if event["type"] == "sources")
    assert sources["sources"][0]["url"] == "https://example.org/gdp"


def test_agent_skips_repeated_action(admin_client, monkeypatch, sandbox):
    _fake_search(monkeypatch)
    _stub_real_model(monkeypatch, [
        "План: искать",
        '{"action":"search","query":"вопрос"}',
        '{"action":"search","query":"вопрос"}',
        '{"action":"answer","text":"Ответ по найденному"}',
    ])
    events = _ndjson(admin_client.post("/api/agent/stream", json={"message": "вопрос"}))
    assert sum(1 for event in events if event["type"] == "tool") == 1
    assert "Ответ по найденному" in "".join(e["token"] for e in events if e["type"] == "token")


def test_agent_strips_think_and_parses_multiline_json(admin_client, monkeypatch, sandbox):
    _stub_real_model(monkeypatch, [
        "План: коротко",
        '<think>надо подумать {"action":"ls"}</think>{"action":"answer","text":"строка 1\nстрока 2"}',
    ])
    events = _ndjson(admin_client.post("/api/agent/stream", json={"message": "привет"}))
    answer = "".join(event["token"] for event in events if event["type"] == "token")
    assert answer == "строка 1\nстрока 2"


def test_agent_nova_search_via_run_collects_sources(admin_client, monkeypatch, sandbox):
    """Модель вызвала `nova search` командой run — источники и заметки всё равно копятся."""
    _fake_search(monkeypatch)
    import routes.terminal as terminal
    monkeypatch.setattr(terminal, "nova_search", agent_routes.nova_search)
    _stub_real_model(monkeypatch, [
        "План: искать",
        '{"action":"run","command":"nova search страны ввп"}',
        '{"action":"answer","text":"Готово"}',
    ])
    events = _ndjson(admin_client.post("/api/agent/stream", json={"message": "страны"}))
    assert any(event["type"] == "sources" for event in events)
>>>>>>> d42dfa25f0f2c0d68742da19cdd015831aee836a
