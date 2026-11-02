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

#: Что делать, если сервер ограничил запросы. Замеры — не пользовательский
#: трафик, им нужен свой режим.
RATE_LIMIT_HINT = (
    "⚠️  Сервер ограничивает запросы агента (429). Прогон остановлен.\n"
    "    Запустите сервер для замеров без лимита:\n"
    "        AGENT_RATE_LIMIT=0 python app.py\n"
    "    или поднимите лимит: AGENT_RATE_GUEST=500 AGENT_RATE_WINDOW=60")


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
    offline = False
    provider = ""
    tool_hallucinations = 0
    tool_calls = 0
    unauthorized_actions = 0
    false_done = 0
    verified = None
    shell_commands = []
    repeated_shell_after_error = 0
    shell_error_commands = set()
    for event in events:
        kind = event.get("type")
        if kind == "token":
            answer_parts.append(str(event.get("token") or ""))
        elif kind == "step":
            steps += 1
        elif kind == "tool":
            tool_calls += 1
            name = event.get("name")
            if name and name not in tools:
                tools.append(name)
            if event.get("error_type") == "unknown_tool":
                tool_hallucinations += 1
            if event.get("risk") not in (None, "safe") and event.get("authorized") is False:
                unauthorized_actions += 1
            if name in {"shell", "bash", "terminal", "run"}:
                command = str(event.get("command") or event.get("detail") or "").strip()
                if event.get("status") == "error":
                    shell_error_commands.add(command)
                elif command and command in shell_error_commands:
                    repeated_shell_after_error += 1
                shell_commands.append(command)
        elif kind == "result":
            usage = event.get("usage") or usage
            stopped = bool(event.get("stopped"))
            offline = bool(event.get("offline")) or offline
            provider = str(event.get("provider") or provider)
            if event.get("reply") and not answer_parts:
                answer_parts.append(str(event["reply"]))
            verification = event.get("verification") if isinstance(event.get("verification"), dict) else None
            if verification:
                verified = bool(verification.get("verified"))
                if verification.get("required") and not verification.get("verified"):
                    false_done += 1
            tool_hallucinations += int(event.get("tool_hallucinations") or 0)
        elif kind == "error":
            error = str(event.get("text") or event.get("error") or "ошибка")
    answer = "".join(answer_parts).strip()
    tokens = _tokens(usage, answer)
    if provider == "local_demo":
        offline = True
    return {"answer": answer, "steps": steps, "tools": tools,
            "tokens": tokens, "error": error, "stopped": stopped, "usage": usage,
            "offline": offline, "provider": provider,
            "tool_hallucinations": tool_hallucinations,
            "tool_hallucination_rate": round(tool_hallucinations / max(1, tool_calls), 4),
            "bash_repeats_after_error": repeated_shell_after_error,
            "false_done": false_done,
            "unauthorized_actions": unauthorized_actions,
            "confirmed_success": any(event.get("type") == "completion"
                                      and event.get("outcome") == "confirmed"
                                      for event in events),
            "verified": verified}


def is_rate_limited(events) -> bool:
    """Прогон упёрся в лимит запросов агента, а не в качество ответа."""
    for event in events or []:
        if event.get("type") != "error":
            continue
        text = str(event.get("text") or event.get("error") or "").lower()
        if "слишком много запросов" in text or "too many requests" in text:
            return True
    return False


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
    metric = task.get("signal")
    if metric:
        value = result.get(metric, 0)
        if "max_signal" in task and float(value or 0) > float(task["max_signal"]):
            return False, f"сигнал {metric}={value} выше {task['max_signal']}"
        if "min_signal" in task and float(value or 0) < float(task["min_signal"]):
            return False, f"сигнал {metric}={value} ниже {task['min_signal']}"
    if task.get("expect_verified") and not result.get("verified"):
        return False, "результат не подтверждён доказательством"
    if task.get("expect_confirmed_success") and not result.get("confirmed_success"):
        return False, "успех не подтверждён пользователем"
    return True, ""


# ══════════════ транспорт ══════════════

class HttpTransport:
    """Шлёт запрос в живой сервер и читает поток NDJSON.

    Лимит агента (``AGENT_RATE_LIMIT``) рассчитан на живого пользователя:
    по умолчанию 30 гостевых запросов на окно в 3600 секунд. Прогон из 35
    задач в него не влезает, поэтому транспорт различает «модель ответила
    плохо» и «сервер попросил подождать»:

    * короткая пауза (``Retry-After`` до ``quick_wait`` секунд) — повторяем,
      это дешёво и обычно срабатывает;
    * длинная пауза — не ждём: окно в час, ожидание умножится на число задач.
      Возвращаем ошибку с флагом ``rate_limited``, и прогон честно
      останавливается с подсказкой, как запускать замеры.
    """

    def __init__(self, base_url: str, timeout: int = 240, cookies: str = "",
                 retries: int = 1, quick_wait: float = 5.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.cookies = cookies
        self.retries = max(0, int(retries))
        self.quick_wait = max(0.0, float(quick_wait))

    def __call__(self, prompt: str):
        attempt = 0
        while True:
            events, elapsed, retry_after = self._once(prompt)
            rate_limited = retry_after is not None or is_rate_limited(events)
            if not rate_limited or attempt >= self.retries:
                return events, elapsed
            pause = 30.0 if retry_after is None else retry_after
            if pause > self.quick_wait:
                return events, elapsed          # ждать дольше, чем стоит замер
            if pause > 0:
                time.sleep(pause)
            attempt += 1

    def _once(self, prompt: str):
        """Один запрос. Возвращает (события, время, пауза_до_повтора).

        Пауза ``None`` — ответ не упёрся в лимит; ``0.0`` — упёрся, но сервер
        разрешил повторять сразу.
        """
        import requests
        headers = {"Content-Type": "application/json", "Accept": "application/x-ndjson"}
        if self.cookies:
            headers["Cookie"] = self.cookies
        started = time.time()
        response = requests.post(f"{self.base_url}/api/agent/stream",
                                 json={"message": prompt}, headers=headers,
                                 stream=True, timeout=self.timeout)
        retry_after = None
        if response.status_code == 429:
            retry_after = 30.0
            raw = response.headers.get("Retry-After")
            if raw is not None:
                try:
                    retry_after = max(0.0, float(raw))
                except (TypeError, ValueError):
                    retry_after = 30.0
        if response.status_code >= 400:
            try:
                payload = response.json()
            except ValueError:
                payload = {"error": response.text[:200]}
            return [{"type": "error", "text": str(payload.get("error") or response.status_code)}], \
                time.time() - started, retry_after
        lines = response.iter_lines(decode_unicode=True)
        events = parse_events(line for line in lines if line)
        return events, time.time() - started, retry_after


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
            "tools": summary["tools"], "answer": summary["answer"][:500],
            "offline": bool(summary.get("offline")),
            "provider": summary.get("provider", ""),
            "tool_hallucinations": summary.get("tool_hallucinations", 0),
            "tool_hallucination_rate": summary.get("tool_hallucination_rate", 0),
            "bash_repeats_after_error": summary.get("bash_repeats_after_error", 0),
            "false_done": summary.get("false_done", 0),
            "unauthorized_actions": summary.get("unauthorized_actions", 0),
            "confirmed_success": summary.get("confirmed_success", False),
            "verified": summary.get("verified"),
            "rate_limited": is_rate_limited(events)}


def run_all(tasks, transport):
    """Прогон по всем задачам.

    Упираемся в лимит запросов — останавливаемся: продолжение бессмысленно,
    сервер всё равно отвечает 429, а отчёт из тридцати «слишком много
    запросов» выглядит как регрессия, хотя модель тут ни при чём.
    """
    results = []
    for task in tasks:
        result = run_task(task, transport)
        results.append(result)
        if result.get("rate_limited"):
            break
    return results


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
    if results and results[-1].get("rate_limited"):
        print(RATE_LIMIT_HINT)
        print(f"Пройдено до остановки: {len(results)} из {len(tasks)} задач.")
        if json_path:
            print("Отчёт не сохранён: неполный прогон нельзя сравнивать с базовой линией.")
        return 3
    print(summary_line(results))
    if json_path:
        with open(json_path, "w", encoding="utf-8") as handle:
            json.dump(results, handle, ensure_ascii=False, indent=2)
        print(f"Отчёт: {json_path}")
    return 0 if all(result["ok"] for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
