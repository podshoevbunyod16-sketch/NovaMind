import json
import pytest

from routes.terminal import _python_command, _run_python_gateway


@pytest.mark.parametrize(("command", "expected"), [
    (["python3", "-m", "novamind_cli", "tools"], ("__tools__", "{}")),
    (["python", "-m", "novamind_cli", "mcp", "tools"], ("__mcp_tools__", "{}")),
    (["python3", "-m", "novamind_cli", "call", "web_search", '{"query":"weather"}'],
     ("web_search", '{"query":"weather"}')),
    (["python", "-m", "novamind_cli", "mcp", "call", '{"server_id":"github","tool":"search"}'],
     ("mcp_call", '{"server_id":"github","tool":"search"}')),
])
def test_native_cli_parser_accepts_only_registered_tool_calls(command, expected):
    target, body = _python_command(command)
    assert target == expected[0]
    assert json.loads(body) == json.loads(expected[1])


@pytest.mark.parametrize(("command", "message"), [
    (["python3", "-m", "novamind_cli", "call", "shell", "[]"], "JSON-объект"),
    (["python3", "-m", "novamind_cli", "call", "web_search", "{"], "invalid JSON"),
    (["python3", "-m", "novamind_cli", "mcp", "call", "[]"], "JSON-объект"),
    (["python3", "-m", "novamind_cli", "exec", "rm", "-rf"], "Ожидается"),
])
def test_native_cli_parser_rejects_malformed_or_arbitrary_commands(command, message):
    with pytest.raises(ValueError, match=message):
        _python_command(command)


def test_regular_python_script_is_not_intercepted():
    assert _python_command(["python3", "report.py"]) is None
    assert _python_command(["python3", "-c", "print(1)"]) is None
    assert _python_command(["python3", "-m", "other_cli", "call", "web_search", "{}"] ) is None


def test_gateway_routes_validated_arguments_to_unified_tool_registry(monkeypatch):
    import tool_registry
    monkeypatch.setattr(tool_registry, "bootstrap_default_tools", lambda: None)
    monkeypatch.setattr(tool_registry, "_workspace_guard", lambda: None)
    seen = []
    monkeypatch.setattr(tool_registry, "call_tool", lambda name, **kwargs:
                        seen.append((name, kwargs)) or {"ok": True, "result": "done"})
    result = _run_python_gateway(["python3", "-m", "novamind_cli", "call",
                                  "web_search", '{"query":"Khujand"}'])
    assert result["exit_code"] == 0
    assert json.loads(result["stdout"]) == {"ok": True, "result": "done"}
    assert seen == [("web_search", {"query": "Khujand"})]


def test_gateway_produces_error_without_executing_malformed_tool_call(monkeypatch):
    import tool_registry
    calls = []
    monkeypatch.setattr(tool_registry, "bootstrap_default_tools", lambda: None)
    monkeypatch.setattr(tool_registry, "call_tool", lambda *a, **kw: calls.append((a, kw)))
    result = _run_python_gateway(["python3", "-m", "novamind_cli", "call", "web_search", "[]"])
    assert result["exit_code"] == 2
    assert "JSON-объект" in result["stderr"]
    assert calls == []


def test_gateway_refuses_workspace_tool_when_access_check_fails(monkeypatch):
    import tool_registry
    tool_registry.bootstrap_default_tools()
    denied = {"ok": False, "error": {"type": "unauthorized", "message": "login required"}}
    monkeypatch.setattr(tool_registry, "_workspace_guard", lambda: denied)
    result = _run_python_gateway(["python3", "-m", "novamind_cli", "call",
                                  "file_read", '{"path":"secret.txt"}'])
    assert result["exit_code"] == 1
    assert "unauthorized" in result["stderr"]


def test_gateway_tool_failure_keeps_structured_failure(monkeypatch):
    import tool_registry
    monkeypatch.setattr(tool_registry, "bootstrap_default_tools", lambda: None)
    monkeypatch.setattr(tool_registry, "_workspace_guard", lambda: None)
    monkeypatch.setattr(tool_registry, "call_tool", lambda *_a, **_kw:
                        {"ok": False, "error": {"type": "upstream", "message": "offline"}})
    result = _run_python_gateway(["python3", "-m", "novamind_cli", "call",
                                  "web_search", '{"query":"x"}'])
    assert result["exit_code"] == 1
    assert json.loads(result["stderr"])["error"]["type"] == "upstream"


def test_tools_catalog_uses_the_single_registry(monkeypatch):
    import tool_registry
    monkeypatch.setattr(tool_registry, "bootstrap_default_tools", lambda: None)
    monkeypatch.setattr(tool_registry, "tool_schemas", lambda: [{"name": "web_search"}])
    result = _run_python_gateway(["python3", "-m", "novamind_cli", "tools"])
    assert result["exit_code"] == 0
    assert json.loads(result["stdout"]) == {"web_search": ""}


def test_mcp_catalog_is_read_only_and_request_local(monkeypatch):
    import mcp_registry
    import tool_registry
    monkeypatch.setattr(tool_registry, "bootstrap_default_tools", lambda: None)
    monkeypatch.setattr(mcp_registry, "connected_tools", lambda: [{"server_id": "demo", "name": "search"}])
    result = _run_python_gateway(["python3", "-m", "novamind_cli", "mcp", "tools"])
    assert result["exit_code"] == 0
    assert json.loads(result["stdout"]) == [{"server_id": "demo", "name": "search"}]


@pytest.mark.parametrize(("alias", "canonical"), [
    ("web_fetch", "web_fetch"), ("file_list", "file_list"),
    ("bash", "bash"), ("python", "python"), ("image_generate", "image_generate"),
])
def test_gateway_preserves_registry_alias_names(alias, canonical):
    target, body = _python_command(["python3", "-m", "novamind_cli", "call", alias, "{}"])
    assert target == canonical
    assert json.loads(body) == {}
