"""Capability-aware task plans. Plans guide the model; they never force calls.

A plan is not decoration: every status change here is driven by a real
observation (a tool call that started or finished).  The engine only marks a
step ``completed`` when the tool that carried it out succeeded, and ``failed``
when that tool failed.  Steps that were never needed are closed as ``skipped``
instead of being ticked as done.
"""
from __future__ import annotations

import re

#: Statuses a plan step may have.  ``doing`` is accepted as an alias of
#: ``in_progress`` for the older AgenticSeek planner payloads.
PLAN_STATUSES = ("pending", "in_progress", "completed", "failed", "skipped")
TERMINAL_STATUSES = frozenset({"completed", "failed", "skipped"})

#: Tool aliases used by AgenticSeek adapters and by the unified registry.
_TOOL_ALIASES = {
    "bash": "terminal", "shell": "terminal", "run": "terminal",
    "files": "file_read", "file_finder": "file_find", "grep": "file_search",
    "read": "file_read", "write": "file_write", "edit": "file_edit",
    "tree": "file_tree", "search": "web_search", "open": "web_open",
}


def normalize_status(status: str) -> str:
    """Return one of :data:`PLAN_STATUSES` (``doing`` → ``in_progress``)."""
    value = str(status or "").strip().lower()
    if value in {"doing", "in-progress", "inprogress", "started", "running"}:
        return "in_progress"
    if value in {"done", "success", "succeeded"}:
        return "completed"
    if value in {"error", "failed"}:
        return "failed"
    if value in {"skip", "not_needed"}:
        return "skipped"
    return value if value in PLAN_STATUSES else "pending"


def normalize_plan(steps) -> list[dict]:
    """Normalize plans coming from the planner agent or from the engine.

    The planner sends ``{id, agent, task, need}``; the engine sends
    ``{id, title, success_criteria, candidate_tools, status}``.  Both must render
    with the same status vocabulary, otherwise the UI shows ticks that never
    happened.
    """
    plan: list[dict] = []
    for index, item in enumerate(steps or [], 1):
        if not isinstance(item, dict):
            continue
        step = dict(item)
        step["id"] = str(step.get("id") or index)
        step["title"] = str(step.get("title") or step.get("task") or step.get("text")
                           or f"Шаг {index}")[:1200]
        step["status"] = normalize_status(step.get("status"))
        tools = step.get("candidate_tools")
        step["candidate_tools"] = [str(tool) for tool in tools] if isinstance(tools, list) else []
        plan.append(step)
    return plan


def _canonical_tool(tool: str) -> str:
    name = str(tool or "").strip().lower()
    if name.startswith("mcp:"):
        return "mcp_call" if "search" not in name else "mcp_search"
    return _TOOL_ALIASES.get(name, name)


def _step_matches(step: dict, tool: str) -> bool:
    """Does this step describe the work ``tool`` actually performs?"""
    candidates = {_canonical_tool(item) for item in step.get("candidate_tools") or []}
    if tool in candidates:
        return True
    step_id = str(step.get("id") or "")
    if step_id in {"verify", "context"}:
        return False
    reads = {"file_read", "file_find", "file_search", "file_tree"}
    writes = {"file_write", "file_edit"}
    executes = {"terminal", "python"}
    searches = {"web_search", "web_open", "knowledge_search", "mcp_search"}
    if step_id in {"research", "crosscheck"}:
        return tool in searches | {"mcp_call"}
    if step_id == "repository":
        return tool in {"git", "mcp_call", "web_open"} | reads
    if step_id == "diagnose":
        return tool in reads | executes | {"git"}
    if step_id in {"change", "execute"}:
        return tool in writes | executes | {"mcp_call"}
    if step_id == "tests":
        return tool in executes or tool == "git"
    if step_id == "resolve":
        return True
    return False


def mark_tool(plan: list[dict], tool: str, status: str,
              summary: str = "") -> tuple[dict | None, bool]:
    """Advance exactly one plan step for a real tool event.

    Returns ``(step, changed)``.  A successful tool never advances an unrelated
    pending step — that was the old visual bug where «Осмотреть файлы» got a
    tick because ``web_search`` succeeded.
    """
    if not plan:
        return None, False
    target = normalize_status(status)
    if target == "pending":
        return None, False
    canonical = _canonical_tool(tool)
    chosen = None
    for item in plan:
        if normalize_status(item.get("status")) in TERMINAL_STATUSES:
            continue
        if str(item.get("id") or "") == "verify":
            continue
        if _step_matches(item, canonical):
            chosen = item
            break
    if chosen is None:
        return None, False

    current = normalize_status(chosen.get("status"))
    if target == "in_progress":
        if current == "pending":
            chosen["status"] = "in_progress"
            chosen["active_tool"] = canonical
            if summary:
                chosen["last_observation"] = str(summary)[:500]
            return chosen, True
        return chosen, False
    if target == "completed":
        # A retry may turn an earlier failure into success; that is honest.
        if current == "completed":
            return chosen, False
        chosen["status"] = "completed"
        chosen["completed_by"] = canonical
        chosen.pop("revision_reason", None)
        if summary:
            chosen["last_observation"] = str(summary)[:500]
        return chosen, True
    # failed
    if current == "failed" and not summary:
        return chosen, False
    chosen["status"] = "failed"
    chosen["failed_by"] = canonical
    chosen["revision_reason"] = str(summary or f"{canonical} завершился ошибкой")[:500]
    return chosen, True


def finalize_plan(plan: list[dict], outcome: str, summary: str = "") -> bool:
    """Close the plan after verification: no invisible pending/doing steps.

    ``verify`` gets the outcome; anything the run did not need becomes
    ``skipped``.  The browser can then show a plan that matches reality rather
    than a list of optimistic ticks.
    """
    changed = False
    done = str(outcome or "") in {"completed", "partially_completed"}
    for item in plan or []:
        status = normalize_status(item.get("status"))
        if str(item.get("id") or "") == "resolve" and done:
            # A plain question is answered by the final text, not by a tool;
            # calling that step "skipped" would understate a real result.
            if status != "completed":
                item["status"] = "completed"
                changed = True
            continue
        if str(item.get("id") or "") == "verify":
            wanted = "completed" if done else "failed"
            if status != wanted:
                item["status"] = wanted
                changed = True
            if summary:
                item["verification_summary"] = str(summary)[:500]
            continue
        if status in {"pending", "in_progress"}:
            item["status"] = "skipped"
            if status == "in_progress":
                item["skipped_reason"] = "шаг не был завершён"
            elif done:
                item["skipped_reason"] = "не потребовалось для результата"
            else:
                item["skipped_reason"] = "задача завершилась раньше шага"
            changed = True
    return changed


def plan_counts(plan: list[dict]) -> dict:
    """Counts for the plan header (completed only; failed is not «done»)."""
    counts = {status: 0 for status in PLAN_STATUSES}
    for item in plan or []:
        counts[normalize_status(item.get("status"))] += 1
    counts["total"] = len(plan or [])
    return counts


def create_plan(goal: str, catalog: list[dict], has_context: bool = False) -> list[dict]:
    """Build a concise, task-specific dependency plan from live capabilities.

    This planner intentionally does not prescribe a fixed call sequence. Each
    item is a goal and evidence criterion; the agent may revise or skip it after
    observations.
    """
    text = str(goal or "").strip()
    low = text.lower()
    available = {str(item.get("id") or "") for item in catalog if item.get("available", True)}
    sources = {str(item.get("source") or "") for item in catalog if item.get("available", True)}
    steps: list[tuple[str, str, str, list[str]]] = []

    if has_context:
        steps.append(("context", "Уточнить релевантный контекст текущего диалога",
                      "Контекст соответствует текущей цели", []))
    if re.search(r"github|репозитор|repository|pull request|issue|ветк|branch", low):
        tools = sorted(x for x in available
                       if "git" in x or "github" in x or x.startswith("mcp:"))[:6]
        steps.append(("repository", "Обнаружить и осмотреть нужный репозиторий и его состояние",
                      "Получены структура, статус и относящиеся к задаче файлы", tools))
    if re.search(r"ошиб|bug|fix|исправ|код|code|test|тест|рефактор", low):
        steps.extend([
            ("diagnose", "Найти причину проблемы по коду и фактическому выводу",
             "Причина подтверждена файлом, диагностикой или тестом",
             sorted(x for x in available if x in {"file_read", "file_search", "shell", "git"})),
            ("change", "Внести минимальные обоснованные изменения",
             "Изменённые файлы соответствуют найденной причине",
             sorted(x for x in available if x in {"file_edit", "file_write", "shell"})),
            ("tests", "Запустить подходящие проверки и устранить оставшиеся ошибки",
             "Команда проверки завершилась успешно",
             sorted(x for x in available if x in {"shell", "python", "git"})),
        ])
    elif re.search(r"найди|поиск|research|исслед|актуаль|источник|новост", low):
        steps.extend([
            ("research", "Собрать сведения из подходящих доступных источников",
             "Есть релевантные источники или результаты инструментов",
             sorted(x for x in available if "search" in x or "open" in x
                    or x.startswith("mcp:"))[:8]),
            ("crosscheck", "Сопоставить ключевые факты и отметить неопределённость",
             "Ключевые утверждения опираются на наблюдения", []),
        ])
    elif re.search(r"созда|запиш|измени|отправ|подключ|удали|сгенер", low):
        steps.append(("execute", "Выполнить запрошенное изменение через разрешённую возможность",
                      "Инструмент подтвердил результат операции",
                      sorted(x for x in available if x in {"file_write", "file_edit", "shell"}
                             or x.startswith("mcp:"))[:8]))
    else:
        steps.append(("resolve", "Получить достаточные данные и выполнить цель пользователя",
                      "Результат непосредственно отвечает исходному запросу",
                      sorted(available)[:6]))

    if "mcp" in sources and not any(any(t.startswith("mcp:") for t in row[3]) for row in steps):
        # MCP is an option, not a compulsory action.
        steps[0][3].extend(sorted(x for x in available if x.startswith("mcp:"))[:4])
    steps.append(("verify", "Проверить результат по наблюдаемым доказательствам",
                  "Нет нерешённых ошибок; критерии цели выполнены", []))
    return [{"id": sid, "title": title, "success_criteria": evidence,
             "candidate_tools": tools, "status": "pending", "depends_on": []}
            for sid, title, evidence, tools in steps]
