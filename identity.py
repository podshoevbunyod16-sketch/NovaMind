"""identity.py - кто этот пользователь.

Один источник правды для изоляции данных: владелец чатов и задач, ключ памяти
и личная рабочая папка выводятся из одной строки-владельца.

    email:<почта>   вход через Google (проверенная личность)
    admin:<имя>     администратор
    anon:<id>       всё остальное, включая вход "по нику": ник вводит кто угодно,
                    поэтому он НЕ является доказательством. Опора - случайный id
                    браузера в подписанной cookie.
"""
import contextvars
import hashlib
import os
import uuid

from flask import has_request_context, session

# Владелец для кода без запроса: потоки агента и планировщика.
# Движок копирует контекст при старте потока (contextvars.copy_context).
_OWNER = contextvars.ContextVar("nova_owner", default="")


def bind_owner(owner):
    return _OWNER.set(owner or "")


def browser_id(sess=None):
    """Случайный id браузера. Лежит в подписанной cookie - подделать нельзя."""
    s = session if sess is None else sess
    sid = s.get("nova_sid")
    if not sid:
        sid = uuid.uuid4().hex
        s["nova_sid"] = sid
    try:
        s.permanent = True
    except Exception:
        pass
    return sid


def chat_owner(sess=None):
    """Строка-владелец текущего пользователя ("" - нет ни запроса, ни привязки)."""
    if sess is None:
        if not has_request_context():
            return _OWNER.get()
        sess = session
    email = str(sess.get("nova_user_email") or "").strip().lower()
    if email and sess.get("nova_google_login"):
        return ("email:" + email)[:200]
    if sess.get("admin_logged_in"):
        name = str(sess.get("admin_username") or "admin").strip().lower()
        return ("admin:" + name)[:200]
    return "anon:" + browser_id(sess)


def is_admin_owner(owner):
    return str(owner or "").startswith("admin:")


def workspace_for(owner, base):
    """Рабочая папка владельца. Админ и внутренние запуски - общий корень
    (старые файлы остаются на месте), остальные - base/users/<хэш>."""
    if not owner or is_admin_owner(owner):
        return base
    digest = hashlib.sha256(owner.encode("utf-8")).hexdigest()[:20]
    return os.path.realpath(os.path.join(base, "users", digest))


def current_workspace(base):
    owner = chat_owner()
    return workspace_for(owner, base) if owner else base
