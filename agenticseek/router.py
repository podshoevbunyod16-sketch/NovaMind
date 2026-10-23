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

ROLE_KEYWORDS = {
    "web": ("http://", "https://", "интернет", "в сети", "поиск", "поищи", "найди",
            "найти", "погугл", "новост", "что нового", "нового в", "погод", "курс",
            "сколько стоит", "цена", "сайт", "статья", "википеди", "узнай"),
    "files": ("файл", "папк", "каталог", "workspace", "рабочая папка", "прочитай",
              "дерево", "список файлов", "запомни", "забудь", "вспомни", "память",
              "задач", "напомни", "mcp", "навык", "скилл"),
    "code": ("код", "скрипт", "python", "javascript", "баг", "ошибк", "тест", "pytest",
             "функци", "класс", "рефактор", "парсинг", "напиши программ", "исправь",
             "запусти", "выполни", "команд", "терминал", "shell", "bash", "git"),
    "talk": ("привет", "здравствуй", "как дела", "спасибо", "кто ты", "что умеешь"),
}

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

    def choose_role(self, text: str) -> str:
        """Pick ``web``/``files``/``code``/``talk`` for the request."""
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
        best, best_score = "talk", 0
        for role, markers in ROLE_KEYWORDS.items():
            score = sum(1 for marker in markers if marker in low)
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
