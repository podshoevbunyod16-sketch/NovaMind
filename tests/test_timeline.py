"""Таймлайн хода работы: «Думал N c» → команда → файл → MCP → поиск.

Проверяем ровно то, что видит пользователь в ходе работы агента: шаги идут
по порядку, у каждого есть человеческая подпись и деталь (команда, путь,
запрос), а старые теги (```bash```, ```file_finder```) продолжают работать
и показываются теми же шагами.
"""

import json

import pytest

from agenticseek import timeline
from agenticseek.core import agent as agent_module
from tests.agent_helpers import answer_of, events_of, model_text, ndjson, script


# ══════════════ подписи шагов (без сети и модели) ══════════════

def test_thought_label_is_human():
    event = timeline.thought_event(4.3, "Проверю маршруты.")
    assert event["type"] == "thought"
    assert event["label"] == "Думал 4 c"
    assert event["seconds"] == 4.3
    assert event["text"] == "Проверю маршруты."
    assert timeline.thought_event(0.2)["label"] == "Думал меньше секунды"


def test_tool_label_speaks_about_the_action():
    command = timeline.tool_event("terminal", "ls -la", command="ls -la")
    assert command["label"] == "Выполнил команду" and command["kind"] == "command"
    assert command["detail"] == "ls -la"

    read = timeline.tool_event("file_read", "templates/index.html")
    assert read["label"] == "Прочитал файл" and read["kind"] == "read"
    assert read["detail"] == "templates/index.html"

    edit = timeline.tool_event("file_edit", json.dumps({"path": "app.py"}))
    assert edit["label"] == "Изменил файл" and edit["files"] == ["app.py"]

    write = timeline.tool_event("file_write", "print(1)", saved="hello.py")
    assert write["label"] == "Создал файл" and write["detail"] == "hello.py"

    search = timeline.tool_event("web_search", "flask магазин")
    assert search["label"] == "Поиск в интернете" and search["kind"] == "search"

    page = timeline.tool_event("web_open", "https://example.com/a")
    assert page["label"] == "Открыл страницу" and page["kind"] == "fetch"

    mcp = timeline.tool_event("mcp_call", json.dumps(
        {"server_id": "exa", "tool": "search", "arguments": {"q": "x"}}))
    assert mcp["label"] == "MCP: exa · search" and mcp["kind"] == "mcp"

    failed = timeline.tool_event("terminal", "python3 app.py", ok=False,
                                 command="python3 app.py")
    assert failed["label"] == "Ошибка команды" and failed["status"] == "error"


def test_python_block_with_path_is_a_created_file():
    """```python:app.py``` — запись файла: так и подписано, без «выполнил Python»."""
    event = timeline.tool_event("python", "print(1)", saved="app.py")
    assert event["label"] == "Создал файл" and event["detail"] == "app.py"


def test_step_counter_reads_naturally():
    assert timeline.summary(1) == "1 шаг"
    assert timeline.summary(3) == "3 шага"
    assert timeline.summary(7, errors=1, thought_seconds=32.4) == "7 шагов · ошибок: 1 · думал 32 c"


# ══════════════ живой поток агента ══════════════

@pytest.fixture()
def fast_thought(monkeypatch):
    """В тестах модель «думает» микросекунды — снижаем порог показа."""
    monkeypatch.setattr(agent_module, "MIN_THOUGHT_SECONDS", 0)


def _post(client, text):
    return ndjson(client.post("/api/agent/stream", json={"message": text}))


def test_run_shows_thought_then_command(admin_client, sandbox, fast_thought, monkeypatch):
    script(monkeypatch, [
        "Сначала посмотрю, что в папке.\n```terminal\nls -la\n```",
        "В папке пусто.",
    ])
    events = _post(admin_client, "посмотри, что в папке")
    kinds = [event["type"] for event in events if event["type"] in ("thought", "tool")]
    assert kinds[:2] == ["thought", "tool"]

    thought = events_of(events, "thought")[0]
    assert thought["label"].startswith("Думал")
    assert "посмотрю" in thought["text"]

    tool = events_of(events, "tool")[0]
    assert tool["name"] == "terminal" and tool["label"] == "Выполнил команду"
    assert "ls -la" in tool["command"]
    assert answer_of(events) == "В папке пусто."


def test_run_shows_read_and_edit_steps(admin_client, sandbox, fast_thought, monkeypatch):
    (sandbox / "app.py").write_text("VERSION = '1.0'\n", encoding="utf-8")
    script(monkeypatch, [
        "```file_read\napp.py\n```",
        '```file_edit\n{"path": "app.py", "old": "VERSION = \'1.0\'", '
        '"new": "VERSION = \'2.0\'"}\n```',
        "Обновил версию.",
    ])
    events = _post(admin_client, "обнови версию в app.py")
    steps = events_of(events, "tool")
    labels = [step["label"] for step in steps]
    assert labels == ["Прочитал файл", "Изменил файл"]
    assert steps[0]["detail"] == "app.py" and steps[1]["files"] == ["app.py"]
    assert "VERSION = '2.0'" in (sandbox / "app.py").read_text(encoding="utf-8")


def test_run_shows_created_file(admin_client, sandbox, fast_thought, monkeypatch):
    script(monkeypatch, [
        '```file_write:notes/todo.md\n- первая задача\n```',
        "Создал notes/todo.md.",
    ])
    events = _post(admin_client, "создай файл со списком задач в папке notes")
    step = events_of(events, "tool")[0]
    assert step["label"] == "Создал файл" and step["detail"] == "notes/todo.md"
    assert (sandbox / "notes" / "todo.md").read_text(encoding="utf-8") == "- первая задача\n"


def test_legacy_bash_and_file_finder_keep_working(admin_client, sandbox,
                                                  fast_thought, monkeypatch):
    """Старые теги не ломают запуск: шаг тот же, просто назван по-новому."""
    (sandbox / "hello.py").write_text("print('привет')\n", encoding="utf-8")
    script(monkeypatch, [
        "```bash\npython3 hello.py\n```",
        "```file_finder\nname=hello.py\n```",
        "```file_finder\naction=read\nname=hello.py\n```",
        "Скрипт печатает привет.",
    ])
    events = _post(admin_client, "запусти hello.py и покажи файл")
    steps = events_of(events, "tool")
    assert [step["name"] for step in steps] == ["terminal", "file_find", "file_find"]
    assert steps[0]["label"] == "Выполнил команду"
    assert "привет" in steps[0]["output"]
    # Старое «прочитать файл» подписано чтением, а не поиском.
    assert steps[1]["label"] == "Нашёл файлы"
    assert steps[2]["label"] == "Прочитал файл"


@pytest.fixture()
def web_and_mcp(monkeypatch):
    """Поиск, чтение страниц и один MCP-сервер — без сети."""
    import mcp_registry
    import routes.agent as agent_routes

    results = [{"title": "Документация asyncio", "url": "https://example.com/a",
                "snippet": "asyncio — библиотека", "host": "example.com"}]

    def search(query, limit=8):
        return list(results), {"backend": "test", "count": len(results), "results": results,
                               "trace": [{"backend": "test", "ok": True, "ms": 3}]}

    def read_page(url, max_chars=6000):
        return f"Текст страницы {url}", None

    monkeypatch.setattr(agent_routes, "nova_search", search)
    monkeypatch.setattr(agent_routes, "_read_page", read_page)
    monkeypatch.setattr(mcp_registry, "connected_tools", lambda: [{"server_id": "exa"}])
    monkeypatch.setattr(mcp_registry, "call_tool",
                        lambda server_id, tool, arguments: {"answer": "42"})
    monkeypatch.setenv("AGENT_CONFIRM", "off")


def test_mcp_and_search_are_timeline_steps(admin_client, sandbox, fast_thought,
                                           web_and_mcp, monkeypatch):
    script(monkeypatch, [
        "```mcp_call\n"
        '{"server_id": "exa", "tool": "search", "arguments": {"query": "asyncio"}}\n```',
        "```web_search\nasyncio документация\n```",
        "```web_open\nhttps://example.com/a\n```",
        "Готово.",
    ])
    events = _post(admin_client, "найди asyncio через MCP и интернет")
    steps = events_of(events, "tool")
    assert [step["name"] for step in steps] == ["mcp_call", "web_search", "web_open"]
    assert steps[0]["label"].startswith("MCP")
    assert steps[1]["label"] == "Поиск в интернете"
    assert steps[2]["label"] == "Открыл страницу"


def test_thought_is_not_shown_for_a_plain_answer(admin_client, sandbox, fast_thought, monkeypatch):
    """«Думал N c» появляется только перед действием, а не перед ответом."""
    script(monkeypatch, ["Просто ответ."])
    events = _post(admin_client, "привет")
    assert events_of(events, "thought") == []
    assert answer_of(events) == "Просто ответ."


def test_failed_command_is_marked_in_the_timeline(admin_client, sandbox,
                                                 fast_thought, monkeypatch):
    script(monkeypatch, [
        "```terminal\npython3 -c 'raise SystemExit(3)'\n```",
        "Команда упала.",
    ])
    events = _post(admin_client, "запусти код, который упадёт")
    step = events_of(events, "tool")[0]
    assert step["status"] == "error" and step["code"] not in (0, None)
    assert step["label"] == "Ошибка команды"
    failures = [event for event in events_of(events, "tool") if event["status"] == "error"]
    assert failures, "ошибка должна быть видна в ходе работы"


def test_prompt_lists_the_timeline_tools_not_the_old_ones(admin_client, sandbox,
                                                         web_and_mcp, monkeypatch):
    seen = []
    script(monkeypatch, ["Готово."], seen)
    _post(admin_client, "запусти ls в терминале и покажи файлы проекта")
    system = model_text(seen)
    tools_block = system.split("ДОСТУПНЫЕ ИНСТРУМЕНТЫ", 1)[-1]
    for tag in ("terminal", "file_read", "file_write", "file_find", "file_edit",
                "mcp_call", "web_search", "web_open"):
        assert f"```{tag}```" in tools_block, tag
    # Старые имена остаются синонимами, но отдельными шагами их не предлагаем.
    assert "- ```file_finder```" not in tools_block
    assert "- ```bash``` —" not in tools_block
    assert "```bash``` тоже работает" in tools_block
