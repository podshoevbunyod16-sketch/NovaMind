import pytest
from types import SimpleNamespace
from agenticseek.tools.terminal_commands import parse_command, dispatch


@pytest.mark.parametrize('command,target,body', [
    ("nova web search 'python docs'", 'web_search', 'python docs'),
    ("nova web open 'https://example.com'", 'web_open', 'https://example.com'),
    ('nova mcp call \'{"server_id":"github","tool":"search"}\'',
     'mcp_call', '{"server_id":"github","tool":"search"}'),
    ('nova call file_read \'{"path":"README.md"}\'',
     'file_read', '{"path":"README.md"}'),
    ('python3 -m novamind_cli call file_read \'{"path":"README.md"}\'',
     'file_read', '{"path":"README.md"}'),
    ('python -m novamind_cli mcp call \'{"server_id":"github","tool":"search"}\'',
     'mcp_call', '{"server_id":"github","tool":"search"}'),
])
def test_parse(command, target, body):
    assert parse_command(command) == (target, body)


@pytest.mark.parametrize('command', [
    'nova call terminal ls', 'nova call python print',
    'nova web search query ; rm -rf data', 'nova mcp call \'[]\'',
    'nova call missing',
    'python3 -m novamind_cli call terminal ls',
    'python3 -m novamind_cli mcp call \'[]\'',
    'python3 -m novamind_cli call web_search \'{"query":\'',
])
def test_reject(command):
    with pytest.raises(ValueError):
        parse_command(command)


def test_normal_shell_unchanged():
    assert parse_command('python3 report.py') is None
    assert parse_command('nova search example') is None


def test_delegation_uses_target_policy_path():
    calls = []
    agent = SimpleNamespace(
        all_tools=lambda: {'mcp_call': object()},
        run_tool=lambda *args: calls.append(args) or (False, 'denied'),
    )
    result = dispatch(agent, 'nova mcp call \'{"tool":"push"}\'')
    assert result == (False, 'denied')
    assert calls == [('mcp_call', '{"tool":"push"}')]


def test_python_gateway_adapts_json_to_legacy_tool_payloads():
    calls = []
    agent = SimpleNamespace(
        all_tools=lambda: {"web_search": object(), "web_open": object(),
                           "file_find": object(), "file_write": object()},
        run_tool=lambda *args: calls.append(args) or (True, "[success] ok"),
    )
    assert dispatch(agent, "python3 -m novamind_cli call web_search "
                           "'{\"query\":\"weather Khujand\"}'")[0] is True
    assert dispatch(agent, "python3 -m novamind_cli call web_open "
                           "'{\"url\":\"https://example.com\"}'")[0] is True
    assert dispatch(agent, "python3 -m novamind_cli call file_find "
                           "'{\"name\":\"app.py\"}'")[0] is True
    assert dispatch(agent, "python3 -m novamind_cli call file_write "
                           "'{\"path\":\"note.txt\",\"content\":\"hello\"}'")[0] is True
    assert calls == [
        ("web_search", "weather Khujand"),
        ("web_open", "https://example.com"),
        ("file_find", "action=find\nname=app.py"),
        ("file_write", "hello", "note.txt"),
    ]


def test_unavailable_not_called():
    agent = SimpleNamespace(all_tools=lambda: {})
    assert dispatch(agent, "nova call file_read README.md")[0] is False


def test_catalog_is_request_local():
    steps = []
    agent = SimpleNamespace(
        all_tools=lambda: {'web_open': SimpleNamespace(tag='web_open', description='Read')},
        take_step=lambda: steps.append(1),
    )
    assert 'web_open' in dispatch(agent, 'nova tools')[1]
    assert steps == [1]
    assert dispatch(agent, 'nova mcp tools')[0] is False
