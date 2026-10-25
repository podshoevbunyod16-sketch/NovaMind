"""Файловые инструменты агента (этап 6).

file_edit — точечная замена с проверкой уникальности и показом diff,
file_search — поиск по содержимому, file_tree — структура проекта.
Перезапись целого файла разрешена только для новых файлов.
"""
import json

import pytest

import routes.agent as agent_routes
import tool_registry

from tests.agent_helpers import answer_of, events_of, model_text, ndjson, script


def _ndjson(response):
    return ndjson(response)


def _answer(events):
    return answer_of(events)


@pytest.fixture(autouse=True)
def tools_ready():
    tool_registry.bootstrap_default_tools()


@pytest.fixture
def as_admin(admin_client):
    """Инструменты реестра проверяют сессию — вызываем их в контексте запроса."""
    import contextlib
    from flask import session

    @contextlib.contextmanager
    def manager():
        with admin_client.application.test_request_context("/"):
            session["admin_logged_in"] = True
            session["admin_username"] = "admin"
            yield

    return manager


def _stub(monkeypatch, answers):
    queue = list(answers)

    def completion(messages, **kwargs):
        return (queue.pop(0) if queue else "готово"), {}

    monkeypatch.setattr(agent_routes, "chat_completion", completion)
    monkeypatch.setattr(agent_routes, "chat_stream", lambda *a, **k: iter(()))
    return queue


# ══════════════ file_edit ══════════════

def test_file_edit_replaces_unique_occurrence(sandbox, as_admin):
    with as_admin():
        (sandbox / "app.py").write_text("def main():\n    print('привет')\n", encoding="utf-8")

        result = tool_registry.call_tool("file_edit", path="app.py", old="привет", new="пока")

        assert result["ok"] is True and result["replaced"] == 1
        assert "пока" in (sandbox / "app.py").read_text(encoding="utf-8")
        assert "-    print('привет')" in result["diff"]
        assert "+    print('пока')" in result["diff"]

def test_file_edit_rejects_ambiguous_and_missing(sandbox, as_admin):
    with as_admin():
        (sandbox / "many.txt").write_text("да\nда\n", encoding="utf-8")
        ambiguous = tool_registry.call_tool("file_edit", path="many.txt", old="да", new="нет")
        assert ambiguous["ok"] is False and "2 раз" in ambiguous["error"]["message"]

        missing = tool_registry.call_tool("file_edit", path="many.txt", old="нет такого", new="x")
        assert missing["ok"] is False and missing["error"]["type"] == "not_found"

        forced = tool_registry.call_tool("file_edit", path="many.txt", old="да", new="нет",
                                         replace_all=True)
        assert forced["ok"] is True and forced["replaced"] == 2
        assert (sandbox / "many.txt").read_text(encoding="utf-8") == "нет\nнет\n"

def test_file_edit_cannot_escape_workspace(sandbox, as_admin):
    with as_admin():
        result = tool_registry.call_tool("file_edit", path="../секрет.txt", old="a", new="b")
        assert result["ok"] is False

# ══════════════ file_write ══════════════

def test_file_write_is_only_for_new_files(sandbox, as_admin):
    with as_admin():
        first = tool_registry.call_tool("file_write", path="notes/new.md", content="один")
        assert first["ok"] is True

        second = tool_registry.call_tool("file_write", path="notes/new.md", content="два")
        assert second["ok"] is False and "file_edit" in second["error"]["message"]
        assert (sandbox / "notes" / "new.md").read_text(encoding="utf-8") == "один"

        forced = tool_registry.call_tool("file_write", path="notes/new.md", content="два",
                                         allow_overwrite=True)
        assert forced["ok"] is True

# ══════════════ file_search / file_tree ══════════════

def test_file_search_finds_content(sandbox, as_admin):
    with as_admin():
        (sandbox / "src").mkdir()
        (sandbox / "src" / "main.py").write_text("import os\nTODO: проверить\n", encoding="utf-8")
        (sandbox / "readme.md").write_text("просто текст\n", encoding="utf-8")

        result = tool_registry.call_tool("file_search", pattern="todo", path=".")
        assert result["ok"] is True and result["count"] == 1
        match = result["matches"][0]
        assert match["path"] == "src/main.py" and match["line"] == 2 and "TODO" in match["text"]

        assert tool_registry.call_tool("file_search", pattern="нет-такого")["count"] == 0
        bad = tool_registry.call_tool("file_search", pattern="([")
        assert bad["ok"] is False and bad["error"]["type"] == "bad_regex"

def test_file_search_ignores_git_and_venv(sandbox, as_admin):
    with as_admin():
        (sandbox / ".git").mkdir()
        (sandbox / ".git" / "config").write_text("pattern here\n", encoding="utf-8")
        (sandbox / "app.py").write_text("pattern here\n", encoding="utf-8")
        result = tool_registry.call_tool("file_search", pattern="pattern")
        assert [item["path"] for item in result["matches"]] == ["app.py"]

def test_file_tree_lists_workspace(sandbox, as_admin):
    with as_admin():
        (sandbox / "docs").mkdir()
        (sandbox / "docs" / "guide.md").write_text("текст", encoding="utf-8")
        result = tool_registry.call_tool("file_tree", path=".", depth=2)
        assert result["ok"] is True
        assert "docs/" in result["tree"] and "docs/guide.md" in result["tree"]

# ══════════════ действия агента ══════════════

def test_agent_edits_file_via_file_edit(admin_client, sandbox, monkeypatch):
    """file_edit меняет только нужный фрагмент и показывает diff."""
    (sandbox / "app.py").write_text("VERSION = '1.0'\n", encoding="utf-8")
    script(monkeypatch, [
        '```file_edit\n{"path": "app.py", "old": "VERSION = \'1.0\'", "new": "VERSION = \'2.0\'"}\n```',
        "Обновил версию",
    ])

    events = _ndjson(admin_client.post("/api/agent/stream",
                                       json={"message": "обнови версию в app.py до 2.0"}))
    assert "VERSION = '2.0'" in (sandbox / "app.py").read_text(encoding="utf-8")
    tool = next(event for event in events if event["type"] == "tool" and event["name"] == "file_edit")
    assert "- VERSION = '1.0'" in tool["output"] and "+ VERSION = '2.0'" in tool["output"]
    assert "Обновил версию" in _answer(events)


def test_file_edit_refuses_ambiguous_or_missing_fragment(admin_client, sandbox, monkeypatch):
    (sandbox / "app.py").write_text("x = 1\nx = 1\n", encoding="utf-8")
    seen = []
    script(monkeypatch, [
        '```file_edit\n{"path": "app.py", "old": "x = 1", "new": "x = 2"}\n```',
        '```file_edit\n{"path": "app.py", "old": "нет такого", "new": "x = 3"}\n```',
        "Файл не тронут",
    ], seen)

    events = _ndjson(admin_client.post("/api/agent/stream",
                                       json={"message": "поправь переменную в файле app.py"}))
    assert (sandbox / "app.py").read_text(encoding="utf-8") == "x = 1\nx = 1\n"
    tools = events_of(events, "tool")
    assert [tool["code"] for tool in tools] == [1, 1]
    assert "неоднозначна" in tools[0]["output"]
    assert "нет фрагмента" in tools[1]["output"]


def test_agent_write_existing_file_returns_hint(admin_client, sandbox, monkeypatch):
    """Полная перезапись существующего файла запрещена — модель получает подсказку."""
    (sandbox / "app.py").write_text("старое содержимое\n", encoding="utf-8")
    script(monkeypatch, [
        "```python:app.py\nновое содержимое\n```",
        "Нужна точечная правка",
    ])

    events = _ndjson(admin_client.post("/api/agent/stream",
                                       json={"message": "перепиши файл app.py"}))
    assert (sandbox / "app.py").read_text(encoding="utf-8") == "старое содержимое\n"
    tool = events_of(events, "tool")[0]
    assert tool["code"] == 1 and "file_edit" in tool["output"]
    assert "Нужна точечная правка" in _answer(events)


def test_agent_creates_new_file_and_lists_it(admin_client, sandbox, monkeypatch):
    script(monkeypatch, [
        "```python:notes/todo.md\n- первая задача\n```",
        "```bash\nls notes\n```",
        "Создал notes/todo.md",
    ])

    events = _ndjson(admin_client.post("/api/agent/stream",
                                       json={"message": "создай файл со списком задач в папке notes"}))
    assert (sandbox / "notes" / "todo.md").read_text(encoding="utf-8") == "- первая задача\n"
    tools = events_of(events, "tool")
    assert tools[0]["name"] == "python" and "todo.md" in tools[0]["command"]
    assert "todo.md" in tools[1]["output"]


def test_agent_greps_and_lists_tree(admin_client, sandbox, monkeypatch):
    """Поиск по содержимому и структура папки — через песочницу (bash)."""
    (sandbox / "main.py").write_text("def hello():\n    return 1\n", encoding="utf-8")
    script(monkeypatch, [
        "```bash\ngrep -n hello main.py\n```",
        "```bash\nls -R\n```",
        "Нашёл функцию",
    ])

    events = _ndjson(admin_client.post("/api/agent/stream",
                                       json={"message": "найди функцию hello в коде проекта"}))
    tools = [event["name"] for event in events if event["type"] == "tool"]
    assert tools == ["terminal", "terminal"]
    assert "1:def hello():" in events_of(events, "tool")[0]["output"]
    assert "main.py" in events_of(events, "tool")[1]["output"]
    assert "Нашёл функцию" in _answer(events)
