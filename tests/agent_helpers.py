"""Общие помощники тестов агента (движок AgenticSeek).

Модель в тестах заменяется скриптом ответов: ``chat_completion`` возвращает
по одному заранее заданному тексту и записывает входящие сообщения, чтобы
проверить, что агент действительно отправил модели нужный контекст.

Ответы пишутся в новом протоколе — блоками:

    ```web_search
    asyncio
    ```

Старый JSON-протокол (``{"action": "search"}``) удалён вместе со старым
движком, поэтому он в тестах больше не используется.
"""
from __future__ import annotations

import json


def ndjson(response):
    """Тело NDJSON-ответа → список событий."""
    return [json.loads(line) for line in response.get_data(as_text=True).splitlines()
            if line.strip()]


def answer_of(events):
    """Склеенный текст ответа (события ``token``)."""
    return "".join(event.get("token", "") for event in events if event["type"] == "token")


def events_of(events, kind):
    return [event for event in events if event["type"] == kind]


def script(monkeypatch, answers, seen=None, module=None):
    """Подменяет ``chat_completion`` в routes.agent скриптом ответов.

    Args:
        answers: список строк; последний ответ повторяется, если скрипт кончился.
        seen: список, куда складывать {"messages": [...], "system": "..."}.
        module: модуль с ``chat_completion`` (по умолчанию routes.agent).

    Returns:
        Список, который опустошается по мере ответов — как старый ``_stub_stream``.
    """
    if module is None:
        import routes.agent as module
    queue = list(answers)
    # Скрипт изображает рабочую модель: без этого resolve_target() вернул бы
    # local_demo (ключей в тестах нет), и движок ушёл бы в офлайн-ветку.
    monkeypatch.setattr(module, "resolve_target",
                        lambda provider=None, model=None: (provider or "groq",
                                                           model or "test-model", {}))

    def fake_complete(messages, provider=None, model=None, system=None, temperature=0.7,
                      max_tokens=None, timeout=90, extra=None, tools=None,
                      tool_choice=None, **kwargs):
        if seen is not None:
            seen.append({"messages": [dict(item) for item in messages],
                         "system": system or ""})
        text = queue.pop(0) if len(queue) > 1 else (queue[0] if queue else "Готово.")
        return text, {"provider": provider or "test", "model": model or "test-model"}

    monkeypatch.setattr(module, "chat_completion", fake_complete)
    return queue


def model_text(seen, index=-1):
    """Текст, который агент отправил модели (system + сообщения)."""
    item = seen[index]
    parts = [item.get("system") or ""]
    parts += [str(message.get("content") or "") for message in item["messages"]]
    return "\n".join(parts)
