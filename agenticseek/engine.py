"""NovaMind agent engine built on the vendored AgenticSeek architecture.

What runs here
--------------
* :class:`agenticseek.router.AgentRouter` picks the executor: ``PlannerAgent``
  for multi-step requests, otherwise ``CasualAgent`` / ``CoderAgent`` /
  ``FileAgent`` / ``BrowserAgent``.
* The planner splits the goal into a ```json plan and hands each step to a
  specialist agent, passing previous results forward (AgenticSeek protocol).
* Specialists call tools with ```tag blocks; results go back into their memory.
* The tools are the vendored AgenticSeek interpreters (bash/python/file_finder)
  plus NovaMind capabilities (search, page reader, MCP, media, memory, tasks,
  schedules, skills) exposed through :class:`agenticseek.tools.nova_tools.BridgeTool`.

What the engine adds around the agents
--------------------------------------
The upstream project is a CLI, so it has no notion of an HTTP request.  The
engine supplies the request-scoped pieces:

* NDJSON progress events (``stage``/``plan``/``step``/``tool``/``sources``/…);
* cancellation (``/api/agent/stop``) and a hard deadline;
* a shared step budget, repeat detection, per-tool timeouts;
* confirmations for risky tools (``agenticseek.policy``);
* run persistence (``database.agent_runs``/``agent_run_steps``) so a reloaded
  page can still show what the agent did;
* a fallback answer when the model or the chosen agent fails: the user always
  gets a message, never a hanging stream.

It is deliberately free of Flask imports: the route passes callables in
(:class:`EngineContext`), which also makes the engine testable without HTTP.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import re
import queue as queue_mod
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

import database
from agenticseek import policy
from agenticseek.core.agent import Agent, clip, dumps, strip_unknown_blocks
from agenticseek.core.markup import sanitize_answer
from agenticseek.core.browser import create_browser
from agenticseek.core.browser_agent import BrowserAgent, NO_BROWSER_TEXT
from agenticseek.core.casual_agent import CasualAgent
from agenticseek.core.coder_agent import CoderAgent
from agenticseek.core.errors import AgenticSeekError, AgentStopped, StepLimitReached
from agenticseek.core.file_agent import FileAgent
from agenticseek.core.planner_agent import PlannerAgent
from agenticseek.core.project_agent import ProjectAgent
from agenticseek.core.prompt_paths import prompt_path
from agenticseek.core.utility import set_output_sink
from agenticseek.offline import OfflineTurn
from agenticseek.providers.nova_provider import NovaProvider
from agenticseek.router import AgentRouter, SHORT_MESSAGE_CHARS
from agenticseek.state import RunState
from agenticseek.timeline import recall_event, think_event
from agenticseek.tools import sandbox, shell_text
from agenticseek.tools.bash_interpreter import BashTool
from agenticseek.tools.file_finder import (FileEditTool, FileFinderTool, FileReadTool,
                                          FileWriteTool)
from agenticseek.tools.nova_tools import build_nova_tools
from agenticseek.tools.py_interpreter import PythonTool
from agenticseek.tools.web_tools import WebOpenTool, WebSearchTool

log = logging.getLogger("novamind.agenticseek")

MAX_STEPS = max(1, min(int(os.getenv("AGENT_MAX_STEPS", "20")), 60))

CLEAN_ANSWER_SYSTEM = (
    "Ты — NovaMind. Ответь пользователю только готовым ответом по-русски: "
    "без рассуждений, без английских черновиков и без пояснений о том, как ты думаешь. "
    "Если ниже есть результаты поиска — опирайся на них и перечисли найденное (название, ссылка, суть). Только если данных нет совсем — скажи это одной короткой фразой.")
#: Blocks that run code: they are never re-dispatched from a plain answer.
#: Запуск команд и кода. В ответе собеседника такой блок — пример, а не
#: действие: «как создать venv?» не должен выполнять свой же ответ. Правки
#: файлов сюда не входят — это всегда намерение, его передают программисту.
EXECUTION_TAGS = {"terminal", "bash", "python", "json"}
#: Tools that touch the machine. The web agent must not get them: its context is
#: untrusted page text, and shell access there is one prompt injection away from
#: running a stranger's command. Coding/file work goes to the agents built for it.
EXECUTION_TOOL_TAGS = {"terminal", "bash", "python", "file_find", "file_finder",
                       "file_read", "file_write", "file_edit"}
#: В демо-режиме (нет ключа провайдера) просьба «выполни команду» всё равно
#: должна показать настоящие шаги терминала, а не беспомощный ответ.
_OFFLINE_TERMINAL_HINT = re.compile(
    r"(?i)(?:выполни|запусти|терминал|рабоч(?:ая|ую|ей)\s+папк|linux|shell|bash|"
    r"команд|pwd|uname|whoami|git\s+status|\bls\b|\bdf\b|\bfree\b)")
_OFFLINE_TERMINAL_COMMANDS = (
    ("pwd", "pwd"), ("uname", "uname -s"), ("whoami", "whoami"),
    ("git status", "git status"), ("ls", "ls -la"), ("df", "df -h ."),
    ("free", "free -m"),
)
#: Просьба явно про MCP/подключённые сервисы — тогда «вспоминаем» и серверы.
#: Иначе показываем только общую строку «Вспоминаю инструменты», чтобы не
#: засорять таймлайн на обычном вопросе («сколько будет 6*7»).
_RECALL_MCP_HINT = re.compile(
    r"(?i)(?:\bmcp\b|github|гитхаб|notion|slack|jira|репозитор\w*|"
    r"пулл[- ]?реквест|issue|подключ\w*\s+сервер|сервер\w*\s+mcp|"
    r"инструмент\w*\s+mcp|вызови\s+инструмент|через\s+сервер)")


class _ThinkScope:
    """Пара событий ``think`` вокруг одного шага движка.

    Движок выполняет часть шагов сам (поиск по кнопке, офлайн-терминал,
    генерация медиа), минуя :meth:`Agent.run_tool`. Такой шаг тоже должен быть
    «обдуман» до и после — пользователь видит одинаковый ритм работы.
    """

    def __init__(self, emit, detail: str):
        self.emit = emit
        self.detail = detail

    def __enter__(self):
        self.emit(think_event("before", self.detail))
        return self

    def __exit__(self, exc_type, exc, tb):
        # Даже упавший шаг осмыслен: агент решает, что делать дальше, по факту
        # неудачи, а не продолжает вслепую.
        self.emit(think_event("after", self.detail, done=True))
        return False


def _offline_terminal_commands(goal: str) -> list[str]:
    """Безопасный набор Linux-команд для офлайн-демо (только allow-list)."""
    low = str(goal or "").lower()
    found = [command for marker, command in _OFFLINE_TERMINAL_COMMANDS if marker in low]
    if not found:
        found = ["pwd", "uname -s", "ls -la"]
    return found[:4]

#: Инструменты, которым нужна рабочая папка (в гостевом запуске их нет).
SANDBOX_TAGS = {"terminal", "python", "file_find", "file_read", "file_write", "file_edit"}
#: ```tag\npayload``` blocks of an answer.
_BLOCK = re.compile(r"```([a-zA-Z_][\w-]*)\n(.*?)```", re.S)
#: Human names of media kinds for the answer text.
KIND_RU = {"image": "изображение", "audio": "аудио", "video": "видео"}
AGENT_TIMEOUT = max(10.0, float(os.getenv("AGENT_TIMEOUT", "9999")))
SPEED = float(os.getenv("AGENTICSEEK_SPEED", "1.0"))     # <1 = faster, shorter turns


@dataclass
class EngineContext:
    """Everything the engine needs from the Flask request.

    Attributes:
        goal: user request.
        complete: NovaMind completion callable
            ``(messages, system=..., temperature=..., max_tokens=..., timeout=...)
            -> (text, meta)``.
        emit: event sink ``(event: dict) -> None``.
        provider/model: identifiers for diagnostics and persistence.
        cancel: ``threading.Event`` set by ``POST /api/agent/stop``.
        deadline: absolute ``time.monotonic()`` deadline (None = AGENT_TIMEOUT).
        history: previous chat messages for context.
        workspace: whether sandbox tools (bash/python/files) are allowed.
        search: ``search(query, limit) -> (results, meta)`` or None.
        read_page: ``reader(url, max_chars) -> (text, error)`` or None.
        runner: sandbox command runner ``(command, timeout=…) -> dict`` or None.
        tools_ctx: object with the NovaMind tool callbacks (see
            :mod:`agenticseek.tools.nova_tools`).
        memory_prompt: memory block appended to the system prompt.
        skills_prompt: skills block appended to the system prompt.
        strict_mcp: only the MCP tool is available in this run.
        finalize: ``(text) -> str`` answer sanitiser (reasoning-leak filters).
        media_available: shortcut for the state flag.
        tools_allowed: when False (auto-search button off) the engine registers
            no tools at all and the router sends the request straight to the AI
            model in one call.
    """

    goal: str
    complete: Callable[..., tuple]
    emit: Callable[[dict], None]
    provider: str = ""
    model: str = ""
    cancel: Optional[threading.Event] = None
    deadline: Optional[float] = None
    history: list = field(default_factory=list)
    workspace: bool = False
    search: Optional[Callable[..., tuple]] = None
    read_page: Optional[Callable[..., tuple]] = None
    runner: Optional[Callable[..., dict]] = None
    tools_ctx: object = None
    memory_prompt: str = ""
    skills_prompt: str = ""
    #: Фрагменты базы знаний (RAG) с цитатами [K1]; пусто — база не подключена.
    knowledge_prompt: str = ""
    #: Те же фрагменты в виде событий для интерфейса (``[{ref, title, excerpt}]``).
    #: Показываются сразу после первого шага, чтобы ход всё равно начинался
    #: со ``stage``: интерфейс ждёт этот порядок.
    knowledge_items: list = field(default_factory=list)
    #: Паттерн мультиагентной оркестрации: ``off`` (по умолчанию), ``auto``,
    #: ``sequential``, ``parallel``, ``critic``, ``router``. См.
    #: :mod:`agenticseek.orchestration`.
    pattern: str = "off"
    strict_mcp: bool = False
    finalize: Optional[Callable[[str], str]] = None
    salvage_answer: Optional[Callable[[str], str]] = None
    media_available: bool = False
    browser_mode: str = ""
    force_role: str = ""
    chat_id: str = ""
    run_id: str = ""
    profile: str = "default"      # ``default`` | ``coding`` (read-first project agent)
    media_kind: str = ""          # detected request for image/audio/video
    media_generate: Optional[Callable[..., tuple]] = None
    local_model: Optional[Callable[..., str]] = None
    offline: bool = False         # provider has no language model (local_demo)
    force_search: bool = False    # the «Поиск» button: search for real, before answering
    save_research: Optional[Callable[..., str]] = None
    tools_allowed: bool = True    # «Автопоиск» выключен → вообще без инструментов
    max_steps: int = MAX_STEPS
    timeout: float = AGENT_TIMEOUT


class AgentEngine:
    """Runs one NovaMind agent turn on top of the AgenticSeek agents."""

    def __init__(self, ctx: EngineContext):
        self.ctx = ctx
        self.state = RunState()
        self.provider = NovaProvider(ctx.complete, ctx.provider, ctx.model,
                                     max_tokens=int(1200 * max(0.6, min(SPEED, 2.0))))
        self.agents: dict[str, object] = {}
        self.router: Optional[AgentRouter] = None
        self.chosen = None
        self.answer = ""
        self._raw_answer = ""
        self.record_id = ""
        self._step_seq = 0
        self._budget = _Budget(ctx.max_steps)
        self._started = time.monotonic()
        self._search_observation = ""
        # Реальные теги инструментов, собранные _build_tools() на шаге
        # «вспоминаю инструменты». Пусто, пока инструменты не получены.
        self.recalled_tools: list[str] = []
        self._mcp_recalled = False

    # ── public API ────────────────────────────────────────────────────────
    def stream(self):
        """Run the turn and yield events as they happen.

        The agent runs in a worker thread because every step is blocking I/O
        (model calls, sandbox commands, page reads); the request thread only
        forwards events, so the browser sees progress immediately. If the client
        disconnects, the generator is closed, ``ctx.cancel`` is set and the
        worker stops at its next checkpoint.
        """
        queue: "queue_mod.Queue" = queue_mod.Queue()
        done = object()

        def emit(event: dict) -> None:
            if isinstance(event, dict):
                queue.put(event)

        self.ctx.emit = _tee(self.ctx.emit, emit)

        def work():
            try:
                self._execute()
            except BaseException as error:  # noqa: BLE001 — reported, then closed
                log.exception("agent worker crashed: %s", type(error).__name__)
                emit({"type": "error", "text": "Внутренняя ошибка агента"})
            finally:
                queue.put(done)

        # Поток агента наследует контекст запроса: рабочая папка и владелец пользователя.
        run_context = contextvars.copy_context()
        thread = threading.Thread(target=lambda: run_context.run(work),
                                  name="agenticseek-run", daemon=True)
        thread.start()
        try:
            while True:
                event = queue.get()
                if event is done:
                    break
                yield event
                if self.ctx.cancel is not None and self.ctx.cancel.is_set():
                    # Keep draining what the agent already produced.
                    continue
        finally:
            if self.ctx.cancel is not None and thread.is_alive():
                self.ctx.cancel.set()
            thread.join(timeout=5)

    def _execute(self) -> None:
        """The turn itself: understand, recall tools, run, finish.

        Порядок важен: сначала шаг понимания запроса, затем «вспоминание» и
        только после него — реальная сборка инструментов (:meth:`_build_agents`
        зовёт :meth:`_build_tools`). Так инструменты появляются у агента не
        заранее, а динамически, когда он решил, что они нужны.
        """
        self.record_id = self._start_run()
        self._understanding_stage()
        self._knowledge_stage()
        try:
            self._recall_start()          # анимация «вспоминаю …»
            self._build_agents()          # ← здесь инструменты реально грузятся
            self._recall_ready()          # «вспомнил», со списком подключённого
            self._run_agent()
        except AgentStopped as stop:
            self.state.status = "stopped"
            self.answer = self._partial_answer(stop)
            self._emit({"type": "step", "icon": "⏹", "text": stop.user_message})
        except AgenticSeekError as error:
            self.state.status = "error"
            self.state.error = error.user_message or error.message
            log.warning("agent run failed: %s", error.summary())
            self._emit({"type": "error", "text": self.state.error})
            self.answer = self._fallback_answer(error)
        except Exception as error:  # noqa: BLE001 — never leave the user without a reply
            self.state.status = "error"
            self.state.error = f"{type(error).__name__}: {error}"
            log.exception("agent engine crashed")
            self._emit({"type": "error", "text": "Внутренняя ошибка агента"})
            self.answer = self._fallback_answer(error)
        finally:
            set_output_sink(None)
            self._shutdown()

        self._raw_answer = str(self.answer or "")
        self.answer = self._finalize(self.answer)
        if not self.answer.strip() and self.state.status == "ok":
            # Модель ушла в рассуждения: просим чистый ответ и спасаем цитату.
            self.answer = self._recover_answer()
        if not self.answer.strip() and getattr(self, "_found", None):
            # поиск есть, ответа нет: отдаём найденное как есть
            self.answer = self._local_digest()
        if not self.answer.strip():
            self.state.status = "error"
            self.state.error = self.state.error or "Агент не вернул ответ"
            self.answer = ("Не удалось получить ответ. "
                           + (f"Причина: {self.state.error}." if self.state.error else ""))
        if self.state.sources:
            self._emit({"type": "sources", "sources": list(self.state.sources)})
        for chunk in _chunks(self.answer):
            self._emit({"type": "token", "token": chunk})
        self._finish_run()

    # ── шаг 1: понять запрос, шаг 2: вспомнить и получить инструменты ─────
    def _knowledge_stage(self) -> None:
        """Фрагменты базы знаний (RAG) — сразу после шага «понимаю запрос».

        Раньше они отправлялись из маршрута до запуска движка, и ход начинался
        с ``step``, а не со ``stage``; интерфейс рассчитывает на первое событие
        ``stage`` и терял постановку задачи.
        """
        items = list(getattr(self.ctx, "knowledge_items", None) or [])
        if not items:
            return
        self._emit({"type": "step", "icon": "📚",
                    "text": f"Нашёл в базе знаний фрагментов: {len(items)}"})
        self._emit({"type": "knowledge", "items": items})

    def _understanding_stage(self) -> None:
        """Первый шаг хода: понять запрос (сцена поиска — для кнопки «Поиск»)."""
        ctx = self.ctx
        searching = bool(ctx.force_search and ctx.search is not None
                         and ctx.tools_allowed)
        goal = clip(str(ctx.goal or "").strip(), 110)
        self._emit({
            "type": "stage",
            "scene": "search" if searching else "agent",
            "title": "Поиск в интернете" if searching else "Понимаю запрос",
            "text": (f"Разбираю задачу «{goal}» и решаю, что понадобится"
                     if goal else "Разбираю задачу и решаю, что понадобится"),
        })

    def _capability_groups(self) -> list[tuple[str, str]]:
        """Какие группы инструментов вообще могут быть в этом запросе.

        Это только анонс для анимации: реальные объекты инструментов создаются
        позже в :meth:`_build_tools`, по тем же признакам контекста.
        """
        ctx = self.ctx
        groups: list[tuple[str, str]] = []
        if ctx.search is not None:
            groups.append(("web_search", "поиск в интернете"))
        if ctx.read_page is not None:
            groups.append(("web_open", "чтение страниц"))
        if ctx.workspace:
            groups.append(("terminal", "Linux-команды и терминал"))
            groups.append(("python", "запуск Python"))
            groups.append(("file_read / file_write / file_edit", "файлы песочницы"))
        if ctx.tools_ctx is not None:
            groups.append(("память, задачи, навыки", "возможности NovaMind"))
            if any(getattr(ctx.tools_ctx, f"media_{kind}_available", False)
                   for kind in ("image", "audio", "video")):
                groups.append(("медиа", "картинки, аудио, видео"))
            if getattr(ctx.tools_ctx, "mcp_tools", None):
                groups.append(("mcp_call", "MCP-инструменты"))
        return groups

    def _mcp_needed(self) -> bool:
        """Нужно ли отдельно «вспоминать MCP-серверы» в этом ходе."""
        ctx = self.ctx
        return bool(ctx.strict_mcp
                    or _RECALL_MCP_HINT.search(str(ctx.goal or ""))
                    or getattr(ctx.tools_ctx, "mcp_tools", None))

    def _recall_start(self) -> None:
        """Живые строки «Вспоминаю инструменты…» / «Вспоминаю MCP-серверы…».

        События приходят ДО :meth:`_build_agents`, поэтому анимация идёт именно
        во время получения инструментов, а не после него. Когда автопоиск
        выключен, инструментов нет вообще — показываем честную строку об этом.
        """
        ctx = self.ctx
        if not ctx.tools_allowed:
            self._emit(recall_event(
                "plain", "Инструменты не нужны", icon="💬",
                detail="Автопоиск выключен: ни одного инструмента, запрос уходит в ИИ-модель",
                done=True))
            self._emit({"type": "step", "icon": "💬",
                        "text": "Понял запрос: отвечаю напрямую, без инструментов"})
            return
        groups = self._capability_groups()
        self._emit(recall_event(
            "tools", "Вспоминаю инструменты", icon="🧰",
            detail=", ".join(description for _name, description in groups)
                   or "базовые инструменты ответа",
            items=[name for name, _description in groups]))
        self._mcp_recalled = self._mcp_needed()
        if self._mcp_recalled:
            connected = list(getattr(ctx.tools_ctx, "mcp_tools", None) or [])
            servers = []
            for item in connected:
                if isinstance(item, dict):
                    name = str(item.get("server_id") or item.get("server")
                               or item.get("name") or "").strip()
                    if name and name not in servers:
                        servers.append(name)
            self._emit(recall_event(
                "mcp", "Вспоминаю MCP-серверы", icon="🔌",
                detail=(f"Подключено MCP-инструментов: {len(connected)}. "
                        "Проверяю, какие серверы доступны"
                        if connected else "Проверяю подключённые MCP-серверы"),
                items=servers or ["список серверов"]))

    def _recall_ready(self) -> None:
        """Инструменты получены: закрываем анимацию реальным списком тегов."""
        ctx = self.ctx
        if not ctx.tools_allowed:
            return
        names = sorted(set(self.recalled_tools)) or [
            name for name, _description in self._capability_groups()]
        self._emit(recall_event(
            "tools", "Вспомнил инструменты", icon="✅",
            detail=f"Подключено для этого запроса: {len(names)}",
            items=names, done=True))
        if self._mcp_recalled:
            self._emit(recall_event(
                "mcp", "Вспомнил MCP-серверы", icon="✅",
                detail="Список MCP-инструментов обновлён" if
                       getattr(ctx.tools_ctx, "mcp_tools", None) else
                       "Серверы проверены; при необходимости подключите их в Настройках",
                done=True))
        self._emit(recall_event(
            "ready", "Инструменты наготове", icon="✅",
            detail=f"Получено инструментов: {len(names)}", items=names, done=True))
        self._emit({"type": "step", "icon": "🧰",
                    "text": "Получил инструменты под задачу: "
                            + (", ".join(names[:8]) or "нет")})

    def _thinking(self, detail: str) -> _ThinkScope:
        """Обернуть шаг движка парой «думаю → обдумываю результат»."""
        return _ThinkScope(self._emit, detail)

    # ── assembly ──────────────────────────────────────────────────────────
    def _build_agents(self) -> None:
        ctx = self.ctx
        name = "NovaMind"
        if not ctx.tools_allowed:
            # Кнопка автопоиска выключена: ни одного инструмента, один прямой
            # запрос к модели. Роутер всё равно выбирает исполнителя — но у него
            # есть только безоружный собеседник.
            self._build_direct_agent(name)
            return
        if ctx.profile == "coding":
            self._build_coding_agents(name)
            return
        # Explicit roles and strict MCP preserve their narrower capabilities.
        mode = os.getenv("AGENTICSEEK_EXECUTION_MODE", "unified").strip().lower()
        conversation_only = bool(re.fullmatch(
            r"\s*(?:привет|здравствуй(?:те)?|спасибо|как дела|hi|hello|thanks)[!?.\s]*",
            ctx.goal, re.I))
        if (mode == "unified" and not ctx.force_role and not ctx.strict_mcp
                and not conversation_only):
            self._build_orchestrator(name)
            return
        browser = self._build_browser()

        planner = PlannerAgent(name, prompt_path("planner_agent"), self.provider,
                               browser=browser,
                               web_agent_factory=lambda *args: BrowserAgent(*args))
        casual = CasualAgent(name, prompt_path("casual_agent"), self.provider)
        coder = CoderAgent(name, prompt_path("coder_agent"), self.provider)
        file_agent = FileAgent(name, prompt_path("file_agent"), self.provider)
        web = BrowserAgent(name, prompt_path("browser_agent"), self.provider, browser=browser)

        agents = [planner, casual, coder, file_agent, web]
        self.agents = {agent.type: agent for agent in agents}

        shared = self._build_tools()
        self._attach_shared(planner, agents, shared)
        if not ctx.workspace:
            # Keep the tool set honest: without the sandbox there is nothing to run.
            # Убираем и алиасы (```bash```, ```file_finder```): иначе гостевой
            # промпт обещает инструменты, которых в этом запуске нет.
            for agent in agents:
                for tag in list(agent.tools):
                    tool = agent.tools[tag]
                    if getattr(tool, "tag", tag) in SANDBOX_TAGS:
                        agent.tools.pop(tag, None)
            planner.agents.pop("coder", None)
            planner.agents.pop("file", None)

        from agenticseek.tools.terminal_commands import HELP as terminal_help
        extras = "\n\n".join(part for part in (
            ctx.memory_prompt, ctx.knowledge_prompt, ctx.skills_prompt,
            terminal_help if ctx.workspace and not ctx.strict_mcp else "",
        ) if part)
        if extras:
            for agent in agents:
                agent.extra_context = extras

        deadline = ctx.deadline or (time.monotonic() + ctx.timeout)
        budget = _Budget(ctx.max_steps)
        confirm = self._confirm_hook()
        record = self._record_hook()
        for agent in agents:
            agent.attach(events=self._emit, cancel=ctx.cancel, deadline=deadline,
                         step_budget=budget, confirm=confirm, record=record,
                         state=self.state)
        self._budget = budget
        self.router = AgentRouter(agents, classify=self._classify,
                                  mode=os.getenv("AGENTICSEEK_ROUTER", "heuristic"))

    def _build_direct_agent(self, name: str) -> None:
        """Auto-search off: register a single tool-free conversation agent.

        Nothing from the request can reach the terminal, web, MCP, media, files
        or memory tools because none are constructed. :class:`AgentRouter` with
        one agent returns it without any classification call, so the request
        goes straight to the AI model exactly once.
        """
        ctx = self.ctx
        agent = CasualAgent(name, prompt_path("casual_agent"), self.provider)
        agent.tools = {}
        agent.alias_tools = {}
        agent.extra_context = (
            "Инструменты отключены пользователем (кнопка автопоиска выключена). "
            "Не вызывай инструменты, не пиши блоки ```tag```, теги <tool_code> "
            "и JSON-действия. Отвечай на языке пользователя только готовым текстом.")
        for block in (ctx.memory_prompt, ctx.knowledge_prompt):
            if block:
                agent.extra_context += "\n\n" + block
        agent.attach(events=self._emit, cancel=ctx.cancel,
                     deadline=ctx.deadline or (time.monotonic() + ctx.timeout),
                     step_budget=self._budget, confirm=None,
                     record=self._record_hook(), state=self.state)
        self.agents = {agent.type: agent}
        self.router = AgentRouter([agent])
        self._emit({"type": "step", "icon": "📨",
                    "text": "Автопоиск выключен: инструменты не используются, "
                            "запрос уходит прямо в ИИ-модель"})

    def _run_direct(self) -> None:
        """One model call with chat history, zero tools and zero side effects."""
        ctx = self.ctx
        self._emit({"type": "stage", "scene": "agent", "title": "Ответ модели",
                    "text": "Отправляю запрос напрямую в ИИ-модель"})
        agent = self.router.select_agent(ctx.goal) if self.router else None
        if agent is None:
            raise AgenticSeekError("No agent available",
                                   "Нет доступного исполнителя для запроса.")
        self.chosen = agent
        # История — настоящими сообщениями, а не склейкой в одну строку.
        history = [item for item in (ctx.history or [])
                   if isinstance(item, dict) and item.get("role") in ("user", "assistant")
                   and str(item.get("content") or "").strip()][-6:]
        for item in history:
            agent.memory.push(item["role"], clip(str(item["content"]), 1200))
        watcher = self._start_stop_watcher(agent)
        try:
            answer, _summary = agent.process(ctx.goal, None)
        finally:
            self._stop_watcher(watcher)
        # Модель могла написать блок инструмента — он не выполнен. Блоки с
        # неизвестными тегами вырезаются; обычные примеры кода (```python,
        # ```bash) остаются текстом ответа и NEVER исполняются.
        raw_answer = str(answer or "")
        answer = strip_unknown_blocks(raw_answer, set()).strip()
        if not answer and _is_tool_call_answer(raw_answer):
            # Весь ответ — попытка вызвать инструмент. Инструментов нет, поэтому
            # просим модель ответить словами (ещё один запрос, без инструментов).
            self._emit({"type": "step", "icon": "ℹ️",
                        "text": "Модель вызвала инструмент — он отключён, прошу текстовый ответ"})
            messages = [
                {"role": "system", "content": CLEAN_ANSWER_SYSTEM},
                {"role": "user", "content": str(ctx.goal or "")},
            ]
            try:
                answer = strip_unknown_blocks(str(self.provider.respond(messages)), set()).strip()
            except AgentStopped:
                raise
            except Exception:  # noqa: BLE001 — ответ всё равно будет через recovery
                answer = ""
            if _is_tool_call_answer(answer):
                answer = ""
        self.answer = answer
        self.state.steps = 0
        if ctx.cancel is not None and ctx.cancel.is_set():
            self.state.status = "stopped"

    def _build_orchestrator(self, name: str) -> None:
        """One model owns the goal and selects request-scoped tools itself."""
        from agenticseek.core.orchestrator_agent import OrchestratorAgent
        from agenticseek.tools.terminal_commands import HELP as terminal_help

        ctx = self.ctx
        agent = OrchestratorAgent(
            name, prompt_path("orchestrator_agent"), self.provider,
            self._build_tools(), max_turns=ctx.max_steps,
        )
        agent.extra_context = "\n\n".join(
            part for part in (ctx.memory_prompt, ctx.knowledge_prompt,
                              ctx.skills_prompt) if part)
        # Operational instructions are separate from untrusted remembered facts.
        if ctx.workspace:
            agent.prompt += "\n\n" + terminal_help
        agent.attach(events=self._emit, cancel=ctx.cancel,
                     deadline=ctx.deadline or (time.monotonic() + ctx.timeout),
                     step_budget=self._budget, confirm=self._confirm_hook(),
                     record=self._record_hook(), state=self.state)
        self.agents = {agent.type: agent}
        self.router = AgentRouter([agent])

    def _build_coding_agents(self, name: str) -> None:
        """Coding profile: one read-first agent over the repository."""
        agent = ProjectAgent(name, prompt_path("project_agent"), self.provider)
        self.agents = {agent.type: agent}
        self.recalled_tools = sorted(getattr(agent, "tools", {}) or {})
        deadline = self.ctx.deadline or (time.monotonic() + self.ctx.timeout)
        agent.attach(events=self._emit, cancel=self.ctx.cancel, deadline=deadline,
                     step_budget=self._budget, confirm=self._confirm_hook(),
                     record=self._record_hook(), state=self.state)
        if self.ctx.memory_prompt:
            agent.extra_context = self.ctx.memory_prompt
        self.router = AgentRouter([agent])

    def _build_tools(self) -> dict:
        """Tools the current run may use.

        A capability is registered only when the route supplied its callback,
        so the model never sees a tool it cannot actually run. When auto-search
        is switched off (``ctx.tools_allowed`` is False) this returns an empty
        dict: the router has only a tool-free conversation agent and the
        request goes straight to the AI model.
        """
        ctx = self.ctx
        if not ctx.tools_allowed:
            self.recalled_tools = []
            return {}
        tools: dict = {}
        if ctx.search is not None:
            tools["web_search"] = WebSearchTool(ctx.search, state=self.state)
        if ctx.read_page is not None:
            tools["web_open"] = WebOpenTool(ctx.read_page, state=self.state)
        if ctx.workspace:
            # Linux-команды и шаги агента исполняются терминальной песочницей.
            tools["terminal"] = BashTool()
            tools["python"] = PythonTool()
            tools["file_read"] = FileReadTool()
            tools["file_write"] = FileWriteTool()
            tools["file_find"] = FileFinderTool()
            tools["file_edit"] = FileEditTool()
        if ctx.tools_ctx is not None:
            tools.update(build_nova_tools(ctx.tools_ctx, state=self.state,
                                          emit=self._emit))
        # Динамическое получение инструментов завершено: запоминаем реальный
        # список для события «Вспомнил инструменты» (а не выдуманный анонс).
        self.recalled_tools = sorted({str(getattr(tool, "tag", tag) or tag)
                                      for tag, tool in tools.items()})
        return tools


    @staticmethod
    def _with_aliases(tools: dict) -> dict:
        """Инструменты + старые теги (``bash``, ``file_finder``): промпты и
        сохранённые навыки могут ссылаться на них, а блоки не должны теряться."""
        merged = dict(tools)
        for tool in list(tools.values()):
            for alias in getattr(tool, "aliases", ()) or ():
                merged.setdefault(alias, tool)
        return merged

    def _attach_shared(self, planner, agents, shared: dict) -> None:
        ctx = self.ctx
        shared = self._with_aliases(shared)
        if ctx.strict_mcp:
            only = {tag: tool for tag, tool in shared.items() if tag == "mcp_call"}
            if only:
                shared = only
                # В строгом режиме реально доступен только mcp_call — список
                # «вспомнил инструменты» должен быть честным.
                self.recalled_tools = ["mcp_call"]
                for agent in agents:
                    if agent.role != "talk":
                        agent.tools = dict(shared)
                self._emit({"type": "step", "icon": "🔒",
                            "text": "Строгий режим MCP: доступен только mcp_call"})
            else:
                self._emit({"type": "step", "icon": "⚠️",
                            "text": "MCP не подключён: строгий режим снят, работаю обычными "
                                    "инструментами. Подключи сервер в Настройках → MCP"})
        is_short_chat = len(str(ctx.goal or "").strip()) <= SHORT_MESSAGE_CHARS
        for agent in agents:
            if agent.role == "talk" and is_short_chat:
                continue
            for tag, tool in shared.items():
                canonical = getattr(tool, "tag", tag) or tag
                if agent.role in ("web", "talk") and canonical in EXECUTION_TOOL_TAGS:
                    continue
                agent.register_tool(tag, tool)
        planner.add_shared_tools({tag: tool for tag, tool in shared.items()})

    def _build_browser(self):
        ctx = self.ctx
        mode = (ctx.browser_mode or os.getenv("AGENTICSEEK_BROWSER", "auto")).strip().lower()
        if not ctx.read_page and mode in ("auto", "http"):
            return None
        try:
            return create_browser(mode, read_page=ctx.read_page)
        except AgenticSeekError as error:
            self._emit({"type": "step", "icon": "⚠️",
                        "text": clip(error.user_message or error.message, 200)})
            return None

    # ── hooks ─────────────────────────────────────────────────────────────
    def _emit(self, event: dict) -> None:
        try:
            self.ctx.emit(event)
        except Exception:  # noqa: BLE001 — a closed stream must not kill the run
            log.debug("event sink failed", exc_info=True)

    def _confirm_hook(self):
        ctx = self.ctx
        if policy.confirm_mode() == "off":
            return None

        def confirm(tool, payload, risk="write"):
            """Ask the user before a risky action; refused unless approved."""
            if not policy.needs_confirmation(tool, payload):
                return True
            record = policy.register_confirmation(tool, payload)
            self._emit({"type": "confirm", "id": record["id"], "tool": record["tool"],
                        "description": record["description"], "risk": record["risk"],
                        "command": record.get("command") or record["description"],
                        # Карточка показывает обратный отсчёт: иначе непонятно, что
                        # вопрос вообще закроется и через сколько.
                        "timeout": policy.confirm_timeout()})
            self._emit({"type": "step", "icon": "🔐",
                        "text": f"Жду подтверждения: {record['description'][:160]}"})
            approved = policy.await_confirmation(record)
            self._emit({"type": "confirm_result", "id": record["id"], "approved": approved})
            if approved:
                self._emit({"type": "step", "icon": "✅", "text": "Разрешено"})
            else:
                # Молчаливое «Запрещено» выглядело как поломка: пользователь не
                # понимал, что карточку нужно нажать и что окно ожидания закрывается.
                waited = policy.confirm_timeout()
                self._emit({"type": "step", "icon": "🚫",
                            "text": "Действие не выполнено: разрешение не получено"
                                    + (f" (ждал {waited:.0f} c)" if waited >= 1 else "") + ". "
                                    "Повторите запрос и нажмите «Разрешить» в карточке "
                                    "«Нужно разрешение» — она в этом же сообщении."})
            return approved

        return confirm

    def _record_hook(self):
        """Persist every executed block (no secrets, payload truncated)."""
        def record(tool, payload, ok, feedback):
            tag = getattr(tool, "tag", str(tool))
            policy.log_action(tag, payload, ok)
            self._step_seq += 1
            try:
                database.add_agent_run_step(
                    self.record_id, self._step_seq, tag,
                    label=policy.redact(str(payload or ""))[:200], ok=ok,
                    output=str(feedback or "")[:1500])
            except Exception:  # noqa: BLE001 — persistence must not stop the run
                log.warning("cannot persist the agent step", exc_info=True)
        return record

    def _classify(self, prompt: str) -> str:
        """One short model call for the router (AGENTICSEEK_ROUTER=llm)."""
        try:
            text, _meta = self.ctx.complete(
                [{"role": "user", "content": prompt}],
                system="Ты — классификатор запросов. Ответь одним словом.",
                temperature=0.0, max_tokens=8, timeout=20)
            return str(text or "")
        except Exception:  # noqa: BLE001 — routing falls back to the heuristic
            return ""

    # ── execution ─────────────────────────────────────────────────────────
    def _run_agent(self) -> None:
        ctx = self.ctx
        if not ctx.tools_allowed:
            # Ни медиа, ни офлайн-поиск, ни автопоиск, ни выбор инструментов.
            self._run_direct()
            return
        if ctx.media_kind and ctx.media_generate is not None:
            self._run_media()
            return
        if ctx.offline or ctx.provider == "local_demo":
            if (ctx.workspace and ctx.runner is not None
                    and _OFFLINE_TERMINAL_HINT.search(ctx.goal)):
                self._run_offline_terminal()
            else:
                self._run_offline()
            return
        if ctx.force_search:
            self._search_observation = self._run_forced_search()
        # Мультиагентные паттерны: только если их включили (AGENT_PATTERN) и
        # запрос действительно сложный. Иначе — прежний путь с одним
        # исполнителем, чтобы простые вопросы не стоили втрое дороже.
        if self._pattern_requested():
            self._run_orchestrated()
            return
        agent = (self.router.by_role(ctx.force_role) if ctx.force_role else None)
        if (agent is None and getattr(self, "_search_observation", "")
                and not _needs_tools(ctx.goal)):
            agent = (self.router.by_role("talk") or self.router.by_type("casual_agent")
                     or (self.router.agents[0] if self.router.agents else None))
            self.router.last_reason = "поиск → ответ по источникам"
        if agent is None:
            agent = self.router.select_agent(ctx.goal)
        if agent is None:
            raise AgenticSeekError("No agent available",
                                   "Нет доступного исполнителя для запроса.")
        self.chosen = agent
        self._emit({"type": "stage", "scene": "agent",
                    "title": f"Агент: {agent.label}",
                    "text": f"Маршрут: {self.router.last_reason or agent.role}"})
        self._emit({"type": "step", "icon": "🧭",
                    "text": f"{agent.label}: {self.router.last_reason or agent.role}"})

        watcher = self._start_stop_watcher(agent)
        try:
            prompt = self._goal_with_context()

            answer, _summary = agent.process(prompt, None)
            missing = self._missing_tools(agent, answer)
            # В разговоре блок ```bash — пример, а не действие: такой ход не
            # переводим программисту, иначе «как создать venv?» выполнит ответ.
            movable = tuple(tag for tag in missing
                            if getattr(agent, "role", "") != "talk"
                            or tag not in EXECUTION_TAGS)
            fallback = self._tool_agent(movable) if movable else None
            if fallback is not None and fallback is not agent:
                # Маршрутизатор ошибся: модель хочет действие, которого у
                # исполнителя нет (веб-агент пытается выполнить bash). Передаём
                # задачу тому, у кого инструмент есть, вместо того чтобы
                # показывать пользователю невыполненный блок.
                self._emit({"type": "step", "icon": "🔁",
                            "text": f"Передаю задачу исполнителю: {fallback.label}"})
                agent = fallback
                self.chosen = agent
                answer, _summary = agent.continue_with(answer, prompt)
            elif any(tag in EXECUTION_TAGS for tag in missing):
                # Выполнить некому — честно говорим, что команда не запускалась.
                self._emit({"type": "step", "icon": "ℹ️",
                            "text": "Команды в ответе показаны как пример и не выполнялись"})
            cleaned = strip_unknown_blocks(answer, set(getattr(agent, "tools", None) or {}))
            if cleaned != str(answer or "").strip():
                # Блок недоступного инструмента — не выполнение: убираем его из
                # ответа и честно говорим, что действие не произошло.
                self._emit({"type": "step", "icon": "⚠️",
                            "text": "Модель вызвала недоступный инструмент — действие не выполнено"})
                answer = cleaned
            if self._search_observation and _is_tool_only(answer):
                answer = self._answer_from_observation(agent)
        finally:
            self._stop_watcher(watcher)

        if ctx.cancel is not None and ctx.cancel.is_set():
            self.state.status = "stopped"
        self.answer = str(answer or "").strip()
        self.state.steps = self._budget.used
        self._emit({"type": "step", "icon": "🏁",
                    "text": f"Готово · шагов: {self.state.steps} · модель: {ctx.model or '?'}"})

    def _pattern_requested(self) -> bool:
        """Нужен ли мультиагентный паттерн для этого запроса."""
        ctx = self.ctx
        pattern = str(getattr(ctx, "pattern", "off") or "off").strip().lower()
        if pattern in ("", "off") or not ctx.tools_allowed:
            return False
        if ctx.offline or ctx.media_kind or ctx.force_role:
            return False
        from agenticseek.orchestration import PATTERNS, choose_pattern
        from agenticseek.router import looks_complex
        if pattern not in PATTERNS:
            return False
        resolved = choose_pattern(ctx.goal) if pattern == "auto" else pattern
        # Короткий разговорный запрос не требует нескольких ролей даже при
        # включённом паттерне: цена важнее «красоты» схемы.
        return bool(looks_complex(ctx.goal)) or resolved == "critic"

    def _run_orchestrated(self) -> None:
        """Ход по паттерну: роли, синтез, критика (см. orchestration.py)."""
        import os
        from agenticseek.orchestration import run as run_pattern
        ctx = self.ctx
        pattern = str(getattr(ctx, "pattern", "auto") or "auto")
        self._emit({"type": "step", "icon": "🧠",
                    "text": f"Мультиагентный режим: {pattern}"})
        # Поиск здесь не запускаем: он уже выполнен, если пользователь нажал
        # «Поиск» (force_search), и его данные попали в цель. Дополнительный
        # поиск на каждый мультиагентный ход удваивал время и расход токенов.
        goal = self._goal_with_context()
        try:
            rounds = int(os.getenv("AGENT_PATTERN_ROUNDS", "2"))
        except ValueError:
            rounds = 2
        result = run_pattern(ctx.complete, goal, pattern=pattern,
                             history=ctx.history, emit=self._emit,
                             max_rounds=max(1, min(rounds, 4)))
        if not result.ok:
            self._emit({"type": "step", "icon": "⚠️",
                        "text": "Паттерн не дал ответа — работаю обычным путём"})
            if self.router is not None:
                self._run_agent_single()
                return
            raise AgenticSeekError("orchestration", "Не удалось получить ответ")
        self.answer = result.answer
        self.state.steps = max(1, len(result.steps))
        self._emit({"type": "step", "icon": "🏁",
                    "text": f"Оркестрация «{result.pattern}»: ролей {len(result.steps)}, "
                            f"шагов {self.state.steps}"})
        for note in result.notes[-3:]:
            self._emit({"type": "step", "icon": "•", "text": clip(note, 200)})
        if result.verdict:
            self._emit({"type": "thought", "done": True, "label": "Проверка критиком",
                        "text": clip(result.verdict, 600)})

    def _run_agent_single(self) -> None:
        """Обычный путь: один исполнитель (без рекурсии в паттерн)."""
        previous = getattr(self.ctx, "pattern", "off")
        try:
            self.ctx.pattern = "off"
            self._run_agent()
        finally:
            self.ctx.pattern = previous

    def _run_media(self) -> None:
        """Explicit "нарисуй …" request: generate directly, no model needed.

        The prompt is already in the user's message, so asking a model to call
        the media tool would only add latency and cost (and small models often
        answer with text instead of calling the tool).
        """
        ctx = self.ctx
        kind = ctx.media_kind
        label = KIND_RU.get(kind, kind)
        self._emit({"type": "stage", "scene": "media", "title": "Генерирую медиа",
                    "text": f"{label}: запрос принят"})
        with self._thinking(f"создать {label}"):
            try:
                result, model, errors = ctx.media_generate(kind, ctx.goal, {})
            except Exception as error:  # noqa: BLE001 — reported honestly
                result, model, errors = None, None, [f"{type(error).__name__}: {error}"]
        if result:
            self.state.add_media(result, model)
            self._emit({"type": "media", "media": result, "model": model})
            state = str(result.get("state") or "").lower()
            if result.get("job_id") and state in ("queued", "pending", "processing", ""):
                # Провайдер рендерит видео асинхронно: работа ещё не готова.
                self.answer = (f"Готово: задание на {label} создано "
                               f"(id {result['job_id']}), оно в очереди у провайдера. "
                               "Файл появится здесь, когда рендер закончится.")
            else:
                self.answer = f"Готово: {label} создан и показан выше."
        else:
            reason = "; ".join(str(item) for item in (errors or [])[-2:]) or "провайдер не ответил"
            self._emit({"type": "step", "icon": "⚠️",
                        "text": f"Не получилось создать {label}: {reason}"})
            self.answer = (f"Не получилось создать {label}: {reason}.\n\n"
                           "Проверьте настройки медиа в настройках или подключите ключ.")
        self.state.steps = 1

    def _run_forced_search(self) -> str:
        """Search because the user pressed «Поиск», not because the model asked.

        The button used to add a hint to the prompt only: when the model decided
        to answer from memory, no search happened and the button did nothing.
        Here the search is unconditional — results, page reads, a note in the
        workspace — and the observation is put into the goal, so the answer is
        built on fresh facts.

        Returns:
            The observation block for the model (empty when search is impossible).
        """
        ctx = self.ctx
        if ctx.search is None:
            return ""
        goal = ctx.goal
        self._emit({"type": "stage", "scene": "search", "title": "Поиск в интернете",
                    "text": "Ищу источники по запросу…"})
        if ctx.cancel is not None and ctx.cancel.is_set():
            return ""
        try:
            results, _meta = ctx.search(_search_query(goal), 5)
        except Exception as error:  # noqa: BLE001 — the answer must still happen
            log.warning("forced search failed: %s", type(error).__name__)
            self._emit({"type": "step", "icon": "⚠️",
                        "text": f"Поиск не удался: {type(error).__name__}"})
            return ""
        results = [item for item in (results or []) if isinstance(item, dict)]
        self._found = results
        if not results:
            self._emit({"type": "step", "icon": "⚠️",
                        "text": f"Поиск «{goal}» ничего не дал"})
            return ("Результаты поиска: поиск ничего не нашёл. Отвечай по своим знаниям "
                    "и честно скажи, что свежих данных получить не удалось.")
        for item in results:
            url = str(item.get("url") or "")
            if url:
                self.state.add_source(url, str(item.get("title") or url))
        lines = "\n".join(
            f"[{index}] {item.get('title') or item.get('host') or item.get('url')}\n"
            f"    {item.get('url', '')}\n    {clip(str(item.get('snippet') or ''), 240)}"
            for index, item in enumerate(results, 1)
        )
        self._emit({"type": "step", "icon": "🔎",
                    "text": f"Нашёл {len(results)} источников по запросу «{goal}»"})
        search_step = f"nova search {goal}"
        self._emit(think_event("before", search_step))
        self._emit({"type": "tool", "name": "web_search", "command": search_step,
                    "code": 0, "output": lines, "results": results})
        self._emit(think_event("after", search_step, done=True))

        pages = []
        for item in results[:3]:
            if ctx.cancel is not None and ctx.cancel.is_set():
                break
            url = str(item.get("url") or "")
            if ctx.read_page is None or not url.startswith(("http://", "https://")):
                continue
            # Думаем, что делать после чтения страницы, и осмысляем результат.
            with self._thinking(f"nova read {url}"):
                try:
                    text, _elapsed = ctx.read_page(url)
                except Exception:  # noqa: BLE001 — a page error is not a run failure
                    continue
                if not text:
                    continue
                pages.append(f"--- {url} ---\n{clip(text, 2000)}")
                self._emit({"type": "tool", "name": "web_open", "command": url, "code": 0,
                            "output": clip(text, 1200)})

        if ctx.save_research is not None:
            body = (f"# {goal}\n\n_поиск {time.strftime('%Y-%m-%d %H:%M')}_\n\n{lines}\n\n"
                    + "\n\n".join(pages))
            try:
                saved = ctx.save_research(goal, body, "search")
            except Exception as error:  # noqa: BLE001
                log.warning("research note not saved: %s", type(error).__name__)
            else:
                if saved:
                    self._emit({"type": "step", "icon": "💾", "text": f"Сохранил {saved}"})

        return ("Результаты поиска (доверяй им больше, чем памяти модели):\n\n" + lines
                + ("\n\nПрочитанные страницы:\n\n" + "\n\n".join(pages) if pages else "")
                + "\n\nВАЖНО: данных достаточно. Ответь пользователю сейчас обычным "
                  "текстом по-русски, со ссылками на номера источников [1], [2]. "
                  "НЕ вызывай web_search и web_open, не пиши теги и блоки.")

    def _run_offline_terminal(self) -> None:
        """Demo mode (no LLM key): still perform real steps in the Linux sandbox.

        Без языковой модели некому придумывать следующий шаг, поэтому набор
        команд детерминирован. Все они идут через тот же runner песочницы, что
        и обычные шаги агента: allow-list, лимиты, личная рабочая папка.
        """
        from agenticseek.timeline import tool_event

        ctx = self.ctx
        self._emit({"type": "stage", "scene": "agent", "title": "Терминал",
                    "text": "Выполняю шаги Linux-командами в песочнице"})
        lines = []
        for index, command in enumerate(_offline_terminal_commands(ctx.goal), 1):
            if ctx.cancel is not None and ctx.cancel.is_set():
                raise AgentStopped("cancelled")
            # Каждый шаг терминала: думаем, что делать дальше → выполняем →
            # обдумываем результат и выбираем следующую команду.
            with self._thinking(command):
                try:
                    result = ctx.runner(command, timeout=20) if ctx.runner is not None else None
                except Exception as error:  # noqa: BLE001 — команда не должна ронять ход
                    result = {"code": 2, "stdout": "", "stderr": str(error)}
                result = result or {"code": 2, "stdout": "", "stderr": "Песочница недоступна"}
                code = int(result.get("code") or 0)
                output = ((result.get("stdout") or "") + (result.get("stderr") or "")).strip()
                ok = code == 0 and not result.get("timed_out")
                self._emit(tool_event("terminal", command, ok=ok, command=command,
                                      output=clip(output, 1200), code=code))
                self.state.add_tool("terminal", ok)   # StepState сам считает шаги
            lines.append(
                f"{index}. `{command}` → {'успех' if ok else f'код {code}'}"
                + (f":\n```text\n{clip(output, 700)}\n```" if output else ""))
        self.answer = (
            "Выполнил шаги в терминале песочницы (демо-режим без языковой модели):\n\n"
            + "\n\n".join(lines)
            + "\n\nПодключите ключ провайдера — тогда ИИ сможет сам выбирать "
              "следующую команду по результату каждого шага.")

    def _run_offline(self) -> None:
        """No language model: real search/media, honest answers (see offline.py)."""
        ctx = self.ctx
        self._emit({"type": "stage", "scene": "agent", "title": "Офлайн-режим",
                    "text": "Языковой модели нет — работаю без неё"})
        turn = OfflineTurn(ctx.goal, search=ctx.search, media=ctx.media_generate,
                           media_kind=ctx.media_kind, local_model=ctx.local_model,
                           emit=self._emit, state=self.state, force_search=ctx.force_search)
        self.answer = turn.run()
        self.state.steps = 1
        self._emit({"type": "step", "icon": "ℹ️",
                    "text": "Офлайн-режим: подключите ключ провайдера для полного агента"})

    def _tool_agent(self, required=()):
        """First agent of this run that can run every tool in ``required``."""
        required = tuple(required or ())
        for role in ("code", "files", "web"):
            agent = self.router.by_role(role)
            tools = agent.all_tools() if hasattr(agent, "all_tools") else \
                (getattr(agent, "tools", None) or {})
            if agent is not None and tools and all(tag in tools for tag in required):
                return agent
        return None

    def _missing_tools(self, agent, text: str) -> tuple[str, ...]:
        """Tools the answer calls but ``agent`` cannot run (empty when none).

        A model wearing the web-agent prompt still answers «создай сайт» with a
        ```bash``` block. The block is executed only by an agent that owns bash:
        elsewhere it would silently do nothing (the old «просит разрешения и не
        работает»). Comment-only shell blocks and prose-only python blocks are
        not actions and are ignored.
        """
        role = getattr(agent, "role", "")
        if role in ("code", "files", "project"):
            return ()
        # Алиасы тоже «свои»: ```bash``` выполняет terminal этого же агента.
        own = set(getattr(agent, "all_tools", lambda: {})().keys()) \
            or set(getattr(agent, "available_tools", lambda: [])())
        missing: list[str] = []
        for tag, payload in _BLOCK.findall(str(text or "")):
            tag = tag.strip().lower()
            if tag in own or tag in missing:
                continue
            if tag in EXECUTION_TAGS - {"json"}:
                if tag in ("bash", "terminal") and not shell_text.has_commands(payload):
                    continue          # блок-комментарий: выполнять нечего
                if tag == "python" and not payload.strip():
                    continue
            missing.append(tag)
        return tuple(missing)

    def _goal_with_context(self) -> str:
        ctx = self.ctx
        parts = []
        history = [item for item in (ctx.history or [])
                   if isinstance(item, dict) and str(item.get("content") or "").strip()]
        if history:
            recent = "\n".join(f"{item.get('role')}: {clip(item.get('content'), 600)}"
                               for item in history[-6:])
            parts.append(f"Недавний разговор (контекст):\n{recent}")
        parts.append(f"Запрос пользователя:\n{ctx.goal}")
        if self._search_observation:
            parts.append(self._search_observation)
        return "\n\n".join(parts)

    def _start_stop_watcher(self, agent):
        cancel = self.ctx.cancel
        if cancel is None:
            return None

        def watch():
            while not cancel.wait(0.25):
                if self.state.status != "ok":
                    return
            try:
                agent.request_stop()
            except Exception:  # noqa: BLE001
                pass

        thread = threading.Thread(target=watch, name="agenticseek-stop", daemon=True)
        thread.start()
        return thread

    @staticmethod
    def _stop_watcher(thread) -> None:
        if thread is not None and thread.is_alive():
            thread.join(timeout=0.05)

    # ── answers ───────────────────────────────────────────────────────────
    def _partial_answer(self, stop: AgentStopped) -> str:
        """Keep what the agent already produced; add the honest stop reason."""
        partial = ""
        for agent in self.agents.values():
            if getattr(agent, "last_answer", ""):
                partial = str(agent.last_answer).strip()
        if getattr(self.chosen, "notes", None):
            url, text = self.chosen.notes[-1]
            partial = (f"Последнее, что удалось прочитать ({url}):\n{clip(text, 1200)}")
        if partial and len(partial) > 40:
            return f"{stop.user_message}\n\n{clip(partial, 3000)}"
        return stop.user_message

    def _fallback_answer(self, error: BaseException) -> str:
        """One plain answer, then an honest error message."""
        try:
            text, _meta = self.ctx.complete(
                [{"role": "user", "content": self.ctx.goal}],
                system=("Ты — NovaMind. Ответь по-русски на запрос пользователя. "
                        "Инструменты сейчас недоступны, поэтому не обещай выполнить "
                        "команды, а дай максимум полезной информации."),
                temperature=0.3, max_tokens=800, timeout=45)
            if text:
                return str(text)
        except Exception:  # noqa: BLE001
            log.debug("fallback answer failed", exc_info=True)
        reason = getattr(error, "user_message", "") or str(error)
        return f"Не удалось выполнить запрос: {reason}"

    def _answer_from_observation(self, agent) -> str:
        """Модель не дала ответ — отвечаем по найденным данным, без инструментов."""
        import re
        self._emit({"type": "step", "icon": "🧠",
                    "text": "Собираю ответ по найденным источникам"})
        messages = [
            {"role": "system", "content": (
                "Ты NovaMind. Отвечай по-русски, чётко и по делу. Инструментов нет: "
                "не пиши теги и блоки вызова. Используй только данные ниже, "
                "ссылайся на номера [1], [2]. Даже если данных мало — перечисли всё найденное: названия, ссылки, суть из описаний. Не отвечай одной фразой «данных недостаточно».")},
            {"role": "user", "content": f"{self.ctx.goal}\n\n{self._search_observation}\n\n"
                                        "Ответь списком из 3–5 пунктов: название, ссылка, "
                                        "1 строка что делает (название, ссылка, 1 строка)."},
        ]
        try:
            text = str(agent.provider.respond(messages) or "")
        except Exception:  # noqa: BLE001
            return ""
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
        return "" if _is_tool_only(text) else text

    def _local_digest(self) -> str:
        """Список найденного без участия модели (если модель сдалась)."""
        items = getattr(self, "_found", None) or []
        if not items:
            return ""
        lines = ["Вот что нашлось по твоему запросу:\n"]
        for index, item in enumerate(items, 1):
            title = str(item.get("title") or item.get("host") or item.get("url") or "").strip()
            snippet = clip(str(item.get("snippet") or "").strip(), 300)
            lines.append(f"{index}. **{title}**\n   {item.get('url', '')}\n   {snippet}".rstrip())
        lines.append("\nМодель не собрала связный ответ, поэтому показываю найденное как есть.")
        return "\n".join(lines)

    def _recover_answer(self) -> str:
        """Last attempt at a clean answer when the model only produced reasoning.

        Two steps, mirroring the behaviour NovaMind had before the rewrite:
        one strict retry («только готовый ответ») and, if that also leaks,
        the quoted draft salvaged out of the leaked text. The caller only runs
        this for a successful run whose sanitised answer came out empty.
        """
        answer = ""
        if not self.ctx.offline:
            try:
                self._emit({"type": "step", "icon": "🧹",
                            "text": "Модель начала с рассуждений — прошу чистый ответ"})
                history = [item for item in (self.ctx.history or [])
                           if isinstance(item, dict) and item.get("content")][-4:]
                text = self.provider.respond(
                    [{"role": "system", "content": CLEAN_ANSWER_SYSTEM}] + history
                    + [{"role": "user", "content": self.ctx.goal
                        + ("\n\n" + self._search_observation
                           if self._search_observation else "")}])
                answer = self._finalize(text)
            except AgentStopped:
                raise
            except Exception as error:  # noqa: BLE001 — recovery must not fail the run
                log.warning("clean-answer retry failed: %s", type(error).__name__)
                answer = ""
        if answer.strip():
            return answer
        if self.ctx.salvage_answer is not None:
            try:
                quoted = str(self.ctx.salvage_answer(self._raw_answer) or "").strip()
            except Exception:  # noqa: BLE001
                log.warning("answer salvage failed", exc_info=True)
                quoted = ""
            if quoted:
                self._emit({"type": "step", "icon": "💬",
                            "text": "Достал ответ из черновика модели"})
                return quoted
        return ""

    def _finalize(self, text: str) -> str:
        # Теги <bash>/<bash code_output> в ответе — не текст для пользователя:
        # команды остаются блоками, сочинённый моделью «вывод» убирается.
        text = sanitize_answer(str(text or "")).strip()
        if text and self.ctx.finalize is not None:
            try:
                text = str(self.ctx.finalize(text) or "").strip()
            except Exception:  # noqa: BLE001 — a filter must not eat the answer
                log.warning("finalize hook failed", exc_info=True)
        return text

    def _shutdown(self) -> None:
        if self.ctx.runner is not None:
            sandbox.set_runner(None)
        for agent in self.agents.values():
            browser = getattr(agent, "browser", None)
            if browser is not None:
                try:
                    browser.close()
                except Exception:  # noqa: BLE001
                    pass

    # ── persistence ───────────────────────────────────────────────────────
    def _start_run(self) -> str:
        ctx = self.ctx
        try:
            record_id = database.create_agent_run(
                goal=ctx.goal, chat_id=ctx.chat_id, run_id=ctx.run_id,
                provider=ctx.provider, model=ctx.model)
        except Exception:  # noqa: BLE001 — persistence must not break a turn
            log.warning("cannot persist the agent run", exc_info=True)
            return ""
        return record_id

    def _finish_run(self) -> None:
        if not self.record_id:
            return
        try:
            database.finish_agent_run(self.record_id, self.state.status, self.answer,
                                      self.state.error, self.state.steps)
        except Exception:  # noqa: BLE001
            log.warning("cannot finish the agent run record", exc_info=True)


class _Budget:
    """Shared step counter (mirrors upstream's step limit, per run)."""

    def __init__(self, limit: int):
        self.limit = max(1, int(limit))
        self.used = 0
        self._lock = threading.Lock()

    def take(self) -> bool:
        with self._lock:
            if self.used >= self.limit:
                raise StepLimitReached()
            self.used += 1
            return True


def _tee(first, second):
    """Forward an event to both sinks (route + engine buffer)."""
    def sink(event):
        for target in (first, second):
            if target is None:
                continue
            try:
                target(event)
            except Exception:  # noqa: BLE001
                log.debug("event sink failed", exc_info=True)
    return sink


def _chunks(text: str, size: int = 48):
    text = text or ""
    for index in range(0, len(text), size):
        yield text[index:index + size]


def run_stream(ctx: EngineContext):
    """Convenience wrapper: run one turn and yield the events."""
    yield from AgentEngine(ctx).stream()



import re as _re

_TOOL_ONLY = _re.compile(
    r"^\s*(?:<tool_code>|<tool_call>|<web_search>|<web_open>|```(?:web_search|web_open))",
    _re.I)


def _is_tool_only(text) -> bool:
    t = str(text or "").strip()
    return not t or bool(_TOOL_ONLY.match(t))


#: Теги, которые в прямом режиме (без инструментов) означают попытку вызова,
#: а не пример кода для пользователя.
_DIRECT_TOOL_TAGS = {
    "terminal", "bash", "sh", "python", "json", "web_search", "web_open",
    "file_read", "file_write", "file_edit", "file_find", "file_finder",
    "mcp_call", "image", "audio", "video", "remember", "recall", "forget",
    "task", "task_done", "schedule", "skill_create", "tool_code", "tool_call",
}


def _is_tool_call_answer(text) -> bool:
    """Ответ целиком представляет собой вызов недоступного инструмента."""
    value = str(text or "").strip()
    if not value:
        return False
    if _TOOL_ONLY.match(value):
        return True
    block = re.fullmatch(r"```([a-zA-Z_][\w-]*)\n(.*?)```", value, re.S)
    if block and block.group(1).strip().lower() in _DIRECT_TOOL_TAGS:
        return True
    if value.startswith("{") and value.endswith("}"):
        try:
            payload = json.loads(value)
        except (ValueError, TypeError):
            return False
        return isinstance(payload, dict) and bool(
            {"tool", "action", "tool_input"} & set(payload))
    return False



def _search_query(goal: str) -> str:
    """Фраза-команда -> поисковый запрос (+ site:github.com для GitHub)."""
    text = str(goal or "")
    q = _re.sub(r"(?i)metatr[ea]+d[ea]r\s*5?", "MetaTrader 5", text)
    q = _re.sub(r"(?i)\bбот\w*", "bot", q)
    q = _re.sub(r"(?i)\b(?:использовать|используй|mcp|github|гитхаб|найти|найди|найдите|несколько|пожалуйста|мне|и|для)\b", " ", q)
    q = _re.sub(r"\s+", " ", q).strip(" .,:;!?")
    if not q:
        return text
    low = text.lower()
    return f"site:github.com {q}" if ("github" in low or "гитхаб" in low) else q


def _needs_tools(goal) -> bool:
    """Нужны ли инструменты (файлы, запуск), а не просто ответ по поиску."""
    return bool(_re.search(
        r"(?i)\b(?:сохрани\w*|создай\w*|запусти\w*|установи\w*|скачай\w*|удали\w*|выполни\w*|"
        r"напиши\s+(?:файл|скрипт|программу))\b|в\s+файл", str(goal or "")))


def _no_data(text) -> bool:
    """Короткая отписка вида «данных недостаточно»."""
    t = str(text or "").strip()
    return len(t) < 250 and bool(_re.search(
        r"(?i)недостаточно|не\s+(?:удалось|могу|нашл)|нет\s+данных|insufficient|not enough", t))
