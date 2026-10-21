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


login_limiter = LoginLimiter()
agent_limiter = RateLimiter()

# Внутренние запуски агента (планировщик, Telegram-бот) идут в том же
# процессе и не должны съедать лимит пользователя. Отличаем их секретным
# заголовком: снаружи такой токен не подделать, он живёт только в памяти.
INTERNAL_RUN_TOKEN = secrets.token_hex(16)


def is_internal_run(request) -> bool:
    """Запрос сделан нашим же процессом (планировщик, бот), а не браузером."""
    return (request.headers.get("X-Internal-Token") or "") == INTERNAL_RUN_TOKEN


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
