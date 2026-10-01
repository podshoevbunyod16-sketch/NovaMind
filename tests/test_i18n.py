"""
Язык интерфейса на сервере: тоҷикӣ (по умолчанию), русский, English.

Проверяется:
  * язык берётся из тела запроса (lang), затем из cookie nova_lang,
    без них — таджикский;
  * статусы агента (этапы, шаги) приходят на языке интерфейса;
  * в системный промпт ИИ дописано правило языка ответа — и в агенте,
    и в обычном чате /send_stream;
  * краткий статус «Пробую <модель>» распознаётся на любом языке;
  * на всех страницах подключён static/i18n.js, а в словаре фронтенда
    есть переводы для статичного текста.
"""
import json
import re
from pathlib import Path

import pytest

import i18n
import media_agent
import routes.agent as agent_routes
import routes.chat as chat_routes

ROOT = Path(__file__).resolve().parent.parent


def _ndjson(response):
    return [json.loads(line) for line in response.get_data(as_text=True).splitlines() if line.strip()]


@pytest.fixture
def fake_media(monkeypatch):
    def fake_generate(kind, prompt, options=None, wanted_model="", on_status=None):
        if on_status:
            on_status(i18n.tr("Пробую {name} ({provider})", name="Flux", provider="Pollinations"))
        model = {"id": "flux", "name": "Flux", "provider_name": "Pollinations"}
        return ({"kind": kind, "url": "/media/x.png", "filename": "x.png", "mime": "image/png",
                 "model": "flux", "model_name": "Flux", "provider": "pollinations",
                 "provider_name": "Pollinations", "elapsed_ms": 900}, model, [])
    monkeypatch.setattr(media_agent, "generate", fake_generate)


@pytest.fixture
def stub_model(monkeypatch):
    seen = []
    answers = ['{"action":"image","prompt":"a red fox","aspect":"1:1"}', "Тайёр!"]

    def fake_stream(messages, **kwargs):
        seen.append(kwargs.get("system", ""))
        text = answers.pop(0) if answers else "Тайёр"
        yield "token", text
        yield "done", ""

    monkeypatch.setattr(agent_routes, "chat_stream", fake_stream)
    monkeypatch.setattr(agent_routes, "chat_completion", lambda messages, **kw: ("ok", {}))
    return seen


# ───────────────────────── выбор языка ─────────────────────────

def test_tr_translates_and_formats():
    assert i18n.tr("Читаю {path}", lang="tg", path="a.py") == "a.py-ро мехонам"
    assert i18n.tr("Читаю {path}", lang="en", path="a.py") == "Reading a.py"
    assert i18n.tr("Читаю {path}", lang="ru", path="a.py") == "Читаю a.py"
    assert i18n.tr("Строки нет в словаре", lang="en") == "Строки нет в словаре"
    assert i18n.normalize("tg-TJ") == "tg" and i18n.normalize("de") == ""


def test_language_from_body_cookie_and_default():
    from app import app as flask_app
    with flask_app.test_request_context("/x", method="POST", json={"message": "hi"}):
        assert i18n.current_lang() == "tg"                      # по умолчанию — таджикский
    with flask_app.test_request_context("/x", headers={"Cookie": "nova_lang=en"}):
        assert i18n.current_lang() == "en"
    with flask_app.test_request_context("/x", method="POST", json={"lang": "ru"},
                                        headers={"Cookie": "nova_lang=en"}):
        assert i18n.current_lang() == "ru"                      # тело важнее cookie
    assert i18n.current_lang() == "ru"                          # вне запроса — исходный текст


def test_language_rule_is_added_once():
    system = i18n.with_language("Ты — Khirad.", "tg")
    assert "таджикском" in system and "тоҷикӣ" in system
    assert i18n.with_language(system, "en") == system           # повторно не дописывается
    assert "Always reply in English" in i18n.with_language("", "en")


def test_attempt_status_is_recognised_in_any_language():
    for lang in i18n.LANGS:
        message = i18n.tr("Пробую {name} ({provider})", lang=lang, name="Flux", provider="Pollinations")
        assert media_agent.is_attempt(message), lang
    assert not media_agent.is_attempt("Отправка запроса к Pollinations")


# ───────────────────────── агент и чат ─────────────────────────

def test_agent_speaks_tajik_by_default(google_client, stub_model, fake_media):
    google_client.delete_cookie("nova_lang")
    events = _ndjson(google_client.post("/api/agent/stream", json={"message": "расм каш"}))
    stage = next(event for event in events if event["type"] == "stage")
    assert stage["title"] == "Тасвир мекашам"
    steps = [event["text"] for event in events if event["type"] == "step"]
    assert any(text.startswith("Кӯшиш мекунам") for text in steps), steps
    assert "тоҷикӣ" in stub_model[0]                              # ИИ отвечает по-таджикски


def test_agent_follows_language_from_request(google_client, stub_model, fake_media):
    events = _ndjson(google_client.post("/api/agent/stream",
                                        json={"message": "draw a fox", "lang": "en"}))
    stage = next(event for event in events if event["type"] == "stage")
    assert stage["title"] == "Drawing an image"
    assert "Always reply in English" in stub_model[0]


def test_agent_status_hint_is_translated(client):
    client.set_cookie("nova_lang", "en")
    hint = client.get("/api/agent/status").get_json()["hint"]
    assert hint.startswith("Sign in")


def test_send_stream_adds_language_rule(client, monkeypatch):
    seen = []

    def fake_stream(messages, **kwargs):
        seen.append(kwargs.get("system", ""))
        yield "token", "Салом!"
        yield "done", ""

    monkeypatch.setattr(chat_routes, "chat_stream", fake_stream)
    response = client.post("/send_stream", json={"message": "салом", "lang": "tg"})
    assert "Салом!" in response.get_data(as_text=True)
    assert seen and "тоҷикӣ" in seen[0]


# ───────────────────────── фронтенд ─────────────────────────

def test_every_page_loads_i18n_first():
    for page in ("index", "settings", "auth", "admin", "composio"):
        html = (ROOT / "templates" / f"{page}.html").read_text(encoding="utf-8")
        assert '<html lang="tg"' in html, page
        head = html.split("</head>")[0]
        assert '<script src="/static/i18n.js"></script>' in head, page


def test_language_picker_in_settings_sidebar_and_auth():
    assert "data-lang-picker" in (ROOT / "templates" / "settings.html").read_text(encoding="utf-8")
    assert 'data-lang-picker="compact"' in (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
    assert "data-lang-picker" in (ROOT / "templates" / "auth.html").read_text(encoding="utf-8")


def test_frontend_default_is_tajik_with_three_languages():
    source = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
    assert "const DEFAULT_LANG = 'tg';" in source
    for code in ("'tg'", "'ru'", "'en'"):
        assert f"code: {code}" in source
    # Ключевые надписи интерфейса переведены
    for key in ("Новый диалог", "Напишите сообщение...", "Настройки AI", "Язык интерфейса", "Войти"):
        assert re.search(r"'" + re.escape(key) + r"': \['[^']+', ", source), key
