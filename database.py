"""
database.py — SQLite база данных для истории чатов
"""
import os
import time
import json
import uuid
import sqlite3
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
    """Возвращает историю сообщений чата."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT role, content FROM messages WHERE chat_id=? ORDER BY created_at ASC LIMIT ?",
            (chat_id, limit)
        ).fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in rows]

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
    """Удаляет чат и все его сообщения."""
    with _db_lock:
        with get_db() as conn:
            conn.execute("DELETE FROM messages WHERE chat_id=?", (chat_id,))
            conn.execute("DELETE FROM chats WHERE id=?", (chat_id,))

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

# ---------- Админ ----------

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
