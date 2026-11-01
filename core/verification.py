"""Evidence-based verification of a finished agent turn.

There are exactly five final outcomes.  They are a closed vocabulary shared by
the engine, the JSON result and the browser: ``completed``,
``partially_completed``, ``failed``, ``cancelled`` and
``requires_user_action``.  A completed turn must not be reported when the only
evidence is the model's own text, and a denied/unavailable tool must not be
reported as a plain failure when the user can unblock it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

#: The only five final states the product may display.
OUTCOMES = ("completed", "partially_completed", "failed", "cancelled",
            "requires_user_action")

OUTCOME_LABELS = {
    "completed": "выполнено",
    "partially_completed": "выполнено частично",
    "failed": "не выполнено",
    "cancelled": "отменено",
    "requires_user_action": "нужно действие пользователя",
}

#: Errors where retrying the same call cannot help but a person can.
_NEEDS_USER_RE = re.compile(
    r"нет прав|не разреш|запрещ|permission|forbidden|unauthori[sz]ed|"
    r"not enabled|выключен|не подключ|not connected|not granted|"
    r"нужно подтвержд|разрешить|подтвердите|missing credential|"
    r"api key|токен|token|авториз|login|войдите|подключите|"
    r"not configured|не настроен|лимит|quota|rate limit",
    re.IGNORECASE,
)

_CANCELLED_RE = re.compile(r"останов|прерван|cancel|abort|отмен", re.IGNORECASE)

_QUESTION_RE = re.compile(
    r"^\s*(?:что|кто|как|почему|зачем|где|когда|сколько|какой|какая|какие|"
    r"what|who|how|why|where|when|which|is|are|can you explain)\b|\?\s*$",
    re.IGNORECASE,
)

_IMPERATIVE_RE = re.compile(
    r"найди|запусти|выполни|созда|запиш|измени|прочита|открой|проверь|"
    r"установ|скачай|сгенер|исправ|почини|удали|отправ|подключ|"
    r"search|find|run|execute|create|write|edit|read|open|build|install|fix|delete|send",
    re.IGNORECASE,
)

_OPERATION_RE = re.compile(
    r"найди|найти|поиск|запусти|запустить|выполни|выполнить|созда|создать|"
    r"запиш|записать|измени|изменить|прочита|прочитать|открой|открыть|"
    r"проверь|проверить|установ|скачай|скачать|сгенер|сгенерир|"
    r"исправ|почини|удали|отправ|подключ|search|find|run|execute|create|"
    r"write|edit|read|open|check|build|test|install|fix|delete|send",
    re.IGNORECASE,
)


@dataclass
class VerificationResult:
    status: str
    ok: bool
    summary: str
    evidence: list[dict] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "ok": self.ok,
            "summary": self.summary,
            "evidence": list(self.evidence),
            "unresolved": list(self.unresolved),
        }


def _ok(item) -> bool:
    return bool(item.get("ok", True)) if isinstance(item, dict) else bool(
        getattr(item, "ok", True))


def _failure_text(item) -> str:
    if isinstance(item, dict):
        text = str(item.get("summary") or item.get("error")
                   or item.get("message") or "").strip()
        return text or str(item.get("command") or "").strip()
    parts = [str(getattr(item, "summary", "") or ""),
             str(getattr(item, "error", "") or "")]
    data = getattr(item, "data", None)
    if isinstance(data, dict):
        parts.append(str(data.get("error") or ""))
    return " ".join(part for part in parts if part).strip()


def _evidence_text(item) -> str:
    if not _ok(item):
        return _failure_text(item)
    if isinstance(item, dict):
        return str(item.get("summary") or "")
    return str(getattr(item, "summary", "") or "")


def _failure_needs_user(item) -> bool:
    kind = ""
    if isinstance(item, dict):
        kind = str(item.get("error_type") or item.get("type") or "").lower()
    else:
        data = getattr(item, "data", None)
        if isinstance(data, dict):
            kind = str(data.get("error_type") or data.get("type") or "").lower()
    if kind in {"requires_user_action", "permission_denied", "unauthorized",
                "not_configured", "cancelled", "cancelled_by_user"}:
        return True
    text = _failure_text(item)
    return bool(text and _NEEDS_USER_RE.search(text))


_SEARCH_GOAL_RE = re.compile(
    r"найди|найти|поиск|исслед|развед|research|search|источник|новост|актуаль|погугл",
    re.IGNORECASE,
)
_OPEN_GOAL_RE = re.compile(r"открой|открыть|перейд|open|url|https?://", re.IGNORECASE)
_READ_GOAL_RE = re.compile(r"прочита|прочитать|read|содержимое|покажи файл|cat\b", re.IGNORECASE)
_WRITE_GOAL_RE = re.compile(
    r"созда|создать|запиш|записать|напиш|написать|create|write|редакт|edit|"
    r"измени|изменить|исправ|исправить|fix|рефактор|удали|удалить|delete|добав",
    re.IGNORECASE)
_FILE_GOAL_RE = re.compile(r"файл|file|report|readme|\.txt|\.md|\.py|проект", re.IGNORECASE)
_RUN_GOAL_RE = re.compile(
    r"запусти|запустить|выполни|выполнить|run|execute|терминал|terminal|команд", re.IGNORECASE)
_TEST_GOAL_RE = re.compile(r"тест|test|pytest|проверк|check|smoke|lint|compile", re.IGNORECASE)
_CODING_GOAL_RE = re.compile(r"код|code|баг|bug|исправ|fix|рефактор|test|тест|репозитор", re.IGNORECASE)
_MEDIA_GOAL_RE = re.compile(
    r"нарису|изображен|картин|image|видео|video|озвуч|аудио|audio|сгенер.*медиа", re.IGNORECASE)
_TEST_OUTPUT_RE = re.compile(
    r"pytest|npm (run )?test|unittest|py_compile|smoke|\btests?\b|check|lint|compile|"
    r"OK\b|passed|прошл", re.IGNORECASE)
_WRITE_COMMAND_RE = re.compile(
    r"touch|mkdir|cat\s*>|echo\s+[^|]*>|tee|\bcp\b|\bmv\b|write|save", re.IGNORECASE)
_TOOL_ALIASES = {"bash": "terminal", "shell": "terminal", "run": "terminal",
                 "files": "file_read", "file_finder": "file_find"}


def _canonical(name) -> str:
    value = str(name or "").strip().lower()
    return _TOOL_ALIASES.get(value, value)


def _successful(observations) -> list:
    return [item for item in observations if _ok(item)]


def _obs_text(item) -> str:
    if isinstance(item, dict):
        return " ".join(str(item.get(key) or "") for key in
                        ("command", "summary", "output", "result"))
    return " ".join(str(getattr(item, key, "") or "") for key in
                    ("command", "summary", "output", "result"))


def _criteria_failures(goal: str, observations, media, sources) -> list[str]:
    """Concrete task criteria, not just «some tool returned success».

    A completed run must demonstrate the requested effect: a written file for a
    file request, a search/open/knowledge result for research, a real test run
    for a test request, and generated media for a media request.  Without this
    check a successful ``web_search`` could mark «создай файл» as completed.
    """
    text = str(goal or "")
    ok = _successful(observations)
    names = {_canonical(item.get("tool") if isinstance(item, dict) else getattr(item, "tool", ""))
             for item in ok}
    evidence_text = " ".join(_obs_text(item) for item in ok)
    source_list = list(sources or [])
    failures: list[str] = []
    if _MEDIA_GOAL_RE.search(text) and not media:
        failures.append("медиа не создано")
    if _SEARCH_GOAL_RE.search(text):
        search_tools = {"web_search", "web_open", "knowledge_search", "mcp_call"}
        if not (names & search_tools) and not source_list:
            failures.append("нет результата поиска или открытого источника")
    if _OPEN_GOAL_RE.search(text) and "web_open" not in names:
        failures.append("страница не открыта инструментом web_open")
    if _READ_GOAL_RE.search(text) and not (names & {"file_read", "file_find", "file_search",
                                                    "file_tree", "web_open"}):
        failures.append("файл или страница не прочитаны")
    if _WRITE_GOAL_RE.search(text) and _FILE_GOAL_RE.search(text):
        wrote = bool(names & {"file_write", "file_edit"}) or bool(
            names & {"terminal", "python"} and _WRITE_COMMAND_RE.search(evidence_text))
        if not wrote:
            failures.append("нет подтверждения записи или изменения файла")
    if _RUN_GOAL_RE.search(text):
        if not (names & {"terminal", "python", "git"}):
            failures.append("команда не выполнялась")
        elif _TEST_GOAL_RE.search(text) and not _TEST_OUTPUT_RE.search(evidence_text):
            failures.append("запуск проверки не подтверждён выводом тестов")
    if _CODING_GOAL_RE.search(text) and _WRITE_GOAL_RE.search(text) \
            and names & {"file_write", "file_edit"} \
            and not _TEST_OUTPUT_RE.search(evidence_text):
        failures.append("изменения не подтверждены запуском подходящей проверки")
    return failures


def verify(goal: str, answer: str, state, plan=None, error: str = "",
           cancelled: bool = False) -> VerificationResult:
    observations = list(getattr(state, "observations", []) or [])
    evidence = list(observations)
    # A failed attempt that was retried successfully is not an unresolved
    # failure.  Only the latest observation for each tool decides its status.
    latest_by_tool: dict[str, object] = {}
    for item in observations:
        name = str((item.get("tool") if isinstance(item, dict)
                    else getattr(item, "tool", "")) or "")
        latest_by_tool[name] = item
    failures = [item for item in latest_by_tool.values() if not _ok(item)]
    media = list(getattr(state, "media", []) or [])
    sources = list(getattr(state, "sources", []) or [])
    answer = str(answer or "").strip()
    error = str(error or "").strip()
    status = str(getattr(state, "status", "ok") or "ok")
    needs_user = any(_failure_needs_user(item) for item in failures)
    cancelled = cancelled or bool(getattr(state, "cancelled", False)) \
        or status == "cancelled" or bool(_CANCELLED_RE.search(error))
    goal_text = str(goal or "")
    operation = bool(_OPERATION_RE.search(goal_text))
    if _QUESTION_RE.search(goal_text) and not _IMPERATIVE_RE.search(goal_text):
        # «Что такое test?» is a question, not an instruction to run tests.
        operation = False
    substantive = bool(answer) or bool(evidence) or bool(media) or bool(sources)

    if cancelled:
        return VerificationResult(
            "cancelled", False,
            "Задача отменена пользователем; незавершённые шаги не считаются выполненными.",
            evidence[-8:], ["задача отменена до получения результата"],
        )

    if needs_user:
        unresolved = [(_failure_text(item) or "инструмент недоступен")[:300]
                      for item in failures if _failure_needs_user(item)]
        return VerificationResult(
            "requires_user_action", False,
            "Нужно действие пользователя: " + (unresolved[0] if unresolved else
                                                "подключите или разрешите инструмент."),
            evidence[-8:], unresolved[-5:] or ["нет разрешения на нужный инструмент"],
        )

    if failures:
        unresolved = [(_failure_text(item) or "инструмент завершился ошибкой")[:300]
                      for item in failures]
        if answer:
            return VerificationResult(
                "partially_completed", False,
                "Ответ получен, но часть действий завершилась ошибкой; см. журнал.",
                evidence[-8:], unresolved[-5:],
            )
        return VerificationResult(
            "failed", False,
            "Задача не выполнена: " + unresolved[0], evidence[-8:], unresolved[-5:],
        )

    if error or status == "error":
        return VerificationResult(
            "failed", False,
            "Задача не выполнена: " + (error or "неизвестная ошибка."),
            evidence[-8:], [error] if error else ["нет ответа"],
        )

    # A bare "ok" from the model is not proof of an operation.  If the plan has
    # unfinished tool steps, the honest outcome is partial.
    pending_steps = []
    for item in plan or []:
        status_name = str(item.get("status") or "pending")
        if str(item.get("id") or "") in {"verify", "context", "crosscheck"}:
            continue            # reason-only steps are closed by verification
        if status_name not in {"completed", "skipped"}:
            pending_steps.append(str(item.get("title") or item.get("id") or "шаг"))

    if operation and not evidence and not media and not sources:
        # The user asked to do something, but the turn produced no observable
        # tool result.  Do not paint a green «выполнено» over a plain answer.
        return VerificationResult(
            "failed", False,
            "Задача не выполнена: нет инструментального подтверждения действия.",
            evidence[-8:], ["действие не подтверждено наблюдением"],
        )

    if not substantive:
        return VerificationResult(
            "failed", False, "Модель не вернула ни результата, ни наблюдений.",
            evidence[-8:], ["пустой ответ"],
        )

    criteria = _criteria_failures(goal_text, observations, media, sources) if operation else []
    if criteria:
        return VerificationResult(
            "failed", False,
            "Задача не выполнена: " + criteria[0] + "; критерии цели не подтверждены.",
            evidence[-8:], criteria[-5:],
        )

    summary = "Задача выполнена; критерии цели подтверждены наблюдениями." if evidence else \
        "Задача выполнена; ответ сформирован без вызовов инструментов."
    if pending_steps and operation:
        return VerificationResult(
            "partially_completed", False,
            "Получен полезный результат, но не все шаги плана были выполнены.",
            evidence[-8:], [f"не выполнено: {title}" for title in pending_steps[-5:]],
        )
    return VerificationResult("completed", True, summary, evidence[-8:], [])
