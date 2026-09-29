"""
Поиск в интернете и единый слой ИИ — проверяются на реальном HTTP.

Внешний интернет в CI/песочнице может быть недоступен, поэтому рядом
поднимается локальный сервер, который притворяется:
  * SearXNG (JSON-выдача),
  * Википедией (/w/api.php),
  * страницей сайта (HTML для чтения),
  * OpenAI-compatible рантаймом (llama-server / Ollama): /v1/models и
    /v1/chat/completions, включая SSE-поток.

Так проверяются настоящие ветки кода (requests, парсинг, NDJSON-поток),
а не заглушки.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import config
import local_llm
import routes.search as search_routes
from ai_providers import chat_completion, chat_stream


# ══════════════════════════════════════════════════════════════════
# ЛОКАЛЬНЫЙ «ИНТЕРНЕТ»
# ══════════════════════════════════════════════════════════════════

PAGES = {
    "1": "<html><head><title>Цены</title></head><body><h1>Обзор рынка</h1>"
         "<p>Средняя цена в 2026 году составляет 12 500 рублей.</p>"
         "<script>var junk = 1;</script></body></html>",
    "2": "<html><body><p>Второй источник: доставка занимает 3 дня.</p></body></html>",
}

SEARCH_RESULTS = [
    {"title": "Обзор рынка 2026", "url": "{base}/page/1", "content": "Средняя цена 12 500 рублей."},
    {"title": "Условия доставки", "url": "{base}/page/2", "content": "Доставка 3 дня."},
]


class MockHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # тишина в логах pytest
        pass

    def _send(self, body, status=200, content_type="application/json", chunked=False):
        payload = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        if chunked:
            self.send_header("Transfer-Encoding", "chunked")
        else:
            self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if chunked:
            for index in range(0, len(payload), 64):
                block = payload[index:index + 64]
                self.wfile.write(b"%x\r\n%s\r\n" % (len(block), block))
            self.wfile.write(b"0\r\n\r\n")
        else:
            self.wfile.write(payload)

    def _base(self):
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def do_GET(self):
        path = self.path.split("?")[0]

        if path == "/search":  # SearXNG
            self._send(json.dumps({
                "results": [dict(item, url=item["url"].format(base=self._base())) for item in SEARCH_RESULTS]
            }))
            return

        if path == "/broken/search":  # инстанс, который всегда падает
            self._send("nope", status=503, content_type="text/plain")
            return

        if path == "/w/api.php":  # Википедия
            self._send(json.dumps({"query": {"search": [
                {"title": "Тестовая статья", "snippet": "Короткое <em>описание</em> статьи."},
            ]}}))
            return

        if path.startswith("/page/"):
            page_id = path.rsplit("/", 1)[-1]
            if page_id in PAGES:
                self._send(PAGES[page_id], content_type="text/html; charset=utf-8")
            else:
                self._send("not found", status=404, content_type="text/plain")
            return

        if path == "/v1/models":  # локальный llama-server
            self._send(json.dumps({"data": [
                {"id": "mock-local-7b", "name": "Mock Local 7B", "context_length": 8192},
            ]}))
            return

        self._send("{}", status=404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw or b"{}")
        except ValueError:
            payload = {}

        if self.path.split("?")[0] != "/v1/chat/completions":
            self._send("{}", status=404)
            return

        question = ""
        for message in reversed(payload.get("messages") or []):
            if message.get("role") == "user":
                question = str(message.get("content") or "")
                break

        if payload.get("stream"):
            chunks = []
            # «План рассуждений» для предпрохода и обычный ответ различаются по промпту.
            if "ход рассуждений" in question:
                text = "План: проверить источник, затем вывести цену."
            elif "12 500" in question or "Данные из интернета" in question:
                text = "По данным источников: 12 500 рублей, доставка 3 дня."
            else:
                text = f"Локальный ответ на: {question[:40]}"
            for piece in [text[i:i + 12] for i in range(0, len(text), 12)]:
                chunks.append("data: " + json.dumps({
                    "choices": [{"delta": {"content": piece}}]
                }, ensure_ascii=False) + "\n\n")
            chunks.append("data: [DONE]\n\n")
            self._send("".join(chunks), content_type="text/event-stream", chunked=True)
            return

        self._send(json.dumps({
            "choices": [{"message": {"role": "assistant",
                                     "content": f"Локальный ответ на: {question[:40]}"}}]
        }))


class MockServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        """requests закрывает keep-alive соединения — это не ошибка теста."""
        import sys
        if sys.exc_info()[0] is ConnectionResetError:
            return
        super().handle_error(request, client_address)


@pytest.fixture(scope="module")
def upstream():
    server = MockServer(("127.0.0.1", 0), MockHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


@pytest.fixture
def mock_search(monkeypatch, upstream):
    """Поиск идёт только в локальный mock: searxng → wiki."""
    monkeypatch.setattr(search_routes, "SEARXNG_INSTANCES", [upstream])
    monkeypatch.setenv("SEARCH_BACKENDS", "searxng,wiki")
    monkeypatch.setattr(search_routes, "READER_TIMEOUT", 2.0)
    return upstream


@pytest.fixture
def local_runtime(monkeypatch, upstream):
    """Активная модель — локальный OpenAI-compatible сервер."""
    monkeypatch.setitem(config.PROVIDERS, "openai_compatible", {
        **config.PROVIDERS["openai_compatible"],
        "url": upstream + "/v1/chat/completions",
        "max_tokens": 1024,
    })
    monkeypatch.setattr(config, "current_provider", "openai_compatible")
    monkeypatch.setattr(config, "current_model", "mock-local-7b")
    return upstream


# ══════════════════════════════════════════════════════════════════
# БЭКЕНДЫ ПОИСКА
# ══════════════════════════════════════════════════════════════════

def test_searxng_backend_returns_real_results(mock_search):
    results, trace = search_routes.search_web("цены 2026")
    assert len(results) == 2
    assert results[0]["title"] == "Обзор рынка 2026"
    assert results[0]["url"].endswith("/page/1")
    assert results[0]["host"] == f"127.0.0.1:{mock_search.rsplit(':', 1)[-1]}"
    assert trace[0]["backend"] == "searxng" and trace[0]["ok"] is True


def test_search_falls_back_to_wikipedia(monkeypatch, upstream):
    monkeypatch.setattr(search_routes, "SEARXNG_INSTANCES", [upstream + "/broken"])
    monkeypatch.setenv("WIKI_API_BASES", upstream + "/w/api.php")
    monkeypatch.setenv("SEARCH_BACKENDS", "searxng,wiki")
    results, trace = search_routes.search_web("тест")
    assert [entry["backend"] for entry in trace] == ["searxng", "wiki"]
    assert trace[0]["ok"] is False and trace[1]["ok"] is True
    assert results[0]["title"].startswith("Тестовая статья")
    assert "<em>" not in results[0]["snippet"]
    # Ссылка на статью: корень сайта + /wiki/ + URL-encoded заголовок
    assert results[0]["url"] == upstream + "/wiki/%D0%A2%D0%B5%D1%81%D1%82%D0%BE%D0%B2%D0%B0%D1%8F_%D1%81%D1%82%D0%B0%D1%82%D1%8C%D1%8F"


def test_wiki_site_root_strips_media_wiki_prefix():
    assert search_routes._wiki_site_root("https://ru.wikipedia.org/w/api.php") == "https://ru.wikipedia.org"
    assert search_routes._wiki_site_root("https://en.wikipedia.org/w/rest.php") == "https://en.wikipedia.org"


def test_search_reports_failure_when_everything_is_down(monkeypatch):
    monkeypatch.setattr(search_routes, "SEARXNG_INSTANCES", ["http://127.0.0.1:1"])
    monkeypatch.setenv("SEARCH_BACKENDS", "searxng")
    results, trace = search_routes.search_web("тест")
    assert results == []
    assert trace and trace[0]["ok"] is False and trace[0]["error"]


def test_fetch_page_text_strips_markup(mock_search):
    text, error = search_routes.fetch_page_text(mock_search + "/page/1")
    assert error is None
    assert "Средняя цена в 2026 году" in text
    assert "var junk" not in text
    assert "<" not in text


def test_search_health_endpoint(client, mock_search):
    data = client.get("/api/search/health?refresh=1").get_json()
    assert data["ok"] is True
    assert "searxng" in data["alive"]
    assert {entry["backend"] for entry in data["backends"]} == {"searxng", "wiki"}


# ══════════════════════════════════════════════════════════════════
# ЕДИНЫЙ СЛОЙ ИИ
# ══════════════════════════════════════════════════════════════════

def test_chat_completion_uses_local_openai_compatible_server(local_runtime):
    reply, meta = chat_completion([{"role": "user", "content": "привет"}])
    assert reply.startswith("Локальный ответ на:")
    assert meta["provider"] == "openai_compatible"
    assert meta["model"] == "mock-local-7b"


def test_local_model_catalog_is_discovered(local_runtime):
    from ai_providers import fetch_available_models
    config.MODEL_CACHE.pop("openai_compatible", None)
    models = fetch_available_models("openai_compatible", force=True)
    assert [m["id"] for m in models] == ["mock-local-7b"]


def test_reasoning_prepass_streams_before_answer(local_runtime):
    kinds = []
    reasoning = []
    answer = []
    for kind, chunk in chat_stream([{"role": "user", "content": "посчитай"}], reasoning=True):
        kinds.append(kind)
        if kind == "reasoning":
            reasoning.append(chunk)
        elif kind == "token":
            answer.append(chunk)
    assert "".join(reasoning).startswith("План:")
    assert "Локальный ответ" in "".join(answer)
    assert kinds.index("reasoning") < kinds.index("token")
    assert kinds[-1] == "done"


def test_offline_model_hides_service_instructions():
    """Офлайн-модель не должна показывать пользователю служебные вставки."""
    reply = local_llm.complete([{"role": "user", "content":
        "сколько стоит iPhone 17\n\n[Поиск в интернете не дал результатов. "
        "Отвечай из своих знаний с пометкой (из базы знаний).]"}])
    assert "iPhone 17" in reply
    assert "[Поиск в интернете" not in reply
    assert "из базы знаний" not in reply


def test_clean_prompt_strips_context_blocks():
    assert local_llm.clean_prompt(
        "Вопрос: курс доллара\n\nДанные из интернета:\n### [1] Источник"
    ) == "курс доллара"


def test_offline_model_is_used_when_no_keys_configured(monkeypatch):
    monkeypatch.setattr(config, "current_provider", "groq")
    monkeypatch.setattr(config, "current_model", "llama-3.3-70b-versatile")
    import groq_rotation
    monkeypatch.setattr(groq_rotation, "GROQ_KEYS", [])
    reply, meta = chat_completion([{"role": "user", "content": "2+2=?"}])
    assert meta["provider"] == "local_demo" and meta["offline"] is True
    assert "2+2 = **4**" in reply


# ══════════════════════════════════════════════════════════════════
# ПОТОК ПОИСКА (то, что видит интерфейс)
# ══════════════════════════════════════════════════════════════════

def _read_ndjson(response):
    return [json.loads(line) for line in response.get_data(as_text=True).splitlines() if line.strip()]


def test_auto_search_stream_shows_scene_steps_sources_and_answer(client, mock_search, local_runtime):
    response = client.post("/api/auto_search_stream", json={"message": "сколько стоит?", "force": True})
    assert response.status_code == 200
    assert response.mimetype == "application/x-ndjson"

    events = _read_ndjson(response)
    types = [event["type"] for event in events]

    # 1. Сначала сцена поиска с подписью «Поищу в интернете»
    first_stage = next(event for event in events if event["type"] == "stage")
    assert first_stage["scene"] == "search"
    assert first_stage["title"] == "Поищу в интернете"
    assert types[0] == "stage"

    # 2. Шаги поиска
    assert "step" in types
    step_texts = " ".join(event["text"] for event in events if event["type"] == "step")
    assert "searxng" in step_texts

    # 3. Источники приходят до ответа
    sources_event = next(event for event in events if event["type"] == "sources")
    assert len(sources_event["sources"]) == 2
    assert types.index("sources") < types.index("token")

    # 4. Ответ печатается токенами
    answer = "".join(event["token"] for event in events if event["type"] == "token")
    assert "12 500" in answer

    # 5. Финальные события
    result = next(event for event in events if event["type"] == "result")
    assert result["searched"] is True and len(result["sources"]) == 2
    assert types[-1] == "done"
    assert events[-1]["searched"] is True

    # 6. Сцена ответа объявляется до токенов
    write_stage = next(event for event in events
                       if event["type"] == "stage" and event.get("scene") == "write")
    assert types.index("stage") < types.index("token")
    assert write_stage["title"]


def test_search_stream_answers_from_knowledge_when_backends_fail(client, monkeypatch, local_runtime):
    monkeypatch.setattr(search_routes, "SEARXNG_INSTANCES", ["http://127.0.0.1:1"])
    monkeypatch.setenv("SEARCH_BACKENDS", "searxng")

    events = _read_ndjson(client.post("/api/auto_search_stream", json={"message": "вопрос", "force": True}))
    result = next(event for event in events if event["type"] == "result")
    assert result["searched"] is False
    assert result["sources"] == []
    answer = "".join(event["token"] for event in events if event["type"] == "token")
    assert answer.strip()
    assert events[-1]["type"] == "done" and events[-1]["searched"] is False


def test_search_stream_recovers_when_stream_dies_midway(client, monkeypatch, mock_search, local_runtime):
    """Поток от модели оборвался — ответ всё равно должен дойти до пользователя."""
    real_stream = search_routes.chat_stream

    def dying_stream(messages, **kwargs):
        yield "token", "Часть ответа "
        raise ConnectionError("провайдер закрыл соединение")

    monkeypatch.setattr(search_routes, "chat_stream", dying_stream)
    monkeypatch.setattr(search_routes, "build_search_query", lambda message: "цены 2026")

    events = _read_ndjson(client.post("/api/auto_search_stream", json={"message": "сколько стоит?", "force": True}))
    types = [event["type"] for event in events]
    assert "token" in types
    # Сервер сам дочитывает ответ обычным запросом и кладёт его в result.reply
    result = next(event for event in events if event["type"] == "result")
    assert result["searched"] is True and result["reply"]
    assert len(result["sources"]) == 2
    assert events[-1]["type"] == "done"


def test_search_stream_falls_back_to_sources_when_model_is_dead(client, monkeypatch, mock_search, local_runtime):
    """Модель молчит и не отвечает даже без стрима — показываем найденное."""
    def dead_stream(messages, **kwargs):
        yield "error", "HTTP 503: upstream unavailable"
        yield "done", ""

    monkeypatch.setattr(search_routes, "chat_stream", dead_stream)
    monkeypatch.setattr(search_routes, "chat_completion",
                        lambda messages, **kwargs: (None, "HTTP 503: upstream unavailable"))
    monkeypatch.setattr(search_routes, "build_search_query", lambda message: "цены 2026")

    events = _read_ndjson(client.post("/api/auto_search_stream", json={"message": "сколько стоит?", "force": True}))
    result = next(event for event in events if event["type"] == "result")
    answer = result["reply"]

    # Данные не теряются: в ответе есть сами найденные источники
    assert "Обзор рынка" in answer and "Условия доставки" in answer
    assert len(result["sources"]) == 2
    assert events[-1]["type"] == "done"
    steps = " ".join(event.get("text", "") for event in events if event["type"] == "step")
    assert "503" in steps


def test_search_stream_rejects_empty_message(client):
    assert client.post("/api/auto_search_stream", json={"message": "  "}).status_code == 400


def test_web_search_json_endpoint_returns_sources(client, mock_search, local_runtime):
    data = client.post("/api/web_search_groq", json={"message": "цены"}).get_json()
    assert data["reply"]
    assert len(data["sources"]) == 2
    assert data["sources"][0]["url"].endswith("/page/1")


def test_auto_search_json_endpoint(client, mock_search, local_runtime):
    data = client.post("/api/auto_search", json={"message": "цены"}).get_json()
    assert data["needs_search"] is True
    assert data["reply"]
    assert data["trace"][0]["backend"] == "searxng"


# ══════════════════════════════════════════════════════════════════
# ЧАТ
# ══════════════════════════════════════════════════════════════════

def test_send_stream_streams_local_llama_tokens(client, local_runtime):
    response = client.post("/send_stream", json={"message": "привет", "reasoning": False})
    events = _read_ndjson(response)
    tokens = "".join(event["token"] for event in events if "token" in event)
    assert "Локальный ответ" in tokens
    done = events[-1]
    assert done["done"] is True and done["has_reply"] is True
    assert done["model"] == "mock-local-7b"


def test_send_fallback_endpoint_works(client, local_runtime):
    data = client.post("/send", json={"message": "привет"}).get_json()
    assert data["reply"].startswith("Локальный ответ")
    assert data["chat_id"]


def test_send_reports_error_instead_of_empty_reply(client, monkeypatch):
    """Если провайдер недоступен — пользователь видит причину, а не пустоту."""
    monkeypatch.setitem(config.PROVIDERS, "openai_compatible", {
        **config.PROVIDERS["openai_compatible"],
        "url": "http://127.0.0.1:1/v1/chat/completions",
    })
    monkeypatch.setattr(config, "current_provider", "openai_compatible")
    monkeypatch.setattr(config, "current_model", "dead-model")
    response = client.post("/send", json={"message": "привет"})
    assert response.status_code == 502
    assert response.get_json()["error"]


def test_ai_status_endpoint_reports_offline_model(client, monkeypatch):
    monkeypatch.setattr(config, "current_provider", "local_demo")
    monkeypatch.setattr(config, "current_model", local_llm.MODEL_ID)
    data = client.get("/api/ai/status").get_json()
    assert data["provider"] == "local_demo"
    assert data["offline"] is True
    assert data["configured"] is True


def test_pages_render_with_liquid_glass(client):
    for path in ("/", "/chat", "/settings", "/admin/login", "/composio"):
        body = client.get(path).get_data(as_text=True)
        assert "liquid-glass.css" in body, path
        assert "lg-grain" in body or path == "/admin/login", path
