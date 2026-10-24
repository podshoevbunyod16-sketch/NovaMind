"""NovaMind capabilities as AgenticSeek tools.

AgenticSeek has no MCP client, no media generation, no long-term memory, no
scheduled jobs and no skill library. NovaMind has all of them, already wired to
its own security policy. Instead of re-implementing them, each capability is
exposed as a tool with a block tag, and the tool calls back into NovaMind
through a context object (built by ``routes/agent.py``):

.. code-block:: python

    ctx.call_mcp(server_id, tool, arguments) -> dict
    ctx.generate_media(kind, prompt, options) -> (result, model, errors)
    ctx.remember(text, kind, tags) / ctx.recall(query, limit) / ctx.forget(id)
    ctx.create_task(title, detail) / ctx.close_task(id, note)
    ctx.create_schedule(prompt, kind, expr)
    ctx.save_skill(name, content)
    ctx.emit(event) / ctx.state

A capability that is not configured for the run is simply not registered, so the
model never sees a tool it cannot use.
"""

from __future__ import annotations

import json
from typing import Any

from agenticseek.core.agent import dumps
from agenticseek.tools.tools import Tools

MAX_PAYLOAD = 20000

#: MCP tool names that change state on the remote service: always confirmed.
_MCP_WRITE_HINTS = ("delete", "remove", "create", "update", "post", "send", "write",
                    "merge", "close", "comment", "push", "upload", "invite", "archive")


class BridgeTool(Tools):
    """A NovaMind action behind one block tag."""

    def __init__(self, tag: str, description: str, handler, risk: str = "write",
                 state=None, emit=None):
        super().__init__()
        self.tag = tag
        self.name = tag
        self.description = description
        self.handler = handler
        self.risk = risk
        self.state = state
        self.emit_event = emit

    def execute(self, payload: str) -> str:
        try:
            result = self.handler(str(payload or "")[:MAX_PAYLOAD])
        except Exception as error:  # noqa: BLE001 — the model must see the failure
            return f"[failure] {self.tag}: {type(error).__name__}: {error}"
        return result if str(result).startswith(("[success]", "[failure]")) else f"[success] {result}"

    def risk_for(self, payload: str) -> str:
        return self.risk

    def execution_failure_check(self, output: str) -> bool:
        return str(output or "").lstrip().startswith("[failure]")


class McpBridgeTool(BridgeTool):
    """mcp_call: модель видит список инструментов; не-админ только читает."""

    def __init__(self, ctx, state=None, emit=None):
<<<<<<< HEAD
        tools = getattr(ctx, "mcp_tools", None) or []
        tools = sorted(tools, key=lambda t: 0 if "search" in t["name"] else 1)
        listing = "; ".join(f"{t['server_id']}:{t['name']}" for t in tools[:60])
=======
        tools = [t for t in (getattr(ctx, "mcp_tools", None) or []) if isinstance(t, dict)]
        # Одна запись без имени не должна валить весь ход агента: раньше это
        # было KeyError: 'name' и пользователь видел «Внутренняя ошибка агента».
        listing = "; ".join(
            f"{t.get('server_id') or t.get('server') or '?'}:"
            f"{t.get('name') or t.get('tool') or '?'}" for t in tools[:40])
>>>>>>> d05c0b4157be846b2329c1282d941c1ee501d7e2
        super().__init__(
            "mcp_call",
            'Вызвать инструмент MCP-сервера: {"server_id":"…","tool":"…","arguments":{}}. '
            "Доступные (server_id:tool): " + (listing or "пусто") +
            ". Выбери подходящий сам, у пользователя server_id не спрашивай. "
            "Для «найди/есть ли» используй search_repositories или search_code; "
            "get_file_contents только когда путь уже известен. "
            "Найдя 2-3 результата, сразу дай итоговый ответ.",
            self._call, risk="write", state=state, emit=emit)
        self.ctx = ctx

    def risk_for(self, payload: str) -> str:
        action = parse_json(payload)
        name = str(action.get("tool") or "").lower()
        return "danger" if any(h in name for h in _MCP_WRITE_HINTS) else "write"

    def _call(self, payload: str) -> str:
        action = parse_json(payload)
        server_id = str(action.get("server_id") or action.get("server") or "").strip()
        tool = str(action.get("tool") or action.get("name") or "").strip()
        arguments = action.get("arguments") or action.get("args") or {}
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except ValueError:
                arguments = {}
        if not server_id or not tool:
            return ("[failure] Нужны server_id и tool. Пример: "
                    '{"server_id":"github","tool":"search_repositories","arguments":{"query":"x"}}')
        # админ: без ограничений; остальные: только чтение
        if not getattr(self.ctx, "is_admin", False) and \
                any(h in tool.lower() for h in _MCP_WRITE_HINTS):
            return "[failure] Инструмент меняет данные: доступен только администратору. Поиск и чтение разрешены."
        try:
            result = self.ctx.call_mcp(server_id, tool, arguments if isinstance(arguments, dict) else {})
        except Exception as error:  # noqa: BLE001
            return f"[failure] MCP {server_id}:{tool} не выполнен: {error}"
        if self.state is not None:
            self.state.add_note(f"MCP {server_id}:{tool}")
        return f"[success] MCP {server_id}:{tool}:\n{dumps(result)[:6000]}"


class MediaTool(BridgeTool):
    """``image`` / ``audio`` / ``video`` — generate media through NovaMind."""

    def __init__(self, kind: str, ctx, state=None, emit=None):
        super().__init__(
            kind, f"Сгенерировать {kind} по описанию (prompt).",
            self._generate, risk="write", state=state, emit=emit)
        self.kind = kind
        self.ctx = ctx

    def _generate(self, payload: str) -> str:
        params = parse_json(payload)
        prompt = str(params.get("prompt") or params.get("text") or payload).strip()
        if not prompt:
            return f"[failure] {self.kind}: нужен prompt."
        try:
            result, model, errors = self.ctx.generate_media(self.kind, prompt, params)
        except Exception as error:  # noqa: BLE001
            return f"[failure] Медиа не создано: {type(error).__name__}: {error}"
        if not result:
            reason = "; ".join(str(item) for item in (errors or [])[-3:]) or "провайдер не ответил"
            return f"[failure] Медиа не создано: {reason}"
        if self.state is not None:
            self.state.add_media(result, model)
        if self.emit_event is not None:
            try:
                self.emit_event({"type": "media", "media": result, "model": model})
            except Exception:  # noqa: BLE001
                pass
        return (f"[success] Медиа создано ({model}) и уже показано пользователю. "
                "Не вставляй ссылку в ответ — достаточно короткого подтверждения.")


def build_nova_tools(ctx, state=None, emit=None) -> dict:
    """Register the NovaMind tools the current run really supports."""
    tools: dict[str, Tools] = {}

    if getattr(ctx, "mcp_tools", None):
        tools["mcp_call"] = McpBridgeTool(ctx, state=state, emit=emit)

    for kind in ("image", "audio", "video"):
        if getattr(ctx, f"media_{kind}_available", False):
            tools[kind] = MediaTool(kind, ctx, state=state, emit=emit)

    if getattr(ctx, "memory_available", False):
        tools["remember"] = BridgeTool(
            "remember", "Запомнить факт о пользователе: {text, kind, tags}",
            lambda payload: _remember(ctx, payload), state=state, emit=emit)
        tools["recall"] = BridgeTool(
            "recall", "Найти факты в памяти: {query, limit}",
            lambda payload: _recall(ctx, payload), risk="safe", state=state, emit=emit)
        tools["forget"] = BridgeTool(
            "forget", "Удалить воспоминание по id: {id}",
            lambda payload: _forget(ctx, payload), state=state, emit=emit)

    if getattr(ctx, "tasks_available", False):
        tools["task"] = BridgeTool(
            "task", "Завести задачу с шагами: {title, detail}",
            lambda payload: _task(ctx, payload), state=state, emit=emit)
        tools["task_done"] = BridgeTool(
            "task_done", "Закрыть задачу: {task_id, note}",
            lambda payload: _task_done(ctx, payload), state=state, emit=emit)

    if getattr(ctx, "schedule_available", False):
        tools["schedule"] = BridgeTool(
            "schedule", "Задача по расписанию: {prompt, cron|every}",
            lambda payload: _schedule(ctx, payload), state=state, emit=emit)

    if getattr(ctx, "skills_available", False):
        tools["skill_create"] = BridgeTool(
            "skill_create", "Сохранить навык (SKILL.md): {name, content}",
            lambda payload: _skill(ctx, payload), state=state, emit=emit)

    return tools


# ── handlers ──────────────────────────────────────────────────────────────
def _remember(ctx, payload: str) -> str:
    params = parse_json(payload)
    text = str(params.get("text") or params.get("value") or "").strip()
    if not text:
        return "[failure] remember: нужен text."
    try:
        item = ctx.remember(text, str(params.get("kind") or "fact"),
                            str(params.get("tags") or ""))
    except ValueError as error:
        return f"[failure] В память не сохранено: {error}"
    return f"[success] Запомнено ({item.get('kind')}): {item.get('text')}"


def _recall(ctx, payload: str) -> str:
    params = parse_json(payload)
    query = str(params.get("query") or params.get("text") or "").strip()
    try:
        limit = max(1, min(int(params.get("limit") or 5), 20))
    except (TypeError, ValueError):
        limit = 5
    items = ctx.recall(query, limit)
    if not items:
        return "[success] В памяти ничего не нашлось."
    return f"[success] Найдено {len(items)}:\n{dumps(items)[:6000]}"


def _forget(ctx, payload: str) -> str:
    params = parse_json(payload)
    memory_id = str(params.get("id") or params.get("memory_id") or "").strip()
    if not memory_id:
        return "[failure] forget: нужен id воспоминания."
    return ("[success] Воспоминание удалено." if ctx.forget(memory_id)
            else "[failure] Запись с таким id не найдена.")


def _task(ctx, payload: str) -> str:
    params = parse_json(payload)
    title = str(params.get("title") or params.get("text") or "").strip()
    if not title:
        return "[failure] task: нужен title."
    task = ctx.create_task(title[:200], str(params.get("detail") or "")[:2000])
    return (f"[success] Задача создана, id={task.get('id')}. Выполни её по шагам "
            "и закрой через ```task_done```.")


def _task_done(ctx, payload: str) -> str:
    params = parse_json(payload)
    task_id = str(params.get("task_id") or params.get("id")
                  or getattr(ctx, "task_id", "") or "").strip()
    if not task_id:
        return "[failure] task_done: не указан task_id."
    note = str(params.get("note") or params.get("text") or "Готово")
    task = ctx.close_task(task_id, note[:1000])
    if not task:
        return "[failure] task_done: задача не найдена."
    return "[success] Задача закрыта. Теперь ответь пользователю."


def _schedule(ctx, payload: str) -> str:
    params = parse_json(payload)
    prompt = str(params.get("prompt") or params.get("text") or "").strip()
    cron = str(params.get("cron") or "").strip()
    every = str(params.get("every") or "").strip()
    if not prompt:
        return "[failure] schedule: нужен prompt — что делать при запуске."
    if not cron and not every:
        return "[failure] schedule: нужно расписание — cron (5 полей) или every (30m, 2h, 1d)."
    try:
        job, description, next_run = ctx.create_schedule(prompt, "cron" if cron else "every",
                                                        cron or every)
    except ValueError as error:
        return f"[failure] Расписание не создано: {error}"
    return (f"[success] Задание создано ({description}), первый запуск {next_run}. "
            "Результаты появятся в чате «⏰ Расписание».")


def _skill(ctx, payload: str) -> str:
    params = parse_json(payload)
    name = str(params.get("name") or "").strip()
    content = str(params.get("content") or "").strip()
    if not name or not content:
        return "[failure] skill_create: нужны name и полный content (SKILL.md)."
    try:
        item = ctx.save_skill(name, content)
    except ValueError as error:
        return f"[failure] Skill не сохранён: {error}"
    return f"[success] Skill «{item.get('name') or name}» сохранён."


def parse_json(payload: str) -> dict:
    """Parse a tool payload: JSON object, ``key=value`` lines, or free text."""
    text = str(payload or "").strip()
    if not text:
        return {}
    if text.startswith("{"):
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                return data
        except ValueError:
            start, end = text.find("{"), text.rfind("}")
            if start != -1 and end > start:
                try:
                    data = json.loads(text[start:end + 1])
                    if isinstance(data, dict):
                        return data
                except ValueError:
                    pass
    result: dict[str, Any] = {}
    for line in text.splitlines():
        line = line.strip().lstrip("-").strip()
        if not line or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().lower()
        if key and " " not in key:
            result[key] = value.strip()
    return result or {"text": text}
