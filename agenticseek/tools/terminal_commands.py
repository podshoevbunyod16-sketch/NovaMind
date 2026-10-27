"""Request-local command routing; never eval model-generated command arguments.

These commands are interpreted by the Python agent, not a system shell. Ordinary
commands still use the existing sandbox. Delegation preserves the target tool's
permissions, confirmations, budget, persistence and timeline events.
"""
from __future__ import annotations

import json
import shlex

HELP = """Единый командный интерфейс (блок terminal):
Предпочитай команды nova в terminal вместо отдельных блоков инструментов.
Python-код запускай блоком python или сохранённым скриптом python3 script.py.
  nova tools
  nova call <имя_инструмента> '<JSON или текст>'
  nova web search 'запрос'
  nova web open 'https://example.com'
  nova mcp tools
  nova mcp call '<JSON: server_id, tool, arguments>'
Для документов запускай python3 script.py (pypdf, python-docx, openpyxl).
Обычные команды выполняются существующей песочницей; sudo и обход прав запрещены.
Сам выбирай команды, читай результат, исправляй ошибки и продолжай до результата.
Не утверждай, что команда выполнена, если нет успешного результата.
GitHub: найди репозиторий, клонируй в workspace, изучи код, воспроизведи ошибку,
исправь и запусти тесты. Публикация только с разрешением и правами подключённого
аккаунта; если push недоступен, подготовь diff и честно сообщи ограничение.
"""


def parse_command(payload: str):
    """Return (target, payload), None for a normal shell command, or raise.

    Only a whole, standalone command is accepted. Shell operators are never
    executed after a delegated call. JSON/text must be a single quoted argument.
    """
    text = str(payload or '').strip()
    if not text.startswith('nova '):
        return None
    args = shlex.split(text)
    if args == ['nova', 'tools']:
        return '__tools__', ''
    if args == ['nova', 'mcp', 'tools']:
        return '__mcp_tools__', ''
    if len(args) == 4 and args[:2] == ['nova', 'call']:
        if args[2] in {'terminal', 'bash', 'python'}:
            raise ValueError('Запускайте код непосредственно блоком python или terminal.')
        return args[2], args[3]
    if len(args) == 4 and args[:3] == ['nova', 'web', 'search']:
        return 'web_search', args[3]
    if len(args) == 4 and args[:3] == ['nova', 'web', 'open']:
        return 'web_open', args[3]
    if len(args) == 4 and args[:3] == ['nova', 'mcp', 'call']:
        value = json.loads(args[3])
        if not isinstance(value, dict):
            raise ValueError('MCP ожидает JSON-объект.')
        return 'mcp_call', args[3]
    # Preserve the existing terminal's nova search/read/help commands.
    if len(args) > 1 and args[1] in {'search', 'read', 'help'}:
        return None
    raise ValueError('Некорректная nova-команда. Используйте nova tools; один вызов на блок.')


def _catalog_step(agent, payload: str, feedback: str):
    """Показываем `nova tools` / `nova mcp tools` как шаг терминала в таймлайне."""
    from agenticseek.timeline import tool_event

    emit = getattr(agent, 'emit', None)
    if callable(emit):
        try:
            emit(tool_event('terminal', str(payload).strip(), ok=True,
                            command=str(payload).strip(), output=feedback, code=0))
        except Exception:  # noqa: BLE001 — отрисовка не должна ломать команду
            pass
    state = getattr(agent, 'state', None)
    if state is not None and hasattr(state, 'add_tool'):
        try:
            state.add_tool('terminal', True)
        except Exception:  # noqa: BLE001
            pass


def dispatch(agent, payload: str):
    """Return normal run_tool result, or None to continue with the sandbox."""
    try:
        parsed = parse_command(payload)
    except (ValueError, TypeError) as exc:
        return False, f'[failure] {exc}'
    if parsed is None:
        return None
    target, body = parsed
    tools = agent.all_tools()
    if target == '__tools__':
        agent.take_step()
        catalog = {getattr(t, 'tag', name): t.description for name, t in tools.items()}
        feedback = '[success] ' + json.dumps(catalog, ensure_ascii=False)
        _catalog_step(agent, payload, feedback)
        return True, feedback
    if target == '__mcp_tools__':
        agent.take_step()
        tool = tools.get('mcp_call')
        if tool is None:
            return False, '[failure] MCP не подключён для этого исполнителя.'
        feedback = '[success] ' + tool.description
        _catalog_step(agent, payload, feedback)
        return True, feedback
    if target not in tools:
        return False, f'[failure] Инструмент {target} недоступен для этого исполнителя.'
    return agent.run_tool(target, body)
