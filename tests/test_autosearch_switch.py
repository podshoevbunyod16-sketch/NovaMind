"""Кнопка «Автопоиск» — главный выключатель инструментов агента.

Выключена: роутер отправляет запрос прямиком в ИИ-модель, не создавая ни
одного инструмента. Включена: агент выполняет шаги в терминале песочницы
обычными Linux-командами.
"""
import json

import pytest

import routes.agent as agent_routes
from agenticseek.engine import AgentEngine, EngineContext
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

def test_engine_direct_mode_registers_no_tools(monkeypatch):
    calls = []

    def complete(messages, **kwargs):
        calls.append(messages)
        return "Ответ модели", {}

    ctx = EngineContext(goal="выполни pwd", complete=complete, emit=lambda e: None,
                        workspace=True, search=lambda *a, **k: ([], {}),
                        read_page=lambda *a, **k: ("", None),
                        tools_allowed=False)
    engine = AgentEngine(ctx)
    engine._build_agents()

    assert engine._build_tools() == {}
    assert len(engine.agents) == 1
    agent = engine.router.select_agent("выполни pwd через терминал")
    assert agent.all_tools() == {}
    assert not any(name in agent.system_prompt() for name in ("```terminal```", "```web_search```"))


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
    assert result["steps"] == 0 and result["tools"] == []
    assert result["searched"] is False
    assert any("Автопоиск выключен" in event.get("text", "")
               for event in events_of(events, "step"))
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

    assert not events_of(events, "tool")
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
    assert tools[0]["name"] == "web_search" and tools[0]["status"] == "error"
    assert "python3 -m novamind_cli call web_search" in tools[0]["command"]
    terminal_tools = [tool for tool in tools if tool["name"] == "terminal"]
    assert len(terminal_tools) == 2, tools
    assert (sandbox / "from-agent.txt").read_text(encoding="utf-8").strip() == "linux"
    result = next(event for event in events if event["type"] == "result")
    assert result["tools"] and "terminal" in result["tools"]
    assert result["steps"] >= 2
    assert "Команды выполнены" in answer_of(events)


def test_agent_terminal_nova_commands_are_real_steps(admin_client, sandbox, monkeypatch):
    """Шаги агента идут через терминал: nova tools / nova web search / Linux."""
    monkeypatch.setattr(agent_routes, "nova_search", lambda *a, **k: ([], {}))
    script(monkeypatch, [
        "```terminal\nnova tools\n```",
        "```terminal\nnova web search 'flask docs'\n```",
        "```terminal\nuname -s\n```",
        "Все шаги сделаны через терминал.",
    ])
    events = ndjson(admin_client.post("/api/agent/stream", json={
        "message": "посмотри инструменты и запусти команду", "tools": True,
    }))
    tools = events_of(events, "tool")
    assert [tool["name"] for tool in tools] == ["terminal", "web_search", "terminal"], tools
    assert "web_search" in tools[0]["output"]          # каталог инструментов
    assert "Linux" in tools[-1]["output"]              # настоящая команда
    assert "Все шаги сделаны" in answer_of(events)


def test_offline_demo_still_executes_terminal_steps(admin_client, sandbox, monkeypatch):
    """Без ключа провайдера шаги терминала тоже реально выполняются."""
    monkeypatch.setattr(agent_routes, "resolve_target",
                        lambda *a, **k: ("local_demo", "nova-local-1", {}))
    events = ndjson(admin_client.post("/api/agent/stream", json={
        "message": "выполни pwd и uname в терминале", "tools": True, "search": False,
    }))
    tools = events_of(events, "tool")
    assert [tool["command"] for tool in tools] == ["pwd", "uname -s"], tools
    assert all(tool["name"] == "terminal" and tool["code"] == 0 for tool in tools)
    assert "Linux" in answer_of(events)
    result = next(event for event in events if event["type"] == "result")
    assert result["steps"] == 2 and result["linux"] is True


def test_offline_demo_ignores_terminal_when_autosearch_off(admin_client, sandbox, monkeypatch):
    monkeypatch.setattr(agent_routes, "resolve_target",
                        lambda *a, **k: ("local_demo", "nova-local-1", {}))
    events = ndjson(admin_client.post("/api/agent/stream", json={
        "message": "выполни pwd в терминале", "tools": False, "search": False,
    }))
    assert not events_of(events, "tool")
    assert next(e for e in events if e["type"] == "result")["steps"] == 0


def test_autosearch_on_registers_terminal_tools(monkeypatch):
    ctx = EngineContext(goal="создай проект", complete=lambda *a, **k: ("Готово", {}),
                        emit=lambda e: None, workspace=True,
                        search=lambda *a, **k: ([], {}),
                        read_page=lambda *a, **k: ("", None),
                        tools_allowed=True)
    engine = AgentEngine(ctx)
    engine._build_agents()
    names = set(engine._build_tools())
    assert {"terminal", "python", "file_read", "file_write", "file_edit",
            "web_search", "web_open"} <= names
