"""Тесты автономного выбора инструментов, MCP-серверов и чтения дерева GitHub."""

from __future__ import annotations

import mcp_registry
import routes.agent as agent_routes
from tests.agent_helpers import answer_of, events_of, model_text, ndjson, script


class _FakeHttpxResp:
    def __init__(self, status_code: int, payload=None, text: str = ""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


def test_github_sketch_url_inspects_repo_tree_instead_of_generating_image(
    admin_client, sandbox, monkeypatch
):
    """Точное воспроизведение бага: ``-sketch`` в GitHub URL не запускает Flux,
    а агент сам открывает репозиторий и возвращает его реальное дерево файлов."""
    prompt = "https://github.com/podshoevbunyod16-sketch/NovaMind дай мне его структура в виде дерево"
    assert agent_routes.requested_media_kind(prompt) is None

    import httpx

    def fake_get(url, headers=None, params=None, timeout=15, follow_redirects=False):
        if url.endswith("/repos/podshoevbunyod16-sketch/NovaMind"):
            return _FakeHttpxResp(200, {
                "stargazers_count": 7,
                "language": "Python",
                "description": "NovaMind AI agent",
                "default_branch": "main",
            })
        if "/git/trees/main" in url:
            return _FakeHttpxResp(200, {
                "tree": [
                    {"path": "agenticseek", "type": "tree"},
                    {"path": "agenticseek/engine.py", "type": "blob"},
                    {"path": "agenticseek/router.py", "type": "blob"},
                    {"path": "routes", "type": "tree"},
                    {"path": "routes/agent.py", "type": "blob"},
                    {"path": "app.py", "type": "blob"},
                ]
            })
        if url.endswith("/readme"):
            return _FakeHttpxResp(200, text="# NovaMind\nАвтономный агент.")
        return _FakeHttpxResp(404)

    monkeypatch.setattr(httpx, "get", fake_get)

    seen = []
    script(
        monkeypatch,
        [
            "```web_open\nhttps://github.com/podshoevbunyod16-sketch/NovaMind\n```",
            "Структура репозитория NovaMind:\n├── agenticseek/\n├── routes/\n└── app.py",
        ],
        seen,
    )
    events = ndjson(admin_client.post("/api/agent/stream", json={"message": prompt}))

    # Генератор картинок НЕ вызывался
    assert events_of(events, "media") == []
    tools = events_of(events, "tool")
    assert len(tools) == 1 and tools[0]["name"] == "web_open"
    assert "├── agenticseek/" in tools[0]["output"]
    assert "├── routes/" in tools[0]["output"]
    assert "└── app.py" in tools[0]["output"]
    assert "agenticseek/" in answer_of(events)


def test_read_page_formats_github_subpath_and_blob(monkeypatch):
    """``_read_page`` поддерживает /tree/<branch>/<subpath> и /blob/<branch>/<file>."""
    import httpx

    def fake_get(url, headers=None, params=None, timeout=15, follow_redirects=False):
        if "/contents/config.py" in url:
            return _FakeHttpxResp(200, text="PORT = 5000\n")
        if url.endswith("/repos/owner/demo"):
            return _FakeHttpxResp(200, {"default_branch": "main", "language": "Python"})
        if "/git/trees/dev" in url:
            return _FakeHttpxResp(200, {
                "tree": [
                    {"path": "src/core.py", "type": "blob"},
                    {"path": "src/utils/io.py", "type": "blob"},
                    {"path": "README.md", "type": "blob"},
                ]
            })
        return _FakeHttpxResp(404)

    monkeypatch.setattr(httpx, "get", fake_get)

    sub_text, sub_err = agent_routes._read_page("https://github.com/owner/demo/tree/dev/src")
    assert sub_err == ""
    assert "demo/src" in sub_text
    assert "utils/" in sub_text and "core.py" in sub_text
    assert "README.md" not in sub_text

    blob_text, blob_err = agent_routes._read_page("https://github.com/owner/demo/blob/main/config.py")
    assert blob_err == ""
    assert "PORT = 5000" in blob_text


def test_autonomous_mcp_selection_without_mentioning_mcp_and_with_random_server_id(
    admin_client, sandbox, monkeypatch
):
    """Пользователь НЕ пишет слово «mcp», а у сервера случайный ID из Настроек:
    агент сам видит сервер по имени/описанию, вызывает инструмент, а серверный ID
    автоматически сопоставляется даже если модель написала имя сервиса."""
    monkeypatch.setattr(mcp_registry, "connected_tools", lambda: [
        {
            "server_id": "xK9_mP2q",
            "server_name": "Todoist Tasks",
            "name": "list_active_tasks",
            "description": "Получить активные задачи пользователя из Todoist",
            "inputSchema": {"type": "object", "properties": {"project": {"type": "string"}}},
        }
    ])
    called = []

    def fake_call_tool(server_id, tool_name, arguments=None):
        called.append((server_id, tool_name, dict(arguments or {})))
        return {"content": [{"type": "text", "text": "1. Купить молоко\n2. Проверить PR"}]}

    monkeypatch.setattr(mcp_registry, "call_tool", fake_call_tool)

    seen = []
    # Модель передаёт человекочитаемое имя "todoist" вместо случайного "xK9_mP2q"
    script(
        monkeypatch,
        [
            '```mcp_call\n{"server_id": "todoist", "tool": "list_active_tasks", "arguments": {}}\n```',
            "Ваши активные задачи в Todoist: 1) Купить молоко, 2) Проверить PR.",
        ],
        seen,
    )
    events = ndjson(admin_client.post(
        "/api/agent/stream",
        json={"message": "покажи что у меня сейчас в todoist"},
    ))

    # В системном промпте есть и название сервера, и описание инструмента
    prompt_seen = model_text(seen)
    assert "Todoist Tasks" in prompt_seen
    assert "Получить активные задачи" in prompt_seen
    # Вызов автоматически разрешился в реальный server_id = "xK9_mP2q"
    assert called == [("xK9_mP2q", "list_active_tasks", {})]
    assert "Купить молоко" in answer_of(events)


def test_freeform_request_without_keywords_still_autonomously_uses_tools(
    admin_client, sandbox, monkeypatch
):
    """Даже если фраза разговорная и не содержит ключевых слов роутера («запомни», «mcp»),
    собеседник (CasualAgent) видит безопасные инструменты и сам вызывает нужный."""
    monkeypatch.setattr(mcp_registry, "connected_tools", lambda: [
        {
            "server_id": "gh_rnd12",
            "server_name": "GitHub",
            "name": "get_me",
            "description": "Профиль текущего авторизованного аккаунта",
            "inputSchema": {"type": "object", "properties": {}},
        }
    ])
    monkeypatch.setattr(
        mcp_registry,
        "call_tool",
        lambda sid, tool, args=None: {"login": "podshoevbunyod16-sketch", "id": 101},
    )

    seen = []
    # Фраза без слов «mcp», «github», «файл», «найди» — попадает в talk, но имеет mcp_call
    script(
        monkeypatch,
        [
            '```mcp_call\n{"server_id": "github", "tool": "get_me", "arguments": {}}\n```',
            "Вы авторизованы как podshoevbunyod16-sketch.",
        ],
        seen,
    )
    events = ndjson(admin_client.post(
        "/api/agent/stream",
        json={"message": "кто я сейчас в подключённом аккаунте разработчика?"},
    ))
    tools = events_of(events, "tool")
    assert len(tools) == 1 and tools[0]["name"] == "mcp_call"
    assert "podshoevbunyod16-sketch" in answer_of(events)
