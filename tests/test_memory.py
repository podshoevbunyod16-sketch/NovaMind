"""Долгосрочная память (этап 4).

Память хранит устойчивые факты о пользователе в SQLite (+ FTS5, если он
есть в сборке sqlite3). Пароли и ключи не сохраняются, в промпт попадает
не больше 1200 символов, гостям память недоступна.
"""
import json

import pytest

import database
import memory
import routes.agent as agent_routes


def _ndjson(response):
    return [json.loads(line) for line in response.get_data(as_text=True).splitlines() if line.strip()]


def _answer(events):
    return "".join(event["token"] for event in events if event["type"] == "token")


@pytest.fixture(autouse=True)
def fresh_memory(tmp_path, monkeypatch):
    """Своя база памяти на каждый тест (репозиторий не трогаем)."""
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "memory_test.db"))
    database.init_db()
    database._memory_ready.clear()
    database._fts_ready.clear()
    yield
    database._memory_ready.clear()
    database._fts_ready.clear()


# ══════════════ база и поиск ══════════════

def test_remember_and_recall_russian():
    item = memory.remember("u1", "Люблю рыбу и готовлю на пару", kind="preference", tags="еда")
    assert item["id"] and item["kind"] == "preference"

    # «рыба» должна находить «рыбу» — без стеммера, по основе слова
    found = memory.recall("u1", "рыба")
    assert [entry["text"] for entry in found] == ["Люблю рыбу и готовлю на пару"]

    # по тегу тоже находится
    assert memory.recall("u1", "еда")[0]["id"] == item["id"]
    # чужие записи не видны
    assert memory.recall("u2", "рыба") == []


def test_memory_kind_is_validated():
    assert memory.remember("u1", "заметка про отпуск", kind="чепуха")["kind"] == "fact"
    assert memory.remember("u1", "решение: делаем на Flask", kind="decision")["kind"] == "decision"


def test_secret_filter_blocks_passwords_and_tokens():
    assert memory.is_secret("пароль: qwerty123") is True
    assert memory.is_secret("Мой API key = abcdef123456") is True
    assert memory.is_secret("токен ghp_abcdefghijklmnopqrstuvwxyz012345") is True
    assert memory.is_secret("sk-abcdefghijklmnopqrstuv") is True
    assert memory.is_secret("Люблю кофе без сахара") is False

    with pytest.raises(ValueError):
        memory.remember("u1", "мой пароль: qwerty123")
    with pytest.raises(ValueError):
        memory.remember("u1", "ключ sk-abcdefghijklmnopqrstuv")
    assert memory.recall("u1", "пароль") == []


def test_memory_requires_user_key():
    with pytest.raises(ValueError):
        memory.remember("", "Люблю кофе")
    assert memory.recall("", "кофе") == []
    assert memory.prompt_block("", "кофе") == ""


def test_prompt_block_limits_items_and_chars():
    for index in range(9):
        memory.remember("u1", f"Проект номер {index}: " + "очень важная деталь " * 20,
                        kind="project", importance=5 - (index % 3))
    block = memory.prompt_block("u1", "проект", limit=5, max_chars=1200)

    assert block
    assert len(block) <= 1200
    assert len(block.splitlines()) <= 5


def test_delete_memory():
    item = memory.remember("u1", "Люблю чай с молоком")
    assert memory.delete("u1", item["id"]) is True
    assert memory.recall("u1", "чай") == []
    assert memory.delete("u1", item["id"]) is False          # повторно — уже нет
    other = memory.remember("u2", "Люблю чай с молоком")
    assert memory.delete("u1", other["id"]) is False          # чужое не удаляется


def test_memory_touches_last_used_at():
    item = memory.remember("u1", "Работаю над NovaMind")
    assert item["last_used_at"] is None
    memory.recall("u1", "NovaMind")
    stored = memory.list_all("u1")[0]
    assert stored["last_used_at"] is not None


# ══════════════ маршруты панели ══════════════

def test_memory_routes_require_login(client):
    assert client.get("/api/memory").status_code == 403
    assert client.post("/api/memory", json={"text": "привет"}).status_code == 403
    status = client.get("/api/memory/status").get_json()
    assert status["available"] is False and status["enabled"] is True


def test_memory_routes_crud(google_client):
    created = google_client.post("/api/memory", json={
        "text": "Пишу проект на Flask", "kind": "project", "tags": "код"})
    assert created.status_code == 201
    memory_id = created.get_json()["item"]["id"]

    listed = google_client.get("/api/memory").get_json()
    assert listed["count"] == 1 and listed["items"][0]["text"] == "Пишу проект на Flask"

    found = google_client.get("/api/memory?q=фласк").get_json()
    assert found["count"] == 0                      # другая основа — не совпадение
    found = google_client.get("/api/memory?q=проект").get_json()
    assert found["count"] == 1

    assert google_client.delete(f"/api/memory/{memory_id}").status_code == 200
    assert google_client.get("/api/memory").get_json()["count"] == 0
    assert google_client.delete(f"/api/memory/{memory_id}").status_code == 404


def test_memory_route_rejects_secrets(google_client):
    response = google_client.post("/api/memory", json={"text": "мой токен: abcdef1234567890"})
    assert response.status_code == 400
    assert "ключ" in response.get_json()["error"]


# ══════════════ агент ══════════════

def test_agent_puts_memory_block_into_prompt(google_client, sandbox, monkeypatch):
    memory.remember("ivan@example.com", "Предпочитаю краткие ответы без воды", kind="preference")
    seen = []

    def completion(messages, **kwargs):
        seen.append({"messages": list(messages), "system": kwargs.get("system", "")})
        return "Коротко: да.", {}

    monkeypatch.setattr(agent_routes, "chat_completion", completion)
    monkeypatch.setattr(agent_routes, "chat_stream", lambda *a, **k: iter(()))

    events = _ndjson(google_client.post("/api/agent/stream",
                                        json={"message": "как дела? ответь кратко"}))
    prompt = seen[0]["system"] + "\n".join(str(item.get("content") or "")
                                            for item in seen[0]["messages"])
    assert "MEMORY" in prompt
    assert "Предпочитаю краткие ответы" in prompt
    assert "Коротко: да." in _answer(events)


def test_guest_has_no_memory_block(client, sandbox, monkeypatch):
    memory.remember("someone@example.com", "Секретная деталь про кофе")
    seen = []

    def completion(messages, **kwargs):
        seen.append(kwargs.get("system", ""))
        return "Ответ", {}

    monkeypatch.setattr(agent_routes, "chat_completion", completion)
    monkeypatch.setattr(agent_routes, "chat_stream", lambda *a, **k: iter(()))

    _ndjson(client.post("/api/agent/stream", json={"message": "найди что-нибудь"}))
    assert all("Секретная деталь" not in system for system in seen)


def test_agent_remember_action_saves_and_blocks_secrets(google_client, sandbox, monkeypatch):
    answers = [
        '{"action":"remember","text":"Работаю над проектом NovaMind","kind":"project","tags":"работа"}',
        "Запомнил.",
    ]
    monkeypatch.setattr(agent_routes, "chat_completion",
                        lambda messages, **kwargs: ((answers.pop(0) if answers else "готово"), {}))
    monkeypatch.setattr(agent_routes, "chat_stream", lambda *a, **k: iter(()))

    events = _ndjson(google_client.post("/api/agent/stream",
                                        json={"message": "запомни, что я работаю над проектом NovaMind"}))
    saved = [item for item in memory.list_all("ivan@example.com")]
    assert saved and saved[0]["text"] == "Работаю над проектом NovaMind"
    assert any(event["type"] == "memory" for event in events)
    assert "Запомнил" in _answer(events)


def test_agent_forget_action_removes_memory(google_client, sandbox, monkeypatch):
    item = memory.remember("ivan@example.com", "Люблю тёмную тему", kind="preference")
    answers = [json.dumps({"action": "forget", "id": item["id"]}, ensure_ascii=False), "Удалил."]
    monkeypatch.setattr(agent_routes, "chat_completion",
                        lambda messages, **kwargs: ((answers.pop(0) if answers else "готово"), {}))
    monkeypatch.setattr(agent_routes, "chat_stream", lambda *a, **k: iter(()))

    _ndjson(google_client.post("/api/agent/stream", json={"message": "забудь про тёмную тему"}))
    assert memory.list_all("ivan@example.com") == []


def test_remember_tool_rejects_secret_without_crashing(sandbox, google_client):
    """Реестр тоже не сохраняет секреты и отдаёт понятную ошибку."""
    import tool_registry
    from flask import session
    tool_registry.bootstrap_default_tools()

    # без входа память недоступна
    result = tool_registry.call_tool("remember", text="Люблю кофе")
    assert result["ok"] is False and "вход" in result["error"]["message"].lower()

    with google_client.application.test_request_context("/"):
        session["nova_google_login"] = True
        session["nova_user_email"] = "ivan@example.com"
        result = tool_registry.call_tool("remember", text="пароль: 123456")
    assert result["ok"] is False and "пароль" in result["error"]["message"]
