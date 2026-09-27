"""HTTP-эндпоинты единой медиа-панели."""
import base64

import media_generation as mg
from conftest import FakeResponse

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


# ---------- Каталог ----------
def test_models_endpoint_returns_catalog_and_statuses(client, gemini_key):
    response = client.get("/api/media/models?type=audio")
    assert response.status_code == 200
    data = response.get_json()
    assert data["type"] == "audio"
    assert data["count"] > 0
    assert all(m["media_type"] == "audio" for m in data["models"])
    assert set(data["providers"]) == set(mg.MEDIA_PROVIDERS)
    assert data["providers"]["google_ai_studio"] is True
    assert data["pricing_verified_at"]


def test_models_endpoint_search_and_type_filter(client, gemini_key):
    found = client.get("/api/media/models?q=tts&include_paid=1").get_json()
    assert found["count"] > 0
    assert all("tts" in (m["id"] + m["name"] + m["provider"]).lower() or "tts" in m["description"].lower()
               for m in found["models"])
    empty = client.get("/api/media/models?q=qqq-zzz").get_json()
    assert empty["count"] == 0 and empty["models"] == []


def test_models_endpoint_rejects_bad_type(client):
    assert client.get("/api/media/models?type=hologram").status_code == 400


def test_models_endpoint_hides_paid_by_default(client, gemini_key):
    ids = {m["id"] for m in client.get("/api/media/models").get_json()["models"]}
    assert "gemini-3.1-flash-image" not in ids
    paid = {m["id"] for m in client.get("/api/media/models?include_paid=1").get_json()["models"]}
    assert "gemini-3.1-flash-image" in paid


def test_providers_endpoint(client, gemini_key):
    data = client.get("/api/media/providers").get_json()
    connected = {p["id"]: p["connected"] for p in data["providers"]}
    assert connected["google_ai_studio"] is True
    assert connected["pollinations"] is True
    assert connected["openrouter"] is False
    assert data["selection"] is None


# ---------- Выбор модели ----------
def test_select_free_model(client, gemini_key):
    response = client.post("/api/media/select", json={
        "provider": "google_ai_studio", "model": "gemini-3.8-flash-tts"})
    assert response.status_code == 200
    selection = response.get_json()["selection"]
    assert selection["model"] == "gemini-3.8-flash-tts"
    assert selection["pricing_status"] == mg.PRICING_FREE

    stored = client.get("/api/media/selection").get_json()["selection"]
    assert stored["model"] == "gemini-3.8-flash-tts"


def test_select_paid_model_requires_confirmation(client, gemini_key):
    response = client.post("/api/media/select", json={
        "provider": "google_ai_studio", "model": "gemini-3.1-flash-image"})
    assert response.status_code == 409
    body = response.get_json()
    assert body["requires"]["paid"] is True
    assert "не используются автоматически" in body["error"]
    assert client.get("/api/media/selection").get_json()["selection"] is None

    confirmed = client.post("/api/media/select", json={
        "provider": "google_ai_studio", "model": "gemini-3.1-flash-image",
        "confirm": {"paid": True}})
    assert confirmed.status_code == 200
    assert confirmed.get_json()["selection"]["pricing_status"] == mg.PRICING_PAID


def test_select_trial_model_requires_confirmation(client):
    response = client.post("/api/media/select", json={"provider": "pollinations", "model": "gptimage"})
    assert response.status_code == 409
    assert response.get_json()["requires"]["trial"] is True

    ok = client.post("/api/media/select", json={
        "provider": "pollinations", "model": "gptimage", "confirm": {"trial": True}})
    assert ok.status_code == 200


def test_select_unknown_model(client):
    response = client.post("/api/media/select", json={"provider": "google_ai_studio", "model": "nope"})
    assert response.status_code == 404


def test_select_for_unconnected_provider(client):
    # OPENROUTER_API_KEY не задан — провайдер не подключён.
    models = client.get("/api/media/models?include_paid=1&include_unknown=1").get_json()["models"]
    openrouter = [m for m in models if m["provider"] == "openrouter"]
    assert openrouter == [] or all(m["provider_connected"] is False for m in openrouter)


def test_clear_selection(client, gemini_key):
    client.post("/api/media/select", json={"provider": "google_ai_studio", "model": "gemini-3.8-flash-tts"})
    assert client.get("/api/media/selection").get_json()["selection"] is not None
    cleared = client.post("/api/media/select", json={"clear": True})
    assert cleared.status_code == 200
    assert client.get("/api/media/selection").get_json()["selection"] is None


# ---------- Генерация ----------
def test_generate_endpoint_uses_selected_model(client, gemini_key, monkeypatch):
    client.post("/api/media/select", json={"provider": "google_ai_studio", "model": "gemini-3.8-flash-tts"})
    monkeypatch.setattr(mg.requests, "post", lambda *a, **k: FakeResponse(200, json_data={
        "candidates": [{"content": {"parts": [
            {"inlineData": {"mimeType": "audio/wav", "data": base64.b64encode(b"RIFFxxxx").decode()}}]}}]}))

    response = client.post("/api/media/generate", json={"prompt": "Привет"})
    assert response.status_code == 200
    data = response.get_json()
    assert data["status"] == "done"
    assert data["media"]["kind"] == "audio"
    assert data["media"]["url"].startswith("/media/")
    assert data["events"], "должны возвращаться реальные статусы выполнения"
    assert data["model"]["model"] == "gemini-3.8-flash-tts"


def test_generate_endpoint_requires_prompt(client, gemini_key):
    response = client.post("/api/media/generate", json={"prompt": "  "})
    assert response.status_code == 400


def test_generate_endpoint_reports_provider_error(client, gemini_key, monkeypatch):
    monkeypatch.setattr(mg.requests, "post", lambda *a, **k: FakeResponse(403, json_data={
        "error": {"code": 403, "message": "API key not valid"}}))
    response = client.post("/api/media/generate", json={
        "provider": "google_ai_studio", "model": "gemini-3.8-flash-tts", "prompt": "текст"})
    assert response.status_code == 502
    body = response.get_json()
    assert body["status"] == "error"
    assert "HTTP 403" in body["error"] and "API key not valid" in body["error"]


def test_generate_endpoint_refuses_paid_without_confirmation(client, gemini_key):
    response = client.post("/api/media/generate", json={
        "provider": "google_ai_studio", "model": "gemini-3.1-flash-image", "prompt": "закат"})
    assert response.status_code == 400
    assert "не используются автоматически" in response.get_json()["error"]
    assert response.get_json()["requires"]["paid"] is True


def test_generated_file_is_served_with_download(client, gemini_key, monkeypatch):
    monkeypatch.setattr(mg.requests, "post", lambda *a, **k: FakeResponse(200, json_data={
        "candidates": [{"content": {"parts": [
            {"inlineData": {"mimeType": "image/png", "data": base64.b64encode(PNG).decode()}}]}}]}))
    response = client.post("/api/media/generate", json={
        "provider": "google_ai_studio", "model": "gemini-3.1-flash-image",
        "prompt": "закат", "confirm": {"paid": True}})
    assert response.status_code == 200
    url = response.get_json()["media"]["url"]

    inline = client.get(url)
    assert inline.status_code == 200 and inline.data == PNG

    download = client.get(url + "?download=1")
    assert download.status_code == 200
    assert "attachment" in download.headers.get("Content-Disposition", "")


def test_job_endpoint_404_for_unknown_job(client):
    assert client.get("/api/media/jobs/unknown-job").status_code == 404


def test_job_endpoint_reports_state(client, gemini_key, monkeypatch):
    monkeypatch.setattr(mg.requests, "post", lambda *a, **k: FakeResponse(200, json_data={
        "name": "operations/xyz"}))
    started = client.post("/api/media/generate", json={
        "provider": "google_ai_studio", "model": "veo-3.1-generate-preview",
        "prompt": "закат", "confirm": {"paid": True}})
    assert started.status_code == 200
    payload = started.get_json()
    assert payload["status"] == "queued"
    job_id = payload["media"]["job_id"]

    monkeypatch.setattr(mg.requests, "get", lambda *a, **k: FakeResponse(200, json_data={
        "name": "operations/xyz", "done": False}))
    polled = client.get(f"/api/media/jobs/{job_id}").get_json()
    assert polled["state"] == "running"
    assert polled["polls"] == 1
    assert polled["events"]
