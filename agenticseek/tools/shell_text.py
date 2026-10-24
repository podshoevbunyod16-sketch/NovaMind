"""Разбор текста shell-блока: что из него реально пойдёт в песочницу.

Зачем отдельный модуль
----------------------
Модель любит «объясняющие» блоки::

    ```bash
    # создам виртуальное окружение
    python -m venv venv
    ```

Песочница NovaMind не выполняет комментарии: строка ``# создам …`` попадает
в ``routes.terminal.check_command`` и отклоняется как «Пустая команда»
(код 2). Агент видел ``[failure] … → код 2: Пустая команда``, тратил шаг,
иногда повторял тот же блок и сдавался — ровно это и случилось на запросе
«сделай интернет-магазин».

Модуль не импортирует Flask и инструменты, поэтому им пользуются и
``agenticseek/tools/bash_interpreter.py`` (что выполнять), и
``agenticseek/policy.py`` (спрашивать ли разрешение и что показать в карточке).
"""

from __future__ import annotations

import re

#: Строка целиком является комментарием шелла (``#`` — первый непробельный символ).
_COMMENT_LINE = re.compile(r"^\s*#")


def is_comment(line: str) -> bool:
    """Строка — комментарий шелла? (``#!`` тоже, это shebang)."""
    return bool(_COMMENT_LINE.match(str(line or "")))


def executable_lines(payload: str) -> list[str]:
    """Строки блока bash, которые действительно являются командами.

    Пустые строки и комментарии отбрасываются. Комментарий в конце команды
    (``ls -la   # список файлов``) остаётся: шелл сам его отбросит, а песочница
    разбирает такую строку нормально.
    """
    lines = []
    for raw in str(payload or "").splitlines():
        line = raw.strip()
        if not line or is_comment(line):
            continue
        lines.append(line)
    return lines


def first_command(payload: str) -> str:
    """Первая настоящая команда блока (для карточки подтверждения) или ''."""
    lines = executable_lines(payload)
    return lines[0] if lines else ""


def has_commands(payload: str) -> bool:
    """Есть ли в блоке хоть одна исполняемая команда."""
    return bool(executable_lines(payload))


#: Команды, которые на телефоне идут долго: им даём повышенный таймаут.
SLOW_MARKERS = (
    "venv", "pip install", "pip3 install", "pip -m install", "python -m pip",
    "npm install", "npm ci", "yarn", "pnpm", "pytest", "make", "cmake",
    "cargo", "go build", "gcc", "g++", "javac", "tsc", "ffmpeg", "ffprobe",
    "git clone", "apt install", "apt-get install", "pkg install", "unzip",
    "tar -x", "sqlite3", "openssl", "yt-dlp",
)


def is_slow(command: str) -> bool:
    """Команда обычно длится дольше обычного шага агента."""
    low = str(command or "").lower()
    return any(marker in low for marker in SLOW_MARKERS)
