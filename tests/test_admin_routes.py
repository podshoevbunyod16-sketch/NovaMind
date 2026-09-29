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
