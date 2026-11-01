"""Capability-aware task plans. Plans guide the model; they never force calls."""
from __future__ import annotations

import re


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
    steps: list[tuple[str, str, list[str]]] = []

    if has_context:
        steps.append(("context", "Уточнить релевантный контекст текущего диалога", "Контекст соответствует текущей цели", []))
    if re.search(r"github|репозитор|repository|pull request|issue|ветк|branch", low):
        tools = [x for x in available if "git" in x or "github" in x or x.startswith("mcp:")][:6]
        steps.append(("repository", "Обнаружить и осмотреть нужный репозиторий и его состояние",
                      "Получены структура, статус и относящиеся к задаче файлы", tools))
    if re.search(r"ошиб|bug|fix|исправ|код|code|test|тест|рефактор", low):
        steps.extend([
            ("diagnose", "Найти причину проблемы по коду и фактическому выводу",
             "Причина подтверждена файлом, диагностикой или тестом", [x for x in available if x in {"file_read", "file_search", "shell", "git"}]),
            ("change", "Внести минимальные обоснованные изменения",
             "Изменённые файлы соответствуют найденной причине", [x for x in available if x in {"file_edit", "file_write", "shell"}]),
            ("tests", "Запустить подходящие проверки и устранить оставшиеся ошибки",
             "Команда проверки завершилась успешно", [x for x in available if x in {"shell", "python", "git"}]),
        ])
    elif re.search(r"найди|поиск|research|исслед|актуаль|источник|новост", low):
        steps.extend([
            ("research", "Собрать сведения из подходящих доступных источников",
             "Есть релевантные источники или результаты инструментов", [x for x in available if "search" in x or "open" in x or x.startswith("mcp:")][:8]),
            ("crosscheck", "Сопоставить ключевые факты и отметить неопределённость",
             "Ключевые утверждения опираются на наблюдения", []),
        ])
    elif re.search(r"созда|запиш|измени|отправ|подключ|удали|сгенер", low):
        steps.append(("execute", "Выполнить запрошенное изменение через разрешённую возможность",
                      "Инструмент подтвердил результат операции", [x for x in available if x in {"file_write", "file_edit", "shell"} or x.startswith("mcp:")][:8]))
    else:
        steps.append(("resolve", "Получить достаточные данные и выполнить цель пользователя",
                      "Результат непосредственно отвечает исходному запросу", list(available)[:6]))

    if "mcp" in sources and not any(any(t.startswith("mcp:") for t in row[3]) for row in steps):
        # MCP is an option, not a compulsory action.
        steps[0][3].extend([x for x in available if x.startswith("mcp:")][:4])
    steps.append(("verify", "Проверить результат по наблюдаемым доказательствам",
                  "Нет нерешённых ошибок; критерии цели выполнены", []))
    return [{"id": sid, "title": title, "success_criteria": evidence,
             "candidate_tools": tools, "status": "pending", "depends_on": ([steps[-1][0]] if False else [])}
            for sid, title, evidence, tools in steps]
