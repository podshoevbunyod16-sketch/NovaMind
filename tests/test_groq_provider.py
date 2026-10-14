"""Regression tests for Groq model selection and API-key rotation."""
import ai_providers
import config
import groq_rotation


def test_groq_fallback_catalog_uses_current_models():
    model_ids = {item["id"] for item in config.PROVIDERS["groq"]["models"]}
    assert "openai/gpt-oss-120b" in model_ids
    assert "openai/gpt-oss-20b" in model_ids
    assert "qwen/qwen3.8-27b" in model_ids
    assert "llama-3.3-70b-versatile" not in model_ids
    assert "llama-3.1-8b-instant" not in model_ids


def test_rotation_returns_empty_when_all_keys_are_unavailable(monkeypatch):
    monkeypatch.setattr(groq_rotation, "GROQ_KEYS", [
        {"key": "invalid-test-key", "index": 1, "exhausted_at": None,
         "cooldown_seconds": 60, "invalid": True},
    ])
    monkeypatch.setattr(groq_rotation, "groq_key_index", 0)
    assert groq_rotation.get_groq_key() == ""


def test_rotation_does_not_return_a_key_in_cooldown(monkeypatch):
    monkeypatch.setattr(groq_rotation, "GROQ_KEYS", [
        {"key": "limited-test-key", "index": 1, "exhausted_at": 1000,
         "cooldown_seconds": 60, "invalid": False},
    ])
    monkeypatch.setattr(groq_rotation, "groq_key_index", 0)
    monkeypatch.setattr(groq_rotation.time, "time", lambda: 1010)
    assert groq_rotation.get_groq_key() == ""


def test_groq_bad_model_error_does_not_rotate_or_disable_key(monkeypatch):
    calls = []
    class Response:
        status_code = 400
        text = '{"error":{"message":"model is unavailable"}}'
    monkeypatch.setattr(ai_providers, "GROQ_KEYS_LIST", lambda: [{"key": "test"}])
    monkeypatch.setattr(ai_providers, "get_groq_key", lambda: "test-key")
    monkeypatch.setattr(ai_providers.requests, "post", lambda *a, **k: Response())
    monkeypatch.setattr(ai_providers, "mark_groq_key_exhausted",
                        lambda **kwargs: calls.append(kwargs))

    data, error = ai_providers.groq_request_with_rotation(
        "https://api.groq.com/openai/v1/chat/completions",
        {"model": "retired-model", "messages": [{"role": "user", "content": "hi"}]},
        {}, timeout=1,
    )
    assert data is None
    assert "Groq HTTP 400" in error
    assert calls == []


def test_groq_429_uses_short_cooldown_for_transient_limit(monkeypatch):
    calls = []
    class Response:
        status_code = 429
        text = '{"error":{"message":"rate limit reached per minute"}}'
    monkeypatch.setattr(ai_providers, "GROQ_KEYS_LIST", lambda: [{"key": "test"}])
    monkeypatch.setattr(ai_providers, "get_groq_key", lambda: "test-key")
    monkeypatch.setattr(ai_providers.requests, "post", lambda *a, **k: Response())
    monkeypatch.setattr(ai_providers, "mark_groq_key_exhausted",
                        lambda **kwargs: calls.append(kwargs))

    data, error = ai_providers.groq_request_with_rotation(
        "https://api.groq.com/openai/v1/chat/completions",
        {"model": "openai/gpt-oss-120b", "messages": [{"role": "user", "content": "hi"}]},
        {}, timeout=1,
    )
    assert data is None
    assert "429" in error
    assert calls == [{"cooldown_seconds": 60, "key": "test-key"}]



def test_groq_401_is_temporary(monkeypatch):
    calls = []
    class Response:
        status_code = 401
        text = '{"error":{"message":"unauthorized"}}'
    monkeypatch.setattr(ai_providers, "GROQ_KEYS_LIST", lambda: [{"key": "test"}])
    monkeypatch.setattr(ai_providers, "get_groq_key", lambda: "test-key")
    monkeypatch.setattr(ai_providers.requests, "post", lambda *a, **k: Response())
    monkeypatch.setattr(ai_providers, "mark_groq_key_exhausted", lambda **kwargs: calls.append(kwargs))
    _, error = ai_providers.groq_request_with_rotation(
        "https://api.groq.com/openai/v1/chat/completions",
        {"model": "openai/gpt-oss-20b", "messages": [{"role": "user", "content": "hi"}]},
        {}, timeout=1, max_retries=1)
    assert "временно" in error
    assert calls == [{"permanent": False, "cooldown_seconds": 60, "key": "test-key", "auth_failure": True}]


def test_retry_after_header_is_used():
    assert ai_providers._retry_after_seconds({"Retry-After": "120"}) == 120


def test_generic_quota_does_not_trigger_daily_cooldown():
    class Response:
        headers = {}
    assert ai_providers._groq_429_cooldown(Response(), "quota exceeded") == 60


def test_gemini_fallback_handles_missing_choices(monkeypatch):
    monkeypatch.setattr(ai_providers, "gemini_stream_request", lambda *a, **k: (None, "stream unavailable"))
    monkeypatch.setattr(ai_providers, "gemini_request", lambda *a, **k: ({"candidates": []}, None))
    assert list(ai_providers._raw_stream("google_ai_studio", "gemini-test",
        {"messages": [{"role": "user", "content": "hello"}]}, {"url": "unused"}, 1)) == []


def test_gemini_error_classifies_quota():
    class Response:
        text = "Quota exceeded"
        def json(self):
            return {"error": {"message": "Quota exceeded", "status": "RESOURCE_EXHAUSTED"}}
    assert "квота" in ai_providers._format_gemini_error(400, Response())
