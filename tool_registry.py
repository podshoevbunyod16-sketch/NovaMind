"""Unified tool registry for NovaMind agents."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Callable, Mapping
import logging, time

log = logging.getLogger("novamind.agent")

RISK_LEVELS = ("safe", "write", "danger")


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    schema: Mapping[str, Any]
    execute: Callable[..., dict]
    permissions: tuple[str, ...] = ()
    timeout: float = 30.0
    # risk: safe — только чтение; write — меняет данные; danger — необратимые
    # или внешние действия (публикация, отправка, установка пакетов).
    risk: str = "safe"

    def call(self, **kwargs) -> dict:
        started = time.monotonic()
        try:
            result = self.execute(**kwargs)
            if not isinstance(result, dict):
                result = {"ok": True, "result": result}
            result.setdefault("ok", True)
            result.setdefault("tool", self.name)
            return result
        except Exception as exc:
            log.exception("tool=%s failed", self.name)
            return {"ok": False, "tool": self.name,
                    "error": {"type": exc.__class__.__name__, "message": str(exc),
                              "retryable": isinstance(exc, (TimeoutError, ConnectionError))}}
        finally:
            log.info("tool=%s duration_ms=%d", self.name,
                     int((time.monotonic()-started)*1000))

_REGISTRY: dict[str, ToolSpec] = {}

def register_tool(spec: ToolSpec) -> ToolSpec:
    _REGISTRY[spec.name] = spec
    return spec

def get_tool(name: str) -> ToolSpec | None:
    return _REGISTRY.get(name)

def list_tools() -> list[ToolSpec]:
    return list(_REGISTRY.values())

# Инструменты, которым нужна рабочая папка и вход пользователя.
WORKSPACE_TOOLS = {"shell", "file_read", "file_write", "git", "file_delete"}


def tool_schemas(workspace: bool | None = None) -> list[dict]:
    """Схемы инструментов для промпта агента.

    `workspace=False` скрывает инструменты, которым нужна песочница: гостю
    незачем показывать shell/file/git, которых у него нет.
    """
    specs = list_tools()
    if workspace is not None:
        specs = [t for t in specs if workspace or t.name not in WORKSPACE_TOOLS]
    schemas = [{"name": t.name, "description": t.description, "schema": dict(t.schema),
                "permissions": list(t.permissions), "timeout": t.timeout,
                "risk": t.risk} for t in specs]
    try:
        import mcp_registry
        connected = mcp_registry.connected_tools()
        if connected:
            available = ", ".join(f"{x['server_id']}:{x['name']}" for x in connected)
            # Если mcp_call уже зарегистрирован, не дублируем его, а дополняем
            # каталогом реально подключённых инструментов.
            existing = next((item for item in schemas if item["name"] == "mcp_call"), None)
            catalog = [{
                "server_id": x["server_id"],
                "name": x["name"],
                "description": str(x.get("description") or "")[:500],
                "inputSchema": x.get("inputSchema") or {"type": "object"},
            } for x in connected]
            entry = existing or {
                "name": "mcp_call",
                "description": "Вызвать инструмент подключённого MCP-сервера.",
                "schema": {
                    "type": "object",
                    "properties": {
                        "server_id": {"type": "string"},
                        "tool": {"type": "string"},
                        "arguments": {"type": "object"},
                    },
                    "required": ["server_id", "tool"],
                },
                "permissions": ["network", "external_service"],
                "timeout": 30,
                "risk": "danger",
            }
            entry["description"] = entry["description"].split(". Доступные")[0] + ". Доступные инструменты: " + available
            entry["mcp_tools"] = catalog
            if existing is None:
                schemas.append(entry)
    except Exception:
        pass
    return schemas

# Действия агента (JSON-протокол) и инструменты реестра, которые за ними стоят.
ACTION_TO_TOOL = {
    "search": "web_search", "open": "web_open", "run": "shell",
    "read": "file_read", "write": "file_write", "git": "git",
    "edit": "file_edit", "grep": "file_search", "tree": "file_tree",
    "mcp_call": "mcp_call", "image": "image", "audio": "audio", "video": "video",
    "skill_create": "skill_create", "task": "task", "task_done": "task_done",
}

# Действия, которым нужна рабочая папка и вход пользователя.
WORKSPACE_ACTIONS = {"run", "read", "write", "ls", "git", "edit", "grep", "tree"}

# Описания действий агента для нативного tool calling: имя → (описание, risk).
AGENT_ACTION_META = {
    "search": ("Поиск в интернете: заголовки, ссылки и выдержки по запросу.", "safe"),
    "open": ("Прочитать страницу по полному URL (http/https) и вернуть текст.", "safe"),
    "run": ("Выполнить одну команду в рабочей папке пользователя.", "write"),
    "read": ("Прочитать текстовый файл рабочей папки.", "safe"),
    "write": ("Создать новый текстовый файл рабочей папки (существующие — через edit).", "write"),
    "edit": ("Точечно заменить old→new в файле; old должен встречаться один раз.", "write"),
    "grep": ("Поиск по содержимому файлов рабочей папки (regex).", "safe"),
    "tree": ("Показать дерево файлов рабочей папки.", "safe"),
    "ls": ("Показать файлы и папки рабочей директории.", "safe"),
    "git": ("Выполнить git-команду в рабочей папке.", "write"),
    "mcp_call": ("Вызвать инструмент подключённого MCP-сервера (server_id, tool, arguments).", "danger"),
    "image": ("Сгенерировать изображение по текстовому описанию.", "write"),
    "audio": ("Озвучить текст (text-to-speech).", "write"),
    "video": ("Сгенерировать видео по описанию.", "write"),
    "skill_create": ("Создать skill из готового SKILL.md (name, content).", "write"),
    "schedule": ("Поставить задачу по расписанию: prompt + cron или every "
                 "(например «0 9 * * *» или «1d»). Результат придёт в отдельный чат.", "write"),
    "task": ("Завести задачу с подпунктами (title, detail, steps).", "write"),
    "task_done": ("Закрыть задачу с пометкой о результате (task_id, note).", "write"),
    "remember": ("Сохранить устойчивый факт о пользователе (text, kind, tags). "
                 "Пароли и ключи не сохраняются.", "write"),
    "recall": ("Найти в долгосрочной памяти пользователя (query, limit).", "safe"),
    "forget": ("Удалить воспоминание по id (id).", "write"),
    "answer": ("Финальный ответ пользователю. Вызывай, когда данных достаточно.", "safe"),
}

# Схемы действий, у которых нет ToolSpec в реестре.
_EXTRA_ACTION_SCHEMAS = {
    "ls": {"type": "object", "properties": {}},
    "remember": {"type": "object",
                 "properties": {"text": {"type": "string"},
                                "kind": {"type": "string",
                                         "enum": ["fact", "preference", "project", "decision"]},
                                "tags": {"type": "string"}},
                 "required": ["text"]},
    "recall": {"type": "object",
               "properties": {"query": {"type": "string"},
                              "limit": {"type": "integer"}},
               "required": ["query"]},
    "forget": {"type": "object",
               "properties": {"id": {"type": "string"}},
               "required": ["id"]},
    "schedule": {"type": "object",
                 "properties": {"prompt": {"type": "string",
                                           "description": "что делать при запуске"},
                                "cron": {"type": "string",
                                         "description": "5 полей: минута час день месяц день_недели"},
                                "every": {"type": "string",
                                          "description": "интервал: 30m, 2h, 1d"}},
                 "required": ["prompt"]},
    "answer": {"type": "object",
               "properties": {"text": {"type": "string", "description": "ответ пользователю"}},
               "required": ["text"]},
}


def native_tool_schemas(workspace: bool | None = None, only=None) -> list[dict]:
    """OpenAI-совместимые tools для нативного tool calling.

    Имена инструментов совпадают с действиями JSON-протокола (search, open,
    run, …), поэтому вызов модели превращается в обычный action без
    переходников. Схемы аргументов берутся из реестра ToolSpec.
    """
    specs = {spec.name: spec for spec in list_tools()}
    tools = []
    for action, (description, risk) in AGENT_ACTION_META.items():
        if only is not None and action not in only:
            continue
        if workspace is False and action in WORKSPACE_ACTIONS:
            continue
        spec = specs.get(ACTION_TO_TOOL.get(action, action))
        parameters = dict(spec.schema) if spec and spec.schema else dict(_EXTRA_ACTION_SCHEMAS.get(action) or {})
        if not parameters:
            parameters = {"type": "object", "properties": {}}
        tools.append({"type": "function", "function": {
            "name": action,
            "description": str(description)[:500],
            "parameters": parameters,
        }})
    return tools


def native_tool_risk(name: str) -> str:
    """Уровень риска действия агента: safe | write | danger."""
    action = str(name or "")
    spec = get_tool(ACTION_TO_TOOL.get(action, action))
    if spec is not None:
        return spec.risk
    return AGENT_ACTION_META.get(action, ("", "safe"))[1]


def call_tool(name: str, **kwargs) -> dict:
    spec = get_tool(name)
    if not spec:
        return {"ok": False, "error": {"type": "unknown_tool", "message": f"Unknown tool: {name}", "retryable": False}}
    return spec.call(**kwargs)

def bootstrap_default_tools():
    _register_agent_tools()
    if get_tool("web_search") is None:
        register_tool(ToolSpec(
            "web_search", "Search the public web and return normalized ranked sources.",
            {"type":"object","properties":{"query":{"type":"string"},"limit":{"type":"integer"}},"required":["query"]},
            lambda query, limit=8: _search_result(__import__("routes.search", fromlist=["search_web"]).search_web(query, num=min(int(limit),12))),
            permissions=("network",), timeout=20))
        register_tool(ToolSpec(
            "web_open", "Open a public HTTP(S) page and extract readable text.",
            {"type":"object","properties":{"url":{"type":"string"},"max_chars":{"type":"integer"}},"required":["url"]},
            lambda url, max_chars=6000: _open_result(__import__("routes.search", fromlist=["fetch_page_text"]).fetch_page_text(url, max_chars=min(int(max_chars),8000))),
            permissions=("network",), timeout=15))
    if get_tool("shell") is None:
        from routes.terminal import run_agent
        register_tool(ToolSpec(
            "shell", "Run a policy-checked command inside the workspace.",
            {"type":"object","properties":{"command":{"type":"string"},"timeout":{"type":"number"}},"required":["command"]},
            lambda command, timeout=12, cancel=None: _workspace_guard() or _shell_result(
                run_agent(command, timeout=min(float(timeout),30), cancel=cancel)),
            permissions=("workspace",), timeout=30, risk="write"))
    if get_tool("file_read") is None:
        from routes.terminal import safe_path
        def read_file(path, max_chars=60000):
            import os
            denied = _workspace_guard()
            if denied:
                return denied
            p=safe_path(path)
            if not os.path.isfile(p):
                return {"ok":False,"error":{"type":"not_found","message":"File not found","retryable":False}}
            with open(p,"r",encoding="utf-8",errors="replace") as f:
                data=f.read(int(max_chars))
            return {"ok":True,"path":path,"content":data}
        register_tool(ToolSpec(
            "file_read", "Read a text file inside workspace.",
            {"type":"object","properties":{"path":{"type":"string"},"max_chars":{"type":"integer"}},"required":["path"]},
            read_file, permissions=("workspace",), timeout=5))

    if get_tool("file_write") is None:
        from routes.terminal import safe_path, relative_to_workspace
        def write_file(path, content="", allow_overwrite=False):
            import os
            denied = _workspace_guard()
            if denied:
                return denied
            p = safe_path(path)
            if os.path.exists(p) and not allow_overwrite:
                # Перезапись целого файла — только для новых файлов:
                # существующий правится точечно через file_edit.
                return {"ok": False, "error": {
                    "type": "file_exists",
                    "message": "Файл уже существует — используйте file_edit (edit) для точечной правки",
                    "retryable": False}}
            os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                f.write(str(content))
            return {"ok": True, "path": relative_to_workspace(p),
                    "bytes": len(str(content).encode("utf-8"))}
        register_tool(ToolSpec(
            "file_write", "Создать новый текстовый файл в рабочей папке (существующие правятся file_edit).",
            {"type":"object","properties":{"path":{"type":"string"},"content":{"type":"string"},
                                           "allow_overwrite":{"type":"boolean"}},
             "required":["path","content"]},
            write_file, permissions=("workspace",), timeout=5, risk="write"))

    if get_tool("file_edit") is None:
        from routes.terminal import safe_path, relative_to_workspace

        def file_edit(path, old, new, replace_all=False):
            """Точечная замена с проверкой уникальности вхождения."""
            import os
            denied = _workspace_guard()
            if denied:
                return denied
            p = safe_path(path)
            if not os.path.isfile(p):
                return {"ok": False, "error": {"type": "not_found",
                                               "message": "Файл не найден", "retryable": False}}
            with open(p, "r", encoding="utf-8", errors="replace") as handle:
                text = handle.read()
            needle = str(old or "")
            if not needle:
                return {"ok": False, "error": {"type": "bad_arguments",
                                               "message": "Нужен непустой old (что заменить)",
                                               "retryable": False}}
            count = text.count(needle)
            if count == 0:
                return {"ok": False, "error": {"type": "not_found",
                                               "message": "Такое вхождение не найдено — прочитайте файл и уточните old",
                                               "retryable": True}}
            if count > 1 and not replace_all:
                return {"ok": False, "error": {"type": "ambiguous",
                                               "message": f"Вхождение встречается {count} раз — уточните old или передайте replace_all=true",
                                               "retryable": True}}
            updated = text.replace(needle, str(new or ""), -1 if replace_all else 1)
            with open(p, "w", encoding="utf-8") as handle:
                handle.write(updated)
            return {"ok": True, "path": relative_to_workspace(p),
                    "replaced": count if replace_all else 1,
                    "diff": _simple_diff(text, updated, relative_to_workspace(p)),
                    "lines_before": text.count("\n") + 1,
                    "lines_after": updated.count("\n") + 1}
        register_tool(ToolSpec(
            "file_edit", "Точечная замена old→new в файле; old обязан встречаться один раз.",
            {"type": "object",
             "properties": {"path": {"type": "string"}, "old": {"type": "string"},
                            "new": {"type": "string"}, "replace_all": {"type": "boolean"}},
             "required": ["path", "old", "new"]},
            file_edit, permissions=("workspace",), timeout=5, risk="write"))

    if get_tool("file_search") is None:
        from routes.terminal import safe_path, relative_to_workspace

        def file_search(pattern, path=".", ignore_case=True, limit=100):
            """Поиск по содержимому файлов рабочей папки (без grep и shell)."""
            import os
            import re
            denied = _workspace_guard()
            if denied:
                return denied
            needle = str(pattern or "")
            if not needle:
                return {"ok": False, "error": {"type": "bad_arguments",
                                               "message": "Нужен непустой pattern",
                                               "retryable": False}}
            try:
                regex = re.compile(needle, re.IGNORECASE if ignore_case else 0)
            except re.error as exc:
                return {"ok": False, "error": {"type": "bad_regex", "message": str(exc),
                                               "retryable": False}}
            root = safe_path(path if path not in (None, "", ".") else "")
            matches = []
            skipped_dirs = {".git", "__pycache__", "node_modules", ".venv", "venv"}
            for base, dirs, files in os.walk(root):
                dirs[:] = [name for name in dirs if name not in skipped_dirs]
                for name in sorted(files):
                    full = os.path.join(base, name)
                    try:
                        if os.path.getsize(full) > 512 * 1024:
                            continue
                        with open(full, "r", encoding="utf-8", errors="replace") as handle:
                            for number, line in enumerate(handle, 1):
                                if regex.search(line):
                                    matches.append({"path": relative_to_workspace(full),
                                                    "line": number, "text": line.rstrip()[:300]})
                                    if len(matches) >= int(limit or 100):
                                        break
                    except OSError:
                        continue
                    if len(matches) >= int(limit or 100):
                        break
                if len(matches) >= int(limit or 100):
                    break
            return {"ok": True, "matches": matches, "count": len(matches)}
        register_tool(ToolSpec(
            "file_search", "Поиск по содержимому файлов рабочей папки (regex).",
            {"type": "object",
             "properties": {"pattern": {"type": "string"}, "path": {"type": "string"},
                            "ignore_case": {"type": "boolean"}, "limit": {"type": "integer"}},
             "required": ["pattern"]},
            file_search, permissions=("workspace",), timeout=20))

    if get_tool("file_tree") is None:
        from routes.terminal import safe_path, relative_to_workspace

        def file_tree(path=".", depth=2, limit=200):
            """Дерево файлов рабочей папки (без shell и без find)."""
            import os
            denied = _workspace_guard()
            if denied:
                return denied
            root = safe_path(path if path not in (None, "", ".") else "")
            depth = max(1, min(int(depth or 2), 6))
            limit = max(1, min(int(limit or 200), 1000))
            root_depth = root.rstrip(os.sep).count(os.sep)
            entries = []
            for base, dirs, files in os.walk(root):
                level = base.rstrip(os.sep).count(os.sep) - root_depth
                if level >= depth:
                    dirs[:] = []
                dirs.sort()
                for name in sorted(dirs):
                    entries.append(relative_to_workspace(os.path.join(base, name)) + "/")
                for name in sorted(files):
                    entries.append(relative_to_workspace(os.path.join(base, name)))
                if len(entries) >= limit:
                    break
            return {"ok": True, "tree": entries[:limit], "count": len(entries[:limit])}
        register_tool(ToolSpec(
            "file_tree", "Дерево файлов и папок рабочей директории.",
            {"type": "object",
             "properties": {"path": {"type": "string"}, "depth": {"type": "integer"},
                            "limit": {"type": "integer"}}},
            file_tree, permissions=("workspace",), timeout=10))

    if get_tool("git") is None:
        from routes.terminal import run_agent
        def git(command="status --short", timeout=12):
            denied = _workspace_guard()
            if denied:
                return denied
            cmd = str(command).strip()
            if not cmd.startswith("git"):
                cmd = "git " + cmd
            return _shell_result(run_agent(cmd, timeout=min(float(timeout), 30)))
        register_tool(ToolSpec(
            "git", "Run a Git command inside the workspace.",
            {"type":"object","properties":{"command":{"type":"string"},"timeout":{"type":"number"}},"required":["command"]},
            git, permissions=("workspace",), timeout=30, risk="write"))

def _register_agent_tools():
    """Инструменты уровня агента, доступные и нативному tool calling.

    Раньше image/audio/video/skill_create/task/task_done/mcp_call жили
    только внутри routes/agent.py и не попадали в реестр, поэтому модель
    в нативном режиме о них не знала.
    """
    if get_tool("mcp_call") is None:
        def mcp_call(server_id, tool, arguments=None):
            import mcp_registry
            if not str(server_id or "").strip() or not str(tool or "").strip():
                return {"ok": False, "error": {"type": "bad_arguments",
                                               "message": "нужны server_id и tool",
                                               "retryable": False}}
            result = mcp_registry.call_tool(str(server_id), str(tool), arguments or {})
            if isinstance(result, dict) and result.get("error"):
                return {"ok": False, "result": result,
                        "error": {"type": "mcp_error", "message": str(result.get("error"))[:500],
                                  "retryable": True}}
            return {"ok": True, "result": result}
        register_tool(ToolSpec(
            "mcp_call", "Вызвать инструмент подключённого MCP-сервера.",
            {"type": "object",
             "properties": {"server_id": {"type": "string"},
                            "tool": {"type": "string"},
                            "arguments": {"type": "object"}},
             "required": ["server_id", "tool"]},
            mcp_call, permissions=("network", "external_service"), timeout=30,
            risk="danger"))

    for kind, title in (("image", "изображение"), ("audio", "аудио"), ("video", "видео")):
        name = kind
        if get_tool(name) is not None:
            continue

        def media(kind=kind, prompt="", model="", size="", voice=""):
            import media_agent
            action = {"prompt": str(prompt), "model": model, "size": size, "voice": voice}
            options = media_agent.build_options(kind, action)
            result, used_model, errors = media_agent.generate(
                kind, str(prompt), options=options, wanted_model=str(model or ""))
            if not result:
                return {"ok": False, "kind": kind,
                        "error": {"type": "media_failed",
                                  "message": "; ".join(str(e) for e in (errors or []))[:500] or "модель не ответила",
                                  "retryable": True}}
            return {"ok": True, "kind": kind, "model": used_model, "result": result}
        register_tool(ToolSpec(
            name, f"Сгенерировать {title} по текстовому описанию.",
            {"type": "object",
             "properties": {"prompt": {"type": "string"}, "model": {"type": "string"},
                            "size": {"type": "string"}, "voice": {"type": "string"}},
             "required": ["prompt"]},
            media, permissions=("network",), timeout=180, risk="write"))

    if get_tool("skill_create") is None:
        def skill_create(name, content):
            import skills_registry
            try:
                item = skills_registry.save_text(str(name), str(content or ""))
            except ValueError as exc:
                return {"ok": False, "error": {"type": "invalid_skill", "message": str(exc),
                                               "retryable": False}}
            return {"ok": True, "skill": item}
        register_tool(ToolSpec(
            "skill_create", "Создать skill (SKILL.md) из готового текста.",
            {"type": "object",
             "properties": {"name": {"type": "string"}, "content": {"type": "string"}},
             "required": ["name", "content"]},
            skill_create, permissions=("workspace",), timeout=5, risk="write"))

    if get_tool("remember") is None:
        def _memory_key():
            try:
                from flask import has_request_context, session
                from routes.terminal import signed_in
                import memory as memory_module
                if not has_request_context():
                    return ""
                return memory_module.user_key(session, signed_in())
            except Exception:                      # noqa: BLE001
                return ""

        def remember(text, kind="fact", tags=""):
            import memory as memory_module
            key = _memory_key()
            if not key:
                return {"ok": False, "error": {"type": "unauthorized",
                                               "message": "Память доступна только после входа",
                                               "retryable": False}}
            try:
                return {"ok": True, "item": memory_module.remember(key, text, kind, tags)}
            except ValueError as exc:
                return {"ok": False, "error": {"type": "rejected", "message": str(exc),
                                               "retryable": False}}

        def recall(query="", limit=5):
            import memory as memory_module
            key = _memory_key()
            if not key:
                return {"ok": False, "error": {"type": "unauthorized",
                                               "message": "Память недоступна", "retryable": False}}
            items = memory_module.recall(key, query, limit=int(limit or 5))
            return {"ok": True, "items": items, "count": len(items)}

        def forget(id):                            # noqa: A002 — имя из задачи
            import memory as memory_module
            key = _memory_key()
            if not key:
                return {"ok": False, "error": {"type": "unauthorized",
                                               "message": "Память недоступна", "retryable": False}}
            removed = memory_module.forget(key, id)
            return {"ok": removed, "id": id}

        register_tool(ToolSpec(
            "remember", "Сохранить устойчивый факт в долгосрочную память.",
            {"type": "object",
             "properties": {"text": {"type": "string"},
                            "kind": {"type": "string",
                                     "enum": ["fact", "preference", "project", "decision"]},
                            "tags": {"type": "string"}},
             "required": ["text"]},
            remember, permissions=("database",), timeout=5, risk="write"))
        register_tool(ToolSpec(
            "recall", "Найти воспоминания по запросу.",
            {"type": "object", "properties": {"query": {"type": "string"},
                                              "limit": {"type": "integer"}},
             "required": ["query"]},
            recall, permissions=("database",), timeout=5, risk="safe"))
        register_tool(ToolSpec(
            "forget", "Удалить воспоминание по id.",
            {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
            forget, permissions=("database",), timeout=5, risk="write"))

    if get_tool("task") is None:
        def task(title, detail="", steps=None):
            import database
            created = database.create_task(str(title), detail=str(detail or ""),
                                           source="agent", steps=steps)
            return {"ok": bool(created), "task": created}
        register_tool(ToolSpec(
            "task", "Завести задачу с подпунктами в detail.",
            {"type": "object",
             "properties": {"title": {"type": "string"}, "detail": {"type": "string"},
                            "steps": {"type": "array", "items": {"type": "string"}}},
             "required": ["title"]},
            task, permissions=("database",), timeout=5, risk="write"))

    if get_tool("task_done") is None:
        def task_done(task_id, note=""):
            import database
            updated = database.update_task(str(task_id), status="done")
            if not updated:
                return {"ok": False, "error": {"type": "not_found", "message": "задача не найдена",
                                               "retryable": False}}
            database.append_task_step(str(task_id), str(note or "Готово"), status="done",
                                      log=str(note or "")[:1000])
            return {"ok": True, "task": database.get_task(str(task_id))}
        register_tool(ToolSpec(
            "task_done", "Закрыть задачу с пометкой о результате.",
            {"type": "object",
             "properties": {"task_id": {"type": "string"}, "note": {"type": "string"}},
             "required": ["task_id"]},
            task_done, permissions=("database",), timeout=5, risk="write"))


def _workspace_guard():
    """Единая проверка доступа к песочнице для инструментов реестра.

    Маршруты Flask проверяют доступ сами, но реестр вызывается и из агента,
    и из будущего кода. Без этой проверки shell/file могли обойти вход.
    """
    try:
        from routes.terminal import is_enabled, can_use_workspace
        if not is_enabled():
            return {"ok": False, "error": {"type": "disabled",
                                           "message": "TERMINAL_ENABLED=0: рабочая папка выключена",
                                           "retryable": False}}
        if not can_use_workspace():
            return {"ok": False, "error": {"type": "unauthorized",
                                           "message": "Нужен вход в аккаунт для работы с файлами",
                                           "retryable": False}}
    except Exception as exc:                 # импорт не должен ломать инструмент
        return {"ok": False, "error": {"type": "unavailable", "message": str(exc),
                                       "retryable": False}}
    return None


def _simple_diff(before, after, path="", context=3):
    """Компактный diff «было/стало» по строкам — для отчёта пользователю."""
    import difflib
    lines = list(difflib.unified_diff(str(before).splitlines(), str(after).splitlines(),
                                      fromfile=f"{path} (было)", tofile=f"{path} (стало)",
                                      lineterm="", n=context))
    text = "\n".join(lines)
    return text[:4000] + ("\n… diff обрезан" if len(text) > 4000 else "")


def _search_result(pair):
    results, trace = pair
    return {"ok": bool(results), "results": results, "trace": trace, "count": len(results)}

def _open_result(pair):
    text, error = pair
    return {"ok": bool(text), "content": text or "",
            "error": None if text else {"type":"fetch_failed","message":error or "empty page","retryable":True}}

def _shell_result(result):
    ok = int(result.get("exit_code", result.get("code",-1))) == 0 and not result.get("timed_out")
    return {**result, "ok": ok, "exit_code": result.get("exit_code", result.get("code",-1)),
            "error": None if ok else {"type":"command_failed" if not result.get("timed_out") else "timeout",
                                      "message": result.get("stderr") or "command failed",
                                      "retryable": bool(result.get("timed_out"))}}
