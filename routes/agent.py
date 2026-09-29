"""
routes/agent.py — агентный режим.

Модель не отвечает сразу, а планирует и действует инструментами:

    план → [команда в песочнице | чтение/запись файла | задача] × N → ответ

Поток событий — NDJSON, ровно как у поиска: интерфейс показывает
`plan`, `step`, `tool` и печатающийся ответ.

Границы (важно — это сервер, а не песочница ОС):
  * AGENT_ENABLED=1 в .env, иначе режим выключен;
  * AGENT_MAX_STEPS шагов и общий AGENT_TIMEOUT — цикл не крутится вечно;
  * команды дополнительно проверяются белым списком routes/terminal.py;
  * инструменты файлов/терминала доступны только администратору, задачи —
    всем (это личный список дел, а не управление сервером).
"""
from flask import Blueprint, Response, request, jsonify, session, stream_with_context
import json
import os
import re
import time

from ai_providers import chat_completion, chat_stream, resolve_target
from routes.terminal import (run_agent as run_terminal, safe_path, relative_to_workspace,
                             ensure_workspace, nova_search, nova_read, _save_research,
                             _format_results as _format_search_results)

agent_bp = Blueprint("agent", __name__)

MAX_STEPS = int(os.getenv("AGENT_MAX_STEPS", "6"))
AGENT_TIMEOUT = float(os.getenv("AGENT_TIMEOUT", "180"))
STEP_TIMEOUT = float(os.getenv("AGENT_STEP_TIMEOUT", "12"))
MAX_OBSERVATION = 2500

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
5. Нужны свежие данные или факты — сначала search, потом опирайся на найденное.
6. Результаты поиска полезно сохранять: write в notes/research/<тема>.md,
   а не держать в ответе — тогда их можно будет открыть позже.
7. В answer пиши по-русски, конкретно, с результатами: что сделано и что получилось.
   Если опирался на интернет — перечисли источники по именам из выдачи."""


def is_enabled():
    return os.getenv("AGENT_ENABLED", os.getenv("TERMINAL_ENABLED", "0")) == "1"


def tools_enabled():
    """Инструменты терминала и файлов — только админу при включённом окружении.

    Флаг читаем в момент вызова, а не на импорте: .env может подгрузиться позже.
    """
    return os.getenv("TERMINAL_ENABLED", "0") == "1" and bool(session.get("admin_logged_in"))


def parse_action(raw):
    """Достаёт JSON-объект действия из ответа модели. None — не разобрали."""
    text = (raw or "").strip()
    if not text:
        return None
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    block = fence.group(1) if fence else None
    if not block:
        start, end = text.find("{"), text.rfind("}")
        block = text[start:end + 1] if 0 <= start < end else None
    if not block:
        return None
    try:
        data = json.loads(block)
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("action") else None


def clip(text, limit=MAX_OBSERVATION):
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + "\n… обрезано"


@agent_bp.route("/api/agent/status")
def agent_status():
    enabled = is_enabled()
    return jsonify({
        "enabled": enabled,
        "tools": tools_enabled(),
        "max_steps": MAX_STEPS,
        "admin": bool(session.get("admin_logged_in")),
        "hint": "" if enabled else "Включите AGENT_ENABLED=1 в .env и перезапустите сервер",
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

    provider, model, _ = resolve_target()
    use_tools = tools_enabled()
    if use_tools:
        ensure_workspace()

    @stream_with_context
    def generate():
        started = time.time()
        state = {"task_id": None, "steps": 0, "answer": ""}
        yield _ndjson({"type": "stage", "scene": "agent", "title": "Планирую",
                       "text": "Разбираю задачу по шагам…"})

        # 1. План одним запросом — чтобы пользователь сразу видел ход мыслей
        plan_text = ""
        if use_tools:
            plan_answer, error = chat_completion(
                [{"role": "user", "content":
                    f"Задача: {goal}\n\nСоставь короткий план из 2-4 пунктов, что нужно сделать "
                    f"в рабочей папке. Без JSON, только текст."}],
                provider=provider, model=model, temperature=0.2, max_tokens=300, timeout=30,
            )
            if plan_answer and not error:
                plan_text = clip(plan_answer, 900)
                yield _ndjson({"type": "plan", "text": plan_text})

        history = [{"role": "system", "content": AGENT_SYSTEM_PROMPT}]
        if plan_text:
            history.append({"role": "assistant", "content": f"План:\n{plan_text}"})
        history.append({"role": "user", "content": f"Задача: {goal}"})

        # 2. Цикл действий
        for _ in range(MAX_STEPS):
            if time.time() - started > AGENT_TIMEOUT:
                yield _ndjson({"type": "step", "icon": "⏱", "text": "Время вышло — собираю ответ"})
                break
            if not use_tools:
                break

            state["steps"] += 1
            raw, error = chat_completion(history, provider=provider, model=model,
                                         temperature=0.1, max_tokens=500, timeout=45)
            action = parse_action(raw)
            if not action:
                # Модель не выдала JSON — считаем это финальным ответом
                if raw and not error:
                    state["answer"] = raw.strip()
                    break
                yield _ndjson({"type": "error", "text": str(error or "модель не ответила")})
                state["answer"] = "Не удалось получить план действий. Попробуйте ещё раз."
                break

            kind = str(action.get("action"))
            observation = yield from _perform(action, kind, use_tools, state, goal=goal)
            history.append({"role": "assistant", "content": json.dumps(action, ensure_ascii=False)[:2000]})
            history.append({"role": "user", "content": f"Результат действия:\n{clip(observation)}"})
            if kind == "answer":
                state["answer"] = str(action.get("text") or "").strip()
                break
        else:
            state["answer"] = state["answer"] or "Достигнут лимит шагов — вот что удалось сделать."

        # 3. Ответ: если модель не отвела финал, собираем его одним запросом
        if not state["answer"]:
            history.append({"role": "user", "content":
                            "Дай финальный ответ пользователю по задаче. JSON: "
                            '{"action":"answer","text":"…"}'})
            raw, _error = chat_completion(history, provider=provider, model=model,
                                          temperature=0.3, max_tokens=1200, timeout=60)
            action = parse_action(raw)
            # Модель могла ответить не тем действием — показываем хоть что-то
            state["answer"] = ((action or {}).get("text") or "").strip() or (raw or "").strip()

        if not state["answer"]:
            state["answer"] = "Не удалось получить ответ. Проверьте модель и настройки."

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

        yield _ndjson({"type": "result", "reply": state["answer"], "steps": state["steps"],
                       "task_id": state["task_id"], "model": model,
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
        results, meta = nova_search(query, limit=6)
        text = _format_search_results(query, results, meta["backend"], meta["elapsed_ms"])
        if not results:
            yield _ndjson({"type": "step", "icon": "⚠️", "text": f"Поиск «{query}» ничего не дал"})
        else:
            yield _ndjson({"type": "step", "icon": "🔎",
                           "text": f"Нашёл {len(results)} по запросу «{query}» ({meta['backend']})"})
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
        text, elapsed = nova_read(url, max_chars=4000)
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
        try:
            path = safe_path(action.get("path", ""))
        except ValueError as exc:
            return str(exc)
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            content = handle.read(MAX_OBSERVATION)
        yield _ndjson({"type": "step", "icon": "📖", "text": f"Читаю {relative_to_workspace(path)}"})
        yield _ndjson({"type": "tool", "name": "read", "command": relative_to_workspace(path),
                       "code": 0, "output": clip(content, 600)})
        return content

    elif kind == "write":
        try:
            path = safe_path(action.get("path", "notes/out.md"))
        except ValueError as exc:
            return str(exc)
        content = str(action.get("content") or "")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(content)
        if state["task_id"]:
            database.append_task_step(state["task_id"], f"Записал {relative_to_workspace(path)}",
                                      status="doing", log=clip(content, 800))
        yield _ndjson({"type": "step", "icon": "✍️", "text": f"Записал {relative_to_workspace(path)}"})
        return f"Файл {relative_to_workspace(path)} записан. Открой его командой cat {relative_to_workspace(path)}."

    elif kind == "run":
        command = str(action.get("command") or "").strip()
        label, icon = f"$ {command}", "⚙️"
    else:
        return f"Неизвестное действие: {kind}. Ответь пользователю."

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
    return output or f"Команда завершилась с кодом {result['code']} и пустым выводом."
