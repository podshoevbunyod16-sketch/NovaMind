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
    summary = mcp_registry.format_tools_summary(tools)
    return (
        "\n\nMCP TOOLS ARE AVAILABLE. Choose the appropriate MCP server and tool automatically "
        "from the user's request without asking the user for server_id or extra instructions.\n"
        "Available MCP tools: " + summary +
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
    last_action = None
    last_error = None
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
        last_action = action
        tool = str(action.get("tool") or action.get("name") or "").strip()
        server_id = mcp_registry.resolve_server_id(
            str(action.get("server_id") or action.get("server") or "").strip(),
            tool,
            tools,
        )
        arguments = action.get("arguments") or action.get("args") or {}
        if not server_id or not tool or not isinstance(arguments, dict):
            last_error = "Модель вернула неполный MCP-вызов."
            history.append({"role": "assistant", "content": reply[:4000]})
            history.append({"role": "user", "content":
                            "MCP ERROR: неполный вызов. Не возвращай JSON пользователю; "
                            "ответь обычным текстом или сформируй корректный mcp_call."})
            continue
        events.append({"type": "step", "icon": "🔌", "text": f"MCP: {server_id}:{tool}"})
        try:
            result = mcp_registry.call_tool(server_id, tool, arguments)
            observation = json.dumps(result, ensure_ascii=False)
            events.append({"type": "tool", "name": "mcp_call", "command": f"{server_id}:{tool}", "code": 0})
        except Exception as exc:
            observation = f"MCP error: {exc}"
            last_error = str(exc)
            err_code = (getattr(exc, "status_code", None)
                        or getattr(getattr(exc, "response", None), "status_code", None)
                        or 1)
            events.append({
                "type": "tool",
                "name": "mcp_call",
                "kind": "mcp",
                "icon": "🔌",
                "label": "Ошибка MCP",
                "detail": f"{server_id} · {tool}",
                "command": f"{server_id}:{tool}",
                "code": err_code,
                "status": "error",
                "output": str(exc)[:1200],
                "error": str(exc)[:1200],
            })
        history.append({"role": "assistant", "content": reply[:4000]})
        history.append({"role": "user", "content": "MCP TOOL RESULT:\n" + observation[:12000] +
                       "\nNow answer the original user request. If more MCP data is needed, emit another mcp_call JSON; otherwise answer normally."})
    if last_action:
        label = f"{last_action.get('server_id')}:{last_action.get('tool')}"
        detail = f" Причина: {last_error}." if last_error else ""
        return f"Не удалось выполнить MCP-инструмент {label}.{detail} Попробуйте повторить запрос или проверить подключение MCP-сервера.", {}, events
    return "Не удалось завершить MCP-действие за допустимое число шагов.", {}, events
