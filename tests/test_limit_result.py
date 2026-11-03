"""_limit_result: большой список MCP-инструментов должен остаться валидным JSON."""
import json

import tool_registry


def _big_listing(count=68):
    tools = [{"name": f"t{i}", "description": "d" * 300,
              "inputSchema": {"type": "object", "required": ["a"],
                              "properties": {f"p{j}": {"type": "string", "description": "x" * 200}
                                             for j in range(12)}}}
             for i in range(count)]
    return {"ok": True, "tool": "mcp:s:list", "result": {"tools": tools, "count": count}}


def test_large_tool_list_is_slimmed_not_truncated():
    value = _big_listing()
    assert len(json.dumps(value)) > tool_registry.MAX_RESULT_CHARS
    out = tool_registry._limit_result(value)
    assert out.get("slimmed") is True and not out.get("truncated")
    assert len(out["result"]["tools"]) == 68
    json.dumps(out)


def test_small_result_is_untouched():
    value = {"ok": True, "tool": "x", "result": "hi"}
    assert tool_registry._limit_result(value) == value
