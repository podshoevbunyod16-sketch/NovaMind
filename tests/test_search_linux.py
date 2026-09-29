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
        return f"Текст страницы {url}. Секретов нет."[:max_chars]

    monkeypatch.setattr(search_routes, "search_web", fake_search)
    monkeypatch.setattr(search_routes, "fetch_page_text", fake_fetch)
    return calls


def _run(client, command):
    return client.post("/api/terminal/run", json={"command": command})


def _ndjson(response):
    return [json.loads(line) for line in response.get_data(as_text=True).splitlines() if line.strip()]


def _stub_model(monkeypatch, answers):
    queue = list(answers)
    monkeypatch.setattr(agent_routes, "chat_completion",
                        lambda messages, **kw: ((queue.pop(0) if queue else ""), {}))


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

def test_agent_searches_and_answers_with_sources(admin_client, sandbox, monkeypatch, web):
    _stub_model(monkeypatch, [
        "План: поищу свежие данные и отвечу",
        '{"action":"search","query":"python asyncio"}',
        '{"action":"answer","text":"По итогам поиска: docs.python.org и PEP 492"}',
    ])
    events = _ndjson(admin_client.post("/api/agent/stream", json={"message": "что нового про asyncio?"}))
    tool = next(event for event in events if event["type"] == "tool")
    assert tool["name"] == "search"
    assert tool["command"] == "nova search python asyncio"
    assert len(tool["results"]) == 3 and tool["results"][1]["host"] == "habr.com"
    answer = "".join(event["token"] for event in events if event["type"] == "token")
    assert "docs.python.org" in answer


def test_agent_opens_page_after_search(admin_client, sandbox, monkeypatch, web):
    _stub_model(monkeypatch, [
        "План: найду и прочитаю",
        '{"action":"search","query":"asyncio"}',
        '{"action":"open","url":"https://peps.python.org/pep-0492/"}',
        '{"action":"answer","text":"Прочитал PEP 492"}',
    ])
    events = _ndjson(admin_client.post("/api/agent/stream", json={"message": "разберись с asyncio"}))
    names = [event.get("name") for event in events if event["type"] == "tool"]
    assert names == ["search", "open"]
    opened = next(event for event in events if event["type"] == "tool" and event["name"] == "open")
    assert "Текст страницы" in opened["output"]


def test_agent_search_step_goes_to_task_log(admin_client, sandbox, monkeypatch, web):
    _stub_model(monkeypatch, [
        "План: заведу задачу, найду, сохраню",
        '{"action":"task","title":"Собрать справку по asyncio","detail":"выжимка для проекта"}',
        '{"action":"search","query":"asyncio cancellation"}',
        '{"action":"write","path":"notes/research/asyncio.md","content":"выжимка"}',
        '{"action":"task_done","note":"справка собрана"}',
        '{"action":"answer","text":"Справка в notes/research/asyncio.md"}',
    ])
    events = _ndjson(admin_client.post("/api/agent/stream", json={"message": "собери справку по asyncio"}))
    result = next(event for event in events if event["type"] == "result")
    task = next(item for item in admin_client.get("/api/tasks").get_json()["tasks"]
                if item["id"] == result["task_id"])
    titles = [step["title"] for step in task["steps"]]
    assert any("Поиск: asyncio cancellation" in title for title in titles)
    assert (sandbox / "notes" / "research" / "asyncio.md").exists()


def test_agent_reports_empty_search(admin_client, sandbox, monkeypatch):
    monkeypatch.setattr(search_routes, "search_web", lambda query, num=8, **kw: ([], []))
    _stub_model(monkeypatch, [
        "План: поищу",
        '{"action":"search","query":"несуществующее"}',
        '{"action":"answer","text":"Ничего не нашлось"}',
    ])
    events = _ndjson(admin_client.post("/api/agent/stream", json={"message": "найди несуществующее"}))
    assert any(event["type"] == "step" and "ничего не дал" in event["text"] for event in events)
    assert not [event for event in events if event["type"] == "tool"]
