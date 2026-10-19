"""
routes/agent.py — агентный режим.

Модель не отвечает сразу, а планирует и действует инструментами:

    план → [команда в песочнице | чтение/запись файла | поиск | задача] × N → ответ

Поток событий — NDJSON, ровно как у поиска: интерфейс показывает
`plan`, `step`, `tool`, `sources` и печатающийся ответ.

Главное правило: пользователь ВСЕГДА получает ответ. Что бы ни случилось —
лимит шагов, таймаут, зацикливание модели, битый JSON — собранные данные
уходят в финальный запрос, и модель отвечает по ним обычным текстом.

Границы (важно — это сервер, а не песочница ОС):
  * AGENT_ENABLED=1 в .env, иначе режим выключен;
  * AGENT_MAX_STEPS шагов и общий AGENT_TIMEOUT — цикл не крутится вечно;
  * команды дополнительно проверяются белым списком routes/terminal.py;
  * инструменты файлов/терминала доступны вошедшим, задачи — всем.
"""
from flask import Blueprint, Response, request, jsonify, session, stream_with_context
from urllib.parse import urlparse
import json
import os
import re
import time

from ai_providers import chat_completion, chat_stream, resolve_target  # noqa: F401 (chat_stream — для тестов)
from tool_registry import bootstrap_default_tools, call_tool, tool_schemas
from routes.terminal import (run_agent as run_terminal, safe_path, relative_to_workspace,
                             ensure_workspace, nova_search, nova_read, _save_research,  # noqa: F401
                             can_use_workspace, signed_in, _format_results as _format_search_results)

agent_bp = Blueprint("agent", __name__)
_AGENT_HISTORY_CACHE = {}

MAX_STEPS = int(os.getenv("AGENT_MAX_STEPS", "20"))
AGENT_TIMEOUT = float(os.getenv("AGENT_TIMEOUT", "180"))
STEP_TIMEOUT = float(os.getenv("AGENT_STEP_TIMEOUT", "12"))
MAX_OBSERVATION = 2500
MAX_NOTE = 1800          # сколько текста каждой находки идёт в финальный запрос
MAX_SOURCES = 12

AGENT_SYSTEM_PROMPT = """Ты — агент NovaMind внутри «маленького Linux»: рабочая папка с файлами и терминалом.

Твои инструменты:
  {"action":"run","command":"ls -la"}                      выполнить команду в рабочей папке
  {"action":"read","path":"hello.py"}                       прочитать файл
  {"action":"write","path":"notes/plan.md","content":"…"}  записать файл
  {"action":"ls"}                                          список файлов
  {"action":"search","query":"что найти"}                   ПОИСК В ИНТЕРНЕТЕ
  {"action":"open","url":"https://…"}                       прочитать страницу
  {"action":"mcp_call","server_id":"github","tool":"...","arguments":{...}}  вызвать MCP-инструмент
  {"action":"image|audio|video","prompt":"…"}              сгенерировать медиа
  {"action":"skill_create","name":"slug","content":"полный SKILL.md"} создать skill
  {"action":"task","title":"…","detail":"…"}                завести задачу с подпунктами в detail
  {"action":"task_done","note":"что сделано"}               задача выполнена
  {"action":"answer","text":"ответ пользователю"}            закончить и ответить

Правила:
1. Ты ДОЛЖЕН отвечать ровно одним JSON-объектом и ничего больше — без слов вокруг.
2. Сначала разберись в задаче (run/read/ls), потом действуй, потом answer.
3. Один инструмент за один шаг. Не выдумывай результат команды — сначала выполни её.
4. Команды выполняются ТОЛЬКО по одной, без | и >.
5. Нужны свежие данные или факты — сначала проверь доступные MCP-инструменты. Для поиска
   предпочитай инструмент Exa внутри MCP, если он есть; web search используй как запасной вариант.
6. Если пользователь просит создать/сгенерировать/нарисовать изображение, видео или аудио,
   сразу используй action=image, action=video или action=audio, а не текстовый ответ.
7. Повторяй действие только если результат пустой или содержит ошибку; успешное действие не повторяй.
8. MCP-инструменты можно вызывать только из AVAILABLE TOOL REGISTRY. Для Notion/GitHub/email
   сначала используй mcp_call с точным server_id и tool, затем проверь результат.
9. Используй SKILL CONTEXT, если запрос соответствует сохранённому skill. Для создания skill
   сформируй полный SKILL.md с frontmatter name и description и используй skill_create.
10. В answer дай ПРЯМОЙ ответ на вопрос пользователя по-русски: сначала суть (названия, цифры, годы),
   потом 1–3 предложения пояснения. Не пересказывай свои шаги и не вставляй JSON.
   Ссылки на источники писать не нужно — интерфейс покажет их сам."""


def is_enabled():
    return os.getenv("AGENT_ENABLED", os.getenv("TERMINAL_ENABLED", "0")) == "1"


def strict_mcp_request(text: str) -> bool:
    """Определяет запросы, где пользователь явно запретил обычный web-search."""
    value = str(text or "").lower()
    explicit = re.search(r"не\s+используй\s+(?:web[_ -]?search|поиск)|только\s+(?:mcp|github|composio|switchboard|youtube)", value)
    named_service = any(name in value for name in ("github mcp", "composio", "switchboard", "mcp-инструмент", "mcp инструмент"))
    return bool(explicit or named_service)


def requested_media_kind(text: str):
    """Возвращает media action только для явного запроса генерации."""
    value = str(text or "").lower()
    if not re.search(r"(сгенерируй|создай|нарисуй|сделай|generate|create|draw)", value):
        return None
    if re.search(r"(изображени|картинк|фото|иллюстрац|image|picture|photo)", value):
        return "image"
    if re.search(r"(видео|ролик|video|clip)", value):
        return "video"
    if re.search(r"(аудио|озвуч|голос|музык|audio|voice|music)", value):
        return "audio"
    return None


def requested_skill_creation(text: str) -> bool:
    value = str(text or "").lower()
    return bool(re.search(r"(создай|создать|сделай|напиши|добавь)\s+(?:мне\s+)?(?:skill|скилл|навык)", value))


def is_simple_chat_request(text: str) -> bool:
    """Определяет короткие вопросы, которым не нужны внешние инструменты."""
    value = str(text or "").strip().lower()
    if not value or len(value) > 240 or requested_media_kind(value):
        return False
    tool_words = (
        "найди", "поищи", "поиск", "проверь", "актуальн", "сегодня", "сейчас",
        "последн", "новост", "ссылк", "открой", "прочитай", "репозитор",
        "github", "youtube", "composio", "mcp", "switchboard", "exa",
        "термукс", "termux", "linux", "команд", "файл", "код", "запусти",
        "папк", "folder", "посчитай", "выполни", "запусти",
        "сгенерируй", "создай", "нарисуй", "отправь", "удали", "сохрани",
        "инструмент", "email", "почт", "notion", "drive",
    )
    return not any(word in value for word in tool_words)


def tools_enabled():
    """Инструменты терминала и файлов — любому вошедшему при включённом окружении.

    Флаг читаем в момент вызова, а не на импорте: .env может подгрузиться позже.
    """
    return os.getenv("TERMINAL_ENABLED", "0") == "1" and can_use_workspace()


# ───────────────────────── разбор ответов модели ─────────────────────────

_THINK_BLOCK = re.compile(r"<think(?:ing)?>.*?</think(?:ing)?>", re.S | re.I)
_THINK_TAIL = re.compile(r"^.*?</think(?:ing)?>", re.S | re.I)
_THINK_OPEN = re.compile(r"</?think(?:ing)?>", re.I)


def strip_think(text):
    """Убирает «рассуждения» из ответа (nemotron, deepseek и др. кладут их прямо в текст)."""
    text = _THINK_BLOCK.sub("", text or "")
    text = _THINK_TAIL.sub("", text)          # закрывающий тег без открывающего
    return _THINK_OPEN.sub("", text).strip()


def _extract_json_object(text):
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    quoted = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if quoted:
            if escaped: escaped = False
            elif ch == "\\": escaped = True
            elif ch == '"': quoted = False
            continue
        if ch == '"': quoted = True
        elif ch == "{": depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0: return text[start:i + 1]
    return None

def parse_action(raw):
    text = strip_think(raw)
    if not text: return None
    candidates = []
    fence = re.search(r"\`\`\`(?:json)?\s*(.*?)\s*\`\`\`", text, re.S | re.I)
    if fence: candidates.append(fence.group(1))
    obj = _extract_json_object(text)
    if obj: candidates.append(obj)
    candidates.append(text)
    for block in candidates:
        try:
            data = json.loads(block.strip(), strict=False)
            if isinstance(data, dict) and data.get("action"): return data
        except (TypeError, ValueError):
            pass
    match = re.search(r"(?im)^ACTION\s*:\s*(SEARCH|OPEN|SHELL|READ|WRITE|ANSWER)\s*(.*)$", text)
    if match:
        action, rest = match.group(1).lower(), match.group(2).strip()
        if action == "search": return {"action":"search","query":rest}
        if action == "open": return {"action":"open","url":rest}
        if action == "shell": return {"action":"run","command":rest}
        if action == "read": return {"action":"read","path":rest}
        if action == "write":
            try: return {"action":"write", **json.loads(rest)}
            except ValueError: return None
        return {"action":"answer","text":rest}
    return None


def looks_like_action(raw):
    """Похоже на JSON действия, но не разобралось (обрезан, кривые кавычки)."""
    text = strip_think(raw).lstrip()
    return text.startswith(("{", "```")) and '"action"' in text


def clip(text, limit=MAX_OBSERVATION):
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + "\n… обрезано"


def _ask(messages, **kwargs):
    """Запрашивает модель через поток; fallback на обычный completion."""
    try:
        chunks = []
        error_text = ""
        saw = False
        for kind, chunk in chat_stream(messages, **kwargs):
            saw = True
            if kind == "token":
                chunks.append(str(chunk or ""))
            elif kind == "error":
                error_text = str(chunk or "")
        text = strip_think("".join(chunks))
        if text:
            offline_fallback = (
                "встроенная **офлайн-модель" in text.lower()
                or "ассистент novamind. спросите что-нибудь" in text.lower()
                or "проверьте модель и настройки" in text.lower()
            )
            if not offline_fallback:
                return text, ""
    except Exception:
        pass
    text, meta = chat_completion(messages, **kwargs)
    text = strip_think(text)
    if text:
        return text, ""
    return "", str(meta) if isinstance(meta, str) and meta else "модель не ответила"

# ───────────────────────── источники и заметки ─────────────────────────

_RESULT_LINE = re.compile(r"^\s*\d+\.\s+(.+)\n\s+(https?://\S+)", re.M)


def _add_sources(state, items):
    """Копит уникальные источники (title, url, host) для сворачиваемого блока."""
    seen = {item["url"] for item in state["sources"]}
    for item in items or []:
        url = str(item.get("url") or "").strip()
        if not url.startswith(("http://", "https://")) or url in seen:
            continue
        seen.add(url)
        state["sources"].append({
            "title": str(item.get("title") or "")[:220],
            "url": url,
            "host": item.get("host") or urlparse(url).netloc,
        })
    del state["sources"][MAX_SOURCES:]


def _sources_from_text(text):
    """Источники из текстовой выдачи `nova search` (когда модель вызвала её командой run)."""
    return [{"title": title.strip(), "url": url} for title, url in _RESULT_LINE.findall(text or "")]


def _note(state, label, text):
    """Запоминает находку — из этих заметок собирается финальный ответ."""
    text = (text or "").strip()
    if text:
        state["notes"].append((label, clip(text, MAX_NOTE)))


def _is_research_command(command):
    parts = str(command or "").split()
    return len(parts) >= 2 and parts[0] == "nova" and parts[1] in ("search", "read")


# ───────────────────────── итоговый ответ ─────────────────────────

def _synthesize(goal, state, provider, model):
    """Финальный запрос: модель отвечает обычным текстом по собранным данным."""
    notes = "\n\n".join(f"[{label}]\n{text}" for label, text in state["notes"][-6:])
    prompt = (
        f"Вопрос пользователя: {goal}\n\n"
        f"Найденные данные:\n{notes or 'ничего не найдено'}\n\n"
        "Ответь пользователю по-русски прямо и по существу, опираясь на эти данные: сначала суть "
        "(названия, цифры, годы), затем 1–3 предложения пояснения. Если данные неполные или "
        "противоречивые — скажи об этом честно. Не пиши JSON, не пересказывай свои шаги, "
        "не показывай команды и не вставляй ссылки."
    )
    text, meta = chat_completion(
        [{"role": "system", "content": "Ты — NovaMind, ассистент. Отвечай точно и по делу."},
         {"role": "user", "content": prompt}],
        provider=provider, model=model, temperature=0.3, max_tokens=1200, timeout=60,
    )
    if text is None:
        text = ""
    action = parse_action(text)
    if action:                                   # модель всё равно ответила JSON-ом
        text = str(action.get("text") or "").strip() if action.get("action") == "answer" else ""
    return text


def _fallback_answer(state):
    """Модель молчит — отдаём хотя бы найденное, а не пустоту."""
    if state["notes"]:
        label, text = state["notes"][0]
        return ("Модель не смогла сформулировать ответ, но данные собраны. "
                f"Вот что нашлось ({label}):\n\n{clip(text, 1500)}\n\n"
                "Источники — в свёрнутом блоке ниже; попробуйте ещё раз или смените модель.")
    return "Не удалось получить ответ. Проверьте модель и настройки."


@agent_bp.route("/api/agent/status")
def agent_status():
    enabled = is_enabled()
    try:
        import mcp_registry
        mcp_tool_count = len(mcp_registry.connected_tools())
    except Exception:
        mcp_tool_count = 0
    mcp_available = mcp_tool_count > 0
    return jsonify({
        "enabled": enabled,
        "tools": tools_enabled(),
        "mcp": mcp_available,
        "mcp_tools": mcp_tool_count,
        "available": bool(is_enabled() and (tools_enabled() or mcp_available)),
        "max_steps": MAX_STEPS,
        "user": signed_in(),
        "admin": bool(session.get("admin_logged_in")),
        "hint": ("" if enabled
                 else "Включите AGENT_ENABLED=1 и при необходимости TERMINAL_ENABLED=1 в .env и перезапустите сервер"),
        "media": {kind: __import__("media_agent").quick_available(kind)
                  for kind in ("image", "audio", "video")},
    })


@agent_bp.route("/api/agent/stream", methods=["POST"])
def agent_stream():
    """Агентный цикл: план → инструменты → ответ. NDJSON."""
    data = request.get_json(silent=True) or {}
    goal = (data.get("message") or "").strip()
    if not goal:
        return jsonify({"error": "Пустое сообщение"}), 400
    if not is_enabled():
        return jsonify({"error": "Агентный режим выключен (AGENT_ENABLED=1)"}), 403
    if not signed_in():
        return jsonify({"error": "Войдите в аккаунт, чтобы включить агентный режим"}), 403

    provider, model, _ = resolve_target()
    use_tools = tools_enabled()
    strict_mcp = strict_mcp_request(goal)
    try:
        import mcp_registry
        mcp_available = bool(mcp_registry.connected_tools())
    except Exception:
        mcp_available = False
    history_key = (
        str(session.get("user_email") or session.get("email") or session.get("username") or "")
        or request.cookies.get("session")
        or "anonymous"
    )
    prior = _AGENT_HISTORY_CACHE.get(history_key, session.get("agent_history", []))
    if not isinstance(prior, list):
        prior = []
    if use_tools:
        bootstrap_default_tools()
        ensure_workspace()
    available_schemas = tool_schemas() if (use_tools or mcp_available) else []
    if strict_mcp:
        # В этом запросе модель физически не получает web/shell/file schemas.
        available_schemas = [item for item in available_schemas if item.get("name") == "mcp_call"]

    @stream_with_context
    def generate():
        started = time.time()
        forced_media_kind = requested_media_kind(goal)
        state = {"task_id": None, "steps": 0, "answer": "", "sources": [], "notes": [],
                 "research": 0, "searched": bool(data.get("search")), "tool_steps": 0,
                 "media_context": [], "media": []}

        # 1. Один первый запрос: простой вопрос получает обычный ответ без лишнего plan/stage.
        # Если модель вернула action=plan, показываем его как план и продолжаем цикл.
        system_content = AGENT_SYSTEM_PROMPT
        if strict_mcp:
            system_content += (
                "\n\nSTRICT MCP MODE: Пользователь запретил web_search. "
                "Не вызывай search, open, run, shell или другие локальные инструменты. "
                "Используй только action=mcp_call с доступным server_id/tool. "
                "Если результат одного MCP-инструмента не содержит запрошенного поля, "
                "сделай дополнительный подходящий MCP-вызов, а не подставляй значение. "
                "Если mcp_call недоступен, ответь action=answer с MCP_UNAVAILABLE и причиной."
            )
        if available_schemas:
            system_content += "\n\nAVAILABLE TOOL REGISTRY:\n" + json.dumps(available_schemas, ensure_ascii=False)
        try:
            import skills_registry
            skill_context = skills_registry.prompt_context()
            if skill_context:
                system_content += "\n\nSKILL CONTEXT (используй подходящие инструкции):\n" + skill_context
        except Exception:
            pass
        if data.get("search") and not strict_mcp:
            system_content += (
                "\n\nSEARCH HINT: web search is allowed, but do not run it automatically. "
                "First inspect the request and available MCP tools; use search only when it is actually needed."
            )
        if available_schemas and not strict_mcp:
            system_content += (
                "\n\nMCP SEARCH PREFERENCE: if AVAILABLE TOOL REGISTRY contains an Exa search tool, "
                "use mcp_call to that Exa tool for web research before the built-in web_search."
            )
        if requested_skill_creation(goal):
            system_content += (
                "\n\nSKILL CREATION MODE: пользователь просит создать skill. Не отвечай только описанием. "
                "Сначала подготовь валидный полный SKILL.md и вызови action=skill_create с name и content, "
                "затем сообщи о сохранении."
            )
        history = [{"role": "system", "content": system_content}]
        for item in prior[-8:]:
            if isinstance(item, dict) and item.get("role") in ("user", "assistant"):
                history.append({"role": item["role"], "content": str(item.get("content") or "")[:4000]})
        history.append({"role": "user", "content": goal})

        if is_simple_chat_request(goal) and not strict_mcp:
            # Короткие разговорные/объяснительные вопросы не должны ждать
            # discovery MCP, web-search и планирования из нескольких шагов.
            fast_history = [{
                "role": "system",
                "content": "Ты NovaMind. Ответь прямо и кратко по-русски. "
                           "Не вызывай инструменты, не пиши JSON и не описывай внутренние шаги.",
            }]
            fast_history.extend(history[-5:])
            fast_answer, fast_error = _ask(
                fast_history, provider=provider, model=model,
                temperature=0.3, max_tokens=700, timeout=30)
            if fast_error:
                fast_answer = "Не удалось получить быстрый ответ: " + str(fast_error)
            parsed_fast = parse_action(fast_answer)
            if parsed_fast and parsed_fast.get("action") == "answer":
                fast_answer = str(parsed_fast.get("text") or "").strip()
            state["answer"] = fast_answer.strip()
            updated_fast_history = (prior + [{"role": "user", "content": goal},
                                             {"role": "assistant", "content": state["answer"]}])[-10:]
            _AGENT_HISTORY_CACHE[history_key] = updated_fast_history
            session["agent_history"] = updated_fast_history
            for chunk in _chunks(state["answer"]):
                yield _ndjson({"type": "token", "token": chunk})
            yield _ndjson({"type": "result", "reply": state["answer"], "steps": 0,
                           "task_id": None, "model": model, "sources": [],
                           "searched": False, "media": [], "linux": False,
                           "offline": provider == "local_demo", "fast": True})
            yield _ndjson({"type": "done"})
            return

        # Раньше флаг search автоматически запускал DuckDuckGo до первого
        # решения модели. Теперь он только передаёт агенту подсказку: агент
        # сам выбирает MCP, web_search, web_open или локальный инструмент.

        # 2. Цикл действий
        seen, duplicates, parse_fails = set(), 0, 0
        outcomes, retries = {}, {}
        limit_reached = False
        for step in range(1, MAX_STEPS + 4):
            if time.time() - started > AGENT_TIMEOUT:
                yield _ndjson({"type": "step", "icon": "⏱", "text": "Время вышло — собираю ответ"})
                break
            ask_kwargs = {"provider": provider, "model": model,
                          "temperature": 0.1, "max_tokens": 1200, "timeout": 60}
            if strict_mcp:
                ask_kwargs["system"] = (
                    "STRICT MCP MODE. Не используй web_search, web_open, shell, run, read или write. "
                    "Выполни только mcp_call; если он недоступен, верни action=answer с MCP_UNAVAILABLE."
                )
            elif not use_tools:
                ask_kwargs["system"] = "Linux-окружение сейчас выключено (TERMINAL_ENABLED=0). Не вызывай инструменты workspace; медиа-действия доступны."
            if data.get("search") and not strict_mcp:
                ask_kwargs["system"] = "ПОИСК ВКЛЮЧЁН. Используй найденные данные и указывай источники [1], [2], [3]."
            raw, error = _ask(history, **ask_kwargs)
            if error:
                yield _ndjson({"type": "error", "text": error})
                break                                   # ответ соберём из найденного

            action = parse_action(raw)
            extracted = _extract_json_object(strip_think(raw)) if raw else None
            if action and extracted and strip_think(raw).strip() != extracted.strip():
                yield _ndjson({"type": "retract", "count": len(strip_think(raw))})
            if not action and raw and '"action"' in raw and _extract_json_object(raw):
                action = parse_action(_extract_json_object(raw))
                if action:
                    yield _ndjson({"type": "retract", "count": len(raw)})
            if forced_media_kind and (not action or str(action.get("action")) == "answer"):
                # Не даём модели превратить явный запрос генерации в текстовый ответ.
                action = {"action": forced_media_kind, "prompt": goal}
            if not action and re.match(r"^План\s*:", strip_think(raw), re.I):
                plan_text = strip_think(raw).split(":", 1)[1].strip()
                yield _ndjson({"type": "stage", "scene": "agent", "title": "Планирую",
                               "text": "Разбираю задачу по шагам…"})
                yield _ndjson({"type": "plan", "text": clip(plan_text, 900)})
                history.append({"role": "assistant", "content": raw[:2000]})
                history.append({"role": "user", "content": "План принят. Выполни задачу по плану и продолжай до готового ответа."})
                continue
            if not action:
                if looks_like_action(raw):
                    # JSON обрезался или сломан: один раз просим повторить, потом сдаёмся
                    parse_fails += 1
                    if parse_fails >= 2:
                        break
                    history.append({"role": "assistant", "content": raw[:500]})
                    history.append({"role": "user", "content":
                                    "Это не разобралось как JSON. Повтори одним корректным JSON-объектом."})
                    continue
                state["answer"] = raw.strip()           # обычный текст — это и есть ответ
                break

            kind = str(action.get("action"))
            if kind == "plan":
                state["steps"] += 1
                steps = action.get("steps") or action.get("plan") or action.get("text") or []
                if isinstance(steps, list):
                    plan_text = "\n".join(f"{i}. {str(item)}" for i, item in enumerate(steps, 1))
                else:
                    plan_text = str(steps)
                yield _ndjson({"type": "stage", "scene": "agent", "title": "Планирую",
                               "text": "Разбираю задачу по шагам…"})
                yield _ndjson({"type": "plan", "text": clip(plan_text, 900)})
                history.append({"role": "assistant", "content": json.dumps(action, ensure_ascii=False)[:2000]})
                history.append({"role": "user", "content": "План принят. Выполни первый пункт и продолжай работу."})
                continue
            if action.get("thought"):
                yield _ndjson({"type": "step", "icon": "🧠", "text": str(action.get("thought"))[:900]})
            if kind not in ("answer", "plan"):
                if state["tool_steps"] >= MAX_STEPS:
                    limit_reached = True
                    break
                state["tool_steps"] += 1
                state["steps"] += 1
                if state["tool_steps"] >= MAX_STEPS:
                    limit_reached = True
            key = _action_key(action, kind)
            previous = outcomes.get(key)
            can_retry = bool(previous is not None and _retryable_observation(previous))
            if key in seen and kind not in ("answer", "task", "task_done") and not can_retry:
                duplicates += 1
                yield _ndjson({"type": "step", "icon": "↺", "text": "Повторное действие пропущено"})
                if duplicates >= 2:
                    break
                observation = ("Ты уже выполнял именно это действие — результат выше. Не повторяй его: "
                               "используй имеющиеся данные и дай action=answer.")
            elif can_retry and retries.get(key, 0) < 2:
                retries[key] = retries.get(key, 0) + 1
                yield _ndjson({"type": "step", "icon": "🔁",
                               "text": f"Повторяю неудачное действие ({retries[key]}/2)"})
                if strict_mcp and kind not in ("mcp_call", "answer", "plan"):
                    observation = "MCP_ONLY: действие запрещено. Используй только mcp_call."
                else:
                    observation = yield from _perform(action, kind, use_tools, state, goal=goal)
            else:
                seen.add(key)
                if strict_mcp and kind not in ("mcp_call", "answer", "plan"):
                    observation = (
                        "MCP_ONLY: действие запрещено. Пользователь разрешил только mcp_call. "
                        "Используй доступный server_id и tool из AVAILABLE TOOL REGISTRY."
                    )
                    yield _ndjson({"type": "tool", "name": kind, "command": "blocked", "code": 1,
                                   "output": observation})
                else:
                    observation = yield from _perform(action, kind, use_tools, state, goal=goal)

            outcomes[key] = observation

            if kind == "answer":
                state["answer"] = str(action.get("text") or "").strip()
                break

            history.append({"role": "assistant", "content": json.dumps(action, ensure_ascii=False)[:2000]})
            observation = clip(observation)
            verification = _verify_action(kind, observation)
            yield _ndjson({"type": "verify", "ok": verification["ok"], "tool": kind,
                           "text": verification["message"]})
            if not verification["ok"]:
                history.append({"role": "user", "content":
                                "VERIFY: действие не подтверждено. Не повторяй вслепую; "
                                "исправь параметры или выбери другой инструмент."})
            nudge = _nudge(state, MAX_STEPS - state["tool_steps"])
            history.append({"role": "user", "content":
                            f"Результат действия:\n{observation}" + (f"\n\n{nudge}" if nudge else "")})

        if limit_reached or (state["tool_steps"] >= MAX_STEPS and not state["answer"]):
            yield _ndjson({"type": "step", "icon": "⏹", "text": "Шаги закончились — готовлю итог"})

        # 3. Нет ответа (лимит шагов, таймаут, повторы, сбой) — собираем его по найденному
        if not state["answer"] and (use_tools or state["notes"]):
            yield _ndjson({"type": "step", "icon": "✍️", "text": "Готовлю ответ по найденному"})
            state["answer"] = _synthesize(goal, state, provider, model)
        if not state["answer"] and not use_tools:
            # инструменты нет — обычный ответ модели
            history.append({"role": "user", "content":
                            "Инструменты отключены: TERMINAL_ENABLED=0. Не вызывай run/read/write; "
                            "ответь пользователю обычным текстом."})
            text, _err = _ask(history, provider=provider, model=model,
                              temperature=0.3, max_tokens=1200, timeout=60)
            action = parse_action(text)
            state["answer"] = (str(action.get("text") or "").strip() if action else text)
        if not state["answer"]:
            state["answer"] = _fallback_answer(state)

        # Источники — отдельным событием; интерфейс держит их свёрнутыми
        if state["sources"]:
            yield _ndjson({"type": "sources", "sources": state["sources"]})

        # 4. Финальный ответ печатаем по кускам — интерфейс любит поток
        for chunk in _chunks(state["answer"]):
            yield _ndjson({"type": "token", "token": chunk})

        if state["task_id"]:
            import database
            task = database.get_task(state["task_id"])
            if task and task["status"] != "done":
                database.update_task(state["task_id"], status="done")
                database.append_task_step(state["task_id"], "Агент закончил", status="done",
                                          log=clip(state["answer"], 1500))
                yield _ndjson({"type": "task", "task": database.get_task(state["task_id"])})

        historical_answer = state["answer"]
        if state["media_context"]:
            historical_answer += "\n" + "\n".join(state["media_context"])
        updated_history = (prior + [{"role": "user", "content": goal},
                                       {"role": "assistant", "content": historical_answer}])[-10:]
        _AGENT_HISTORY_CACHE[history_key] = updated_history
        session["agent_history"] = updated_history
        yield _ndjson({"type": "result", "reply": state["answer"], "steps": state["steps"],
                       "task_id": state["task_id"], "model": model,
                       "sources": state["sources"], "searched": state["searched"],
                       "media": state["media"], "linux": bool(use_tools),
                       "offline": provider == "local_demo"})
        yield _ndjson({"type": "done"})

    return Response(generate(), mimetype="application/x-ndjson",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _ndjson(payload):
    return json.dumps(payload, ensure_ascii=False) + "\n"


def _chunks(text, size=48):
    text = text or ""
    for index in range(0, len(text), size):
        yield text[index:index + size]


def _action_key(action, kind):
    """Ключ действия для поиска повторов."""
    value = (action.get("command") or action.get("query") or action.get("url")
             or action.get("path") or "")
    if kind == "mcp_call":
        value = f"{action.get('server_id', '')}:{action.get('tool', '')}:{json.dumps(action.get('arguments') or {}, sort_keys=True, ensure_ascii=False)}"
    if kind == "write":
        value = f"{value}|{str(action.get('content') or '')[:200]}"
    return (kind, " ".join(str(value).lower().split()))


def _retryable_observation(observation):
    """Пустой/ошибочный результат можно повторить ограниченное число раз."""
    text = str(observation or "").strip().lower()
    if not text:
        return True
    markers = (
        "ошибка", "error", "не выполнен", "не выполнено", "недоступ",
        "таймаут", "timeout", "пустой результат", "ничего не найден",
        "mcp_unavailable", "http 4", "http 5", "код -1",
    )
    return any(marker in text for marker in markers)


def _find_exa_search_tool():
    """Находит Exa search tool среди инструментов единого MCP-шлюза."""
    try:
        import mcp_registry
        tools = mcp_registry.connected_tools()
    except Exception:
        return None
    candidates = [item for item in tools
                  if "exa" in str(item.get("name", "")).lower()
                  and any(word in str(item.get("name", "")).lower()
                          for word in ("search", "find", "research"))]
    return candidates[0] if candidates else None


def _exa_arguments(tool, query, limit=3):
    schema = tool.get("inputSchema") or {}
    props = schema.get("properties") or {}
    args = {}
    query_key = next((key for key in ("query", "searchQuery", "q") if key in props), "query")
    args[query_key] = query
    limit_key = next((key for key in ("numResults", "limit", "maxResults") if key in props), None)
    if limit_key:
        args[limit_key] = limit
    return args


def _verify_action(kind, observation):
    text = str(observation or "").strip()
    if kind in ("search", "open", "read", "run", "write", "ls", "mcp_call") and not text:
        return {"ok": False, "message": "Проверка не пройдена: инструмент вернул пустой результат."}
    if kind == "run" and "код -1" in text:
        return {"ok": False, "message": "Проверка не пройдена: команда не выполнилась."}
    if kind == "search" and "ничего не вернул" in text.lower():
        return {"ok": False, "message": "Поиск не подтвердил наличие результатов."}
    return {"ok": True, "message": "Результат инструмента получен и принят."}


def _nudge(state, remaining):
    """Подсказка модели: пора отвечать, а не искать дальше."""
    if remaining <= 0:
        return "Шагов не осталось. Дай финальный ответ прямо сейчас: {\"action\":\"answer\",\"text\":\"…\"}."
    if remaining == 1:
        return "Остался последний шаг. Больше не ищи — ответь action=answer по собранным данным."
    if state["research"] >= 3:
        return ("Данных из интернета уже достаточно. Если можешь ответить — сразу action=answer; "
                "новый поиск — только если чего-то принципиально не хватает.")
    return ""


def _perform(action, kind, use_tools, state, goal=""):
    """Одно действие агента.

    Генератор: отдаёт события потока (шаги, вывод команд, задачи) и
    возвращает наблюдение, которое уходит обратно в модель.
    """
    import database

    if kind == "skill_create":
        import skills_registry
        try:
            item = skills_registry.save_text(action.get("name"), action.get("content") or "")
            yield _ndjson({"type": "skill", "skill": item})
            return f"Skill «{item['name']}» сохранён и доступен в настройках и следующих запросах."
        except ValueError as exc:
            return f"Skill не сохранён: {exc}"

    if kind == "mcp_call":
        import mcp_registry
        server_id = str(action.get("server_id") or "").strip()
        tool_name = str(action.get("tool") or "").strip()
        arguments = action.get("arguments") or {}
        if not server_id or not tool_name or not isinstance(arguments, dict):
            return "MCP: нужны server_id, tool и объект arguments."
        try:
            result = mcp_registry.call_tool(server_id, tool_name, arguments)
            text = json.dumps(result, ensure_ascii=False, indent=2)
            state["notes"].append(f"MCP {server_id}:{tool_name}\n{text[:MAX_NOTE]}")
            state["research"] += 1
            yield _ndjson({"type": "tool", "name": "mcp_call",
                           "command": f"{server_id}:{tool_name}", "code": 0,
                           "output": clip(text, 1500)})
            return text
        except Exception as exc:
            message = f"MCP {server_id}:{tool_name} не выполнен: {exc}"
            yield _ndjson({"type": "tool", "name": "mcp_call",
                           "command": f"{server_id}:{tool_name}", "code": 1,
                           "output": message})
            return message

    if kind == "media_models":
        import media_agent
        media_type = str(action.get("type") or "all")
        text = media_agent.describe(media_type)
        yield _ndjson({"type": "step", "icon": "🎨", "text": "Проверил доступные media-модели"})
        return text

    if kind in ("image", "audio", "video"):
        import media_agent
        prompt = str(action.get("prompt") or action.get("text") or "").strip()
        options = media_agent.build_options(kind, action)
        errors = []
        yield _ndjson({"type": "stage", "scene": "media", "title": "Рисую медиа",
                       "text": f"Подбираю модель для типа: {kind}"})
        def status(message):
            yield_value = _ndjson({"type": "step", "icon": "🎨", "text": str(message)[:500]})
            # A generator callback cannot yield into the outer generator;
            # retain the final error/result while generation remains synchronous.
            errors.append(yield_value)
        result, model, generation_errors = media_agent.generate(kind, prompt, options=options,
                                                                  wanted_model=str(action.get("model") or ""))
        errors.extend(generation_errors or [])
        if result:
            state["media_context"].append(f"Создано {kind}: {prompt}")
            state["media"].append(result)
            yield _ndjson({"type": "media", "media": result, "model": model})
            if isinstance(result, dict) and result.get("state") == "queued":
                return f"Медиа поставлено в очередь: {prompt}"
            return f"Медиа создано: {prompt}"
        yield _ndjson({"type": "step", "icon": "⚠️", "text": "Не получилось создать медиа"})
        return "Не получилось создать медиа: " + "; ".join(str(item) for item in errors[-3:])

    if kind == "task":
        task = database.create_task(
            title=(action.get("title") or goal or "Задача агента")[:200],
            detail=(action.get("detail") or goal or "")[:2000],
            source="agent",
        )
        state["task_id"] = task["id"]
        database.append_task_step(task["id"], "Задача заведена", status="doing", log=goal[:1000])
        yield _ndjson({"type": "task", "task": task})
        yield _ndjson({"type": "step", "icon": "📋", "text": f"Задача: {task['title']}"})
        return f"Задача создана, id={task['id']}. Дальше выполняй её по шагам."

    if kind == "task_done":
        if state["task_id"]:
            database.update_task(state["task_id"], status="done")
            database.append_task_step(state["task_id"], action.get("note") or "Готово",
                                      status="done", log=str(action.get("note") or "")[:1000])
            yield _ndjson({"type": "task", "task": database.get_task(state["task_id"])})
        return "Задача закрыта. Теперь ответь пользователю."

    if kind == "answer":
        return ""  # цикл прервётся сам

    if not use_tools:
        return "Инструменты недоступны: TERMINAL_ENABLED=0. Ответь пользователю по имеющимся данным."

    if kind == "search":
        state["searched"] = True
        query = str(action.get("query") or "").strip()
        if not query:
            return 'Пустой поисковый запрос. Повтори с {"action":"search","query":"..."}.'
        exa_tool = _find_exa_search_tool()
        if exa_tool:
            import mcp_registry
            server_id = str(exa_tool.get("server_id") or "")
            tool_name = str(exa_tool.get("name") or "")
            try:
                result = mcp_registry.call_tool(
                    server_id, tool_name, _exa_arguments(exa_tool, query, limit=3))
                text = json.dumps(result, ensure_ascii=False, indent=2)
                state["research"] += 1
                state["notes"].append(f"MCP Exa {tool_name}\n{text[:MAX_NOTE]}")
                yield _ndjson({"type": "tool", "name": "mcp_call",
                               "command": f"{server_id}:{tool_name}", "code": 0,
                               "output": clip(text, 1800)})
                if result and not (isinstance(result, dict) and result.get("error")):
                    return text
                return "Exa MCP вернул пустой или ошибочный результат."
            except Exception as exc:
                yield _ndjson({"type": "tool", "name": "mcp_call",
                               "command": f"{server_id}:{tool_name}", "code": 1,
                               "output": f"Exa MCP error: {exc}"})
                # Ниже будет обычный поиск только как fallback после сбоя Exa.
        state["research"] += 1
        # Use the same function as the terminal/UI search. This keeps one
        # internet-search implementation (httpx + BeautifulSoup4) and makes source
        # collection deterministic for agent actions.
        results, search_meta = nova_search(query, limit=3)
        results = results or []
        trace = search_meta.get("trace") or []
        text = "\n".join(
            f"{i}. {item.get('title','')}\n   {item.get('url','')}\n   {item.get('snippet','')}"
            for i, item in enumerate(results, 1)
        ) or "Поиск ничего не вернул."
        backend = next((x.get("backend") for x in trace if x.get("ok")), "registry")
        elapsed = next((x.get("ms") for x in trace if x.get("ok")), 0)
        if not results:
            yield _ndjson({"type": "step", "icon": "⚠️", "text": f"Поиск «{query}» ничего не дал"})
        else:
            _add_sources(state, results)
            _note(state, f"поиск «{query}»", text)
            yield _ndjson({"type": "step", "icon": "🔎",
                           "text": f"Нашёл {len(results)} по запросу «{query}» ({backend}, {elapsed} мс)"})
            yield _ndjson({"type": "tool", "name": "search", "command": f"nova search {query}",
                           "code": 0, "output": "\n".join(
                               f"[{i}] {item.get('title','')}\n   {item.get('url','')}\n   {item.get('snippet','')}"
                               for i, item in enumerate(results, 1)
                           ), "results": results})
        if state["task_id"]:
            import database as _db
            _db.append_task_step(state["task_id"], f"Поиск: {query}", status="done",
                                 log=text[:1200])
        return text

    if kind == "open":
        url = str(action.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            return "Нужен полный адрес страницы, например https://example.com"
        state["research"] += 1
        started_open = time.time()
        open_result = call_tool("web_open", url=url, max_chars=4000)
        elapsed = int((time.time() - started_open) * 1000)
        text = open_result.get("content") or ""
        if text and not text.startswith("Текст страницы"):
            text = "Текст страницы:\n" + text
        if text:
            _add_sources(state, [{"title": urlparse(url).netloc, "url": url}])
            _note(state, f"страница {url}", text)
        if state["task_id"]:
            import database as _db
            _db.append_task_step(state["task_id"], f"Прочитал {url}", status="done",
                                 log=(text or "")[:1200])
        yield _ndjson({"type": "step", "icon": "📰", "text": f"Прочитал {url} ({elapsed} мс)"})
        yield _ndjson({"type": "tool", "name": "open", "command": url, "code": 0 if text else 1,
                       "output": (text or "страница пустая")[:1500]})
        return text or "Страница не прочиталась."

    if kind == "ls":
        command, label, icon = "ls -la", "Смотрю файлы", "📂"
    elif kind == "read":
        rel_path = str(action.get("path", ""))
        read_result = call_tool("file_read", path=rel_path, max_chars=MAX_OBSERVATION)
        if not read_result.get("ok"):
            return str((read_result.get("error") or {}).get("message") or "Не удалось прочитать файл")
        try:
            path = safe_path(rel_path)
        except ValueError as exc:
            return str(exc)
        content = str(read_result.get("content") or "")
        yield _ndjson({"type": "step", "icon": "📖", "text": f"Читаю {relative_to_workspace(path)}"})
        yield _ndjson({"type": "tool", "name": "read", "command": relative_to_workspace(path),
                       "code": 0, "output": clip(content, 600)})
        return content

    elif kind == "write":
        rel_path = str(action.get("path", "notes/out.md"))
        content = str(action.get("content") or "")
        write_result = call_tool("file_write", path=rel_path, content=content)
        if not write_result.get("ok"):
            return str((write_result.get("error") or {}).get("message") or "Не удалось записать файл")
        try:
            path = safe_path(rel_path)
        except ValueError as exc:
            return str(exc)
        if state["task_id"]:
            database.append_task_step(state["task_id"], f"Записал {relative_to_workspace(path)}",
                                      status="doing", log=clip(content, 800))
        yield _ndjson({"type": "step", "icon": "✍️", "text": f"Записал {relative_to_workspace(path)}"})
        yield _ndjson({"type": "tool", "name": "write", "command": relative_to_workspace(path),
                       "code": 0, "output": f"Файл {relative_to_workspace(path)} записан."})
        return f"Файл {relative_to_workspace(path)} записан. Открой его командой cat {relative_to_workspace(path)}."

    elif kind == "run":
        command = str(action.get("command") or "").strip()
        label, icon = f"$ {command}", "⚙️"
    else:
        return f"Неизвестное действие: {kind}. Ответь пользователю."

    result = call_tool("shell", command=command, timeout=STEP_TIMEOUT)
    raw_code = result.get("exit_code", result.get("code"))
    result["code"] = int(-1 if raw_code is None else raw_code)
    stdout = str(result.get("stdout") or "")
    stderr = str(result.get("stderr") or "")
    output = stdout + (("\n" + stderr) if stderr else "")
    if _is_research_command(command) and not result["code"]:
        # Keep source metadata even when terminal output is abbreviated.
        state["research"] += 1
        parsed_sources = _sources_from_text(output)
        if parsed_sources:
            _add_sources(state, parsed_sources)
        else:
            try:
                query = str(command).split(None, 2)[2]
                fresh_results, _meta = nova_search(query, limit=6)
                _add_sources(state, fresh_results)
            except Exception:
                pass
        _note(state, command, output)
    if state["task_id"]:
        database.append_task_step(
            state["task_id"], label, status="error" if result["code"] else "done",
            log=clip(output, 1200) or f"код {result['code']}",
        )
    status = "❌" if result["code"] else "✅"
    yield _ndjson({"type": "step", "icon": status, "text": f"{label} — код {result['code']}"})
    yield _ndjson({"type": "tool", "name": "run", "command": command, "code": result["code"],
                   "output": clip(output, 1500)})
    if result["code"]:
        return (f"код выхода {result['code']}.\n" + output) if output else (
            f"Команда завершилась с кодом выхода {result['code']} и пустым выводом."
        )
    return output or "Команда завершилась успешно, но ничего не вывела."
