"""Нативный tool calling на уровне провайдера и реестра инструментов.

Сам агент работает на протоколе блоков AgenticSeek (``\u0060\u0060\u0060web_search``), а не
на ``tool_calls``: блок виден пользователю как шаг, не требует поддержки
``tools`` у провайдера и одинаково работает у всех моделей. Нативный слой
остаётся в ``ai_providers``/``tool_registry`` — им пользуются другие места
(чат с инструментами, MCP-панель), и его нельзя молча ломать.

Эти тесты проверяют обе половины: провайдер умеет tool_calls, а агент
по-прежнему ходит через блоки (никаких ``tools`` в запросе).
"""
import json

import pytest

import ai_providers
import routes.agent as agent_routes
import tool_registry
from tests.agent_helpers import answer_of, events_of, model_text, ndjson, script


def _ndjson(response):
    return [json.loads(line) for line in response.get_data(as_text=True).splitlines() if line.strip()]


def _answer(events):
    return "".join(event["token"] for event in events if event["type"] == "token")


def _normalized(call_id, name, arguments, arguments_error=""):
    """То, что ai_providers.normalize_tool_calls отдаёт агенту."""
    return {"id": call_id, "name": name, "arguments": arguments,
            "raw_arguments": json.dumps(arguments, ensure_ascii=False),
            "arguments_error": arguments_error}


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


# ══════════════ агент: протокол блоков ══════════════

def _fake_search(monkeypatch):
    results = [{"title": "Документация asyncio", "url": "https://docs.python.org/asyncio",
                "snippet": "asyncio — библиотека", "host": "docs.python.org"}]
    calls = []

    def search(query, limit=8):
        calls.append(query)
        return list(results), {"backend": "test", "query": query, "results": results}

    monkeypatch.setattr(agent_routes, "nova_search", search)
    monkeypatch.setattr(agent_routes, "nova_read",
                        lambda url, max_chars=6000: ("Текст страницы " + url, 2))
    return calls


def test_agent_sends_no_native_tools_to_the_model(admin_client, sandbox, monkeypatch):
    """Агент использует блоки, а не tool_calls: запрос к модели без ``tools``."""
    seen = []
    script(monkeypatch, ["Просто ответ"], seen)
    ndjson(admin_client.post("/api/agent/stream", json={"message": "найди asyncio"}))
    assert all(item.get("tools") is None for item in seen)
    assert "web_search" in model_text(seen).split("ДОСТУПНЫЕ")[-1]


def test_several_blocks_in_one_answer_run_in_order(admin_client, sandbox, monkeypatch):
    calls = _fake_search(monkeypatch)
    monkeypatch.setattr(agent_routes, "nova_read",
                        lambda url, max_chars=6000: ("Текст страницы", 2))

    def search(query, limit=8):
        return [{"title": "asyncio", "url": "https://docs.python.org/asyncio",
                 "snippet": "библиотека", "host": "docs.python.org"}], {"backend": "test"}

    monkeypatch.setattr(agent_routes, "nova_search", search)
    script(monkeypatch, [
        "```web_search\nasyncio\n```\n```web_open\nhttps://docs.python.org/asyncio\n```",
        "Готово: два инструмента",
    ])
    events = ndjson(admin_client.post("/api/agent/stream",
                                      json={"message": "найди и открой документацию asyncio"}))
    assert [tool["name"] for tool in events_of(events, "tool")] == ["web_search", "web_open"]
    assert answer_of(events) == "Готово: два инструмента"


def test_unknown_tool_tag_is_not_shown_to_the_user(admin_client, sandbox, monkeypatch):
    """Блок несуществующего инструмента не попадает в чат как «сделано»."""
    seen = []
    script(monkeypatch, ["Проверил.\n```nonexistent_tool\nx\n```"], seen)
    events = ndjson(admin_client.post("/api/agent/stream", json={"message": "сделай что-нибудь"}))
    assert not events_of(events, "tool")
    assert "nonexistent_tool" not in answer_of(events)
    assert "Проверил." in answer_of(events)
    assert any("недоступный инструмент" in event["text"] for event in events_of(events, "step"))


def test_tool_agent_gets_one_retry_after_unknown_block(admin_client, sandbox, monkeypatch):
    """Агент с инструментами получает отказ и отвечает словами."""
    seen = []
    script(monkeypatch, ["```no_such_tool\nx\n```", "Инструмента нет — объясню словами"], seen)
    events = ndjson(admin_client.post("/api/agent/stream",
                                      json={"message": "проверь файлы в рабочей папке"}))
    assert not events_of(events, "tool")
    assert answer_of(events) == "Инструмента нет — объясню словами"
    assert "no_such_tool" in model_text(seen, -1)


def test_block_payload_is_passed_verbatim(admin_client, sandbox, monkeypatch):
    """Содержимое блока не «чинится»: что модель написала, то и выполняется."""
    seen = []
    script(monkeypatch, ["```bash\necho привет\n```", "Готово"], seen)
    events = ndjson(admin_client.post("/api/agent/stream", json={"message": "выполни echo привет"}))
    tool = events_of(events, "tool")[0]
    assert "привет" in tool["output"]
