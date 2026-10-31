from __future__ import annotations

import time

import mcp_registry
import skills_registry
import tool_registry


def test_unified_catalog_discovers_builtin_mcp_and_skill(monkeypatch):
    monkeypatch.setattr(mcp_registry, "connected_tools", lambda: [{
        "server_id": "demo", "server_name": "Demo MCP", "name": "lookup",
        "description": "Lookup", "inputSchema": {"type": "object"},
    }])
    monkeypatch.setattr("skills_registry.list_skills", lambda: [{
        "id": "review", "name": "Review", "description": "Review code",
        "enabled": True, "tools": ["file_read"],
    }])
    catalog = tool_registry.unified_catalog(workspace=True)
    ids = {item["id"] for item in catalog}
    assert "web_search" in ids
    assert "mcp:demo:lookup" in ids
    assert "skill:review" in ids


def test_unified_mcp_executes_real_registry_interface(monkeypatch):
    monkeypatch.setattr(mcp_registry, "call_tool",
                        lambda server_id, name, args: {"server": server_id, "name": name, "args": args})
    result = tool_registry.execute_unified("mcp:demo:lookup", {"q": "x"})
    assert result == {"ok": True, "tool": "mcp:demo:lookup",
                      "result": {"server": "demo", "name": "lookup", "args": {"q": "x"}}}


def test_skill_context_selects_relevant_and_validates_tools(tmp_path, monkeypatch):
    monkeypatch.setattr(skills_registry, "SKILLS_DIR", tmp_path / "skills")
    skills_registry.save_text("python-review", """---
name: Python Review
description: Проверка Python кода
tools: file_read, missing_tool
---
Проверь Python код и тесты.
""")
    skills_registry.save_text("travel", """---
name: Travel
description: Планирование путешествий
---
Составь маршрут поездки.
""")
    context = skills_registry.prompt_context("проверь Python код")
    assert "Python Review" in context and "Travel" not in context
    validation = skills_registry.validate_skill("python-review")
    assert validation["ok"] is False and validation["missing_tools"] == ["missing_tool"]


def test_parallel_executor_preserves_order(monkeypatch):
    def fake(tool_id, args):
        if args.get("wait"):
            time.sleep(.02)
        return {"ok": True, "tool": tool_id, "value": args["value"]}
    monkeypatch.setattr(tool_registry, "execute_unified", fake)
    monkeypatch.setattr(tool_registry, "unified_catalog", lambda **_k: [
        {"id": "a", "parallel_safe": True, "conflict_key": "a"},
        {"id": "b", "parallel_safe": True, "conflict_key": "b"},
    ])
    results = tool_registry.execute_parallel([
        {"tool": "a", "arguments": {"value": 1, "wait": True}},
        {"tool": "b", "arguments": {"value": 2}},
    ])
    assert [item["value"] for item in results] == [1, 2]
