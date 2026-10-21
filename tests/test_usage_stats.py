"""Счётчик токенов, стоимости и лимитов Groq (бонус Б3)."""
import json

import pytest

import database
import groq_rotation
import routes.agent as agent_routes
import usage_stats


def _ndjson(response):
    return [json.loads(line) for line in response.get_data(as_text=True).splitlines() if line.strip()]


@pytest.fixture(autouse=True)
def clean_state():
    groq_rotation.reset_limits()
    yield
    groq_rotation.reset_limits()


@pytest.fixture
def db(tmp_path, monkeypatch, isolated_env):
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "usage.db"))
    monkeypatch.setattr(database, "_usage_ready", set())
    database.init_db()
    return database


def _stub_model(monkeypatch, answers):
    queue = list(answers)

    def fake_completion(messages, **kwargs):
        return (queue.pop(0) if queue else "готово"), {}

    def fake_stream(messages, **kwargs):
        yield "token", queue.pop(0) if queue else "готово"
        yield "done", ""

    monkeypatch.setattr(agent_routes, "chat_completion", fake_completion)
    monkeypatch.setattr(agent_routes, "chat_stream", fake_stream)
    return queue


# ══════════════ подсчёт токенов ══════════════

def test_estimate_tokens_by_length():
    assert usage_stats.estimate_tokens("") == 0
    assert usage_stats.estimate_tokens("abcd") == 1
    assert usage_stats.estimate_tokens("a" * 400) == 100


def test_usage_is_taken_from_provider():
    prompt, completion = usage_stats.tokens_from_usage(
        {"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150})
    assert (prompt, completion) == (120, 30)


def test_gemini_style_usage_is_understood():
    prompt, completion = usage_stats.tokens_from_usage(
        {"promptTokenCount": 11, "candidatesTokenCount": 7})
    assert (prompt, completion) == (11, 7)


def test_total_without_split_counts_as_prompt():
    assert usage_stats.tokens_from_usage({"total_tokens": 50}) == (50, 0)
    assert usage_stats.tokens_from_usage(None) == (None, None)


# ══════════════ деньги ══════════════

def test_free_providers_cost_nothing():
    assert usage_stats.cost_of("local_demo", "nova-local-1", 1000, 500) == 0.0
    assert usage_stats.cost_of("groq", "llama-3.3-70b", 1000, 500) == 0.0


def test_model_price_wins_over_provider_price():
    cost = usage_stats.cost_of("openrouter", "openai/gpt-4o-mini", 1_000_000, 1_000_000)
    assert cost == pytest.approx(0.75)          # 0.15 + 0.60


def test_unknown_provider_has_no_price():
    assert usage_stats.cost_of("unknown-provider", "unknown-model", 10, 10) is None
    assert usage_stats.format_cost(None) == "цена неизвестна"


def test_format_helpers():
    assert usage_stats.format_tokens(999) == "999"
    assert usage_stats.format_tokens(1200) == "1.2 тыс."
    assert usage_stats.format_tokens(2_400_000) == "2.4 млн"
    assert usage_stats.format_cost(0) == "бесплатно"
    assert usage_stats.format_cost(0.004) == "< $0.01"
    assert usage_stats.format_cost(1.234) == "$1.23"


# ══════════════ запись в базу ══════════════

def test_record_turn_saves_and_sums(db):
    chat_id = db.create_chat("Расход")
    row = usage_stats.record_turn(chat_id, "local_demo", "nova-local-1",
                                  prompt_text="a" * 400, answer_text="b" * 200)
    assert row["prompt_tokens"] == 100 and row["completion_tokens"] == 50
    assert row["total_tokens"] == 150
    assert row["chat"]["total_tokens"] == 150 and row["chat"]["turns"] == 1

    usage_stats.record_turn(chat_id, "local_demo", "nova-local-1",
                            prompt_text="", answer_text="x" * 40)
    totals = db.usage_totals(chat_id)
    assert totals["turns"] == 2 and totals["total_tokens"] >= 150


def test_record_turn_prefers_provider_usage(db):
    chat_id = db.create_chat("Точный расход")
    row = usage_stats.record_turn(chat_id, "groq", "llama-3.3-70b",
                                  prompt_text="a" * 1000, answer_text="b" * 1000,
                                  usage={"prompt_tokens": 12, "completion_tokens": 8})
    assert row["total_tokens"] == 20


def test_totals_are_separate_between_chats(db):
    first = db.create_chat("Первый")
    second = db.create_chat("Второй")
    usage_stats.record_turn(first, "local_demo", "m", "a" * 40, "b" * 40)
    assert db.usage_totals(first)["turns"] == 1
    assert db.usage_totals(second)["turns"] == 0


def test_chat_summary_has_labels(db):
    chat_id = db.create_chat("Подписи")
    usage_stats.record_turn(chat_id, "google_ai_studio", "gemini-2.0-flash",
                            usage={"prompt_tokens": 1000, "completion_tokens": 500})
    summary = usage_stats.chat_summary(chat_id)
    assert summary["tokens_label"].endswith("тыс.")
    assert "$" in summary["cost_label"]        # 1000/500 токенов ≈ доли цента


# ══════════════ лимиты Groq ══════════════

def test_record_limits_reads_groq_headers():
    groq_rotation.record_limits({
        "x-ratelimit-remaining-requests": "42",
        "x-ratelimit-limit-requests": "1000",
        "x-ratelimit-remaining-tokens": "12000",
        "x-ratelimit-limit-tokens": "20000",
    })
    status = groq_rotation.limits_status()
    assert status["remaining_tokens"] == 12000
    assert status["remaining_requests"] == 42


def test_limits_ignore_missing_headers():
    assert groq_rotation.record_limits({}) is None
    assert groq_rotation.limits_status()["remaining_tokens"] is None


def test_warning_when_tokens_almost_gone(monkeypatch):
    monkeypatch.setattr(groq_rotation, "GROQ_KEYS", [], raising=False)
    groq_rotation.record_limits({"x-ratelimit-remaining-tokens": "500"})
    warning = groq_rotation.limits_warning()
    assert "токенов" in warning and "Groq" in warning


def test_no_warning_when_limits_are_fine():
    groq_rotation.record_limits({"x-ratelimit-remaining-tokens": "90000",
                                 "x-ratelimit-remaining-requests": "500"})
    assert groq_rotation.limits_warning() == ""


def test_parse_retry_after():
    assert usage_stats.parse_retry_after("Please try again in 12.5s.") == 12
    assert usage_stats.parse_retry_after("try again in 1500ms") == 1
    assert usage_stats.parse_retry_after("try again in 2m") == 120
    assert usage_stats.parse_retry_after("нет тут времени") == 0


# ══════════════ сквозная проверка через агента ══════════════

def test_agent_result_reports_usage(google_client, sandbox, monkeypatch):
    _stub_model(monkeypatch, ["Привет! Чем помочь?"])
    events = _ndjson(google_client.post("/api/agent/stream", json={"message": "привет"}))
    usage_events = [event for event in events if event["type"] == "usage"]
    assert usage_events, "поток должен содержать событие usage"
    usage = usage_events[0]["usage"]
    assert usage["total_tokens"] > 0 and usage["chat"]["turns"] >= 1
    result = next(event for event in events if event["type"] == "result")
    assert result["usage"]["total_tokens"] == usage["total_tokens"]


def test_usage_endpoint(client):
    chat_id = database.create_chat("Эндпоинт")
    database.add_usage(chat_id, "groq", "llama-3.3-70b", 100, 50, 0.0)
    with client.session_transaction() as session:
        session["chat_id"] = chat_id
    payload = client.get("/api/usage").get_json()
    assert payload["chat"]["total_tokens"] == 150
    assert payload["chat"]["turns"] == 1
    assert "warning" in payload["groq"]
