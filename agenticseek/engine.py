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
from agenticseek.router import AgentRouter
from agenticseek.state import RunState
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
    "Если данных не хватает — скажи это одной короткой честной фразой.")
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
#: Инструменты, которым нужна рабочая папка (в гостевом запуске их нет).
SANDBOX_TAGS = {"terminal", "python", "file_find", "file_read", "file_write", "file_edit"}
#: ```tag\npayload``` blocks of an answer.
_BLOCK = re.compile(r"```([a-zA-Z_][\w-]*)\n(.*?)```", re.S)
#: Human names of media kinds for the answer text.
KIND_RU = {"image": "изображение", "audio": "аудио", "video": "видео"}
AGENT_TIMEOUT = max(10.0, float(os.getenv("AGENT_TIMEOUT", "180")))
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

        thread = threading.Thread(target=work, name="agenticseek-run", daemon=True)
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
        """The turn itself: build, run, finish. Emits through ``ctx.emit``."""
        self.record_id = self._start_run()
        if not (self.ctx.force_search and self.ctx.search is not None):
            # «Поиск» начинается с собственной сцены поиска, а не с «Думаю».
            self._emit({"type": "stage", "scene": "agent", "title": "Думаю",
                        "text": "Выбираю исполнителя AgenticSeek…"})
        try:
            self._build_agents()
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

    # ── assembly ──────────────────────────────────────────────────────────
    def _build_agents(self) -> None:
        ctx = self.ctx
        name = "NovaMind"
        if ctx.profile == "coding":
            self._build_coding_agents(name)
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

        extras = "\n\n".join(part for part in (ctx.memory_prompt, ctx.skills_prompt) if part)
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

    def _build_coding_agents(self, name: str) -> None:
        """Coding profile: one read-first agent over the repository."""
        agent = ProjectAgent(name, prompt_path("project_agent"), self.provider)
        self.agents = {agent.type: agent}
        deadline = self.ctx.deadline or (time.monotonic() + self.ctx.timeout)
        agent.attach(events=self._emit, cancel=self.ctx.cancel, deadline=deadline,
                     step_budget=self._budget, confirm=self._confirm_hook(),
                     record=self._record_hook(), state=self.state)
        if self.ctx.memory_prompt:
            agent.extra_context = self.ctx.memory_prompt
        self.router = AgentRouter([agent])

    def _build_tools(self) -> dict:
        ctx = self.ctx
        tools: dict = {}
        if ctx.search is not None:
            tools["web_search"] = WebSearchTool(ctx.search, state=self.state)
        if ctx.read_page is not None:
            tools["web_open"] = WebOpenTool(ctx.read_page, state=self.state)
        if ctx.workspace:
            tools["terminal"] = BashTool()
            tools["python"] = PythonTool()
            tools["file_read"] = FileReadTool()
            tools["file_write"] = FileWriteTool()
            tools["file_find"] = FileFinderTool()
            tools["file_edit"] = FileEditTool()
        if ctx.tools_ctx is not None:
            tools.update(build_nova_tools(ctx.tools_ctx, state=self.state, emit=self._emit))
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
                for agent in agents:
                    if agent.role != "talk":
                        agent.tools = dict(shared)
                self._emit({"type": "step", "icon": "🔒",
                            "text": "Строгий режим MCP: доступен только mcp_call"})
            else:
                self._emit({"type": "step", "icon": "⚠️",
                            "text": "MCP не подключён: строгий режим снят, работаю обычными "
                                    "инструментами. Подключи сервер в Настройках → MCP"})
        for agent in agents:
            if agent.role == "talk":
                continue
            for tag, tool in shared.items():
                canonical = getattr(tool, "tag", tag) or tag
                if agent.role == "web" and canonical in EXECUTION_TOOL_TAGS:
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
        if ctx.media_kind and ctx.media_generate is not None:
            self._run_media()
            return
        if ctx.offline or ctx.provider == "local_demo":
            self._run_offline()
            return
        if ctx.force_search:
            self._search_observation = self._run_forced_search()
        agent = (self.router.by_role(ctx.force_role) if ctx.force_role else None) \
            or self.router.select_agent(ctx.goal)
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
        finally:
            self._stop_watcher(watcher)

        if ctx.cancel is not None and ctx.cancel.is_set():
            self.state.status = "stopped"
        self.answer = str(answer or "").strip()
        self.state.steps = self._budget.used
        self._emit({"type": "step", "icon": "🏁",
                    "text": f"Готово · шагов: {self.state.steps} · модель: {ctx.model or '?'}"})

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
            results, _meta = ctx.search(goal, 3)
        except Exception as error:  # noqa: BLE001 — the answer must still happen
            log.warning("forced search failed: %s", type(error).__name__)
            self._emit({"type": "step", "icon": "⚠️",
                        "text": f"Поиск не удался: {type(error).__name__}"})
            return ""
        results = [item for item in (results or []) if isinstance(item, dict)]
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
        self._emit({"type": "tool", "name": "web_search", "command": f"nova search {goal}",
                    "code": 0, "output": lines, "results": results})

        pages = []
        for item in results[:3]:
            if ctx.cancel is not None and ctx.cancel.is_set():
                break
            url = str(item.get("url") or "")
            if ctx.read_page is None or not url.startswith(("http://", "https://")):
                continue
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
                + ("\n\nПрочитанные страницы:\n\n" + "\n\n".join(pages) if pages else ""))

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
                    + [{"role": "user", "content": self.ctx.goal}])
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
