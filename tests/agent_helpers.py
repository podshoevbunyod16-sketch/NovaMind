"""Test fixtures for driving the first-party ReAct API with deterministic replies."""
from __future__ import annotations

import json
import re


def ndjson(response):
    return [json.loads(line) for line in response.get_data(as_text=True).splitlines()
            if line.strip()]


def answer_of(events):
    return "".join(event.get("token", "") for event in events if event["type"] == "token")


def events_of(events, kind):
    return [event for event in events if isinstance(event, dict) and event.get("type") == kind]


def _tool_payload(tag, body):
    """Translate legacy test-fixture notation into the current JSON tool call."""
    tag = tag.strip()
    if tag == "json":
        try:
            payload = json.loads(body)
        except ValueError:
            return None
        if isinstance(payload, dict) and isinstance(payload.get("tool"), str):
            return payload
        if isinstance(payload, dict) and payload.get("action"):
            action = str(payload["action"])
            mapping = {"search": "web_search", "open": "web_open", "run": "shell",
                       "read": "file_read", "write": "file_write", "edit": "file_edit",
                       "grep": "file_search", "tree": "file_tree"}
            tool = mapping.get(action, action)
            arguments = payload.get("arguments") or {
                key: value for key, value in payload.items() if key != "action"}
            return {"tool": tool, "arguments": arguments}
        return None

    base, _, qualifier = tag.partition(":")
    text = body.strip()
    if base in {"bash", "terminal", "shell"}:
        return {"tool": "shell", "arguments": {"command": text}}
    if base == "python" and qualifier:
        return {"tool": "file_write", "arguments": {"path": qualifier, "content": body.strip("\n")}}
    # Bare Python fences are examples, not an execution request.
    if base == "file_write" and qualifier:
        return {"tool": "file_write", "arguments": {"path": qualifier, "content": body.strip("\n")}}
    if base == "web_search":
        return {"tool": "web_search", "arguments": {"query": text}}
    if base in {"web_open", "web_fetch"}:
        return {"tool": "web_open", "arguments": {"url": text}}
    if base == "file_read":
        return {"tool": "file_read", "arguments": {"path": text}}
    if base == "file_finder":
        values = dict(re.findall(r"(?m)^\s*([a-z_]+)\s*=\s*(.*?)\s*$", text))
        action = values.get("action", "tree")
        name = values.get("name", ".")
        if action == "read":
            return {"tool": "file_read", "arguments": {"path": name}}
        if action in {"search", "grep"}:
            return {"tool": "file_search", "arguments": {"pattern": values.get("pattern", name),
                                                               "path": values.get("path", ".")}}
        return {"tool": "file_tree", "arguments": {"path": name}}
    try:
        payload = json.loads(text)
    except ValueError:
        payload = None
    known = {"file_edit", "mcp_call", "remember", "recall", "forget", "schedule",
             "task", "task_done", "image", "audio", "video", "skill_create",
             "no_such_tool", "nonexistent_tool"}
    if base in known and isinstance(payload, dict):
        return {"tool": base, "arguments": payload}
    return None


def _translate_fixture_response(text):
    """Convert fixture code blocks to one-tool-per-ReAct-turn JSON calls."""
    blocks = list(re.finditer(r"```([\w.-]+(?::[^\n]+)?)\s*\n(.*?)```", str(text or ""), re.S))
    calls = []
    for match in blocks:
        call = _tool_payload(match.group(1), match.group(2))
        if call is not None:
            calls.append(call)
    if not calls:
        return str(text or "").strip(), []
    def encoded(call):
        return "```json\n" + json.dumps(call, ensure_ascii=False) + "\n```"
    return encoded(calls[0]), [encoded(call) for call in calls[1:]]


def script(monkeypatch, answers, seen=None, module=None):
    """Stub the sole provider entrypoint and record the prompt passed to each turn."""
    import ai_providers
    queue = list(answers)
    deferred_calls = []
    monkeypatch.setattr(ai_providers, "resolve_target",
                        lambda provider=None, model=None: (provider or "groq",
                                                           model or "test-model", {"max_tokens": 4096}))

    def fake_complete(messages, provider=None, model=None, system=None, temperature=0.7,
                      max_tokens=None, timeout=90, extra=None, tools=None,
                      tool_choice=None, **kwargs):
        if seen is not None:
            seen.append({"messages": [dict(item) for item in messages],
                         "system": system or "", "tools": tools, "max_tokens": max_tokens})
        if deferred_calls:
            text = deferred_calls.pop(0)
        else:
            text = queue.pop(0) if len(queue) > 1 else (queue[0] if queue else "Готово.")
            translated, deferred = _translate_fixture_response(text)
            text = translated
            deferred_calls.extend(deferred)
        return text, {"provider": provider or "test", "model": model or "test-model"}

    monkeypatch.setattr(ai_providers, "chat_completion", fake_complete)
    return queue


def model_text(seen, index=-1):
    item = seen[index]
    parts = [item.get("system") or ""]
    parts += [str(message.get("content") or "") for message in item["messages"]]
    return "\n".join(parts)
