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


# ---------- Groq TTS ----------
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
    result, error = mg.generate_media(model_for("playai-tts", "groq"), "Съешь ещё этих булок")
    assert error is None
    assert captured["url"] == "https://api.groq.com/openai/v1/audio/speech"
    assert captured["body"]["model"] == "playai-tts"
    assert captured["headers"]["Authorization"] == "Bearer gsk-test"
    assert result["kind"] == "audio" and result["provider"] == "groq"


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
