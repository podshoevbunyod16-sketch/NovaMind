"""Read-first tools over the NovaMind source tree (coding profile).

The AgenticSeek coder agent edits files inside the sandbox workspace. The
NovaMind "coding agent" (admin only) works on the *project* itself, where a
wrong edit breaks the running server — so its tools are read-only and a change
is delivered as a patch the administrator reviews:

``project_tree``    list source files
``project_read``    read one file
``project_search``  find a string, with file:line
``project_defs``    find a function/class definition
``project_syntax``  ``ast.parse`` a Python file
``project_tests``   run one ``tests/test_*.py`` file
``project_patch``   propose a unified diff (never applied)

Path rules: relative paths inside the repository, no ``..``, no VCS directories,
no environment/secret files, a size limit, and an allow-list of text suffixes.
The tool output is untrusted data (a comment in the code could contain
instructions), so the agent prompt marks it as such.
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from agenticseek.core.agent import clip
from agenticseek.core.errors import AgenticSeekError
from agenticseek.tools.tools import Tools

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MAX_FILE_BYTES = 80_000
MAX_TREE = 200
MAX_HITS = 40
TEST_TIMEOUT = 60
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", "instance",
             "uploads", "workspace", ".agent-data", ".npm", ".pytest_cache"}
ALLOWED_SUFFIXES = {".py", ".js", ".mjs", ".css", ".html", ".json", ".md", ".yml",
                    ".yaml", ".toml", ".txt", ".cfg", ".ini"}
BLOCKED_NAMES = {".env", ".env.local", ".env.production", "credentials.json",
                 "secrets.json", "novamind_chats.db"}


def first_argument(payload: str) -> str:
    """Первая непустая строка блока.

    Блок приходит как ``"\\npath\\n"`` (переводы строк вокруг полезной нагрузки),
    поэтому ``splitlines()[0]`` без ``strip`` даёт пустую строку.
    """
    for line in str(payload or "").strip().splitlines():
        value = line.strip()
        if value:
            return value
    return ""


def safe_project_path(raw: str) -> Path:
    """Resolve a relative path inside the repository or raise ValueError."""
    value = str(raw or "").strip().replace("\\", "/")
    if not value or value.startswith("/") or re.match(r"^[A-Za-z]:", value):
        raise ValueError("Нужен относительный путь внутри проекта.")
    parts = Path(value).parts
    if any(part in ("..", ".") for part in parts):
        raise ValueError("Выход за пределы проекта запрещён.")
    if any(part in SKIP_DIRS for part in parts) or Path(value).name.lower() in BLOCKED_NAMES:
        raise ValueError("Этот путь закрыт для агента.")
    path = (PROJECT_ROOT / value).resolve()
    if not path.is_relative_to(PROJECT_ROOT):
        raise ValueError("Выход за пределы проекта запрещён.")
    if path.suffix.lower() not in ALLOWED_SUFFIXES:
        raise ValueError("Тип файла не разрешён для чтения.")
    return path


def iter_source_files():
    root = Path(PROJECT_ROOT).resolve()
    for current, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
        for name in sorted(files):
            path = Path(current) / name
            if name.lower() in BLOCKED_NAMES or path.suffix.lower() not in ALLOWED_SUFFIXES:
                continue
            try:
                # Симлинк может указывать за пределы проекта: такой файл не читаем.
                if path.is_symlink() and not path.resolve().is_relative_to(root):
                    continue
                if path.stat().st_size <= MAX_FILE_BYTES:
                    yield path
            except OSError:
                continue


class _ProjectTool(Tools):
    risk = "safe"

    def __init__(self):
        super().__init__()
        self.work_dir = str(PROJECT_ROOT)
        self.proposals: list[dict] = []

    def resolve_path(self, path: str) -> str:
        return str(safe_project_path(path))


class ProjectTreeTool(_ProjectTool):
    tag = "project_tree"
    name = "ProjectTree"
    description = "Список файлов проекта (относительные пути)."

    def execute(self, payload: str) -> str:
        items = [path.relative_to(PROJECT_ROOT).as_posix() for path in iter_source_files()]
        shown = items[:MAX_TREE]
        more = f"\n… ещё {len(items) - len(shown)} файлов" if len(items) > len(shown) else ""
        return f"[success] Файлы проекта ({len(items)}):\n" + "\n".join(shown) + more


class ProjectReadTool(_ProjectTool):
    tag = "project_read"
    name = "ProjectRead"
    description = "Прочитать файл проекта: путь относительно корня."

    def execute(self, payload: str) -> str:
        path = safe_project_path(first_argument(payload))
        if not path.is_file():
            return f"[failure] Файл не найден: {path.relative_to(PROJECT_ROOT)}"
        if path.stat().st_size > MAX_FILE_BYTES:
            return "[failure] Файл больше лимита чтения."
        text = path.read_text(encoding="utf-8", errors="replace")
        return (f"[success] {path.relative_to(PROJECT_ROOT)} ({len(text)} символов):\n"
                f"{clip(text, 6000)}")


class ProjectSearchTool(_ProjectTool):
    tag = "project_search"
    name = "ProjectSearch"
    description = "Найти строку в файлах проекта: текст запроса."

    def execute(self, payload: str) -> str:
        query = str(payload or "").strip().splitlines()[0][:160].lower()
        if not query:
            return "[failure] Пустой поисковый запрос."
        hits = []
        for path in iter_source_files():
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for number, line in enumerate(lines, 1):
                if query in line.lower():
                    hits.append(f"{path.relative_to(PROJECT_ROOT).as_posix()}:{number}: "
                                f"{line.strip()[:200]}")
                    if len(hits) >= MAX_HITS:
                        break
            if len(hits) >= MAX_HITS:
                break
        if not hits:
            return f"[failure] «{query}» не найдено в проекте."
        return f"[success] Найдено {len(hits)}:\n" + "\n".join(hits)


class ProjectDefsTool(_ProjectTool):
    tag = "project_defs"
    name = "ProjectDefs"
    description = "Найти определения функций и классов: имя."

    def execute(self, payload: str) -> str:
        name = first_argument(payload)
        if not name:
            return "[failure] Нужно имя функции или класса."
        pattern = re.compile(rf"^\s*(?:async\s+def|def|class)\s+{re.escape(name)}\b")
        hits = []
        for path in iter_source_files():
            if path.suffix != ".py":
                continue
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for number, line in enumerate(lines, 1):
                if pattern.search(line):
                    hits.append(f"{path.relative_to(PROJECT_ROOT).as_posix()}:{number}: "
                                f"{line.strip()[:200]}")
        if not hits:
            return f"[failure] Определение «{name}» не найдено."
        return f"[success] Определения «{name}»:\n" + "\n".join(hits[:MAX_HITS])


class ProjectSyntaxTool(_ProjectTool):
    tag = "project_syntax"
    name = "ProjectSyntax"
    description = "Проверить синтаксис Python-файла (ast.parse)."

    def execute(self, payload: str) -> str:
        path = safe_project_path(first_argument(payload))
        if path.suffix != ".py":
            return "[failure] Проверка синтаксиса доступна только для .py."
        try:
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as error:
            return (f"[failure] SyntaxError: {error.msg}, строка {error.lineno}, "
                    f"столбец {error.offset}")
        return f"[success] {path.relative_to(PROJECT_ROOT)}: синтаксических ошибок нет."


class ProjectTestsTool(_ProjectTool):
    tag = "project_tests"
    name = "ProjectTests"
    description = "Запустить один файл tests/test_*.py (pytest)."

    def execute(self, payload: str) -> str:
        relative = str(payload or "").strip().replace("\\", "/")
        if (not relative.startswith("tests/test_") or not relative.endswith(".py")
                or ".." in Path(relative).parts):
            return "[failure] Разрешён только конкретный файл tests/test_*.py."
        path = safe_project_path(relative)
        if not path.is_file():
            return "[failure] Файл тестов не найден."
        env = {key: value for key, value in os.environ.items()
               if not any(secret in key.upper() for secret in
                          ("API_KEY", "TOKEN", "SECRET", "PASSWORD", "PRIVATE_KEY"))}
        env.update({"PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(PROJECT_ROOT)})
        try:
            completed = subprocess.run(
                [sys.executable, "-m", "pytest", relative, "-q"],
                cwd=str(PROJECT_ROOT), env=env, text=True, capture_output=True,
                timeout=TEST_TIMEOUT, check=False)
        except subprocess.TimeoutExpired:
            return f"[failure] Тесты остановлены по тайм-ауту ({TEST_TIMEOUT} с)."
        except OSError as error:
            return f"[failure] Не удалось запустить pytest: {error}"
        output = (completed.stdout or "")[-4000:] + (completed.stderr or "")[-1500:]
        status = "[success]" if completed.returncode == 0 else "[failure]"
        return f"{status} pytest {relative} → код {completed.returncode}:\n{clip(output, 5000)}"


class ProjectPatchTool(_ProjectTool):
    tag = "project_patch"
    name = "ProjectPatch"
    description = "Предложить патч (unified diff или фрагмент) — не применяется."

    risk = "write"

    def __init__(self, emit=None):
        super().__init__()
        self.emit_event = emit

    def execute(self, payload: str) -> str:
        text = str(payload or "").strip()
        if not text:
            return "[failure] Пустой патч."
        path_part, body = "", ""
        if text.startswith("{"):
            # JSON-форма: {"path": "...", "old": "...", "new": "..."} или {"path","patch"}.
            try:
                params = json.loads(text)
            except ValueError:
                return "[failure] Патч похож на JSON, но не разбирается."
            if not isinstance(params, dict):
                return "[failure] Патч должен быть объектом JSON."
            path_part = str(params.get("path") or params.get("file") or "").strip()
            if params.get("old") is not None or params.get("new") is not None:
                body = "\n".join([f"- {line}" for line in str(params.get("old") or "").splitlines()]
                                 + [f"+ {line}" for line in str(params.get("new") or "").splitlines()])
            else:
                body = str(params.get("patch") or params.get("diff") or "").strip()
        else:
            header, _, body = text.partition("\n")
            path_part = header.strip()
            if path_part.startswith("path="):
                path_part = path_part.split("=", 1)[1].strip()
        if not path_part:
            return ("[failure] Нужен путь файла: первая строка блока, "
                    'например routes/agent.py, или {"path": "routes/agent.py", ...}')
        try:
            path = safe_project_path(path_part)
        except ValueError as error:
            return f"[failure] {error}"
        if not path.is_file():
            return f"[failure] Файла {path_part} нет в проекте."
        diff = body.strip() or text
        record = {"path": path.relative_to(PROJECT_ROOT).as_posix(),
                  "patch": clip(diff, 12000), "applied": False}
        self.proposals.append(record)
        if self.emit_event is not None:
            try:
                self.emit_event({"type": "patch", "patch": record})
            except Exception:  # noqa: BLE001
                pass
        return (f"[success] Патч для {record['path']} принят как предложение "
                "(не применён). Скажи пользователю, что изменение нужно проверить вручную.")


def build_project_tools(emit=None) -> dict:
    """The tool set of the coding profile."""
    tools = {
        "project_tree": ProjectTreeTool(),
        "project_read": ProjectReadTool(),
        "project_search": ProjectSearchTool(),
        "project_defs": ProjectDefsTool(),
        "project_syntax": ProjectSyntaxTool(),
        "project_tests": ProjectTestsTool(),
        "project_patch": ProjectPatchTool(emit=emit),
    }
    return tools
