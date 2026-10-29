"""
Поиск в интернете как функция Linux-окружения.

Идея: свежие данные доступны не отдельной кнопкой, а через ту же песочницу —
    * терминал:  nova search <запрос> / nova read <url>
    * агент:     {"action": "search", ...} и {"action": "open", "url": ...}
    * панель:    /api/linux/search, /api/linux/save, /api/linux/read
    * по коду:   mode=code — тот же grep, но по рабочей папке

Сеть в тестах не нужна: подменяем search_web и fetch_page_text.
"""
import json
import os

import pytest

import routes.agent as agent_routes
import routes.terminal as term_routes
import routes.search as search_routes


RESULTS = [
    {"title": "asyncio — документация", "url": "https://docs.python.org/3/library/asyncio.html",
     "snippet": "Asynchronous I/O, event loop, tasks and queues.", "host": "docs.python.org"},
    {"title": " asyncio на Хабре", "url": "https://habr.com/ru/post/1",
     "snippet": "Практика asyncio: gather, wait_for, отмена задач.", "host": "habr.com"},
    {"title": "PEP 492", "url": "https://peps.python.org/pep-0492/",
     "snippet": "Yields from coroutines and async/await syntax.", "host": "peps.python.org"},
]


@pytest.fixture
def web(monkeypatch):
    """Поиск и чтение страниц работают без интернета."""
    calls = []

    def fake_search(query, num=8, backends=None, on_progress=None):
        calls.append({"query": query, "num": num})
        trace = [{"backend": "searxng", "ok": True, "count": len(RESULTS), "ms": 120, "error": None}]
        return list(RESULTS), trace

    def fake_fetch(url, max_chars=6000):
        calls.append({"url": url, "max_chars": max_chars})
        # как настоящий fetch_page_text: (текст, ошибка)
        return f"Текст страницы {url}. Секретов нет. " * 12, None

    monkeypatch.setattr(search_routes, "search_web", fake_search)
    monkeypatch.setattr(search_routes, "fetch_page_text", fake_fetch)
    return calls


def _run(client, command):
    return client.post("/api/terminal/run", json={"command": command})


def _ndjson(response):
    return [json.loads(line) for line in response.get_data(as_text=True).splitlines() if line.strip()]


def _stub_model(monkeypatch, answers, seen=None):
    """Модель отвечает строками по очереди — потоком, как настоящая."""
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


# ══════════════ терминал ══════════════

def test_nova_search_prints_sources(admin_client, sandbox, web):
    result = _run(admin_client, "nova search python asyncio").get_json()
    assert result["code"] == 0
    assert "docs.python.org" in result["stdout"]
    assert "asyncio — документация" in result["stdout"]
    assert "Практика asyncio" in result["stdout"]
    assert web[0]["query"] == "python asyncio"


def test_nova_search_save_writes_note(admin_client, sandbox, web):
    result = _run(admin_client, "nova search asyncio --save").get_json()
    assert "💾 сохранено" in result["stdout"]
    saved = [name for name in os.listdir(sandbox / "notes" / "research")]
    assert len(saved) == 1 and saved[0].endswith("-search.md")
    body = (sandbox / "notes" / "research" / saved[0]).read_text(encoding="utf-8")
    assert "asyncio" in body and "docs.python.org" in body


def test_nova_read_page(admin_client, sandbox, web):
    result = _run(admin_client, "nova read https://example.com/doc").get_json()
    assert result["code"] == 0 and "Текст страницы" in result["stdout"]


def test_nova_read_requires_full_url(admin_client, sandbox, web):
    result = _run(admin_client, "nova read example.com").get_json()
    assert result["code"] == 2 and "полный адрес" in result["stderr"]


def test_nova_tools_lists_linux_commands(admin_client, sandbox):
    result = _run(admin_client, "nova tools").get_json()
    assert result["code"] == 0
    assert "grep" in result["stdout"] and "python3" in result["stdout"]
    assert "nova web search" in result["stdout"]


def test_nova_web_search_alias(admin_client, sandbox, web):
    result = _run(admin_client, "nova web search 'python asyncio'").get_json()
    assert result["code"] == 0 and "docs.python.org" in result["stdout"]


def test_nova_web_open_alias(admin_client, sandbox, web):
    result = _run(admin_client, "nova web open https://example.com/doc").get_json()
    assert result["code"] == 0 and "Текст страницы" in result["stdout"]


def test_nova_mcp_tools_catalog(admin_client, sandbox):
    result = _run(admin_client, "nova mcp tools").get_json()
    assert result["code"] == 0 and "MCP-инструменты" in result["stdout"]


def test_nova_call_is_explained_in_plain_terminal(admin_client, sandbox):
    result = _run(admin_client, "nova call file_read README.md").get_json()
    assert result["code"] == 2 and "шагах агента" in result["stderr"]


def test_python_gateway_really_calls_tool_in_terminal(admin_client, sandbox):
    (sandbox / "gateway.txt").write_text("real output", encoding="utf-8")
    result = _run(
        admin_client,
        "python3 -m novamind_cli call file_read "
        "'{\"path\":\"gateway.txt\"}'",
    ).get_json()
    assert result["code"] == 0
    assert "real output" in result["stdout"]


def test_python_gateway_bad_json_returns_real_error(admin_client, sandbox):
    result = _run(
        admin_client,
        "python3 -m novamind_cli call file_read '{\"path\":}'",
    ).get_json()
    assert result["code"] == 2
    assert "JSON" in result["stderr"]


def test_nova_unknown_verb_and_help(admin_client, sandbox, web):
    unknown = _run(admin_client, "nova delete everything").get_json()
    assert unknown["code"] == 2 and "неизвестная команда" in unknown["stderr"]
    help_text = _run(admin_client, "nova help").get_json()
    assert "nova search" in help_text["stdout"] and "nova read" in help_text["stdout"]


def test_nova_empty_query_is_explained(admin_client, sandbox, web):
    result = _run(admin_client, "nova search").get_json()
    assert result["code"] == 2 and "укажите запрос" in result["stderr"]


def test_terminal_help_lists_nova(admin_client, sandbox):
    text = admin_client.get("/api/terminal/help").get_json()["text"]
    assert "nova search" in text and "nova read" in text


# ══════════════ эндпоинты панели ══════════════

def test_linux_search_endpoint(google_client, sandbox, web):
    data = google_client.post("/api/linux/search", json={"query": "asyncio"}).get_json()
    assert data["mode"] == "web" and data["count"] == 3
    assert data["backend"] == "searxng"
    assert data["results"][0]["host"] == "docs.python.org"


def test_linux_search_requires_signed_in(client, sandbox, web):
    assert client.post("/api/linux/search", json={"query": "x"}).status_code == 403


def test_linux_search_rejects_empty_query(google_client, sandbox, web):
    assert google_client.post("/api/linux/search", json={"query": "  "}).status_code == 400


def test_linux_search_by_code_uses_grep(google_client, sandbox, web):
    (sandbox / "app.py").write_text("import asyncio\n\nasync def main():\n    await asyncio.sleep(1)\n",
                                    encoding="utf-8")
    data = google_client.post("/api/linux/search", json={"query": "asyncio", "mode": "code"}).get_json()
    assert data["mode"] == "code" and data["count"] >= 2
    assert any(match["path"].endswith("app.py") for match in data["results"])
    assert any("asyncio.sleep" in match["snippet"] for match in data["results"])


def test_linux_save_writes_research_note(admin_client, sandbox, web):
    data = admin_client.post("/api/linux/save", json={
        "query": "asyncio", "results": RESULTS[:2], "note": "забрать в проект",
    }).get_json()
    body = (sandbox / data["path"]).read_text(encoding="utf-8")
    assert "habr.com" in body and "забрать в проект" in body


def test_linux_read_endpoint(admin_client, sandbox, web):
    data = admin_client.post("/api/linux/read", json={"url": "https://example.com/a"}).get_json()
    assert "Текст страницы" in data["text"]
    assert admin_client.post("/api/linux/read", json={"url": "ftp://x"}).status_code == 400


# ══════════════ агент ══════════════

from tests.agent_helpers import answer_of, events_of, model_text, ndjson, script


def test_agent_searches_and_answers_with_sources(admin_client, sandbox, monkeypatch, web):
    """Блок ```web_search``` идёт в настоящий поиск, ссылки попадают в ответ."""
    script(monkeypatch, [
        "```web_search\npython asyncio\n```",
        "По итогам поиска: docs.python.org [1] и PEP 492 [3]",
    ])
    events = ndjson(admin_client.post("/api/agent/stream",
                                      json={"message": "что нового про asyncio?"}))
    tool = events_of(events, "tool")[0]
    assert tool["name"] == "web_search"
    assert "docs.python.org" in tool["output"] and "habr.com" in tool["output"]
    assert {"query": "python asyncio"} in [{"query": item["query"]} for item in web]

    sources = events_of(events, "sources")[0]["sources"]
    assert [item["url"] for item in sources] == [item["url"] for item in RESULTS]
    assert sources[0]["title"] == "asyncio — документация"
    assert "docs.python.org" in answer_of(events)


def test_agent_opens_page_after_search(admin_client, sandbox, monkeypatch, web):
    script(monkeypatch, [
        "```web_search\nasyncio\n```",
        "```web_open\nhttps://peps.python.org/pep-0492/\n```",
        "Прочитал PEP 492",
    ])
    events = ndjson(admin_client.post("/api/agent/stream", json={"message": "найди документацию по asyncio"}))
    assert [tool["name"] for tool in events_of(events, "tool")] == ["web_search", "web_open"]
    opened = events_of(events, "tool")[1]
    assert "Текст страницы" in opened["output"]
    assert any(item.get("url") == "https://peps.python.org/pep-0492/" for item in web)
    assert "Прочитал PEP 492" in answer_of(events)


def test_search_step_is_persisted_in_run_log(admin_client, sandbox, monkeypatch, web):
    """«Что делал агент»: шаг поиска сохраняется в базу запусков."""
    script(monkeypatch, [
        "```web_search\nasyncio cancellation\n```",
        "Справка собрана",
    ])
    events = ndjson(admin_client.post("/api/agent/stream", json={"message": "найди asyncio cancellation"}))
    run_id = next(event for event in events if event["type"] == "result")["run_id"]
    run = admin_client.get(f"/api/agent/runs/{run_id}").get_json()["run"]
    assert [step["tool"] for step in run["steps_log"]] == ["web_search"]
    assert run["steps_log"][0]["ok"] == 1


def test_agent_reports_empty_search(admin_client, sandbox, monkeypatch):
    monkeypatch.setattr(search_routes, "search_web", lambda query, num=8, **kw: ([], []))
    script(monkeypatch, [
        "```web_search\nнесуществующее\n```",
        "Ничего не нашлось",
    ])
    events = ndjson(admin_client.post("/api/agent/stream", json={"message": "найди несуществующее"}))
    tool = events_of(events, "tool")[0]
    assert tool["code"] == 1 and "ничего не нашёл" in tool["output"]
    assert answer_of(events) == "Ничего не нашлось"


# ══════════════ кнопка «Поиск» (автопоиск) ══════════════

def test_search_button_goes_through_linux(google_client, sandbox, monkeypatch, web):
    """search=true: поиск выполняется безусловно, ещё до выбора исполнителя."""
    seen = []
    script(monkeypatch, ["Цена — 12 500 ₽ [1], подробности [2]."], seen)
    events = ndjson(google_client.post("/api/agent/stream",
                                       json={"message": "сколько стоит asyncio?", "search": True}))
    kinds = [event["type"] for event in events]

    assert events[0]["type"] == "stage" and events[0]["scene"] == "search"
    tools = events_of(events, "tool")
    assert tools[0]["name"] == "web_search"
    assert tools[0]["command"] == (
        "python3 -m novamind_cli call web_search "
        "'{\"query\":\"сколько стоит asyncio\",\"limit\":5}'"
    )
    assert all("nova " not in tool["command"] for tool in tools)
    assert [tool["name"] for tool in tools[1:]] == ["web_open", "web_open", "web_open"]
    assert "sources" in kinds and kinds.index("sources") < kinds.index("token")

    # выдержки сохранены в рабочую папку
    notes = list((sandbox / "notes" / "research").glob("*-search.md"))
    assert notes and "docs.python.org" in notes[0].read_text(encoding="utf-8")
    assert any(event["type"] == "step" and "Сохранил" in event["text"] for event in events)

    # модель получила найденное и прочитанные страницы
    observation = model_text(seen)
    assert "[1] asyncio — документация" in observation and "Текст страницы" in observation

    result = next(event for event in events if event["type"] == "result")
    assert result["searched"] is True and len(result["sources"]) == 3
    assert "12 500" in answer_of(events)
    assert len(seen) == 1, "поиск делает кнопка, а не модель"


def test_search_button_can_search_again(google_client, sandbox, monkeypatch, web):
    """Кнопка + собственный блок модели: два поиска, источники без дублей."""
    script(monkeypatch, [
        "```web_search\nasyncio 3.13 changes\n```",
        "Итог по двум поискам [1]",
    ])
    events = ndjson(google_client.post("/api/agent/stream",
                                       json={"message": "что нового про asyncio", "search": True}))
    searches = [tool for tool in events_of(events, "tool") if tool["name"] == "web_search"]
    assert len(searches) == 2
    result = next(event for event in events if event["type"] == "result")
    assert len(result["sources"]) == 3


def test_search_button_with_empty_results(google_client, sandbox, monkeypatch):
    monkeypatch.setattr(search_routes, "search_web", lambda query, num=8, **kw: ([], []))
    seen = []
    script(monkeypatch, ["Отвечу из базы знаний"], seen)
    events = ndjson(google_client.post("/api/agent/stream",
                                       json={"message": "редкий вопрос", "search": True}))
    assert any(event["type"] == "step" and "ничего не дал" in event["text"] for event in events)
    assert "ничего не нашёл" in model_text(seen)
    assert answer_of(events) == "Отвечу из базы знаний"


def test_search_button_works_without_workspace(client, monkeypatch, web):
    """Гость: поиск и чтение страниц работают, заметка в папку не пишется."""
    script(monkeypatch, ["Готово"])
    events = ndjson(client.post("/api/agent/stream",
                                json={"message": "найди asyncio", "search": True}))
    assert events_of(events, "tool")[0]["name"] == "web_search"
    assert not any(event["type"] == "step" and "Сохранил" in event["text"] for event in events)
    assert answer_of(events) == "Готово"


# ══════════════ MCP ══════════════

def test_mcp_call_reaches_model_answer(admin_client, sandbox, monkeypatch):
    import mcp_registry

    monkeypatch.setattr(mcp_registry, "connected_tools", lambda: [{"server_id": "exa"}])
    monkeypatch.setattr(mcp_registry, "call_tool",
                        lambda server_id, tool, arguments: {"answer": "42"})
    monkeypatch.setenv("AGENT_CONFIRM", "off")
    seen = []
    script(monkeypatch, [
        '```mcp_call\n{"server_id": "exa", "tool": "search", "arguments": {"query": "x"}}\n```',
        "Итог: 42",
    ], seen)
    events = ndjson(admin_client.post("/api/agent/stream", json={"message": "узнай ответ через mcp"}))
    tool = events_of(events, "tool")[0]
    assert tool["name"] == "mcp_call" and "42" in tool["output"]
    assert answer_of(events) == "Итог: 42"
    assert "42" in model_text(seen, -1)


def test_mcp_tool_is_absent_without_servers(admin_client, sandbox, monkeypatch):
    import mcp_registry

    monkeypatch.setattr(mcp_registry, "connected_tools", lambda: [])
    seen = []
    script(monkeypatch, ["Нечего вызывать"], seen)
    ndjson(admin_client.post("/api/agent/stream", json={"message": "найди через mcp"}))
    offered = model_text(seen).split("ДОСТУПНЫЕ")[-1]
    assert "mcp_call" not in offered


# ══════════════ устойчивость ══════════════

def test_broken_tool_does_not_break_the_stream(admin_client, sandbox, monkeypatch, web):
    """Ошибка бэкенда поиска возвращается модели, а не обрывает поток."""
    def boom(query, num=8, **kw):
        raise RuntimeError("backend down")

    monkeypatch.setattr(search_routes, "search_web", boom)
    script(monkeypatch, [
        "```web_search\nasyncio\n```",
        "Поиск недоступен, отвечу по памяти",
    ])
    events = ndjson(admin_client.post("/api/agent/stream", json={"message": "найди asyncio"}))
    tool = events_of(events, "tool")[0]
    assert tool["code"] == 1 and "не удался" in tool["output"]
    assert answer_of(events) == "Поиск недоступен, отвечу по памяти"
    assert events[-1]["type"] == "done"
