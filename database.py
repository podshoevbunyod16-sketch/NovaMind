"""
database.py — SQLite база данных для истории чатов
"""
import os
import time
import json
import uuid
import sqlite3
import re
import threading
from flask import session

DB_PATH = os.path.join(os.path.dirname(__file__), "novamind_chats.db")
_db_lock = threading.Lock()

def get_db():
    """Возвращает подключение к SQLite (thread-local)."""
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """Создаёт таблицы если не существуют."""
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS chats (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at REAL NOT NULL,
                FOREIGN KEY (chat_id) REFERENCES chats(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages(chat_id);
            CREATE INDEX IF NOT EXISTS idx_chats_updated ON chats(updated_at DESC);
        """)
    print("[DB] База данных чатов инициализирована:", DB_PATH)

init_db()

def create_chat(title="Новый чат"):
    """Создаёт новый чат, возвращает его id."""
    chat_id = str(uuid.uuid4())
    now = time.time()
    with _db_lock:
        with get_db() as conn:
            conn.execute(
                "INSERT INTO chats (id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (chat_id, title, now, now)
            )
    return chat_id

def get_chat_history(chat_id, limit=50):
    """Возвращает последние `limit` сообщений чата в хронологическом порядке.

    Раньше отдавались ПЕРВЫЕ сообщения — в длинном диалоге модель «забывала»
    свежий контекст. Теперь берём хвост и разворачиваем обратно.
    """
    with get_db() as conn:
        rows = conn.execute(
            "SELECT role, content FROM messages WHERE chat_id=? "
            "ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (chat_id, limit)
        ).fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]

def add_message(chat_id, role, content):
    """Добавляет сообщение в историю чата."""
    now = time.time()
    with _db_lock:
        with get_db() as conn:
            conn.execute(
                "INSERT INTO messages (chat_id, role, content, created_at) VALUES (?, ?, ?, ?)",
                (chat_id, role, content, now)
            )
            conn.execute(
                "UPDATE chats SET updated_at=? WHERE id=?",
                (now, chat_id)
            )
            # Авто-заголовок: первое сообщение пользователя
            row = conn.execute(
                "SELECT title FROM chats WHERE id=?", (chat_id,)
            ).fetchone()
            if row and row["title"] == "Новый чат" and role == "user":
                title = content[:60].replace("\n", " ").strip()
                conn.execute("UPDATE chats SET title=? WHERE id=?", (title, chat_id))

def list_chats(limit=50):
    """Возвращает список чатов (новые сначала)."""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT c.id, c.title, c.created_at, c.updated_at,
               COUNT(m.id) as msg_count
               FROM chats c LEFT JOIN messages m ON c.id=m.chat_id
               GROUP BY c.id ORDER BY c.updated_at DESC LIMIT ?""",
            (limit,)
        ).fetchall()
    return [dict(r) for r in rows]

def delete_chat(chat_id):
    """Удаляет чат, его сообщения и трассы шагов."""
    with _db_lock:
        with get_db() as conn:
            conn.execute("DELETE FROM messages WHERE chat_id=?", (chat_id,))
            conn.execute("DELETE FROM chats WHERE id=?", (chat_id,))
    try:
        clear_traces(chat_id)          # таблицы может ещё не быть — не страшно
    except sqlite3.Error:
        pass

def get_or_create_session_chat():
    """Получает или создаёт chat_id для текущей Flask-сессии."""
    if "chat_id" not in session:
        session["chat_id"] = create_chat()
    return session["chat_id"]

def trim_messages(chat_id, max_messages=100):
    """Оставляет только последние max_messages в чате."""
    with _db_lock:
        with get_db() as conn:
            total = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE chat_id=?", (chat_id,)
            ).fetchone()[0]
            if total > max_messages:
                conn.execute("""
                    DELETE FROM messages WHERE chat_id=? AND id NOT IN (
                        SELECT id FROM messages WHERE chat_id=?
                        ORDER BY created_at DESC LIMIT ?
                    )
                """, (chat_id, chat_id, max_messages))

# ══════════════════════════════════════════════════════════════
# ТРАССА ШАГОВ АГЕНТА (бонус Б2)
# «Что делал агент»: шаги и команды сохраняются рядом с чатом, чтобы
# панель можно было открыть после перезагрузки страницы.
# ══════════════════════════════════════════════════════════════

_traces_ready = set()


def init_traces():
    """Таблица трасс создаётся лениво — на старых базах её нет."""
    if DB_PATH in _traces_ready:
        return
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS step_traces (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT NOT NULL,
                run_id TEXT DEFAULT '',
                steps TEXT NOT NULL,
                created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_traces_chat ON step_traces(chat_id, created_at DESC);
        """)
    _traces_ready.add(DB_PATH)


def save_trace(chat_id, run_id, steps, limit_per_chat=100):
    """Сохраняет трассу хода агента. Пустую трассу не пишем."""
    if not chat_id or not steps:
        return 0
    init_traces()
    payload = json.dumps(steps, ensure_ascii=False)
    with _db_lock:
        with get_db() as conn:
            cursor = conn.execute(
                "INSERT INTO step_traces (chat_id, run_id, steps, created_at) VALUES (?, ?, ?, ?)",
                (chat_id, run_id or "", payload, time.time())
            )
            conn.execute("""
                DELETE FROM step_traces WHERE chat_id=? AND id NOT IN (
                    SELECT id FROM step_traces WHERE chat_id=?
                    ORDER BY created_at DESC, id DESC LIMIT ?
                )
            """, (chat_id, chat_id, limit_per_chat))
            return cursor.lastrowid


def get_traces(chat_id, limit=20):
    """Трассы чата (новые последними) — для панели «Что делал агент»."""
    if not chat_id:
        return []
    init_traces()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, run_id, steps, created_at FROM step_traces "
            "WHERE chat_id=? ORDER BY created_at DESC, id DESC LIMIT ?",
            (chat_id, limit)
        ).fetchall()
    traces = []
    for row in reversed(rows):
        try:
            steps = json.loads(row["steps"])
        except (TypeError, ValueError):
            steps = []
        traces.append({"id": row["id"], "run_id": row["run_id"],
                       "steps": steps, "created_at": row["created_at"]})
    return traces


def clear_traces(chat_id):
    """Удаляет трассы чата (вместе с чатом или по кнопке «очистить»)."""
    if not chat_id:
        return 0
    init_traces()
    with _db_lock:
        with get_db() as conn:
            cursor = conn.execute("DELETE FROM step_traces WHERE chat_id=?", (chat_id,))
            return cursor.rowcount


# ══════════════════════════════════════════════════════════════
# РАСХОД ТОКЕНОВ (бонус Б3)
# Одна строка — один ход агента: сколько ушло на промпт и на ответ.
# ══════════════════════════════════════════════════════════════

_usage_ready = set()


def init_usage():
    """Таблица расхода создаётся лениво — старые базы её не знают."""
    if DB_PATH in _usage_ready:
        return
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS token_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT DEFAULT '',
                provider TEXT DEFAULT '',
                model TEXT DEFAULT '',
                prompt_tokens INTEGER NOT NULL DEFAULT 0,
                completion_tokens INTEGER NOT NULL DEFAULT 0,
                total_tokens INTEGER NOT NULL DEFAULT 0,
                cost_usd REAL NOT NULL DEFAULT 0,
                created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_usage_chat ON token_usage(chat_id, created_at DESC);
        """)
    _usage_ready.add(DB_PATH)


def add_usage(chat_id, provider, model, prompt_tokens, completion_tokens, cost_usd=0.0):
    """Записывает расход одного хода и возвращает строку."""
    init_usage()
    prompt_tokens = max(0, int(prompt_tokens or 0))
    completion_tokens = max(0, int(completion_tokens or 0))
    total = prompt_tokens + completion_tokens
    now = time.time()
    with _db_lock:
        with get_db() as conn:
            conn.execute(
                "INSERT INTO token_usage (chat_id, provider, model, prompt_tokens, "
                "completion_tokens, total_tokens, cost_usd, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (chat_id or "", provider or "", model or "", prompt_tokens,
                 completion_tokens, total, float(cost_usd or 0.0), now)
            )
    return {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
            "total_tokens": total, "cost_usd": round(float(cost_usd or 0.0), 6),
            "provider": provider or "", "model": model or "", "created_at": now}


def usage_totals(chat_id=None):
    """Сумма по чату (или по всей базе, если chat_id не задан)."""
    init_usage()
    query = ("SELECT COUNT(*) AS turns, COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens, "
             "COALESCE(SUM(completion_tokens), 0) AS completion_tokens, "
             "COALESCE(SUM(total_tokens), 0) AS total_tokens, "
             "COALESCE(SUM(cost_usd), 0) AS cost_usd FROM token_usage")
    params = ()
    if chat_id:
        query += " WHERE chat_id=?"
        params = (chat_id,)
    with get_db() as conn:
        row = conn.execute(query, params).fetchone()
    return {"turns": int(row["turns"] or 0),
            "prompt_tokens": int(row["prompt_tokens"] or 0),
            "completion_tokens": int(row["completion_tokens"] or 0),
            "total_tokens": int(row["total_tokens"] or 0),
            "cost_usd": round(float(row["cost_usd"] or 0), 6)}


# ══════════════════════════════════════════════════════════════
# ЗАДАЧИ ПО РАСПИСАНИЮ (бонус Б4)
# ══════════════════════════════════════════════════════════════

_schedule_ready = set()


def init_schedule():
    """Таблица расписания создаётся лениво."""
    if DB_PATH in _schedule_ready:
        return
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS scheduled_jobs (
                id TEXT PRIMARY KEY,
                owner TEXT DEFAULT '',
                owner_admin INTEGER NOT NULL DEFAULT 0,
                prompt TEXT NOT NULL,
                kind TEXT NOT NULL DEFAULT 'every',      -- cron | every
                expr TEXT NOT NULL,
                chat_id TEXT DEFAULT '',
                result_chat_id TEXT DEFAULT '',
                next_run_at REAL NOT NULL,
                last_run_at REAL,
                runs INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'active',   -- active | paused | error
                last_result TEXT DEFAULT '',
                created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_jobs_due ON scheduled_jobs(status, next_run_at);
        """)
    _schedule_ready.add(DB_PATH)


def add_job(job):
    """Сохраняет задание по расписанию."""
    init_schedule()
    with _db_lock:
        with get_db() as conn:
            conn.execute(
                "INSERT INTO scheduled_jobs (id, owner, owner_admin, prompt, kind, expr, "
                "chat_id, result_chat_id, next_run_at, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (job["id"], job.get("owner", ""), 1 if job.get("owner_admin") else 0,
                 job["prompt"], job.get("kind", "every"), job["expr"],
                 job.get("chat_id", ""), job.get("result_chat_id", ""),
                 job["next_run_at"], job.get("status", "active"), time.time())
            )
    return get_job(job["id"])


def get_job(job_id):
    init_schedule()
    with get_db() as conn:
        row = conn.execute("SELECT * FROM scheduled_jobs WHERE id=?", (job_id,)).fetchone()
    return _job_dict(row) if row else None


def list_jobs(owner=None):
    init_schedule()
    with get_db() as conn:
        if owner:
            rows = conn.execute(
                "SELECT * FROM scheduled_jobs WHERE owner=? ORDER BY created_at DESC",
                (owner,)).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM scheduled_jobs ORDER BY created_at DESC").fetchall()
    return [_job_dict(row) for row in rows]


def due_jobs(now, limit=5):
    """Задания, которым пора выполняться."""
    init_schedule()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM scheduled_jobs WHERE status='active' AND next_run_at<=? "
            "ORDER BY next_run_at LIMIT ?", (now, limit)).fetchall()
    return [_job_dict(row) for row in rows]


def update_job(job_id, **fields):
    """Обновляет поля задания (next_run_at, runs, status, last_result…)."""
    if not fields:
        return get_job(job_id)
    init_schedule()
    allowed = {"prompt", "kind", "expr", "next_run_at", "last_run_at", "runs",
               "status", "last_result", "result_chat_id", "chat_id"}
    columns = [key for key in fields if key in allowed]
    if not columns:
        return get_job(job_id)
    assignments = ", ".join(f"{column}=?" for column in columns)
    values = [fields[column] for column in columns] + [job_id]
    with _db_lock:
        with get_db() as conn:
            conn.execute(f"UPDATE scheduled_jobs SET {assignments} WHERE id=?", values)
    return get_job(job_id)


def delete_job(job_id):
    init_schedule()
    with _db_lock:
        with get_db() as conn:
            cursor = conn.execute("DELETE FROM scheduled_jobs WHERE id=?", (job_id,))
            return cursor.rowcount


def _job_dict(row):
    job = dict(row)
    job["owner_admin"] = bool(job.get("owner_admin"))
    return job


# ---------- Админ ----------

# ══════════════════════════════════════════════════════════════
# ПАМЯТЬ (этап 4)
# Долгосрочные факты о пользователе: SQLite + FTS5, если он есть.
# В Termux FTS5 обычно собран в stdlib sqlite3; если нет — тот же поиск
# работает через LIKE, и ничего не падает.
# ══════════════════════════════════════════════════════════════

_memory_ready = set()
_fts_ready = {}


def _fts5_available(conn) -> bool:
    """Есть ли FTS5 в этом sqlite3 (проверяем один раз на базу)."""
    try:
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS _fts_probe USING fts5(x)")
        conn.execute("DROP TABLE IF EXISTS _fts_probe")
        return True
    except sqlite3.Error:
        return False


def init_memory():
    """Таблица памяти создаётся лениво, как и задачи."""
    if DB_PATH in _memory_ready:
        return
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS memory (
                id TEXT PRIMARY KEY,
                user_key TEXT NOT NULL,
                kind TEXT NOT NULL DEFAULT 'fact',
                text TEXT NOT NULL,
                tags TEXT DEFAULT '',
                importance INTEGER NOT NULL DEFAULT 1,
                created_at REAL NOT NULL,
                last_used_at REAL
            );
            CREATE INDEX IF NOT EXISTS idx_memory_user ON memory(user_key, created_at DESC);
        """)
        _fts_ready[DB_PATH] = _fts5_available(conn)
        if _fts_ready[DB_PATH]:
            conn.executescript("""
                CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
                    id UNINDEXED, user_key UNINDEXED, text, tags, tokenize='unicode61'
                );
            """)
    _memory_ready.add(DB_PATH)


def memory_fts5() -> bool:
    """Доступен ли полнотекстовый поиск (для /api/memory/status)."""
    init_memory()
    return bool(_fts_ready.get(DB_PATH))


def _memory_row(row):
    return {"id": row["id"], "kind": row["kind"], "text": row["text"],
            "tags": row["tags"] or "", "importance": int(row["importance"] or 1),
            "created_at": row["created_at"], "last_used_at": row["last_used_at"]}


def add_memory(user_key, text, kind="fact", tags="", importance=1):
    init_memory()
    memory_id = str(uuid.uuid4())
    now = time.time()
    with _db_lock:
        with get_db() as conn:
            conn.execute(
                "INSERT INTO memory (id, user_key, kind, text, tags, importance,"
                " created_at, last_used_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (memory_id, user_key, kind, text, tags, importance, now, None),
            )
            if _fts_ready.get(DB_PATH):
                try:
                    conn.execute("INSERT INTO memory_fts (id, user_key, text, tags)"
                                 " VALUES (?, ?, ?, ?)", (memory_id, user_key, text, tags))
                except sqlite3.Error:
                    pass
    return {"id": memory_id, "kind": kind, "text": text, "tags": tags or "",
            "importance": importance, "created_at": now, "last_used_at": None}


def _word_stems(word):
    """Основы слова для поиска по-русски: рыба → рыб, кофе → коф."""
    word = str(word or "")
    stems = {word}
    if len(word) >= 5:
        stems.add(word[:-1])
        stems.add(word[:-2])
    elif len(word) == 4:
        stems.add(word[:-1])
    return {stem for stem in stems if len(stem) >= 3} or {word}


def search_memory(user_key, query="", limit=5):
    """Поиск по памяти пользователя.

    Сначала — LIKE-подстроки (работает по-русски без морфологии: «рыба»
    находит «рыбу»), затем, если доступен FTS5, результаты дополняются
    полнотекстовыми совпадениями. Сортировка: релевантность, важность, свежесть.
    """
    init_memory()
    words = [w for w in re.findall(r"[\wёЁ-]+", str(query or "").lower()) if len(w) > 2][:8]
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM memory WHERE user_key=?", (user_key,)).fetchall()
        fts_ids = set()
        if words and _fts_ready.get(DB_PATH):
            try:
                match = " OR ".join(f'"{w}"*' for w in words)
                for fts_row in conn.execute(
                        "SELECT id FROM memory_fts WHERE memory_fts MATCH ? AND user_key=?",
                        (match, user_key)):
                    fts_ids.add(fts_row["id"])
            except sqlite3.Error:
                fts_ids = set()

    scored = []
    for row in rows:
        text = (row["text"] or "").lower()
        tags = (row["tags"] or "").lower()
        score = 0
        for word in words:
            # Русская морфология без стеммера: «рыба» должна находить «рыбу»,
            # поэтому кроме слова проверяем его укороченные основы.
            stems = _word_stems(word)
            if any(stem in text for stem in stems):
                score += 3
            if any(stem in tags for stem in stems):
                score += 2
        if row["id"] in fts_ids:
            score += 4
        if words and score == 0:
            continue
        scored.append((score, int(row["importance"] or 1), row["created_at"], row))

    if words and not scored:
        return []
    scored.sort(key=lambda item: (-item[0], -item[1], -item[2]))
    return [_memory_row(item[3]) for item in scored[:max(1, int(limit or 5))]]


def touch_memory(ids):
    """Отмечает, что воспоминания пригодились (last_used_at)."""
    if not ids:
        return
    init_memory()
    now = time.time()
    with _db_lock:
        with get_db() as conn:
            conn.executemany("UPDATE memory SET last_used_at=? WHERE id=?",
                             [(now, str(memory_id)) for memory_id in ids])


def delete_memory(user_key, memory_id):
    init_memory()
    with _db_lock:
        with get_db() as conn:
            cursor = conn.execute("DELETE FROM memory WHERE id=? AND user_key=?",
                                  (str(memory_id), user_key))
            removed = cursor.rowcount > 0
            if removed and _fts_ready.get(DB_PATH):
                try:
                    conn.execute("DELETE FROM memory_fts WHERE id=?", (str(memory_id),))
                except sqlite3.Error:
                    pass
    return removed


def count_memory(user_key):
    init_memory()
    with get_db() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM memory WHERE user_key=?",
                           (user_key,)).fetchone()
    return int(row["n"] if row else 0)


# ══════════════════════════════════════════════════════════════
# ЗАДАЧИ
# Список дел, который живёт рядом с чатами: их создаёт человек или
# агентный режим, у каждой задачи есть шаги и лог выполнения.
# ══════════════════════════════════════════════════════════════

_tasks_ready = set()

def init_tasks():
    """Таблица задач создаётся лениво — чтобы старый novamind_chats.db не ломался.

    Проверяем один раз на каждую базу: /api/tasks дёргается часто,
    а CREATE TABLE IF NOT EXISTS на каждый запрос не нужен.
    """
    if DB_PATH in _tasks_ready:
        return
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                detail TEXT DEFAULT '',
                status TEXT NOT NULL DEFAULT 'todo',      -- todo | doing | done
                source TEXT NOT NULL DEFAULT 'user',      -- user | agent
                steps TEXT NOT NULL DEFAULT '[]',         -- JSON: [{title, status, log, at}]
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_tasks_updated ON tasks(updated_at DESC);
        """)
    _tasks_ready.add(DB_PATH)

def _row_to_task(row):
    try:
        steps = json.loads(row["steps"] or "[]")
    except (TypeError, ValueError):
        steps = []
    data = dict(row)
    data["steps"] = steps if isinstance(steps, list) else []
    return data

def list_tasks(status=None, limit=100):
    init_tasks()
    with get_db() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE status=? ORDER BY updated_at DESC LIMIT ?",
                (status, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM tasks ORDER BY updated_at DESC LIMIT ?", (limit,)
            ).fetchall()
    return [_row_to_task(r) for r in rows]

def create_task(title, detail="", source="user", steps=None):
    init_tasks()
    task_id = str(uuid.uuid4())
    now = time.time()
    with _db_lock:
        with get_db() as conn:
            conn.execute(
                "INSERT INTO tasks (id, title, detail, status, source, steps, created_at, updated_at)"
                " VALUES (?, ?, ?, 'todo', ?, ?, ?, ?)",
                (task_id, title[:200], detail[:2000], source, json.dumps(steps or [], ensure_ascii=False), now, now),
            )
    return get_task(task_id)

def get_task(task_id):
    init_tasks()
    with get_db() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    return _row_to_task(row) if row else None

def update_task(task_id, **fields):
    """Обновляет перечисленные поля задачи (title, detail, status)."""
    init_tasks()
    allowed = {key: fields[key] for key in ("title", "detail", "status")
               if key in fields and fields[key] is not None}
    if not allowed:
        return get_task(task_id)
    sets = ", ".join(f"{key}=?" for key in allowed)
    with _db_lock:
        with get_db() as conn:
            conn.execute(f"UPDATE tasks SET {sets}, updated_at=? WHERE id=?",
                         (*allowed.values(), time.time(), task_id))
    return get_task(task_id)

def append_task_step(task_id, title, status="doing", log=""):
    """Дописывает шаг к задаче: агент так показывает, что именно он делает."""
    task = get_task(task_id)
    if not task:
        return None
    steps = task["steps"]
    steps.append({"title": title[:200], "status": status, "log": log[:4000], "at": time.time()})
    with _db_lock:
        with get_db() as conn:
            conn.execute("UPDATE tasks SET steps=?, updated_at=? WHERE id=?",
                         (json.dumps(steps[-40:], ensure_ascii=False), time.time(), task_id))
    return get_task(task_id)

def delete_task(task_id):
    init_tasks()
    with _db_lock:
        with get_db() as conn:
            conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))


# ══════════════════════════════════════════════════════════════
# ЗАПУСКИ АГЕНТА (движок AgenticSeek)
#
# Один ход агента = запись в agent_runs + шаги в agent_run_steps. Это
# отдельная сущность, а не часть истории сообщений: история чата не
# меняется, а панель «Что делал агент» может показать план, шаги и
# причину остановки даже после перезагрузки страницы.
# ══════════════════════════════════════════════════════════════

_runs_ready = set()


def init_agent_runs():
    """Создаёт таблицы запусков агента (лениво, как tasks/memory)."""
    if DB_PATH in _runs_ready:
        return
    with _db_lock:
        with get_db() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS agent_runs (
                    id TEXT PRIMARY KEY,
                    chat_id TEXT DEFAULT '',
                    run_id TEXT DEFAULT '',
                    goal TEXT NOT NULL DEFAULT '',
                    engine TEXT NOT NULL DEFAULT 'agenticseek',
                    status TEXT NOT NULL DEFAULT 'running',
                    route TEXT DEFAULT '',
                    provider TEXT DEFAULT '',
                    model TEXT DEFAULT '',
                    plan TEXT NOT NULL DEFAULT '[]',
                    steps INTEGER NOT NULL DEFAULT 0,
                    answer TEXT DEFAULT '',
                    error TEXT DEFAULT '',
                    started_at REAL NOT NULL,
                    finished_at REAL
                );
                CREATE INDEX IF NOT EXISTS idx_agent_runs_chat
                    ON agent_runs(chat_id, started_at DESC);
                CREATE INDEX IF NOT EXISTS idx_agent_runs_run ON agent_runs(run_id);
                CREATE TABLE IF NOT EXISTS agent_run_steps (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    tool TEXT DEFAULT '',
                    label TEXT DEFAULT '',
                    ok INTEGER NOT NULL DEFAULT 1,
                    output TEXT DEFAULT '',
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_agent_run_steps
                    ON agent_run_steps(run_id, seq);
            """)
            _ensure_agent_columns(conn)
    _runs_ready.add(DB_PATH)


def _ensure_agent_columns(conn):
    """Дописывает колонки, появившиеся после первого запуска (миграция).

    База пользователя живёт дольше кода: таблицы создаются через
    CREATE TABLE IF NOT EXISTS, поэтому новая колонка не появится сама.
    ALTER TABLE ADD COLUMN безопасен: существующие строки получают значение
    по умолчанию, данные не перезаписываются.
    """
    wanted = {
        "agent_runs": {
            "chat_id": "TEXT DEFAULT ''", "run_id": "TEXT DEFAULT ''",
            "route": "TEXT DEFAULT ''", "provider": "TEXT DEFAULT ''",
            "model": "TEXT DEFAULT ''", "plan": "TEXT NOT NULL DEFAULT '[]'",
            "steps": "INTEGER NOT NULL DEFAULT 0", "answer": "TEXT DEFAULT ''",
            "error": "TEXT DEFAULT ''", "finished_at": "REAL",
        },
        "agent_run_steps": {
            "label": "TEXT DEFAULT ''", "ok": "INTEGER NOT NULL DEFAULT 1",
            "output": "TEXT DEFAULT ''",
        },
    }
    for table, columns in wanted.items():
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, definition in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def create_agent_run(goal, chat_id="", run_id="", provider="", model="",
                     engine="agenticseek"):
    """Регистрирует запуск агента. Возвращает id записи ("" при ошибке)."""
    init_agent_runs()
    record_id = str(uuid.uuid4())
    with _db_lock:
        with get_db() as conn:
            conn.execute(
                "INSERT INTO agent_runs (id, chat_id, run_id, goal, engine, status,"
                " provider, model, plan, steps, answer, error, started_at, finished_at)"
                " VALUES (?, ?, ?, ?, ?, 'running', ?, ?, '[]', 0, '', '', ?, NULL)",
                (record_id, str(chat_id or ""), str(run_id or ""),
                 str(goal or "")[:4000], str(engine or "agenticseek"),
                 str(provider or ""), str(model or ""), time.time()),
            )
    return record_id


def update_agent_run(record_id, **fields):
    """Обновляет поля запуска: status, route, plan, steps, answer, error…"""
    if not record_id:
        return None
    init_agent_runs()
    allowed = ("status", "route", "plan", "steps", "answer", "error",
               "provider", "model", "finished_at", "chat_id")
    values = {key: fields[key] for key in allowed if key in fields}
    if not values:
        return get_agent_run(record_id)
    if "plan" in values and not isinstance(values["plan"], str):
        values["plan"] = json.dumps(values["plan"], ensure_ascii=False)[:20000]
    if "answer" in values:
        values["answer"] = str(values["answer"] or "")[:20000]
    if "error" in values:
        values["error"] = str(values["error"] or "")[:2000]
    sets = ", ".join(f"{key}=?" for key in values)
    with _db_lock:
        with get_db() as conn:
            conn.execute(f"UPDATE agent_runs SET {sets} WHERE id=?",
                         (*values.values(), record_id))
    return get_agent_run(record_id)


def add_agent_run_step(run_id, seq, tool, label="", ok=True, output=""):
    """Добавляет шаг запуска. run_id — agent_runs.id или agent_runs.run_id."""
    init_agent_runs()
    if not run_id:
        return None
    with _db_lock:
        with get_db() as conn:
            row = conn.execute(
                "SELECT id FROM agent_runs WHERE id=? OR run_id=?"
                " ORDER BY started_at DESC LIMIT 1", (str(run_id), str(run_id))).fetchone()
            if row is None:
                return None
            record_id = row["id"]
            conn.execute(
                "INSERT INTO agent_run_steps (run_id, seq, tool, label, ok, output,"
                " created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (record_id, int(seq or 1), str(tool or "")[:80], str(label or "")[:400],
                 1 if ok else 0, str(output or "")[:4000], time.time()),
            )
            conn.execute("UPDATE agent_runs SET steps=? WHERE id=?", (int(seq or 1), record_id))
    return seq


def get_agent_run(record_id):
    """Запуск + шаги. record_id — id записи или run_id."""
    if not record_id:
        return None
    init_agent_runs()
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM agent_runs WHERE id=? OR run_id=?"
            " ORDER BY started_at DESC LIMIT 1", (str(record_id), str(record_id))).fetchone()
        if row is None:
            return None
        steps = conn.execute(
            "SELECT seq, tool, label, ok, output, created_at FROM agent_run_steps"
            " WHERE run_id=? ORDER BY seq", (row["id"],)).fetchall()
    data = dict(row)
    try:
        data["plan"] = json.loads(data.get("plan") or "[]")
    except (TypeError, ValueError):
        data["plan"] = []
    data["steps_log"] = [dict(step) for step in steps]
    return data


def list_agent_runs(chat_id=None, limit=20):
    """Последние запуски агента (для панели «Что делал агент»)."""
    init_agent_runs()
    with get_db() as conn:
        if chat_id:
            rows = conn.execute(
                "SELECT id, chat_id, run_id, goal, status, route, provider, model,"
                " steps, answer, error, started_at, finished_at FROM agent_runs"
                " WHERE chat_id=? ORDER BY started_at DESC LIMIT ?",
                (str(chat_id), int(limit))).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, chat_id, run_id, goal, status, route, provider, model,"
                " steps, answer, error, started_at, finished_at FROM agent_runs"
                " ORDER BY started_at DESC LIMIT ?", (int(limit),)).fetchall()
    return [dict(row) for row in rows]


def finish_agent_run(record_id, status, answer="", error="", steps=0):
    """Закрывает запуск: статус, ответ, время окончания."""
    return update_agent_run(record_id, status=status, answer=answer, error=error,
                            steps=int(steps or 0), finished_at=time.time())
