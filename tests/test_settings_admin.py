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


def test_env_can_grant_admin_features_to_every_signed_in_user(client, monkeypatch):
    monkeypatch.setenv("ADMIN_ACCESS_FOR_ALL_USERS", "1")
    with client.session_transaction() as sess:
        sess["nova_signed_in"] = True
        sess["nova_user_nick"] = "ordinary-user"

    session_info = client.get("/api/session/check").get_json()
    assert session_info["signed_in"] is True
    assert session_info["admin"] is True
    assert client.get("/api/settings/mcp").status_code == 200
    assert client.get("/settings").status_code == 200


def test_env_admin_features_still_reject_anonymous_guests(client, monkeypatch):
    monkeypatch.setenv("ADMIN_ACCESS_FOR_ALL_USERS", "1")
    assert client.get("/api/session/check").get_json()["admin"] is False
    assert client.get("/api/settings/mcp").status_code == 403


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


def test_chat_attachment_menu_contains_mcp_manager_and_catalog(admin_client):
    page = admin_client.get("/chat").get_data(as_text=True)
    assert 'id="attachMcpPanel"' in page
    assert 'id="attachMcpConnected"' in page
    assert 'id="attachMcpCatalog"' in page
    assert 'id="mcpActiveBar"' in page

    payload = admin_client.get("/api/settings/mcp").get_json()
    catalog_ids = {item["id"] for item in payload["catalog"]}
    assert {"custom", "context7", "github", "notion", "sentry"} <= catalog_ids


def test_attachment_menu_opens_upward_on_mobile():
    import pathlib

    css = (pathlib.Path(__file__).resolve().parent.parent / "static" / "style.css").read_text(encoding="utf-8")
    block = css.split(".attach-dropdown {", 1)[1].split("}", 1)[0]
    assert "bottom:" in block
    assert "top: auto" in block
    assert ".attach-dropdown .toolbar-btn span { display: inline; }" in css


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
