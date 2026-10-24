"""Разбор текста bash-блока — общий для инструмента и для диалога разрешений.

Модели регулярно присылают блок, первая строка которого — комментарий::

    ```bash
    # в корне проекта
    ```

Раньше такой блок целиком уходил в песочницу, `check_command` видел только
комментарий, отвечал `Пустая команда` (код 2), и шаг агента терялся. Здесь
одно место, где решается, что в блоке действительно является командой.

Второй случай из журнала с телефона: `python -m venv venv` убивался по
таймауту (12 c) и приходил как голый `код -9`. Ниже — список «долгих» команд,
которым даётся увеличенный лимит, и определение запуска сервера (его в
песочнице запускать нельзя: он занимает терминал до таймаута).
"""

from __future__ import annotations

import os
import re

#: Команды, которые почти всегда дольше обычного лимита.
SLOW_MARKERS = (
    "venv", "pip install", "pip3 install", "pip uninstall", "-m pip", "npm install",
    "npm ci", "npm i ", "npm update", "yarn", "pnpm", "npx ", "poetry", "uv pip",
    "pytest", "unittest", "make", "makefile", "gcc ", "g++ ", "clang", "cargo",
    "rustc", "go build", "go test", "javac", "gradle", "mvn ", "tsc", "webpack",
    "vite", "next build", "git clone", "git fetch", "git pull", "tar ", "unzip",
    "gzip", "xz ", "ffmpeg", "yt-dlp", "sqlite3 ", "apt ", "apt-get", "pkg install",
    "pkg upgrade", "npm run build", "compress", "dumpdata", "loaddata",
)

#: Явный запуск долгоживущего сервера: он занимает терминал, а не завершается.
SERVER_MARKERS = (
    "flask run", "flask --app", "manage.py runserver", "runserver", "uvicorn",
    "gunicorn", "waitress", "npm start", "npm run dev", "npm run serve", "yarn dev",
    "yarn start", "pnpm dev", "next dev", "vite --host", "http.server", "serve ",
    "node server", "node app", "deno run --watch", "php -S", "cargo run",
)

#: Файлы, имя которых выдаёт сервер (для `python3 app.py`).
_SERVER_FILES = re.compile(r"\b(python3?|py)\s+([\w./-]+\.py)\b")
_SERVER_CALL = re.compile(r"\.run\s*\(|app\.run|serve_forever\s*\(|uvicorn\.run", re.I)
#: Что считается «достаточно длинной» командой, чтобы читать её как сервер.
_SERVER_TAIL = 40


def strip_comment(line: str) -> str:
    """Строка без inline-комментария; кавычки учитываются.

    ``echo '# не комментарий'`` остаётся как есть, ``ls -la  # список`` — без
    хвоста. Полноценный shell-парсер здесь не нужен: блок состоит из простых
    команд, а спорные случаи безопаснее не трогать.
    """
    quote = ""
    for index, char in enumerate(line):
        if quote:
            if char == quote:
                quote = ""
            continue
        if char in ("'", '"'):
            quote = char
            continue
        if char == "#" and (index == 0 or line[index - 1].isspace()):
            return line[:index].rstrip()
    return line.rstrip()


def executable_lines(payload: str) -> list[str]:
    """Команды блока: без пустых строк, комментариев и shebang."""
    result: list[str] = []
    for raw in str(payload or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line = strip_comment(line).strip()
        if line:
            result.append(line)
    return result


def first_command(payload: str) -> str:
    """Первая настоящая команда блока (для карточки «Нужно разрешение»)."""
    lines = executable_lines(payload)
    return lines[0] if lines else ""


def has_commands(payload: str) -> bool:
    return bool(executable_lines(payload))


def is_slow(command: str) -> bool:
    """Команда заведомо долгая — ей нужен увеличенный лимит времени."""
    low = str(command or "").lower()
    return any(marker in low for marker in SLOW_MARKERS)


def server_hint(command: str, work_dir: str = "") -> str:
    """Пустая строка, если это обычная команда; иначе — подсказка.

    Сервер не запускаем: в песочнице нет портов для пользователя, а терминал
    занят до таймаута. Проверять приложение надо через test client, а сервер
    пользователь запускает сам.
    """
    low = str(command or "").lower().strip()
    if not low:
        return ""
    for marker in SERVER_MARKERS:
        if marker in low:
            return _SERVER_TEXT
    match = _SERVER_FILES.search(low)
    if match and work_dir:
        candidate = os.path.join(work_dir, match.group(2))
        try:
            with open(candidate, "r", encoding="utf-8", errors="replace") as handle:
                body = handle.read()
        except OSError:
            return ""
        if _SERVER_CALL.search(body):
            return _SERVER_TEXT
    return ""


_SERVER_TEXT = (
    "Это запуск веб-сервера: в песочнице он занимает терминал до таймаута и "
    "ничего не проверяет. Проверь приложение без сервера — через test client "
    "(Flask): `python3 -c \"from app import app; c=app.test_client(); "
    "print(c.get('/').status_code)\"` или отдельным скриптом-проверкой. "
    "Сервер пользователь запустит сам, когда попросит."
)
