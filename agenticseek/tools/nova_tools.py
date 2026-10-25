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
import logging
import re
from typing import Any

import mcp_registry
from agenticseek.core.agent import dumps
from agenticseek.tools.tools import Tools

mcp_log = logging.getLogger("novamind.mcp")

MAX_PAYLOAD = 20000
MAX_MCP_SEARCH_CALLS = 3

#: MCP tool names that change state on the remote service: always confirmed.
_MCP_WRITE_HINTS = ("delete", "remove", "create", "update", "post", "send", "write",
                    "merge", "close", "comment", "push", "upload", "invite", "archive")

_GITHUB_SEARCH_TOOLS = {
    "search_repositories", "search_code", "search_issues",
    "search_users", "search_pull_requests",
}
_REPO_SEARCH_GOAL_RE = re.compile(
    r"(?i)(?<![\wа-яё])(?:программ\w*|репозитори\w*|проект\w*|бот\w*|библиотек\w*|"
    r"утилит\w*|приложени\w*|сервис\w*|скрипт\w*|repo|repos|repositories|projects|"
    r"programs|bots)(?![\wа-яё])"
)
_EXPLICIT_CODE_GOAL_RE = re.compile(
    r"(?i)(?:search_code|строк\w*\s+кода|внутри\s+кода|идентификатор|функци\w*\s+в\s+репозитории)"
)
_CODE_QUALIFIER_RE = re.compile(r"(?i)\b(?:repo:|path:|filename:|extension:|symbol:)")
_HTTP_STATUS_RE = re.compile(r"\b(400|401|403|404|408|409|413|422|429|500|502|503|504)\b")
_QUERY_FILLER_RE = re.compile(
    r"(?i)(?<![\wа-яё])(?:пожалуйста|найди(?:те)?|найти|поищи(?:те)?|покажи(?:те)?|"
    r"используй(?:те)?|через|помощью|несколько|программ\w*|репозитори\w*|на\s+github|"
    r"на\s+гитхаб\w*|в\s+github|mcp)(?![\wа-яё])"
)


def _compact_github_query(raw_query: str) -> str:
    """Укорачивает слишком длинный разговорный запрос для GitHub Search API."""
    q = re.sub(r"\s+", " ", str(raw_query or "")).strip()
    if len(q) <= 90:
        return q
    cleaned = _QUERY_FILLER_RE.sub(" ", q)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,.;:!?")
    if not cleaned:
        cleaned = q
    words = cleaned.split()
    return " ".join(words[:8])[:100]


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
        tools = [t for t in (getattr(ctx, "mcp_tools", None) or []) if isinstance(t, dict)]
        # Одна запись без имени не должна валить весь ход агента: раньше это
        # было KeyError: 'name' и пользователь видел «Внутренняя ошибка агента».
        listing = mcp_registry.format_tools_summary(tools, max_tools=40)
        super().__init__(
            "mcp_call",
            'Вызвать инструмент подключённого MCP-сервера: {"server_id":"…","tool":"…","arguments":{}}. '
            "Доступные серверы и инструменты (server_id:tool): " + listing +
            ". Выбирай подходящий сервер и инструмент САМ по смыслу запроса пользователя — "
            "без просьбы уточнить server_id или написать «используй MCP». "
            "Для запросов «найди программы/репозитории/проекты/ботов» на GitHub используй "
            'search_repositories (НЕ search_code) с коротким запросом (2–4 слова) и "minimal_output": true. '
            "Перед первым поиском на GitHub MCP вызови get_me один раз; "
            f"не делай больше {MAX_MCP_SEARCH_CALLS} поисковых вызовов за запуск. "
            'Для чтения файла или корня известного репозитория передавай owner, repo и path ("" для корня). '
            "Найдя нужные данные (2–3 результата при поиске), сразу дай итоговый ответ.",
            self._call, risk="write", state=state, emit=emit)
        self.ctx = ctx
        self.tools_catalog = tools
        self.last_error_code: int | None = None
        self._local_get_me_done: set[str] = set()
        self._local_search_count: int = 0

    def risk_for(self, payload: str) -> str:
        action = parse_json(payload)
        name = str(action.get("tool") or "").lower()
        return "danger" if any(h in name for h in _MCP_WRITE_HINTS) else "write"

    def _server_tool_names(self, server_id: str) -> set[str]:
        return {
            str(t.get("name") or t.get("tool") or "").strip()
            for t in self.tools_catalog
            if str(t.get("server_id") or t.get("server") or "").strip() == server_id
            and (t.get("name") or t.get("tool"))
        }

    def _is_github_server(self, server_id: str, tool: str) -> bool:
        names = self._server_tool_names(server_id)
        if "get_me" in names or "search_repositories" in names:
            return True
        if "github" in server_id.lower():
            return True
        for entry in self.tools_catalog:
            if str(entry.get("server_id") or "") == server_id:
                if "github" in str(entry.get("server_name") or "").lower():
                    return True
        return tool in _GITHUB_SEARCH_TOOLS and (not names or "get_me" in names)

    def _get_me_set(self) -> set[str]:
        done = getattr(self.ctx, "_mcp_get_me_done", None)
        if isinstance(done, set):
            return done
        try:
            self.ctx._mcp_get_me_done = self._local_get_me_done
            return self.ctx._mcp_get_me_done
        except Exception:  # noqa: BLE001
            return self._local_get_me_done

    def _search_count(self) -> int:
        return int(getattr(self.ctx, "_mcp_search_count", self._local_search_count) or 0)

    def _inc_search_count(self) -> int:
        nxt = self._search_count() + 1
        self._local_search_count = nxt
        try:
            self.ctx._mcp_search_count = nxt
        except Exception:  # noqa: BLE001
            pass
        return nxt

    def _call(self, payload: str) -> str:
        self.last_error_code = None
        action = parse_json(payload)
        tool = str(action.get("tool") or action.get("name") or "").strip()
        raw_server_id = str(action.get("server_id") or action.get("server") or "").strip()
        server_id = mcp_registry.resolve_server_id(raw_server_id, tool, self.tools_catalog)
        arguments = action.get("arguments") or action.get("args") or {}
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except ValueError:
                arguments = {}
        arguments = dict(arguments) if isinstance(arguments, dict) else {}
        if not server_id or not tool:
            return ("[failure] Нужны server_id и tool. Пример: "
                    '{"server_id":"github","tool":"search_repositories","arguments":{"query":"x","minimal_output":true}}')
        # админ: без ограничений; остальные: только чтение
        if not getattr(self.ctx, "is_admin", False) and \
                any(h in tool.lower() for h in _MCP_WRITE_HINTS):
            return "[failure] Инструмент меняет данные: доступен только администратору. Поиск и чтение разрешены."

        # Нормализация owner/repo и path для get_file_contents на GitHub MCP.
        if tool == "get_file_contents":
            repo_arg = str(arguments.get("repo") or "").strip()
            gh_match = re.match(
                r"^(?:https?://github\.com/)?([^/\s]+)/([^/\s?#]+?)(?:\.git)?/?$",
                repo_arg,
                re.I,
            )
            if gh_match and not arguments.get("owner"):
                arguments["owner"] = gh_match.group(1)
                arguments["repo"] = gh_match.group(2)
            arguments.setdefault("path", "")

        # Для запросов «найди программы/репозитории/ботов» заменяем search_code на search_repositories.
        goal = str(getattr(self.ctx, "goal", "") or "")
        query_val = str(arguments.get("query") or arguments.get("q") or "")
        server_names = self._server_tool_names(server_id)
        if tool == "search_code" and not _CODE_QUALIFIER_RE.search(query_val):
            wants_repos = bool(_REPO_SEARCH_GOAL_RE.search(goal)) or (
                not _EXPLICIT_CODE_GOAL_RE.search(goal) and len(query_val) > 50
            )
            if wants_repos and (not server_names or "search_repositories" in server_names):
                tool = "search_repositories"

        if tool == "search_repositories":
            arguments["minimal_output"] = True
            if "query" in arguments:
                arguments["query"] = _compact_github_query(str(arguments["query"]))

        is_search = "search" in tool.lower()
        if is_search and self._search_count() >= MAX_MCP_SEARCH_CALLS:
            return (f"[failure] Достигнут лимит поисковых вызовов MCP за запуск "
                    f"(не более {MAX_MCP_SEARCH_CALLS}). Дай итоговый ответ по уже найденным данным.")

        done_get_me = self._get_me_set()
        if tool == "get_me":
            done_get_me.add(server_id)
        elif is_search and server_id not in done_get_me and self._is_github_server(server_id, tool):
            done_get_me.add(server_id)
            try:
                self.ctx.call_mcp(server_id, "get_me", {})
                if self.state is not None:
                    self.state.add_note(f"MCP {server_id}:get_me")
            except Exception as pre_err:  # noqa: BLE001 — не блокируем поиск при отсутствии get_me у кастомного мока
                pre_code = (getattr(pre_err, "status_code", None)
                            or getattr(getattr(pre_err, "response", None), "status_code", None))
                if not getattr(pre_err, "_mcp_logged", False):
                    mcp_log.warning("MCP get_me перед поиском [%s] status=%s: %s",
                                    server_id, pre_code or "error", pre_err)

        if is_search:
            self._inc_search_count()

        try:
            result = self.ctx.call_mcp(server_id, tool, arguments)
        except Exception as error:  # noqa: BLE001
            status_code = (
                getattr(error, "status_code", None)
                or getattr(getattr(error, "response", None), "status_code", None)
            )
            err_text = str(error).strip() or type(error).__name__
            if status_code is None:
                m = _HTTP_STATUS_RE.search(err_text)
                if m:
                    status_code = int(m.group(1))
            self.last_error_code = status_code
            if not getattr(error, "_mcp_logged", False):
                mcp_log.error("MCP вызов не выполнен [%s:%s] status=%s: %s",
                              server_id, tool, status_code or "error", err_text[:500])
            prefix = f"MCP {server_id}:{tool}"
            if status_code and str(status_code) not in err_text:
                prefix += f" (HTTP {status_code})"
            return f"[failure] {prefix}: {err_text}"
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
