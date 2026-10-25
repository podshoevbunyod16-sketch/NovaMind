"""security.py — ограничения частоты и защита от перебора (этап 7).

Два независимых механизма, оба в памяти процесса (Termux, один сервер):

  * LoginLimiter — до 5 неудачных попыток входа в админку за 10 минут
    отдельно по IP и по имени аккаунта, затем блок до конца окна;
  * RateLimiter — лимит запросов к агенту: 30/час гостю, 200/час
    вошедшему (настраивается в .env).

Зависимостей нет: только stdlib. Память ограничена по числу ключей, чтобы
долгий аптайм не съедал RAM.
"""
from __future__ import annotations

import os
import secrets
import threading
import time
from collections import deque

from flask import session

import config

MAX_KEYS = 4096


def _env_int(name, default):
    try:
        return int(str(os.getenv(name, default)).strip())
    except (TypeError, ValueError):
        return int(default)


def client_ip(request) -> str:
    """IP клиента с учётом обратного прокси (X-Forwarded-For)."""
    forwarded = (request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
    return forwarded or (request.remote_addr or "unknown")


class _SlidingWindow:
    """Общее хранилище: ключ → отметки времени событий."""

    def __init__(self, max_keys=MAX_KEYS):
        self._events: dict[str, deque] = {}
        self._lock = threading.Lock()
        self._max_keys = max_keys

    def _trim(self, key, window):
        events = self._events.get(key)
        if events is None:
            return None
        edge = time.time() - window
        while events and events[0] < edge:
            events.popleft()
        return events

    def hit(self, key, limit, window):
        """Регистрирует событие. Возвращает (allowed, retry_after, remaining)."""
        now = time.time()
        with self._lock:
            events = self._trim(key, window) or deque()
            self._events[key] = events
            if len(events) >= limit:
                retry_after = max(1, int(events[0] + window - now) + 1)
                return False, retry_after, 0
            events.append(now)
            if len(self._events) > self._max_keys:
                # выкидываем самый старый ключ
                oldest = min(self._events, key=lambda item: self._events[item][0]
                             if self._events[item] else 0)
                self._events.pop(oldest, None)
            return True, 0, max(0, limit - len(events))

    def peek(self, key, limit, window):
        """Проверка без регистрации события."""
        with self._lock:
            events = self._trim(key, window) or deque()
            if len(events) >= limit:
                retry_after = max(1, int(events[0] + window - time.time()) + 1)
                return False, retry_after, 0
            return True, 0, max(0, limit - len(events))

    def reset(self, key):
        with self._lock:
            self._events.pop(key, None)

    def clear(self):
        with self._lock:
            self._events.clear()


class LoginLimiter(_SlidingWindow):
    """Неудачные попытки входа: блок по IP и по аккаунту."""

    def max_attempts(self):
        return _env_int("ADMIN_LOGIN_MAX_ATTEMPTS", 5)

    def window(self):
        return _env_int("ADMIN_LOGIN_WINDOW", 600)

    def blocked(self, *keys):
        """Проверяет ключи; возвращает (allowed, retry_after, blocked_key)."""
        for key in keys:
            allowed, retry_after, _ = self.peek(key, self.max_attempts(), self.window())
            if not allowed:
                return False, retry_after, key
        return True, 0, ""

    def fail(self, *keys):
        for key in keys:
            self.hit(key, self.max_attempts(), self.window())

    def succeed(self, *keys):
        for key in keys:
            self.reset(key)


class RateLimiter(_SlidingWindow):
    """Общий лимит запросов (например, /api/agent/stream)."""

    def limit_for(self, signed_in: bool) -> int:
        return _env_int("AGENT_RATE_USER" if signed_in else "AGENT_RATE_GUEST",
                        200 if signed_in else 30)

    def window(self) -> int:
        return _env_int("AGENT_RATE_WINDOW", 3600)


class TTSRateLimiter(RateLimiter):
    """Отдельный лимит на озвучку: TTS дороже обычного текста у провайдеров."""

    def limit_for(self, signed_in: bool) -> int:
        return _env_int("TTS_RATE_USER" if signed_in else "TTS_RATE_GUEST",
                        120 if signed_in else 30)

    def window(self) -> int:
        return _env_int("TTS_RATE_WINDOW", 3600)


login_limiter = LoginLimiter()
agent_limiter = RateLimiter()
tts_limiter = TTSRateLimiter()

# Внутренние запуски агента (планировщик, Telegram-бот) идут в том же
# процессе и не должны съедать лимит пользователя. Отличаем их секретным
# заголовком: снаружи такой токен не подделать, он живёт только в памяти.
INTERNAL_RUN_TOKEN = secrets.token_hex(16)


def is_internal_run(request) -> bool:
    """Запрос сделан нашим же процессом (планировщик, бот), а не браузером."""
    return (request.headers.get("X-Internal-Token") or "") == INTERNAL_RUN_TOKEN


def tts_rate_check(request, signed_in: bool, key_suffix: str = ""):
    """Лимит запросов к озвучке. Возвращает (allowed, retry_after, limit, remaining)."""
    if os.getenv("TTS_RATE_LIMIT", "1") != "1":
        return True, 0, 0, 0
    limit = tts_limiter.limit_for(signed_in)
    window = tts_limiter.window()
    if limit <= 0:
        return True, 0, 0, 0
    scope = f"tts:user:{key_suffix}" if signed_in and key_suffix else f"tts:ip:{client_ip(request)}"
    allowed, retry_after, remaining = tts_limiter.hit(scope, limit, window)
    return allowed, retry_after, limit, remaining


def agent_rate_check(request, signed_in: bool, key_suffix: str = ""):
    """Проверяет лимит агента и регистрирует запрос.

    Возвращает (allowed, retry_after, limit, remaining, scope).
    """
    if os.getenv("AGENT_RATE_LIMIT", "1") != "1":
        return True, 0, 0, 0, ""
    limit = agent_limiter.limit_for(signed_in)
    window = agent_limiter.window()
    if limit <= 0:                       # 0 — без ограничений
        return True, 0, limit, 0, ""
    scope = f"user:{key_suffix}" if signed_in and key_suffix else f"ip:{client_ip(request)}"
    allowed, retry_after, remaining = agent_limiter.hit(scope, limit, window)
    return allowed, retry_after, limit, remaining, scope


def same_secret(expected, given) -> bool:
    """Сравнение кода администратора за постоянное время.

    ``secrets.compare_digest`` не принимает строки с не-ASCII символами —
    код из кириллицы или эмодзи ронял вход с TypeError. Сравниваем байты.
    """
    left = str(expected or "").encode("utf-8", "replace")
    right = str(given or "").encode("utf-8", "replace")
    return secrets.compare_digest(left, right)


# ── кто администратор ────────────────────────────────────────────────────
#: Поля сессии, в которых лежит личность вошедшего человека.
IDENTITY_KEYS = ("nova_user_email", "user_email", "nova_user_nick", "login", "username")


def normalize_identity(value) -> str:
    """Логин/почта в одном виде: без пробелов, кавычек и регистра."""
    return config.clean_env_value(value).casefold()


def admin_logins() -> set[str]:
    """Все логины администратора: ADMIN_USER и необязательный ADMIN_EMAIL."""
    stored = getattr(config, "ADMIN_LOGINS", None)
    if stored:
        return {normalize_identity(item) for item in stored if item}
    return {normalize_identity(item) for item in
            (getattr(config, "ADMIN_USER", ""), getattr(config, "ADMIN_EMAIL", "")) if item}


def is_admin_identity(identity) -> bool:
    """Совпадает ли логин/почта с администратором (без учёта регистра)."""
    normalized = normalize_identity(identity)
    return bool(normalized) and normalized in admin_logins()


def session_identities() -> list[str]:
    """Кем вошёл человек — все известные поля сессии."""
    found = []
    for key in IDENTITY_KEYS:
        try:
            value = session.get(key)
        except RuntimeError:          # вне контекста запроса
            return found
        if value:
            found.append(str(value))
    return found


def is_admin() -> bool:
    """Администратор ли текущая сессия.

    Админом считается либо тот, кто ввёл код на странице входа, либо человек,
    который вошёл своим обычным логином/Google-почтой, совпадающей с
    ``ADMIN_USER``/``ADMIN_EMAIL``. Раньше второй случай не учитывался: человек
    со своим e-mail в ADMIN_USER входил через Google как обычный пользователь
    и не видел ни настроек, ни MCP.
    """
    try:
        if session.get("admin_logged_in"):
            return True
    except RuntimeError:
        return False
    return any(is_admin_identity(identity) for identity in session_identities())


def grant_admin(identity: str = "", via: str = "") -> bool:
    """Выдать права администратора, если личность совпадает с настройками."""
    if not is_admin_identity(identity):
        return False
    try:
        session["admin_logged_in"] = True
        session["admin_username"] = session.get("admin_username") or str(identity)
        session["admin_via"] = via or "identity"
        session["nova_is_admin"] = True
    except RuntimeError:
        return False
    return True


def admin_state() -> dict:
    """Короткий ответ интерфейсу: админ ли я и что делать, если нет.

    Логин администратора отдаём только вошедшему человеку: гостю он не нужен,
    а лишняя подсказка «вот чей это сервер» снаружи не нужна вовсе.
    """
    admin = is_admin()
    login = getattr(config, "ADMIN_USER", "admin")
    signed = bool(session_identities()) or admin
    return {
        "admin": admin,
        "admin_via": session.get("admin_via", "") if admin else "",
        "admin_login": login if signed else "",
        "admin_login_is_email": "@" in login,
    }
