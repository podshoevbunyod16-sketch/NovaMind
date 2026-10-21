"""evals — прогон агента по набору задач и таблица результатов.

Идея простая: 25 задач в YAML, каждая — обычный запрос пользователя плюс
ожидания (какие инструменты должны сработать, что должно быть в ответе,
сколько шагов допустимо). Раннер шлёт запрос в живой сервер тем же
эндпоинтом, что и браузер, и считает успех, шаги, время и токены.

    python evals/run.py                     # все задачи
    python evals/run.py --only search       # только категория
    python evals/run.py --json out.json     # машинный отчёт

Зависимостей нет: только stdlib + requests (он и так в requirements.txt).
"""
from __future__ import annotations

import json
import os
import statistics
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from evals import yamlmini                                        # noqa: E402

TASKS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tasks")


# ══════════════ разбор потока ответа ══════════════

def parse_events(lines):
    """NDJSON-поток агента → список событий (битые строки пропускаем)."""
    events = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def summarize(events):
    """Собирает из событий ответ, шаги, инструменты и оценку токенов."""
    answer_parts = []
    steps = 0
    tools = []
    usage = None
    error = ""
    stopped = False
    for event in events:
        kind = event.get("type")
        if kind == "token":
            answer_parts.append(str(event.get("token") or ""))
        elif kind == "step":
            steps += 1
        elif kind == "tool":
            name = event.get("name")
            if name and name not in tools:
                tools.append(name)
        elif kind == "result":
            usage = event.get("usage") or usage
            stopped = bool(event.get("stopped"))
            if event.get("reply") and not answer_parts:
                answer_parts.append(str(event["reply"]))
        elif kind == "error":
            error = str(event.get("text") or event.get("error") or "ошибка")
    answer = "".join(answer_parts).strip()
    tokens = _tokens(usage, answer)
    return {"answer": answer, "steps": steps, "tools": tools,
            "tokens": tokens, "error": error, "stopped": stopped, "usage": usage}


def _tokens(usage, answer):
    """Токены: из ответа провайдера, иначе грубая оценка по символам."""
    if isinstance(usage, dict):
        for key in ("total_tokens", "total", "completion_tokens"):
            value = usage.get(key)
            if isinstance(value, int) and value > 0:
                return value
    return max(1, (len(answer) + 3) // 4) if answer else 0


# ══════════════ проверка задачи ══════════════

def evaluate(task, result):
    """Возвращает (успех, причина отказа)."""
    if result.get("error"):
        return False, f"ошибка: {result['error'][:80]}"
    if result.get("stopped"):
        return False, "остановлено"
    if not result.get("answer"):
        return False, "пустой ответ"
    answer = result["answer"].lower()
    for expectation in task.get("expect") or []:
        if expectation.lower() not in answer:
            return False, f"в ответе нет «{expectation}»"
    for tool in task.get("tools") or []:
        if tool not in result.get("tools", []):
            return False, f"инструмент {tool} не вызывался"
    for tool in task.get("forbid_tools") or []:
        if tool in result.get("tools", []):
            return False, f"вызван запрещённый инструмент {tool}"
    if result.get("steps", 0) > task.get("max_steps", 8):
        return False, f"слишком много шагов: {result['steps']}"
    return True, ""


# ══════════════ транспорт ══════════════

class HttpTransport:
    """Шлёт запрос в живой сервер и читает поток NDJSON."""

    def __init__(self, base_url: str, timeout: int = 240, cookies: str = ""):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.cookies = cookies

    def __call__(self, prompt: str):
        import requests
        headers = {"Content-Type": "application/json", "Accept": "application/x-ndjson"}
        if self.cookies:
            headers["Cookie"] = self.cookies
        started = time.time()
        response = requests.post(f"{self.base_url}/api/agent/stream",
                                 json={"message": prompt}, headers=headers,
                                 stream=True, timeout=self.timeout)
        if response.status_code >= 400:
            try:
                payload = response.json()
            except ValueError:
                payload = {"error": response.text[:200]}
            return [{"type": "error", "text": str(payload.get("error") or response.status_code)}], \
                time.time() - started
        lines = response.iter_lines(decode_unicode=True)
        events = parse_events(line for line in lines if line)
        return events, time.time() - started


def run_task(task, transport):
    """Один прогон: запрос → события → оценка. Возвращает строку отчёта."""
    started = time.time()
    try:
        events, _elapsed = transport(task["prompt"])
    except Exception as error:                                   # сеть, таймаут и т.п.
        return {"id": task["id"], "category": task.get("category", ""),
                "ok": False, "reason": f"сбой транспорта: {error}"[:120],
                "steps": 0, "time": round(time.time() - started, 2), "tokens": 0}
    elapsed = time.time() - started
    summary = summarize(events)
    ok, reason = evaluate(task, summary)
    return {"id": task["id"], "category": task.get("category", ""), "ok": ok,
            "reason": reason, "steps": summary["steps"],
            "time": round(elapsed, 2), "tokens": summary["tokens"],
            "tools": summary["tools"], "answer": summary["answer"][:500]}


def run_all(tasks, transport):
    return [run_task(task, transport) for task in tasks]


# ══════════════ отчёт ══════════════

def format_table(results):
    """Простая таблица без внешних библиотек."""
    header = ("#", "Задача", "Итог", "Шаги", "Время, с", "Токены", "Комментарий")
    rows = []
    for index, result in enumerate(results, 1):
        rows.append((str(index), result["id"], "✅" if result["ok"] else "❌",
                     str(result.get("steps", 0)), f"{result.get('time', 0):.2f}",
                     str(result.get("tokens", 0)), (result.get("reason") or "")[:40]))
    widths = [max(len(header[i]), *(len(row[i]) for row in rows)) if rows else len(header[i])
              for i in range(len(header))]
    def line(cells):
        return "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(cells)).rstrip()
    output = [line(header), "  ".join("-" * width for width in widths)]
    output += [line(row) for row in rows]
    return "\n".join(output)


def summary_line(results):
    total = len(results) or 1
    passed = sum(1 for result in results if result["ok"])
    times = [result.get("time", 0) for result in results]
    tokens = sum(result.get("tokens", 0) for result in results)
    steps = [result.get("steps", 0) for result in results]
    return (f"Успех: {passed}/{total} ({passed * 100 // total}%) · "
            f"среднее время {statistics.mean(times):.2f} с · "
            f"средние шаги {statistics.mean(steps):.1f} · токенов {tokens}")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    base_url = os.getenv("SITE_URL", "http://localhost:5000")
    only = ""
    json_path = ""
    tasks_dir = TASKS_DIR
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == "--base-url" and index + 1 < len(argv):
            base_url = argv[index + 1]; index += 2
        elif arg == "--only" and index + 1 < len(argv):
            only = argv[index + 1].lower(); index += 2
        elif arg == "--json" and index + 1 < len(argv):
            json_path = argv[index + 1]; index += 2
        elif arg == "--tasks" and index + 1 < len(argv):
            tasks_dir = argv[index + 1]; index += 2
        elif arg in ("-h", "--help"):
            print(__doc__)
            return 0
        else:
            print(f"Неизвестный аргумент: {arg}\n{__doc__}")
            return 2
    tasks = yamlmini.load_tasks(tasks_dir)
    if only:
        tasks = [task for task in tasks
                 if only in str(task.get("category", "")).lower() or only in task["id"].lower()]
    if not tasks:
        print("Задачи не найдены.")
        return 1
    print(f"Сервер: {base_url} · задач: {len(tasks)}")
    results = run_all(tasks, HttpTransport(base_url))
    print(format_table(results))
    print(summary_line(results))
    if json_path:
        with open(json_path, "w", encoding="utf-8") as handle:
            json.dump(results, handle, ensure_ascii=False, indent=2)
        print(f"Отчёт: {json_path}")
    return 0 if all(result["ok"] for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
