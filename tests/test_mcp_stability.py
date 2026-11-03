from __future__ import annotations

import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

import mcp_registry as mcp


@pytest.fixture(autouse=True)
def close_stdio_sessions():
    yield
    mcp._close_stdio_sessions()


class Response:
    def __init__(self, status=200, body=None, headers=None, text=None):
        self.status_code = status
        self.headers = {"Content-Type": "application/json", **(headers or {})}
        self._body = body
        self.text = text if text is not None else (json.dumps(body) if body is not None else "")

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body

    def close(self):
        pass


def store_server(monkeypatch, tmp_path, sid="server", **fields):
    path = tmp_path / "mcp.json"
    default = {"name": sid, "url": "https://mcp.test/mcp", "transport": "streamable_http",
               "auth": "none", "connected": True, "initialized": True}
    default.update(fields)
    path.write_text(json.dumps({sid: default}), encoding="utf-8")
    monkeypatch.setattr(mcp, "STORE", path)
    mcp._TOOL_CACHE.clear()
    mcp._FAILURE_UNTIL.clear()
    return path


def test_streamable_http_discovery_and_request_metadata(monkeypatch, tmp_path):
    store_server(monkeypatch, tmp_path, connected=False, initialized=False,
                 auth="bearer", token="do-not-log")
    observed = []

    def post(url, json=None, headers=None, **kwargs):
        observed.append((json, dict(headers or {}), kwargs))
        method = json["method"]
        if method == "server/discover":
            return Response(body={"jsonrpc": "2.0", "id": json["id"], "result": {
                "resultType": "complete", "supportedVersions": ["2026-07-28"],
                "capabilities": {"tools": {}}, "_meta": {"io.modelcontextprotocol/serverInfo": {"name": "mock"}}}})
        if method == "tools/list":
            return Response(body={"jsonrpc": "2.0", "id": json["id"], "result": {"tools": [
                {"name": "lookup", "description": "Read a record", "inputSchema": {
                    "type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"]},
                    "annotations": {"readOnlyHint": True}}
            ]}})
        return Response(202, {}, {"Content-Type": "application/json"}, "")

    monkeypatch.setattr(mcp.requests, "post", post)
    result = mcp.test_connection("server")
    assert result["connected"] is True and result["tools"] == 1
    assert [call[0]["method"] for call in observed] == ["server/discover", "tools/list"]
    assert observed[0][1]["Authorization"] == "Bearer do-not-log"
    assert observed[0][1]["MCP-Protocol-Version"] == "2026-07-28"
    assert observed[0][1]["Mcp-Method"] == "server/discover"
    assert observed[0][0]["params"]["_meta"]["io.modelcontextprotocol/protocolVersion"] == "2026-07-28"
    assert observed[1][1]["Mcp-Method"] == "tools/list"
    assert mcp.public_connections()[0]["connected"] is True


def test_call_validates_schema_and_mirrors_safe_custom_headers(monkeypatch, tmp_path):
    store_server(monkeypatch, tmp_path, protocol_mode="modern", protocol_version="2026-07-28")
    schema = {"type": "object", "properties": {
        "team": {"type": "string", "x-mcp-header": "Team"},
        "name": {"type": "string"}}, "required": ["team", "name"]}
    mcp._TOOL_CACHE["server"] = (time.monotonic() + 60, [{"name": "lookup", "inputSchema": schema,
        "annotations": {"readOnlyHint": True}}])
    calls = []

    def post(_url, json=None, headers=None, **_kwargs):
        calls.append((json, dict(headers or {})))
        return Response(body={"jsonrpc": "2.0", "id": json["id"], "result": {
            "content": [{"type": "text", "text": "found"}], "structuredContent": {"id": 7}, "isError": False}})

    monkeypatch.setattr(mcp.requests, "post", post)
    with pytest.raises(ValueError, match="обязательный аргумент"):
        mcp.call_tool("server", "lookup", {"team": "ops"})
    assert not calls
    result = mcp.call_tool("server", "lookup", {"team": "ops", "name": "hello"})
    assert result["structuredContent"] == {"id": 7}
    body, headers = calls[0]
    assert body["params"]["arguments"] == {"team": "ops", "name": "hello"}
    assert headers["Mcp-Method"] == "tools/call" and headers["Mcp-Name"] == "lookup"
    assert headers["Mcp-Param-Team"] == "ops"
    assert body["params"]["_meta"]["io.modelcontextprotocol/protocolVersion"] == "2026-07-28"


def test_streamable_http_falls_back_to_legacy_initialize(monkeypatch, tmp_path):
    store_server(monkeypatch, tmp_path, connected=False, initialized=False)
    observed = []
    def post(_url, json=None, headers=None, **_kwargs):
        observed.append((json, dict(headers or {})))
        if json["method"] == "server/discover":
            return Response(404, {"jsonrpc": "2.0", "id": json["id"], "error": {"code": -32601, "message": "Method not found"}})
        if json["method"] == "initialize":
            return Response(body={"jsonrpc": "2.0", "id": json["id"], "result": {
                "protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "legacy"}}})
        if json["method"] == "tools/list":
            return Response(body={"jsonrpc": "2.0", "id": json["id"], "result": {"tools": []}})
        return Response(202, {}, {"Content-Type": "application/json"}, "")
    monkeypatch.setattr(mcp.requests, "post", post)
    assert mcp.test_connection("server")["connected"] is True
    assert mcp._connection("server")["protocol_mode"] == "legacy"
    assert [item[0]["method"] for item in observed] == ["server/discover", "initialize", "notifications/initialized", "tools/list"]
    assert observed[1][1]["MCP-Protocol-Version"] == "2025-06-18"
    assert "Mcp-Method" not in observed[1][1]


def test_http_sse_uses_live_event_stream_and_reinitializes_per_exchange(monkeypatch, tmp_path):
    store_server(monkeypatch, tmp_path, transport="http_sse", connected=False, initialized=False)
    get_count = []

    class SSE(Response):
        def __init__(self, posted):
            super().__init__(200, None, {"Content-Type": "text/event-stream"}, "")
            self.posted = posted

        def iter_lines(self, decode_unicode=True):
            yield "event: endpoint"
            yield "data: /messages?session=x"
            yield ""
            index = 0
            while index < len(self.posted):
                message = self.posted[index]
                index += 1
                if message.get("id") is None:
                    continue
                method = message["method"]
                if method == "initialize":
                    body = {"jsonrpc": "2.0", "id": message["id"], "result": {
                        "protocolVersion": "2024-11-05", "capabilities": {}, "serverInfo": {"name": "legacy"}}}
                elif method == "tools/list":
                    body = {"jsonrpc": "2.0", "id": message["id"], "result": {"tools": [{
                        "name": "legacy_read", "inputSchema": {"type": "object"}, "annotations": {"readOnlyHint": True}}]}}
                else:
                    body = {"jsonrpc": "2.0", "id": message["id"], "result": {"content": [{"type": "text", "text": "ok"}]}}
                yield "event: message"
                yield "data: " + json.dumps(body)
                yield ""

    posted = []
    def get(_url, **_kwargs):
        get_count.append(1)
        return SSE(posted)
    def post(_url, json=None, **_kwargs):
        posted.append(json)
        return Response(202, {}, {"Content-Type": "text/plain"}, "")
    monkeypatch.setattr(mcp.requests, "get", get)
    monkeypatch.setattr(mcp.requests, "post", post)

    assert mcp.test_connection("server")["connected"] is True
    assert len(get_count) == 2
    assert [message["method"] for message in posted] == ["initialize", "initialize", "notifications/initialized", "tools/list"]


def test_stdio_transport_legacy_framing_and_secret_isolation(monkeypatch, tmp_path):
    server_script = r'''import sys,json
count=0
init_notifications=0
while True:
    headers={}
    while True:
        line=sys.stdin.buffer.readline()
        if not line: sys.exit(0)
        if line in (b"\r\n",b"\n"): break
        key,value=line.decode().split(":",1); headers[key.lower()]=value.strip()
    raw=sys.stdin.buffer.read(int(headers["content-length"]))
    msg=json.loads(raw)
    if msg.get("method") == "notifications/initialized":
        init_notifications+=1
        continue
    if "id" not in msg: continue
    if msg["method"] == "initialize":
        result={"protocolVersion":"2025-06-18","capabilities":{"tools":{}},"serverInfo":{"name":"stdio-mock"}}
    elif msg["method"] == "tools/list":
        result={"tools":[{"name":"echo","inputSchema":{"type":"object","properties":{"text":{"type":"string"}},"required":["text"]},"annotations":{"readOnlyHint":True},"observed_init_notifications":init_notifications}]}
    elif msg["method"] == "tools/call":
        count+=1
        result={"content":[{"type":"text","text":str(count)+":"+msg["params"]["arguments"]["text"]}],"isError":False}
    else: result={}
    body=json.dumps({"jsonrpc":"2.0","id":msg["id"],"result":result}).encode()
    sys.stdout.buffer.write(("Content-Length: %d\r\n\r\n"%len(body)).encode()+body); sys.stdout.buffer.flush()
'''
    store_server(monkeypatch, tmp_path, transport="stdio", url="", command=sys.executable,
                 args=["-c", server_script], env={"MCP_TEST_SECRET": "stdio-secret"}, auth="env",
                 connected=False, initialized=False)
    monkeypatch.setenv("APP_PRIVATE_SECRET", "must-not-be-inherited")
    env = mcp._stdio_env(mcp._connection("server"))
    assert env["MCP_TEST_SECRET"] == "stdio-secret"
    assert "APP_PRIVATE_SECRET" not in env
    tools = mcp.list_tools("server", force=True)
    assert tools[0]["name"] == "echo"
    assert tools[0]["observed_init_notifications"] == 1
    result = mcp.call_tool("server", "echo", {"text": "hello"})
    again = mcp.call_tool("server", "echo", {"text": "world"})
    assert result["content"][0]["text"] == "1:hello"
    assert again["content"][0]["text"] == "2:world"
    assert len(mcp._STDIO_SESSIONS) == 1


def test_modern_stdio_uses_newline_json_rpc_without_initialize(monkeypatch, tmp_path):
    server_script = r'''import sys,json
count=0
for line in sys.stdin:
    msg=json.loads(line)
    method=msg["method"]
    if method == "server/discover":
        result={"resultType":"complete","supportedVersions":["2026-07-28"],"capabilities":{"tools":{}},"_meta":{"io.modelcontextprotocol/serverInfo":{"name":"modern-stdio"}}}
    elif method == "tools/list": result={"tools":[{"name":"modern_tool","inputSchema":{"type":"object"},"annotations":{"readOnlyHint":True}}]}
    else:
        count+=1
        result={"content":[{"type":"text","text":str(count)}]}
    body={"jsonrpc":"2.0","id":msg["id"],"result":result}
    sys.stdout.write(json.dumps(body)+"\n"); sys.stdout.flush()
'''
    store_server(monkeypatch, tmp_path, transport="stdio", url="", command=sys.executable,
                 args=["-c", server_script], connected=False, initialized=False)
    tools = mcp.list_tools("server", force=True)
    assert tools[0]["name"] == "modern_tool"
    item = mcp._connection("server")
    assert item["protocol_mode"] == "modern" and item["protocol_version"] == "2026-07-28"
    first = mcp.call_tool("server", "modern_tool", {})
    second = mcp.call_tool("server", "modern_tool", {})
    assert first["content"][0]["text"] == "1"
    assert second["content"][0]["text"] == "2"
    assert len(mcp._STDIO_SESSIONS) == 1


def test_discovery_isolates_offline_server_and_parallel_calls_overlap(monkeypatch, tmp_path):
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({
        "bad": {"name": "bad", "url": "https://bad.test/mcp", "transport": "streamable_http", "auth": "none", "connected": False},
        "one": {"name": "one", "url": "https://one.test/mcp", "transport": "streamable_http", "auth": "none", "connected": True, "initialized": True},
        "two": {"name": "two", "url": "https://two.test/mcp", "transport": "streamable_http", "auth": "none", "connected": True, "initialized": True},
    }), encoding="utf-8")
    monkeypatch.setattr(mcp, "STORE", path)
    monkeypatch.setenv("MCP_MAX_RETRIES", "0")
    mcp._TOOL_CACHE.clear(); mcp._FAILURE_UNTIL.clear()
    for sid, name in (("one", "read_one"), ("two", "read_two")):
        mcp._TOOL_CACHE[sid] = (time.monotonic() + 60, [{"name": name, "inputSchema": {"type": "object"}, "annotations": {"readOnlyHint": True}}])
    def post(url, json=None, **_kwargs):
        if "bad.test" in url: raise mcp.requests.ConnectionError("connect fail")
        time.sleep(.15)
        return Response(body={"jsonrpc": "2.0", "id": json["id"], "result": {"content": [{"type": "text", "text": "ok"}]}})
    monkeypatch.setattr(mcp.requests, "post", post)
    tools = mcp.connected_tools()
    assert {x["server_id"] for x in tools} == {"one", "two"}
    active = 0; peak = 0; guard = threading.Lock(); entered = threading.Barrier(2)
    def concurrent_post(url, json=None, **_kwargs):
        nonlocal active, peak
        with guard: active += 1; peak = max(peak, active)
        entered.wait(timeout=2)
        time.sleep(.03)
        with guard: active -= 1
        return Response(body={"jsonrpc": "2.0", "id": json["id"], "result": {"content": [{"type": "text", "text": "ok"}]}})
    monkeypatch.setattr(mcp.requests, "post", concurrent_post)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda pair: mcp.call_tool(*pair), [("one", "read_one", {}), ("two", "read_two", {})]))
    assert len(results) == 2 and peak == 2


def test_server_names_and_tool_catalog_support_pagination_and_secret_redaction(tmp_path, monkeypatch):
    store_server(monkeypatch, tmp_path, url="https://user:pass@mcp.test/mcp?access_token=very-secret")
    assert "user:pass" not in mcp.public_connections()[0]["url"]
    assert "very-secret" not in mcp.public_connections()[0]["url"]
    tools = [{"name": f"tool_{i}", "inputSchema": {"type": "object"}} for i in range(7)]
    mcp._TOOL_CACHE["server"] = (time.monotonic() + 60, tools)
    page = mcp.tools_page("server", offset=3, limit=2)
    assert [item["name"] for item in page["tools"]] == ["tool_3", "tool_4"]
    assert page["next_offset"] == 5


def test_react_runs_independent_mcp_calls_concurrently_and_returns_results(monkeypatch, tmp_path):
    from agent.loop import AgentContext, run_react
    from tool_registry import ToolSpec

    servers = {sid: {"name": sid, "url": f"https://{sid}.test/mcp", "transport": "streamable_http"}
               for sid in ("one", "two")}
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps(servers), encoding="utf-8")
    monkeypatch.setattr(mcp, "STORE", path)
    monkeypatch.setattr(mcp, "_TOOL_CACHE", {
        sid: (time.monotonic() + 60, [{"name": "read", "annotations": {"readOnlyHint": True}}])
        for sid in ("one", "two")})

    barrier = threading.Barrier(2)
    lock = threading.Lock()
    active = 0
    peak = 0

    def call_tool(name, server_id, tool, arguments=None):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        barrier.wait(timeout=2)
        with lock:
            active -= 1
        return {"ok": True, "result": {"server": server_id, "tool": tool, "value": (arguments or {}).get("key")}}

    turns = [0]
    def complete(*_args, **_kwargs):
        turns[0] += 1
        if turns[0] == 1:
            return "", {"tool_calls": [
                {"function": {"name": "mcp_call", "arguments": {"server_id": "one", "tool": "read", "arguments": {"key": "a"}}}},
                {"function": {"name": "mcp_call", "arguments": {"server_id": "two", "tool": "read", "arguments": {"key": "b"}}}},
            ]}
        return "Оба MCP-сервера вернули результаты.", {"model": "mock"}

    schema = {"type": "object", "properties": {"server_id": {"type": "string"}, "tool": {"type": "string"},
              "arguments": {"type": "object"}}, "required": ["server_id", "tool"]}
    spec = ToolSpec("mcp_call", "Read-only MCP call", schema, lambda **_kw: {"ok": True}, risk="safe")
    events = []
    result = run_react(AgentContext(goal="Read values from two MCP servers", complete=complete,
                                    emit=events.append, tools=[spec], call_tool=call_tool,
                                    max_steps=8, timeout=5, max_tokens=1000))
    assert result.answer == "Оба MCP-сервера вернули результаты."
    assert peak == 2
    observations = [event for event in events if event.get("type") == "tool"]
    assert len(observations) == 2 and all(event["status"] == "ok" for event in observations)


def test_mcp_search_read_opt_in_checks_tool_annotation_before_call(monkeypatch):
    from flask import session
    from app import app
    import routes.search as search
    calls = []
    monkeypatch.setenv("MCP_ALLOW_USER_READ", "1")
    monkeypatch.setattr(mcp, "cached_tool_annotations", lambda _sid, _tool: {"idempotentHint": True})
    monkeypatch.setattr(mcp, "call_tool", lambda *args, **kwargs: calls.append(args) or {"results": []})
    tool = {"server_id": "search", "name": "search_web"}
    with app.test_request_context("/"):
        session["nova_user_email"] = "user@example.test"
        session["nova_google_login"] = True
        results, error = search.search_web_exa_mcp("hello", tool=tool)
    assert results == [] and "read-only" in error
    assert calls == []


def test_non_admin_read_opt_in_cannot_call_unannotated_or_write_mcp_tools(monkeypatch):
    from flask import session
    from app import app
    import tool_registry
    tool_registry.bootstrap_default_tools()
    spec = tool_registry.get_tool("mcp_call")
    calls = []
    monkeypatch.setenv("MCP_ALLOW_USER_READ", "1")
    monkeypatch.setattr(mcp, "cached_tool_annotations", lambda _sid, tool:
        {"readOnlyHint": True} if tool == "read" else {"idempotentHint": True})
    monkeypatch.setattr(mcp, "call_tool", lambda *args, **kwargs: calls.append((args, kwargs)) or {"content": []})
    with app.test_request_context("/"):
        session["nova_user_email"] = "user@example.test"
        session["nova_google_login"] = True
        allowed = spec.execute(server_id="server", tool="read", arguments={})
        rejected = spec.execute(server_id="server", tool="write", arguments={})
    assert allowed["ok"] is True
    assert rejected["ok"] is False and rejected["error"]["type"] == "permission_denied"
    assert len(calls) == 1


def test_cancelled_react_run_does_not_prelaunch_parallel_mcp_calls():
    from agent.loop import AgentContext, run_react
    from tool_registry import ToolSpec
    cancel = threading.Event()
    cancel.set()
    executed = []
    def complete(*_args, **_kwargs):
        return "", {"tool_calls": [
            {"function": {"name": "mcp_call", "arguments": {"server_id": "one", "tool": "read", "arguments": {}}}},
            {"function": {"name": "mcp_call", "arguments": {"server_id": "two", "tool": "read", "arguments": {}}}},
        ]}
    spec = ToolSpec("mcp_call", "Read-only MCP call", {"type": "object", "properties": {
        "server_id": {"type": "string"}, "tool": {"type": "string"}, "arguments": {"type": "object"}},
        "required": ["server_id", "tool"]}, lambda **_kw: executed.append(1) or {"ok": True}, risk="safe")
    result = run_react(AgentContext(goal="Read two MCP records", complete=complete, tools=[spec],
                                    call_tool=lambda *a, **kw: executed.append(1) or {"ok": True},
                                    cancel=cancel, max_steps=4, timeout=4))
    assert result.status == "cancelled"
    assert executed == []


def test_mcp_risk_policy_trusts_readonly_annotation_and_fails_closed(monkeypatch, tmp_path):
    import agent.policy as policy
    store_server(monkeypatch, tmp_path)
    payload = json.dumps({"server_id": "server", "tool": "mystery_action"})
    mcp._TOOL_CACHE["server"] = (time.monotonic() + 60, [{"name": "mystery_action",
        "annotations": {"readOnlyHint": True}}])
    assert policy.risk_of("mcp_call", payload) == "safe"
    mcp._TOOL_CACHE["server"] = (time.monotonic() + 60, [{"name": "mystery_action",
        "annotations": {"idempotentHint": True}}])
    assert policy.risk_of("mcp_call", payload) == "danger"
    mcp._TOOL_CACHE.clear()
    assert policy.risk_of("mcp_call", payload) == "danger"


def test_auth_forbidden_is_not_retried_even_for_idempotent_discovery(monkeypatch, tmp_path):
    store_server(monkeypatch, tmp_path)
    monkeypatch.setenv("MCP_MAX_RETRIES", "4")
    calls = []
    def post(*_args, **_kwargs):
        calls.append(1)
        return Response(403, {"message": "Forbidden: invalid credentials"})
    monkeypatch.setattr(mcp.requests, "post", post)
    with pytest.raises(mcp.MCPError, match="403"):
        mcp.request("server", "tools/list")
    assert len(calls) == 1


def test_mcp_failure_cannot_become_an_unverified_success_claim(monkeypatch, tmp_path):
    from agent.loop import AgentContext, run_react
    from tool_registry import ToolSpec

    store_server(monkeypatch, tmp_path, sid="notes")
    monkeypatch.setattr(mcp, "_TOOL_CACHE", {"notes": (time.monotonic() + 60,
        [{"name": "read", "annotations": {"readOnlyHint": True}}])})
    turns = [0]
    def complete(*_args, **_kwargs):
        turns[0] += 1
        if turns[0] == 1:
            return "", {"tool_calls": [{"function": {"name": "mcp_call", "arguments": {
                "server_id": "notes", "tool": "read", "arguments": {}}}}]}
        return "Проверено успешно, всё выполнено.", {"model": "mock"}
    def failed_call(*_args, **_kwargs):
        return {"ok": False, "error": {"type": "MCPError", "message": "MCP HTTP 503: unavailable"}}
    spec = ToolSpec("mcp_call", "MCP call", {"type": "object", "properties": {
        "server_id": {"type": "string"}, "tool": {"type": "string"}, "arguments": {"type": "object"}},
        "required": ["server_id", "tool"]}, lambda **_kwargs: {"ok": True}, risk="safe")
    result = run_react(AgentContext(goal="проверь данные в MCP", complete=complete, tools=[spec],
                                    call_tool=failed_call, max_steps=4, timeout=4))
    assert result.observations[0]["result"]["ok"] is False
    assert result.answer.startswith("Не могу честно подтвердить завершение задачи")


def test_react_stops_repeated_mcp_failures_and_requests_final_answer(monkeypatch, tmp_path):
    from agent.loop import AgentContext, run_react
    from tool_registry import ToolSpec

    store_server(monkeypatch, tmp_path, sid="wiki")
    monkeypatch.setattr(mcp, "_TOOL_CACHE", {"wiki": (time.monotonic() + 60,
        [{"name": "read_wiki_structure", "annotations": {"readOnlyHint": True}}])})
    calls = []
    tool_definitions = []
    def complete(_messages, **kwargs):
        tool_definitions.append(kwargs.get("tools", "missing"))
        if len(tool_definitions) <= 3:
            return "", {"tool_calls": [{"function": {"name": "mcp_call", "arguments": {
                "server_id": "wiki", "tool": "read_wiki_structure", "arguments": {"repo": "owner/repo"}}}}]}
        return "DeepWiki сообщает, что репозиторий не проиндексирован; повторять запрос не следует.", {"model": "mock"}

    def failed_call(*_args, **_kwargs):
        calls.append(1)
        return {"ok": False, "error": {"type": "MCPError", "message": "Repository not indexed"}}

    spec = ToolSpec("mcp_call", "MCP call", {"type": "object", "properties": {
        "server_id": {"type": "string"}, "tool": {"type": "string"}, "arguments": {"type": "object"}},
        "required": ["server_id", "tool"]}, lambda **_kwargs: {"ok": True}, risk="safe")
    result = run_react(AgentContext(goal="Изучи структуру репозитория", complete=complete, tools=[spec],
                                    call_tool=failed_call, max_steps=8, timeout=4))

    assert len(tool_definitions) == 4
    assert all(definitions for definitions in tool_definitions[:3])
    assert tool_definitions[3] is None
    assert len(calls) == 1  # repeated identical calls are blocked by the loop
    assert len(result.observations) == 3
    assert result.observations[0]["result"]["ok"] is False
    assert all(item["result"]["error"]["type"] == "repeat_call"
               for item in result.observations[1:])
    assert "индекс" in result.answer.lower() or "заверш" in result.answer.lower()


def test_settings_tools_endpoint_requires_admin_before_stdio_discovery(client, sandbox, monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("unauthenticated settings request must not start MCP discovery")
    monkeypatch.setattr(mcp, "list_tools", forbidden)
    response = client.get("/api/settings/mcp/local/tools")
    assert response.status_code == 403


def test_mcp_connection_store_reads_legacy_root_file(tmp_path, monkeypatch):
    legacy = tmp_path / "mcp_connections.json"
    legacy.write_text(json.dumps({"existing": {"name": "existing server", "url": "https://mcp.test"}}),
                      encoding="utf-8")
    monkeypatch.setattr(mcp, "ROOT", tmp_path)
    monkeypatch.setattr(mcp, "LEGACY_STORE", legacy)
    monkeypatch.setattr(mcp, "STORE", tmp_path / "new-location.json")
    assert mcp._read()["existing"]["name"] == "existing server"
