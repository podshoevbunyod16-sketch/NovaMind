"""Версионирование промптов: снимки, архив, закрепление, diff, откат."""
import json

import pytest

import prompt_registry


@pytest.fixture()
def registry(tmp_path, monkeypatch):
    """Отдельная папка промптов: тесты не трогают настоящий manifest.json."""
    base = tmp_path / "prompts" / "base"
    base.mkdir(parents=True)
    (base / "planner.txt").write_text("Ты — планировщик. Версия 1.", encoding="utf-8")
    (base / "writer.txt").write_text("Ты — редактор. Версия 1.", encoding="utf-8")
    monkeypatch.setattr(prompt_registry, "PROMPT_DIR", str(tmp_path / "prompts"))
    monkeypatch.setattr(prompt_registry, "BASE_DIR", str(base))
    monkeypatch.setattr(prompt_registry, "VERSIONS_DIR", str(tmp_path / "prompts" / "versions"))
    monkeypatch.setattr(prompt_registry, "MANIFEST_PATH",
                        str(tmp_path / "prompts" / "manifest.json"))
    return base


def test_snapshot_creates_manifest_and_archive(registry):
    info = prompt_registry.snapshot("planner")
    assert info["changed"] is True
    assert len(info["version"]) == 12
    manifest = json.loads(
        (registry.parent / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["prompts"]["planner"]["active"] == info["version"]
    archived = (registry.parent / "versions" / "planner" / f"{info['version']}.txt")
    assert archived.is_file()
    assert "Версия 1" in archived.read_text(encoding="utf-8")


def test_snapshot_is_idempotent(registry):
    first = prompt_registry.snapshot("planner")
    second = prompt_registry.snapshot("planner")
    assert first["version"] == second["version"]
    assert second["changed"] is False
    assert len(prompt_registry.history("planner")) == 1


def test_new_content_creates_new_version(registry):
    first = prompt_registry.snapshot("planner")
    (registry / "planner.txt").write_text("Ты — планировщик. Версия 2.", encoding="utf-8")
    second = prompt_registry.snapshot("planner")
    assert second["version"] != first["version"]
    history = prompt_registry.history("planner")
    assert len(history) == 2
    assert history[0]["active"] is True


def test_pin_and_unpin(registry):
    prompt_registry.snapshot("planner")
    old_version = prompt_registry.record_use("planner")["version"]
    (registry / "planner.txt").write_text("Ты — планировщик. Версия 2.", encoding="utf-8")
    prompt_registry.snapshot("planner")

    pinned = prompt_registry.pin("planner", old_version)
    assert pinned["pinned"] == old_version
    assert prompt_registry.resolve("planner").endswith(f"{old_version}.txt")
    assert "Версия 1" in prompt_registry.load("planner")

    prompt_registry.unpin("planner")
    assert prompt_registry.resolve("planner").endswith("planner.txt")


def test_env_pin_wins(registry, monkeypatch):
    prompt_registry.snapshot("planner")
    version = prompt_registry.record_use("planner")["version"]
    monkeypatch.setenv("PROMPT_VERSION_PLANNER", version)
    resolved = prompt_registry.record_use("planner")
    assert resolved["version"] == version
    assert resolved["pinned"] is True


def test_pin_rejects_unknown_version(registry):
    prompt_registry.snapshot("planner")
    with pytest.raises(FileNotFoundError):
        prompt_registry.pin("planner", "deadbeef1234")
    with pytest.raises(ValueError):
        prompt_registry.pin("planner", "не-хеш")


def test_missing_pin_falls_back_to_current(registry, monkeypatch):
    prompt_registry.snapshot("planner")
    monkeypatch.setenv("PROMPT_VERSION_PLANNER", "000000000000")
    assert prompt_registry.resolve("planner").endswith("planner.txt")


def test_diff_between_versions(registry):
    prompt_registry.snapshot("planner")
    old = prompt_registry.record_use("planner")["version"]
    (registry / "planner.txt").write_text("Ты — планировщик. Версия 2.", encoding="utf-8")
    diff = prompt_registry.diff("planner", old)
    assert "-Ты — планировщик. Версия 1." in diff
    assert "+Ты — планировщик. Версия 2." in diff


def test_list_prompts_reports_state(registry):
    prompt_registry.snapshot_all()
    prompts = {item["name"]: item for item in prompt_registry.list_prompts()}
    assert set(prompts) == {"planner", "writer"}
    assert prompts["planner"]["version"]
    assert prompts["planner"]["versions"] == 1


def test_invalid_name_is_rejected(registry):
    with pytest.raises(ValueError):
        prompt_registry.snapshot("../etc/passwd")
    with pytest.raises(ValueError):
        prompt_registry.resolve("planner; rm -rf /")


def test_record_use_all_returns_versions(registry):
    prompt_registry.snapshot_all()
    versions = prompt_registry.record_use_all()
    assert set(versions) == {"planner", "writer"}
    assert all(len(value) == 12 for value in versions.values())


def test_missing_prompt_raises(registry):
    with pytest.raises(FileNotFoundError):
        prompt_registry.snapshot("ghost")


def test_summary_counts_pinned(registry, monkeypatch):
    prompt_registry.snapshot_all()
    version = prompt_registry.record_use("planner")["version"]
    monkeypatch.setenv("PROMPT_VERSION_PLANNER", version)
    data = prompt_registry.summary()
    assert data["prompts"] == 2
    assert data["pinned"] == 1
    assert data["env_prefix"] == "PROMPT_VERSION_"
