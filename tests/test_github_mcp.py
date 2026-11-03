"""Тесты GitHub MCP в агенте, отображения ошибок MCP и логирования в nova.log."""
from __future__ import annotations

import json
import logging

import pytest
import requests

import config
import mcp_registry
from tests.agent_helpers import answer_of, events_of, ndjson, script


class _FakeResp:
    def __init__(self, status_code: int, body: dict | str, headers: dict | None = None):
        self.status_code = status_code
        self.headers = {"content-type": "application/json", **(headers or {})}
        if isinstance(body, dict):
            self._json = body
            self.text = json.dumps(body, ensure_ascii=False)
        else:
            self._json = None
            self.text = str(body)

    def json(self):
        if self._json is None:
            raise ValueError("not json")
        return self._json


def test_mcp_calls_preserve_server_tool_names_and_arguments(admin_client, sandbox, monkeypatch):
    """MCP tool names and arguments are passed through without GitHub-specific rewriting."""
    calls: list[tuple[str, str, dict]] = []

    monkeypatch.setattr(mcp_registry, "connected_tools", lambda: [
        {"server_id": "github", "name": "get_me"},
        {"server_id": "github", "name": "search_repositories"},
        {"server_id": "github", "name": "search_code"},
    ])

    def fake_call_tool(server_id, tool_name, arguments=None):
        calls.append((server_id, tool_name, dict(arguments or {})))
        if tool_name == "get_me":
            return {"login": "bunyod"}
        return {"items": [{"full_name": "demo/mt5-bot", "html_url": "https://github.com/demo/mt5-bot"}]}

    monkeypatch.setattr(mcp_registry, "call_tool", fake_call_tool)
    monkeypatch.setenv("AGENT_CONFIRM", "off")

    script(monkeypatch, [
        '```mcp_call\n{"server_id": "github", "tool": "search_code", '
        '"arguments": {"query": "пожалуйста найди на github несколько программ торговых ботов python"}}\n```',
        '```mcp_call\n{"server_id": "github", "tool": "search_repositories", '
        '"arguments": {"query": "mt5 trading bot", "minimal_output": false}}\n```',
        "Нашёл репозиторий demo/mt5-bot.",
    ])

    events = ndjson(admin_client.post(
        "/api/agent/stream",
        json={"message": "найди на github программы и репозитории торговых ботов"},
    ))

    assert answer_of(events) == "Нашёл репозиторий demo/mt5-bot."
    assert [c[1] for c in calls] == ["search_code", "search_repositories"]
    assert calls[0][2] == {"query": "пожалуйста найди на github несколько программ торговых ботов python"}
    assert calls[1][2] == {"query": "mt5 trading bot", "minimal_output": False}


def test_mcp_only_request_exposes_registry_tools_without_shell(
    admin_client, sandbox, tmp_path, monkeypatch
):
    """MCP-only request calls the connected tool directly; local shell is not offered."""
    store = tmp_path / "mcp_connections.json"
    store.write_text(json.dumps({
        "github-real-id": {
            "name": "GitHub MCP", "url": "https://mcp.example.test/mcp",
            "auth": "bearer", "token": "secret", "connected": True,
            "initialized": True,
        }
    }), encoding="utf-8")
    monkeypatch.setattr(mcp_registry, "STORE", store)
    monkeypatch.setattr(mcp_registry, "connected_tools", lambda: [
        {"server_id": "github-real-id", "server_name": "GitHub MCP",
         "name": "search_repositories", "inputSchema": {"type": "object"}},
    ])
    calls = []
    monkeypatch.setattr(
        mcp_registry, "call_tool",
        lambda server_id, tool, arguments=None: calls.append(
            (server_id, tool, dict(arguments or {}))) or {"items": [{"full_name": "demo/repo"}]},
    )
    monkeypatch.setenv("AGENT_CONFIRM", "off")
    seen = []
    script(monkeypatch, [
        '```mcp_call\n{"server_id":"github-real-id","tool":"search_repositories",'
        '"arguments":{"query":"flask"}}\n```',
        "Нашёл demo/repo.",
    ], seen)
    events = ndjson(admin_client.post("/api/agent/stream", json={
        "message": "используй только GitHub MCP и найди flask репозиторий",
    }))

    assert calls == [
        ("github-real-id", "search_repositories", {"query": "flask"}),
    ]
    tool = events_of(events, "tool")[0]
    assert tool["name"] == "mcp_call" and tool["status"] == "ok"
    offered = {item["function"]["name"] for item in seen[0]["tools"]}
    assert "shell" not in offered and "mcp_call" in offered
    assert answer_of(events) == "Нашёл demo/repo."


def test_mcp_only_request_refuses_ordinary_shell(admin_client, sandbox, monkeypatch):
    monkeypatch.setattr(mcp_registry, "connected_tools", lambda: [
        {"server_id": "github", "name": "search_repositories"},
    ])
    monkeypatch.setenv("AGENT_CONFIRM", "off")
    script(monkeypatch, ["```terminal\nuname -s\n```", "Shell не запускался."])
    events = ndjson(admin_client.post("/api/agent/stream", json={
        "message": "используй только GitHub MCP; запусти uname",
    }))
    tool = events_of(events, "tool")[0]
    assert tool["name"] == "shell" and tool["status"] == "error"
    assert "недоступен в этом запуске" in tool["output"]


def test_mcp_search_calls_have_no_arbitrary_three_call_cap(admin_client, sandbox, monkeypatch):
    """MCP searches can exceed three calls while the global ReAct step budget remains bounded."""
    search_invocations = []

    monkeypatch.setattr(mcp_registry, "connected_tools", lambda: [
        {"server_id": "github", "name": "get_me"},
        {"server_id": "github", "name": "search_repositories"},
    ])

    def fake_call_tool(server_id, tool_name, arguments=None):
        if tool_name != "get_me":
            search_invocations.append((tool_name, dict(arguments or {})))
        return {"items": []}

    monkeypatch.setattr(mcp_registry, "call_tool", fake_call_tool)
    monkeypatch.setenv("AGENT_CONFIRM", "off")

    script(monkeypatch, [
        '```mcp_call\n{"server_id": "github", "tool": "search_repositories", "arguments": {"query": "q1"}}\n```',
        '```mcp_call\n{"server_id": "github", "tool": "search_repositories", "arguments": {"query": "q2"}}\n```',
        '```mcp_call\n{"server_id": "github", "tool": "search_repositories", "arguments": {"query": "q3"}}\n```',
        '```mcp_call\n{"server_id": "github", "tool": "search_repositories", "arguments": {"query": "q4"}}\n```',
        "Отвечаю по трём выполненным поискам.",
    ])

    events = ndjson(admin_client.post(
        "/api/agent/stream",
        json={"message": "найди репозитории на github"},
    ))

    assert len(search_invocations) == 4
    tool_events = events_of(events, "tool")
    assert len(tool_events) == 4
    assert all(event["status"] == "ok" for event in tool_events)
    assert answer_of(events) == "Отвечаю по трём выполненным поискам."


def test_mcp_registry_retries_only_safe_discovery_requests(tmp_path, monkeypatch):
    """Rate-limit retries are bounded and limited to idempotent MCP discovery."""
    store = tmp_path / "mcp_connections.json"
    store.write_text(json.dumps({
        "github": {"name": "GitHub", "url": "https://mcp.example.test/mcp",
                   "auth": "bearer", "token": "secret", "transport": "streamable_http",
                   "connected": True, "initialized": True}
    }), encoding="utf-8")
    monkeypatch.setattr(mcp_registry, "STORE", store)
    monkeypatch.setenv("MCP_MAX_RETRIES", "2")
    slept = []
    monkeypatch.setattr(mcp_registry.time, "sleep", lambda delay: slept.append(delay))
    calls = []
    responses = iter([
        _FakeResp(429, {"message": "Too Many Requests"}, headers={"Retry-After": "1"}),
        _FakeResp(403, {"message": "You have triggered a secondary rate limit"}),
        _FakeResp(200, {"jsonrpc": "2.0", "id": 1, "result": {"tools": []}}),
    ])
    monkeypatch.setattr(mcp_registry.requests, "post", lambda *a, **kw: calls.append(kw) or next(responses))

    result = mcp_registry.request("github", "tools/list")
    assert result["result"]["tools"] == []
    assert len(calls) == 3
    assert slept == [1.0, pytest.approx(.4)]


def test_mcp_registry_never_retries_non_idempotent_tool_calls(tmp_path, monkeypatch):
    store = tmp_path / "mcp_connections.json"
    store.write_text(json.dumps({
        "notes": {"name": "Notes", "url": "https://mcp.example.test/mcp",
                  "transport": "streamable_http", "auth": "none",
                  "connected": True, "initialized": True}
    }), encoding="utf-8")
    monkeypatch.setattr(mcp_registry, "STORE", store)
    monkeypatch.setenv("MCP_MAX_RETRIES", "3")
    monkeypatch.setattr(mcp_registry, "_TOOL_CACHE", {
        "notes": (mcp_registry.time.monotonic() + 60, [{
            "name": "create_note", "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
            "annotations": {"readOnlyHint": False, "destructiveHint": False},
        }])
    })
    calls = []
    monkeypatch.setattr(mcp_registry.requests, "post", lambda *a, **kw: calls.append(kw) or _FakeResp(503, {"message": "unavailable"}))
    with pytest.raises(mcp_registry.MCPError, match="503"):
        mcp_registry.call_tool("notes", "create_note", {"text": "hello"})
    assert len(calls) == 1


def test_mcp_error_surfaces_real_server_message_and_logs_status_to_nova_log(
    admin_client, sandbox, tmp_path, monkeypatch
):
    """В блоке «Ошибка MCP» виден настоящий текст сервера + код ответа попадает в nova.log."""
    log_path = tmp_path / "nova.log"
    log_path.write_text("СТАРАЯ СТРОКА ЛОГА\n", encoding="utf-8")

    root_logger = logging.getLogger()
    added_handlers = []
    monkeypatch.setenv("NOVA_LOG_FILE", str(log_path))
    before = set(root_logger.handlers)
    config.configure_logging(str(log_path), force_file=True)
    added_handlers = [h for h in root_logger.handlers if h not in before]

    try:
        store = tmp_path / "mcp_connections.json"
        store.write_text(json.dumps({
            "github": {"name": "GitHub", "url": "https://mcp.example.test/mcp",
                       "auth": "bearer", "token": "secret-token-value",
                       "connected": True, "initialized": True}
        }), encoding="utf-8")
        monkeypatch.setattr(mcp_registry, "STORE", store)
        monkeypatch.setenv("MCP_MAX_RETRIES", "0")
        monkeypatch.setenv("AGENT_CONFIRM", "off")

        monkeypatch.setattr(mcp_registry, "connected_tools", lambda: [
            {"server_id": "github", "name": "search_repositories"},
        ])
        monkeypatch.setattr(
            mcp_registry.requests,
            "post",
            lambda *a, **kw: _FakeResp(
                403,
                {"message": "API rate limit exceeded for installation"},
            ),
        )

        script(monkeypatch, [
            '```mcp_call\n{"server_id": "github", "tool": "search_repositories", "arguments": {"query": "flask"}}\n```',
            "Сервер GitHub вернул лимит запросов.",
        ])

        events = ndjson(admin_client.post(
            "/api/agent/stream",
            json={"message": "найди репозитории flask на github"},
        ))

        tool_ev = events_of(events, "tool")[0]
        assert tool_ev["label"] == "Ошибка MCP"
        assert tool_ev["code"] == 403
        assert "API rate limit exceeded for installation" in tool_ev["error"]
        assert "API rate limit exceeded for installation" in tool_ev["output"]

        for h in added_handlers:
            h.flush()
        log_text = log_path.read_text(encoding="utf-8")
        # Старый лог не затёрт (режим append), код ответа и текст ошибки записаны, секрета нет
        assert log_text.startswith("СТАРАЯ СТРОКА ЛОГА\n")
        assert "status=403" in log_text
        assert "API rate limit exceeded for installation" in log_text
        assert "secret-token-value" not in log_text
    finally:
        for h in added_handlers:
            root_logger.removeHandler(h)
            h.close()
