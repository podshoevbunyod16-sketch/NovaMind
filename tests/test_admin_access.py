"""Вход администратора: логин из ADMIN_USER, регистр, e-mail, .env и сессии.

Это ответ на «я администратор, но панель и MCP мне недоступны». Проверяем
каждый способ, которым человек попадает на сервер:

* вход по коду на странице входа (логин — ADMIN_USER, это может быть e-mail);
* вход через Google, когда почта совпадает с ADMIN_USER/ADMIN_EMAIL;
* вход по нику, когда ник совпал с логином администратора;
* разбор .env (кавычки, `export`, комментарии) — иначе код «не подходил»;
* ключ подписи cookie: сессия должна переживать перезапуск сервера.
"""

import importlib
import os

import pytest


# ══════════════ .env ══════════════

def test_env_values_drop_quotes_exports_and_comments():
    import config

    assert config.parse_env_line('ADMIN_USER="podshoevbunyod16@gmail.com"') == (
        "ADMIN_USER", "podshoevbunyod16@gmail.com")
    assert config.parse_env_line("export ADMIN_CODE = '08080909b'  # мой код") == (
        "ADMIN_CODE", "08080909b")
    assert config.parse_env_line("ADMIN_CODE=08080909b  # код") == ("ADMIN_CODE", "08080909b")
    assert config.parse_env_line("# ADMIN_CODE=закомментировано") is None
    assert config.parse_env_line("мусор без равно") is None
    assert config.parse_env_line("=пустой-ключ") is None


def test_env_file_is_loaded_without_breaking_existing_vars(tmp_path, monkeypatch):
    import config

    path = tmp_path / ".env"
    path.write_text(
        '# комментарий\n'
        'ADMIN_USER="podshoevbunyod16@gmail.com"\n'
        'export ADMIN_CODE = "08080909b"   # код\n'
        'ALREADY_SET=из-файла\n',
        encoding="utf-8")
    monkeypatch.setenv("ALREADY_SET", "из-окружения")
    monkeypatch.delenv("ADMIN_USER", raising=False)
    monkeypatch.delenv("ADMIN_CODE", raising=False)

    config.load_env_file(str(path))

    assert os.environ["ADMIN_USER"] == "podshoevbunyod16@gmail.com"
    assert os.environ["ADMIN_CODE"] == "08080909b"
    # Уже заданная переменная окружения главнее файла.
    assert os.environ["ALREADY_SET"] == "из-окружения"


# ══════════════ ключ сессии ══════════════

def test_session_secret_survives_a_restart(tmp_path):
    import config

    path = str(tmp_path / ".session_secret")
    first = config.load_session_secret(path)
    second = config.load_session_secret(path)
    assert first and first == second
    assert os.path.isfile(path)
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"


def test_explicit_session_secret_wins(tmp_path):
    import config

    path = str(tmp_path / ".session_secret")
    assert config.load_session_secret(path, "мой-ключ") == "мой-ключ"


# ══════════════ вход по коду ══════════════

@pytest.fixture()
def owner(monkeypatch):
    """Настройки как у владельца сервера: e-mail в ADMIN_USER."""
    import config
    import routes.admin as admin_routes
    import routes.terminal as terminal_routes
    import routes.coding_agent as coding_routes

    login = "podshoevbunyod16@gmail.com"
    monkeypatch.setattr(config, "ADMIN_USER", login)
    monkeypatch.setattr(config, "ADMIN_EMAIL", "")
    monkeypatch.setattr(config, "ADMIN_LOGINS", frozenset({login.casefold()}))
    monkeypatch.setattr(admin_routes, "ADMIN_USER", login)
    monkeypatch.setattr(admin_routes, "ADMIN_EMAIL", "")
    monkeypatch.setattr(admin_routes, "ADMIN_CREDENTIALS", {login: "08080909b"})
    # Сессия из прошлого теста не должна влиять.
    monkeypatch.undo
    return login


def _login(client, username, code):
    return client.post("/api/admin/login", json={"username": username, "code": code})


def test_owner_logs_in_with_email_and_gets_settings(client, owner, sandbox):
    response = _login(client, owner, "08080909b")
    assert response.status_code == 200
    assert response.get_json()["success"] is True
    assert client.get("/settings").status_code == 200
    assert client.get("/api/settings/mcp").status_code == 200
    assert client.get("/api/admin/check").get_json()["logged_in"] is True


def test_login_is_case_insensitive_and_trims(client, owner, sandbox):
    assert _login(client, "  PodshoevBunyod16@Gmail.com ", "08080909b").get_json()["success"]
    assert client.get("/api/settings/mcp").status_code == 200


def test_wrong_code_names_the_reason(client, owner, sandbox):
    data = _login(client, owner, "неверный").get_json()
    assert data["success"] is False
    assert "код" in data["error"].lower()


def test_unknown_login_explains_which_login_to_use(client, owner, sandbox):
    """Частая ошибка: в ADMIN_USER почта, а в форму пишут «admin»."""
    data = _login(client, "admin", "08080909b").get_json()
    assert data["success"] is False
    assert owner in data["error"] and data["admin_login_is_email"] is True
    assert client.get("/settings").status_code == 403


# ══════════════ вход через Google / по нику ══════════════

def test_google_email_of_the_owner_is_admin(client, owner, sandbox):
    with client.session_transaction() as session:
        session["nova_user_nick"] = "Bunyod"
        session["nova_user_email"] = owner
        session["nova_google_login"] = True
    assert client.get("/api/session/check").get_json()["admin"] is True
    assert client.get("/settings").status_code == 200
    assert client.get("/api/settings/mcp").status_code == 200
    assert client.get("/api/agent/status").get_json()["admin"] is True


def test_google_email_of_a_guest_stays_a_guest(client, owner, sandbox):
    with client.session_transaction() as session:
        session["nova_user_nick"] = "Гость"
        session["nova_user_email"] = "guest@example.com"
        session["nova_google_login"] = True
    assert client.get("/api/session/check").get_json()["admin"] is False
    assert client.get("/settings").status_code == 403


def test_nick_login_matching_admin_login_grants_admin(client, owner, sandbox):
    data = client.post("/api/session/login", json={"nick": owner}).get_json()
    assert data["success"] is True and data["admin"] is True
    assert client.get("/settings").status_code == 200


def test_plain_nick_login_does_not_grant_admin(client, owner, sandbox):
    data = client.post("/api/session/login", json={"nick": "Вася"}).get_json()
    assert data["success"] is True and data["admin"] is False
    assert client.get("/settings").status_code == 403


def test_admin_email_env_variable_also_works(client, owner, sandbox, monkeypatch):
    """ADMIN_USER — ник, ADMIN_EMAIL — почта: подходят оба."""
    import config
    import routes.admin as admin_routes

    monkeypatch.setattr(config, "ADMIN_USER", "bunyod")
    monkeypatch.setattr(config, "ADMIN_EMAIL", owner)
    monkeypatch.setattr(config, "ADMIN_LOGINS",
                        frozenset({"bunyod", owner.casefold()}))
    monkeypatch.setattr(admin_routes, "ADMIN_CREDENTIALS", {"bunyod": "08080909b"})

    with client.session_transaction() as session:
        session["nova_user_email"] = owner
    assert client.get("/settings").status_code == 200

    fresh = client.application.test_client()
    assert _login(fresh, "bunyod", "08080909b").get_json()["success"] is True
    assert fresh.get("/api/settings/mcp").status_code == 200


# ══════════════ страницы входа ══════════════

def test_login_page_shows_the_admin_login(client, owner):
    body = client.get("/admin/login").get_data(as_text=True)
    assert owner in body
    assert "ADMIN_CODE" in body


def test_denied_page_tells_which_login_to_use(client, owner, sandbox):
    with client.session_transaction() as session:
        session["nova_user_nick"] = "Гость"
    response = client.get("/settings")
    assert response.status_code == 403
    body = response.get_data(as_text=True)
    assert owner in body                       # кто администратор
    assert "Гость" in body                     # кем вошли сейчас
    assert 'id="adminLogin"' in body           # можно войти прямо здесь
    assert "/api/admin/login" in body


def test_denied_api_tells_which_login_to_use(client, owner, sandbox):
    data = client.get("/api/settings/mcp").get_json()
    assert data["admin_only"] is True
    assert data["admin_login"] == owner
    assert data["admin_login_is_email"] is True


# ══════════════ разбор «почему недоступно» ══════════════

def test_access_report_for_admin(client, owner, sandbox):
    _login(client, owner, "08080909b")
    data = client.get("/api/admin/access").get_json()
    assert data["admin"] is True
    assert data["admin_login"] == owner
    assert "enabled" in data["sandbox"] and "enabled" in data["agent"]
    assert "servers_connected" in data["mcp"]
    assert data["settings_panel"] == "доступна"


def test_access_report_is_closed_for_guests(client, owner, sandbox):
    response = client.get("/api/admin/access")
    assert response.status_code == 403
    assert response.get_json()["admin"] is False
    # Гостю не показываем, чей это сервер и какой у него логин.
    assert response.get_json()["admin_login"] == ""


# ══════════════ побочное: XML-вызовы инструментов не теряются ══════════════

def test_xml_tool_call_is_still_a_block(monkeypatch):
    """<web_search>…</web_search> от модели = обычный блок, а не мусор в ответе."""
    from agenticseek.tools.tools import Tools

    tool = Tools()
    tool.tag = "web_search"
    tool.risk = "safe"
    blocks = tool.load_blocks("<web_search>asyncio</web_search>")
    assert blocks and "asyncio" in blocks[0][0]
