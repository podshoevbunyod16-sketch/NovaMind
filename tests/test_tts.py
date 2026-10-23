"""Озвучка ответов: подготовка текста, части, настройки, API.

Провайдеры в тестах не вызываются по сети: `media_generation.generate_media`
подменяется функцией, которая кладёт в каталог медиа маленький wav — ровно так
же, как это делает настоящий бэкенд через `save_media`. Поэтому тесты проверяют
нашу логику (нарезка, ретраи, отмена, ошибки, лимиты), а не доступность
внешнего сервиса.
"""
import json
import struct

import pytest

import config
import media_generation as mg
import security
import tts_service

WAV_HEADER = b"RIFF" + struct.pack("<I", 36) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 1, 8000, 8000, 1, 8) + b"data" + struct.pack("<I", 0)


def fake_audio(model, prompt, options=None, on_status=None, confirm=None):
    """Успешный TTS-бэкенд: возвращает сохранённый файл и текст запроса."""
    if on_status:
        on_status(f"Генерирую {prompt[:20]}")
    result = mg.save_media("audio", WAV_HEADER, "audio/wav", prefix="groq_tts")
    result["prompt"] = prompt
    result["voice"] = (options or {}).get("voice", "")
    return result, None


@pytest.fixture
def tts_provider(monkeypatch, gemini_key):
    """Настроенный провайдер: ключ есть, запросы подменены."""
    monkeypatch.setattr(mg, "generate_media", fake_audio)
    return "google_ai_studio"


def _events(payload):
    return [json.loads(line) for line in payload.splitlines() if line.strip()]


# ══════════════ подготовка текста ══════════════

def test_markdown_is_not_read_aloud():
    text = ("## Итог\n\nСмотри **важно** и [документацию](https://example.com/doc).\n"
            "- первый пункт\n- второй пункт\n\n> цитата\n\n`code_inline` и https://ya.ru\n")
    speech = tts_service.speech_text(text)
    for marker in ("##", "**", "](", "https://", "`", ">"):
        assert marker not in speech
    assert "важно" in speech and "документацию" in speech
    assert "первый пункт" in speech and "второй пункт" in speech
    assert "цитата" in speech


def test_links_are_spoken_as_words():
    speech = tts_service.speech_text("Подробнее: https://example.com/a?b=1 — там всё есть.")
    assert "ссылка" in speech
    assert "http" not in speech


def test_tables_are_read_row_by_row():
    speech = tts_service.speech_text(
        "| Модель | Цена |\n|---|---|\n| Flash | 0 |\n| Pro | 5 |")
    assert "Модель — Цена" in speech
    assert "Flash — 0" in speech
    assert "---" not in speech


def test_code_blocks_are_skipped_by_default():
    text = "Вот пример:\n```python\nprint('секретный код')\n```\nГотово."
    assert "секретный код" not in tts_service.speech_text(text)
    assert "секретный код" in tts_service.speech_text(text, read_code=True)


def test_original_message_is_not_modified():
    text = "# Заголовок\n\n**жирный** и `код`"
    snapshot = str(text)
    tts_service.speech_text(text)
    assert text == snapshot


def test_empty_text_has_no_speech():
    assert tts_service.speech_text("   \n\n") == ""
    assert tts_service.speech_text("```python\nprint(1)\n```") == ""


def test_long_text_is_split_without_breaking_words():
    text = " ".join(f"Предложение номер {index} про важные вещи." for index in range(60))
    chunks = tts_service.split_text(text, limit=120, max_chunks=50)
    assert len(chunks) > 3
    for chunk in chunks:
        assert len(chunk) <= 120
        assert not chunk.startswith(" ")
        assert not chunk.endswith(" ")
    # Порядок сохранён и ничего не потеряно.
    assert " ".join(chunks).split() == text.split()


def test_single_long_sentence_is_split_on_words():
    text = "слово " * 200
    chunks = tts_service.split_text(text.strip(), limit=100, max_chunks=500)
    assert all(len(chunk) <= 100 for chunk in chunks)
    assert all("  " not in chunk for chunk in chunks)
    assert " ".join(chunks).split() == text.split()


def test_chunks_are_limited():
    chunks = tts_service.split_text("Фраза. " * 200, limit=80, max_chunks=3)
    assert len(chunks) == 3


def test_language_detection():
    assert tts_service.detect_language("Привет, как дела?") == "ru"
    assert tts_service.detect_language("Hello, how are you?") == "en"


# ══════════════ каталог голосов и статус ══════════════

def test_status_is_honest_without_keys():
    status = tts_service.status()
    assert status["available"] is False
    assert status["mode"] == "browser"
    assert "GEMINI_API_KEY" in status["error"]
    assert all(not provider["available"] for provider in status["providers"])


def test_status_lists_real_voices_when_key_present(gemini_key):
    status = tts_service.status()
    assert status["available"] is True
    assert status["mode"] == "ai"
    assert status["provider"] == "google_ai_studio"
    voices = tts_service.voice_catalog("google_ai_studio")
    assert {voice["id"] for voice in voices} >= {"Kore", "Puck"}
    assert all(voice["available"] for voice in voices)


def test_groq_voices_are_english_only():
    voices = [voice for voice in tts_service.voice_catalog("groq")]
    assert voices and all(voice["languages"] == ["en"] for voice in voices)


def test_custom_endpoint_appears_when_url_set(monkeypatch):
    monkeypatch.setenv("AUDIO_TTS_URL", "https://tts.example.com/v1/audio/speech")
    providers = {item["id"]: item for item in tts_service.provider_choices()}
    assert providers["custom"]["available"] is True
    model, error = tts_service.resolve_model("custom")
    assert error == "" and model["backend"] == "custom_tts"


def test_unknown_voice_is_rejected():
    with pytest.raises(ValueError):
        tts_service.save_settings({"voice": "нет-такого-голоса"})


def test_unknown_provider_is_rejected():
    with pytest.raises(ValueError):
        tts_service.save_settings({"provider": "unknown-tts"})


def test_speech_settings_are_persisted():
    saved = tts_service.save_settings({"auto_speak": True, "lang": "ru", "read_code": True})
    assert saved["auto_speak"] is True and saved["lang"] == "ru"
    assert config.speech_settings["auto_speak"] is True
    with open(config.RUNTIME_SETTINGS_FILE, encoding="utf-8") as handle:
        stored = json.load(handle)
    assert stored["speech"]["auto_speak"] is True
    assert stored["speech"]["lang"] == "ru"


# ══════════════ синтез ══════════════

def test_synthesize_returns_audio_for_each_chunk(tts_provider, monkeypatch):
    monkeypatch.setattr(tts_service, "CHUNK_CHARS", 60)
    text = ("Первая фраза для проверки озвучки. " * 4).strip()
    chunks, error = tts_service.synthesize_all(text)
    assert error == ""
    assert len(chunks) > 1
    assert all(chunk["mime"] == "audio/wav" for chunk in chunks)
    assert all(chunk["url"].startswith("/media/") for chunk in chunks)
    assert [chunk["index"] for chunk in chunks] == sorted(chunk["index"] for chunk in chunks)


def test_start_event_describes_provider_and_voice(tts_provider):
    events = list(tts_service.synthesize("Привет!", voice="Puck"))
    start = events[0]
    assert start["type"] == "start"
    assert start["provider"] == "google_ai_studio"
    assert start["voice"] == "Puck"
    assert start["mode"] == "ai"
    assert start["job_id"]


def test_missing_provider_reports_browser_mode():
    events = list(tts_service.synthesize("Привет!"))
    assert events[0]["type"] == "error"
    assert events[0]["mode"] == "browser"


def test_provider_error_is_reported(monkeypatch, gemini_key):
    monkeypatch.setattr(mg, "generate_media", lambda *a, **k: (None, "Google AI Studio: таймаут запроса"))
    events = list(tts_service.synthesize("Привет!"))
    assert events[-1]["type"] == "error"
    assert "таймаут" in events[-1]["error"].lower()


def test_transient_error_is_retried_once(monkeypatch, gemini_key):
    calls = {"count": 0}

    def flaky(model, prompt, options=None, on_status=None, confirm=None):
        calls["count"] += 1
        if calls["count"] == 1:
            return None, "Groq: таймаут запроса синтеза речи"
        return fake_audio(model, prompt, options)

    monkeypatch.setattr(mg, "generate_media", flaky)
    chunks, error = tts_service.synthesize_all("Привет, это проверка повтора.")
    assert error == ""
    assert calls["count"] == 2
    assert chunks


def test_permanent_error_is_not_retried(monkeypatch, gemini_key):
    calls = {"count": 0}

    def broken(model, prompt, options=None, on_status=None, confirm=None):
        calls["count"] += 1
        return None, "Google AI Studio вернул HTTP 400: bad request"

    monkeypatch.setattr(mg, "generate_media", broken)
    _, error = tts_service.synthesize_all("Привет!")
    assert "400" in error
    assert calls["count"] == 1


def test_cancel_between_chunks_stops_generation(tts_provider, monkeypatch):
    monkeypatch.setattr(tts_service, "CHUNK_CHARS", 40)
    text = "Одна фраза. Другая фраза. Третья фраза. Четвёртая фраза."
    produced = {"count": 0}

    def counting(model, prompt, options=None, on_status=None, confirm=None):
        produced["count"] += 1
        return fake_audio(model, prompt, options)

    monkeypatch.setattr(mg, "generate_media", counting)

    collected = []
    for event in tts_service.synthesize(text):
        collected.append(event)
        if event["type"] == "chunk":
            tts_service.cancel_job(collected[0]["job_id"])
    assert collected[-1]["type"] == "cancelled"
    assert produced["count"] < 4


def test_non_audio_result_is_not_played(monkeypatch, gemini_key):
    def image_instead(model, prompt, options=None, on_status=None, confirm=None):
        return {"kind": "audio", "url": "/media/x.png", "mime": "image/png"}, None

    monkeypatch.setattr(mg, "generate_media", image_instead)
    events = list(tts_service.synthesize("Привет!"))
    errors = [event for event in events if event["type"] == "chunk_error"]
    assert errors and "не аудио" in errors[0]["error"]
    assert not [event for event in events if event["type"] == "chunk"]


def test_cleanup_removes_only_old_tts_files(monkeypatch, tmp_path):
    monkeypatch.setattr(mg, "MEDIA_DIR", str(tmp_path))
    (tmp_path / "groq_tts_old.wav").write_bytes(WAV_HEADER)
    (tmp_path / "groq_tts_new.wav").write_bytes(WAV_HEADER)
    (tmp_path / "fox.png").write_bytes(b"png")
    import os
    import time
    old = time.time() - 7200
    os.utime(tmp_path / "groq_tts_old.wav", (old, old))
    removed = tts_service.cleanup_media(max_age=3600, keep=10)
    assert removed == 1
    assert not (tmp_path / "groq_tts_old.wav").exists()
    assert (tmp_path / "groq_tts_new.wav").exists()
    assert (tmp_path / "fox.png").exists()          # чужие файлы не трогаем


# ══════════════ HTTP API ══════════════

def test_status_endpoint(client):
    response = client.get("/api/tts/status")
    assert response.status_code == 200
    data = response.get_json()
    assert data["available"] is False
    assert data["voices"] and data["providers"]
    assert "settings" in data and "limits" in data
    assert data["rate_limit"]["limit"] > 0


def test_voices_endpoint_filters(client):
    all_voices = client.get("/api/tts/voices").get_json()
    assert all_voices["count"] == len(all_voices["voices"])
    english = client.get("/api/tts/voices?lang=en").get_json()
    assert english["count"] <= all_voices["count"]
    assert all("en" in voice["languages"] for voice in english["voices"])
    google = client.get("/api/tts/voices?provider=google_ai_studio").get_json()
    assert google["voices"] and all(v["provider"] == "google_ai_studio" for v in google["voices"])


def test_settings_endpoint_roundtrip(client, tts_provider):
    saved = client.post("/api/tts/settings", json={"auto_speak": True, "voice": "Puck"})
    assert saved.status_code == 200
    assert saved.get_json()["settings"]["auto_speak"] is True
    data = client.get("/api/tts/settings").get_json()
    assert data["settings"]["auto_speak"] is True
    assert data["settings"]["voice"] == "Puck"
    # Настройки озвучки не затирают выбранную модель чата.
    assert "provider" in config.speech_settings


def test_settings_endpoint_rejects_bad_values(client):
    assert client.post("/api/tts/settings", json={"voice": "нет"}).status_code == 400
    assert client.post("/api/tts/settings", json={"lang": "kz"}).status_code == 400
    assert client.post("/api/tts/settings", json={"provider": "nope"}).status_code == 400


def test_speech_endpoint_requires_text(client):
    assert client.post("/api/tts/speech", json={}).status_code == 400
    assert client.post("/api/tts/speech", json={"text": "   "}).status_code == 400
    assert client.post("/api/tts/speech", json=[1, 2]).status_code == 400


def test_speech_endpoint_limits_text_length(client):
    too_long = "а" * (tts_service.MAX_TEXT_CHARS + 1)
    response = client.post("/api/tts/speech", json={"text": too_long})
    assert response.status_code == 413
    assert response.get_json()["limit"] == tts_service.MAX_TEXT_CHARS


def test_speech_endpoint_without_provider_says_so(client):
    response = client.post("/api/tts/speech", json={"text": "Привет!"})
    assert response.status_code == 200
    events = _events(response.get_data(as_text=True))
    assert events[0]["type"] == "error"
    assert events[0]["mode"] == "browser"


def test_speech_endpoint_streams_audio(client, tts_provider):
    response = client.post("/api/tts/speech", json={"text": "Привет! Это озвучка.", "voice": "Puck"})
    assert response.status_code == 200
    assert response.mimetype == "application/x-ndjson"
    events = _events(response.get_data(as_text=True))
    assert events[0]["type"] == "start"
    chunks = [event for event in events if event["type"] == "chunk"]
    assert chunks and chunks[0]["url"].startswith("/media/")
    assert events[-1]["type"] == "done"
    # Файл действительно создан и отдаётся по ссылке.
    filename = chunks[0]["url"].split("/")[-1]
    from routes.media import MEDIA_DIR
    import os
    assert os.path.isfile(os.path.join(MEDIA_DIR, filename))


def test_speech_endpoint_reports_provider_failure(client, monkeypatch, gemini_key):
    monkeypatch.setattr(mg, "generate_media", lambda *a, **k: (None, "Google AI Studio вернул HTTP 500: boom"))
    response = client.post("/api/tts/speech", json={"text": "Привет!"})
    assert response.status_code == 200
    events = _events(response.get_data(as_text=True))
    assert events[-1]["type"] == "error"
    assert "500" in events[-1]["error"]


def test_speech_endpoint_is_rate_limited(client, tts_provider, monkeypatch):
    monkeypatch.setenv("TTS_RATE_GUEST", "2")
    security.tts_limiter.clear()
    for _ in range(2):
        assert client.post("/api/tts/speech", json={"text": "Привет"}).status_code == 200
    blocked = client.post("/api/tts/speech", json={"text": "Привет"})
    assert blocked.status_code == 429
    assert blocked.headers.get("Retry-After")
    security.tts_limiter.clear()


def test_cancel_endpoint(client):
    empty = client.post("/api/tts/cancel", json={})
    assert empty.status_code == 400
    job = tts_service._register_job(3)
    response = client.post("/api/tts/cancel", json={"job_id": job.id})
    assert response.get_json()["cancelled"] is True
    assert job.cancel.is_set()
    assert client.post("/api/tts/cancel", json={"job_id": "нет"}).get_json()["cancelled"] is False


def test_chat_keeps_working_when_tts_is_broken(client, monkeypatch):
    """Ошибка озвучки не должна ломать обычную генерацию текста."""
    def boom(*args, **kwargs):
        raise RuntimeError("TTS упал")

    monkeypatch.setattr(tts_service, "synthesize", boom)
    response = client.post("/api/tts/speech", json={"text": "Привет!"})
    events = _events(response.get_data(as_text=True))
    assert events[-1]["type"] == "error"
    assert "TTS упал" in events[-1]["error"]
    assert client.get("/api/tts/status").status_code == 200


def test_legacy_media_tts_endpoint_still_works(client, tts_provider):
    response = client.post("/api/media/tts", json={"text": "Привет из старого API"})
    assert response.status_code == 200
    assert response.get_json()["success"] is True
