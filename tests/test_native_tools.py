"""Нативный tool calling (этап 1).

Модель вызывает настоящие функции вместо JSON-текста, но старый протокол
{"action": ...} остаётся запасным режимом:

  * провайдер принимает `tools` — работаем нативно, включая несколько
    tool_calls в одном ответе;
  * провайдер не знает про `tools` — молча откатываемся на JSON;
  * аргументы вызова пришли битым JSON — инструмент не выполняем,
    а просим модель повторить.
"""
import json

import pytest

import ai_providers
import routes.agent as agent_routes
import tool_registry


def _ndjson(response):
    return [json.loads(line) for line in response.get_data(as_text=True).splitlines() if line.strip()]


def _answer(events):
    return "".join(event["token"] for event in events if event["type"] == "token")


def _normalized(call_id, name, arguments, arguments_error=""):
    """То, что ai_providers.normalize_tool_calls отдаёт агенту."""
    return {"id": call_id, "name": name, "arguments": arguments,
            "raw_arguments": json.dumps(arguments, ensure_ascii=False),
            "arguments_error": arguments_error}


@pytest.fixture
def fake_search(monkeypatch):
    results = [{"title": "Документация asyncio", "url": "https://docs.python.org/asyncio",
                "snippet": "asyncio — библиотека", "host": "docs.python.org"}]
    calls = []

    def search(query, limit=8):
        calls.append(query)
        return list(results), {"backend": "test", "elapsed_ms": 2, "query": query,
                               "results": results, "count": len(results),
                               "trace": [{"backend": "test", "ok": True, "ms": 2}]}

    monkeypatch.setattr(agent_routes, "nova_search", search)
    monkeypatch.setattr(agent_routes, "nova_read", lambda url, max_chars=4000: ("Текст страницы " + url, 2))
    return calls


@pytest.fixture
def native_provider(monkeypatch):
    """Провайдер, который умеет нативный tool calling."""
    monkeypatch.setattr(agent_routes, "resolve_target", lambda *a, **k: ("groq", "llama-3.3-70b", {}))
    agent_routes._NATIVE_TOOL_BLOCKED.clear()
    yield
    agent_routes._NATIVE_TOOL_BLOCKED.clear()


def _stub_native(monkeypatch, script, seen=None):
    """Модель отвечает заранее заданными шагами (native tool calls)."""
    queue = list(script)

    def fake_completion(messages, **kwargs):
        if seen is not None:
            seen.append({"messages": list(messages), "tools": kwargs.get("tools"),
                         "system": kwargs.get("system", "")})
        item = queue.pop(0) if queue else {"text": "готово"}
        if isinstance(item, str):
            return item, {"provider": "groq", "tool_calls": []}
        return item.get("text", ""), {"provider": "groq",
                                      "tool_calls": item.get("tool_calls") or [],
                                      "native_tools": True}

    monkeypatch.setattr(agent_routes, "chat_completion", fake_completion)
    monkeypatch.setattr(agent_routes, "chat_stream", lambda *a, **k: iter(()))
    return queue


# ══════════════ ai_providers ══════════════

def test_supports_native_tools_by_provider_and_env(monkeypatch):
    assert ai_providers.supports_native_tools("groq", "llama") is True
    assert ai_providers.supports_native_tools("openrouter", "any") is True
    assert ai_providers.supports_native_tools("google_ai_studio", "gemini-2.0-flash") is True
    assert ai_providers.supports_native_tools("local_demo", "nova-local-1") is False

    monkeypatch.setenv("AI_NATIVE_TOOLS", "0")
    assert ai_providers.supports_native_tools("groq", "llama") is False


def test_normalize_tool_calls_handles_broken_json():
    calls = ai_providers.normalize_tool_calls([
        {"id": "a", "function": {"name": "search", "arguments": '{"query": "x"}'}},
        {"id": "b", "function": {"name": "open", "arguments": '{"url": '}},
        {"id": "c", "function": {"name": "ls", "arguments": {}}},
        "мусор",
    ])

    assert [call["name"] for call in calls] == ["search", "open", "ls"]
    assert calls[0]["arguments"] == {"query": "x"} and calls[0]["arguments_error"] == ""
    assert calls[1]["arguments"] == {} and "JSON" in calls[1]["arguments_error"]
    assert calls[2]["arguments_error"] == ""


def test_chat_completion_sends_tools_and_returns_calls(monkeypatch):
    captured = {}

    def fake_request(url, payload, headers, timeout=90, max_retries=2):
        captured["payload"] = payload
        return {"choices": [{"message": {"role": "assistant", "content": "",
                                         "tool_calls": [{
                                             "id": "call_1", "type": "function",
                                             "function": {"name": "search",
                                                          "arguments": '{"query": "asyncio"}'}}]}}],
                "usage": {"total_tokens": 12}}, None

    monkeypatch.setattr(ai_providers, "resolve_target",
                        lambda *a, **k: ("groq", "llama-3.3-70b", {"url": "http://x", "max_tokens": 100}))
    monkeypatch.setattr(ai_providers, "openai_compatible_request", fake_request)
    monkeypatch.setattr(ai_providers, "groq_request_with_rotation", fake_request)

    text, meta = ai_providers.chat_completion(
        [{"role": "user", "content": "найди asyncio"}],
        tools=[{"type": "function", "function": {"name": "search", "parameters": {"type": "object"}}}],
    )

    assert captured["payload"]["tools"][0]["function"]["name"] == "search"
    assert captured["payload"]["tool_choice"] == "auto"
    assert text == ""                      # пустой content при tool_calls — норма
    assert meta["tool_calls"][0]["name"] == "search"
    assert meta["tool_calls"][0]["arguments"] == {"query": "asyncio"}


def test_chat_completion_ignores_tools_for_local_demo(monkeypatch):
    monkeypatch.setattr(ai_providers, "resolve_target",
                        lambda *a, **k: ("local_demo", "nova-local-1", {}))
    text, meta = ai_providers.chat_completion(
        [{"role": "user", "content": "привет"}],
        tools=[{"type": "function", "function": {"name": "search"}}],
    )
    assert meta["tool_calls"] == [] and meta["offline"] is True


def test_gemini_payload_converts_tools_and_messages():
    tools = [{"type": "function", "function": {
        "name": "search", "description": "поиск",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}},
                       "additionalProperties": False}}}]
    payload = ai_providers.gemini_tools_payload(tools)

    declaration = payload[0]["functionDeclarations"][0]
    assert declaration["name"] == "search"
    assert "additionalProperties" not in declaration["parameters"]

    system, contents = ai_providers.gemini_messages_from_openai([
        {"role": "system", "content": "ты агент"},
        {"role": "user", "content": "найди"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "search", "arguments": '{"query": "x"}'}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "результаты"},
    ])

    assert system == "ты агент"
    assert contents[1]["parts"][0]["functionCall"] == {"name": "search", "args": {"query": "x"}}
    assert contents[2]["parts"][0]["functionResponse"]["name"] == "search"


# ══════════════ реестр инструментов ══════════════

def test_native_schemas_contain_required_tools():
    tool_registry.bootstrap_default_tools()
    names = [tool["function"]["name"] for tool in tool_registry.native_tool_schemas()]

    for required in ("search", "open", "run", "read", "write", "ls", "mcp_call",
                     "image", "audio", "video", "skill_create", "task", "task_done", "answer"):
        assert required in names
    # у каждого инструмента корректная OpenAI-схема
    for tool in tool_registry.native_tool_schemas():
        assert tool["type"] == "function"
        assert tool["function"]["parameters"]["type"] == "object"


def test_guest_native_schemas_hide_workspace_tools():
    tool_registry.bootstrap_default_tools()
    names = [tool["function"]["name"]
             for tool in tool_registry.native_tool_schemas(workspace=False)]
    assert "search" in names and "answer" in names
    assert "run" not in names and "write" not in names


def test_tool_schemas_report_risk():
    tool_registry.bootstrap_default_tools()
    risks = {item["name"]: item["risk"] for item in tool_registry.tool_schemas()}
    assert risks["web_search"] == "safe"
    assert risks["file_write"] == "write"
    assert risks["mcp_call"] == "danger"


# ══════════════ агент: нативный режим ══════════════

def test_agent_uses_native_tool_call_and_answers(admin_client, sandbox, fake_search,
                                                 native_provider, monkeypatch):
    seen = []
    _stub_native(monkeypatch, [
        {"tool_calls": [_normalized("call_1", "search", {"query": "asyncio"})]},
        {"tool_calls": [_normalized("call_2", "answer", {"text": "asyncio — библиотека Python"})]},
        # ответ на обязательную самопроверку (этап 5)
        {"tool_calls": [_normalized("call_3", "answer", {"text": "asyncio — библиотека Python"})]},
    ], seen)

    events = _ndjson(admin_client.post("/api/agent/stream", json={"message": "найди документацию asyncio"}))

    assert fake_search == ["asyncio"]
    tool = next(event for event in events if event["type"] == "tool")
    assert tool["name"] == "search"
    assert "asyncio" in _answer(events)

    # модель получила инструменты и историю с результатом вызова
    assert seen[0]["tools"] and seen[0]["tools"][0]["function"]["name"] == "search"
    assert "НАТИВНОГО TOOL CALLING" in seen[0]["system"]
    tool_messages = [item for item in seen[1]["messages"] if item.get("role") == "tool"]
    assert tool_messages and tool_messages[0]["tool_call_id"] == "call_1"
    assistant_calls = [item for item in seen[1]["messages"]
                       if item.get("role") == "assistant" and item.get("tool_calls")]
    assert assistant_calls[0]["tool_calls"][0]["function"]["name"] == "search"


def test_agent_executes_every_native_call_from_one_response(admin_client, sandbox, fake_search,
                                                            native_provider, monkeypatch):
    seen = []
    _stub_native(monkeypatch, [
        {"tool_calls": [
            _normalized("call_1", "search", {"query": "asyncio"}),
            _normalized("call_2", "open", {"url": "https://docs.python.org/asyncio"}),
        ]},
        {"tool_calls": [_normalized("call_3", "answer", {"text": "готово: два инструмента"})]},
        {"tool_calls": [_normalized("call_4", "answer", {"text": "готово: два инструмента"})]},
    ], seen)

    events = _ndjson(admin_client.post("/api/agent/stream", json={"message": "найди и изучи asyncio по шагам"}))

    names = [event["name"] for event in events if event["type"] == "tool"]
    assert names == ["search", "open"]

    tool_messages = [item for item in seen[1]["messages"] if item.get("role") == "tool"]
    assert [item["tool_call_id"] for item in tool_messages] == ["call_1", "call_2"]
    assert "два инструмента" in _answer(events)


def test_agent_falls_back_to_json_protocol_when_tools_unsupported(admin_client, sandbox, fake_search,
                                                                  native_provider, monkeypatch):
    answers = ['{"action":"search","query":"asyncio"}', "asyncio — библиотека [1]"]
    calls = {"with_tools": 0}

    def completion(messages, **kwargs):
        if kwargs.get("tools"):
            calls["with_tools"] += 1
            raise TypeError("chat_completion() got an unexpected keyword argument 'tools'")
        return (answers.pop(0) if answers else "готово"), {}

    monkeypatch.setattr(agent_routes, "chat_completion", completion)
    monkeypatch.setattr(agent_routes, "chat_stream", lambda *a, **k: iter(()))

    events = _ndjson(admin_client.post("/api/agent/stream", json={"message": "найди asyncio"}))

    assert calls["with_tools"] == 1                     # попробовали ровно один раз
    assert any(event["type"] == "step" and "JSON-протокол" in event["text"] for event in events)
    assert fake_search == ["asyncio"]
    assert "библиотека" in _answer(events)
    # провайдер запомнен как «без tools» и больше не проверяется
    assert ("groq", "llama-3.3-70b") in agent_routes._NATIVE_TOOL_BLOCKED


def test_agent_retries_when_tool_arguments_are_broken(admin_client, sandbox, fake_search,
                                                      native_provider, monkeypatch):
    broken = _normalized("call_1", "search", {}, arguments_error="аргументы не JSON")
    seen = []
    _stub_native(monkeypatch, [
        {"tool_calls": [broken]},
        {"tool_calls": [_normalized("call_2", "answer", {"text": "повторил и ответил"})]},
    ], seen)

    events = _ndjson(admin_client.post("/api/agent/stream", json={"message": "найди asyncio"}))

    assert fake_search == []                            # инструмент не выполнялся
    assert any(event["type"] == "step" and "Не разобрал аргументы" in event["text"] for event in events)
    tool_messages = [item for item in seen[1]["messages"] if item.get("role") == "tool"]
    assert tool_messages and "ОШИБКА" in tool_messages[0]["content"]
    assert "повторил" in _answer(events)


def test_agent_native_mode_can_be_disabled_by_env(admin_client, sandbox, fake_search,
                                                  native_provider, monkeypatch):
    monkeypatch.setenv("AGENT_NATIVE_TOOLS", "0")
    monkeypatch.setattr(agent_routes, "NATIVE_TOOLS_ENABLED", False)
    answers = ['{"action":"search","query":"asyncio"}', "ответ по JSON [1]"]
    monkeypatch.setattr(agent_routes, "chat_completion",
                        lambda messages, **kwargs: ((answers.pop(0) if answers else "готово"), {}))
    monkeypatch.setattr(agent_routes, "chat_stream", lambda *a, **k: iter(()))

    events = _ndjson(admin_client.post("/api/agent/stream", json={"message": "найди asyncio"}))

    assert fake_search == ["asyncio"]
    assert "JSON" in _answer(events)
