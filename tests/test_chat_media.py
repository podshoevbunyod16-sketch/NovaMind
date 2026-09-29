"""Интеграция медиа-генерации с обычным чатом: промпт в поле чата → медиа в переписке."""
import base64
import json

import media_generation as mg
from conftest import FakeResponse

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
WAV = b"RIFF$\x00\x00\x00WAVEfmt " + b"\x00" * 16


def audio_response():
    return FakeResponse(200, json_data={"candidates": [{"content": {"parts": [
        {"inlineData": {"mimeType": "audio/wav", "data": base64.b64encode(WAV).decode()}}]}}]})


def select_tts(client):
    response = client.post("/api/media/select", json={
        "provider": "google_ai_studio", "model": "gemini-3.8-flash-tts"})
    assert response.status_code == 200


def ndjson_events(raw: str):
    return [json.loads(line) for line in raw.splitlines() if line.strip()]


# ---------- /send ----------
def test_send_with_media_returns_media_and_saves_marker(client, gemini_key, monkeypatch):
    select_tts(client)
    monkeypatch.setattr(mg.requests, "post", lambda *a, **k: audio_response())

    response = client.post("/send", json={"message": "Озвучь приветствие", "media": True})
    assert response.status_code == 200
    data = response.get_json()
    assert data["media"]["kind"] == "audio"
    assert data["media"]["url"].startswith("/media/")
    assert data["chat_id"]

    history = client.get("/api/history").get_json()["history"]
    markers = [m for m in history if m["role"] == "assistant" and m["content"].startswith("MEDIA_RESULT:")]
    assert len(markers) == 1
    payload = json.loads(markers[0]["content"][len("MEDIA_RESULT:"):])
    assert payload["kind"] == "audio"
    assert payload["url"] == data["media"]["url"]
    assert payload["filename"] == data["media"]["filename"]


def test_send_without_media_selection_is_rejected(client, gemini_key):
    response = client.post("/send", json={"message": "сделай картинку", "media": True})
    assert response.status_code == 400
    assert "не выбрана" in response.get_json()["error"]


def test_send_without_media_flag_keeps_text_chat(client, gemini_key, monkeypatch):
    select_tts(client)
    monkeypatch.setattr("routes.chat.chat_completion",
                        lambda *a, **k: ("текстовый ответ", {"provider": "groq", "model": "test"}))
    response = client.post("/send", json={"message": "обычный вопрос"})
    assert response.status_code == 200
    assert response.get_json()["reply"] == "текстовый ответ"
    assert "media" not in response.get_json()


def test_send_reports_provider_error_in_chat(client, gemini_key, monkeypatch):
    select_tts(client)
    monkeypatch.setattr(mg.requests, "post", lambda *a, **k: FakeResponse(400, json_data={
        "error": {"code": 400, "message": "Invalid JSON payload"}}))
    response = client.post("/send", json={"message": "текст", "media": True})
    assert response.status_code == 502
    assert "HTTP 400" in response.get_json()["error"]

    history = client.get("/api/history").get_json()["history"]
    assert any(m["role"] == "assistant" and "HTTP 400" in m["content"] for m in history)


# ---------- /send_stream ----------
def test_send_stream_emits_real_statuses_then_media(client, gemini_key, monkeypatch):
    select_tts(client)
    monkeypatch.setattr(mg.requests, "post", lambda *a, **k: audio_response())

    response = client.post("/send_stream", json={"message": "Озвучь текст", "media": True})
    assert response.status_code == 200
    events = ndjson_events(response.get_data(as_text=True))

    kinds = [e.get("status") for e in events if e.get("status")]
    assert kinds[0] == "started"
    assert "progress" in kinds, "должны передаваться реальные шаги запроса"
    assert any(e.get("media") for e in events)
    assert events[-1] == {"done": True}

    # Никакого выдуманного прогресса: только текст статуса и реальное время.
    for event in events:
        assert "percent" not in event
        assert "progress_value" not in event
        if "elapsed_ms" in event:
            assert isinstance(event["elapsed_ms"], int)

    media_event = next(e for e in events if e.get("media"))
    assert media_event["media"]["kind"] == "audio"
    assert "Отправка запроса" in " ".join(e.get("message", "") for e in events)


def test_send_stream_without_selection_returns_400(client, gemini_key):
    response = client.post("/send_stream", json={"message": "картинку", "media": True})
    assert response.status_code == 400
    assert "не выбрана" in response.get_json()["error"]


def test_send_stream_reports_error_event(client, gemini_key, monkeypatch):
    select_tts(client)
    monkeypatch.setattr(mg.requests, "post", lambda *a, **k: FakeResponse(429, json_data={
        "error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "quota"}}))
    response = client.post("/send_stream", json={"message": "текст", "media": True})
    events = ndjson_events(response.get_data(as_text=True))
    errors = [e["error"] for e in events if e.get("error")]
    assert errors and "RESOURCE_EXHAUSTED" in errors[0]


def test_stream_result_is_stored_in_history(client, gemini_key, monkeypatch):
    select_tts(client)
    monkeypatch.setattr(mg.requests, "post", lambda *a, **k: audio_response())
    response = client.post("/send_stream", json={"message": "Озвучь", "media": True})
    response.get_data()  # генератор потока выполняется при чтении тела
    history = client.get("/api/history").get_json()["history"]
    assert any(m["content"].startswith("MEDIA_RESULT:") for m in history)


# ---------- Команда /image (регрессия: раньше падала на NameError) ----------
def test_image_command_uses_media_engine(client, monkeypatch):
    captured = {}

    def fake_get(url, **kwargs):
        captured["url"] = url
        return FakeResponse(200, content=PNG, headers={"Content-Type": "image/png"})

    monkeypatch.setattr(mg.requests, "get", fake_get)
    response = client.post("/command", json={"command": "/image рыжий кот на крыше"})
    assert response.status_code == 200
    body = response.get_json()
    assert "result" in body and body.get("error") is None
    assert "image.pollinations.ai" in captured["url"]
    assert "![Image](/media/" in body["result"]


def test_unknown_command_still_reports_error(client):
    response = client.post("/command", json={"command": "/nothing-here"})
    assert response.status_code == 200
    assert "Неизвестная команда" in response.get_json()["error"]
