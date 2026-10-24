"""Агент в главном чате: доступ, история, инструменты, медиа и офлайн-режим.

Регрессионные тесты на то, что раньше было сломано:

  * агент был выключен по умолчанию (AGENT_ENABLED=0) и требовал входа;
  * «быстрый ответ» проглатывал действие — инструмент не выполнялся;
  * без TERMINAL_ENABLED=1 отключался и поиск в интернете;
  * ходы агента не попадали в историю чата;
  * статусы генерации медиа не доходили до интерфейса;
  * офлайн-«модель» отвечала заглушкой «вопрос принят и разобран».

Действия модели оформляются блоками AgenticSeek (``agenticseek/core/agent.py``):
```web_search``, ```bash``, ```image`` и так далее.
"""
import json

import pytest

import local_llm
import media_agent
import routes.agent as agent_routes
from tests.agent_helpers import answer_of, events_of, model_text, ndjson, script


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
    script(monkeypatch, [
        "Ищу в интернете.\n```web_search\nasyncio\n```",
        "asyncio — это библиотека [1]",
    ])
    events = ndjson(client.post("/api/agent/stream", json={"message": "найди asyncio"}))
    assert fake_search and fake_search[0]["query"] == "asyncio"
    tools = events_of(events, "tool")
    assert tools and tools[0]["name"] == "web_search"
    result = next(event for event in events if event["type"] == "result")
    assert result["linux"] is False
    assert "asyncio" in answer_of(events)


def test_guest_agent_refuses_workspace_but_explains(client, sandbox, monkeypatch):
    """Команда в песочнице гостю недоступна, но ответ всё равно приходит."""
    script(monkeypatch, ["Файлы доступны только после входа в аккаунт."])
    events = ndjson(client.post("/api/agent/stream", json={"message": "покажи файлы"}))
    assert not events_of(events, "tool")
    reply = answer_of(events).lower()
    assert "войд" in reply or "доступны только" in reply


def test_status_reports_capabilities_without_login(client):
    status = client.get("/api/agent/status").get_json()
    assert status["enabled"] is True and status["available"] is True
    assert status["tools"] is False and status["guest"] is True
    assert status["search"] is True
    assert status["engine"] == "agenticseek"
    assert "TERMINAL_ENABLED" in status["workspace_hint"]


# ══════════════ быстрый путь не теряет действия ══════════════

def test_search_block_is_executed_and_not_shown_as_text(google_client, monkeypatch, fake_search):
    """Ответ с блоком поиска: блок выполняется, а в чат идёт только текст."""
    script(monkeypatch, [
        "```web_search\nпогода Худжанд\n```",
        "Сегодня тепло.",
    ])
    events = ndjson(google_client.post("/api/agent/stream", json={"message": "найди погоду"}))
    assert events_of(events, "tool")
    answer = answer_of(events)
    assert "```" not in answer and "Сегодня тепло" in answer


def test_simple_question_is_answered_in_one_call(google_client, monkeypatch):
    queue = script(monkeypatch, ["Привет! Чем помочь?", "лишний"])
    events = ndjson(google_client.post("/api/agent/stream", json={"message": "привет"}))
    assert queue == ["лишний"]
    assert answer_of(events) == "Привет! Чем помочь?"
    assert not events_of(events, "tool") and not events_of(events, "plan")


# ══════════════ история ══════════════

def test_agent_turn_is_saved_to_chat_history(google_client, monkeypatch):
    script(monkeypatch, ["Запомнил: меня зовут Бунёд."])
    ndjson(google_client.post("/api/agent/stream", json={"message": "меня зовут Бунёд"}))
    history = google_client.get("/api/history").get_json()["history"]
    assert any(m["role"] == "user" and "Бунёд" in m["content"] for m in history)
    assert any(m["role"] == "assistant" and "Запомнил" in m["content"] for m in history)


def test_agent_remembers_previous_turns_through_database(google_client, monkeypatch):
    seen = []
    script(monkeypatch, ["Запомнил."], seen)
    ndjson(google_client.post("/api/agent/stream", json={"message": "меня зовут Бунёд"}))
    script(monkeypatch, ["Тебя зовут Бунёд."], seen)
    ndjson(google_client.post("/api/agent/stream", json={"message": "как меня зовут?"}))
    assert "Бунёд" in model_text(seen, -1)


# ══════════════ инструменты и флаги результата ══════════════

def test_linux_flag_is_true_only_after_workspace_action(admin_client, monkeypatch, sandbox):
    script(monkeypatch, ["```bash\npwd\n```", "Готово."])
    events = ndjson(admin_client.post("/api/agent/stream", json={"message": "запусти pwd"}))
    result = next(event for event in events if event["type"] == "result")
    assert result["linux"] is True and "terminal" in result["tools"]


def test_git_command_runs_through_the_sandbox(admin_client, monkeypatch, sandbox):
    script(monkeypatch, ["```bash\ngit status --short\n```", "Проверил git."])
    events = ndjson(admin_client.post("/api/agent/stream", json={"message": "проверь git"}))
    tool = next(event for event in events if event["type"] == "tool")
    assert tool["command"].startswith("git status")
    result = next(event for event in events if event["type"] == "result")
    assert "terminal" in result["tools"]


def test_workspace_tools_are_hidden_from_guest_prompt(client, sandbox, monkeypatch):
    seen = []
    script(monkeypatch, ["Ответ без файлов"], seen)
    ndjson(client.post("/api/agent/stream", json={"message": "что в папке?"}))
    system = model_text(seen)
    # В промпте есть примеры блоков, поэтому проверяем список инструментов
    # запуска: у гостя нет файлов и терминала, но поиск и задачи остаются.
    tools_block = system.split("ДОСТУПНЫЕ ИНСТРУМЕНТЫ", 1)[-1]
    assert "```web_search" in tools_block
    for hidden in ("```bash", "```python", "```file_finder"):
        assert hidden not in tools_block
    assert "Рабочая папка:" in system      # папка названа, но инструментов нет


# ══════════════ медиа ══════════════

def _fake_generate(monkeypatch, result, errors=None, calls=None):
    def fake_generate(kind, prompt, options=None, wanted_model="", on_status=None):
        if calls is not None:
            calls.append(prompt)
        if on_status:
            on_status("Отправка запроса к Pollinations (flux)")
            on_status("Провайдер вернул ответ, сохраняю файл")
        return result, ({"id": "flux", "name": "Pollinations Flux"} if result else None), errors or []
    monkeypatch.setattr(media_agent, "generate", fake_generate)


def test_media_generation_reports_real_statuses(google_client, monkeypatch):
    _fake_generate(monkeypatch, {"kind": "image", "url": "/media/x.png",
                                 "filename": "x.png", "model_name": "Pollinations Flux"})
    events = ndjson(google_client.post("/api/agent/stream", json={"message": "нарисуй закат"}))
    steps = [event["text"] for event in events_of(events, "step")]
    assert any("Отправка запроса" in text for text in steps)
    assert any("сохраняю файл" in text for text in steps)
    media = events_of(events, "media")[0]
    assert media["media"]["url"] == "/media/x.png"


def test_media_is_marked_in_history_for_reload(google_client, monkeypatch):
    _fake_generate(monkeypatch, {"kind": "image", "url": "/media/x.png",
                                 "filename": "x.png", "model_name": "Pollinations Flux"})
    ndjson(google_client.post("/api/agent/stream", json={"message": "нарисуй маяк"}))
    history = google_client.get("/api/history").get_json()["history"]
    markers = [m for m in history if m["content"].startswith("MEDIA_RESULT:")]
    assert markers and "/media/x.png" in markers[0]["content"]


def test_media_request_does_not_ask_the_model(client, sandbox, monkeypatch):
    """Промпт уже в сообщении: модель для генерации не нужна (экономия запросов)."""
    calls, seen = [], []
    _fake_generate(monkeypatch, {"kind": "image", "url": "/media/y.png"}, calls=calls)
    script(monkeypatch, ["этот ответ не должен использоваться"], seen)
    events = ndjson(client.post("/api/agent/stream", json={"message": "нарисуй закат"}))
    assert seen == [] and calls and calls[0] == "нарисуй закат"
    assert answer_of(events).startswith("Готово")


# ══════════════ совместимость: /send_stream с agent=true ══════════════

def test_send_stream_can_delegate_to_agent(client, sandbox, monkeypatch, fake_search):
    script(monkeypatch, ["```web_search\nкурс доллара\n```", "Курс найден."])
    events = ndjson(client.post("/send_stream", json={"message": "какой курс", "agent": True}))
    assert events_of(events, "tool")
    assert answer_of(events) == "Курс найден."


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
    events = ndjson(client.post("/api/agent/stream", json={"message": "найди asyncio"}))
    assert fake_search, "поиск не вызывался"
    assert fake_search[0]["query"] == "asyncio", fake_search
    assert any(event["type"] == "tool" and event.get("name") == "web_search"
               for event in events)
    result = next(event for event in events if event["type"] == "result")
    assert result["offline"] is True and result["searched"] is True
    assert "docs.python.org/asyncio" in answer_of(events)


def test_offline_agent_keeps_honest_answer_when_search_is_empty(client, sandbox, monkeypatch):
    monkeypatch.setattr(agent_routes, "nova_search",
                        lambda query, limit=8: ([], {"backend": "none"}))
    events = ndjson(client.post("/api/agent/stream", json={"message": "найди неизвестно-что"}))
    assert any(event["type"] == "step" and "ничего не дал" in event["text"] for event in events)
    assert "языковой модели нет" in answer_of(events).lower()


def test_offline_intent_never_hijacks_a_working_model(client, sandbox, fake_search, monkeypatch):
    """Если модель ответила по-настоящему, офлайн-ветка в дело не лезет."""
    script(monkeypatch, ["asyncio — это библиотека для асинхронности."])
    events = ndjson(client.post("/api/agent/stream", json={"message": "найди asyncio"}))
    assert not fake_search, "поиск запустился, хотя модель ответила сама"
    assert "библиотека для асинхронности" in answer_of(events)


# ══════════════ офлайн-медиа и служебные сообщения ══════════════

def test_offline_media_request_says_why_and_tries_once(client, sandbox, monkeypatch):
    attempts = []
    _fake_generate(monkeypatch, None,
                   errors=["Нет доступных моделей (image). Подключить: POLLINATIONS_API_KEY"],
                   calls=attempts)
    events = ndjson(client.post("/api/agent/stream", json={"message": "нарисуй кота"}))
    reply = answer_of(events)
    assert "Не получилось создать" in reply and "POLLINATIONS_API_KEY" in reply
    assert "языковой модели нет" not in reply.lower()
    assert len(attempts) == 1, attempts
    failures = [event for event in events_of(events, "step")
                if "Не получилось создать" in event["text"]]
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
