"""Админ-маршруты раньше падали с NameError (не хватало импортов) — проверяем, что живы."""
import config


def test_admin_stats_works(admin_client):
    data = admin_client.get("/api/admin/stats").get_json()
    assert "current_provider" in data and isinstance(data["plugins_loaded"], list)
    assert isinstance(data["custom_commands"], list)


def test_admin_settings_change_real_config(admin_client, monkeypatch):
    monkeypatch.setattr(config, "system_prompt", "старый")
    admin_client.post("/api/admin/settings", json={"system_prompt": "новый промпт"})
    assert config.system_prompt == "новый промпт"


def test_admin_groq_keys_status_and_reset(admin_client):
    data = admin_client.get("/api/admin/groq_keys").get_json()
    assert data["cooldown_hours"] > 0
    assert admin_client.post("/api/admin/groq_keys/reset").get_json()["success"] is True


def test_admin_saved_code_roundtrip_is_confined(admin_client, tmp_path, monkeypatch):
    import routes.admin as admin
    monkeypatch.setattr(admin, "SAVED_CODES_DIR", str(tmp_path))
    resp = admin_client.post("/api/admin/save_code", json={"filename": "../../evil.py", "code": "print(1)"})
    assert resp.get_json()["success"] is True
    assert (tmp_path / "evil.py").exists()          # имя урезано до basename, из папки не выйти
    assert admin_client.get("/api/admin/saved_codes").get_json()["files"] == ["evil.py"]


def test_google_callback_without_code_is_400(client):
    assert client.get("/auth/google/callback").status_code == 400


def test_session_login_grants_admin_for_admin_nick_only_on_local_loopback(client, monkeypatch):
    from app import app as flask_app
    monkeypatch.setenv("ADMIN_NICKS", "Bunyod, localroot")

    # 1. Локальный вход с 127.0.0.1 под ником из ADMIN_NICKS -> admin_logged_in
    c_v4 = flask_app.test_client()
    resp = c_v4.post(
        "/api/session/login",
        json={"nick": "bunyod"},
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )
    assert resp.status_code == 200
    assert resp.get_json()["admin"] is True
    assert c_v4.get("/api/session/check").get_json()["admin"] is True
    assert c_v4.get("/api/admin/check").get_json()["logged_in"] is True

    # 2. Локальный вход с ::1 (IPv6 loopback) -> admin_logged_in
    c_v6 = flask_app.test_client()
    resp_v6 = c_v6.post(
        "/api/session/login",
        json={"nick": "localroot"},
        environ_base={"REMOTE_ADDR": "::1"},
    )
    assert resp_v6.status_code == 200
    assert resp_v6.get_json()["admin"] is True

    # 3. Запрос не с localhost (например, из внешней/локальной сети) -> НЕ админ
    c_ext = flask_app.test_client()
    resp_ext = c_ext.post(
        "/api/session/login",
        json={"nick": "Bunyod"},
        environ_base={"REMOTE_ADDR": "192.168.1.44"},
    )
    assert resp_ext.status_code == 200
    assert resp_ext.get_json()["admin"] is False
    assert c_ext.get("/api/admin/check").get_json()["logged_in"] is False

    # 4. Запрос с 127.0.0.1, но через туннель/прокси (X-Forwarded-For / CF-Connecting-IP) -> НЕ админ
    c_tunnel = flask_app.test_client()
    resp_tunnel = c_tunnel.post(
        "/api/session/login",
        json={"nick": "Bunyod"},
        headers={"X-Forwarded-For": "203.0.113.99"},
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )
    assert resp_tunnel.status_code == 200
    assert resp_tunnel.get_json()["admin"] is False

    # 5. Обычный ник с 127.0.0.1 -> НЕ админ
    c_guest = flask_app.test_client()
    resp_guest = c_guest.post(
        "/api/session/login",
        json={"nick": "ОбычныйГость"},
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )
    assert resp_guest.status_code == 200
    assert resp_guest.get_json()["admin"] is False

