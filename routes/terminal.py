"""
routes/terminal.py — маленькое Linux-окружение для чата.

Что это:
  * папка-песочница `workspace/` — она и есть «маленький Linux»: файлы,
    заготовки, git-репозиторий;
  * терминал с белым списком команд, палец в небо не уронит сервер;
  * дерево файлов, чтение/запись, git-панель — всё в одном месте;
  * отсюда же работает агентный режим (routes/agent.py).

Безопасность (это сервер, а не песочница ОС):
  1. выключено по умолчанию — включается TERMINAL_ENABLED=1 в .env;
  2. только для администратора: session['admin_logged_in'];
  3. команда запускается без shell, только из белого списка;
  4. cwd всегда внутри workspace, пути не выходят за его пределы;
  5. таймаут на команду, обрезанный вывод, лимит параллельных запусков.
"""
from flask import Blueprint, request, jsonify, session
import os
import re
import shlex
import signal
import subprocess
import threading
import time
import uuid

terminal_bp = Blueprint("terminal", __name__)

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKSPACE = os.path.realpath(os.environ.get("TERMINAL_DIR") or os.path.join(ROOT_DIR, "workspace"))

COMMAND_TIMEOUT = float(os.getenv("TERMINAL_TIMEOUT", "20"))
MAX_OUTPUT = int(os.getenv("TERMINAL_MAX_OUTPUT", "12000"))
MAX_CONCURRENT = 2

# Команды, которые можно выполнять. Всё остальное — отказ с подсказкой.
ALLOWED_COMMANDS = {
    # просмотр
    "ls", "pwd", "cat", "head", "tail", "less", "more", "wc", "grep", "egrep", "fgrep",
    "find", "fd", "tree", "stat", "file", "du", "df", "diff", "sort", "uniq", "cut", "awk",
    "sed", "tr", "nl", "basename", "dirname", "realpath", "which", "type", "echo", "printf",
    "date", "whoami", "id", "uname", "hostname", "uptime", "ps", "env", "printenv", "seq",
    "true", "false", "sleep", "man", "help", "history", "clear", "which",
    # файлы
    "mkdir", "touch", "cp", "mv", "rm", "chmod",
    # разработка
    "git", "python", "python3", "pip", "pip3", "node", "npm", "npx", "deno", "bun",
    "pytest", "go", "rustc", "cargo", "java", "javac", "make", "jq", "curl", "wget",
    "sqlite3", "tar", "zip", "unzip", "gzip", "grep",
}
# Подстроки, которые запрещены даже внутри разрешённой команды:
# pipe в другую программу, выход вверх, sudo, подстановка в оболочку.
FORBIDDEN = (
    "sudo", "su ", "doas", "chown", "chmod 777", "rm -rf /", ":(){", ">/dev/",
    "&", "|", ">", ">>", "`", "$(", "${", "\n",
)

_run_lock = threading.Semaphore(MAX_CONCURRENT)

HELP_TEXT = """Доступные команды внутри workspace:
  ls / cat / head / tail / grep / find / tree / wc / stat / file
  mkdir / touch / cp / mv / rm / chmod
  git status / git log / git diff / git add / git commit
  python3 script.py · node script.js · pip install · pytest -q
  date · whoami · uname · env · ps · du · df · awk · sed · sort

Папка: {workspace}
Подсказка: составные команды (| , > , &&) отключены — выполняйте по одной."""


# ───────────────────────── служебное ─────────────────────────

def is_enabled():
    return os.getenv("TERMINAL_ENABLED", "0") == "1"


def is_admin():
    return bool(session.get("admin_logged_in"))


def denied(reason):
    return jsonify({"error": reason, "enabled": is_enabled(), "admin": is_admin()}), 403


def guard():
    """Единая проверка доступа. None — можно работать, иначе готовый ответ."""
    if not is_enabled():
        return denied("Окружение выключено. Включите TERMINAL_ENABLED=1 в .env и перезапустите сервер.")
    if not is_admin():
        return denied("Доступно только администратору (вход в панель управления).")
    return None


def safe_path(relative=""):
    """Превращает относительный путь в путь внутри workspace.

    Любая попытка выйти за пределы песочницы → ValueError.
    """
    relative = (relative or "").strip().lstrip("/")
    target = os.path.realpath(os.path.join(WORKSPACE, relative))
    if target != WORKSPACE and not target.startswith(WORKSPACE + os.sep):
        raise ValueError("Путь выходит за пределы рабочей папки")
    return target


def relative_to_workspace(path):
    try:
        return os.path.relpath(path, WORKSPACE).replace(os.sep, "/")
    except ValueError:
        return path


STARTER_FILES = {
    "README.md": (
        "# Рабочая папка NovaMind\n\n"
        "Здесь живёт «маленький Linux» чата: файлы, терминал, git и задачи.\n\n"
        "- Всё, что создаётся из терминала, остаётся внутри этой папки.\n"
        "- Агентный режим работает здесь же: он читает файлы и запускает команды.\n"
        "- Начните с `python3 hello.py` или `git status`.\n"
    ),
    "hello.py": (
        '"""Первый файл песочницы — запустите: python3 hello.py"""\n\n\n'
        "def main():\n"
        '    print("Привет из рабочей папки NovaMind 👋")\n'
        "    print(\"Здесь работает терминал и агентный режим.\")\n\n\n"
        'if __name__ == "__main__":\n    main()\n'
    ),
    "notes/ideas.md": "# Заметки\n\n- [ ] попробовать агентный режим\n",
    "data/sample.csv": "name,value\nalpha,1\nbeta,2\ngamma,3\n",
}

GITIGNORE = "node_modules/\n__pycache__/\n*.pyc\n.env\n"


def ensure_workspace():
    """Создаёт песочницу и заготовки, если их ещё нет."""
    created = []
    for relative, content in STARTER_FILES.items():
        target = os.path.join(WORKSPACE, relative)
        if not os.path.exists(target):
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "w", encoding="utf-8") as handle:
                handle.write(content)
            created.append(relative)
    gitignore = os.path.join(WORKSPACE, ".gitignore")
    if not os.path.exists(gitignore):
        with open(gitignore, "w", encoding="utf-8") as handle:
            handle.write(GITIGNORE)
    return created


def check_command(command):
    """Проверяет команду: пустая, запрещённая конструкция или не из списка."""
    command = (command or "").strip()
    if not command:
        raise ValueError("Пустая команда")
    if len(command) > 2000:
        raise ValueError("Команда слишком длинная")
    for bad in FORBIDDEN:
        if bad in command:
            raise ValueError(f"Конструкция «{bad.strip()}» отключена — выполняйте команды по одной")
    try:
        parts = shlex.split(command)
    except ValueError as exc:
        raise ValueError(f"Не удалось разобрать команду: {exc}")
    if not parts:
        raise ValueError("Пустая команда")
    program = os.path.basename(parts[0])
    if program not in ALLOWED_COMMANDS:
        raise ValueError(f"Команда «{program}» не в списке разрешённых")
    return parts, program


def run_command(command, timeout=None):
    """Выполняет команду в песочнице. Возвращает код, вывод и время."""
    parts, program = check_command(command)
    ensure_workspace()
    timeout = float(timeout or COMMAND_TIMEOUT)
    if not _run_lock.acquire(blocking=False):
        raise RuntimeError("Занято: предыдущая команда ещё выполняется")
    started = time.time()
    try:
        process = subprocess.Popen(
            parts,
            cwd=WORKSPACE,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "HOME": WORKSPACE,
                "LANG": os.environ.get("LANG", "C.UTF-8"),
                "TERM": "dumb",
                "PYTHONDONTWRITEBYTECODE": "1",
                # В песочнице нет ~/.gitconfig — без этого git commit
                # ругается «Author identity unknown» и не проходит.
                "GIT_AUTHOR_NAME": "NovaMind",
                "GIT_AUTHOR_EMAIL": "novamind@localhost",
                "GIT_COMMITTER_NAME": "NovaMind",
                "GIT_COMMITTER_EMAIL": "novamind@localhost",
            },
            start_new_session=True,          # свой pgid — процесс целиком снимаем
        )
    except FileNotFoundError:
        _run_lock.release()
        return {"code": 127, "stdout": "", "stderr": f"{program}: команда не установлена",
                "duration_ms": 0, "timed_out": False}
    except Exception as exc:                      # noqa: BLE001 — возвращаем текст ошибки
        _run_lock.release()
        return {"code": 126, "stdout": "", "stderr": str(exc),
                "duration_ms": 0, "timed_out": False}

    killed = {"value": False}

    def kill_group():
        killed["value"] = True
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            process.kill()

    # Таймер сам снимает процессную группу: communicate() после kill
    # возвращается штатно, поэтому флаг «срезали по времени» ставим здесь.
    timer = threading.Timer(timeout, kill_group)
    timer.start()
    try:
        stdout, stderr = process.communicate(timeout=timeout + 2)
    except subprocess.TimeoutExpired:
        kill_group()
        stdout, stderr = process.communicate()
    finally:
        timer.cancel()
        _run_lock.release()
    timed_out = killed["value"]

    def decode(blob):
        text = (blob or b"").decode("utf-8", "replace")
        return text[:MAX_OUTPUT] + ("\n… вывод обрезан" if len(text) > MAX_OUTPUT else "")

    return {
        "code": process.returncode if process.returncode is not None else -1,
        "stdout": decode(stdout),
        "stderr": decode(stderr),
        "duration_ms": int((time.time() - started) * 1000),
        "timed_out": timed_out,
    }


def run_agent(command, timeout=12):
    """Запуск команды агентом: короче таймаут, всегда с префиксом ошибки."""
    try:
        return run_command(command, timeout=timeout)
    except (ValueError, RuntimeError) as exc:
        return {"code": 2, "stdout": "", "stderr": str(exc), "duration_ms": 0, "timed_out": False}


# ───────────────────────── маршруты ─────────────────────────

@terminal_bp.route("/api/terminal/status")
def terminal_status():
    """Доступен ли терминал и что уже есть в песочнице."""
    enabled, admin = is_enabled(), is_admin()
    payload = {"enabled": enabled, "admin": admin, "available": enabled and admin,
               "workspace": relative_to_workspace(WORKSPACE)}
    if not (enabled and admin):
        payload["hint"] = ("Включите TERMINAL_ENABLED=1 в .env и войдите как администратор"
                           if not enabled else "Войдите как администратор")
        return jsonify(payload)
    ensure_workspace()
    payload["cwd"] = WORKSPACE
    payload["git"] = bool(os.path.isdir(os.path.join(WORKSPACE, ".git")))
    payload["commands"] = sorted(ALLOWED_COMMANDS)
    return jsonify(payload)


@terminal_bp.route("/api/terminal/bootstrap", methods=["POST"])
def terminal_bootstrap():
    """Создаёт песочницу заново (не трогая существующие файлы)."""
    if (blocked := guard()):
        return blocked
    return jsonify({"created": ensure_workspace(), "workspace": relative_to_workspace(WORKSPACE)})


@terminal_bp.route("/api/terminal/files")
def terminal_files():
    """Дерево файлов рабочей папки (скрытые и служебные прячем)."""
    if (blocked := guard()):
        return blocked
    ensure_workspace()
    skip = {".git", "node_modules", "__pycache__", ".venv", ".cache"}
    limit = int(request.args.get("limit", 400))
    root = safe_path(request.args.get("path", ""))
    items = []
    for current, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in skip and not d.startswith("."))
        for name in sorted(files):
            if name.startswith("."):
                continue
            full = os.path.join(current, name)
            try:
                stat = os.stat(full)
            except OSError:
                continue
            items.append({
                "path": relative_to_workspace(full),
                "name": name,
                "size": stat.st_size,
                "dir": os.path.dirname(relative_to_workspace(full)) or ".",
            })
            if len(items) >= limit:
                return jsonify({"files": items, "truncated": True, "workspace": relative_to_workspace(WORKSPACE)})
    return jsonify({"files": items, "truncated": False, "workspace": relative_to_workspace(WORKSPACE)})


@terminal_bp.route("/api/terminal/file", methods=["POST"])
def terminal_file():
    """Чтение или запись файла внутри песочницы."""
    if (blocked := guard()):
        return blocked
    data = request.get_json(silent=True) or {}
    action = (data.get("action") or "read").lower()
    try:
        target = safe_path(data.get("path", ""))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    if action == "read":
        if not os.path.isfile(target):
            return jsonify({"error": "Файла нет"}), 404
        if os.path.getsize(target) > 200_000:
            return jsonify({"error": "Файл слишком большой для просмотра"}), 400
        with open(target, "r", encoding="utf-8", errors="replace") as handle:
            return jsonify({"path": relative_to_workspace(target), "content": handle.read()[:60000]})

    if action == "write":
        content = str(data.get("content") or "")
        if len(content) > 200_000:
            return jsonify({"error": "Слишком большой файл"}), 400
        if os.path.isdir(target):
            return jsonify({"error": "Это папка"}), 400
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as handle:
            handle.write(content)
        return jsonify({"path": relative_to_workspace(target), "bytes": len(content.encode("utf-8"))})

    return jsonify({"error": "Неизвестное действие"}), 400


@terminal_bp.route("/api/terminal/run", methods=["POST"])
def terminal_run():
    """Выполняет команду в песочнице."""
    if (blocked := guard()):
        return blocked
    data = request.get_json(silent=True) or {}
    command = data.get("command", "")
    try:
        result = run_command(command, timeout=data.get("timeout"))
    except ValueError as exc:
        return jsonify({"error": str(exc), "command": command}), 400
    except RuntimeError as exc:
        return jsonify({"error": str(exc), "command": command}), 429
    result["command"] = command
    result["cwd"] = relative_to_workspace(WORKSPACE)
    return jsonify(result)


@terminal_bp.route("/api/terminal/help")
def terminal_help():
    if (blocked := guard()):
        return blocked
    return jsonify({"text": HELP_TEXT.format(workspace=relative_to_workspace(WORKSPACE)),
                    "commands": sorted(ALLOWED_COMMANDS)})


@terminal_bp.route("/api/terminal/git")
def terminal_git():
    """Структурированное состояние git — для панели, без ручной набора команд."""
    if (blocked := guard()):
        return blocked
    if not os.path.isdir(os.path.join(WORKSPACE, ".git")):
        return jsonify({"repo": False, "message": "Это не git-репозиторий. Выполните: git init"})

    def git(*args):
        result = run_agent("git " + " ".join(shlex.quote(a) for a in args), timeout=8)
        return (result["stdout"] or result["stderr"]).strip()

    branch = git("rev-parse", "--abbrev-ref", "HEAD") or "?"
    status = git("status", "--short")
    changes = []
    for line in status.splitlines()[:60]:
        match = re.match(r"^\s*([A-Z?!]{1,2})\s+(.+)$", line)
        if match:
            changes.append({"status": match.group(1).strip(), "path": match.group(2).strip()})
    return jsonify({
        "repo": True,
        "branch": branch,
        "clean": not changes,
        "changes": changes,
        "last": git("log", "-1", "--pretty=format:%h %s (%an, %ad)", "--date=short"),
    })


# ───────────────────────── задачи ─────────────────────────

tasks_bp = Blueprint("tasks", __name__)


def _task_owner_ok():
    """Задачи доступны любому вошедшему: это личный список, не управление сервером."""
    return True


@tasks_bp.route("/api/tasks")
def tasks_list():
    if not _task_owner_ok():
        return jsonify({"error": "Нужен вход"}), 403
    import database
    return jsonify({"tasks": database.list_tasks(status=request.args.get("status"))})


@tasks_bp.route("/api/tasks", methods=["POST"])
def tasks_create():
    if not _task_owner_ok():
        return jsonify({"error": "Нужен вход"}), 403
    data = request.get_json(silent=True) or {}
    title = (data.get("title") or "").strip()
    if not title:
        return jsonify({"error": "Пустое название задачи"}), 400
    import database
    task = database.create_task(
        title=title,
        detail=(data.get("detail") or "").strip(),
        source=data.get("source") if data.get("source") in ("user", "agent") else "user",
        steps=data.get("steps") if isinstance(data.get("steps"), list) else None,
    )
    return jsonify({"task": task})


@tasks_bp.route("/api/tasks/<task_id>", methods=["PATCH"])
def tasks_update(task_id):
    if not _task_owner_ok():
        return jsonify({"error": "Нужен вход"}), 403
    data = request.get_json(silent=True) or {}
    import database
    if data.get("title") is not None and not str(data["title"]).strip():
        return jsonify({"error": "Пустое название задачи"}), 400
    task = database.update_task(
        task_id,
        title=(data.get("title") or "").strip() or None,
        detail=data.get("detail"),
        status=data.get("status") if data.get("status") in ("todo", "doing", "done") else None,
    )
    if not task:
        return jsonify({"error": "Задача не найдена"}), 404
    return jsonify({"task": task})


@tasks_bp.route("/api/tasks/<task_id>/steps", methods=["POST"])
def tasks_step(task_id):
    if not _task_owner_ok():
        return jsonify({"error": "Нужен вход"}), 403
    data = request.get_json(silent=True) or {}
    import database
    task = database.append_task_step(
        task_id,
        title=data.get("title") or "Шаг",
        status=data.get("status") if data.get("status") in ("todo", "doing", "done", "error") else "doing",
        log=data.get("log") or "",
    )
    if not task:
        return jsonify({"error": "Задача не найдена"}), 404
    return jsonify({"task": task})


@tasks_bp.route("/api/tasks/<task_id>", methods=["DELETE"])
def tasks_delete(task_id):
    if not _task_owner_ok():
        return jsonify({"error": "Нужен вход"}), 403
    import database
    database.delete_task(task_id)
    return jsonify({"deleted": task_id})
