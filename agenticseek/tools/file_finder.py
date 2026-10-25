"""File search/read tool (block tag ``file_finder``).

Derived from AgenticSeek ``sources/tools/fileFinder.py`` (GPL-3.0, see
NOTICE.md): the block syntax is unchanged —

.. code-block:: text

    ```file_finder
    name=notes.md
    ```

    ```file_finder
    action=read
    name=notes.md
    ```

NovaMind adaptations:

* confined to the agent workspace (``resolve_workspace_path``), so a model
  cannot walk the server filesystem through a crafted name;
* secret-looking files are refused (``.env``, keys, tokens, cookies, the chat
  database, ``.git/config``) — the agent must never read credentials, even if a
  web page tells it to;
* no attachment/PDF stack: upstream reads PDFs with ``pypdf``; NovaMind does not
  pull an extra dependency into the main environment, so PDFs are reported as
  unsupported instead of failing obscurely;
* results are limited in count and size, so the context window stays sane.
"""

from __future__ import annotations

import os
import re

from agenticseek.core.agent import clip
from agenticseek.tools.tools import Tools

MAX_RESULTS = 20
MAX_READ_BYTES = 200_000
_SECRET_NAMES = re.compile(
    r"(^\.env($|\.)|\.env$|\.pem$|\.key$|\.p12$|\.pfx$|id_rsa|id_ed25519|"
    r"credentials|secrets?\.|\.netrc|cookies|novamind_chats\.db$|\.sqlite3?$)", re.I)


class FileFinderTool(Tools):
    #: Поиск по файлам. Чтение вынесено в отдельный ``file_read``, поэтому в
    #: таймлайне шаг честно называется «Нашёл файл», а не «Прочитал файл».
    tag = "file_find"
    aliases = ("file_finder",)
    name = "FileFinder"
    description = "Найти или прочитать файл в рабочей папке: name=... | action=read name=..."
    risk = "safe"

    def execute(self, payload: str) -> str:
        params = _parse_params(payload)
        name = params.get("name") or params.get("path") or params.get("file") or ""
        action = (params.get("action") or "find").strip().lower()
        if not name:
            return ("[failure] Нужно имя файла. Пример: name=notes.md "
                    "или action=read name=notes.md")
        if _SECRET_NAMES.search(os.path.basename(name)):
            return f"[failure] Файл «{name}» похож на секрет — читать его запрещено."
        try:
            target = self.resolve_path(name)
        except PermissionError:
            return f"[failure] Путь «{name}» вне рабочей папки."
        except ValueError:
            return "[failure] Пустое имя файла."

        if action in ("read", "cat", "open"):
            return self._read(file_path=target, original=name)
        found = self._find(target, os.path.basename(name))
        if not found:
            return (f"[failure] Файл «{name}» не найден в рабочей папке. "
                    "Проверь имя или используй bash: ls")
        listing = "\n".join(found)
        return f"[success] Найдено {len(found)}:\n{listing}"

    # ── helpers ───────────────────────────────────────────────────────────
    def _read(self, file_path: str, original: str) -> str:
        if not os.path.exists(file_path):
            # Allow action=read name=partial-name: resolve by search first.
            matches = self._find(self.work_dir, os.path.basename(original))
            if len(matches) != 1:
                return f"[failure] Файл «{original}» не найден."
            file_path = os.path.join(self.work_dir, matches[0])
        if os.path.isdir(file_path):
            return f"[failure] «{original}» — папка, а не файл. Используй bash: ls"
        if file_path.lower().endswith(".pdf"):
            return ("[failure] PDF не поддерживается: в окружении нет парсера PDF. "
                    "Попроси пользователя вставить текст.")
        try:
            with open(file_path, "rb") as handle:
                raw = handle.read(MAX_READ_BYTES + 1)
        except OSError as error:
            return f"[failure] Не удалось прочитать «{original}»: {error}"
        if len(raw) > MAX_READ_BYTES:
            return f"[failure] Файл «{original}» больше {MAX_READ_BYTES} байт — не читаю целиком."
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return f"[failure] «{original}» — двоичный файл, читать текстом нельзя."
        relative = os.path.relpath(file_path, self.work_dir)
        return f"[success] {relative} ({len(text)} символов):\n{clip(text, 6000)}"

    def _find(self, start: str, filename: str) -> list[str]:
        """Breadth-first search for ``filename`` under the workspace."""
        if os.path.exists(start) and os.path.isfile(start):
            return [os.path.relpath(start, self.work_dir)]
        results: list[str] = []
        base = start if os.path.isdir(start) else self.work_dir
        for root, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if not d.startswith((".git", ".agent-tmp", "__pycache__"))]
            for item in files:
                if item == filename or (filename.lower() in item.lower() and len(filename) > 2):
                    if _SECRET_NAMES.search(item):
                        continue
                    results.append(os.path.relpath(os.path.join(root, item), self.work_dir))
                    if len(results) >= MAX_RESULTS:
                        return sorted(results)
        return sorted(results)


def _parse_params(payload: str) -> dict:
    params = {}
    for line in str(payload or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if "=" in line:
            key, value = line.split("=", 1)
        elif ":" in line and " " not in line.split(":", 1)[0]:
            key, value = line.split(":", 1)
        else:
            params.setdefault("name", line)
            continue
        params[key.strip().lower()] = value.strip()
    return params


class FileEditTool(Tools):
    """``file_edit`` — точечная правка файла в рабочей папке.

    Полный файл перезаписывается блоком ``python:path``, но так легко потерять
    то, что модель не видела. Правка ``старое → новое`` безопаснее и понятнее
    пользователю: если фрагмента нет или он встречается дважды, файл не
    меняется и модель получает объяснение.

    Payload — JSON::

        {"path": "app.py", "old": "VERSION = '1.0'", "new": "VERSION = '2.0'"}
    """

    tag = "file_edit"
    name = "FileEdit"
    description = 'Заменить фрагмент в существующем файле: {"path","old","new"}'
    risk = "write"

    def execute(self, payload: str) -> str:
        params = parse_json_payload(payload)
        path = str(params.get("path") or params.get("file") or params.get("name") or "").strip()
        old = str(params.get("old") or params.get("find") or "")
        new = str(params.get("new") or params.get("replace") or "")
        if not path:
            return '[failure] file_edit: нужен path. Пример: {"path":"app.py","old":"a","new":"b"}'
        if not old:
            return "[failure] file_edit: нужен old — что именно заменить."
        if _SECRET_NAMES.search(os.path.basename(path)):
            return f"[failure] Файл «{path}» похож на секрет — менять его запрещено."
        try:
            target = self.resolve_path(path)
        except (PermissionError, ValueError) as error:
            return f"[failure] Путь «{path}» не разрешён: {error}"
        if not os.path.isfile(target):
            return f"[failure] Файл «{path}» не найден — сначала создай его блоком с именем файла."
        try:
            with open(target, "r", encoding="utf-8") as handle:
                text = handle.read()
        except (OSError, UnicodeDecodeError) as error:
            return f"[failure] «{path}» не читается как текст: {error}"
        count = text.count(old)
        if count == 0:
            return f"[failure] В «{path}» нет фрагмента {old[:120]!r} — файл не изменён."
        if count > 1:
            return (f"[failure] Фрагмент встречается {count} раз — правка неоднозначна, "
                    "уточни old (добавь контекст). Файл не изменён.")
        updated = text.replace(old, new, 1)
        try:
            with open(target, "w", encoding="utf-8") as handle:
                handle.write(updated)
        except OSError as error:
            return f"[failure] Не удалось записать «{path}»: {error}"
        relative = os.path.relpath(target, self.work_dir)
        diff = "\n".join([f"- {line}" for line in old.splitlines()]
                          + [f"+ {line}" for line in new.splitlines()])
        return f"[success] {relative}: заменён один фрагмент.\n{diff}"


def parse_json_payload(payload: str) -> dict:
    """JSON-полезная нагрузка блока (или пустой словарь)."""
    import json

    text = str(payload or "").strip()
    if not text.startswith("{"):
        return {}
    try:
        value = json.loads(text)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


class FileReadTool(Tools):
    """``file_read`` — прочитать файл рабочей папки.

    Заменяет ``file_finder action=read``: в таймлайне это шаг «Прочитал файл»,
    а не поиск. Скрытые файлы и секреты по-прежнему не читаются.
    """

    tag = "file_read"
    name = "FileRead"
    description = "Прочитать файл: путь строкой или {\"path\": \"app.py\"}"
    risk = "safe"

    def execute(self, payload: str) -> str:
        params = parse_json_payload(payload) or _parse_params(payload)
        name = (params.get("path") or params.get("name") or params.get("file")
                or _first_line(payload))
        name = str(name or "").strip().strip("`")
        if not name:
            return ("[failure] file_read: нужен путь. Пример:\n"
                    "```file_read\ntemplates/index.html\n```")
        if _SECRET_NAMES.search(os.path.basename(name)):
            return f"[failure] Файл «{name}» похож на секрет — читать его запрещено."
        # Тот же разбор, что у file_finder: поведение чтения не меняется.
        reader = FileFinderTool()
        try:
            target = reader.resolve_path(name)
        except PermissionError:
            return f"[failure] Путь «{name}» вне рабочей папки."
        except ValueError:
            return "[failure] Пустое имя файла."
        return reader._read(file_path=target, original=name)


class FileWriteTool(Tools):
    """``file_write:путь`` — создать файл. Содержимое пишет движок.

    Блок с путём после тега сохраняется на диск до вызова инструмента, поэтому
    здесь остаётся только подтвердить запись и подсказать, как проверить файл.
    """

    tag = "file_write"
    name = "FileWrite"
    description = "Создать файл: путь после тега, содержимое — тело блока"
    risk = "write"

    def execute(self, payload: str) -> str:
        saved = str(getattr(self, "saved_path", "") or "")
        if not saved:
            return ("[failure] file_write: нужен путь после тега, например\n"
                    "```file_write:app.py\nprint('hello')\n```")
        if saved.endswith(".py"):
            check = f"`python3 -m py_compile {saved}` или `python3 {saved}`"
        else:
            check = f"`head -20 {saved}`"
        return (f"[success] Файл сохранён: {saved}\n"
                f"Проверь его отдельной командой в блоке bash: {check}.")


def _first_line(payload: str) -> str:
    for line in str(payload or "").splitlines():
        line = line.strip()
        if line and "=" not in line:
            return line
    return ""
