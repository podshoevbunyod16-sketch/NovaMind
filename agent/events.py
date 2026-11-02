"""Russian, user-facing descriptions for first-party tool activity events."""
from __future__ import annotations

import json


def tool_presentation(name: str, arguments=None, result=None, *, status="ok") -> dict:
    """Return a concise label and safe detail for an emitted tool event."""
    name = str(name or "")
    args = arguments if isinstance(arguments, dict) else {}
    output = result if isinstance(result, dict) else {}
    failed = str(status or "").lower() == "error" or output.get("ok") is False
    labels = {
        "shell": ("Ошибка команды" if failed else "Выполнил команду", "command"),
        "file_read": ("Прочитал файл", "read"),
        "file_write": ("Создал файл" if not failed else "Не создал файл", "write"),
        "file_edit": ("Изменил файл" if not failed else "Не изменил файл", "edit"),
        "file_search": ("Нашёл файлы", "search_files"),
        "file_tree": ("Показал структуру", "tree"),
        "web_search": ("Поиск в интернете", "search"),
        "web_open": ("Открыл страницу", "fetch"),
        "mcp_call": ("Вызвал подключённый сервис", "mcp"),
        "image": ("Создал изображение" if not failed else "Не создал изображение", "media"),
        "audio": ("Создал аудио" if not failed else "Не создал аудио", "media"),
        "video": ("Создал видео" if not failed else "Не создал видео", "media"),
        "task": ("Создал задачу" if not failed else "Не создал задачу", "task"),
        "task_done": ("Закрыл задачу" if not failed else "Не закрыл задачу", "task"),
    }
    label, kind = labels.get(name, ("Выполнил действие" if not failed else "Действие не выполнено", "tool"))
    if name == "mcp_call" and failed:
        label = "Ошибка MCP"
    detail = str(args.get("command") or args.get("path") or args.get("query") or args.get("url") or "")
    if name == "mcp_call":
        server, tool = str(args.get("server_id") or ""), str(args.get("tool") or "")
        detail = " · ".join(part for part in (server, tool) if part)
    if name == "file_write":
        detail = str(output.get("path") or args.get("path") or "")
    if name == "file_edit":
        detail = str(output.get("path") or args.get("path") or "")
    return {"label": label, "kind": kind, "detail": detail}


def presentation_from_event(event: dict) -> dict:
    """Enrich a ReAct tool event, accepting its JSON-encoded argument field."""
    raw = event.get("command")
    try:
        arguments = json.loads(raw) if isinstance(raw, str) else (raw or {})
    except (ValueError, TypeError):
        arguments = {}
    result = event.get("result") if isinstance(event.get("result"), dict) else {}
    return tool_presentation(event.get("name"), arguments, result,
                             status=event.get("status", "ok"))
