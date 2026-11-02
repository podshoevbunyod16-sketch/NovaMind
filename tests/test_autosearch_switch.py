"""Кнопка «Автопоиск» — главный выключатель инструментов агента.

Выключена: роутер отправляет запрос прямиком в ИИ-модель, не создавая ни
одного инструмента. Включена: агент выполняет шаги в терминале песочницы
обычными Linux-командами.
"""
import json

import pytest

import routes.agent as agent_routes
from agent.loop import AgentContext, run_react
from tests.agent_helpers import answer_of, events_of, ndjson, script


def _ndjson(response):
    return [json.loads(line) for line in response.get_data(as_text=True).splitlines()
            if line.strip()]


# ══════════════ разбор флага ══════════════

@pytest.mark.parametrize("value,expected", [
    (False, False), ("false", False), (0, False), ("off", False),
    (True, True), ("true", True), (1, True), ("on", True),
])
def test_request_tools_allowed_parses_flag(value, expected):
    assert agent_routes.request_tools_allowed({"tools": value}) is expected


def test_request_tools_allowed_defaults_to_tools_for_old_clients():
    assert agent_routes.request_tools_allowed({}) is True
    assert agent_routes.request_tools_allowed({"message": "привет"}) is True
    # Telegram и планировщик не шлют флаг — инструменты у них работают.
    assert agent_routes.request_tools_allowed({"search": True}) is True
    # Старый фронтенд слал только search: выключенный автопоиск = без инструментов.
    assert agent_routes.request_tools_allowed({"search": False}) is False


# ══════════════ движок: режим без инструментов ══════════════

def test_direct_mode_uses_no_first_party_tools():
    calls = []

    def complete(messages, **kwargs):
        calls.append(kwargs)
        return "Ответ модели", {}

    result = run_react(AgentContext("выполни pwd", complete=complete, tools=[]))
    assert result.answer == "Ответ модели"
    assert len(calls) == 1
    assert calls[0]["tools"] is None


# ══════════════ HTTP: автопоиск выключен ══════════════

def test_autosearch_off_sends_request_straight_to_model(admin_client, sandbox, monkeypatch):
    """Один вызов модели, ноль инструментов, ноль шагов, ответ без блоков."""
    seen = []
    script(monkeypatch, ["Ответ без инструментов."], seen)
    events = ndjson(admin_client.post("/api/agent/stream", json={
        "message": "выполни pwd и найди погоду",
        "tools": False,
        "search": False,
    }))

    assert not events_of(events, "tool"), events
    assert not events_of(events, "sources")
    assert len(seen) == 1, "прямой режим = ровно один запрос к модели"
    assert answer_of(events) == "Ответ без инструментов."
    result = next(event for event in events if event["type"] == "result")
    assert result["steps"] == 0
    assert result["searched"] is False
    assert seen[0]["tools"] is None
    assert list(sandbox.iterdir()) == [], "диск не тронут"


def test_autosearch_off_does_not_execute_model_tool_block(admin_client, sandbox, monkeypatch):
    """Даже если модель написала блок команды, он не выполняется и не показывается."""
    seen = []
    script(monkeypatch, [
        "```terminal\necho взлом > marker.txt\n```",
        "Инструменты отключены, отвечаю текстом.",
    ], seen)
    events = ndjson(admin_client.post("/api/agent/stream", json={
        "message": "запусти команду", "search": False,  # старый фронтенд
    }))

    tool_events = events_of(events, "tool")
    assert all(event.get("status") == "error" for event in tool_events), tool_events
    assert not (sandbox / "marker.txt").exists()
    assert "```terminal" not in answer_of(events)
    assert "Инструменты отключены" in answer_of(events)


def test_autosearch_off_keeps_ordinary_code_examples(admin_client, sandbox, monkeypatch):
    """Пример кода в ответе — просто текст: он показывается, но не исполняется."""
    script(monkeypatch, ["```python\nprint('hello')\n```"])
    events = ndjson(admin_client.post("/api/agent/stream", json={
        "message": "покажи пример кода", "tools": False,
    }))
    assert not events_of(events, "tool")
    assert "print('hello')" in answer_of(events)
    assert not (sandbox / "hello.py").exists()
    assert not any("web_search" in str(event) for event in events_of(events, "sources"))


def test_autosearch_off_skips_search_and_media(admin_client, sandbox, monkeypatch):
    search_calls = []

    def fake_search(query, limit=8, **kwargs):
        search_calls.append(query)
        return [{"title": "источник", "url": "https://example.com", "snippet": "данные"}], {}

    monkeypatch.setattr(agent_routes, "nova_search", fake_search)
    seen = []
    script(monkeypatch, ["Нарисую словами: вот кот."], seen)
    events = ndjson(admin_client.post("/api/agent/stream", json={
        "message": "нарисуй кота и найди погоду", "tools": False,
    }))

    assert search_calls == []
    assert not events_of(events, "tool")
    assert not events_of(events, "media")
    assert not events_of(events, "sources")
    assert answer_of(events) == "Нарисую словами: вот кот."


def test_autosearch_off_keeps_chat_history(admin_client, sandbox, monkeypatch):
    """История доходит до модели настоящими сообщениями."""
    seen = []
    script(monkeypatch, ["Учёл контекст."], seen)
    with admin_client.session_transaction() as sess:
        sess["agent_memory_key"] = "direct-history"
    # первый ход кладём в историю, второй — проверяем
    ndjson(admin_client.post("/api/agent/stream",
                             json={"message": "запомни число 42", "tools": False}))
    ndjson(admin_client.post("/api/agent/stream",
                             json={"message": "какое число?", "tools": False}))
    text = " ".join(str(item) for call in seen for item in call["messages"])
    assert "42" in text or "запомни число" in text


# ══════════════ запасной обычный чат без агента ══════════════

def test_plain_chat_also_has_no_tools_when_autosearch_off(client, monkeypatch):
    """/send и /send_stream не должны дёргать MCP или медиа при tools:false."""
    import routes.chat as chat_routes

    calls = []

    def fake_mcp(*args, **kwargs):
        calls.append("mcp")
        return "ответ через MCP", {}, []

    def fake_media(data):
        calls.append("media")
        return None, None, None

    monkeypatch.setattr(chat_routes, "complete_with_mcp", fake_mcp)
    monkeypatch.setattr(chat_routes, "media_generation_context", fake_media)

    response = client.post("/send", json={"message": "привет", "tools": False})
    assert response.status_code == 200
    assert calls == []

    for flag, expected in ((False, []), (True, ["media", "mcp"])):
        calls.clear()
        stream = client.post("/send_stream", json={"message": "привет", "tools": flag})
        assert stream.status_code == 200
        stream.get_data()          # генератор выполняется при чтении ответа
        assert calls == expected, (flag, calls)


# ══════════════ HTTP: автопоиск включён, шаги через терминал ══════════════

def test_autosearch_on_runs_linux_command_steps(admin_client, sandbox, monkeypatch):
    """Включённый автопоиск: агент шагает через terminal и реально исполняет Linux."""
    monkeypatch.setattr(agent_routes, "nova_search", lambda *a, **k: ([], {}))
    seen = []
    script(monkeypatch, [
        "```terminal\nuname -s\n```",
        "```bash\necho linux > from-agent.txt\n```",
        "Команды выполнены в Linux.",
    ], seen)
    events = ndjson(admin_client.post("/api/agent/stream", json={
        "message": "выполни команды в терминале",
        "tools": True,
        "search": True,
    }))

    tools = events_of(events, "tool")
    assert [tool["name"] for tool in tools] == ["shell", "shell"], tools
    assert (sandbox / "from-agent.txt").read_text(encoding="utf-8").strip() == "linux"
    result = next(event for event in events if event["type"] == "result")
    assert result["steps"] == 2 and result["linux"] is True
    assert "Команды выполнены" in answer_of(events)


def test_agent_terminal_nova_commands_are_real_steps(admin_client, sandbox, monkeypatch):
    """Шаги агента идут через терминал: nova tools / nova web search / Linux."""
    monkeypatch.setattr(agent_routes, "nova_search", lambda *a, **k: ([], {}))
    script(monkeypatch, [
        '```json\n{"tool":"file_tree","arguments":{"path":"."}}\n```',
        '```json\n{"tool":"web_search","arguments":{"query":"flask docs"}}\n```',
        "```terminal\nuname -s\n```",
        "Все шаги сделаны через инструменты.",
    ])
    events = ndjson(admin_client.post("/api/agent/stream", json={
        "message": "посмотри инструменты и запусти команду", "tools": True,
    }))
    tools = events_of(events, "tool")
    assert [tool["name"] for tool in tools] == ["file_tree", "web_search", "shell"], tools
    assert tools[0]["status"] == "ok"
    assert "Linux" in tools[-1]["output"]
    assert "Все шаги сделаны" in answer_of(events)


def test_offline_demo_is_available_without_external_model(admin_client, sandbox, monkeypatch):
    """Офлайн-провайдер отвечает сам и не изображает выполнение неподдерживаемых действий."""
    import ai_providers
    monkeypatch.setattr(ai_providers, "resolve_target",
                        lambda *a, **k: ("local_demo", "nova-local-1", {}))
    events = ndjson(admin_client.post("/api/agent/stream", json={
        "message": "привет", "tools": True, "search": False,
    }))
    result = next(event for event in events if event["type"] == "result")
    assert result["provider"] == "local_demo"
    assert answer_of(events)
    assert not events_of(events, "tool")


def test_offline_demo_ignores_terminal_when_autosearch_off(admin_client, sandbox, monkeypatch):
    import ai_providers
    monkeypatch.setattr(ai_providers, "resolve_target",
                        lambda *a, **k: ("local_demo", "nova-local-1", {}))
    events = ndjson(admin_client.post("/api/agent/stream", json={
        "message": "выполни pwd в терминале", "tools": False, "search": False,
    }))
    assert not events_of(events, "tool")
    assert next(e for e in events if e["type"] == "result")["steps"] == 0


def test_autosearch_on_registers_first_party_tools():
    import tool_registry

    tool_registry.bootstrap_default_tools()
    names = {spec.name for spec in tool_registry.list_tools()}
    assert {"shell", "file_read", "file_write", "file_edit", "file_search",
            "file_tree", "web_search", "web_open"} <= names
