"""
routes/agent.py — главный чат с Linux-окружением.

Отдельного «агентного режима» больше нет: каждое сообщение в главном чате
идёт сюда, и модель сама решает, как отвечать.

    вопрос → [ответ сразу]                                  простые вопросы
    вопрос → план → [команда | файл | поиск | страница] × N → ответ   задачи

Как это устроено:
  * каждый шаг — потоковый запрос к модели. Если модель начинает писать
    обычный текст, он сразу печатается в чат (одним запросом, без задержек).
    Если модель отвечает JSON-объектом — это действие в Linux-окружении:
    сервер выполняет его, отдаёт результат модели, и цикл повторяется,
    пока цель не достигнута;
  * кнопка «Поиск» (автопоиск) тоже работает через окружение: сначала
    выполняется `nova search`, читаются лучшие страницы, выдержки
    сохраняются в notes/research/, а дальше модель может искать ещё,
    открывать страницы и считать в python — и отвечает со ссылками [1], [2];
  * история диалога берётся из базы, ответ сохраняется туда же.

Границы (это сервер, а не песочница ОС):
  * TERMINAL_ENABLED=1 в .env, иначе главный чат работает без окружения;
  * AGENT_MAX_STEPS действий и общий AGENT_TIMEOUT — цикл не крутится вечно;
  * команды проверяются белым списком routes/terminal.py.
"""
from flask import Blueprint, Response, request, jsonify, session, stream_with_context
import json
import os
import re
import time

from ai_providers import chat_completion, chat_stream, resolve_target
from routes.terminal import (run_agent as run_terminal, safe_path, relative_to_workspace,
                             ensure_workspace, nova_search, nova_read, _save_research,
                             can_use_workspace, signed_in)

agent_bp = Blueprint("agent", __name__)

MAX_STEPS = int(os.getenv("AGENT_MAX_STEPS", "8"))
AGENT_TIMEOUT = float(os.getenv("AGENT_TIMEOUT", "240"))
STEP_TIMEOUT = float(os.getenv("AGENT_STEP_TIMEOUT", "30"))
STREAM_TIMEOUT = float(os.getenv("AGENT_STREAM_TIMEOUT", "120"))
SEARCH_PAGES = int(os.getenv("AGENT_SEARCH_PAGES", os.getenv("SEARCH_MAX_PAGES", "3")))
MAX_OBSERVATION = 3500
HISTORY_LIMIT = 16

TOOL_ACTIONS = {"plan", "run", "read", "write", "ls", "search", "open", "task", "task_done"}
ALL_ACTIONS = TOOL_ACTIONS | {"answer"}

AGENT_SYSTEM_PROMPT = """{persona}
Сегодня {date}.

У тебя есть своё Linux-окружение: рабочая папка с терминалом, файлами, python3, node, git
и доступом в интернет. Пользователь пишет с телефона — отвечай по делу, без воды.

КАК ОТВЕЧАТЬ
• Если можно ответить сразу (приветствие, объяснение, перевод, знания, совет) —
  просто ответь обычным текстом в markdown. Без JSON.
• Если для цели нужно действовать — посчитать, запустить или проверить код, создать или
  прочитать файлы, узнать свежие данные из интернета — работай в окружении ПО ШАГАМ,
  пока цель не достигнута. Каждый шаг — ТОЛЬКО один JSON-объект, без слов вокруг:

  {{"action":"plan","steps":["шаг 1","шаг 2"]}}              план для сложной задачи (первым шагом)
  {{"action":"write","path":"calc.py","content":"print(2**10)"}}  записать файл
  {{"action":"run","command":"python3 calc.py","thought":"проверю расчёт"}}  выполнить команду
  {{"action":"read","path":"data.csv"}}                        прочитать файл
  {{"action":"ls"}}                                            список файлов
  {{"action":"search","query":"что найти"}}                    поиск в интернете
  {{"action":"open","url":"https://…"}}                        прочитать страницу
  {{"action":"task","title":"…","detail":"…"}}                 завести задачу (для длинной работы)
  {{"action":"task_done","note":"что сделано"}}                закрыть задачу

• После каждого действия придёт его результат. Смотри на него и решай следующий шаг.
  Ошибка — исправь (перепиши файл, поправь команду) и попробуй снова.
• Никогда не выдумывай результат команды или страницы — сначала выполни действие.
• Когда цель достигнута — напиши финальный ответ обычным текстом (markdown):
  что сделано и какой результат. Код, который пользователю пригодится, покажи в ответе.

ОГРАНИЧЕНИЯ ТЕРМИНАЛА
Одна команда за раз, без |, >, &&, $(), sudo. Сложную логику пиши в файл (write)
и запускай (run: python3 файл.py). Разрешены: ls, cat, grep, find, head, tail, wc,
mkdir, cp, mv, rm, python3, pip, node, npm, git, curl, sqlite3, date и похожие.
Команда `nova search <запрос>` и `nova read <url>` тоже работают в терминале."""

SEARCH_ADDON = """

ПОИСК ВКЛЮЧЁН
Пользователь нажал «Поиск»: окружение уже нашло источники и прочитало лучшие страницы —
результат лежит в истории выше. Если данных мало или они противоречат друг другу — сделай
ещё search или open. Цифры можно пересчитать в python. В финальном ответе опирайся на
найденное, ссылайся на источники как [1], [2] (номера из выдачи) и прямо говори, чего
в источниках нет."""

NO_TOOLS_PROMPT = "{persona}\nСегодня {date}. Отвечай по-русски, конкретно и по делу."


def is_enabled():
    return os.getenv("AGENT_ENABLED", os.getenv("TERMINAL_ENABLED", "0")) == "1"


def tools_enabled():
    """Инструменты терминала и файлов — любому вошедшему при включённом окружении.

    Флаг читаем в момент вызова, а не на импорте: .env может подгрузиться позже.
    """
    return os.getenv("TERMINAL_ENABLED", "0") == "1" and can_use_workspace()


def parse_action(raw):
    """Достаёт JSON-объект действия из ответа модели. None — не разобрали."""
    text = (raw or "").strip()
    if not text:
        return None
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    block = fence.group(1) if fence else None
    if not block:
        start, end = text.find("{"), text.rfind("}")
        block = text[start:end + 1] if 0 <= start < end else None
    if not block:
        return None
    try:
        data = json.loads(block)
    except (TypeError, ValueError):
        # модели любят сырые переводы строк внутри "content" — пробуем мягкий разбор
        try:
            data = json.loads(block, strict=False)
        except (TypeError, ValueError):
            return None
    return data if isinstance(data, dict) and data.get("action") else None


def _action_share(text):
    """Какую долю ответа занимает JSON-объект (чтобы не принять пример из ответа за действие)."""
    start, end = text.find("{"), text.rfind("}")
    if not (0 <= start < end):
        return 0.0
    return (end - start + 1) / max(1, len(text.strip()))


def clip(text, limit=MAX_OBSERVATION):
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + "\n… обрезано"


def _decide(buffer):
    """По началу ответа понять: это действие (JSON) или обычный текст.

    True — действие, False — текст, None — пока непонятно, ждём ещё токенов.
    """
    text = buffer.lstrip()
    if not text:
        return None
    if text[0] == "{":
        return True
    if not text.startswith("`"):
        return False
    if len(text) < 3:
        return None
    if not text.startswith("```"):
        return False
    if "\n" not in text:
        return None if len(text) < 24 else False
    head, rest = text.split("\n", 1)
    head = head.strip().lower()
    if head == "```json":
        return True
    if head != "```":
        return False            # ```python и т.п. — это ответ с кодом
    rest = rest.lstrip()
    if not rest:
        return None
    return rest[0] == "{"


def _persona():
    try:
        import config
        return config.system_prompt
    except Exception:          # pragma: no cover - config всегда есть в приложении
        return "Ты — NovaMind, умный AI-ассистент."


def _ndjson(payload):
    return json.dumps(payload, ensure_ascii=False) + "\n"


def _chunks(text, size=48):
    text = text or ""
    for index in range(0, len(text), size):
        yield text[index:index + size]


def _history_for_prompt(chat_id):
    """Последние сообщения диалога — чтобы ИИ помнил контекст, как в обычном чате."""
    if not chat_id:
        return []
    try:
        from database import get_chat_history
        rows = get_chat_history(chat_id, limit=HISTORY_LIMIT)
    except Exception:
        return []
    history = []
    for row in rows:
        content = str(row.get("content") or "")
        if not content or content.startswith(("MEDIA_RESULT:", "COMPOSIO_CARDS:", "COMPOSIO_AUTH:")):
            continue
        role = "assistant" if row.get("role") in ("assistant", "ai", "model") else "user"
        history.append({"role": role, "content": clip(content, 3000)})
    return history


# ───────────────────────── маршруты ─────────────────────────

@agent_bp.route("/api/agent/status")
def agent_status():
    enabled = is_enabled()
    tools = tools_enabled()
    hint = ""
    if not enabled or os.getenv("TERMINAL_ENABLED", "0") != "1":
        hint = "Linux-окружение выключено: добавьте TERMINAL_ENABLED=1 в .env и перезапустите сервер"
    elif not tools:
        hint = "Войдите в аккаунт, чтобы ИИ мог работать в Linux-окружении"
    return jsonify({
        "enabled": enabled,
        "tools": tools,
        "available": enabled and tools,
        "max_steps": MAX_STEPS,
        "user": signed_in(),
        "admin": bool(session.get("admin_logged_in")),
        "hint": hint,
    })


@agent_bp.route("/api/agent/stream", methods=["POST"])
def agent_stream():
    """Главный чат: ИИ отвечает сам или работает в Linux по шагам. NDJSON."""
    data = request.get_json(silent=True) or {}
    goal = (data.get("message") or "").strip()
    if not goal:
        return jsonify({"error": "Пустое сообщение"}), 400
    if not is_enabled():
        return jsonify({"error": "Linux-окружение выключено (TERMINAL_ENABLED=1)"}), 403
    if not signed_in():
        return jsonify({"error": "Войдите в аккаунт, чтобы ИИ работал в Linux-окружении"}), 403

    search_mode = bool(data.get("search"))
    reasoning = bool(data.get("reasoning"))
    provider, model, _ = resolve_target()
    use_tools = tools_enabled()
    if use_tools:
        ensure_workspace()

    # История и сохранение — как у обычного чата (/send_stream)
    chat_id = None
    history = []
    if data.get("save_history", True):
        try:
            from database import get_or_create_session_chat, add_message
            chat_id = get_or_create_session_chat()
            history = _history_for_prompt(chat_id)
            add_message(chat_id, "user", goal)
        except Exception as exc:        # история не должна ронять ответ
            print(f"[agent] история недоступна: {exc}")
            chat_id = None

    date = time.strftime("%Y-%m-%d")
    if use_tools:
        system = AGENT_SYSTEM_PROMPT.format(persona=_persona(), date=date)
        if search_mode:
            system += SEARCH_ADDON
    else:
        system = NO_TOOLS_PROMPT.format(persona=_persona(), date=date)

    @stream_with_context
    def generate():
        started = time.time()
        state = {"task_id": None, "steps": 0, "answer": "", "sources": [],
                 "searched": False, "announced": False}
        messages = list(history) + [{"role": "user", "content": goal}]

        try:
            if search_mode and use_tools:
                yield from _auto_search(goal, state, messages)

            for step_no in range(MAX_STEPS + 1):
                out_of_time = time.time() - started > AGENT_TIMEOUT
                allow = use_tools and step_no < MAX_STEPS and not out_of_time
                if use_tools and not allow and step_no > 0:
                    note = "Время вышло" if out_of_time else "Шаги закончились"
                    yield _ndjson({"type": "step", "icon": "⏱", "text": f"{note} — собираю ответ"})
                    messages.append({"role": "user", "content":
                                     f"{note}. Больше никаких действий: напиши финальный ответ "
                                     f"пользователю обычным текстом по тому, что удалось сделать."})

                outcome = yield from _model_step(
                    messages, system, provider, model,
                    reasoning=reasoning and step_no == 0,
                    allow_action=allow,
                )
                if outcome["kind"] == "answer":
                    state["answer"] = outcome["text"]
                    break
                if outcome["kind"] == "error":
                    yield _ndjson({"type": "error", "text": str(outcome["error"] or "модель не ответила")})
                    break

                action = outcome["action"]
                kind = str(action.get("action"))
                if not state["announced"] and not search_mode:
                    state["announced"] = True
                    yield _ndjson({"type": "stage", "scene": "agent", "title": "Работаю в Linux",
                                   "text": "Иду к цели по шагам…"})
                state["steps"] += 1
                thought = str(action.get("thought") or "").strip()
                if thought:
                    yield _ndjson({"type": "step", "icon": "💭", "text": thought[:200]})
                observation = yield from _perform(action, kind, use_tools, state, goal=goal)
                messages.append({"role": "assistant",
                                 "content": json.dumps(action, ensure_ascii=False)[:3000]})
                messages.append({"role": "user", "content":
                                 f"Результат действия:\n{clip(observation)}\n\n"
                                 f"Дальше: следующий шаг (JSON) или, если цель достигнута, "
                                 f"финальный ответ обычным текстом."})
        except GeneratorExit:
            raise
        except Exception as exc:
            yield _ndjson({"type": "error", "text": f"{exc.__class__.__name__}: {exc}"})

        if not state["answer"]:
            state["answer"] = "Не удалось получить ответ модели. Проверьте модель в настройках и повторите."
            for chunk in _chunks(state["answer"]):
                yield _ndjson({"type": "token", "token": chunk})

        if state["task_id"]:
            import database
            task = database.get_task(state["task_id"])
            if task and task["status"] != "done":
                database.update_task(state["task_id"], status="done")
                database.append_task_step(state["task_id"], "ИИ закончил", status="done",
                                          log=clip(state["answer"], 1500))
                yield _ndjson({"type": "task", "task": database.get_task(state["task_id"])})

        if chat_id:
            try:
                from database import add_message, trim_messages
                add_message(chat_id, "assistant", state["answer"])
                trim_messages(chat_id, max_messages=100)
            except Exception as exc:
                print(f"[agent] не удалось сохранить ответ: {exc}")

        yield _ndjson({"type": "result", "reply": state["answer"], "steps": state["steps"],
                       "task_id": state["task_id"], "model": model, "provider": provider,
                       "sources": state["sources"], "searched": state["searched"],
                       "offline": provider == "local_demo"})
        yield _ndjson({"type": "done"})

    return Response(generate(), mimetype="application/x-ndjson",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ───────────────────────── один шаг модели ─────────────────────────

def _model_step(messages, system, provider, model, reasoning=False, allow_action=True):
    """Потоковый запрос к модели.

    Генератор событий (reasoning/token/retract). Возвращает словарь:
      {"kind": "answer", "text": …}   — модель ответила текстом (уже напечатан);
      {"kind": "action", "action": …} — модель выбрала действие в окружении;
      {"kind": "error",  "error": …}  — модель не ответила.
    """
    buffer = ""
    mode = None                   # None — ещё не решили, "action" / "text"
    failure = None
    try:
        for kind, chunk in chat_stream(messages, provider=provider, model=model, system=system,
                                       temperature=0.3, timeout=STREAM_TIMEOUT,
                                       reasoning=reasoning):
            if kind == "reasoning":
                if chunk:
                    yield _ndjson({"type": "reasoning", "token": chunk})
                continue
            if kind == "error":
                failure = chunk
                continue
            if kind == "done":
                break
            if kind != "token" or not chunk:
                continue
            buffer += chunk
            if mode is None:
                decision = _decide(buffer)
                if decision is None:
                    continue
                mode = "action" if decision else "text"
                if mode == "text":
                    yield _ndjson({"type": "token", "token": buffer})
                continue
            if mode == "text":
                yield _ndjson({"type": "token", "token": chunk})
    except GeneratorExit:
        raise
    except Exception as exc:      # сеть или провайдер упали посреди потока
        failure = f"{exc.__class__.__name__}: {exc}"

    text = buffer.strip()
    if not text:
        # Поток пустой — один повтор без стрима, затем честная ошибка
        raw, error = chat_completion(messages, provider=provider, model=model, system=system,
                                     temperature=0.3, timeout=STREAM_TIMEOUT)
        if not raw or not str(raw).strip():
            return {"kind": "error", "error": failure or error}
        text, mode = str(raw).strip(), None

    if mode == "text":
        # Текст уже напечатан. Бывает, что модель пишет фразу, а потом JSON действия —
        # тогда забираем напечатанное назад и выполняем действие.
        if allow_action:
            action = parse_action(text)
            if (action and action.get("action") in TOOL_ACTIONS
                    and _action_share(text) >= 0.6):
                yield _ndjson({"type": "retract"})
                return {"kind": "action", "action": action}
        return {"kind": "answer", "text": text}

    action = parse_action(text)
    if action and action.get("action") == "answer":
        answer = str(action.get("text") or "").strip() or text
        for chunk in _chunks(answer):
            yield _ndjson({"type": "token", "token": chunk})
        return {"kind": "answer", "text": answer}
    if action and allow_action and action.get("action") in TOOL_ACTIONS:
        return {"kind": "action", "action": action}

    # Не разобрали JSON (или действия сейчас нельзя) — показываем как есть
    for chunk in _chunks(text):
        yield _ndjson({"type": "token", "token": chunk})
    return {"kind": "answer", "text": text}


# ───────────────────────── источники ─────────────────────────

def _add_sources(state, results):
    """Складывает найденное в общий список источников. Возвращает [(номер, источник)]."""
    numbered = []
    known = {item["url"]: index for index, item in enumerate(state["sources"], 1)}
    for item in results or []:
        url = item.get("url")
        if not url:
            continue
        if url not in known:
            state["sources"].append({
                "title": item.get("title") or item.get("host") or url,
                "url": url,
                "snippet": item.get("snippet", ""),
                "host": item.get("host", ""),
            })
            known[url] = len(state["sources"])
        numbered.append((known[url], state["sources"][known[url] - 1]))
    return numbered


def _format_numbered(query, numbered, backend=None, elapsed=0):
    lines = [f"🔎 «{query}» — найдено {len(numbered)} ({elapsed / 1000:.1f} c"
             + (f", {backend}" if backend else "") + ")"]
    for number, item in numbered:
        lines.append(f"[{number}] {item['title']}")
        lines.append(f"    {item['url']}")
        if item.get("snippet"):
            lines.append(f"    {item['snippet'][:260]}")
    return "\n".join(lines)


def _search_query(goal):
    """Поисковый запрос из сообщения: без лишнего запроса к модели — быстрее на телефоне."""
    text = " ".join((goal or "").split())
    return text[:160] or goal


def _auto_search(goal, state, messages):
    """Кнопка «Поиск»: поиск и чтение страниц через Linux-окружение.

    Всё, что найдено, видно в блоке «Работа в Linux», сохраняется в
    notes/research/ и уходит модели как результат первого шага.
    """
    from routes.search import _read_pages

    query = _search_query(goal)
    state["searched"] = True
    yield _ndjson({"type": "stage", "scene": "search", "title": "Поищу в интернете",
                   "text": "Ищу через Linux-окружение…"})
    results, meta = nova_search(query, limit=8)
    numbered = _add_sources(state, results)
    listing = _format_numbered(query, numbered, meta.get("backend"), meta.get("elapsed_ms", 0))
    action = {"action": "search", "query": query}

    if not numbered:
        yield _ndjson({"type": "step", "icon": "⚠️", "text": "Поиск ничего не дал — попробую по-другому"})
        messages.append({"role": "assistant", "content": json.dumps(action, ensure_ascii=False)})
        messages.append({"role": "user", "content":
                         "Результат действия: поиск ничего не нашёл. Попробуй другой запрос "
                         "(search) или ответь из своих знаний с пометкой «(из базы знаний)»."})
        return

    yield _ndjson({"type": "step", "icon": "🔎",
                   "text": f"Нашёл {len(numbered)} источников ({meta.get('backend') or 'поиск'})"})
    yield _ndjson({"type": "tool", "name": "search", "command": f"nova search {query}",
                   "code": 0, "output": listing, "results": results})
    yield _ndjson({"type": "sources", "sources": state["sources"]})

    pages = []
    top = [item for _, item in numbered[:max(0, SEARCH_PAGES)]]
    if top:
        yield _ndjson({"type": "step", "icon": "📄", "text": f"Читаю {len(top)} страницы…"})
        pages = _read_pages(top)
        for page in pages:
            yield _ndjson({"type": "tool", "name": "open", "command": f"nova read {page['url']}",
                           "code": 0 if page["chars"] > 200 else 1,
                           "output": clip(page["text"], 400)})

    number_of = {item["url"]: number for number, item in numbered}
    context_parts = [listing, ""]
    for page in pages:
        if page.get("text"):
            context_parts.append(f"### [{number_of.get(page['url'], '?')}] {page['title']}\n"
                                 f"URL: {page['url']}\n{page['text']}")
    context = "\n\n".join(context_parts)

    try:
        stamp = time.strftime("%Y-%m-%d %H:%M")
        saved = _save_research(query, f"# {query}\n\n_поиск {stamp}_\n\n{context}\n", "search")
        yield _ndjson({"type": "step", "icon": "💾", "text": f"Сохранил выдержки: {saved}"})
    except OSError as exc:
        print(f"[agent] не удалось сохранить заметку: {exc}")

    yield _ndjson({"type": "stage", "scene": "write", "title": "Разбираю найденное",
                   "text": "Решаю, хватает ли данных"})
    messages.append({"role": "assistant", "content": json.dumps(action, ensure_ascii=False)})
    messages.append({"role": "user", "content":
                     f"Результат действия (поиск и чтение страниц):\n{clip(context, 9000)}\n\n"
                     f"Дальше: ещё search/open/run, если данных мало, или финальный ответ "
                     f"обычным текстом со ссылками [n]."})


# ───────────────────────── действия ─────────────────────────

def _perform(action, kind, use_tools, state, goal=""):
    """Одно действие в окружении.

    Генератор: отдаёт события потока (шаги, вывод команд, задачи) и
    возвращает наблюдение, которое уходит обратно в модель.
    """
    import database

    if kind == "plan":
        steps = action.get("steps")
        if isinstance(steps, list):
            text = "\n".join(f"{index}. {str(step).strip()}" for index, step in enumerate(steps, 1)
                             if str(step).strip())
        else:
            text = str(steps or action.get("text") or "").strip()
        if text:
            yield _ndjson({"type": "plan", "text": clip(text, 900)})
        return "План принят. Выполняй первый шаг."

    if kind == "task":
        task = database.create_task(
            title=(action.get("title") or goal or "Задача ИИ")[:200],
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

    if not use_tools:
        return "Инструменты недоступны. Ответь пользователю по имеющимся данным."

    if kind == "search":
        query = str(action.get("query") or "").strip()
        if not query:
            return 'Пустой поисковый запрос. Повтори с {"action":"search","query":"..."}.'
        results, meta = nova_search(query, limit=6)
        numbered = _add_sources(state, results)
        text = _format_numbered(query, numbered, meta.get("backend"), meta.get("elapsed_ms", 0))
        if not numbered:
            yield _ndjson({"type": "step", "icon": "⚠️", "text": f"Поиск «{query}» ничего не дал"})
            text += "\nНичего не нашлось. Попробуй другой запрос."
        else:
            state["searched"] = True
            yield _ndjson({"type": "step", "icon": "🔎",
                           "text": f"Нашёл {len(numbered)} по запросу «{query}» ({meta.get('backend')})"})
            yield _ndjson({"type": "tool", "name": "search", "command": f"nova search {query}",
                           "code": 0, "output": text, "results": results})
            yield _ndjson({"type": "sources", "sources": state["sources"]})
        if state["task_id"]:
            database.append_task_step(state["task_id"], f"Поиск: {query}", status="done",
                                      log=text[:1200])
        return text

    if kind == "open":
        url = str(action.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            return "Нужен полный адрес страницы, например https://example.com"
        text, elapsed = nova_read(url, max_chars=4000)
        if state["task_id"]:
            database.append_task_step(state["task_id"], f"Прочитал {url}", status="done",
                                      log=(text or "")[:1200])
        yield _ndjson({"type": "step", "icon": "📰", "text": f"Прочитал {url} ({elapsed} мс)"})
        yield _ndjson({"type": "tool", "name": "open", "command": url, "code": 0 if text else 1,
                       "output": (text or "страница пустая")[:1500]})
        return text or "Страница не прочиталась."

    if kind == "ls":
        command, label = "ls -la", "Смотрю файлы"
    elif kind == "read":
        try:
            path = safe_path(action.get("path", ""))
        except ValueError as exc:
            return str(exc)
        if not os.path.isfile(path):
            yield _ndjson({"type": "tool", "name": "read", "command": f"cat {action.get('path', '')}",
                           "code": 1, "output": "Файла нет"})
            return f"Файла {action.get('path', '')} нет. Посмотри список: {{\"action\":\"ls\"}}"
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            content = handle.read(MAX_OBSERVATION)
        yield _ndjson({"type": "step", "icon": "📖", "text": f"Читаю {relative_to_workspace(path)}"})
        yield _ndjson({"type": "tool", "name": "read", "command": f"cat {relative_to_workspace(path)}",
                       "code": 0, "output": clip(content, 600)})
        return content or "(файл пустой)"

    elif kind == "write":
        try:
            path = safe_path(action.get("path", "notes/out.md"))
        except ValueError as exc:
            return str(exc)
        if os.path.isdir(path):
            return "Это папка, укажи путь к файлу."
        content = str(action.get("content") or "")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(content)
        relative = relative_to_workspace(path)
        if state["task_id"]:
            database.append_task_step(state["task_id"], f"Записал {relative}",
                                      status="doing", log=clip(content, 800))
        yield _ndjson({"type": "step", "icon": "✍️", "text": f"Записал {relative}"})
        yield _ndjson({"type": "tool", "name": "write", "command": f"write {relative}",
                       "code": 0, "output": clip(content, 600)})
        return f"Файл {relative} записан ({len(content)} символов)."

    elif kind == "run":
        command = str(action.get("command") or "").strip()
        label = f"$ {command}"
    else:
        return f"Неизвестное действие: {kind}. Используй одно из: {', '.join(sorted(ALL_ACTIONS))}."

    result = run_terminal(command, timeout=STEP_TIMEOUT)
    output = (result["stdout"] or "") + (("\n" + result["stderr"]) if result["stderr"] else "")
    if state["task_id"]:
        database.append_task_step(
            state["task_id"], label, status="error" if result["code"] else "done",
            log=clip(output, 1200) or f"код {result['code']}",
        )
    status = "❌" if result["code"] else "✅"
    yield _ndjson({"type": "step", "icon": status, "text": f"{label} — код {result['code']}"})
    yield _ndjson({"type": "tool", "name": "run", "command": command, "code": result["code"],
                   "output": clip(output, 1500)})
    body = output.strip() or "(пустой вывод)"
    return f"$ {command}\nкод выхода: {result['code']}\n{body}"
