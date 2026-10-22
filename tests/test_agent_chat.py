"""Агент в главном чате: доступ, история, инструменты, медиа и офлайн-режим.

Это регрессионные тесты на то, что раньше было сломано:

  * агент был выключен по умолчанию (AGENT_ENABLED=0) и требовал входа;
  * «быстрый ответ» проглатывал JSON-действие — инструмент не выполнялся,
    а в чат попадал сырой JSON;
  * без TERMINAL_ENABLED=1 отключался и поиск в интернете;
  * ходы агента не попадали в историю чата;
  * статусы генерации медиа не доходили до интерфейса;
  * офлайн-«модель» отвечала заглушкой «вопрос принят и разобран».
"""
import json

import pytest

import local_llm
import media_agent
import routes.agent as agent_routes
import routes.terminal as term_routes
import tool_registry


def _ndjson(response):
    return [json.loads(line) for line in response.get_data(as_text=True).splitlines() if line.strip()]


def _answer(events):
    return "".join(event["token"] for event in events if event["type"] == "token")


def _stub_stream(monkeypatch, answers, seen=None):
    queue = list(answers)

    def fake_stream(messages, **kwargs):
        if seen is not None:
            seen.append({"messages": list(messages), "system": kwargs.get("system", "")})
        text = queue.pop(0) if queue else "готово"
        yield "token", text
        yield "done", ""

    monkeypatch.setattr(agent_routes, "chat_stream", fake_stream)
    monkeypatch.setattr(agent_routes, "chat_completion",
                        lambda messages, **kw: ((queue.pop(0) if queue else ""), {}))
    return queue


@pytest.fixture
def fake_search(monkeypatch):
    results = [{"title": "Документация asyncio", "url": "https://docs.python.org/asyncio",
                "snippet": "asyncio — библиотека", "host": "docs.python.org"}]
    calls = []

    def search(query, limit=8):
        calls.append({"query": query, "limit": limit})
        return list(results), {"backend": "test", "elapsed_ms": 3, "query": query,
                               "results": results, "count": len(results),
                               "trace": [{"backend": "test", "ok": True, "ms": 3}]}

    monkeypatch.setattr(agent_routes, "nova_search", search)
    monkeypatch.setattr(agent_routes, "nova_read", lambda url, max_chars=4000: ("Текст страницы " + url, 2))
    return calls


# ══════════════ доступ без входа ══════════════

def test_guest_agent_can_search_without_login(client, sandbox, monkeypatch, fake_search):
    """Гость получает агента: поиск работает, рабочая папка — нет."""
    _stub_stream(monkeypatch, [
        '{"action":"search","query":"asyncio"}',
        "asyncio — это библиотека [1]",
    ])
    events = _ndjson(client.post("/api/agent/stream", json={"message": "найди asyncio"}))
    assert fake_search and fake_search[0]["query"] == "asyncio"
    tools = [event for event in events if event["type"] == "tool"]
    assert tools and tools[0]["name"] == "search"
    result = next(event for event in events if event["type"] == "result")
    assert result["linux"] is False
    assert "asyncio" in _answer(events)


def test_guest_agent_refuses_workspace_but_explains(client, sandbox, monkeypatch):
    """Команда в песочнице гостю недоступна, но ответ всё равно приходит."""
    _stub_stream(monkeypatch, [
        '{"action":"run","command":"ls"}',
        "Файлы доступны только после входа.",
    ])
    events = _ndjson(client.post("/api/agent/stream", json={"message": "покажи файлы"}))
    assert not [event for event in events if event["type"] == "tool"]
    assert "войдите" in _answer(events).lower() or "доступны только" in _answer(events).lower()


def test_status_reports_capabilities_without_login(client):
    status = client.get("/api/agent/status").get_json()
    assert status["enabled"] is True and status["available"] is True
    assert status["tools"] is False and status["guest"] is True
    assert status["search"] is True
    assert "TERMINAL_ENABLED" in status["workspace_hint"]


# ══════════════ быстрый путь не теряет действия ══════════════

def test_fast_answer_never_returns_raw_json(google_client, monkeypatch, fake_search):
    """Короткий вопрос + ответ-действие: действие выполняется, JSON не утекает."""
    _stub_stream(monkeypatch, [
        '{"action":"search","query":"погода"}',
        "Сегодня тепло.",
    ])
    events = _ndjson(google_client.post("/api/agent/stream", json={"message": "что нового"}))
    assert any(event["type"] == "tool" for event in events)
    answer = _answer(events)
    assert "{" not in answer and "Сегодня тепло" in answer


def test_fast_path_still_answers_simple_question_in_one_call(google_client, monkeypatch):
    queue = _stub_stream(monkeypatch, ["Привет! Чем помочь?", "лишний"])
    events = _ndjson(google_client.post("/api/agent/stream", json={"message": "привет"}))
    assert queue == ["лишний"]
    assert _answer(events) == "Привет! Чем помочь?"
    assert not any(event["type"] in ("tool", "plan") for event in events)


# ══════════════ история ══════════════

def test_agent_turn_is_saved_to_chat_history(google_client, monkeypatch):
    _stub_stream(monkeypatch, ["Запомнил: меня зовут Бунёд."])
    _ndjson(google_client.post("/api/agent/stream", json={"message": "меня зовут Бунёд"}))
    history = google_client.get("/api/history").get_json()["history"]
    assert any(m["role"] == "user" and "Бунёд" in m["content"] for m in history)
    assert any(m["role"] == "assistant" and "Запомнил" in m["content"] for m in history)


def test_agent_remembers_previous_turns_through_database(google_client, monkeypatch):
    seen = []
    _stub_stream(monkeypatch, ["Запомнил."], seen)
    _ndjson(google_client.post("/api/agent/stream", json={"message": "меня зовут Бунёд"}))
    _stub_stream(monkeypatch, ["Тебя зовут Бунёд."], seen)
    _ndjson(google_client.post("/api/agent/stream", json={"message": "как меня зовут?"}))
    previous = " ".join(message["content"] for message in seen[-1]["messages"])
    assert "Бунёд" in previous


# ══════════════ инструменты и флаги результата ══════════════

def test_linux_flag_is_true_only_after_workspace_action(admin_client, monkeypatch, sandbox):
    _stub_stream(monkeypatch, ['{"action":"run","command":"pwd"}', "Готово."])
    events = _ndjson(admin_client.post("/api/agent/stream", json={"message": "где я?"}))
    result = next(event for event in events if event["type"] == "result")
    assert result["linux"] is True and "run" in result["tools"]


def test_git_action_runs_through_registry(admin_client, monkeypatch, sandbox):
    _stub_stream(monkeypatch, ['{"action":"git","command":"status --short"}', "Проверил git."])
    events = _ndjson(admin_client.post("/api/agent/stream", json={"message": "проверь git"}))
    tool = next(event for event in events if event["type"] == "tool")
    assert tool["command"].startswith("git status")
    result = next(event for event in events if event["type"] == "result")
    assert "git" in result["tools"]


def test_workspace_tools_are_hidden_from_guest_prompt(client, sandbox, monkeypatch):
    seen = []
    _stub_stream(monkeypatch, ["Ответ без файлов"], seen)
    _ndjson(client.post("/api/agent/stream", json={"message": "что в папке?"}))
    system = seen[0]["system"]
    assert '"action":"run"' not in system and '"action":"write"' not in system
    assert "TERMINAL_ENABLED=0" in system


def test_registry_blocks_workspace_tools_without_login(client, sandbox):
    tool_registry.bootstrap_default_tools()
    with client.application.test_request_context("/api/agent/stream"):
        result = tool_registry.call_tool("file_write", path="hack.txt", content="x")
    assert result["ok"] is False
    assert result["error"]["type"] == "unauthorized"


# ══════════════ медиа ══════════════

def test_media_generation_reports_real_statuses(google_client, monkeypatch):
    def fake_generate(kind, prompt, options=None, wanted_model="", on_status=None):
        if on_status:
            on_status("Отправка запроса к Pollinations (flux)")
            on_status("Провайдер вернул ответ, сохраняю файл")
        return ({"kind": kind, "url": "/media/x.png", "filename": "x.png",
                 "model_name": "Pollinations Flux"}, 
                {"id": "flux", "name": "Pollinations Flux", "provider_name": "Pollinations"}, [])

    monkeypatch.setattr(media_agent, "generate", fake_generate)
    _stub_stream(monkeypatch, ['{"action":"image","prompt":"закат"}', "Готово."])
    events = _ndjson(google_client.post("/api/agent/stream", json={"message": "нарисуй закат"}))
    steps = [event["text"] for event in events if event["type"] == "step"]
    assert any("Отправка запроса" in text for text in steps)
    assert any("сохраняю файл" in text for text in steps)
    media = next(event for event in events if event["type"] == "media")
    assert media["media"]["url"] == "/media/x.png"


def test_media_is_marked_in_history_for_reload(google_client, monkeypatch):
    def fake_generate(kind, prompt, options=None, wanted_model="", on_status=None):
        return ({"kind": kind, "url": "/media/x.png", "filename": "x.png",
                 "model_name": "Pollinations Flux"},
                {"id": "flux", "name": "Pollinations Flux", "provider_name": "Pollinations"}, [])

    monkeypatch.setattr(media_agent, "generate", fake_generate)
    _stub_stream(monkeypatch, ['{"action":"image","prompt":"маяк"}', "Готово."])
    _ndjson(google_client.post("/api/agent/stream", json={"message": "нарисуй маяк"}))
    history = google_client.get("/api/history").get_json()["history"]
    markers = [m for m in history if m["content"].startswith("MEDIA_RESULT:")]
    assert markers and "/media/x.png" in markers[0]["content"]


# ══════════════ совместимость: /send_stream с agent=true ══════════════

def test_send_stream_can_delegate_to_agent(client, sandbox, monkeypatch, fake_search):
    _stub_stream(monkeypatch, ['{"action":"search","query":"курс"}', "Курс найден."])
    events = _ndjson(client.post("/send_stream", json={"message": "какой курс", "agent": True}))
    assert any(event["type"] == "tool" for event in events)
    assert _answer(events) == "Курс найден."


# ══════════════ офлайн-режим: реальные операции, а не заглушка ══════════════

def _offline(text):
    return local_llm.complete([{"role": "user", "content": text}])


def test_offline_arithmetic_is_real():
    assert "**4**" in _offline("2+2")
    assert "**30**" in _offline("15% от 200")


def test_offline_units_are_real():
    assert "°F" in _offline("20 C в F")
    assert "фунт" in _offline("5 кг в фунты")
    assert "6.2137" in _offline("10 км в мили")


def test_offline_text_tools_are_real():
    assert "0L/RgNC40LLQtdGC" in _offline("base64: привет")
    assert "e58f1e8c55fa105bdd3f40e5037eb0b039b5998d52c05e6cd98878dd2da5cab2" in _offline("sha256: привет")
    assert '"a": 1' in _offline('json: {"a":1}')


def test_offline_is_honest_about_not_being_a_model():
    reply = _offline("как приготовить плов")
    assert "языковой модели нет" in reply.lower()
    assert "токенов" not in reply.lower()          # старой заглушки больше нет
    assert "GROQ_API_KEY" in reply                 # и есть инструкция, что подключить


def test_offline_datetime_is_real():
    import datetime
    reply = _offline("сколько времени")
    assert datetime.datetime.now().strftime("%H:%M") in reply


# ══════════════ офлайн-агент: поиск настоящим инструментом ══════════════

def test_offline_agent_searches_when_model_is_absent(client, sandbox, fake_search):
    """Без языковой модели «найди …» всё равно идёт в настоящий поиск."""
    events = _ndjson(client.post("/api/agent/stream", json={"message": "найди asyncio"}))
    assert fake_search, "поиск не вызывался"
    assert fake_search[0]["query"] == "asyncio", fake_search
    assert any(event["type"] == "tool" and event.get("name") == "search" for event in events)
    result = next(event for event in events if event["type"] == "result")
    assert result["offline"] is True and result["searched"] is True
    assert "docs.python.org/asyncio" in _answer(events)


def test_offline_agent_keeps_honest_answer_when_search_is_empty(client, sandbox, monkeypatch):
    monkeypatch.setattr(agent_routes, "nova_search", lambda query, limit=8: ([], {"backend": "none"}))
    events = _ndjson(client.post("/api/agent/stream", json={"message": "найди неизвестно-что"}))
    assert any(event["type"] == "step" and "ничего не дал" in event["text"] for event in events)
    assert "языковой модели нет" in _answer(events).lower()


def test_offline_intent_never_hijacks_a_working_model(client, sandbox, fake_search, monkeypatch):
    """Если модель ответила по-настоящему, офлайн-ветка в дело не лезет."""
    _stub_stream(monkeypatch, ["asyncio — это библиотека для асинхронности."])
    events = _ndjson(client.post("/api/agent/stream", json={"message": "найди asyncio"}))
    assert not fake_search, "поиск запустился, хотя модель ответила сама"
    assert "библиотека для асинхронности" in _answer(events)


# ══════════════ офлайн-медиа и служебные сообщения ══════════════

def test_offline_media_request_says_why_and_tries_once(client, sandbox, monkeypatch):
    attempts = []

    def generate(kind, prompt, options=None, wanted_model="", on_status=None):
        attempts.append(prompt)
        return None, None, ["Нет доступных моделей (image). Подключить: POLLINATIONS_API_KEY"]

    monkeypatch.setattr(media_agent, "generate", generate)
    events = _ndjson(client.post("/api/agent/stream", json={"message": "нарисуй кота"}))
    reply = _answer(events)
    assert "Не получилось создать" in reply and "POLLINATIONS_API_KEY" in reply
    assert "языковой модели нет" not in reply.lower()
    assert attempts == ["кота"], attempts            # промпт без «нарисуй», попытка одна
    failures = [event for event in events
                if event["type"] == "step" and "Не получилось создать" in event["text"]]
    assert len(failures) == 1 and "Нет доступных моделей" in failures[0]["text"]


# ══════════════ ленивые инструменты и счётчик токенов ══════════════

def test_hello_is_one_small_request_without_registries(client, monkeypatch):
    """«Привет» — один запрос, без реестра инструментов и без опроса MCP."""
    import mcp_registry
    import skills_registry

    seen = []
    calls = {"connected": 0, "schemas": 0, "skills": 0, "workspace": 0}

    def connected(*args, **kwargs):
        calls["connected"] += 1
        return []

    def schemas(*args, **kwargs):
        calls["schemas"] += 1
        return [{"name": "mcp_call", "inputSchema": {"type": "object"}}]

    def skills(*args, **kwargs):
        calls["skills"] += 1
        return "SKILL CONTEXT\n" + ("большой навык " * 500)

    monkeypatch.setattr(mcp_registry, "connected_tools", connected)
    monkeypatch.setattr(agent_routes, "tool_schemas", schemas)
    monkeypatch.setattr(skills_registry, "prompt_context", skills)
    monkeypatch.setattr(agent_routes, "ensure_workspace", lambda: calls.__setitem__("workspace", calls["workspace"] + 1))

    def fake_stream(messages, **kwargs):
        seen.append({"messages": list(messages), "system": kwargs.get("system", "")})
        yield "token", "Привет! Чем помочь?"
        yield "usage", {"prompt": 42, "completion": 8, "total": 50}
        yield "done", ""

    monkeypatch.setattr(agent_routes, "chat_stream", fake_stream)
    monkeypatch.setattr(agent_routes, "chat_completion",
                        lambda *args, **kwargs: ("не должен вызываться", {"usage": {"prompt": 9000, "completion": 9000, "total": 18000}}))

    events = _ndjson(client.post("/api/agent/stream", json={"message": "Привет"}))
    assert len(seen) == 1
    blob = json.dumps(seen[0], ensure_ascii=False)
    assert "AVAILABLE TOOL REGISTRY" not in blob
    assert "SKILL CONTEXT" not in blob
    assert "inputSchema" not in blob
    assert len(blob) // 3 < 1500
    assert calls == {"connected": 0, "schemas": 0, "skills": 0, "workspace": 0}
    result = next(event for event in events if event["type"] == "result")
    assert result["fast"] is True
    assert result["tokens"] == {"prompt": 42, "completion": 8, "total": 50}
    assert _answer(events) == "Привет! Чем помочь?"


def test_hello_with_search_button_does_not_search(client, monkeypatch, fake_search):
    _stub_stream(monkeypatch, ["Привет!"])
    events = _ndjson(client.post("/api/agent/stream", json={"message": "Привет", "search": True}))
    assert not fake_search
    assert not any(event["type"] == "tool" for event in events)
    result = next(event for event in events if event["type"] == "result")
    assert result["searched"] is False and result["fast"] is True
    assert "Привет" in _answer(events)


def test_news_query_searches_tools_before_calling_them(client, monkeypatch):
    import mcp_registry

    tools = [{
        "server_id": "exa",
        "name": "web_search_exa",
        "description": "Search the web for news and pages",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "длинное описание " * 40},
                "numResults": {"type": "integer"},
            },
            "required": ["query"],
        },
    }]
    monkeypatch.setattr(mcp_registry, "connected_tools", lambda force=False: tools)
    called = []
    monkeypatch.setattr(
        mcp_registry, "call_tool",
        lambda server_id, tool, arguments: called.append((server_id, tool, arguments)) or {
            "results": [{"title": "Python 3.13", "url": "https://docs.python.org/3.13/"}],
        },
    )
    seen = []
    answers = [
        '{"action":"tool_search","query":"search news"}',
        '{"action":"mcp_call","server_id":"exa","tool":"web_search_exa","arguments":{"query":"Python 3.13"}}',
        "Python 3.13 уже вышел.",
    ]

    def fake_stream(messages, **kwargs):
        seen.append({"messages": list(messages), "system": kwargs.get("system", "")})
        yield "token", answers.pop(0)
        yield "done", ""

    monkeypatch.setattr(agent_routes, "chat_stream", fake_stream)
    monkeypatch.setattr(agent_routes, "chat_completion", lambda *args, **kwargs: ("", {}))
    events = _ndjson(client.post("/api/agent/stream", json={"message": "Найди новости про Python 3.13"}))
    system = seen[0]["system"]
    assert "inputSchema" not in system
    assert "AVAILABLE TOOL REGISTRY" not in system
    assert "SKILL CONTEXT" not in system
    assert "tool_search" in system and "exa" in system
    assert any(event["type"] == "tool" and event["name"] == "tool_search" for event in events)
    assert called and called[0][0] == "exa" and called[0][1] == "web_search_exa"
    assert "Python 3.13" in _answer(events)
    assert any(event["type"] == "result" for event in events)


def test_tool_search_returns_at_most_five_without_schema(monkeypatch):
    import mcp_registry

    tools = []
    for index in range(8):
        tools.append({
            "server_id": "composio",
            "name": f"search_tool_{index}",
            "description": "search news " + ("extra schema text " * 30),
            "inputSchema": {
                "type": "object",
                "properties": {"q": {"type": "string", "description": "huge"}},
                "required": ["q"],
            },
        })
    monkeypatch.setattr(mcp_registry, "connected_tools", lambda force=False: tools)
    found = agent_routes.search_mcp_tools("search news", limit=5)
    assert 1 <= len(found) <= 5
    blob = json.dumps(found, ensure_ascii=False)
    assert "inputSchema" not in blob
    assert found[0]["parameters"] == [{"name": "q", "type": "string", "required": True}]
    assert len(found[0]["description"]) <= 150
    assert found[0]["server_id"] == "composio"


def test_connected_tools_cache_skips_network_until_save(tmp_path, monkeypatch):
    import mcp_registry

    monkeypatch.setattr(mcp_registry, "STORE", tmp_path / "mcp.json")
    mcp_registry.invalidate_tools_cache()
    mcp_registry._write({
        "exa": {"name": "Exa", "url": "https://example.test/mcp", "auth": "none", "connected": True},
    })
    calls = {"n": 0}

    def fake_list(server_id):
        calls["n"] += 1
        return [{"name": "search", "description": "find pages", "inputSchema": {"type": "object"}}]

    monkeypatch.setattr(mcp_registry, "list_tools", fake_list)
    clock = {"t": 1_000.0}
    monkeypatch.setattr(mcp_registry.time, "time", lambda: clock["t"])
    try:
        first = mcp_registry.connected_tools()
        second = mcp_registry.connected_tools()
        assert calls["n"] == 1
        assert first[0]["name"] == "search" and second[0]["name"] == "search"
        clock["t"] = 1_200.0
        mcp_registry.connected_tools()
        assert calls["n"] == 1
        mcp_registry.save_connection({
            "id": "github", "name": "GitHub", "url": "https://example.test/gh", "auth": "none",
        })
        mcp_registry.connected_tools()
        assert calls["n"] == 2
        mcp_registry.connected_tools(force=True)
        assert calls["n"] == 3
    finally:
        mcp_registry.invalidate_tools_cache()


def test_turn_tokens_are_recorded_once(client, monkeypatch):
    def fake_stream(messages, **kwargs):
        yield "token", "ок"
        yield "usage", {"prompt": 1000, "completion": 200, "total": 1200}
        yield "done", ""

    monkeypatch.setattr(agent_routes, "chat_stream", fake_stream)
    monkeypatch.setattr(
        agent_routes, "chat_completion",
        lambda *args, **kwargs: ("повтор", {"usage": {"prompt": 1000, "completion": 200, "total": 1200}}),
    )
    events = _ndjson(client.post("/api/agent/stream", json={"message": "Привет"}))
    result = next(event for event in events if event["type"] == "result")
    assert result["tokens"]["total"] == 1200
    usage = client.get("/api/usage").get_json()
    assert usage["turns"] == 1
    assert usage["total"] == 1200
    assert usage["prompt"] == 1000 and usage["completion"] == 200
    # Шапка показывает total, а не prompt+completion+total.
    assert usage["total"] != usage["prompt"] + usage["completion"] + usage["total"]

    events = _ndjson(client.post("/api/agent/stream", json={"message": "Спасибо"}))
    second = next(event for event in events if event["type"] == "result")
    usage = client.get("/api/usage").get_json()
    assert second["tokens"]["total"] == 1200
    assert usage["turns"] == 2
    assert usage["total"] == result["tokens"]["total"] + second["tokens"]["total"]

    import database
    database.record_turn_usage("again", "same-key", 5, 5, 10)
    database.record_turn_usage("again", "same-key", 5, 5, 10)
    scoped = database.usage_summary("again")
    assert scoped["turns"] == 1 and scoped["total"] == 10


def test_stream_saves_trace_without_request_context(client, monkeypatch):
    """После чтения истории генератор не должен трогать session."""
    real_session = agent_routes.session

    class Gate:
        open = True

        def get(self, *args, **kwargs):
            if not Gate.open:
                raise RuntimeError("Working outside of request context")
            return real_session.get(*args, **kwargs)

        def __getitem__(self, key):
            if not Gate.open:
                raise RuntimeError("Working outside of request context")
            return real_session[key]

        def __setitem__(self, key, value):
            if not Gate.open:
                raise RuntimeError("Working outside of request context")
            real_session[key] = value

    monkeypatch.setattr(agent_routes, "session", Gate())
    real_turn = agent_routes._turn_history

    def wrapped(history_key, limit=10):
        result = real_turn(history_key, limit=limit)
        Gate.open = False
        return result

    monkeypatch.setattr(agent_routes, "_turn_history", wrapped)
    _stub_stream(monkeypatch, ["Привет!"])
    events = _ndjson(client.post("/api/agent/stream", json={"message": "Привет"}))
    result = next(event for event in events if event["type"] == "result")
    assert "Привет" in _answer(events)
    import database
    trace = database.latest_agent_trace(result["chat_id"])
    assert trace and trace["goal"] == "Привет"
    assert any(event.get("type") == "result" for event in trace["events"])


def test_fast_context_stays_bounded_over_fifteen_turns(client, monkeypatch):
    sizes = []

    def fake_stream(messages, **kwargs):
        sizes.append(sum(len(str(item.get("content") or "")) for item in messages))
        yield "token", "ок"
        yield "done", ""

    monkeypatch.setattr(agent_routes, "chat_stream", fake_stream)
    monkeypatch.setattr(agent_routes, "chat_completion", lambda *args, **kwargs: ("ок", {}))
    for _ in range(15):
        events = _ndjson(client.post("/api/agent/stream", json={"message": "Привет"}))
        assert any(event["type"] == "result" for event in events)
    assert len(sizes) == 15
    assert max(sizes) < 2000
    assert sizes[-1] == sizes[8]


def test_offline_ignores_internal_observations_between_steps():
    """Служебная подсказка агента не должна становиться «вопросом пользователя»."""
    reply = local_llm.complete([
        {"role": "user", "content": "сколько времени"},
        {"role": "user", "content": "Инструменты рабочей папки отключены (TERMINAL_ENABLED=0 "
                                    "или нет входа). Не вызывай run/read/write; ответь обычным текстом."},
    ])
    assert "Инструменты рабочей папки" not in reply
    assert ":" in reply
