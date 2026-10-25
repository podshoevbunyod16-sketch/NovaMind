"""Agent router: which executor should handle the request?

Derived from AgenticSeek ``sources/router.py`` (GPL-3.0, see NOTICE.md). The
classification scheme is upstream's:

* ``HIGH`` complexity → the planner (several dependent steps);
* otherwise a role is chosen among ``talk`` / ``files`` / ``code`` / ``web`` and
  the agent declaring that role handles the request;
* very short messages ("привет", "ок") always go to the talking agent, without
  spending a request on classification.

What changed and why:

* upstream loads ``facebook/bart-large-mnli`` with transformers for the
  zero-shot classifier (≈1.6 GB of RAM, class C on Termux) and keeps an
  ``adaptive_classifier``. NovaMind needs a router that works on a phone and
  costs nothing, so the default is a deterministic heuristic; a single cheap
  model call is used only when ``AGENTICSEEK_ROUTER=llm`` is set;
* NovaMind's own multi-step heuristic (:func:`looks_complex`) is reused as the
  complexity signal, so the behaviour of the product does not regress for the
  requests it already handled well.

The router never raises: on any doubt it falls back to the talking agent.
"""

from __future__ import annotations

import logging
import os
import re

log = logging.getLogger("agenticseek.router")

DEFAULT_MODE = os.getenv("AGENTICSEEK_ROUTER", "heuristic").strip().lower()
SHORT_MESSAGE_CHARS = 8

#: Few-shot examples for the optional model classifier (adapted from upstream
#: ``router.py`` TASK_EXAMPLES, translated for Russian-speaking users).
TASK_EXAMPLES = (
    ("привет", "talk"),
    ("как дела?", "talk"),
    ("спасибо, всё работает", "talk"),
    ("найди в интернете погоду в Худжанде", "web"),
    ("что нового в Python 3.13?", "web"),
    ("прочитай файл notes.md", "files"),
    ("покажи дерево рабочей папки", "files"),
    ("сохрани этот текст в todo.txt", "files"),
    ("напиши функцию на Python для сортировки", "code"),
    ("исправь ошибку в app.py", "code"),
    ("запусти тесты и почини упавшие", "code"),
)

ROLE_PRIORITY = ("code", "files", "web", "talk")
MCP_ROLE_PRIORITY = ("files", "code", "web")

ROLE_KEYWORDS = {
    "web": ("http://", "https://", "интернет", "в сети", "поиск", "поищи", "найди",
            "найти", "погугл", "новост", "что нового", "нового в", "погод", "температур",
            "прогноз", "курс", "сколько стоит", "цена", "сайт", "статья", "википеди",
            "узнай", "ссылк"),
    "files": ("файл", "папк", "каталог", "директори", "структур", "содержим",
              "workspace", "рабочая папка", "прочитай", "дерево", "список файлов",
              "репозитори", "запомни", "забудь", "вспомни", "память", "заметк",
              "сохрани", "запиши", "удали", "переименуй", "перемести", "скопируй",
              "задач", "список дел", "напомни", "расписани", "каждый день", "будильник",
              "таймер", "mcp", "навык", "скилл"),
    "code": ("код", "скрипт", "python", "javascript", "баг", "ошибк", "тест", "pytest",
             "функци", "класс", "рефактор", "парсинг", "напиши программ", "исправь",
             "запусти", "выполни", "команд", "терминал", "shell", "bash", "git",
             "ветк", "коммит", "скача", "склонир", "clone", "систем", "процесс",
             "диск", "озу", "порт", "сервер", "пинг", "ping", "curl", "лог",
             "установи", "поставь", "сборк"),
    "talk": ("привет", "здравствуй", "как дела", "спасибо", "кто ты", "что умеешь"),
}

#: Падежные, числовые и глагольные окончания русского языка (без словообразовательных
#: суффиксов вроде «-ор», «-ика», «-аж», чтобы «курсор» не совпадал с «курс»,
#: а «багаж» — с «баг»).
_RU_ENDINGS = (
    r"(?:а|я|у|ю|о|е|и|ы|ь|й|ом|ем|ём|ой|ей|ёй|ою|ею|ью|"
    r"ов|ев|ёв|ам|ям|ами|ями|ах|ях|те|ть|ти)?"
)

_MARKER_OVERRIDES = {
    "в сети": r"(?<![\wа-яё])в\s+сет[иь](?![\wа-яё])",
    "что нового": r"(?<![\wа-яё])что\s+нового(?![\wа-яё])",
    "нового в": r"(?<![\wа-яё])нового\s+в(?![\wа-яё])",
    "сколько стоит": r"(?<![\wа-яё])сколько\s+сто[ия]т(?![\wа-яё])",
    "статья": r"(?<![\wа-яё])(?:стать(?:я|и|е|ю|ей|ёй|ям|ями|ях)|статей)(?![\wа-яё])",
    "папк": r"(?<![\wа-яё])(?:папк(?:а|и|е|у|ой|ою|ам|ами|ах)|папок)(?![\wа-яё])",
    "рабочая папка": r"(?<![\wа-яё])рабоч(?:ая|ей|ую)\s+папк(?:а|и|е|у|ой|ою)(?![\wа-яё])",
    "список файлов": r"(?<![\wа-яё])спис(?:ок|ка|ку|ком|ке)\s+файлов(?![\wа-яё])",
    "забудь": r"(?<![\wа-яё])(?:забуд(?:ь|ьте)|забыть)(?![\wа-яё])",
    "прочитай": r"(?<![\wа-яё])(?:прочит(?:ай|айте|ать)|прочти(?:те)?)(?![\wа-яё])",
    "ошибк": r"(?<![\wа-яё])(?:ошибк(?:а|и|е|у|ой|ою|ам|ами|ах)|ошибок)(?![\wа-яё])",
    "сборк": r"(?<![\wа-яё])(?:сборк(?:а|и|е|у|ой|ою|ам|ами|ах)|сборок)(?![\wа-яё])",
    "напиши программ": r"(?<![\wа-яё])напи(?:ши|шите|сать)\s+программ(?:у|ы|а)?(?![\wа-яё])",
    "рефактор": r"(?<![\wа-яё])(?:от)?рефактор(?:инг(?:а|у|ом|е)?|ить|и|ьте)?(?![\wа-яё])",
    "погугл": r"(?<![\wа-яё])погугл(?:и|ить|ите|ил[аио]?)(?![\wа-яё])",
    "узнай": r"(?<![\wа-яё])узна(?:й|йте|ть)(?![\wа-яё])",
    "найди": r"(?<![\wа-яё])найд(?:и|ите|ём)(?![\wа-яё])",
    "поищи": r"(?<![\wа-яё])поищ(?:и|ите|ем)(?![\wа-яё])",
    "запусти": r"(?<![\wа-яё])запуст(?:и|ите|ить)(?![\wа-яё])",
    "выполни": r"(?<![\wа-яё])выполн(?:и|ите|ить)(?![\wа-яё])",
    "установи": r"(?<![\wа-яё])установ(?:и|ите|ить)(?![\wа-яё])",
    "запомни": r"(?<![\wа-яё])запомн(?:и|ите|ить)(?![\wа-яё])",
    "вспомни": r"(?<![\wа-яё])вспомн(?:и|ите|ить)(?![\wа-яё])",
    "напомни": r"(?<![\wа-яё])напомн(?:и|ите|ить)(?![\wа-яё])",
    "сохрани": r"(?<![\wа-яё])сохран(?:и|ите|ить)(?![\wа-яё])",
    "запиши": r"(?<![\wа-яё])запиш(?:и|ите)|записать(?![\wа-яё])",
    "удали": r"(?<![\wа-яё])удал(?:и|ите|ить)(?![\wа-яё])",
    "переименуй": r"(?<![\wа-яё])переимен(?:уй|уйте|овать)(?![\wа-яё])",
    "перемести": r"(?<![\wа-яё])перемест(?:и|ите|ить)(?![\wа-яё])",
    "скопируй": r"(?<![\wа-яё])скопир(?:уй|уйте|овать)(?![\wа-яё])",
    "скача": r"(?<![\wа-яё])скача(?:й|йте|ть)(?![\wа-яё])",
    "склонир": r"(?<![\wа-яё])склонир(?:уй|уйте|овать)(?![\wа-яё])",
    "ветк": r"(?<![\wа-яё])(?:ветк(?:а|и|е|у|ой|ою|ам|ами|ах)|веток)(?![\wа-яё])",
    "заметк": r"(?<![\wа-яё])(?:заметк(?:а|и|е|у|ой|ою|ам|ами|ах)|заметок)(?![\wа-яё])",
    "ссылк": r"(?<![\wа-яё])(?:ссылк(?:а|и|е|у|ой|ою|ам|ами|ах)|ссылок)(?![\wа-яё])",
    "список дел": r"(?<![\wа-яё])спис(?:ок|ка|ку|ком|ке)\s+дел(?![\wа-яё])",
    "каждый день": r"(?<![\wа-яё])кажд(?:ый|ого|ому|ым)\s+д(?:ень|ня|ню|нём)(?![\wа-яё])",
    "что умеешь": r"(?<![\wа-яё])что\s+(?:ты\s+)?умеешь(?![\wа-яё])",
}


def _compile_marker(marker: str) -> re.Pattern:
    """Компилирует ключевое слово с границами слов и русскими окончаниями."""
    key = marker.strip().lower()
    if key in _MARKER_OVERRIDES:
        return re.compile(_MARKER_OVERRIDES[key], re.I)
    if key.startswith(("http://", "https://")):
        return re.compile(rf"(?<![\wа-яё]){re.escape(key)}", re.I)
    if re.fullmatch(r"[a-z0-9_.-]+", key):
        return re.compile(rf"(?<![\wа-яё]){re.escape(key)}(?![\wа-яё])", re.I)
    parts = []
    for token in key.split():
        if re.fullmatch(r"[a-z0-9_.-]+", token):
            parts.append(re.escape(token))
        else:
            stem = token[:-1] if len(token) >= 4 and token[-1] in "аяоьеуюй" else token
            parts.append(re.escape(stem) + _RU_ENDINGS)
    return re.compile(rf"(?<![\wа-яё])" + r"\s+".join(parts) + rf"(?![\wа-яё])", re.I)


_ROLE_PATTERNS: dict[str, tuple[tuple[str, re.Pattern], ...]] = {
    role: tuple((marker, _compile_marker(marker)) for marker in markers)
    for role, markers in ROLE_KEYWORDS.items()
}

#: Поисковые запросы с упоминанием MCP или GitHub маршрутизируем агенту, у
#: которого реально подключён ``mcp_call`` (в движке это в первую очередь
#: ``FileAgent`` / ``CoderAgent``; ``BrowserAgent`` навязывает цикл
#: ``web_search`` → ``web_open`` и падает при ``strict_mcp`` без браузера).
_MCP_TARGET_RE = re.compile(
    r"(?<![\wа-яё])(?:mcp|github|гитхаб(?:а|у|ом|е)?)(?![\wа-яё])", re.I)
_GITHUB_URL_IN_GOAL_RE = re.compile(r"https?://github\.com/\S+", re.I)
_MCP_ACTION_RE = re.compile(
    r"(?<![\wа-яё])(?:найд(?:и|ите|ём)|найти|поищ(?:и|ите|ем)|"
    r"поиск(?:ай|ать|а|у|ом|е)?|ищ(?:и|ите|у)|искать|погугл(?:и|ить|ите)|"
    r"узна(?:й|йте|ть)|покаж(?:и|ите)|посмотр(?:и|ите|еть)|провер(?:ь|ьте|ить)|"
    r"подбер(?:и|ите)|выбер(?:и|ите)|откро(?:й|йте)|прочит(?:ай|айте|ать)|"
    r"да(?:й|йте)|вывед(?:и|ите)|получи(?:те)?|созда(?:й|йте|ть)|"
    r"структур\w*|дерев\w*|репозитори\w*|коммит\w*|ветк\w*|issue\w*|pull\s+request\w*|"
    r"мои\s+|мой\s+|моих\s+|есть\s+ли|search|find|lookup|list|open|show|get)(?![\wа-яё])",
    re.I,
)
_SEARCH_VERB_RE = re.compile(
    r"(?<![\wа-яё])(?:найд(?:и|ите|ём)|найти|поищ(?:и|ите|ем)|"
    r"поиск(?:ай|ать|а|у|ом|е)?|ищ(?:и|ите|у)|искать|погугл(?:и|ить|ите)|"
    r"узна(?:й|йте|ть)|покаж(?:и|ите)|посмотр(?:и|ите|еть)|провер(?:ь|ьте|ить)|"
    r"подбер(?:и|ите)|выбер(?:и|ите)|есть\s+ли|search|find|lookup)(?![\wа-яё])", re.I)
_GENERIC_MCP_WORDS = frozenset({
    "mcp", "server", "servers", "сервер", "серверы", "custom", "свой", "http", "https", "api",
})

#: «Создай сайт», «сделай интернет-магазин», «напиши бота» — это работа для
#: кодера, хотя слова «сайт» и «интернет» одновременно являются маркерами
#: веб-поиска. Без этой проверки запрос «создай интернет-магазин на Flask»
#: уходил веб-агенту, который умеет только искать страницы, и пользователь
#: вместо результата получал отказ или просьбу разрешения.
_BUILD_VERB = re.compile(
    r"(?:^|[^\wа-яё])(?:созда|сдела|сделай|напиш|напис|разработ|сверста|собер|сбери|"
    r"реализу|запрограмм|постро|построй|подними|сгенерир|сколоти|build|create|make|"
    r"write|develop|implement|generate)", re.I)
_BUILD_OBJECT = ("сайт", "магазин", "приложени", "бот", "апи", "api", "парсер", "скрипт",
                 "сервис", "лендинг", "панел", "дашборд", "игр", "программ", "функци",
                 "класс", "модул", "flask", "django", "fastapi", "react", "telegram")
_BUILD_OBJECT_RE = re.compile(
    r"(?<![\wа-яё])(?:" + "|".join(re.escape(obj) for obj in _BUILD_OBJECT) + r")", re.I)
#: Запросы-поиски внутри «создай»-фразы не превращают её в кодинг: «создай
#: список книг, найди в интернете» — по-прежнему про поиск. Достаточно, чтобы
#: не было глагола-действия над объектом создания.
_SEARCH_ONLY = re.compile(r"^(?:найди|поищи|погугли|search|find)\b", re.I)

_COMPLEX_MARKERS = ("затем", "потом", "после этого", "и сохрани", "сравни", "проанализируй",
                    "план", "по шагам", "несколько шагов", "сначала", "в конце",
                    "then ", "and save", "compare", "step by step")
_SEQUENCE = ("затем", "потом", "после этого", "сначала", "в конце", "и сохрани",
             "и запиши", "и сделай", "then ", "after that", "and save")
_ACTIONS = ("найди", "поищи", "прочитай", "открой", "напиши", "создай", "сделай",
            "запусти", "проверь", "исправь", "построй", "сгенерируй", "сохрани",
            "search", "find", "read", "write", "create", "build", "run", "fix")


def looks_complex(text: str) -> bool:
    """Does the request need several dependent steps?

    Deterministic replacement for a second model call. A sequencing word plus an
    action is enough; long requests with two actions also qualify. This is the
    signal the router uses for ``HIGH`` complexity.
    """
    low = str(text or "").lower()
    if not low.strip():
        return False
    if len(low) > 1200:
        return True
    actions = sum(1 for marker in _ACTIONS if marker in low)
    if any(marker in low for marker in _SEQUENCE) and actions >= 1:
        return True
    if any(marker in low for marker in _COMPLEX_MARKERS) and actions >= 2:
        return True
    return False


def is_build_request(text: str) -> bool:
    """Does the user ask to *create* a site/app/program rather than to find one?

    «Создай интернет-магазин на Flask» — кодинг; «найди сайт с рецептами» и
    «что пишут на сайте новостей» — веб-поиск. Проверка нужна раньше подсчёта
    ключевых слов: у «сайт»/«интернет» и у кодера маркеры пересекаются.
    """
    low = str(text or "").strip().lower()
    if not low or not _BUILD_VERB.search(low):
        return False
    if _SEARCH_ONLY.search(low):
        return False   # «найди, как создать сайт» — это поиск, а не разработка
    return bool(_BUILD_OBJECT_RE.search(low))


def is_mcp_search_request(text: str) -> bool:
    """Есть ли в запросе ``mcp`` или ``github`` вместе с глаголом поиска/действия или ссылкой GitHub."""
    low = str(text or "").strip().lower()
    if not low:
        return False
    if _GITHUB_URL_IN_GOAL_RE.search(low):
        return True
    if re.search(r"(?<![\wа-яё])mcp(?![\wа-яё])", low):
        return True
    return bool(_MCP_TARGET_RE.search(low) and (_SEARCH_VERB_RE.search(low) or _MCP_ACTION_RE.search(low)))


def _connected_mcp_tokens(agents) -> set[str]:
    """Имена подключённых MCP-серверов из ``mcp_call.tools_catalog``."""
    tokens: set[str] = set()
    for agent in agents or ():
        tools = getattr(agent, "tools", None) or {}
        mcp_tool = tools.get("mcp_call")
        catalog = getattr(mcp_tool, "tools_catalog", None) if mcp_tool is not None else None
        for entry in catalog or ():
            if not isinstance(entry, dict):
                continue
            for raw in (entry.get("server_id"), entry.get("server_name")):
                for word in re.findall(r"[\wа-яё]{3,}", str(raw or "").lower()):
                    if word not in _GENERIC_MCP_WORDS and not word.isdigit():
                        tokens.add(word)
    return tokens


def _agent_has_mcp(agent) -> bool:
    """Проверяет, зарегистрирован ли у агента инструмент ``mcp_call``."""
    if agent is None:
        return False
    if callable(getattr(agent, "all_tools", None)):
        if "mcp_call" in (agent.all_tools() or {}):
            return True
    if callable(getattr(agent, "available_tools", None)):
        if "mcp_call" in (agent.available_tools() or ()):
            return True
    tools = getattr(agent, "tools", None) or {}
    return "mcp_call" in tools


class AgentRouter:
    """Chooses the agent for a request.

    Args:
        agents: instantiated agents; their ``role``/``type`` identify them.
        classify: optional callable ``(prompt) -> str`` (the model classifier).
        mode: ``heuristic`` (default) or ``llm``.
        complexity: optional callable ``(text) -> bool`` overriding the
            complexity heuristic (the engine passes :func:`looks_complex`).
    """

    def __init__(self, agents, classify=None, mode=None, complexity=None):
        self.agents = list(agents or [])
        self.classify = classify
        self.mode = (mode or DEFAULT_MODE or "heuristic").strip().lower()
        self.complexity = complexity or looks_complex
        self.last_reason = ""

    # ── lookups ───────────────────────────────────────────────────────────
    def by_role(self, role: str):
        for agent in self.agents:
            if agent.role == role:
                return agent
        return None

    def by_type(self, agent_type: str):
        for agent in self.agents:
            if agent.type == agent_type:
                return agent
        return None

    def planner(self):
        return self.by_type("planner_agent")

    def mcp_agent(self):
        """Возвращает агента с ``mcp_call`` (приоритет: ``files`` → ``code`` → ``web``)."""
        for role in MCP_ROLE_PRIORITY:
            agent = self.by_role(role)
            if _agent_has_mcp(agent):
                return agent
        for agent in self.agents:
            if getattr(agent, "type", "") != "planner_agent" and getattr(agent, "role", "") != "talk":
                if _agent_has_mcp(agent):
                    return agent
        return self.by_role("files") or self.by_role("code")

    def is_complex(self, text: str) -> str:
        try:
            if self.complexity(text):
                return "HIGH"
        except Exception:  # noqa: BLE001 — a broken heuristic must not break chat
            log.debug("complexity heuristic failed", exc_info=True)
        if self.mode != "llm" or self.classify is None:
            return "LOW"
        examples = "\n".join(f"- {sample!r} -> {label}" for sample, label in
                             (("найди цены и сохрани в файл", "HIGH"),
                              ("привет", "LOW"),
                              ("перескажи прошлый ответ", "LOW")))
        answer = self._ask(
            "Classify the workload of the request. HIGH = several dependent steps "
            "(search AND save, read AND build, compare AND conclude). "
            "LOW = one action or conversation.\n"
            f"Examples:\n{examples}\n\nRequest: {text[:800]}\n"
            "Answer with one word: HIGH or LOW.")
        return "HIGH" if "high" in answer.lower() else "LOW"

    def _matches_connected_mcp(self, text: str) -> bool:
        """Упоминает ли пользователь один из реально подключённых MCP-серверов."""
        low = str(text or "").strip().lower()
        if not low:
            return False
        for token in _connected_mcp_tokens(self.agents):
            if re.search(rf"(?<![\wа-яё]){re.escape(token)}(?![\wа-яё])", low):
                return True
        return False

    def choose_role(self, text: str) -> str:
        """Pick ``web``/``files``/``code``/``talk`` for the request."""
        if is_mcp_search_request(text) or self._matches_connected_mcp(text):
            agent = self.mcp_agent()
            return agent.role if agent is not None else "files"
        if self.mode == "llm" and self.classify is not None:
            examples = "\n".join(f"- {sample!r} -> {label}" for sample, label in TASK_EXAMPLES)
            answer = self._ask(
                "Choose the best agent role for the request. Roles: talk (conversation), "
                "files (find/read/organise files), code (write, run or debug code), "
                f"web (search the web, open pages).\nExamples:\n{examples}\n\n"
                f"Request: {text[:800]}\nAnswer with one word.")
            label = self._label(answer)
            if label:
                return label
        return self.heuristic_role(text)

    def heuristic_role(self, text: str) -> str:
        low = str(text or "").lower()
        if is_mcp_search_request(low) or self._matches_connected_mcp(low):
            agent = self.mcp_agent()
            return agent.role if agent is not None else "files"
        if is_build_request(low):
            return "code"
        best, best_score = "talk", 0
        # При равенстве положительных баллов выигрывает роль выше в ROLE_PRIORITY:
        # code → files → web → talk.
        for role in ROLE_PRIORITY:
            patterns = _ROLE_PATTERNS.get(role, ())
            score = sum(1 for _marker, pattern in patterns if pattern.search(low))
            if score > best_score:
                best, best_score = role, score
        return best

    def select_agent(self, text: str):
        """Return the agent that should process ``text`` (never None if any agent)."""
        if not self.agents:
            return None
        text = str(text or "").strip()
        if len(self.agents) == 1:
            self.last_reason = "single agent"
            return self.agents[0]
        if len(text) <= SHORT_MESSAGE_CHARS:
            agent = self.by_role("talk") or self.by_type("casual_agent")
            if agent is not None:
                self.last_reason = "short message → talk"
                return agent

        if self.is_complex(text) == "HIGH":
            planner = self.planner()
            if planner is not None:
                self.last_reason = "complex → planner"
                return planner

        if is_mcp_search_request(text) or self._matches_connected_mcp(text):
            mcp = self.mcp_agent()
            if mcp is not None:
                self.last_reason = f"mcp/repo → {mcp.role}"
                return mcp

        role = self.choose_role(text)
        agent = self.by_role(role)
        if agent is None:
            agent = self.by_role("talk") or self.by_type("casual_agent") or self.agents[0]
            self.last_reason = f"no {role} agent → {agent.role}"
            return agent
        self.last_reason = f"role → {role}"
        return agent

    # ── helpers ───────────────────────────────────────────────────────────
    def _ask(self, prompt: str) -> str:
        if self.classify is None:
            return ""
        try:
            return str(self.classify(prompt) or "").strip()
        except Exception as error:  # noqa: BLE001 — routing must not break a turn
            log.warning("router classifier failed: %s", type(error).__name__)
            return ""

    @staticmethod
    def _label(answer: str) -> str:
        low = str(answer or "").lower()
        for role in ("files", "code", "web", "talk"):
            if re.search(rf"\b{role}\b", low):
                return role
        return ""
