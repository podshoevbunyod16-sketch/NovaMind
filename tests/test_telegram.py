"""Telegram-мост (бонус Б5): polling, ответы и кнопки подтверждения."""
import json

import pytest

import telegram_bot


@pytest.fixture(autouse=True)
def no_token(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_ALLOWED_USERS", raising=False)


class FakeApi:
    """Записывает вызовы Telegram и отдаёт заранее заданные ответы."""

    def __init__(self, results=None):
        self.calls = []
        self.results = results or {}

    def __call__(self, method, http_timeout=30, **params):
        self.calls.append({"method": method, "params": params})
        return self.results.get(method, {"message_id": 100 + len(self.calls)})

    def methods(self):
        return [call["method"] for call in self.calls]

    def first(self, method):
        return next(call for call in self.calls if call["method"] == method)


# ══════════════ настройки ══════════════

def test_bot_disabled_without_token():
    assert telegram_bot.enabled() is False
    assert telegram_bot.status()["enabled"] is False


def test_bot_enabled_with_token(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    assert telegram_bot.enabled() is True
    assert telegram_bot.status()["enabled"] is True


def test_bot_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_ENABLED", "0")
    assert telegram_bot.enabled() is False


def test_allowed_users_whitelist(monkeypatch):
    assert telegram_bot.is_allowed(42) is True          # список пуст — пускаем всех
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "42, 77")
    assert telegram_bot.is_allowed(42) is True
    assert telegram_bot.is_allowed(99) is False


# ══════════════ API и тексты ══════════════

def test_api_call_requires_token():
    with pytest.raises(telegram_bot.TelegramError):
        telegram_bot.api_call("getMe")


def test_api_call_reports_error_without_token(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:secret-token")

    class Response:
        status_code = 401
        def json(self):
            return {"ok": False, "description": "Unauthorized 123:secret-token"}

    monkeypatch.setattr(telegram_bot.requests, "post", lambda *a, **k: Response())
    with pytest.raises(telegram_bot.TelegramError) as error:
        telegram_bot.api_call("getMe")
    assert "secret-token" not in str(error.value)
    assert "<token>" in str(error.value)


def test_api_call_returns_result(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:secret-token")
    seen = {}

    class Response:
        def json(self):
            return {"ok": True, "result": {"id": 7}}

    def fake_post(url, json=None, timeout=None):
        seen["url"] = url
        seen["timeout"] = timeout
        seen["payload"] = json
        return Response()

    monkeypatch.setattr(telegram_bot.requests, "post", fake_post)
    assert telegram_bot.api_call("sendMessage", chat_id=5, text="привет") == {"id": 7}
    assert seen["url"].endswith("/sendMessage") and "secret-token" in seen["url"]
    assert seen["payload"] == {"chat_id": 5, "text": "привет"}


def test_split_message_respects_limit():
    assert telegram_bot.split_message("") == []
    assert telegram_bot.split_message("привет") == ["привет"]
    long_text = "\n".join(f"строка номер {index}" for index in range(500))
    chunks = telegram_bot.split_message(long_text, limit=200)
    assert len(chunks) > 1 and all(len(chunk) <= 200 for chunk in chunks)
    assert "".join(chunks).replace("\n", "") == long_text.replace("\n", "")


def test_split_message_cuts_huge_line():
    chunks = telegram_bot.split_message("я" * 500, limit=100)
    assert len(chunks) == 5 and all(len(chunk) == 100 for chunk in chunks)


def test_confirm_keyboard_and_parse():
    keyboard = telegram_bot.confirm_keyboard("abc123")
    buttons = [button for row in keyboard["inline_keyboard"] for button in row]
    assert buttons[0]["callback_data"] == "confirm:abc123:yes"
    assert any(button["callback_data"] == "confirm:abc123:always" for button in buttons)
    assert telegram_bot.parse_callback("confirm:abc123:no") == ("abc123", False, False)
    assert telegram_bot.parse_callback("confirm:abc123:always") == ("abc123", True, True)
    assert telegram_bot.parse_callback("confirm:abc123:maybe") == (None, None, None)
    assert telegram_bot.parse_callback("что-то другое") == (None, None, None)


# ══════════════ сообщения ══════════════

def test_start_command(monkeypatch):
    api = FakeApi()
    bridge = telegram_bot.Bridge(api=api)
    handled = bridge.handle_message({
        "chat": {"id": 10}, "from": {"id": 5, "first_name": "Иван"}, "text": "/start"})
    assert handled is True
    assert api.first("sendMessage")["params"]["text"].startswith("Привет")


def test_blocked_user_gets_refusal(monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "1")
    api = FakeApi()
    bridge = telegram_bot.Bridge(api=api)
    assert bridge.handle_message({
        "chat": {"id": 10}, "from": {"id": 5}, "text": "привет"}) is False
    assert "ограничен" in api.first("sendMessage")["params"]["text"]


def test_telegram_agent_events_use_react_with_a_stable_owned_chat(client, monkeypatch):
    import ai_providers
    import database

    monkeypatch.setattr(ai_providers, "chat_completion",
                        lambda *args, **kwargs: ("Ответ из ReAct.", {"model": "mock"}))
    chat_id = telegram_bot.chat_for(711, "Test")
    events = list(telegram_bot.agent_events(711, chat_id, "Ответь кратко"))
    result = next(event for event in events if event.get("type") == "result")
    assert result["reply"] == "Ответ из ReAct."
    assert any(event.get("type") == "completion" for event in events)
    with __import__("app").app.test_request_context("/"):
        from flask import session
        session["nova_sid"] = telegram_bot._telegram_sid(711)
        assert database.chat_allowed(chat_id)


def test_telegram_react_question_resumes_durable_run(client, monkeypatch):
    import ai_providers

    answers = iter([
        ('```json\n{"tool":"ask_user","arguments":{"question":"Какой формат?","options":["CSV","JSON"]}}\n```', {}),
        ("Пользователь выбрал CSV; продолжаю с этим форматом.", {"model": "mock"}),
    ])
    monkeypatch.setattr(ai_providers, "chat_completion", lambda *args, **kwargs: next(answers))
    chat_id = telegram_bot.chat_for(712, "Question")
    paused = list(telegram_bot.agent_events(712, chat_id, "Подготовь файл"))
    question = next(event for event in paused if event.get("type") == "ask_user")
    resumed = telegram_bot.continue_react_run(712, question["run_id"], answer="CSV")
    result = next(event for event in resumed if event.get("type") == "result")
    assert "CSV" in result["reply"]
    assert any(event.get("type") == "completion" for event in resumed)


def test_telegram_react_confirmation_callback_is_bound_to_requesting_user(monkeypatch):
    resumed = []
    monkeypatch.setattr(telegram_bot, "continue_react_run",
                        lambda user_id, run_id, **kwargs: resumed.append((user_id, run_id, kwargs))
                        or [{"type": "result", "status": "completed", "reply": "Готово после одобрения."}])
    api = FakeApi()
    bridge = telegram_bot.Bridge(api=api, events_factory=lambda *_: iter([
        {"type": "confirmation", "run_id": "react-run-1", "tool": "file_write"},
        {"type": "result", "status": "paused", "reply": "Жду подтверждения."},
    ]))
    bridge.run_turn(90, 5, "Создай файл")
    denied = bridge.handle_callback({"id": "cb-bad", "data": "confirm:react-run-1:yes",
                                     "from": {"id": 6}, "message": {"chat": {"id": 90}}})
    assert denied is False and not resumed
    approved = bridge.handle_callback({"id": "cb-ok", "data": "confirm:react-run-1:yes",
                                       "from": {"id": 5}, "message": {"chat": {"id": 90}}})
    assert approved is True
    assert resumed == [(5, "react-run-1", {"approved": True})]
    repeat = bridge.handle_callback({"id": "cb-repeat", "data": "confirm:react-run-1:yes",
                                     "from": {"id": 5}, "message": {"chat": {"id": 90}}})
    assert repeat is False and len(resumed) == 1
    assert any(call["method"] == "sendMessage" and "Готово после одобрения" in call["params"]["text"]
               for call in api.calls)


def test_run_turn_sends_trace_and_answer(client):
    """Ход агента: шаг, ответ — и всё это уходит в Telegram."""
    def events(user_id, chat_id, text):
        yield {"type": "step", "icon": "🔎", "text": "ищу в интернете"}
        yield {"type": "tool", "name": "search", "command": "search «погода»"}
        yield {"type": "result", "reply": "Завтра солнечно."}

    api = FakeApi()
    bridge = telegram_bot.Bridge(api=api, events_factory=events)
    answer = bridge.run_turn(10, 5, "погода завтра", name="Иван")
    assert answer == "Завтра солнечно."
    texts = [call["params"]["text"] for call in api.calls
             if call["method"] == "sendMessage"]
    assert any("Что делал агент" in text for text in texts)
    assert texts[-1] == "Завтра солнечно."
    assert "sendChatAction" in api.methods()


def test_unknown_callback_is_answered():
    api = FakeApi()
    bridge = telegram_bot.Bridge(api=api)
    assert bridge.handle_callback({"id": "cb", "data": "неизвестно"}) is False
    assert "Неизвестное" in api.first("answerCallbackQuery")["params"]["text"]


def test_handle_update_routes_both_kinds():
    api = FakeApi()
    bridge = telegram_bot.Bridge(api=api)
    assert bridge.handle_update({"message": {"chat": {"id": 1}, "from": {"id": 2},
                                             "text": "/start"}}, sync=True) is True
    assert bridge.handle_update({"callback_query": {"id": "x", "data": "нет"}}) is False
    assert bridge.handle_update({"edited_message": {}}) is False


def test_get_updates_asks_for_both_types():
    api = FakeApi(results={"getUpdates": []})
    bridge = telegram_bot.Bridge(api=api)
    assert bridge.get_updates(offset=5) == []
    params = api.first("getUpdates")["params"]
    assert params["offset"] == 5
    assert params["allowed_updates"] == ["message", "callback_query"]


# ══════════════ запуск ══════════════

def test_start_without_token_does_nothing():
    assert telegram_bot.start() is False


def test_status_does_not_leak_token(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:super-secret")
    payload = json.dumps(telegram_bot.status())
    assert "super-secret" not in payload
    assert telegram_bot.status()["enabled"] is True
