"""Tool-call decoding and validation for the first-party ReAct protocol."""
import json

import pytest

from agent.loop import _extract_fenced, _normalize_native, _valid
from guardrails import check_output


@pytest.mark.parametrize("name,arguments", [
    ("web_search", {"query": "погода Душанбе сегодня"}),
    ("web_open", {"url": "https://example.com"}),
    ("file_read", {"path": "notes.txt"}),
    ("file_write", {"path": "notes.txt", "content": "текст"}),
    ("shell", {"command": "printf hello"}),
    ("mcp_call", {"server_id": "local", "tool": "read", "arguments": {}}),
])
def test_fenced_json_uses_tool_and_arguments(name, arguments):
    raw = "Перед вызовом.\n```json\n" + json.dumps(
        {"tool": name, "arguments": arguments}, ensure_ascii=False) + "\n```"
    call, remainder = _extract_fenced(raw)
    assert call == {"tool": name, "arguments": arguments}
    assert remainder == "Перед вызовом."


@pytest.mark.parametrize("raw", [
    'Example: {"tool":"shell","arguments":{"command":"ls"}}',
    '```json\n{"tool":"shell","arguments": {broken}}\n```',
    '```python\nprint(1)\n```',
])
def test_examples_and_malformed_payloads_are_not_valid_calls(raw):
    call, _ = _extract_fenced(raw)
    assert call is None


def test_provider_native_tool_calls_are_normalized():
    calls = _normalize_native([{"function": {"name": "lookup",
                                                "arguments": '{"query":"x"}'}}])
    assert calls == [{"tool": "lookup", "arguments": {"query": "x"}}]


def test_schema_validation_rejects_missing_and_extra_arguments():
    schema = {"type": "object", "properties": {"query": {"type": "string"}},
              "required": ["query"], "additionalProperties": False}
    assert _valid({}, schema)
    assert _valid({"query": "x", "extra": True}, schema)
    assert _valid({"query": "x"}, schema) is None


def test_missing_arguments_are_rejected_by_the_declared_schema():
    call, _ = _extract_fenced('{"tool":"shell"}')
    assert call and call["tool"] == "shell"
    schema = {"type": "object", "properties": {"command": {"type": "string"}},
              "required": ["command"]}
    assert _valid(call.get("arguments"), schema)


def test_display_sanitization_does_not_change_non_execution_context():
    raw = 'Пояснение: {"tool":"shell","arguments":{"command":"ls"}}'
    verdict = check_output(raw)
    assert verdict.text == raw
