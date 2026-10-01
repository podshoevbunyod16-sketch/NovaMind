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
    return "".join(event["token"] for event in events if event["type"] == "token")


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
    seen = []
    _stub_model(monkeypatch, [
        '{"action":"image","prompt":"a cute red fox in a snowy forest, watercolor","aspect":"16:9"}',
        "Готово! Нарисовал лисичку в зимнем лесу акварелью.",
    ], seen)
    events = _ndjson(google_client.post("/api/agent/stream", json={"message": "нарисуй лису"}))

    assert fake_media[0]["kind"] == "image"
    assert fake_media[0]["options"]["width"] == 1344          # 16:9
    stage = next(event for event in events if event["type"] == "stage")
    assert stage["scene"] == "media" and "Рисую" in stage["title"]
    media = next(event for event in events if event["type"] == "media")["media"]
    assert media["url"] == "/media/image_1.png" and media["model_name"] == "Pollinations Flux"
    assert not [event for event in events if event["type"] == "tool"]   # это не Linux
    assert "лисичку" in _answer(events)

    result = next(event for event in events if event["type"] == "result")
    assert result["media"][0]["url"] == "/media/image_1.png"
    assert result["linux"] is False
    # Модель узнала, что файл уже показан — ссылку повторять не надо
    observation = seen[1]["messages"][-1]["content"]
    assert "уже показано пользователю" in observation
    # Промпт объясняет медиа-инструменты
    assert '"action":"image"' in seen[0]["system"] and '"action":"video"' in seen[0]["system"]


def test_media_works_without_linux(google_client, monkeypatch, fake_media):
    monkeypatch.setenv("TERMINAL_ENABLED", "0")
    seen = []
    _stub_model(monkeypatch, [
        '{"action":"audio","text":"Добро пожаловать в NovaMind!","voice":"Kore"}',
        "Озвучил приветствие.",
    ], seen)
    events = _ndjson(google_client.post("/api/agent/stream", json={"message": "озвучь приветствие"}))
    assert fake_media[0]["kind"] == "audio" and fake_media[0]["options"] == {"voice": "Kore"}
    assert any(event["type"] == "media" for event in events)
    assert '"action":"run"' not in seen[0]["system"]          # Linux-команд в промпте нет
    assert "Linux-окружение сейчас выключено" in seen[0]["system"]


def test_linux_action_without_linux_goes_back_to_model(google_client, monkeypatch):
    monkeypatch.setenv("TERMINAL_ENABLED", "0")
    seen = []
    _stub_model(monkeypatch, ['{"action":"run","command":"ls"}', "Посчитал в уме: 42"], seen)
    events = _ndjson(google_client.post("/api/agent/stream", json={"message": "посчитай"}))
    assert not [event for event in events if event["type"] == "tool"]
    assert "TERMINAL_ENABLED" in seen[1]["messages"][-1]["content"]
    assert _answer(events) == "Посчитал в уме: 42"


def test_failed_generation_is_explained_to_model(google_client, monkeypatch):
    monkeypatch.setattr(media_agent, "generate",
                        lambda *a, **k: (None, None, ["Нет доступных моделей (видео). Подключить: VIDEO_API_URL"]))
    seen = []
    _stub_model(monkeypatch, ['{"action":"video","prompt":"ocean waves at sunset"}',
                              "Видео сейчас сделать не могу: нужен видео-провайдер."], seen)
    events = _ndjson(google_client.post("/api/agent/stream", json={"message": "сделай видео"}))
    assert not [event for event in events if event["type"] == "media"]
    assert any(event["type"] == "step" and "Не получилось" in event["text"] for event in events)
    assert "VIDEO_API_URL" in seen[1]["messages"][-1]["content"]


def test_video_job_is_reported_as_queued(google_client, monkeypatch):
    def queued(kind, prompt, options=None, wanted_model="", on_status=None):
        return ({"kind": "video", "job_id": "job-7", "state": "queued", "model": "veo",
                 "model_name": "Veo 3.1", "provider_name": "Google AI Studio"},
                {"name": "Veo 3.1", "provider_name": "Google AI Studio"}, [])
    monkeypatch.setattr(media_agent, "generate", queued)
    seen = []
    _stub_model(monkeypatch, ['{"action":"video","prompt":"a cat surfing"}', "Видео готовится."], seen)
    events = _ndjson(google_client.post("/api/agent/stream", json={"message": "видео с котом"}))
    media = next(event for event in events if event["type"] == "media")["media"]
    assert media["job_id"] == "job-7" and media["state"] == "queued"
    assert "очередь" in seen[1]["messages"][-1]["content"]


def test_media_models_action_lists_choices(google_client, monkeypatch):
    monkeypatch.setenv("POLLINATIONS_API_KEY", "pk-test")
    seen = []
    _stub_model(monkeypatch, ['{"action":"media_models","type":"image"}', "Есть flux и turbo."], seen)
    _ndjson(google_client.post("/api/agent/stream", json={"message": "какие модели рисуют?"}))
    observation = seen[1]["messages"][-1]["content"]
    assert "flux" in observation and "turbo" in observation


def test_generated_media_is_remembered_in_history(google_client, monkeypatch, fake_media):
    _stub_model(monkeypatch, ['{"action":"image","prompt":"a lighthouse at night"}', "Маяк готов."])
    _ndjson(google_client.post("/api/agent/stream", json={"message": "нарисуй маяк"}))
    seen = []
    _stub_model(monkeypatch, ["Сделаю ярче."], seen)
    _ndjson(google_client.post("/api/agent/stream", json={"message": "сделай ярче"}))
    history = " ".join(message["content"] for message in seen[0]["messages"])
    assert "a lighthouse at night" in history


def test_status_check_is_instant_and_needs_no_network(google_client, monkeypatch):
    """Статус агента грузится при каждом открытии чата — он не ходит в каталоги провайдеров."""
    def forbidden(*args, **kwargs):
        raise AssertionError("статус не должен вызывать живой каталог")
    monkeypatch.setattr(media_agent, "candidates", forbidden)
    assert google_client.get("/api/agent/status").get_json()["media"] == {
        "image": False, "audio": False, "video": False}
    monkeypatch.setenv("POLLINATIONS_API_KEY", "pk-test")
    media = google_client.get("/api/agent/status").get_json()["media"]
    assert media["image"] is True and media["audio"] is True
