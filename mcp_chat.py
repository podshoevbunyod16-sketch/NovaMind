"""MCP-aware completion helpers shared by ordinary chat endpoints."""
from __future__ import annotations

import json
import re
from typing import Any

from ai_providers import chat_completion
import mcp_registry

_JSON_RE = re.compile(r"\{.*\}", re.S)


def _extract_action(text: str) -> dict[str, Any] | None:
    text = str(text or "").strip()
    candidates = [text]
    match = _JSON_RE.search(text)
    if match:
        candidates.append(match.group(0))
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except (TypeError, ValueError):
            continue
        if isinstance(value, dict) and value.get("action"):
            return value
    return None


def _tool_prompt(tools: list[dict[str, Any]]) -> str:
    names = ", ".join(f"{item['server_id']}:{item['name']}" for item in tools)
    return (
        "\n\nMCP TOOLS ARE AVAILABLE. Available tools: " + names +
        "\nIf the user's request needs one, respond with exactly one JSON object and nothing else: "
        '{"action":"mcp_call","server_id":"...","tool":"...","arguments":{}}' +
        "\nAfter the tool result, answer the user normally. Do not invent tool results. "
        "For destructive or external side-effect actions (send, delete, modify, publish), "
        "ask the user for confirmation before calling the tool."
    )


def complete_with_mcp(messages: list[dict[str, Any]], *, system: str, provider: str,
                      model: str, temperature: float = 0.7, max_tokens: int = 8192,
                      timeout: float = 120, max_calls: int = 4):
    """Return (reply, meta, events). If no MCP is connected, behaves normally."""
    try:
        tools = mcp_registry.connected_tools()
    except Exception:
        tools = []
    if not tools:
        return None, None, []

    prompt = system + _tool_prompt(tools)
    history = list(messages)
    events: list[dict[str, Any]] = []
    for _ in range(max_calls):
        reply, meta = chat_completion(
            history, system=prompt, provider=provider, model=model,
            temperature=temperature, max_tokens=max_tokens, timeout=timeout,
        )
        if reply is None:
            return None, meta, events
        action = _extract_action(reply)
        if not action or action.get("action") != "mcp_call":
            return reply, meta, events
        server_id = str(action.get("server_id") or "").strip()
        tool = str(action.get("tool") or "").strip()
        arguments = action.get("arguments") or {}
        if not server_id or not tool or not isinstance(arguments, dict):
            return reply, meta, events
        events.append({"type": "step", "icon": "🔌", "text": f"MCP: {server_id}:{tool}"})
        try:
            result = mcp_registry.call_tool(server_id, tool, arguments)
            observation = json.dumps(result, ensure_ascii=False)
            events.append({"type": "tool", "name": "mcp_call", "command": f"{server_id}:{tool}", "code": 0})
        except Exception as exc:
            observation = f"MCP error: {exc}"
            events.append({"type": "tool", "name": "mcp_call", "command": f"{server_id}:{tool}", "code": 1})
        history.append({"role": "assistant", "content": reply[:4000]})
        history.append({"role": "user", "content": "MCP TOOL RESULT:\n" + observation[:12000] +
                       "\nNow answer the original user request. If more MCP data is needed, emit another mcp_call JSON; otherwise answer normally."})
    return "Не удалось завершить MCP-действие за допустимое число шагов.", {}, events
