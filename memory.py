"""Долгосрочная память NovaMind (этап 4).

Что это: несколько устойчивых фактов о пользователе (предпочтения, проект,
решения), которые агент вспоминает в начале каждого хода и может пополнять
сам. Память живёт в той же SQLite-базе, что и чаты, — никаких тяжёлых
зависимостей вроде faiss/chromadb, всё работает в Termux.

Правила:
  * гостям память недоступна;
  * сохраняются только устойчивые факты и явные просьбы «запомни»;
  * пароли, токены и ключи не сохраняются никогда (фильтр is_secret);
  * в промпт попадает максимум 5 записей и не больше MEMORY_PROMPT_LIMIT
    символов, чтобы не раздувать запрос к модели.
"""
from __future__ import annotations

import os
import re

import database

MEMORY_KINDS = ("fact", "preference", "project", "decision")
DEFAULT_KIND = "fact"
MEMORY_PROMPT_ITEMS = 5
MEMORY_PROMPT_LIMIT = 1200

# Шаблоны секретов: такие записи не сохраняем.
_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(пароль|passw(or)?d|passcode|пин[- ]?код|pin)\b\s*[:=—-]?\s*\S+"),
    re.compile(r"(?i)\b(токен|token|api[-_ ]?key|apikey|secret|client[-_ ]?secret|"
               r"приватн\w*\s+ключ|seed[- ]?phrase|мнемоник\w*)\b\s*[:=—-]?\s*\S+"),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{12,}"),                       # OpenAI/Groq
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}"),                   # GitHub
    re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}"),                # Slack
    re.compile(r"\b\d{4}[- ]?\d{4}[- ]?\d{4}[- ]?\d{4}\b"),        # номер карты
    re.compile(r"\b[A-Fa-f0-9]{32,}\b"),                           # длинный hex-ключ
    re.compile(r"\bB[0-9A-Za-z]{9,}\b"),                          # токен бота/сессия
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)


def memory_enabled() -> bool:
    return os.getenv("MEMORY_ENABLED", "1") == "1"


def is_secret(text: str) -> bool:
    """Похоже ли содержимое на пароль/токен/ключ."""
    value = str(text or "")
    if not value.strip():
        return False
    return any(pattern.search(value) for pattern in _SECRET_PATTERNS)


def user_key(session, signed_in) -> str:
    """Return a stable, non-colliding key for this authenticated identity."""
    if not memory_enabled() or not signed_in:
        return ""
    email = str(session.get("nova_user_email") or session.get("user_email") or session.get("email") or "").strip().lower()
    if email and session.get("nova_google_login"):
        return ("email:" + email)[:180]
    if session.get("admin_logged_in"):
        return ("admin:" + str(session.get("admin_username") or "admin").strip().lower())[:180]
    nick = str(session.get("nova_user_nick") or session.get("username") or "").strip().lower()
    if nick:
        import identity
        return ("nick:" + nick + ":" + identity.browser_id(session))[:180]
    key = str(session.get("agent_memory_key") or "").strip()
    return ("session:" + key)[:180] if key else ""


def remember(key, text, kind=DEFAULT_KIND, tags="", importance=1):
    """Сохраняет воспоминание. Возвращает запись или бросает ValueError."""
    if not key:
        raise ValueError("Память доступна только вошедшим пользователям")
    value = str(text or "").strip()
    if len(value) < 3:
        raise ValueError("Слишком короткий текст для памяти")
    if is_secret(value):
        raise ValueError("Похоже на пароль или ключ — такое не запоминаю")
    kind = str(kind or DEFAULT_KIND).strip().lower()
    if kind not in MEMORY_KINDS:
        kind = DEFAULT_KIND
    if isinstance(tags, (list, tuple)):
        tags = ", ".join(str(tag) for tag in tags)
    try:
        importance = max(1, min(5, int(importance or 1)))
    except (TypeError, ValueError):
        importance = 1
    return database.add_memory(key, value[:2000], kind, str(tags or "")[:300], importance)


def recall(key, query="", limit=MEMORY_PROMPT_ITEMS):
    """Находит релевантные воспоминания (по-русски — подстрокой)."""
    if not key:
        return []
    items = database.search_memory(key, str(query or ""), limit=limit)
    ids = [item["id"] for item in items]
    if ids:
        database.touch_memory(ids)
    return items


def forget(key, memory_id):
    """Удаляет своё воспоминание."""
    if not key:
        raise ValueError("Память доступна только вошедшим пользователям")
    return database.delete_memory(key, str(memory_id))


def prompt_block(key, query="", limit=MEMORY_PROMPT_ITEMS,
                 max_chars=MEMORY_PROMPT_LIMIT) -> str:
    """Компактный блок MEMORY для системного промпта (≤ max_chars)."""
    if not key:
        return ""
    items = recall(key, query, limit=limit)
    if not items:
        return ""
    lines = []
    used = 0
    for item in items:
        kind = item.get("kind") or "fact"
        text = str(item.get("text") or "").strip()
        line = f"- ({kind}) {text}"
        if used + len(line) + 1 > max_chars:
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines)


def list_all(key, query="", limit=100):
    if not key:
        return []
    return database.search_memory(key, str(query or ""), limit=limit)


def delete(key, memory_id):
    return forget(key, memory_id)
