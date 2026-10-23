"""
ИИ сам генерирует изображения, аудио и видео — без кнопок и ручного выбора модели.

Проверяется:
  * выбор модели: только подключённые и бесплатные (или свой endpoint из .env),
    платные — лишь с MEDIA_ALLOW_PAID=1;
  * если провайдер отказал — берётся следующая модель;
  * модель, которую назвал ИИ, пробуется первой;
  * агент в главном чате вызывает image/audio/video, показывает файл
    и работает даже без Linux-окружения.
"""
import json

import pytest

import media_agent
import media_generation as mg
import routes.agent as agent_routes
from tests.agent_helpers import answer_of, events_of, model_text, ndjson, script


@pytest.fixture(autouse=True)
def offline_catalogs(monkeypatch):
    """Живые каталоги провайдеров в тестах недоступны — как при плохой сети."""
    def boom(*args, **kwargs):
        raise mg.requests.exceptions.ConnectionError("нет сети в тестах")
    monkeypatch.setattr(mg.requests, "get", boom)


def _ids(models):
    return [model["id"] for model in models]


# ══════════════ выбор модели ══════════════

def test_no_keys_means_no_models_and_a_clear_hint():
    assert media_agent.candidates("image") == []
    text = media_agent.describe("image")
    assert "моделей нет" in text and "POLLINATIONS_API_KEY" in text


def test_pollinations_key_gives_free_image_models_even_if_catalog_is_down(monkeypatch):
    monkeypatch.setenv("POLLINATIONS_API_KEY", "pk-test")
    models = media_agent.candidates("image")
    assert _ids(models)[:2] == ["flux", "turbo"]
    assert all(model["pricing_status"] == mg.PRICING_FREE for model in models)


def test_own_endpoint_from_env_goes_first(monkeypatch):
    monkeypatch.setenv("POLLINATIONS_API_KEY", "pk-test")
    monkeypatch.setenv("IMAGE_GENERATION_URL", "https://example.com/v1/images/generations")
    monkeypatch.setenv("IMAGE_MODEL", "my-sdxl")
    assert _ids(media_agent.candidates("image"))[0] == "my-sdxl"


def test_paid_models_are_never_picked_without_permission(gemini_key, monkeypatch):
    # Gemini: TTS бесплатный, картинки и Veo — платные
    assert _ids(media_agent.candidates("audio"))[0] == "gemini-3.8-flash-tts"
    assert media_agent.candidates("image") == []
    assert media_agent.candidates("video") == []

    monkeypatch.setenv("MEDIA_ALLOW_PAID", "1")
    media_agent.reset_cache()
    assert "gemini-3.1-flash-image" in _ids(media_agent.candidates("image"))
    assert media_agent.candidates("video")


def test_model_named_by_ai_is_tried_first(monkeypatch):
    monkeypatch.setenv("POLLINATIONS_API_KEY", "pk-test")
    ordered = media_agent._prefer(media_agent.candidates("image"), "turbo")
    assert ordered[0]["id"] == "turbo"


def test_build_options_understands_size_and_aspect():
    assert media_agent.build_options("image", {"aspect": "16:9"})["width"] == 1344
    assert media_agent.build_options("image", {"size": "512x768"})["height"] == 768
    clamped = media_agent.build_options("image", {"size": "4096x100"})
    assert (clamped["width"], clamped["height"]) == (2048, 256)
    assert media_agent.build_options("audio", {"voice": "Kore"}) == {"voice": "Kore"}
    video = media_agent.build_options("video", {"duration": 100, "aspect": "9:16"})
    assert video == {"duration": 20, "aspect_ratio": "9:16"}


def test_generate_falls_back_to_next_model(monkeypatch):
    monkeypatch.setenv("POLLINATIONS_API_KEY", "pk-test")
    tried = []

    def fake_generate(model, prompt, options=None, on_status=None, confirm=None):
        tried.append(model["id"])
        if model["id"] == "flux":
            return None, "HTTP 503: перегружен"
        return {"kind": "image", "url": "/media/x.png", "filename": "x.png",
                "model": model["id"], "model_name": model["name"]}, None

    monkeypatch.setattr(mg, "generate_media", fake_generate)
    result, model, errors = media_agent.generate("image", "a red fox", {})
    assert tried == ["flux", "turbo"]
    assert result["url"] == "/media/x.png" and model["id"] == "turbo"
    assert errors and "503" in errors[0]


def test_generate_without_models_explains_what_to_connect():
    result, model, errors = media_agent.generate("video", "a sunset timelapse", {})
    assert result is None and model is None
    assert "VIDEO_API_URL" in errors[0]


# ══════════════ агент в главном чате ══════════════

def _ndjson(response):
    return [json.loads(line) for line in response.get_data(as_text=True).splitlines() if line.strip()]


def _stub_model(monkeypatch, answers, seen=None):
    queue = list(answers)

    def fake_stream(messages, **kwargs):
        if seen is not None:
            seen.append({"messages": list(messages), "system": kwargs.get("system", "")})
        text = queue.pop(0) if queue else "готово"
        for index in range(0, len(text), 9):
            yield "token", text[index:index + 9]
        yield "done", ""

    monkeypatch.setattr(agent_routes, "chat_stream", fake_stream)
    monkeypatch.setattr(agent_routes, "chat_completion", lambda messages, **kw: ("готово", {}))
    return queue


def _answer(events):
    return answer_of(events)


@pytest.fixture
def fake_media(monkeypatch):
    """Генерация без сети: каждая модель «рисует» файл, запросы записываются."""
    calls = []

    def fake_generate(kind, prompt, options=None, wanted_model="", on_status=None):
        calls.append({"kind": kind, "prompt": prompt, "options": options, "model": wanted_model})
        if on_status:
            on_status("Пробую Pollinations Flux (Pollinations)")
        model = {"id": "flux", "name": "Pollinations Flux", "provider_name": "Pollinations"}
        result = {"kind": kind, "url": f"/media/{kind}_1.png", "filename": f"{kind}_1.png",
                  "mime": "image/png", "model": "flux", "model_name": "Pollinations Flux",
                  "provider": "pollinations", "provider_name": "Pollinations", "elapsed_ms": 2100}
        return result, model, []

    monkeypatch.setattr(media_agent, "generate", fake_generate)
    return calls


def test_ai_draws_picture_by_itself(google_client, monkeypatch, fake_media):
    """«Нарисуй лису» — одна генерация и ни одного запроса к модели."""
    seen = []
    script(monkeypatch, ["этот ответ не должен понадобиться"], seen)

    events = ndjson(google_client.post("/api/agent/stream", json={"message": "нарисуй лису"}))
    assert len(fake_media) == 1 and fake_media[0]["kind"] == "image"
    assert "нарисуй лису" in fake_media[0]["prompt"]
    assert seen == [], "явный медиа-запрос не зовёт модель"
    stage = next(event for event in events_of(events, "stage") if event["scene"] == "media")
    assert "медиа" in stage["title"].lower()
    media = events_of(events, "media")[0]["media"]
    assert media["url"] == "/media/image_1.png" and media["model_name"] == "Pollinations Flux"
    assert not events_of(events, "tool")          # это не Linux
    assert "Готово" in answer_of(events)

    result = next(event for event in events if event["type"] == "result")
    assert result["media"][0]["url"] == "/media/image_1.png"
    assert result["linux"] is False


def test_media_status_is_forwarded_as_steps(google_client, monkeypatch, fake_media):
    """Сообщения провайдера («пробую модель …») видны пользователю как шаги."""
    events = ndjson(google_client.post("/api/agent/stream", json={"message": "нарисуй маяк"}))
    assert any("Пробую Pollinations Flux" in event["text"] for event in events_of(events, "step"))


def test_media_works_without_linux(google_client, monkeypatch, fake_media):
    monkeypatch.setenv("TERMINAL_ENABLED", "0")
    events = ndjson(google_client.post("/api/agent/stream", json={"message": "озвучь приветствие"}))
    assert fake_media[0]["kind"] == "audio"
    assert events_of(events, "media")
    assert next(e for e in events if e["type"] == "result")["linux"] is False


def test_terminal_off_is_reported_to_the_model(google_client, monkeypatch):
    """Без песочницы промпт честно говорит, что команд нет."""
    monkeypatch.setenv("TERMINAL_ENABLED", "0")
    seen = []
    script(monkeypatch, ["Посчитал в уме: 42"], seen)
    events = ndjson(google_client.post("/api/agent/stream", json={"message": "посчитай в уме"}))
    assert answer_of(events) == "Посчитал в уме: 42"
    offered = model_text(seen).split("ДОСТУПНЫЕ")[-1]
    assert "```bash" not in offered


def test_failed_generation_is_explained_to_model(google_client, monkeypatch):
    monkeypatch.setattr(media_agent, "generate",
                        lambda *a, **k: (None, None, ["Нет доступных моделей (видео). Подключить: VIDEO_API_URL"]))
    script(monkeypatch, ["этот ответ не должен понадобиться"])
    events = ndjson(google_client.post("/api/agent/stream", json={"message": "сделай видео волн"}))
    assert not events_of(events, "media")
    step = next(event for event in events_of(events, "step") if "Не получилось" in event["text"])
    assert "VIDEO_API_URL" in step["text"]
    assert "Не получилось создать видео" in answer_of(events)


def test_video_job_is_reported_as_queued(google_client, monkeypatch):
    def queued(kind, prompt, options=None, wanted_model="", on_status=None):
        return ({"kind": "video", "job_id": "job-7", "state": "queued", "model": "veo",
                 "model_name": "Veo 3.1", "provider_name": "Google AI Studio"},
                {"name": "Veo 3.1", "provider_name": "Google AI Studio"}, [])
    monkeypatch.setattr(media_agent, "generate", queued)
    events = ndjson(google_client.post("/api/agent/stream", json={"message": "сделай видео с котом"}))
    media = events_of(events, "media")[0]["media"]
    assert media["job_id"] == "job-7" and media["state"] == "queued"
    assert "очеред" in answer_of(events).lower()


def test_model_is_not_asked_to_list_media_models(google_client, monkeypatch):
    """Список моделей — это /api/agent/status и describe(), а не ход агента."""
    seen = []
    script(monkeypatch, ["Доступные модели перечислены в настройках."], seen)
    events = ndjson(google_client.post("/api/agent/stream", json={"message": "какие модели рисуют?"}))
    assert answer_of(events) == "Доступные модели перечислены в настройках."
    assert not events_of(events, "media")


def test_generated_media_is_remembered_in_history(google_client, monkeypatch, fake_media):
    """После перезагрузки страницы медиа отрисуется из маркера в истории."""
    script(monkeypatch, ["не нужен"])
    ndjson(google_client.post("/api/agent/stream", json={"message": "нарисуй маяк"}).get_data()
           if False else google_client.post("/api/agent/stream", json={"message": "нарисуй маяк"}))

    seen = []
    script(monkeypatch, ["Сделаю ярче."], seen)
    ndjson(google_client.post("/api/agent/stream", json={"message": "сделай ярче"}))
    history = model_text(seen)
    assert "MEDIA_RESULT:" in history and "/media/image_1.png" in history
