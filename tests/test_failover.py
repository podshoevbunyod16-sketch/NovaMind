"""Автопереключение моделей: лимит одной модели не должен ронять агента.

Проверяем: следующая модель получает ТЕ ЖЕ данные агента (messages), исчерпанная
модель отдыхает, ошибки не теряются, а выключение через AI_FAILOVER=0 работает.
"""
import pytest

import ai_providers
import config

SEARCH = "[Результат web_search] " + "NovaMind — Flask-проект. " * 20
MESSAGES = [
    {"role": "user", "content": "Создай сайт"},
    {"role": "assistant", "content": "Ищу информацию"},
    {"role": "user", "content": SEARCH},
]


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    ai_providers._FAILOVER_COOLDOWN.clear()
    monkeypatch.delenv("AI_FAILOVER", raising=False)
    monkeypatch.delenv("POLLINATIONS_FALLBACK_MODEL", raising=False)
    yield
    ai_providers._FAILOVER_COOLDOWN.clear()


def _setup(monkeypatch, creds, behaviour, primary="groq"):
    """behaviour(provider, model) -> (text, meta) для подменённого одиночного запроса."""
    calls = []
    monkeypatch.setattr(config, "has_credentials", lambda name: name in creds or name == "local_demo")
    monkeypatch.setattr(
        ai_providers, "resolve_target",
        lambda provider=None, model=None: (
            primary, "openai/gpt-oss-120b" if primary == "groq" else "nova-local-1",
            config.PROVIDERS[primary]))

    def fake_once(messages, provider=None, model=None, **kwargs):
        calls.append({"provider": provider, "model": model, "messages": messages, "kwargs": kwargs})
        text, meta = behaviour(provider, model)
        if text is not None and isinstance(meta, dict):
            meta = dict(meta, provider=provider, model=model)
        return text, meta

    monkeypatch.setattr(ai_providers, "_chat_completion_once", fake_once)
    return calls


def test_limit_switches_to_next_provider_and_keeps_agent_data(monkeypatch):
    def behaviour(provider, model):
        if provider == "groq":
            return None, "Groq HTTP 429: превышен лимит запросов/токенов."
        return "Готовый ответ", {"offline": False}

    calls = _setup(monkeypatch, {"groq", "google_ai_studio"}, behaviour)
    text, meta = ai_providers.chat_completion(MESSAGES, system="sys", extra={"reasoning_effort": "low"})

    assert text == "Готовый ответ"
    assert meta["provider"] == "google_ai_studio"
    assert meta["requested_provider"] == "groq"
    assert [a["provider"] for a in meta["failover"]] == ["groq", "groq", "groq"]
    last = calls[-1]
    # данные агента дошли до запасной модели целиком
    assert last["messages"] == MESSAGES
    assert last["kwargs"]["system"] == "sys"
    # параметры, подобранные под первую модель, запасной не передаются
    assert last["kwargs"]["extra"] is None


def test_first_model_works_without_extra_meta(monkeypatch):
    calls = _setup(monkeypatch, {"groq"}, lambda p, m: ("ок", {}))
    text, meta = ai_providers.chat_completion(MESSAGES, extra={"a": 1})
    assert text == "ок" and "failover" not in meta
    assert len(calls) == 1 and calls[0]["kwargs"]["extra"] == {"a": 1}


def test_exhausted_model_rests_and_is_skipped_next_time(monkeypatch):
    def behaviour(provider, model):
        if provider == "groq":
            return None, "Groq HTTP 429: лимит"
        return "ответ", {}

    calls = _setup(monkeypatch, {"groq", "openrouter"}, behaviour)
    ai_providers.chat_completion(MESSAGES)
    calls.clear()
    text, _ = ai_providers.chat_completion(MESSAGES)
    assert text == "ответ"
    assert [c["provider"] for c in calls] == ["openrouter"]   # Groq отдыхает


def test_bad_key_rests_whole_provider(monkeypatch):
    def behaviour(provider, model):
        if provider == "groq":
            return None, "Groq HTTP 401: API-ключ отклонён"
        return "ответ", {}

    calls = _setup(monkeypatch, {"groq", "cerebras"}, behaviour)
    ai_providers.chat_completion(MESSAGES)
    calls.clear()
    ai_providers.chat_completion(MESSAGES)
    assert [c["provider"] for c in calls] == ["cerebras"]


def test_provider_exception_goes_to_next(monkeypatch):
    def behaviour(provider, model):
        if provider == "groq":
            raise RuntimeError("boom")
        return "ответ", {}

    calls = _setup(monkeypatch, {"groq", "openrouter"}, behaviour)
    text, meta = ai_providers.chat_completion(MESSAGES)
    assert text == "ответ" and meta["failover"][0]["error"].startswith("RuntimeError")


def test_all_models_down_reports_every_attempt(monkeypatch):
    _setup(monkeypatch, {"groq", "openrouter"}, lambda p, m: (None, f"{p} HTTP 500"))
    text, error = ai_providers.chat_completion(MESSAGES)
    assert text is None
    assert "Все модели недоступны" in error and "openrouter" in error and "groq" in error


def test_single_candidate_returns_original_error(monkeypatch):
    _setup(monkeypatch, set(), lambda p, m: (None, "оригинальная ошибка"), primary="local_demo")
    assert ai_providers.chat_completion(MESSAGES) == (None, "оригинальная ошибка")


def test_failover_can_be_disabled(monkeypatch):
    monkeypatch.setenv("AI_FAILOVER", "0")
    calls = _setup(monkeypatch, {"groq", "openrouter"}, lambda p, m: (None, "лимит"))
    text, error = ai_providers.chat_completion(MESSAGES)
    assert text is None and error == "лимит" and len(calls) == 1


def test_attempt_limit_is_respected(monkeypatch):
    monkeypatch.setenv("AI_FAILOVER_MAX", "2")
    calls = _setup(monkeypatch, {"groq", "openrouter", "cerebras"}, lambda p, m: (None, "HTTP 500"))
    ai_providers.chat_completion(MESSAGES)
    assert len(calls) == 2


def test_max_tokens_is_clamped_for_fallback(monkeypatch):
    def behaviour(provider, model):
        return (None, "HTTP 500") if provider == "groq" else ("ок", {})

    calls = _setup(monkeypatch, {"groq", "google_ai_studio"}, behaviour)
    ai_providers.chat_completion(MESSAGES, max_tokens=30000)
    assert calls[-1]["provider"] == "google_ai_studio"
    assert calls[-1]["kwargs"]["max_tokens"] == config.PROVIDERS["google_ai_studio"]["max_tokens"]


def test_shrink_keeps_recent_messages_and_system():
    big = "x" * 40000
    msgs = [{"role": "system", "content": big}] + [
        {"role": "user", "content": big} for _ in range(5)
    ] + [{"role": "user", "content": "свежий " + "y" * 4000}]
    out = ai_providers._failover_shrink(msgs, max_chars=50000)
    assert out[0]["content"] == big                   # system не трогаем
    assert len(out[1]["content"]) < 3000              # старое сжато
    assert len(out[2]["content"]) < 3000
    assert out[-4:] == msgs[-4:]                      # последние 4 целы, включая свежее
    assert msgs[1]["content"] == big                  # исходный список не изменён


@pytest.mark.parametrize("error,kind", [
    ("Groq HTTP 429: превышен лимит", "limit"),
    ("Google AI Studio HTTP 429 (превышена квота)", "limit"),
    ("Groq HTTP 401: API-ключ отклонён", "auth"),
    ("Google AI Studio: GEMINI_API_KEY не найден в .env", "auth"),
    ("модель не найдена или недоступна", "model"),
    ("Таймаут запроса к Groq", "error"),
])
def test_error_classification(error, kind):
    assert ai_providers._failover_classify(error)[0] == kind
