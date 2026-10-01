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

MAX_STEPS = int(os.getenv("AGENT_MAX_STEPS", "8"))
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
  {"action":"task","title":"…","detail":"…"}                завести задачу с подпунктами в detail
  {"action":"task_done","note":"что сделано"}               задача выполнена
  {"action":"answer","text":"ответ пользователю"}            закончить и ответить

Правила:
1. Ты ДОЛЖЕН отвечать ровно одним JSON-объектом и ничего больше — без слов вокруг.
2. Сначала разберись в задаче (run/read/ls), потом действуй, потом answer.
3. Один инструмент за один шаг. Не выдумывай результат команды — сначала выполни её.
4. Команды выполняются ТОЛЬКО по одной, без | и >.
5. Нужны свежие данные или факты — сделай search (обычно хватает 1–3 поисков), потом опирайся на найденное.
6. НЕ повторяй одно и то же действие: результат уже перед тобой. Как только данных хватает —
   сразу action=answer. Шагов у тебя мало, лишние поиски и чтение страниц тратят их впустую.
7. В answer дай ПРЯМОЙ ответ на вопрос пользователя по-русски: сначала суть (названия, цифры, годы),
   потом 1–3 предложения пояснения. Не пересказывай свои шаги и не вставляй JSON.
   Ссылки на источники писать не нужно — интерфейс покажет их сам."""


def is_enabled():
    return os.getenv("AGENT_ENABLED", os.getenv("TERMINAL_ENABLED", "0")) == "1"


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
    text, _error = _ask(
        [{"role": "system", "content": "Ты — NovaMind, ассистент. Отвечай точно и по делу."},
         {"role": "user", "content": prompt}],
        provider=provider, model=model, temperature=0.3, max_tokens=1200, timeout=60,
    )
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
    return jsonify({
        "enabled": enabled,
        "tools": tools_enabled(),
        "available": bool(is_enabled() and tools_enabled()),
        "max_steps": MAX_STEPS,
        "user": signed_in(),
        "admin": bool(session.get("admin_logged_in")),
        "hint": ("" if enabled
                 else "Включите AGENT_ENABLED=1 и TERMINAL_ENABLED=1 в .env и перезапустите сервер"),
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
    prior = session.get("agent_history", [])
    if not isinstance(prior, list):
        prior = []
    if use_tools:
        bootstrap_default_tools()
        ensure_workspace()

    @stream_with_context
    def generate():
        started = time.time()
        state = {"task_id": None, "steps": 0, "answer": "", "sources": [], "notes": [],
                 "research": 0, "searched": bool(data.get("search"))}

        # 1. Один первый запрос: простой вопрос получает обычный ответ без лишнего plan/stage.
        # Если модель вернула action=plan, показываем его как план и продолжаем цикл.
        history = [{"role": "system", "content": AGENT_SYSTEM_PROMPT + "\n\nAVAILABLE TOOL REGISTRY:\n" + json.dumps(tool_schemas(), ensure_ascii=False) if use_tools else AGENT_SYSTEM_PROMPT}]
        for item in prior[-8:]:
            if isinstance(item, dict) and item.get("role") in ("user", "assistant"):
                history.append({"role": item["role"], "content": str(item.get("content") or "")[:4000]})
        history.append({"role": "user", "content": f"Задача: {goal}"})

        if data.get("search") and use_tools:
            yield _ndjson({"type": "stage", "scene": "search", "title": "Поиск в интернете",
                           "text": "Ищу свежие данные и проверяю страницы…"})
            pre = call_tool("web_search", query=goal, limit=3)
            pre_results = pre.get("results") or []
            pre_text = "\n".join(f"{i}. {x.get('title','')}\n   {x.get('url','')}\n   {x.get('snippet','')}"
                                  for i, x in enumerate(pre_results, 1))
            if pre_results:
                state["research"] += 1
                _add_sources(state, pre_results)
                _note(state, f"поиск «{goal}»", pre_text)
                yield _ndjson({"type": "tool", "name": "search", "command": f"nova search {goal}",
                               "code": 0, "output": pre_text, "results": pre_results})
                try:
                    body = f"# {goal}\n\n" + pre_text + "\n"
                    saved_path = _save_research(goal, body, "search")
                    yield _ndjson({"type": "step", "icon": "💾",
                                   "text": f"Сохранил результаты поиска: {saved_path}"})
                except Exception:
                    pass
                for item in pre_results:
                    opened = call_tool("web_open", url=item.get("url"), max_chars=3000)
                    page = opened.get("content") or ""
                    if page:
                        page = "Текст страницы:\n" + page if not page.startswith("Текст страницы") else page
                        _note(state, f"страница {item.get('url')}", page)
                        yield _ndjson({"type": "tool", "name": "open", "command": item.get("url"),
                                       "code": 0, "output": clip(page, 1500)})
            else:
                yield _ndjson({"type": "step", "icon": "⚠️", "text": "Поиск ничего не дал"})
            history.append({"role": "user", "content": "ПОИСК ВКЛЮЧЁН. Используй найденные данные. Если их достаточно — ответь; если нет — можешь выполнить ещё один search."})

        # 2. Цикл действий
        seen, duplicates, parse_fails = set(), 0, 0
        for step in range(1, MAX_STEPS + 1):
            if time.time() - started > AGENT_TIMEOUT:
                yield _ndjson({"type": "step", "icon": "⏱", "text": "Время вышло — собираю ответ"})
                break
            if not use_tools:
                break

            state["steps"] += 1
            ask_kwargs = {"provider": provider, "model": model,
                          "temperature": 0.1, "max_tokens": 1200, "timeout": 60}
            if data.get("search"):
                ask_kwargs["system"] = "ПОИСК ВКЛЮЧЁН. Используй найденные данные и указывай источники [1], [2], [3]."
            raw, error = _ask(history, **ask_kwargs)
            if error:
                yield _ndjson({"type": "error", "text": error})
                break                                   # ответ соберём из найденного

            action = parse_action(raw)
            if not action and raw and '"action"' in raw and _extract_json_object(raw):
                action = parse_action(_extract_json_object(raw))
                if action:
                    yield _ndjson({"type": "retract", "count": len(raw)})
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
            key = _action_key(action, kind)
            if key in seen and kind not in ("answer", "task", "task_done"):
                duplicates += 1
                yield _ndjson({"type": "step", "icon": "↺", "text": "Повторное действие пропущено"})
                if duplicates >= 2:
                    break
                observation = ("Ты уже выполнял именно это действие — результат выше. Не повторяй его: "
                               "используй имеющиеся данные и дай action=answer.")
            else:
                seen.add(key)
                observation = yield from _perform(action, kind, use_tools, state, goal=goal)

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
            nudge = _nudge(state, MAX_STEPS - step)
            history.append({"role": "user", "content":
                            f"Результат действия:\n{observation}" + (f"\n\n{nudge}" if nudge else "")})

        if state["steps"] >= MAX_STEPS and not state["answer"]:
            yield _ndjson({"type": "step", "icon": "⏹", "text": "Шаги закончились — готовлю итог"})

        # 3. Нет ответа (лимит шагов, таймаут, повторы, сбой) — собираем его по найденному
        if not state["answer"] and (use_tools or state["notes"]):
            yield _ndjson({"type": "step", "icon": "✍️", "text": "Готовлю ответ по найденному"})
            state["answer"] = _synthesize(goal, state, provider, model)
        if not state["answer"] and not use_tools:
            # инструментов нет — обычный ответ модели
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

        session["agent_history"] = (prior + [{"role": "user", "content": goal},
                                             {"role": "assistant", "content": state["answer"]}])[-10:]
        yield _ndjson({"type": "result", "reply": state["answer"], "steps": state["steps"],
                       "task_id": state["task_id"], "model": model,
                       "sources": state["sources"], "searched": state["searched"],
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
    if kind == "write":
        value = f"{value}|{str(action.get('content') or '')[:200]}"
    return (kind, " ".join(str(value).lower().split()))


def _verify_action(kind, observation):
    text = str(observation or "").strip()
    if kind in ("search", "open", "read", "run", "write", "ls") and not text:
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
        return "Инструменты недоступны. Ответь пользователю по имеющимся данным."

    if kind == "search":
        query = str(action.get("query") or "").strip()
        if not query:
            return 'Пустой поисковый запрос. Повтори с {"action":"search","query":"..."}.'
        state["research"] += 1
        tool_result = call_tool("web_search", query=query, limit=3)
        results = tool_result.get("results") or []
        trace = tool_result.get("trace") or []
        text = "\n".join(
            f"{i}. {item.get('title','')}\n   {item.get('url','')}\n   {item.get('snippet','')}"
            for i, item in enumerate(results, 1)
        ) or str(tool_result.get("error") or "Поиск ничего не вернул.")
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
                           "code": 0, "output": text, "results": results})
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
        open_result = call_tool("web_open", url=url, max_chars=4000)
        text = open_result.get("content") or ""
        if text and not text.startswith("Текст страницы"):
            text = "Текст страницы:\n" + text
        elapsed = 0
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
    result["code"] = int(result.get("exit_code", result.get("code", -1)) or -1)
    stdout = str(result.get("stdout") or "")
    stderr = str(result.get("stderr") or "")
    output = stdout + (("\n" + stderr) if stderr else "")
    if _is_research_command(command) and not result["code"]:
        # модель искала не через action=search, а командой `nova search …` — учитываем так же
        state["research"] += 1
        _add_sources(state, _sources_from_text(output))
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
        return (f"Код выхода {result['code']}.\\n" + output) if output else (
            f"Команда завершилась с кодом выхода {result['code']} и пустым выводом."
        )
    return output or "Команда завершилась успешно, но ничего не вывела."
