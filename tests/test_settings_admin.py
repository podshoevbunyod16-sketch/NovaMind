"""Панель настроек — только администратору.

Проверяем и страницу, и API: обычный пользователь не должен ни открыть
панель, ни прочитать каталог моделей, MCP-серверы или навыки. Переключатель
языка интерфейса остаётся доступным — это настройка пользователя.
"""

import re

import pytest


def test_guest_cannot_open_settings_page(client):
    response = client.get("/settings")
    assert response.status_code == 403
    body = response.get_data(as_text=True)
    assert "только администратору" in body
    # Страница закрыта, но из неё видно, куда идти дальше.
    assert "/chat" in body and "/admin/login" in body


def test_guest_gets_403_from_settings_api(client):
    for path in ("/api/settings/providers", "/api/settings/models?provider=groq",
                 "/api/settings/mcp", "/api/settings/skills",
                 "/api/settings/media-models"):
        response = client.get(path)
        assert response.status_code == 403, path
        data = response.get_json() or {}
        assert data.get("admin_only") is True, path


def test_guest_cannot_change_the_model(client):
    response = client.post("/api/settings/select", json={"provider": "groq", "model": "x"})
    assert response.status_code == 403


def test_guest_keeps_the_ui_language_switch(client):
    """Язык интерфейса — настройка пользователя, а не сервера."""
    assert client.get("/api/settings/lang").status_code == 200
    response = client.post("/api/settings/lang", json={"language": "ru"})
    assert response.status_code == 200 and response.get_json()["language"] == "ru"


def test_guest_still_opens_the_chat(client):
    for path in ("/", "/chat"):
        assert client.get(path).status_code == 200, path


def test_admin_opens_settings_page(admin_client):
    response = admin_client.get("/settings")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Настройки AI" in body and "liquid-glass.css" in body


def test_admin_reads_settings_api(admin_client):
    assert admin_client.get("/api/settings/providers").status_code == 200
    assert admin_client.get("/api/settings/mcp").status_code == 200
    assert admin_client.get("/api/settings/skills").status_code == 200


def test_settings_links_are_hidden_for_non_admins():
    """Кнопка в шапке скрыта, пока сервер не подтвердил права администратора."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    html = (root / "templates" / "index.html").read_text(encoding="utf-8")
    # Кнопка есть в разметке, но изначально скрыта: показываем после проверки прав.
    assert re.search(r'id="btnSettings"[^>]*\bhidden\b', html), html.count("btnSettings")

    app_js = (root / "static" / "app.js").read_text(encoding="utf-8")
    assert "function applySettingsAccess" in app_js
    assert "Настройки доступны только администратору" in app_js
    # Права берём у сервера, а не только из localStorage.
    assert "/api/session/check" in app_js
