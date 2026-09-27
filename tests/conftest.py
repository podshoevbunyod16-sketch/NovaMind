"""Общие фикстуры: изолированное окружение без API-ключей и тестовый клиент Flask."""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

KEY_VARS = (
    "GEMINI_API_KEY", "GOOGLE_AI_STUDIO_KEY", "OPENROUTER_API_KEY", "GROQ_API_KEY",
    "GROQ_API_KEY_1", "GROQ_API_KEY_2", "CEREBRAS_API_KEY", "OPENAI_API_KEY",
    "IMAGE_GENERATION_URL", "IMAGE_MODEL", "IMAGE_API_KEY",
    "AUDIO_TTS_URL", "AUDIO_TTS_MODEL", "AUDIO_API_KEY",
    "VIDEO_API_URL", "VIDEO_MODEL", "VIDEO_API_KEY", "POLLINATIONS_TOKEN",
)


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """Каждый тест: чистые ключи, своя папка медиа, свои runtime-настройки."""
    for var in KEY_VARS:
        monkeypatch.delenv(var, raising=False)

    import media_generation as mg
    media_dir = tmp_path / "generated_media"
    monkeypatch.setattr(mg, "MEDIA_DIR", str(media_dir))
    mg.reset_catalog_cache()

    import config
    monkeypatch.setattr(config, "RUNTIME_SETTINGS_FILE", str(tmp_path / "runtime_settings.json"))
    config.save_selected_media_model({})

    import groq_rotation
    monkeypatch.setattr(groq_rotation, "GROQ_KEYS", [], raising=False)

    yield

    config.save_selected_media_model({})
    mg.reset_catalog_cache()


@pytest.fixture
def gemini_key(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")


@pytest.fixture
def client(tmp_path, monkeypatch, isolated_env):
    """Flask test client с отдельной базой данных (репозиторий не трогает)."""
    import database
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "test_chats.db"))
    database.init_db()

    import routes.media as media_routes
    monkeypatch.setattr(media_routes, "MEDIA_DIR", str(tmp_path / "generated_media"))

    from app import app as flask_app
    flask_app.config.update(TESTING=True)
    with flask_app.test_client() as test_client:
        yield test_client


class FakeResponse:
    """Мини-замена requests.Response для тестов без сети."""

    def __init__(self, status_code=200, json_data=None, content=b"", headers=None, text=""):
        self.status_code = status_code
        self._json = json_data
        self.content = content
        self.headers = headers or {}
        self.text = text or ("" if json_data is None else str(json_data))

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"HTTP {self.status_code}")
