from __future__ import annotations

import json
import queue
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import mcp_registry
import mcp_transports
from tests.agent_helpers import answer_of, events_of, ndjson, script as model_script


def _stdio_server(path):
    path.write_text('''import json, sys
for line in sys.stdin:
    req=json.loads(line); method=req.get("method"); rid=req.get("id")
    if method=="initialize": result={"protocolVersion":"2025-06-18","capabilities":{"tools":{}},"serverInfo":{"name":"stdio-test","version":"1"}}
    elif method=="tools/list": result={"tools":[{"name":"echo","description":"echo","inputSchema":{"type":"object"}},{"name":"verify","description":"verify","inputSchema":{"type":"object"}}]}
    elif method=="tools/call": result={"content":[{"type":"text","text":str(req.get("params",{}).get("name","tool")).upper()+":"+str(req.get("params",{}).get("arguments",{}).get("text",""))}]}
    else: result={}
    if rid is not None:
        print(json.dumps({"jsonrpc":"2.0","id":rid,"result":result}), flush=True)
''', encoding="utf-8")


def test_stdio_full_lifecycle_and_persistence(tmp_path, monkeypatch):
    store = tmp_path / "connections.json"
    script = tmp_path / "server.py"
    _stdio_server(script)
    monkeypatch.setattr(mcp_registry, "STORE", store)
    monkeypatch.setenv("MCP_STDIO_ENABLED", "1")
    monkeypatch.setenv("MCP_STDIO_COMMANDS", "python")

    server_id = mcp_registry.save_connection({
        "name": "STDIO test", "transport": "stdio", "command": sys.executable,
        "args": [str(script)], "auth": "none",
    })
    checked = mcp_registry.test_connection(server_id)
    assert checked["connected"] is True and checked["tools"] == ["echo", "verify"]
    assert mcp_registry.public_connections()[0]["transport"] == "stdio"
    assert [item["name"] for item in mcp_registry.connected_tools()] == ["echo", "verify"]
    result = mcp_registry.call_tool(server_id, "echo", {"text": "works"})
    assert result["content"][0]["text"] == "ECHO:works"

    mcp_registry.disconnect_connection(server_id)
    assert mcp_registry.public_connections()[0]["connected"] is False
    mcp_registry.remove_connection(server_id)
    assert mcp_registry.public_connections() == []


def test_real_stdio_agent_two_tools_observation_and_verification(
    admin_client, sandbox, tmp_path, monkeypatch
):
    store = tmp_path / "agent-connections.json"
    server_script = tmp_path / "agent-server.py"
    _stdio_server(server_script)
    monkeypatch.setattr(mcp_registry, "STORE", store)
    monkeypatch.setenv("MCP_STDIO_ENABLED", "1")
    monkeypatch.setenv("MCP_STDIO_COMMANDS", "python")
    monkeypatch.setenv("AGENT_CONFIRM", "off")
    server_id = mcp_registry.save_connection({
        "name": "Agent STDIO", "transport": "stdio", "command": sys.executable,
        "args": [str(server_script)], "auth": "none",
    })
    assert mcp_registry.test_connection(server_id)["connected"] is True
    model_script(monkeypatch, [
        f'```mcp_call\n{{"server_id":"{server_id}","tool":"echo","arguments":{{"text":"data"}}}}\n```',
        f'```mcp_call\n{{"server_id":"{server_id}","tool":"verify","arguments":{{"text":"ECHO:data"}}}}\n```',
        "Данные получены и проверены.",
    ])
    try:
        events = ndjson(admin_client.post("/api/agent/stream", json={
            "message": "Используй MCP: получи данные, затем проверь их",
            "tools": False, "search": False,
        }))
        tool_events = events_of(events, "tool")
        assert [event["name"] for event in tool_events] == ["mcp_call", "mcp_call"]
        assert "ECHO:data" in tool_events[0]["output"]
        assert "VERIFY:ECHO:data" in tool_events[1]["output"]
        assert len(events_of(events, "observation")) == 2
        assert events_of(events, "verification_completed")[-1]["ok"] is True
        assert events_of(events, "task_completed")[-1]["verified"] is True
        assert answer_of(events) == "Данные получены и проверены."
    finally:
        mcp_transports.close_all()


class _SseHandler(BaseHTTPRequestHandler):
    messages: "queue.Queue[dict]" = queue.Queue()

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        self.wfile.write(b"event: endpoint\ndata: /messages\n\n"); self.wfile.flush()
        while True:
            try:
                value = self.messages.get(timeout=2)
            except queue.Empty:
                return
            self.wfile.write(("event: message\ndata: " + json.dumps(value) + "\n\n").encode())
            self.wfile.flush()

    def do_POST(self):
        size = int(self.headers.get("content-length", "0"))
        req = json.loads(self.rfile.read(size) or b"{}")
        method = req.get("method")
        if method == "initialize":
            result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}}
        elif method == "tools/list":
            result = {"tools": [{"name": "ping", "inputSchema": {"type": "object"}}]}
        elif method == "tools/call":
            result = {"content": [{"type": "text", "text": "pong"}]}
        else:
            result = {}
        if req.get("id") is not None:
            self.messages.put({"jsonrpc": "2.0", "id": req["id"], "result": result})
        self.send_response(202); self.end_headers()

    def log_message(self, *_args):
        pass


def test_legacy_sse_full_lifecycle(tmp_path, monkeypatch):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _SseHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    monkeypatch.setattr(mcp_registry, "STORE", tmp_path / "sse.json")
    monkeypatch.setattr(mcp_registry, "TIMEOUT", 3.0)
    try:
        server_id = mcp_registry.save_connection({
            "name": "SSE test", "transport": "sse",
            "url": f"http://127.0.0.1:{server.server_port}/sse", "auth": "none",
        })
        checked = mcp_registry.test_connection(server_id)
        assert checked["connected"] is True and checked["tools"] == ["ping"]
        assert mcp_registry.call_tool(server_id, "ping", {})["content"][0]["text"] == "pong"
    finally:
        mcp_transports.close_all()
        server.shutdown(); server.server_close(); thread.join(timeout=2)
