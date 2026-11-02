"""First-party activity-event labels and NDJSON ordering."""
import json

import pytest

from agent.events import tool_presentation
from tests.agent_helpers import answer_of, events_of, model_text, ndjson, script


def test_tool_labels_describe_the_action():
    assert tool_presentation("shell", {"command": "ls -la"}) == {
        "label": "Выполнил команду", "kind": "command", "detail": "ls -la"}
    assert tool_presentation("file_read", {"path": "templates/index.html"})["label"] == "Прочитал файл"
    assert tool_presentation("file_edit", {"path": "app.py"})["label"] == "Изменил файл"
    assert tool_presentation("file_write", {"path": "hello.py"}, {"path": "hello.py"}) == {
        "label": "Создал файл", "kind": "write", "detail": "hello.py"}
    assert tool_presentation("web_search", {"query": "flask магазин"})["label"] == "Поиск в интернете"
    assert tool_presentation("web_open", {"url": "https://example.com/a"})["label"] == "Открыл страницу"
    assert tool_presentation("mcp_call", {"server_id": "exa", "tool": "search"})["detail"] == "exa · search"
    assert tool_presentation("shell", {"command": "python app.py"}, status="error")["label"] == "Ошибка команды"


def test_file_write_label_uses_resolved_workspace_path():
    item = tool_presentation("file_write", {"path": "nested/file.txt"}, {"path": "nested/file.txt"})
    assert item["label"] == "Создал файл" and item["detail"] == "nested/file.txt"


def test_result_step_count_is_emitted_by_react(admin_client, sandbox, monkeypatch):
    script(monkeypatch, ["```bash\npwd\n```", "Готово."])
    events = ndjson(admin_client.post("/api/agent/stream", json={"message": "запусти pwd"}))
    result = next(event for event in events if event["type"] == "result")
    assert result["steps"] == len(events_of(events, "tool")) == 1


def _post(client, text):
    return ndjson(client.post("/api/agent/stream", json={"message": text}))


def test_run_shows_stage_then_command(admin_client, sandbox, monkeypatch):
    script(monkeypatch, ["Сначала посмотрю, что в папке.\n```terminal\nls -la\n```", "В папке пусто."])
    events = _post(admin_client, "посмотри, что в папке")
    assert events[0]["type"] == "capability"
    assert any(event["type"] == "stage" for event in events)
    tool = events_of(events, "tool")[0]
    assert tool["name"] == "shell" and tool["label"] == "Выполнил команду"
    assert json.loads(tool["command"])["command"] == "ls -la"
    assert answer_of(events) == "В папке пусто."


def test_run_shows_read_and_edit_steps(admin_client, sandbox, monkeypatch):
    (sandbox / "app.py").write_text("VERSION = '1.0'\n", encoding="utf-8")
    script(monkeypatch, ["```file_read\napp.py\n```",
                         '```file_edit\n{"path":"app.py","old":"VERSION = \'1.0\'","new":"VERSION = \'2.0\'"}\n```',
                         "Обновил версию."])
    events = _post(admin_client, "обнови версию в app.py")
    steps = events_of(events, "tool")
    assert [step["label"] for step in steps] == ["Прочитал файл", "Изменил файл"]
    assert steps[0]["detail"] == "app.py" and steps[1]["detail"] == "app.py"
    assert "VERSION = '2.0'" in (sandbox / "app.py").read_text(encoding="utf-8")


def test_run_shows_created_file(admin_client, sandbox, monkeypatch):
    script(monkeypatch, ["```python:notes/todo.md\n- первая задача\n```", "Создал notes/todo.md."])
    events = _post(admin_client, "создай файл со списком задач в папке notes")
    step = events_of(events, "tool")[0]
    assert step["label"] == "Создал файл" and step["detail"] == "notes/todo.md"
    assert (sandbox / "notes" / "todo.md").read_text(encoding="utf-8") == "- первая задача"


def test_first_party_shell_and_file_search_steps(admin_client, sandbox, monkeypatch):
    (sandbox / "hello.py").write_text("print('привет')\n", encoding="utf-8")
    script(monkeypatch, ["```bash\npython3 hello.py\n```",
                         '```json\n{"tool":"file_search","arguments":{"pattern":"hello"}}\n```',
                         '```json\n{"tool":"file_read","arguments":{"path":"hello.py"}}\n```',
                         "Скрипт печатает привет."])
    events = _post(admin_client, "запусти hello.py и покажи файл")
    steps = events_of(events, "tool")
    assert [step["name"] for step in steps] == ["shell", "file_search", "file_read"]
    assert steps[0]["label"] == "Выполнил команду" and "привет" in steps[0]["result"]["stdout"]
    assert steps[1]["label"] == "Нашёл файлы"
    assert steps[2]["label"] == "Прочитал файл"


def test_mcp_and_search_are_first_party_tool_events(admin_client, sandbox, monkeypatch):
    import mcp_registry
    import routes.agent as agent_routes
    results = [{"title": "Документация asyncio", "url": "https://example.com/a",
                "snippet": "asyncio — библиотека", "host": "example.com"}]
    monkeypatch.setattr(agent_routes, "nova_search", lambda query, limit=8:
                        (results, {"results": results, "count": 1, "query": query}))
    monkeypatch.setattr(mcp_registry, "connected_tools", lambda: [{"server_id": "exa"}])
    monkeypatch.setattr(mcp_registry, "call_tool", lambda server_id, tool, arguments: {"answer": "42"})
    monkeypatch.setenv("AGENT_CONFIRM", "off")
    script(monkeypatch, ['```json\n{"tool":"mcp_call","arguments":{"server_id":"exa","tool":"search","arguments":{"query":"asyncio"}}}\n```',
                         "```web_search\nasyncio документация\n```", "```web_open\nhttps://example.com/a\n```", "Готово."])
    events = _post(admin_client, "найди asyncio через MCP и интернет")
    steps = events_of(events, "tool")
    assert [step["name"] for step in steps] == ["mcp_call", "web_search", "web_open"]
    assert steps[0]["label"] == "Вызвал подключённый сервис"
    assert steps[1]["label"] == "Поиск в интернете"
    assert steps[2]["label"] == "Открыл страницу"


def test_plain_answer_has_no_tool_step(admin_client, sandbox, monkeypatch):
    script(monkeypatch, ["Просто ответ."])
    events = _post(admin_client, "привет")
    assert not events_of(events, "tool") and answer_of(events) == "Просто ответ."


def test_failed_command_is_marked_in_activity(admin_client, sandbox, monkeypatch):
    script(monkeypatch, ["```terminal\npython3 -c 'raise SystemExit(3)'\n```", "Команда упала."])
    events = _post(admin_client, "запусти код, который упадёт")
    step = events_of(events, "tool")[0]
    assert step["status"] == "error" and step["label"] == "Ошибка команды"
    assert step["result"]["exit_code"] != 0


def test_model_receives_registered_tools_not_markup_prompt(admin_client, sandbox, monkeypatch):
    seen = []
    script(monkeypatch, ["Готово."], seen)
    _post(admin_client, "запусти ls в терминале и покажи файлы проекта")
    assert seen and seen[0]["tools"]
    names = {item["function"]["name"] for item in seen[0]["tools"]}
    assert {"shell", "file_read", "file_write", "file_search", "file_tree", "web_search", "web_open"} <= names
    assert "```bash```" not in model_text(seen)
