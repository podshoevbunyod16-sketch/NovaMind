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


def test_github_mcp_rewrites_search_code_sets_minimal_output_and_calls_get_me_once(
    admin_client, sandbox, monkeypatch
):
    """Запрос «найди программы/репозитории»: get_me 1 раз, search_repositories + minimal_output=true."""
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
    # get_me вызван ровно один раз перед первым поиском, а не перед каждым
    assert [c[1] for c in calls] == ["get_me", "search_repositories", "search_repositories"]
    assert calls[1][2].get("minimal_output") is True
    assert calls[2][2].get("minimal_output") is True


def test_strict_mcp_uses_python_gateway_and_registry_connections(
    admin_client, sandbox, tmp_path, monkeypatch
):
    """MCP-only request still uses terminal syntax, but never enables shell."""
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
    command = (
        "python3 -m novamind_cli mcp call "
        "'{\"server_id\":\"github-real-id\",\"tool\":\"search_repositories\","
        "\"arguments\":{\"query\":\"flask\"}}'"
    )
    script(monkeypatch, [f"```terminal\n{command}\n```", "Нашёл demo/repo."])

    events = ndjson(admin_client.post("/api/agent/stream", json={
        "message": "используй только GitHub MCP и найди flask репозиторий",
    }))

    assert calls == [
        ("github-real-id", "get_me", {}),
        ("github-real-id", "search_repositories", {"query": "flask", "minimal_output": True}),
    ]
    tool = events_of(events, "tool")[0]
    assert tool["name"] == "mcp_call" and tool["command"] == command
    assert tool["status"] == "ok"
    assert answer_of(events) == "Нашёл demo/repo."
    assert any("shell запрещён" in event.get("text", "")
               for event in events_of(events, "step"))


def test_strict_mcp_gateway_refuses_ordinary_shell(admin_client, sandbox, monkeypatch):
    monkeypatch.setattr(mcp_registry, "connected_tools", lambda: [
        {"server_id": "github", "name": "search_repositories"},
    ])
    monkeypatch.setenv("AGENT_CONFIRM", "off")
    script(monkeypatch, ["```terminal\nuname -s\n```", "Shell не запускался."])
    events = ndjson(admin_client.post("/api/agent/stream", json={
        "message": "используй только GitHub MCP; запусти uname",
    }))
    tool = events_of(events, "tool")[0]
    assert tool["name"] == "terminal" and tool["status"] == "error"
    assert "shell-команды запрещены" in tool["output"]


def test_github_mcp_limits_search_calls_to_three_per_run(admin_client, sandbox, monkeypatch):
    """Не более 3 поисковых вызовов MCP за один запуск агента."""
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

    assert len(search_invocations) == 3
    tool_events = events_of(events, "tool")
    assert len(tool_events) == 4
    assert tool_events[3]["status"] == "error"
    assert "не более 3" in tool_events[3]["output"]
    assert answer_of(events) == "Отвечаю по трём выполненным поискам."


def test_mcp_registry_retries_on_403_and_429(tmp_path, monkeypatch):
    """При кодах 403 и 429 MCP делает паузу и повторяет запрос."""
    store = tmp_path / "mcp_connections.json"
    store.write_text(json.dumps({
        "github": {"name": "GitHub", "url": "https://mcp.example.test/mcp",
                   "auth": "bearer", "token": "secret", "connected": True, "initialized": True}
    }), encoding="utf-8")
    monkeypatch.setattr(mcp_registry, "STORE", store)
    monkeypatch.setenv("MCP_MAX_RETRIES", "2")
    monkeypatch.setenv("MCP_RETRY_DELAY", "0.25")

    slept: list[float] = []
    monkeypatch.setattr(mcp_registry.time, "sleep", lambda s: slept.append(s))

    responses = iter([
        _FakeResp(429, {"message": "Too Many Requests"}, headers={"Retry-After": "1"}),
        _FakeResp(403, {"message": "You have triggered a secondary rate limit"}),
        _FakeResp(200, {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": "ok"}]}}),
    ])
    monkeypatch.setattr(mcp_registry.requests, "post", lambda *a, **kw: next(responses))

    result = mcp_registry.call_tool("github", "search_repositories", {"query": "flask"})
    assert result["content"][0]["text"] == "ok"
    assert len(slept) == 2
    assert slept[0] == 1.0


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
