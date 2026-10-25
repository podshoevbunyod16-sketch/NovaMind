"""Ход агента как таймлайн: «Думал N c» → «Выполнил команду» → «Изменил файл».

Современный агент показывает не один сворачивающийся лог, а последовательность
шагов, как в Claude Code / Cursor:

    🧠 Думал 4 c
    ⌨️ Выполнил команду            ls -la
    🧠 Думал 2 c
    📄 Прочитал файл               templates/index.html
    ✏️ Изменил файл                index.html
    🔌 MCP: exa · search           {"query": "flask"}
    🌐 Поиск в интернете           "flask магазин"
    📥 Открыл страницу             https://example.com
    ✕ Ошибка                       python3 app.py → код 1

Один модуль решает две задачи: агент берёт отсюда подписи для событий, а тесты
проверяют их напрямую, не заглядывая в UI.

Тексты — по-русски: интерфейс NovaMind русский, и «Ran command» рядом с русским
ответом выглядел бы чужим.
"""

from __future__ import annotations

import json
import re

#: Быстрее этого «думанье» показывать не стоит: «Думал 0 c» выглядит поломкой.
MIN_THOUGHT_SECONDS = 0.4

#: Действие → (иконка, подпись, тип строки).
ACTIONS = {
    "terminal": ("⌨️", "Выполнил команду", "command"),
    "bash": ("⌨️", "Выполнил команду", "command"),          # старый тег
    "file_find": ("🔎", "Нашёл файлы", "find"),
    "file_read": ("📄", "Прочитал файл", "read"),
    "file_write": ("📝", "Создал файл", "write"),
    "file_edit": ("✏️", "Изменил файл", "edit"),
    "web_search": ("🌐", "Поиск в интернете", "search"),
    "web_open": ("📥", "Открыл страницу", "fetch"),
    "mcp_call": ("🔌", "MCP", "mcp"),
    "image": ("🖼", "Создал изображение", "media"),
    "audio": ("🔊", "Создал аудио", "media"),
    "video": ("🎬", "Создал видео", "media"),
    "remember": ("🧠", "Запомнил", "memory"),
    "recall": ("🧠", "Вспомнил", "memory"),
    "forget": ("🧠", "Забыл", "memory"),
    "task": ("📋", "Завёл задачу", "task"),
    "task_done": ("✅", "Закрыл задачу", "task"),
    "schedule": ("⏰", "Задача по расписанию", "task"),
    "skill_create": ("📚", "Сохранил навык", "skill"),
    # Инструменты кодинг-агента по репозиторию (только для администратора).
    "project_tree": ("🗂", "Показал структуру проекта", "read"),
    "project_read": ("📄", "Прочитал файл", "read"),
    "project_search": ("🔎", "Искал по коду", "find"),
    "project_defs": ("🔍", "Нашёл определения", "find"),
    "project_syntax": ("🧪", "Проверил синтаксис", "command"),
    "project_tests": ("🧪", "Запустил тесты", "command"),
    "project_patch": ("🩹", "Предложил патч (не применён)", "edit"),
    "file_finder": ("🔎", "Нашёл файлы", "find"),        # старый тег
    "python": ("⌨️", "Выполнил Python", "command"),      # старый тег
}

_ERROR_LABELS = {
    "command": "Ошибка команды",
    "read": "Файл не прочитан",
    "write": "Файл не создан",
    "edit": "Не удалось изменить файл",
    "fetch": "Страница не открылась",
    "search": "Поиск не удался",
    "mcp": "Ошибка MCP",
}


def plural_ru(count: int, one: str, few: str, many: str) -> str:
    """1 шаг / 2 шага / 5 шагов."""
    count = abs(int(count))
    if count % 10 == 1 and count % 100 != 11:
        return one
    if 2 <= count % 10 <= 4 and not 12 <= count % 100 <= 14:
        return few
    return many


def seconds_label(seconds: float) -> str:
    """«Думал 4 c» / «Думал меньше секунды»."""
    value = float(seconds or 0)
    if value < 1:
        return "Думал меньше секунды"
    return f"Думал {value:.0f} c"


def thought_event(seconds: float, text: str = "") -> dict:
    """Событие «модель думала N секунд» с её собственными словами (если есть)."""
    value = round(float(seconds or 0), 1)
    return {"type": "thought", "seconds": value, "label": seconds_label(value),
            "icon": "🧠", "text": _clip(text, 1200)}


def plan_text(answer: str) -> str:
    """Что модель сказала перед вызовом инструмента — словами, без блоков."""
    body = re.sub(r"```.*?```", " ", str(answer or ""), flags=re.S)
    body = re.sub(r"</?[a-z_]+[^>]*>", " ", body, flags=re.I)
    return re.sub(r"\s+", " ", body).strip()


def tool_event(tag: str, payload: str, *, ok: bool = True, saved: str = "",
               command: str = "", output: str = "", code: int | None = None) -> dict:
    """Событие шага: иконка, подпись, деталь (команда/путь/запрос), статус.

    ``command`` — уже посчитанное описание блока (для bash это первая команда).
    """
    icon, label, kind = ACTIONS.get(tag, ("🔧", tag, "tool"))
    detail = ""
    files: list[str] = []

    if tag in ("terminal", "bash"):
        detail = command or ""
    elif tag == "python" and saved:
        # ```python:path``` — это запись файла, а не запуск кода (старый протокол).
        # Пользователю важно видеть «Создал файл app.py», а не «Выполнил Python».
        icon, label, kind = ACTIONS["file_write"]
        detail = saved
        files = [saved]
    elif tag in ("file_read", "file_find", "file_finder"):
        detail = _first_value(payload, ("path", "name", "file")) or _one_line(payload)
        if tag in ("file_find", "file_finder") and _reads(payload):
            # Старый ```file_finder action=read``` — это чтение, а не поиск.
            icon, label, kind = ACTIONS["file_read"]
    elif tag in ("file_write", "file_edit"):
        detail = saved or _json_value(payload, "path") or _one_line(payload)
        if detail:
            files = [detail]
        if tag == "file_edit":
            label = "Изменил файл"
    elif tag.startswith("project_"):
        detail = (_json_value(payload, "path") or _json_value(payload, "query")
                  or _json_value(payload, "name") or _one_line(payload))
        if tag == "project_patch" and detail:
            files = [detail]
    elif tag == "web_search":
        detail = _one_line(payload)
    elif tag == "web_open":
        detail = _one_line(payload)
    elif tag == "mcp_call":
        server = _json_value(payload, "server_id") or _json_value(payload, "server")
        tool = _json_value(payload, "tool") or _json_value(payload, "name")
        detail = " · ".join(part for part in (server, tool) if part)
        label = f"MCP: {detail}" if detail else "MCP"
    elif tag in ("image", "audio", "video"):
        detail = _json_value(payload, "prompt") or _one_line(payload)
    elif tag in ("remember", "recall", "forget", "schedule", "skill_create"):
        detail = _json_value(payload, "text") or _json_value(payload, "query") \
            or _json_value(payload, "prompt") or _json_value(payload, "name") \
            or _one_line(payload)
    elif tag == "task" or tag == "task_done":
        detail = _json_value(payload, "title") or _json_value(payload, "task_id")
    elif tag == "python":
        detail = command or _one_line(payload)
    else:
        detail = command or _one_line(payload)

    if not ok:
        label = _ERROR_LABELS.get(kind, label)
    if tag == "file_finder" and kind == "find":
        label = "Файлы не найдены" if not ok else "Нашёл файлы"
    return {
        "type": "tool",
        "name": tag,
        "kind": kind,
        "icon": icon,
        "label": label,
        "detail": _clip(detail, 200),
        "command": command or detail,
        "files": files,
        "code": (1 if not ok else 0) if code is None else code,
        "status": "ok" if ok else "error",
        "output": _clip(output, 1200),
    }


def summary(steps: int, errors: int = 0, thought_seconds: float = 0.0) -> str:
    """Подпись в шапке: «7 шагов · ошибок: 1 · думал 32 c»."""
    parts = []
    if steps:
        parts.append(f"{steps} {plural_ru(steps, 'шаг', 'шага', 'шагов')}")
    if errors:
        parts.append(f"ошибок: {errors}")
    if thought_seconds >= 1:
        parts.append(f"думал {thought_seconds:.0f} c")
    return " · ".join(parts)


# ── helpers ───────────────────────────────────────────────────────────────
def _reads(payload: str) -> bool:
    return bool(re.search(r"^\s*action\s*[=:]\s*read\b", str(payload or ""), re.M | re.I))


def _clip(text: str, limit: int) -> str:
    text = str(text or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _one_line(payload: str) -> str:
    for line in str(payload or "").splitlines():
        line = line.strip()
        if line:
            return line
    return ""


def _json_payload(payload: str) -> dict:
    text = str(payload or "").strip()
    if not text.startswith("{"):
        return {}
    try:
        value = json.loads(text)
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _json_value(payload: str, key: str) -> str:
    value = _json_payload(payload).get(key)
    return str(value).strip() if value not in (None, "") else ""


def _first_value(payload: str, keys) -> str:
    """Поле из JSON или из строк вида ``path=templates/index.html``."""
    for key in keys:
        value = _json_value(payload, key)
        if value:
            return value
    match = re.search(r"^\s*(?:path|name|file)\s*[=:]\s*(\S+)", str(payload or ""),
                      re.M | re.I)
    return match.group(1) if match else ""
