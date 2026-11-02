"""Провайдерские tool_calls и first-party ReAct через общий реестр NovaMind."""
import json

import pytest

import ai_providers
import guardrails
import tool_registry
import routes.agent as agent_routes
import routes.terminal as terminal_routes
from tests.agent_helpers import answer_of, events_of, model_text, ndjson, script


def test_supports_native_tools_by_provider_and_env(monkeypatch):
    assert ai_providers.supports_native_tools("groq", "llama") is True
    assert ai_providers.supports_native_tools("openrouter", "any") is True
    assert ai_providers.supports_native_tools("google_ai_studio", "gemini-2.0-flash") is True
    assert ai_providers.supports_native_tools("local_demo", "nova-local-1") is False
    monkeypatch.setenv("AI_NATIVE_TOOLS", "0")
    assert ai_providers.supports_native_tools("groq", "llama") is False


def test_normalize_tool_calls_handles_broken_json():
    calls = ai_providers.normalize_tool_calls([
        {"id": "a", "function": {"name": "web_search", "arguments": '{"query": "x"}'}},
        {"id": "b", "function": {"name": "web_open", "arguments": '{"url": '}},
        {"id": "c", "function": {"name": "shell", "arguments": {}}},
        "мусор",
    ])
    assert [call["name"] for call in calls] == ["web_search", "web_open", "shell"]
    assert calls[0]["arguments"] == {"query": "x"} and calls[0]["arguments_error"] == ""
    assert calls[1]["arguments"] == {} and "JSON" in calls[1]["arguments_error"]
    assert calls[2]["arguments_error"] == ""


def test_chat_completion_sends_tools_and_returns_calls(monkeypatch):
    captured = {}

    def fake_request(url, payload, headers, timeout=90, max_retries=2):
        captured["payload"] = payload
        return {"choices": [{"message": {"role": "assistant", "content": "",
                    "tool_calls": [{"id": "call_1", "type": "function",
                                    "function": {"name": "web_search",
                                                 "arguments": '{"query": "asyncio"}'}}]}}],
                "usage": {"total_tokens": 12}}, None

    monkeypatch.setattr(ai_providers, "resolve_target",
                        lambda *a, **k: ("groq", "llama-3.3-70b", {"url": "http://x", "max_tokens": 100}))
    monkeypatch.setattr(ai_providers, "openai_compatible_request", fake_request)
    monkeypatch.setattr(ai_providers, "groq_request_with_rotation", fake_request)
    text, meta = ai_providers.chat_completion(
        [{"role": "user", "content": "найди asyncio"}],
        tools=[{"type": "function", "function": {"name": "web_search", "parameters": {"type": "object"}}}],
    )
    assert captured["payload"]["tools"][0]["function"]["name"] == "web_search"
    assert captured["payload"]["tool_choice"] == "auto"
    assert text == ""
    assert meta["tool_calls"][0]["name"] == "web_search"
    assert meta["tool_calls"][0]["arguments"] == {"query": "asyncio"}


def test_chat_completion_ignores_tools_for_local_demo(monkeypatch):
    monkeypatch.setattr(ai_providers, "resolve_target",
                        lambda *a, **k: ("local_demo", "nova-local-1", {}))
    text, meta = ai_providers.chat_completion(
        [{"role": "user", "content": "привет"}],
        tools=[{"type": "function", "function": {"name": "web_search"}}])
    assert meta["tool_calls"] == [] and meta["offline"] is True


def test_gemini_payload_converts_tools_and_messages():
    tools = [{"type": "function", "function": {
        "name": "web_search", "description": "поиск",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}},
                       "additionalProperties": False}}}]
    payload = ai_providers.gemini_tools_payload(tools)
    declaration = payload[0]["functionDeclarations"][0]
    assert declaration["name"] == "web_search"
    assert "additionalProperties" not in declaration["parameters"]
    system, contents = ai_providers.gemini_messages_from_openai([
        {"role": "system", "content": "ты агент"},
        {"role": "user", "content": "найди"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "web_search", "arguments": '{"query": "x"}'}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "результаты"},
    ])
    assert system == "ты агент"
    assert contents[1]["parts"][0]["functionCall"] == {"name": "web_search", "args": {"query": "x"}}
    assert contents[2]["parts"][0]["functionResponse"]["name"] == "web_search"


def test_first_party_registry_exposes_complete_valid_schemas():
    tool_registry.bootstrap_default_tools()
    schemas = tool_registry.native_tool_schemas()
    names = {tool["function"]["name"] for tool in schemas}
    assert {"search", "open", "run", "read", "write", "edit", "grep", "tree",
            "mcp_call", "image", "audio", "video", "task", "task_done"} <= names
    for tool in schemas:
        assert tool["type"] == "function"
        assert tool["function"]["parameters"]["type"] == "object"


def test_guest_registry_hides_workspace_capabilities():
    tool_registry.bootstrap_default_tools()
    names = {tool["name"] for tool in tool_registry.tool_schemas(workspace=False)}
    assert {"web_search", "web_open"} <= names
    assert not names.intersection({"shell", "file_read", "file_write", "file_edit",
                                   "file_search", "file_tree"})


def test_tool_registry_reports_risk():
    tool_registry.bootstrap_default_tools()
    risks = {item["name"]: item["risk"] for item in tool_registry.tool_schemas()}
    assert risks["web_search"] == "safe"
    assert risks["file_write"] == "write"
    assert risks["mcp_call"] == "danger"


def test_react_passes_native_schemas_and_executes_model_tool_calls(admin_client, sandbox, monkeypatch):
    results = [{"title": "Документация asyncio", "url": "https://docs.python.org/asyncio",
                "snippet": "asyncio — библиотека", "host": "docs.python.org"}]
    monkeypatch.setattr(agent_routes, "nova_search", lambda query, limit=8:
                        (results, {"results": results, "count": 1, "query": query}))
    monkeypatch.setattr(ai_providers, "resolve_target",
                        lambda *a, **k: ("groq", "mock", {"max_tokens": 2048}))
    seen = []
    answers = iter([
        ("", {"tool_calls": [{"id": "c1", "name": "web_search",
                               "arguments": {"query": "asyncio"}}]}),
        ("Нашёл документацию asyncio.", {"model": "mock"}),
    ])

    def complete(messages, **kwargs):
        seen.append(kwargs)
        return next(answers)

    monkeypatch.setattr(ai_providers, "chat_completion", complete)
    events = ndjson(admin_client.post("/api/agent/stream", json={"message": "найди asyncio"}))
    assert seen and any(item["function"]["name"] == "web_search" for item in seen[0]["tools"])
    assert events_of(events, "tool")[0]["name"] == "web_search"
    assert events_of(events, "sources")[0]["sources"][0]["url"].endswith("/asyncio")
    assert "Нашёл документацию" in answer_of(events)


def test_multiple_first_party_tool_calls_run_sequentially(admin_client, sandbox, monkeypatch):
    monkeypatch.setattr(agent_routes, "nova_search", lambda *a, **k: (
        [{"title": "asyncio", "url": "https://docs.python.org/asyncio", "snippet": "библиотека"}], {}))
    script(monkeypatch, ["```web_search\nasyncio\n```\n```web_open\nhttps://docs.python.org/asyncio\n```",
                         "Готово: два инструмента"])
    events = ndjson(admin_client.post("/api/agent/stream",
                                      json={"message": "найди и открой документацию asyncio"}))
    assert [tool["name"] for tool in events_of(events, "tool")] == ["web_search", "web_open"]
    assert answer_of(events) == "Готово: два инструмента"


def test_unknown_tool_call_is_reported_not_exposed_as_answer(admin_client, sandbox, monkeypatch):
    seen = []
    script(monkeypatch, ['```json\n{"tool":"not_registered","arguments":{"x":1}}\n```',
                         "Проверил: такого инструмента нет."] , seen)
    events = ndjson(admin_client.post("/api/agent/stream", json={"message": "проверь"}))
    tool = events_of(events, "tool")[0]
    assert tool["error_type"] == "unknown_tool"
    assert "not_registered" not in answer_of(events)
    assert "not_registered" in model_text(seen, -1)


def test_tools_false_sends_no_schemas_and_executes_no_calls(admin_client, sandbox, monkeypatch):
    seen = []
    script(monkeypatch, ['```json\n{"tool":"shell","arguments":{"command":"touch forbidden"}}\n```',
                         "Инструменты отключены, действие не выполнено."], seen)
    events = ndjson(admin_client.post("/api/agent/stream", json={
        "message": "создай файл", "tools": False}))
    assert seen[0]["tools"] is None
    assert all(item["status"] == "error" and item.get("error_type") == "unknown_tool"
               for item in events_of(events, "tool"))
    assert not (sandbox / "forbidden").exists()
    assert "действие не выполнено" in answer_of(events)


def test_shell_comment_handling_is_first_party(monkeypatch):
    prepared, _ = terminal_routes.check_command("ls -la   # список файлов")
    assert prepared[0][0] == ["ls", "-la"]
    quoted, _ = terminal_routes.check_command("echo '# не комментарий'")
    assert quoted[0][0] == ["echo", "# не комментарий"]
    with pytest.raises(ValueError, match="Пустая команда"):
        terminal_routes.check_command("# один комментарий")


def test_shell_rejects_server_launch_and_allows_flask_test_client(admin_client, sandbox,
                                                                   monkeypatch):
    (sandbox / "app.py").write_text("from flask import Flask\napp = Flask(__name__)\napp.run()\n",
                                    encoding="utf-8")
    denied = terminal_routes.run_agent("python3 app.py")
    assert denied["code"] == 2 and "test_client" in denied["stderr"]
    captured = []
    monkeypatch.setattr(terminal_routes, "run_command", lambda command, timeout=None, cancel=None:
                        (captured.append((command, timeout)) or {
                            "code": 0, "stdout": "200", "stderr": "", "timed_out": False}))
    (sandbox / "smoke.py").write_text("from app import app\nprint(app.test_client().get('/').status_code)\n",
                                      encoding="utf-8")
    assert terminal_routes.run_agent("python3 smoke.py")["stdout"] == "200"
    assert captured[0][0] == "python3 smoke.py"


def test_agent_shell_comment_only_failure_is_explained_and_recoverable(admin_client, sandbox,
                                                                         monkeypatch):
    seen = []
    script(monkeypatch, ["```bash\n# в корне проекта\n```",
                         "```python:app.py\nprint('hello')\n```", "Готово: app.py создан."], seen)
    events = ndjson(admin_client.post("/api/agent/stream",
                                      json={"message": "Создай app.py"}))
    assert "Готово: app.py создан." in answer_of(events)
    tools = events_of(events, "tool")
    assert tools[0]["name"] == "shell" and tools[0]["status"] == "error"
    assert "Пустая команда" in tools[0]["result"]["stderr"]
    assert (sandbox / "app.py").is_file()


def test_shell_timeout_and_oom_hints_are_actionable():
    timeout = tool_registry._shell_result({"code": -9, "stdout": "", "stderr": "", "timed_out": True})
    assert timeout["error"]["type"] == "timeout" and "разделите задачу" in timeout["hint"]
    oom = tool_registry._shell_result({"code": -9, "stdout": "", "stderr": "", "timed_out": False})
    assert "нехватки памяти" in oom["hint"] and "Termux" in oom["hint"]


def test_slow_install_operations_get_bounded_longer_timeouts(monkeypatch):
    calls = []
    monkeypatch.setattr(terminal_routes, "run_command", lambda command, timeout=None, cancel=None:
                        (calls.append((command, timeout)) or {"code": 0, "stdout": "", "stderr": ""}))
    terminal_routes.run_agent("pip install flask", timeout=12)
    terminal_routes.run_agent("ls -la", timeout=12)
    assert calls[0][1] >= 60 and calls[0][1] <= 120
    assert calls[1][1] < 60


def test_absolute_paths_in_agent_commands_are_rejected():
    with __import__("pytest").raises(ValueError):
        terminal_routes.check_command("python -m venv /tmp/nova-venv")
