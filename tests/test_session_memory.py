"""Память одного диалога: путь поиска, привязка chat_id, офлайн-ход.

Регрессии на реальные жалобы:

1. «Первый вопрос про Луну, второй — ни о чём»: `/api/auto_search_stream`
   не читал историю и вообще ничего не сохранял в базу — путь поиска был
   «памятью без прошлого», а ходы агента и поиска разъезжались по двум
   серверным чатам.
2. Фронтенд не шлёт chat_id: без привязки сессия молча перескакивала на
   новый чат (или продолжала чужой), и диалог «расщеплялся» на две записи.
3. Офлайн-режим строил ответ без предыдущих реплик.

Плюс привязка MCP к флагу MCP_ALLOW_USER_READ в агентном ходе и включение
автопоиска по умолчанию (без него агент ходил в обход MCP и скиллов).
"""
import json
from pathlib import Path

import pytest

import routes.search as search_routes
from tests.agent_helpers import ndjson

ROOT = Path(__file__).resolve().parent.parent


def _events(response):
    return [json.loads(line) for line in response.get_data(as_text=True).splitlines()
            if line.strip()]


def _result(events):
    return next(event for event in events if event["type"] == "result")


# ══════════════ путь поиска: история чтётся и сохраняется ══════════════

@pytest.fixture
def search_world(monkeypatch):
    """Поиск без сети и без модели: источники и ответ подменены, вход — записан."""
    captured = {"bodies": [], "queries": []}

    def fake_stream(messages, **kwargs):
        captured["bodies"].append([dict(item) for item in messages])
        yield "token", "Диаметр Луны — 3474 км."
        yield "done", ""

    def fake_parallel(query, emit):
        # Генератор без событий шага: yield from вернёт (results, trace),
        # не пропуская в поток None.
        if False:  # noqa: SIM108 — маркер генератора
            yield None
        return ([{"title": "Луна", "url": "https://example.org/moon",
                  "snippet": "Спутник Земли, средний диаметр 3474 км."}], ["mock"])

    def fake_query(message, history=None):
        captured["queries"].append((message, list(history or [])))
        return "луна диаметр"

    monkeypatch.setattr(search_routes, "chat_stream", fake_stream)
    monkeypatch.setattr(search_routes, "_parallel_search", fake_parallel)
    monkeypatch.setattr(search_routes, "_read_pages", lambda sources: [])
    monkeypatch.setattr(search_routes, "build_search_query", fake_query)
    return captured


def test_search_stream_feeds_history_into_answer_and_keeps_it_in_db(client, search_world):
    import database

    first = _events(client.post("/api/auto_search_stream",
                                json={"message": "Какой диаметр у Луны?",
                                      "force": True}))
    first_result = _result(first)
    chat_id = first_result.get("chat_id")
    assert chat_id, "путь поиска обязан вернуть chat_id для привязки фронтенда"

    # Ответ и вопрос сохранены в ОБЩУЮ с чатом исторь��.
    history = database.get_chat_history(chat_id, limit=10)
    assert [(m["role"], m["content"]) for m in history] == [
        ("user", "Какой диаметр у Луны?"),
        ("assistant", "Диаметр Луны — 3474 км."),
    ]

    # Второй ход: уточнение без собственного смысла — модель должна видеть
    # предыдущие реплики, и в промпт, и в поисковый запрос.
    second = _events(client.post("/api/auto_search_stream",
                                 json={"message": "А сколько он весит?",
                                      "force": True}))
    assert _result(second).get("chat_id") == chat_id

    answer_body = search_world["bodies"][-1][0]["content"]
    assert "Недавний разговор" in answer_body
    assert "Какой диаметр у Луны?" in answer_body
    assert "3474" in answer_body

    _message, query_history = search_world["queries"][-1]
    assert any("Какой диаметр у Луны?" in str(item.get("content"))
               for item in query_history)

    assert len(database.get_chat_history(chat_id, limit=10)) == 4


def test_search_stream_chat_id_null_starts_fresh_and_value_rebinds(client, search_world):
    import database

    first = _result(_events(client.post("/api/auto_search_stream",
                                        json={"message": "первый вопрос",
                                              "force": True})))
    original = first["chat_id"]

    fresh = _result(_events(client.post("/api/auto_search_stream",
                                        json={"message": "новый диалог",
                                              "force": True,
                                              "chat_id": None})))
    assert fresh["chat_id"] and fresh["chat_id"] != original
    assert len(database.get_chat_history(fresh["chat_id"], limit=10)) == 2

    # Возврат к прежнему чату: его история продолжает наполняться.
    back = _result(_events(client.post("/api/auto_search_stream",
                                       json={"message": "снова про луну",
                                             "force": True,
                                             "chat_id": original})))
    assert back["chat_id"] == original
    assert len(database.get_chat_history(original, limit=10)) == 4
    assert len(database.get_chat_history(fresh["chat_id"], limit=10)) == 2


def test_build_search_query_uses_conversation_context(monkeypatch):
    seen = []
    monkeypatch.setattr(search_routes, "resolve_target",
                        lambda *a, **k: ("groq", "test-model", {}))
    monkeypatch.setattr(
        search_routes, "chat_completion",
        lambda messages, **kw: (seen.append(messages) or "луна диаметр", {}))
    search_routes.build_search_query(
        "а его размер?", history=[{"role": "user", "content": "Какой диаметр у Луны?"}])
    prompt = seen[0][0]["content"]
    assert "Контекст разговора" in prompt
    assert "Какой диаметр у Луны?" in prompt
    assert "уточнение" in prompt


# ══════════════ агент: привязка chat_id из интерфейса ══════════════

def test_agent_stream_follows_client_chat_id(admin_client, sandbox, monkeypatch):
    import database
    from tests.agent_helpers import script

    seen = []
    script(monkeypatch, ["Первый ответ.", "Ответ в новом чате.", "Снова первый."], seen)

    first = _result(ndjson(admin_client.post(
        "/api/agent/stream", json={"message": "первый вопрос", "tools": False})))
    original = first["chat_id"]
    assert original

    fresh = _result(ndjson(admin_client.post(
        "/api/agent/stream",
        json={"message": "новый диалог", "tools": False, "chat_id": None})))
    assert fresh["chat_id"] and fresh["chat_id"] != original

    back = _result(ndjson(admin_client.post(
        "/api/agent/stream",
        json={"message": "снова первый", "tools": False, "chat_id": original})))
    assert back["chat_id"] == original

    def call_text(call):
        return " ".join([str(call.get("system") or "")]
                        + [str(item) for item in call["messages"]])

    # Третий ход видит историю ПЕРВОГО чата…
    assert "первый вопрос" in call_text(seen[2])
    # …а второй — чужой контекст не видит: диалоги не смешиваются.
    assert "первый вопрос" not in call_text(seen[1])

    assert len(database.get_chat_history(original, limit=50)) == 4
    assert len(database.get_chat_history(fresh["chat_id"], limit=50)) == 2


def test_agent_stream_without_chat_id_keeps_legacy_session_flow(admin_client, sandbox,
                                                                monkeypatch):
    """Клиенты без chat_id (Telegram, curl) работают как раньше."""
    from tests.agent_helpers import script

    seen = []
    script(monkeypatch, ["Раз.", "Два."], seen)
    first = _result(ndjson(admin_client.post(
        "/api/agent/stream", json={"message": "раз", "tools": False})))
    second = _result(ndjson(admin_client.post(
        "/api/agent/stream", json={"message": "два", "tools": False})))
    assert first["chat_id"] == second["chat_id"]


# ══════════════ офлайн-ход не теряет разговор ══════════════

def test_offline_turn_passes_history_to_local_model():
    from agenticseek.offline import OfflineTurn

    captured = []
    turn = OfflineTurn(
        "а сколько он весит?",
        local_model=lambda messages: captured.append(messages) or "Около 73 кг.",
        history=[{"role": "user", "content": "Расскажи про кита"},
                 {"role": "assistant", "content": "Синий кит весит около 73 тонн."}],
    )
    assert turn.run() == "Около 73 кг."
    roles = [item["role"] for item in captured[0]]
    assert roles == ["user", "assistant", "user"]
    assert "кит" in captured[0][0]["content"]
    assert captured[0][-1]["content"] == "а сколько он весит?"


def test_offline_search_query_includes_previous_user_message():
    from agenticseek.offline import OfflineTurn

    queries = []
    turn = OfflineTurn(
        "а где он живёт?",
        search=lambda query, limit: (queries.append(query) or ([], {})),
        history=[{"role": "user", "content": "Что такое синий кит?"}],
        force_search=True,
    )
    turn.run()
    assert queries and "синий кит" in queries[0]


# ══════════════ MCP: флаг чтения действует и в агентном ходе ══════════════

def test_mcp_read_flag_grants_non_admin_mcp_call(monkeypatch):
    import capabilities

    monkeypatch.setenv("MCP_ALLOW_USER_READ", "1")
    assert "mcp_call" in capabilities.scopes_for_role("user")
    _token, cap = capabilities.mint("email:a@b.c", "run-1", role="user",
                                    tools=["mcp_call", "terminal"])
    assert "mcp_call" in cap.tools


def test_mcp_scope_for_users_is_admin_only_without_flag():
    import capabilities

    assert "mcp_call" not in capabilities.scopes_for_role("user")
    assert "mcp_call" not in capabilities.scopes_for_role("guest")


def test_run_tool_scope_requests_mcp_for_users_only_with_flag(monkeypatch):
    import routes.agent as agent_routes

    monkeypatch.setenv("MCP_ALLOW_USER_READ", "1")
    scope = agent_routes._run_tool_scope(tools_allowed=True, use_tools=False,
                                         signed=True, admin=False, media_ready={})
    assert "mcp_call" in scope

    monkeypatch.delenv("MCP_ALLOW_USER_READ")
    scope = agent_routes._run_tool_scope(tools_allowed=True, use_tools=False,
                                         signed=True, admin=False, media_ready={})
    assert "mcp_call" not in scope


# ══════════════ фронтенд: инструменты по умолчанию, chat_id в каждом пути ════

def test_frontend_disables_agent_tools_only_when_explicitly_off():
    """Свежий браузер должен ходить с инструментами (MCP, скиллы, поиск)."""
    source = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
    assert "localStorage.getItem('nova_web_search') !== '0'" in source


def test_frontend_sends_chat_id_from_every_chat_path():
    app = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
    linux = (ROOT / "static" / "linux.js").read_text(encoding="utf-8")

    assert "function serverChatRequest()" in app
    # /send, /send_stream и /api/auto_search_stream — все три пути чата.
    assert app.count("...serverChatRequest()") >= 3
    # Агент (в т.ч. coding/stream) шлёт тот же chat_id.
    assert "...chatRef" in linux
    # Новый диалог сбрасывает и серверную половину истории (флаг живёт
    # в сторе и переживает перезагрузку страницы).
    assert "store.resetServer = true" in app
    assert "resetServer: parsed.resetServer === true" in app
    # Ответ сохраняется в чат, в котором ход начался.
    assert "}, turnChatId);" in app
