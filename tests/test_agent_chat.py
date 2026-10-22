"""Агент в главном чате: доступ, история, инструменты, медиа и офлайн-режим.

Это регрессионные тесты на то, что раньше было сломано:

  * агент был выключен по умолчанию (AGENT_ENABLED=0) и требовал входа;
  * «быстрый ответ» проглатывал JSON-действие — инструмент не выполнялся,
    а в чат попадал сырой JSON;
  * без TERMINAL_ENABLED=1 отключался и поиск в интернете;
  * ходы агента не попадали в историю чата;
  * статусы генерации медиа не доходили до интерфейса;
  * офлайн-«модель» отвечала заглушкой «вопрос принят и разобран».
"""
import json

import pytest

import local_llm
import media_agent
import routes.agent as agent_routes
import routes.terminal as term_routes
import tool_registry


def _ndjson(response):
    return [json.loads(line) for line in response.get_data(as_text=True).splitlines() if line.strip()]


def _answer(events):
    return "".join(event["token"] for event in events if event["type"] == "token")


def _stub_stream(monkeypatch, answers, seen=None):
    queue = list(answers)

    def fake_stream(messages, **kwargs):
        if seen is not None:
            seen.append({"messages": list(messages), "system": kwargs.get("system", "")})
        text = queue.pop(0) if queue else "готово"
        yield "token", text
        yield "done", ""

    monkeypatch.setattr(agent_routes, "chat_stream", fake_stream)
    monkeypatch.setattr(agent_routes, "chat_completion",
                        lambda messages, **kw: ((queue.pop(0) if queue else ""), {}))
    return queue


@pytest.fixture
def fake_search(monkeypatch):
    results = [{"title": "Документация asyncio", "url": "https://docs.python.org/asyncio",
                "snippet": "asyncio — библиотека", "host": "docs.python.org"}]
    calls = []

    def search(query, limit=8):
        calls.append({"query": query, "limit": limit})
        return list(results), {"backend": "test", "elapsed_ms": 3, "query": query,
                               "results": results, "count": len(results),
                               "trace": [{"backend": "test", "ok": True, "ms": 3}]}

    monkeypatch.setattr(agent_routes, "nova_search", search)
    monkeypatch.setattr(agent_routes, "nova_read", lambda url, max_chars=4000: ("Текст страницы " + url, 2))
    return calls


# ══════════════ доступ без входа ══════════════

def test_guest_agent_can_search_without_login(client, sandbox, monkeypatch, fake_search):
    """Гость получает агента: поиск работает, рабочая папка — нет."""
    _stub_stream(monkeypatch, [
        '{"action":"search","query":"asyncio"}',
        "asyncio — это библиотека [1]",
    ])
    events = _ndjson(client.post("/api/agent/stream", json={"message": "найди asyncio"}))
    assert fake_search and fake_search[0]["query"] == "asyncio"
    tools = [event for event in events if event["type"] == "tool"]
    assert tools and tools[0]["name"] == "search"
    result = next(event for event in events if event["type"] == "result")
    assert result["linux"] is False
    assert "asyncio" in _answer(events)


def test_guest_agent_refuses_workspace_but_explains(client, sandbox, monkeypatch):
    """Команда в песочнице гостю недоступна, но ответ всё равно приходит."""
    _stub_stream(monkeypatch, [
        '{"action":"run","command":"ls"}',
        "Файлы доступны только после входа.",
    ])
    events = _ndjson(client.post("/api/agent/stream", json={"message": "покажи файлы"}))
    assert not [event for event in events if event["type"] == "tool"]
    assert "войдите" in _answer(events).lower() or "доступны только" in _answer(events).lower()


def test_status_reports_capabilities_without_login(client):
    status = client.get("/api/agent/status").get_json()
    assert status["enabled"] is True and status["available"] is True
    assert status["tools"] is False and status["guest"] is True
    assert status["search"] is True
    assert "TERMINAL_ENABLED" in status["workspace_hint"]


# ══════════════ быстрый путь не теряет действия ══════════════

def test_fast_answer_never_returns_raw_json(google_client, monkeypatch, fake_search):
    """Короткий вопрос + ответ-действие: действие выполняется, JSON не утекает."""
    _stub_stream(monkeypatch, [
        '{"action":"search","query":"погода"}',
        "Сегодня тепло.",
    ])
    events = _ndjson(google_client.post("/api/agent/stream", json={"message": "что нового"}))
    assert any(event["type"] == "tool" for event in events)
    answer = _answer(events)
    assert "{" not in answer and "Сегодня тепло" in answer


def test_fast_path_still_answers_simple_question_in_one_call(google_client, monkeypatch):
    queue = _stub_stream(monkeypatch, ["Привет! Чем помочь?", "лишний"])
    events = _ndjson(google_client.post("/api/agent/stream", json={"message": "привет"}))
    assert queue == ["лишний"]
    assert _answer(events) == "Привет! Чем помочь?"
    assert not any(event["type"] in ("tool", "plan") for event in events)


# ══════════════ история ══════════════

def test_agent_turn_is_saved_to_chat_history(google_client, monkeypatch):
    _stub_stream(monkeypatch, ["Запомнил: меня зовут Бунёд."])
    _ndjson(google_client.post("/api/agent/stream", json={"message": "меня зовут Бунёд"}))
    history = google_client.get("/api/history").get_json()["history"]
    assert any(m["role"] == "user" and "Бунёд" in m["content"] for m in history)
    assert any(m["role"] == "assistant" and "Запомнил" in m["content"] for m in history)


def test_agent_remembers_previous_turns_through_database(google_client, monkeypatch):
    seen = []
    _stub_stream(monkeypatch, ["Запомнил."], seen)
    _ndjson(google_client.post("/api/agent/stream", json={"message": "меня зовут Бунёд"}))
    _stub_stream(monkeypatch, ["Тебя зовут Бунёд."], seen)
    _ndjson(google_client.post("/api/agent/stream", json={"message": "как меня зовут?"}))
    previous = " ".join(message["content"] for message in seen[-1]["messages"])
    assert "Бунёд" in previous


# ══════════════ инструменты и флаги результата ══════════════

def test_linux_flag_is_true_only_after_workspace_action(admin_client, monkeypatch, sandbox):
    _stub_stream(monkeypatch, ['{"action":"run","command":"pwd"}', "Готово."])
    events = _ndjson(admin_client.post("/api/agent/stream", json={"message": "где я?"}))
    result = next(event for event in events if event["type"] == "result")
    assert result["linux"] is True and "run" in result["tools"]


def test_git_action_runs_through_registry(admin_client, monkeypatch, sandbox):
    _stub_stream(monkeypatch, ['{"action":"git","command":"status --short"}', "Проверил git."])
    events = _ndjson(admin_client.post("/api/agent/stream", json={"message": "проверь git"}))
    tool = next(event for event in events if event["type"] == "tool")
    assert tool["command"].startswith("git status")
    result = next(event for event in events if event["type"] == "result")
    assert "git" in result["tools"]


def test_workspace_tools_are_hidden_from_guest_prompt(client, sandbox, monkeypatch):
    seen = []
    _stub_stream(monkeypatch, ["Ответ без файлов"], seen)
    _ndjson(client.post("/api/agent/stream", json={"message": "что в папке?"}))
    system = seen[0]["system"]
    assert '"action":"run"' not in system and '"action":"write"' not in system
    assert "TERMINAL_ENABLED=0" in system


def test_registry_blocks_workspace_tools_without_login(client, sandbox):
    tool_registry.bootstrap_default_tools()
    with client.application.test_request_context("/api/agent/stream"):
        result = tool_registry.call_tool("file_write", path="hack.txt", content="x")
    assert result["ok"] is False
    assert result["error"]["type"] == "unauthorized"


# ══════════════ медиа ══════════════

def test_media_generation_reports_real_statuses(google_client, monkeypatch):
    def fake_generate(kind, prompt, options=None, wanted_model="", on_status=None):
        if on_status:
            on_status("Отправка запроса к Pollinations (flux)")
            on_status("Провайдер вернул ответ, сохраняю файл")
        return ({"kind": kind, "url": "/media/x.png", "filename": "x.png",
                 "model_name": "Pollinations Flux"}, 
                {"id": "flux", "name": "Pollinations Flux", "provider_name": "Pollinations"}, [])

    monkeypatch.setattr(media_agent, "generate", fake_generate)
    _stub_stream(monkeypatch, ['{"action":"image","prompt":"закат"}', "Готово."])
    events = _ndjson(google_client.post("/api/agent/stream", json={"message": "нарисуй закат"}))
    steps = [event["text"] for event in events if event["type"] == "step"]
    assert any("Отправка запроса" in text for text in steps)
    assert any("сохраняю файл" in text for text in steps)
    media = next(event for event in events if event["type"] == "media")
    assert media["media"]["url"] == "/media/x.png"


def test_media_is_marked_in_history_for_reload(google_client, monkeypatch):
    def fake_generate(kind, prompt, options=None, wanted_model="", on_status=None):
        return ({"kind": kind, "url": "/media/x.png", "filename": "x.png",
                 "model_name": "Pollinations Flux"},
                {"id": "flux", "name": "Pollinations Flux", "provider_name": "Pollinations"}, [])

    monkeypatch.setattr(media_agent, "generate", fake_generate)
    _stub_stream(monkeypatch, ['{"action":"image","prompt":"маяк"}', "Готово."])
    _ndjson(google_client.post("/api/agent/stream", json={"message": "нарисуй маяк"}))
    history = google_client.get("/api/history").get_json()["history"]
    markers = [m for m in history if m["content"].startswith("MEDIA_RESULT:")]
    assert markers and "/media/x.png" in markers[0]["content"]


# ══════════════ совместимость: /send_stream с agent=true ══════════════

def test_send_stream_can_delegate_to_agent(client, sandbox, monkeypatch, fake_search):
    _stub_stream(monkeypatch, ['{"action":"search","query":"курс"}', "Курс найден."])
    events = _ndjson(client.post("/send_stream", json={"message": "какой курс", "agent": True}))
    assert any(event["type"] == "tool" for event in events)
    assert _answer(events) == "Курс найден."


# ══════════════ офлайн-режим: реальные операции, а не заглушка ══════════════

def _offline(text):
    return local_llm.complete([{"role": "user", "content": text}])


def test_offline_arithmetic_is_real():
    assert "**4**" in _offline("2+2")
    assert "**30**" in _offline("15% от 200")


def test_offline_units_are_real():
    assert "°F" in _offline("20 C в F")
    assert "фунт" in _offline("5 кг в фунты")
    assert "6.2137" in _offline("10 км в мили")


def test_offline_text_tools_are_real():
    assert "0L/RgNC40LLQtdGC" in _offline("base64: привет")
    assert "e58f1e8c55fa105bdd3f40e5037eb0b039b5998d52c05e6cd98878dd2da5cab2" in _offline("sha256: привет")
    assert '"a": 1' in _offline('json: {"a":1}')


def test_offline_is_honest_about_not_being_a_model():
    reply = _offline("как приготовить плов")
    assert "языковой модели нет" in reply.lower()
    assert "токенов" not in reply.lower()          # старой заглушки больше нет
    assert "GROQ_API_KEY" in reply                 # и есть инструкция, что подключить


def test_offline_datetime_is_real():
    import datetime
    reply = _offline("сколько времени")
    assert datetime.datetime.now().strftime("%H:%M") in reply


# ══════════════ офлайн-агент: поиск настоящим инструментом ══════════════

def test_offline_agent_searches_when_model_is_absent(client, sandbox, fake_search):
    """Без языковой модели «найди …» всё равно идёт в настоящий поиск."""
    events = _ndjson(client.post("/api/agent/stream", json={"message": "найди asyncio"}))
    assert fake_search, "поиск не вызывался"
    assert fake_search[0]["query"] == "asyncio", fake_search
    assert any(event["type"] == "tool" and event.get("name") == "search" for event in events)
    result = next(event for event in events if event["type"] == "result")
    assert result["offline"] is True and result["searched"] is True
    assert "docs.python.org/asyncio" in _answer(events)


def test_offline_agent_keeps_honest_answer_when_search_is_empty(client, sandbox, monkeypatch):
    monkeypatch.setattr(agent_routes, "nova_search", lambda query, limit=8: ([], {"backend": "none"}))
    events = _ndjson(client.post("/api/agent/stream", json={"message": "найди неизвестно-что"}))
    assert any(event["type"] == "step" and "ничего не дал" in event["text"] for event in events)
    assert "языковой модели нет" in _answer(events).lower()


def test_offline_intent_never_hijacks_a_working_model(client, sandbox, fake_search, monkeypatch):
    """Если модель ответила по-настоящему, офлайн-ветка в дело не лезет."""
    _stub_stream(monkeypatch, ["asyncio — это библиотека для асинхронности."])
    events = _ndjson(client.post("/api/agent/stream", json={"message": "найди asyncio"}))
    assert not fake_search, "поиск запустился, хотя модель ответила сама"
    assert "библиотека для асинхронности" in _answer(events)


# ══════════════ офлайн-медиа и служебные сообщения ══════════════

def test_offline_media_request_says_why_and_tries_once(client, sandbox, monkeypatch):
    attempts = []

    def generate(kind, prompt, options=None, wanted_model="", on_status=None):
        attempts.append(prompt)
        return None, None, ["Нет доступных моделей (image). Подключить: POLLINATIONS_API_KEY"]

    monkeypatch.setattr(media_agent, "generate", generate)
    events = _ndjson(client.post("/api/agent/stream", json={"message": "нарисуй кота"}))
    reply = _answer(events)
    assert "Не получилось создать" in reply and "POLLINATIONS_API_KEY" in reply
    assert "языковой модели нет" not in reply.lower()
    assert attempts == ["кота"], attempts            # промпт без «нарисуй», попытка одна
    failures = [event for event in events
                if event["type"] == "step" and "Не получилось создать" in event["text"]]
    assert len(failures) == 1 and "Нет доступных моделей" in failures[0]["text"]


def test_offline_ignores_internal_observations_between_steps():
    """Служебная подсказка агента не должна становиться «вопросом пользователя»."""
    reply = local_llm.complete([
        {"role": "user", "content": "сколько времени"},
        {"role": "user", "content": "Инструменты рабочей папки отключены (TERMINAL_ENABLED=0 "
                                    "или нет входа). Не вызывай run/read/write; ответь обычным текстом."},
    ])
    assert "Инструменты рабочей папки" not in reply
    assert ":" in reply
