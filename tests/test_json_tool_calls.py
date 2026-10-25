import json
import pytest

from agenticseek.core.markup import normalize_tool_json, sanitize_answer
from tests.agent_helpers import script, ndjson, answer_of, events_of


@pytest.mark.parametrize('name,payload,expected', [
    ('web_search', 'погода Душанбе сегодня', 'погода Душанбе сегодня'),
    ('web_search', {'query': 'Душанбе'}, 'Душанбе'),
    ('web_open', {'url': 'https://example.com'}, 'https://example.com'),
    ('terminal', {'command': 'ls'}, 'ls'),
    ('python', {'code': 'print(1)'}, 'print(1)'),
    ('mcp_call', {'server_id': 'x', 'tool': 'read'}, '{"server_id": "x", "tool": "read"}'),
])
def test_normalize(name, payload, expected):
    raw = json.dumps({'tool': name, 'tool_input': payload}, ensure_ascii=False)
    assert normalize_tool_json(raw) == f'```{name}\n{expected}\n```'
    assert normalize_tool_json('```json\n' + raw + '\n```') == normalize_tool_json(raw)
    assert sanitize_answer(raw) == raw  # display is not an execution context


@pytest.mark.parametrize('raw', [
    'Например: {"tool":"terminal","tool_input":"ls"}',
    '{"tool":"terminal"}', '[]', '{"tool":"terminal","tool_input":null}',
    '{"tool":"bad```tag","tool_input":"ls"}',
    '```python\nprint(1)\n```',
])
def test_do_not_interpret_examples_or_invalid_payloads(raw):
    assert normalize_tool_json(raw) == raw


def test_screenshot_search_calls_backend_and_returns_answer(admin_client, monkeypatch):
    import routes.agent as routes
    queries = []
    def search(query, limit=6):
        queries.append(query)
        return [{'title': 'Погода Душанбе', 'url': 'https://example.com/weather',
                 'snippet': 'Тестовая температура: 20 градусов'}], {}
    monkeypatch.setattr(routes, 'nova_search', search)
    seen = []
    script(monkeypatch, [
        '{"tool":"web_search","tool_input":"погода Душанбе сегодня"}',
        'По тестовому источнику: 20 градусов.',
    ], seen)
    events = ndjson(admin_client.post('/api/agent/stream',
                                     json={'message': 'Погода в Душанбе сегодня'}))
    assert queries == ['погода Душанбе сегодня']
    assert any(e.get('name') == 'web_search' for e in events_of(events, 'tool'))
    assert '20 градусов' in answer_of(events)
    assert 'tool_input' not in answer_of(events)
    assert any('Тестовая температура' in str(item['messages']) for item in seen)


def test_terminal_json_reaches_sandbox(admin_client, sandbox, monkeypatch):
    script(monkeypatch, [
        '{"tool":"terminal","tool_input":{"command":"printf fixed > json-tool.txt"}}',
        'Файл создан.',
    ])
    events = ndjson(admin_client.post('/api/agent/stream',
                                     json={'message': 'выполни команду и создай файл'}))
    assert (sandbox / 'json-tool.txt').read_text() == 'fixed'
    assert any(e.get('name') == 'terminal' for e in events_of(events, 'tool'))


def test_unknown_json_tool_not_reported_as_success(admin_client, monkeypatch):
    script(monkeypatch, ['{"tool":"missing_tool","tool_input":"data"}',
                         'Этот инструмент недоступен.'])
    events = ndjson(admin_client.post('/api/agent/stream', json={'message': 'найди погоду'}))
    assert not events_of(events, 'tool')
    assert 'tool_input' not in answer_of(events)
