"""Regression tests: provider selection is strict and never fails over."""
import ai_providers
import config


def test_failover_targets_contains_only_selected_provider(monkeypatch):
    monkeypatch.setattr(ai_providers, "resolve_target", lambda *a, **k: (
        "groq", "openai/gpt-oss-120b", config.PROVIDERS["groq"]))
    assert ai_providers.free_failover_targets() == [
        ("groq", "openai/gpt-oss-120b")
    ]


def test_chat_completion_does_not_switch_after_provider_error(monkeypatch):
    calls = []

    def fail_selected(messages, provider=None, model=None, **kwargs):
        calls.append((provider, model))
        return None, "Groq HTTP 429: rate limit"

    monkeypatch.setattr(ai_providers, "resolve_target", lambda *a, **k: (
        "groq", "openai/gpt-oss-120b", config.PROVIDERS["groq"]))
    monkeypatch.setattr(ai_providers, "_chat_completion_once", fail_selected)

    text, error = ai_providers.chat_completion(
        [{"role": "user", "content": "вопрос"}]
    )

    assert text is None
    assert "Groq HTTP 429" in error
    assert calls == [("groq", "openai/gpt-oss-120b")]


def test_chat_stream_does_not_switch_after_error(monkeypatch):
    calls = []

    def selected_stream(messages, provider=None, model=None, **kwargs):
        calls.append((provider, model))
        yield "error", "Groq HTTP 429: rate limit"
        yield "done", ""

    monkeypatch.setattr(ai_providers, "resolve_target", lambda *a, **k: (
        "groq", "openai/gpt-oss-120b", config.PROVIDERS["groq"]))
    monkeypatch.setattr(ai_providers, "_chat_stream_once", selected_stream)

    events = list(ai_providers.chat_stream(
        [{"role": "user", "content": "вопрос"}]
    ))

    assert calls == [("groq", "openai/gpt-oss-120b")]
    assert ("error", "Groq HTTP 429: rate limit") in events
    assert events[-1] == ("done", "")


def test_resolve_target_keeps_selected_provider_without_credentials(monkeypatch):
    monkeypatch.setattr(config, "current_provider", "groq")
    monkeypatch.setattr(config, "current_model", "openai/gpt-oss-120b")
    monkeypatch.setattr(config, "has_credentials", lambda _provider: False)
    monkeypatch.setattr(config, "first_available_provider", lambda: "google_ai_studio")

    provider, model, _cfg = ai_providers.resolve_target()

    assert provider == "groq"
    assert model == "openai/gpt-oss-120b"


def test_startup_provider_check_does_not_change_provider(monkeypatch):
    monkeypatch.setattr(config, "current_provider", "groq")
    monkeypatch.setattr(config, "current_model", "openai/gpt-oss-120b")
    monkeypatch.setattr(config, "has_credentials", lambda _provider: False)
    monkeypatch.setattr(config, "first_available_provider", lambda: "google_ai_studio")

    config.ensure_usable_provider()

    assert config.current_provider == "groq"
    assert config.current_model == "openai/gpt-oss-120b"
