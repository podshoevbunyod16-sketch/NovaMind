"""scheduler.py — задачи по расписанию (бонус Б4).

Пользователь говорит «каждое утро в 9 присылай сводку», агент вызывает
инструмент `schedule`, и задание ложится в таблицу `scheduled_jobs`.
Фоновый поток раз в полминуты проверяет, кому пора, запускает того же
агента и складывает результат в отдельный чат «⏰ Расписание».

Никаких cron-демонов и зависимостей: разбор crontab свой, на stdlib.
Поддерживается классический формат из пяти полей:

    минута час день-месяца месяц день-недели
    */15 * * * *      каждые 15 минут
    0 9 * * 1-5       будни в 9:00
    30 18 1 * *       первое число в 18:30
    0 8 * * MON       по понедельникам в 8:00

Вместо cron можно задать интервал: `every: 30m`, `every: 2h`, `every: 1d`.
"""
from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from datetime import datetime, time as dt_time, timedelta

import database

log = logging.getLogger("novamind.scheduler")

MAX_LOOKAHEAD_DAYS = 400
TICK_SECONDS = int(os.getenv("SCHEDULER_TICK", "50"))
MIN_EVERY = 60                      # чаще раза в минуту не запускаем

_MONTHS = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
           "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}
_WEEKDAYS = {"SUN": 0, "MON": 1, "TUE": 2, "WED": 3, "THU": 4, "FRI": 5, "SAT": 6}

_thread = None
_stop_event = threading.Event()
_app = None


# ══════════════ интервалы ══════════════

def parse_every(text) -> int:
    """'30m' / '2h' / '1d' / '90' → секунды."""
    value = str(text or "").strip().lower()
    if not value:
        raise ValueError("Пустой интервал")
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    number, unit = value, "m"
    if value[-1] in units:
        number, unit = value[:-1], value[-1]
    try:
        amount = float(number)
    except ValueError:
        raise ValueError(f"Не понял интервал {text!r}. Примеры: 30m, 2h, 1d") from None
    seconds = int(amount * units[unit])
    if seconds < MIN_EVERY:
        raise ValueError("Слишком частый запуск: минимум одна минута")
    return seconds


# ══════════════ cron ══════════════

def _cron_number(token, aliases):
    token = token.strip().upper()
    if token in aliases:
        return aliases[token]
    try:
        return int(token)
    except ValueError:
        raise ValueError(f"Не число в cron-выражении: {token!r}") from None


def parse_cron_field(field, low, high, aliases=None, wrap=0):
    """'*/15' → {0,15,30,45}; '1-5' → {1,2,3,4,5}; '1,3' → {1,3}."""
    aliases = aliases or {}
    values = set()
    field = str(field or "").strip()
    if not field:
        raise ValueError("Пустое поле cron")
    for part in field.split(","):
        part = part.strip()
        if not part:
            raise ValueError("Пустое поле cron")
        step = 1
        if "/" in part:
            part, _, step_text = part.partition("/")
            try:
                step = int(step_text)
            except ValueError:
                raise ValueError(f"Неверный шаг cron: {step_text!r}") from None
            if step <= 0:
                raise ValueError("Шаг cron должен быть положительным")
        if part in ("*", ""):
            start, end = low, high
        elif "-" in part:
            left, _, right = part.partition("-")
            start = _cron_number(left, aliases)
            end = _cron_number(right, aliases)
        else:
            start = end = _cron_number(part, aliases)
        if wrap:                             # 7 = воскресенье наравне с 0
            start = low if start == wrap else start
            end = low if end == wrap else end
        if start < low or start > high or end < low or end > high:
            raise ValueError(f"Значение {start}-{end} вне диапазона {low}-{high}")
        if start <= end:
            values.update(range(start, end + 1, step))
        else:                                # диапазон через границу, 5-0
            values.update(range(start, high + 1, step))
            values.update(range(low, end + 1, step))
    return values


def parse_cron(expr):
    """Возвращает (минуты, часы, дни, месяцы, дни недели)."""
    fields = str(expr or "").split()
    if len(fields) != 5:
        raise ValueError("Cron должен состоять из 5 полей: минута час день месяц день_недели")
    return (
        parse_cron_field(fields[0], 0, 59),
        parse_cron_field(fields[1], 0, 23),
        parse_cron_field(fields[2], 1, 31),
        parse_cron_field(fields[3], 1, 12, _MONTHS),
        parse_cron_field(fields[4], 0, 6, _WEEKDAYS, wrap=7),
    )


def validate(expr) -> bool:
    """Проверка выражения без исключений (для инструмента агента)."""
    try:
        parse_cron(expr)
        return True
    except ValueError:
        return False


def cron_matches(expr, moment) -> bool:
    """Подходит ли момент под cron-выражение."""
    minutes, hours, days, months, weekdays = parse_cron(expr)
    cron_weekday = (moment.weekday() + 1) % 7          # Mon=1 … Sun=0
    return (moment.minute in minutes and moment.hour in hours
            and moment.day in days and moment.month in months
            and cron_weekday in weekdays)


def next_cron_time(expr, from_ts=None) -> float:
    """Ближайший запуск по cron после from_ts (метка времени)."""
    minutes, hours, days, months, weekdays = parse_cron(expr)
    start = datetime.fromtimestamp(from_ts or time.time()).replace(second=0, microsecond=0)
    start += timedelta(minutes=1)
    day = start.date()
    for _ in range(MAX_LOOKAHEAD_DAYS):
        cron_weekday = (day.weekday() + 1) % 7
        if day.day in days and day.month in months and cron_weekday in weekdays:
            for hour in sorted(hours):
                for minute in sorted(minutes):
                    candidate = datetime.combine(day, dt_time(hour, minute))
                    if candidate >= start:
                        return candidate.timestamp()
        day += timedelta(days=1)
    raise ValueError("Не удалось найти следующий запуск в пределах года")


def next_run_for(kind, expr, from_ts=None) -> float:
    """Следующая метка запуска для задания любого типа."""
    now = from_ts or time.time()
    if kind == "cron":
        return next_cron_time(expr, now)
    return now + parse_every(expr)


# ══════════════ задания ══════════════

def create_job(owner, prompt, kind, expr, chat_id="", owner_admin=False):
    """Создаёт задание и считает первый запуск. Ошибки — ValueError."""
    prompt = str(prompt or "").strip()
    if not prompt:
        raise ValueError("Пустой запрос задания")
    kind = str(kind or "every").strip().lower()
    if kind not in ("cron", "every"):
        raise ValueError("Тип расписания: cron или every")
    if kind == "cron":
        parse_cron(expr)
    else:
        parse_every(expr)
    result_chat_id = database.create_chat(_result_chat_title(owner))
    job = {
        "id": uuid.uuid4().hex[:12],
        "owner": owner or "",
        "owner_admin": bool(owner_admin),
        "prompt": prompt[:2000],
        "kind": kind,
        "expr": str(expr).strip(),
        "chat_id": chat_id or "",
        "result_chat_id": result_chat_id,
        "next_run_at": next_run_for(kind, expr),
        "status": "active",
    }
    return database.add_job(job)


def _result_chat_title(owner) -> str:
    """Один отдельный чат на владельца: результаты не мешают разговору."""
    return f"⏰ Расписание · {owner}" if owner else "⏰ Расписание"


def result_chat_for(owner) -> str:
    """Отдельный чат для результатов (или новый, если ещё нет)."""
    title = _result_chat_title(owner)
    try:
        for chat in database.list_chats(limit=200):
            if chat.get("title") == title:
                return chat["id"]
    except Exception:
        pass
    return database.create_chat(title)


def describe(job) -> str:
    """Человеческое описание расписания."""
    if job.get("kind") == "cron":
        return f"cron «{job.get('expr')}»"
    return f"каждые {job.get('expr')}"


# ══════════════ выполнение ══════════════

def _run_agent_prompt(job):
    """Запускает того же агента внутри процесса (без HTTP и без сети).

    Сессия выставляется по владельцу задания: администратор получает
    доступ к рабочей папке, остальные — как обычный вошедший пользователь.
    """
    from flask import session
    from app import app
    import routes.agent as agent_routes

    result = {"reply": "", "error": "", "saved": True}
    import security
    with app.test_request_context(
            "/api/agent/stream", method="POST", json={"message": job["prompt"]},
            headers={"X-Internal-Token": security.INTERNAL_RUN_TOKEN}):
        session["chat_id"] = job["result_chat_id"]
        if job.get("owner"):
            session["nova_user_nick"] = job["owner"]
            session["nova_signed_in"] = True
        if job.get("owner_admin"):
            session["admin_logged_in"] = True
            session["admin_username"] = "admin"
        response = agent_routes.agent_stream()
        for raw in response.response:
            try:
                import json
                event = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if event.get("type") == "result":
                result["reply"] = str(event.get("reply") or "")
            elif event.get("type") == "error":
                result["error"] = str(event.get("text") or "")
    return result


def run_job(job, runner=None):
    """Выполняет одно задание и обновляет его состояние."""
    runner = runner or _run_agent_prompt
    outcome = runner(job) or {}
    reply = str(outcome.get("reply") or "").strip()
    error = str(outcome.get("error") or "").strip()
    now = time.time()

    if reply and not outcome.get("saved", True):
        # Свой раннер (тесты, внешний вызов) сообщения не сохраняет.
        database.add_message(job["result_chat_id"], "user", job["prompt"])
        database.add_message(job["result_chat_id"], "assistant", reply)
    elif error and not outcome.get("saved", True):
        database.add_message(job["result_chat_id"], "user", job["prompt"])

    try:
        following = next_run_for(job["kind"], job["expr"], now)
        status = "active"
    except ValueError:
        following = now + 3600
        status = "error"
    database.update_job(
        job["id"], last_run_at=now, next_run_at=following,
        runs=int(job.get("runs") or 0) + 1, status=status,
        last_result=(reply or error)[:2000],
    )
    log.info("[schedule] задание %s выполнено (ответ %s симв.)", job["id"], len(reply))
    return {"id": job["id"], "reply": reply, "error": error}


def tick(runner=None, now=None):
    """Один проход планировщика: выполняет всё, чему пора."""
    jobs = database.due_jobs(now or time.time(), limit=5)
    results = []
    for job in jobs:
        try:
            results.append(run_job(job, runner=runner))
        except Exception as exc:                     # одно задание не рушит остальные
            log.warning("[schedule] задание %s упало: %s", job.get("id"), exc, exc_info=True)
            try:
                database.update_job(job["id"], status="error", last_result=str(exc)[:500],
                                    last_run_at=time.time(),
                                    next_run_at=time.time() + 3600)
            except Exception:
                pass
    return results


# ══════════════ фоновый поток ══════════════

def _loop(app, interval):
    global _app
    _app = app
    while not _stop_event.wait(interval):
        try:
            with app.app_context():
                tick()
        except Exception:
            log.warning("[schedule] ошибка в фоновом цикле", exc_info=True)


def start(app=None, interval=None):
    """Запускает фоновый поток (повторный вызов ничего не делает)."""
    global _thread
    if _thread and _thread.is_alive():
        return False
    if app is None:
        from app import app as flask_app
        app = flask_app
    _stop_event.clear()
    _thread = threading.Thread(target=_loop, args=(app, interval or TICK_SECONDS),
                               name="novamind-scheduler", daemon=True)
    _thread.start()
    log.info("[schedule] планировщик запущен, шаг %s с", interval or TICK_SECONDS)
    return True


def stop():
    """Останавливает фоновый поток (для тестов и корректного выхода)."""
    global _thread
    _stop_event.set()
    if _thread and _thread.is_alive():
        _thread.join(timeout=2)
    _thread = None
