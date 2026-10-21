"""telegram_bot.py — мост NovaMind в Telegram (бонус Б5).

Тот же агент, что и в браузере, но общение идёт через Telegram:

  * длинный polling (getUpdates) — никаких вебхуков и внешних адресов,
    поэтому работает и в Termux за NAT;
  * сообщение пользователя запускает обычный агентный ход, ответ
    приходит в чат; у каждого telegram-пользователя свой чат в базе,
    поэтому память и история не пересекаются;
  * опасные действия приходят карточкой с кнопками «Разрешить» /
    «Отклонить» (inline keyboard), решение уходит в то же подтверждение,
    которого ждёт агент.

Токен берётся ТОЛЬКО из окружения (.env): TELEGRAM_BOT_TOKEN.
В код, логи и сообщения он не попадает.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time

import requests

import database

log = logging.getLogger("novamind.telegram")

API_BASE = "https://api.telegram.org"
MAX_MESSAGE = 4000
POLL_TIMEOUT = int(os.getenv("TELEGRAM_POLL_TIMEOUT", "25"))
TRACE_LIMIT = 6

_stop_event = threading.Event()
_thread = None
_user_locks: dict = {}
_locks_guard = threading.Lock()


class TelegramError(RuntimeError):
    """Ошибка Telegram API (без токена в тексте)."""


# ══════════════ настройки ══════════════

def bot_token() -> str:
    """Токен только из окружения. Пустая строка — бот выключен."""
    return os.getenv("TELEGRAM_BOT_TOKEN", "").strip()


def enabled() -> bool:
    return bool(bot_token()) and os.getenv("TELEGRAM_ENABLED", "1") == "1"


def allowed_users() -> set:
    """TELEGRAM_ALLOWED_USERS=123,456 — белый список. Пусто — все."""
    raw = os.getenv("TELEGRAM_ALLOWED_USERS", "").strip()
    return {part.strip() for part in raw.split(",") if part.strip()}


def is_allowed(user_id) -> bool:
    allowed = allowed_users()
    return not allowed or str(user_id) in allowed


# ══════════════ HTTP ══════════════

def api_call(method, http_timeout=30, **params):
    """Вызов метода Telegram Bot API. Токен не попадает в сообщения об ошибках.

    `http_timeout` — таймаут HTTP, остальные именованные аргументы уходят
    в JSON как параметры метода (в том числе telegram-овский `timeout`).
    """
    token = bot_token()
    if not token:
        raise TelegramError("TELEGRAM_BOT_TOKEN не задан в .env")
    url = f"{API_BASE}/bot{token}/{method}"
    try:
        response = requests.post(url, json=params, timeout=http_timeout)
    except requests.exceptions.Timeout:
        raise TelegramError(f"Таймаут запроса {method}") from None
    except requests.exceptions.RequestException as error:
        raise TelegramError(f"Нет связи с Telegram: {error.__class__.__name__}") from None
    try:
        data = response.json()
    except ValueError:
        raise TelegramError(f"Telegram вернул не JSON ({method})") from None
    if not data.get("ok"):
        description = str(data.get("description") or "неизвестная ошибка")
        raise TelegramError(description.replace(token, "<token>"))
    return data.get("result")


def split_message(text, limit=MAX_MESSAGE):
    """Режет длинный ответ на куски по абзацам/строкам."""
    text = str(text or "").strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]
    chunks, current = [], ""
    for paragraph in text.split("\n"):
        piece = paragraph if not current else "\n" + paragraph
        if len(current) + len(piece) > limit:
            if current:
                chunks.append(current)
            while len(paragraph) > limit:          # очень длинная строка
                chunks.append(paragraph[:limit])
                paragraph = paragraph[limit:]
            current = paragraph
        else:
            current += piece
    if current:
        chunks.append(current)
    return chunks


def confirm_keyboard(confirm_id: str) -> dict:
    """Кнопки подтверждения опасного шага."""
    return {"inline_keyboard": [
        [{"text": "✅ Разрешить", "callback_data": f"confirm:{confirm_id}:yes"},
         {"text": "✅ Всегда", "callback_data": f"confirm:{confirm_id}:always"}],
        [{"text": "🚫 Отклонить", "callback_data": f"confirm:{confirm_id}:no"}],
    ]}


def parse_callback(data: str):
    """'confirm:<id>:yes' → (id, True, False); ':always' → (id, True, True).

    Неизвестное действие → (None, None, None).
    """
    parts = str(data or "").split(":")
    if len(parts) != 3 or parts[0] != "confirm":
        return None, None, None
    if parts[2] not in ("yes", "no", "always"):
        return None, None, None
    return parts[1], parts[2] in ("yes", "always"), parts[2] == "always"


# ══════════════ чат пользователя ══════════════

def chat_for(user_id, name="") -> str:
    """Свой чат в базе на каждого telegram-пользователя."""
    title = f"TG {user_id}"
    try:
        for chat in database.list_chats(limit=200):
            if chat.get("title") == title:
                return chat["id"]
    except Exception:
        pass
    return database.create_chat(f"{title} · {name}" if name else title)


def owner_key(user_id) -> str:
    return f"tg:{user_id}"


# ══════════════ запуск агента ══════════════

def agent_events(user_id, chat_id, text):
    """Генератор событий агента для одного сообщения из Telegram."""
    import security
    from app import app
    import routes.agent as agent_routes

    with app.test_request_context(
            "/api/agent/stream", method="POST", json={"message": text},
            headers={"X-Internal-Token": security.INTERNAL_RUN_TOKEN}):
        from flask import session
        session["chat_id"] = chat_id
        session["nova_user_nick"] = owner_key(user_id)
        session["nova_signed_in"] = True
        response = agent_routes.agent_stream()
        for raw in response.response:
            try:
                event = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if isinstance(event, dict):
                yield event


class Bridge:
    """Мост Telegram ↔ агент. Все внешние вызовы — через self.api."""

    def __init__(self, api=None, events_factory=None):
        self.api = api or api_call
        self.events_factory = events_factory or agent_events
        self.confirms = {}          # confirm_id → (chat_id, message_id)

    # ── вспомогательное ──
    def send(self, chat_id, text, reply_markup=None):
        for chunk in split_message(text):
            params = {"chat_id": chat_id, "text": chunk,
                      "disable_web_page_preview": True}
            if reply_markup:
                params["reply_markup"] = reply_markup
            self.api("sendMessage", **params)

    def _lock(self, user_id):
        with _locks_guard:
            return _user_locks.setdefault(str(user_id), threading.Lock())

    # ── обработка сообщений ──
    def handle_message(self, message):
        chat_id = (message.get("chat") or {}).get("id")
        user = message.get("from") or {}
        user_id = user.get("id")
        text = str(message.get("text") or "").strip()
        if not chat_id or not user_id or not text:
            return False
        if not is_allowed(user_id):
            self.send(chat_id, "⛔ Доступ к боту ограничен. Попросите владельца "
                               "добавить ваш id в TELEGRAM_ALLOWED_USERS.")
            return False
        if text.startswith("/start"):
            self.send(chat_id, "Привет! Я NovaMind. Напишите задачу — найду, посчитаю, "
                               "поработаю с кодом. Опасные шаги спрошу кнопками.")
            return True
        self.run_turn(chat_id, user_id, text, name=user.get("first_name") or "")
        return True

    def run_turn(self, chat_id, user_id, text, name=""):
        """Полный ход агента: события → сообщения Telegram."""
        with self._lock(user_id):
            try:
                self.api("sendChatAction", chat_id=chat_id, action="typing")
            except TelegramError:
                pass
            internal_chat = chat_for(user_id, name)
            answer = ""
            steps = []
            try:
                for event in self.events_factory(user_id, internal_chat, text):
                    kind = event.get("type")
                    if kind == "step":
                        steps.append(f"{event.get('icon') or '•'} {event.get('text') or ''}")
                    elif kind == "tool" and event.get("command"):
                        steps.append(f"⌨️ {event['command']}")
                    elif kind == "confirm":
                        sent = self.api("sendMessage", chat_id=chat_id,
                                        text=("⚠️ Нужно разрешение\n"
                                              f"{event.get('tool') or 'действие'}: "
                                              f"{event.get('command') or ''}"),
                                        reply_markup=confirm_keyboard(event.get("id")))
                        self.confirms[event.get("id")] = (
                            chat_id, (sent or {}).get("message_id"))
                    elif kind == "result":
                        answer = str(event.get("reply") or "")
                    elif kind == "error":
                        answer = answer or f"❌ {event.get('text') or 'ошибка'}"
            except Exception as exc:                    # бот не должен падать
                log.warning("Ошибка хода из Telegram: %s", exc, exc_info=True)
                self.send(chat_id, f"❌ Ошибка: {exc}")
                return ""
            if steps:
                # Короткая трасса: что делал агент, без простыни.
                trace = "\n".join(steps[-TRACE_LIMIT:])
                self.send(chat_id, f"🧭 Что делал агент:\n{trace}")
            if answer:
                self.send(chat_id, answer)
            return answer

    # ── подтверждения ──
    def handle_callback(self, callback):
        data = callback.get("data") or ""
        confirm_id, approve, always = parse_callback(data)
        callback_id = callback.get("id")
        if not confirm_id:
            self.api("answerCallbackQuery", callback_query_id=callback_id,
                     text="Неизвестное действие")
            return False
        import routes.agent as agent_routes
        ok, reason = agent_routes.resolve_confirmation(confirm_id, approve, always=always)
        if ok:
            if approve and always:
                text = "Разрешено всегда — выполняю"
            else:
                text = "Разрешено — выполняю" if approve else "Отклонено"
        elif reason == "already_resolved":
            text = "Решение уже принято"
        else:
            text = "Время ожидания истекло"
        try:
            self.api("answerCallbackQuery", callback_query_id=callback_id, text=text)
        except TelegramError:
            pass
        chat_id = ((callback.get("message") or {}).get("chat") or {}).get("id")
        if chat_id:
            self.send(chat_id, f"✓ {text}")
        return ok

    # ── обновления ──
    def handle_update(self, update, sync=False):
        if "message" in update:
            if sync:
                return self.handle_message(update["message"])
            thread = threading.Thread(target=self.handle_message,
                                      args=(update["message"],), daemon=True)
            thread.start()
            return True
        if "callback_query" in update:
            return self.handle_callback(update["callback_query"])
        return False

    def get_updates(self, offset=0, timeout=POLL_TIMEOUT):
        return self.api("getUpdates", http_timeout=timeout + 10, offset=offset,
                        timeout=timeout,
                        allowed_updates=["message", "callback_query"])

    def poll_forever(self, stop_event=None):
        stop_event = stop_event or _stop_event
        offset = 0
        log.info("Telegram-бот запущен (long polling)")
        while not stop_event.is_set():
            try:
                updates = self.get_updates(offset)
            except TelegramError as error:
                log.warning("Ошибка getUpdates: %s", error)
                stop_event.wait(5)
                continue
            for update in updates or []:
                offset = max(offset, int(update.get("update_id", 0)) + 1)
                try:
                    self.handle_update(update)
                except Exception:
                    log.warning("Не удалось обработать обновление", exc_info=True)


# ══════════════ фоновый запуск ══════════════

def start(bridge=None):
    """Поднимает бота в отдельном потоке. Возвращает True, если запустился."""
    global _thread
    if not enabled():
        log.info("Telegram-бот выключен: нет TELEGRAM_BOT_TOKEN")
        return False
    if _thread and _thread.is_alive():
        return False
    _stop_event.clear()
    bridge = bridge or Bridge()
    _thread = threading.Thread(target=bridge.poll_forever, args=(_stop_event,),
                               name="novamind-telegram", daemon=True)
    _thread.start()
    return True


def stop():
    global _thread
    _stop_event.set()
    if _thread and _thread.is_alive():
        _thread.join(timeout=3)
    _thread = None


def status():
    """Состояние бота без токена (можно безопасно отдавать в API/UI)."""
    return {"enabled": enabled(), "allowed_users": len(allowed_users()),
            "poll_timeout": POLL_TIMEOUT}


if __name__ == "__main__":
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    if not enabled():
        raise SystemExit("Задайте TELEGRAM_BOT_TOKEN в .env и перезапустите.")
    Bridge().poll_forever()
