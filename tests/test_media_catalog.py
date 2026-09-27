"""Каталог медиа-моделей: только подключённые провайдеры и подтверждённые бесплатные API."""
import media_generation as mg
from conftest import FakeResponse


def ids(models):
    return {m["id"] for m in models}


# ---------- Фильтрация по статусу цены ----------
def test_paid_models_are_hidden_by_default(gemini_key):
    models = mg.media_catalog()
    assert "gemini-3.8-flash-tts" in ids(models)
    # Платные image/video модели Google не попадают в выдачу без явного флага.
    assert "gemini-3.1-flash-image" not in ids(models)
    assert "veo-3.1-generate-preview" not in ids(models)
    for model in models:
        assert model["pricing_status"] != mg.PRICING_PAID


def test_paid_models_appear_only_with_explicit_flag(gemini_key):
    models = mg.media_catalog(include_paid=True)
    paid = [m for m in models if m["id"] == "gemini-3.1-flash-image"]
    assert paid, "платная модель должна появляться при include_paid"
    assert paid[0]["pricing_status"] == mg.PRICING_PAID
    assert paid[0]["auto_usable"] is False
    assert paid[0]["trial_credits"] is True


def test_only_free_models_are_auto_usable(gemini_key):
    for model in mg.media_catalog(include_paid=True, include_unknown=True):
        if model["auto_usable"]:
            assert model["pricing_status"] == mg.PRICING_FREE
            assert model["provider_connected"] is True


def test_trial_credits_are_marked_separately():
    models = mg.media_catalog(include_trial=True)
    trial = [m for m in models if m["pricing_status"] == mg.PRICING_TRIAL]
    assert trial, "модели на пробных кредитах должны быть в каталоге"
    for model in trial:
        assert model["pricing_label"] == "Пробные кредиты"
        assert model["auto_usable"] is False
        assert "кредит" in model["pricing_note"].lower()


def test_trial_models_can_be_switched_off():
    assert mg.media_catalog(include_trial=True) != []
    assert all(m["pricing_status"] != mg.PRICING_TRIAL
               for m in mg.media_catalog(include_trial=False))


def test_free_models_have_a_pricing_source_and_date(gemini_key):
    free = [m for m in mg.media_catalog() if m["pricing_status"] == mg.PRICING_FREE]
    assert free
    for model in free:
        assert model["pricing_source"], "нужен источник подтверждения бесплатности"
        assert model["pricing_verified_at"], "нужна дата проверки цены"


# ---------- Типы и поиск ----------
def test_type_filter_returns_single_media_type(gemini_key):
    for media_type in mg.MEDIA_TYPES:
        models = mg.media_catalog(media_type=media_type, include_paid=True)
        assert models, f"для {media_type} должны быть модели"
        assert all(m["media_type"] == media_type for m in models)


def test_search_query_matches_name_provider_and_id(gemini_key):
    assert any(m["id"] == "veo-3.1-generate-preview"
               for m in mg.media_catalog(query="veo", include_paid=True))
    assert any(m["id"] == "gemini-3.8-flash-tts"
               for m in mg.media_catalog(query="google", media_type="audio"))
    assert mg.media_catalog(query="несуществующая-модель-xyz") == []


def test_provider_filter(gemini_key):
    models = mg.media_catalog(provider="pollinations")
    assert models
    assert all(m["provider"] == "pollinations" for m in models)


# ---------- Подключение провайдеров ----------
def test_unconnected_provider_is_reported_and_blocked(monkeypatch):
    # GEMINI_API_KEY не задан (isolated_env) → провайдер не подключён.
    models = mg.media_catalog(media_type="audio", provider="google_ai_studio")
    gemini_tts = [m for m in models if m["id"] == "gemini-3.8-flash-tts"]
    assert gemini_tts and gemini_tts[0]["provider_connected"] is False
    assert gemini_tts[0]["auto_usable"] is False

    error = mg.validate_selection(gemini_tts[0])
    assert error and "не подключён" in error


def test_pollinations_needs_no_key():
    assert mg.provider_connected("pollinations") is True
    models = mg.media_catalog(provider="pollinations")
    assert any(m["auto_usable"] for m in models)


def test_provider_statuses_cover_all_media_providers(gemini_key):
    statuses = mg.provider_statuses()
    assert set(statuses) == set(mg.MEDIA_PROVIDERS)
    assert statuses["google_ai_studio"] is True
    assert statuses["openrouter"] is False


# ---------- Живые данные OpenRouter ----------
def test_openrouter_pricing_is_taken_from_provider_data():
    free = mg.classify_openrouter_model({"id": "vendor/img:free", "pricing": {"prompt": "0", "completion": "0"}})
    assert free["pricing_status"] == mg.PRICING_FREE

    zero = mg.classify_openrouter_model({"id": "vendor/img", "pricing": {"prompt": "0", "completion": "0.0", "image": "0"}})
    assert zero["pricing_status"] == mg.PRICING_FREE

    paid = mg.classify_openrouter_model({"id": "vendor/img", "pricing": {"prompt": "0.5", "completion": "2"}})
    assert paid["pricing_status"] == mg.PRICING_PAID

    unknown = mg.classify_openrouter_model({"id": "vendor/img", "pricing": {}})
    assert unknown["pricing_status"] == mg.PRICING_UNKNOWN


def test_openrouter_catalog_marks_media_modalities(monkeypatch, gemini_key):
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    payload = {"data": [
        {"id": "vendor/flux-free:free", "name": "Flux Free",
         "pricing": {"prompt": "0", "completion": "0", "image": "0"},
         "architecture": {"output_modalities": ["image"]}},
        {"id": "vendor/nano-banana", "name": "Nano",
         "pricing": {"prompt": "0.02", "image": "0.04"},
         "architecture": {"output_modalities": ["image"]}},
        {"id": "vendor/llm", "name": "Text only",
         "pricing": {"prompt": "0", "completion": "0"},
         "architecture": {"output_modalities": ["text"]}},
    ]}

    def fake_get(url, **kwargs):
        assert "openrouter.ai/api/v1/models" in url
        return FakeResponse(200, json_data=payload)

    monkeypatch.setattr(mg.requests, "get", fake_get)
    models = mg.media_catalog(provider="openrouter", include_paid=True)
    assert "vendor/flux-free:free" in ids(models)
    assert "vendor/nano-banana" in ids(models)
    assert "vendor/llm" not in ids(models), "текстовые модели не должны попадать в медиа-каталог"

    free_only = mg.media_catalog(provider="openrouter")
    assert ids(free_only) == {"vendor/flux-free:free"}


def test_catalog_survives_provider_outage(monkeypatch, gemini_key):
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    import requests

    def boom(*args, **kwargs):
        raise requests.ConnectionError("нет сети")

    monkeypatch.setattr(mg.requests, "get", boom)
    models = mg.media_catalog()
    assert models, "кураторский каталог должен работать без сети"
    assert "gemini-3.8-flash-tts" in ids(models)


# ---------- Выбор модели ----------
def test_find_media_model_rejects_paid_without_flag(gemini_key):
    model, error = mg.find_media_model("google_ai_studio", "gemini-3.1-flash-image")
    assert model is None
    assert "не используется автоматически" in error

    model, error = mg.find_media_model("google_ai_studio", "gemini-3.1-flash-image", include_paid=True)
    assert model and error is None
    assert model["pricing_status"] == mg.PRICING_PAID


def test_find_media_model_unknown_id(gemini_key):
    model, error = mg.find_media_model("google_ai_studio", "no-such-model")
    assert model is None and "не найдена" in error


def test_validate_selection_requires_confirmation(gemini_key):
    model, _ = mg.find_media_model("google_ai_studio", "gemini-3.1-flash-image", include_paid=True)
    assert "Платные модели" in mg.validate_selection(model)
    assert mg.validate_selection(model, {"paid": True}) is None

    trial, error = mg.find_media_model("pollinations", "gptimage")
    assert error is None and trial["pricing_status"] == mg.PRICING_TRIAL
    assert "пробных кредитах" in mg.validate_selection(trial)
    assert mg.validate_selection(trial, {"trial": True}) is None
