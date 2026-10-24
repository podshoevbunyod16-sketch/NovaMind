"""Движок генерации: реальные запросы провайдерам, реальные ошибки, никакого выдуманного прогресса."""
import base64
import os

import pytest

import media_generation as mg
from conftest import FakeResponse

PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
WAV_BYTES = b"RIFF$\x00\x00\x00WAVEfmt " + b"\x00" * 16


def model_for(model_id, provider="google_ai_studio"):
    model, error = mg.find_media_model(provider, model_id, include_paid=True, include_unknown=True)
    assert error is None, error
    return model


def statuses_of(callback_list):
    return [item for item in callback_list]


# ---------- Google AI Studio: изображения ----------
def test_google_image_sends_response_modalities_and_saves_file(gemini_key, monkeypatch, tmp_path):
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None, **kwargs):
        captured["url"] = url
        captured["body"] = json
        captured["headers"] = headers
        return FakeResponse(200, json_data={"candidates": [{"content": {"parts": [
            {"inlineData": {"mimeType": "image/png", "data": base64.b64encode(PNG_BYTES).decode()}}
        ]}}]})

    monkeypatch.setattr(mg.requests, "post", fake_post)
    events = []
    result, error = mg.generate_media(model_for("gemini-3.1-flash-image"), "красный закат",
                                     on_status=events.append, confirm={"paid": True})
    assert error is None
    assert captured["url"].endswith("/models/gemini-3.1-flash-image:generateContent")
    assert captured["body"]["generationConfig"]["responseModalities"] == ["IMAGE"]
    assert captured["headers"]["x-goog-api-key"] == "test-gemini-key"

    assert result["kind"] == "image"
    assert result["mime"] == "image/png"
    assert result["url"].startswith("/media/")
    assert os.path.isfile(result["path"])
    assert open(result["path"], "rb").read() == PNG_BYTES
    # Статусы — реальные шаги запроса, без процентов.
    assert any("Отправка запроса к Google AI Studio" in s for s in events)
    assert not any("%" in s for s in events)


def test_provider_error_is_returned_verbatim(gemini_key, monkeypatch):
    def fake_post(url, **kwargs):
        return FakeResponse(429, json_data={"error": {
            "code": 429, "status": "RESOURCE_EXHAUSTED",
            "message": "You exceeded your current quota"}})

    monkeypatch.setattr(mg.requests, "post", fake_post)
    result, error = mg.generate_media(model_for("gemini-3.1-flash-image"), "тест",
                                      confirm={"paid": True})
    assert result is None
    assert "HTTP 429" in error and "RESOURCE_EXHAUSTED" in error


def test_missing_image_in_response_is_reported(gemini_key, monkeypatch):
    monkeypatch.setattr(mg.requests, "post", lambda *a, **k: FakeResponse(200, json_data={
        "candidates": [{"content": {"parts": [{"text": "не могу"}]}, "finishReason": "IMAGE_SAFETY"}]}))
    result, error = mg.generate_media(model_for("gemini-3.1-flash-image"), "тест", confirm={"paid": True})
    assert result is None and "finishReason=IMAGE_SAFETY" in error


# ---------- Google AI Studio: TTS ----------
def test_google_tts_uses_speech_config_and_saves_wav(gemini_key, monkeypatch):
    captured = {}

    def fake_post(url, json=None, **kwargs):
        captured["body"] = json
        return FakeResponse(200, json_data={"candidates": [{"content": {"parts": [
            {"inlineData": {"mimeType": "audio/wav", "data": base64.b64encode(WAV_BYTES).decode()}}
        ]}}]})

    monkeypatch.setattr(mg.requests, "post", fake_post)
    result, error = mg.generate_media(model_for("gemini-3.8-flash-tts"), "Привет, мир!")
    assert error is None
    speech = captured["body"]["generationConfig"]["speechConfig"]["voiceConfig"]["prebuiltVoiceConfig"]
    assert speech["voiceName"] == "Kore"
    assert captured["body"]["generationConfig"]["responseModalities"] == ["AUDIO"]
    assert result["kind"] == "audio" and result["filename"].endswith(".wav")
    assert open(result["path"], "rb").read() == WAV_BYTES


def test_tts_is_free_and_needs_no_confirmation(gemini_key):
    model = model_for("gemini-3.8-flash-tts")
    assert model["pricing_status"] == mg.PRICING_FREE
    assert mg.validate_selection(model) is None


# ---------- Платные модели не используются автоматически ----------
def test_paid_model_refused_without_explicit_confirmation(gemini_key, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("платная модель не должна вызываться без подтверждения")

    monkeypatch.setattr(mg.requests, "post", forbidden)
    result, error = mg.generate_media(model_for("gemini-3.1-flash-image"), "тест")
    assert result is None
    assert "Платные модели не используются автоматически" in error


def test_paid_model_works_after_confirmation(gemini_key, monkeypatch):
    monkeypatch.setattr(mg.requests, "post", lambda *a, **k: FakeResponse(200, json_data={
        "candidates": [{"content": {"parts": [
            {"inlineData": {"mimeType": "image/png", "data": base64.b64encode(PNG_BYTES).decode()}}]}}]}))
    result, error = mg.generate_media(model_for("gemini-3.1-flash-image"), "тест", confirm={"paid": True})
    assert error is None and result["pricing_status"] == mg.PRICING_PAID


# ---------- Groq TTS (Orpheus) ----------
def _fake_wav(seconds_bytes: int = 8) -> bytes:
    """Настоящий wav из тишины — чтобы проверить склейку частей."""
    import io
    import struct
    import wave
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(8000)
        wav.writeframes(struct.pack("<h", 0) * seconds_bytes)
    return buffer.getvalue()


def groq_tts_model(model_id="canopylabs/orpheus-v1-english", **options):
    """Модель Groq из реестра без похода в живой каталог."""
    entry = next(item for item in mg.MEDIA_MODEL_REGISTRY if item["id"] == model_id)
    model = dict(entry)
    model["options"] = {**(entry.get("options") or {}), **options}
    model.update({
        "provider_connected": True, "in_provider_catalog": True,
        "pricing_label": mg.PRICING_LABELS[entry["pricing_status"]],
        "provider_name": mg.MEDIA_PROVIDERS["groq"],
    })
    return model


def pollinations_tts_model(model_id, **options):
    """Модель Pollinations из реестра (без сети)."""
    entry = next(item for item in mg.MEDIA_MODEL_REGISTRY if item["id"] == model_id)
    model = dict(entry)
    model["options"] = {**(entry.get("options") or {}), **options}
    model.update({
        "provider_connected": True, "in_provider_catalog": True,
        "pricing_label": mg.PRICING_LABELS[entry["pricing_status"]],
        "provider_name": mg.MEDIA_PROVIDERS["pollinations"],
    })
    return model


def test_groq_tts_uses_audio_speech_endpoint(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk-test")
    import groq_rotation
    monkeypatch.setattr(groq_rotation, "GROQ_KEYS",
                        [{"key": "gsk-test", "index": 0, "exhausted_at": None}])
    captured = {}

    def fake_post(url, json=None, headers=None, **kwargs):
        captured.update(url=url, body=json, headers=headers)
        return FakeResponse(200, content=WAV_BYTES, headers={"Content-Type": "audio/wav"})

    monkeypatch.setattr(mg.requests, "post", fake_post)
    result, error = mg.generate_media(groq_tts_model(), "Съешь ещё этих булок")
    assert error is None
    assert captured["url"] == mg.GROQ_TTS_URL
    assert captured["body"]["model"] == "canopylabs/orpheus-v1-english"
    assert captured["body"]["response_format"] == "wav"
    assert captured["headers"]["Authorization"] == "Bearer gsk-test"
    assert result["kind"] == "audio" and result["provider"] == "groq"


def test_groq_tts_splits_long_text_into_200_char_requests(monkeypatch):
    """Orpheus принимает максимум 200 символов: длинный текст режем сами."""
    monkeypatch.setenv("GROQ_API_KEY", "gsk-test")
    import groq_rotation
    monkeypatch.setattr(groq_rotation, "GROQ_KEYS",
                        [{"key": "gsk-test", "index": 0, "exhausted_at": None}])
    sent = []

    def fake_post(url, json=None, headers=None, **kwargs):
        sent.append(json["input"])
        return FakeResponse(200, content=_fake_wav(), headers={"Content-Type": "audio/wav"})

    monkeypatch.setattr(mg.requests, "post", fake_post)
    text = "Это длинный ответ. " * 30
    result, error = mg.generate_media(groq_tts_model(), text)
    assert error is None
    assert len(sent) > 1
    assert all(len(part) <= mg.GROQ_TTS_MAX_INPUT for part in sent)
    assert " ".join(sent).split() == text.split()      # ничего не потеряно
    # Все части склеены в один валидный wav.
    import io
    import wave
    with wave.open(io.BytesIO(open(result["path"], "rb").read()), "rb") as wav:
        # Каждая часть вернула по 8 сэмплов — склейка сохранила их все.
        assert wav.getnframes() == 8 * len(sent)


def test_groq_terms_error_becomes_actionable(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk-test")
    import groq_rotation
    monkeypatch.setattr(groq_rotation, "GROQ_KEYS",
                        [{"key": "gsk-test", "index": 0, "exhausted_at": None}])
    monkeypatch.setattr(mg.requests, "post", lambda *a, **k: FakeResponse(400, json_data={
        "error": {"message": "The model `canopylabs/orpheus-v1-english` requires terms acceptance. "
                             "Please have the org admin accept the terms at "
                             "https://console.groq.com/playground?model=canopylabs%2Forpheus-v1-english",
                  "code": "model_terms_required"}}))
    result, error = mg.generate_media(groq_tts_model(), "Привет")
    assert result is None
    assert "Accept terms" in error
    assert "console.groq.com/playground" in error
    assert "model_terms_required" in error          # дословный ответ провайдера сохранён


# ---------- Pollinations TTS ----------
def test_pollinations_speech_model_uses_audio_speech(monkeypatch):
    monkeypatch.setenv("POLLINATIONS_API_KEY", "pk-test")
    monkeypatch.setattr(mg, "pollinations_tts_model", lambda *a, **k: {
        "id": "openai/tts-1", "endpoint": "speech",
        "voices": ["alloy", "nova"], "aliases": []})
    captured = {}

    def fake_post(url, json=None, headers=None, **kwargs):
        captured.update(url=url, body=json)
        return FakeResponse(200, content=b"ID3audio", headers={"Content-Type": "audio/mpeg"})

    monkeypatch.setattr(mg.requests, "post", fake_post)
    result, error = mg.generate_media(pollinations_tts_model("openai/tts-1"), "Привет")
    assert error is None
    assert captured["url"].endswith("/v1/audio/speech")
    assert captured["body"]["voice"] == "nova"
    assert captured["body"]["model"] == "openai/tts-1"
    assert result["mime"] == "audio/mpeg"


def test_pollinations_chat_audio_model_uses_chat_endpoint(monkeypatch):
    """openai-audio (openai/gpt-audio-mini) не принимает /v1/audio/speech."""
    monkeypatch.setenv("POLLINATIONS_API_KEY", "pk-test")
    monkeypatch.setattr(mg, "pollinations_tts_model", lambda *a, **k: {
        "id": "openai/gpt-audio-mini", "endpoint": "chat",
        "voices": ["alloy", "nova"], "aliases": ["openai-audio"]})
    captured = {}

    def fake_post(url, json=None, headers=None, **kwargs):
        captured.update(url=url, body=json)
        return FakeResponse(200, json_data={"choices": [{"message": {
            "audio": {"data": base64.b64encode(b"ID3audio").decode()}}}]})

    monkeypatch.setattr(mg.requests, "post", fake_post)
    result, error = mg.generate_media(pollinations_tts_model("openai/gpt-audio-mini"), "Привет")
    assert error is None
    assert captured["url"].endswith("/v1/chat/completions")
    assert captured["body"]["modalities"] == ["text", "audio"]
    assert captured["body"]["audio"]["voice"] == "alloy"
    assert result["kind"] == "audio" and result["mime"] == "audio/mpeg"


def test_pollinations_text_model_rejection_is_retried_on_chat(monkeypatch):
    """Если каталог устарел, провайдер сам подсказывает правильный endpoint."""
    monkeypatch.setenv("POLLINATIONS_API_KEY", "pk-test")
    monkeypatch.setattr(mg, "pollinations_tts_model", lambda *a, **k: {
        "id": "openai/gpt-audio-mini", "endpoint": "speech",
        "voices": ["alloy"], "aliases": []})
    calls = []

    def fake_post(url, json=None, headers=None, **kwargs):
        calls.append(url)
        if url.endswith("/v1/audio/speech"):
            return FakeResponse(400, json_data={"success": False, "error": {
                "message": 'Model "openai-audio" is a text model and cannot be used on '
                           'the audio endpoint. Use the text endpoint instead.',
                "code": "BAD_REQUEST"}})
        return FakeResponse(200, json_data={"choices": [{"message": {
            "audio": {"data": base64.b64encode(b"ID3audio").decode()}}}]})

    monkeypatch.setattr(mg.requests, "post", fake_post)
    result, error = mg.generate_media(
        pollinations_tts_model("openai/gpt-audio-mini", endpoint="speech"), "Привет")
    assert error is None
    assert calls[-1].endswith("/v1/chat/completions")
    assert result["kind"] == "audio"


def test_pollinations_voice_is_replaced_when_model_does_not_support_it(monkeypatch):
    monkeypatch.setenv("POLLINATIONS_API_KEY", "pk-test")
    monkeypatch.setattr(mg, "pollinations_tts_model", lambda *a, **k: {
        "id": "openai/tts-1", "endpoint": "speech",
        "voices": ["af_alloy", "af_bella"], "aliases": []})
    captured = {}
    monkeypatch.setattr(mg.requests, "post", lambda url, json=None, **k: (
        captured.update(body=json) or FakeResponse(200, content=b"ID3audio",
                                                   headers={"Content-Type": "audio/mpeg"})))
    result, error = mg.generate_media(
        pollinations_tts_model("openai/tts-1", voice="nova"), "Привет")
    assert error is None and result
    assert captured["body"]["voice"] == "af_alloy"


def test_pollinations_tts_catalog_drops_transcription_and_music(monkeypatch):
    models = {
        "/audio/models": [
            {"name": "openai/whisper-large-v3", "title": "Whisper",
             "input_modalities": ["audio"], "output_modalities": ["text"],
             "supported_endpoints": ["/v1/audio/transcriptions"]},
            {"name": "elevenlabs/music-v2", "title": "ElevenLabs Music",
             "input_modalities": ["text"], "output_modalities": ["audio"],
             "supported_endpoints": ["/v1/audio/speech"]},
            {"name": "openai/tts-1", "title": "OpenAI TTS",
             "input_modalities": ["text"], "output_modalities": ["audio"],
             "supported_endpoints": ["/v1/audio/speech", "/v1/chat/completions"],
             "voices": ["alloy", "nova"], "paid_only": False,
             "health": {"status": "healthy"}},
        ],
        "/text/models": [
            {"name": "openai/gpt-audio-mini", "title": "GPT Audio Mini",
             "input_modalities": ["text", "audio"], "output_modalities": ["audio", "text"],
             "supported_endpoints": ["/v1/chat/completions"], "aliases": ["openai-audio"],
             "voices": ["alloy"], "paid_only": False},
            {"name": "openai/gpt-4o-mini", "title": "GPT-4o Mini",
             "input_modalities": ["text"], "output_modalities": ["text"],
             "supported_endpoints": ["/v1/chat/completions"]},
        ],
    }

    def fake_catalog(path, timeout=8.0):
        return models[path]

    monkeypatch.setattr(mg, "_pollinations_catalog", fake_catalog)
    mg.reset_tts_catalog_cache()
    try:
        catalog = mg.pollinations_tts_catalog(force=True)
    finally:
        mg.reset_tts_catalog_cache()

    ids = [item["id"] for item in catalog]
    assert ids == ["openai/tts-1", "openai/gpt-audio-mini"], ids
    assert catalog[0]["endpoint"] == "speech" and catalog[1]["endpoint"] == "chat"
    assert catalog[1]["aliases"] == ["openai-audio"]
    # Whisper (STT), музыка и текстовая модель в TTS-каталог не попадают.
    assert not {"openai/whisper-large-v3", "elevenlabs/music-v2", "openai/gpt-4o-mini"} & set(ids)


# ---------- Pollinations (без ключа) ----------
def test_pollinations_rejects_non_image_payload(monkeypatch):
    monkeypatch.setattr(mg.requests, "get",
                        lambda *a, **k: FakeResponse(200, content=b"<html>rate limited</html>",
                                                     headers={"Content-Type": "text/html"}))
    result, error = mg.generate_media(model_for("flux", "pollinations"), "кот")
    assert result is None and "не изображение" in error


def test_pollinations_image_is_saved(monkeypatch):
    captured = {}

    def fake_get(url, **kwargs):
        captured["url"] = url
        return FakeResponse(200, content=PNG_BYTES, headers={"Content-Type": "image/jpeg"})

    monkeypatch.setattr(mg.requests, "get", fake_get)
    result, error = mg.generate_media(model_for("flux", "pollinations"), "рыжий кот")
    assert error is None
    assert "image.pollinations.ai/prompt/" in captured["url"]
    assert "model=flux" in captured["url"]
    assert result["mime"] == "image/jpeg" and result["filename"].endswith(".jpg")


# ---------- Видео: асинхронная задача ----------
def test_video_job_is_queued_with_real_operation_id(gemini_key, monkeypatch):
    captured = {}

    def fake_post(url, json=None, **kwargs):
        captured["url"] = url
        captured["body"] = json
        return FakeResponse(200, json_data={"name": "models/veo-3.1-generate-preview/operations/abc123"})

    monkeypatch.setattr(mg.requests, "post", fake_post)
    events = []
    result, error = mg.generate_media(model_for("veo-3.1-generate-preview"), "закат над морем",
                                      options={"duration": 8}, on_status=events.append,
                                      confirm={"paid": True})
    assert error is None
    assert captured["url"].endswith(":predictLongRunning")
    assert captured["body"]["parameters"]["durationSeconds"] == 8
    assert result["state"] == "queued"
    assert result["job_id"] == "models/veo-3.1-generate-preview/operations/abc123"
    assert "url" not in result, "файла ещё нет — выдумывать результат нельзя"
    assert mg.get_job(result["job_id"])["state"] == "queued"


def test_job_polling_reports_provider_state(gemini_key, monkeypatch):
    monkeypatch.setattr(mg.requests, "post", lambda *a, **k: FakeResponse(200, json_data={
        "name": "models/veo-3.1-generate-preview/operations/op1"}))
    result, error = mg.generate_media(model_for("veo-3.1-generate-preview"), "закат",
                                      confirm={"paid": True})
    assert error is None
    job_id = result["job_id"]

    calls = {"n": 0}

    def fake_get(url, params=None, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return FakeResponse(200, json_data={"name": "operations/op1", "done": False})
        if "operations/" in url:
            return FakeResponse(200, json_data={"name": "operations/op1", "done": True, "response": {
                "generateVideoResponse": {"generatedSamples": [
                    {"video": {"uri": "https://generativelanguage.googleapis.com/v1beta/files/v1?alt=media"}}]}}})
        return FakeResponse(200, content=b"\x00\x00\x00\x18ftypmp42", headers={"Content-Type": "video/mp4"})

    monkeypatch.setattr(mg.requests, "get", fake_get)

    running = mg.poll_job(job_id)
    assert running["state"] == "running"
    assert running["polls"] == 1
    assert running.get("media") is None

    done = mg.poll_job(job_id)
    assert done["state"] == "succeeded"
    assert done["media"]["kind"] == "video"
    assert os.path.isfile(done["media"]["path"])


def test_job_polling_reports_provider_failure(gemini_key, monkeypatch):
    monkeypatch.setattr(mg.requests, "post", lambda *a, **k: FakeResponse(200, json_data={
        "name": "operations/op2"}))
    result, _ = mg.generate_media(model_for("veo-3.1-generate-preview"), "закат", confirm={"paid": True})
    monkeypatch.setattr(mg.requests, "get", lambda *a, **k: FakeResponse(200, json_data={
        "name": "operations/op2", "done": True, "error": {"code": 400, "message": "prompt blocked"}}))
    record = mg.poll_job(result["job_id"])
    assert record["state"] == "failed"
    assert "prompt blocked" in record["error"]


# ---------- Общие проверки ----------
def test_empty_prompt_is_rejected(gemini_key):
    result, error = mg.generate_media(model_for("gemini-3.8-flash-tts"), "   ")
    assert result is None and "Пустой промпт" in error


def test_result_contains_pricing_and_timing(gemini_key, monkeypatch):
    monkeypatch.setattr(mg.requests, "post", lambda *a, **k: FakeResponse(200, json_data={
        "candidates": [{"content": {"parts": [
            {"inlineData": {"mimeType": "audio/wav", "data": base64.b64encode(WAV_BYTES).decode()}}]}}]}))
    result, error = mg.generate_media(model_for("gemini-3.8-flash-lite-tts"), "тест")
    assert error is None
    assert result["pricing_status"] == mg.PRICING_FREE
    assert isinstance(result["elapsed_ms"], int) and result["elapsed_ms"] >= 0
    assert result["provider_name"] == "Google AI Studio"


def test_unconnected_provider_blocks_generation(monkeypatch):
    # GEMINI_API_KEY не задан.
    result, error = mg.generate_media(model_for("gemini-3.8-flash-tts"), "тест")
    assert result is None and "не подключён" in error


def test_saved_files_go_to_media_dir(gemini_key, monkeypatch, tmp_path):
    monkeypatch.setattr(mg.requests, "post", lambda *a, **k: FakeResponse(200, json_data={
        "candidates": [{"content": {"parts": [
            {"inlineData": {"mimeType": "image/png", "data": base64.b64encode(PNG_BYTES).decode()}}]}}]}))
    result, error = mg.generate_media(model_for("gemini-3.1-flash-image"), "тест", confirm={"paid": True})
    assert error is None
    assert os.path.dirname(result["path"]) == mg.MEDIA_DIR
    assert os.path.dirname(mg.MEDIA_DIR) == str(tmp_path)
