"""Free-only provider failover for sync and streaming chat."""
import ai_providers
import config


def test_failover_switches_provider_and_keeps_failure_trace(monkeypatch):
    monkeypatch.setattr(ai_providers, "free_failover_targets", lambda *a, **k: [
        ("groq", "openai/gpt-oss-120b"),
        ("google_ai_studio", "gemini-2.0-flash"),
        ("local_demo", "nova-local-1"),
    ])
    calls = []

    def fake_once(messages, provider=None, model=None, **kwargs):
        calls.append((provider, model))
        if provider == "groq":
            return None, "timeout"
        return "Ответ резервной бесплатной модели", {"provider": provider, "model": model}

    monkeypatch.setattr(ai_providers, "_chat_completion_once", fake_once)
    text, meta = ai_providers.chat_completion([{"role": "user", "content": "вопрос"}])

    assert text == "Ответ резервной бесплатной модели"
    assert calls == [
        ("groq", "openai/gpt-oss-120b"),
        ("google_ai_studio", "gemini-2.0-flash"),
    ]
    assert meta["provider"] == "google_ai_studio"
    assert meta["failover_count"] == 1 and meta["free_model"] is True
    assert meta["failover"][0]["error"] == "timeout"


def test_failover_never_selects_paid_openrouter_model(monkeypatch):
    monkeypatch.setattr(ai_providers, "resolve_target", lambda *a, **k: (
        "openrouter", "vendor/paid-model", config.PROVIDERS["openrouter"]))
    monkeypatch.setattr(config, "has_credentials", lambda provider: provider == "openrouter")

    targets = ai_providers.free_failover_targets("openrouter", "vendor/paid-model")

    assert ("openrouter", "vendor/paid-model") not in targets
    assert any(provider == "openrouter" and model.endswith(":free")
               for provider, model in targets)
    assert all(ai_providers.is_free_ai_model(provider, model)
               for provider, model in targets)


def test_stream_retries_before_first_token_without_mixing_answers(monkeypatch):
    monkeypatch.setattr(ai_providers, "free_failover_targets", lambda *a, **k: [
        ("groq", "free-a"), ("cerebras", "free-b")])

    def fake_stream(messages, provider=None, model=None, **kwargs):
        if provider == "groq":
            yield "reasoning", "черновик не должен показываться"
            yield "error", "connection reset"
            yield "done", ""
            return
        yield "reasoning", "проверяю"
        yield "token", "Готовый "
        yield "token", "ответ"
        yield "done", ""

    monkeypatch.setattr(ai_providers, "_chat_stream_once", fake_stream)
    events = list(ai_providers.chat_stream([{"role": "user", "content": "вопрос"}]))

    assert ("reasoning", "черновик не должен показываться") not in events
    assert ("reasoning", "проверяю") in events
    assert "".join(value for kind, value in events if kind == "token") == "Готовый ответ"
    assert events[-1] == ("done", "")


def test_stream_does_not_switch_after_partial_answer(monkeypatch):
    monkeypatch.setattr(ai_providers, "free_failover_targets", lambda *a, **k: [
        ("groq", "free-a"), ("cerebras", "free-b")])
    calls = []

    def fake_stream(messages, provider=None, model=None, **kwargs):
        calls.append(provider)
        yield "token", "часть ответа"
        yield "error", "stream interrupted"
        yield "done", ""

    monkeypatch.setattr(ai_providers, "_chat_stream_once", fake_stream)
    events = list(ai_providers.chat_stream([{"role": "user", "content": "вопрос"}]))

    assert calls == ["groq"]
    assert ("token", "часть ответа") in events
    assert any(kind == "error" for kind, _value in events)
