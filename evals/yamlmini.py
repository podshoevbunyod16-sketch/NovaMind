"""Мини-разбор YAML без внешних зависимостей (Termux-совместимо).

Формат намеренно простой: несколько документов через `---`, в каждом —
плоские пары `ключ: значение`, значение может быть строкой, числом,
списком в квадратных скобках или списком через запятую.

    id: search-01
    category: search
    prompt: Найди курс биткоина
    tools: search, open
    expect: биткоин
    max_steps: 8
    ---

Такой файл — настоящий YAML (поток документов), а PyYAML не нужен:
на Android лишняя зависимость с компиляцией — лишняя головная боль.
"""
from __future__ import annotations

import os


class YamlError(ValueError):
    """Ошибка разбора: файл не соответствует поддерживаемому подмножеству."""


def _strip_comment(line: str) -> str:
    """Убирает комментарий, не трогая `#` внутри кавычек."""
    quote = ""
    for index, char in enumerate(line):
        if char in "\"'":
            if quote == char:
                quote = ""
            elif not quote:
                quote = char
        elif char == "#" and not quote:
            return line[:index]
    return line


def _value(raw: str):
    text = raw.strip()
    if not text:
        return ""
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].strip()
        if not inner:
            return []
        return [_unquote(item.strip()) for item in inner.split(",") if item.strip()]
    lowered = text.lower()
    if lowered in ("true", "yes", "да"):
        return True
    if lowered in ("false", "no", "нет"):
        return False
    if text.isdigit() or (text.startswith("-") and text[1:].isdigit()):
        return int(text)
    return _unquote(text)


def _unquote(text: str) -> str:
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    return text


def _split_list(value):
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if not value:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return [str(value)]


def parse_documents(text: str):
    """Разбирает поток YAML-документов в список словарей."""
    documents, current = [], {}
    for number, line in enumerate(text.splitlines(), 1):
        clean = _strip_comment(line).rstrip()
        if not clean.strip():
            continue
        if clean.strip() == "---":
            if current:
                documents.append(current)
            current = {}
            continue
        if clean.strip() == "...":
            if current:
                documents.append(current)
            current = {}
            continue
        if ":" not in clean:
            raise YamlError(f"строка {number}: ожидалось «ключ: значение» — {clean.strip()!r}")
        key, _, raw = clean.partition(":")
        key = key.strip()
        if not key or key.startswith("-"):
            raise YamlError(f"строка {number}: неверный ключ {clean.strip()!r}")
        current[key] = _value(raw)
    if current:
        documents.append(current)
    return documents


def load_file(path: str):
    with open(path, encoding="utf-8") as handle:
        return parse_documents(handle.read())


def load_tasks(directory: str):
    """Читает все `*.yaml` из каталога (по алфавиту) и проверяет поля."""
    tasks = []
    if not os.path.isdir(directory):
        raise YamlError(f"нет каталога с задачами: {directory}")
    for name in sorted(os.listdir(directory)):
        if not name.endswith((".yaml", ".yml")):
            continue
        for document in load_file(os.path.join(directory, name)):
            document.setdefault("source", name)
            document["expect"] = _split_list(document.get("expect"))
            document["tools"] = _split_list(document.get("tools"))
            document["forbid_tools"] = _split_list(document.get("forbid_tools"))
            document["max_steps"] = int(document.get("max_steps") or 8)
            document["id"] = str(document.get("id") or f"{name}:{len(tasks) + 1}")
            document["prompt"] = str(document.get("prompt") or "").strip()
            if not document["prompt"]:
                raise YamlError(f"{name}: у задачи {document['id']} нет prompt")
            tasks.append(document)
    return tasks
