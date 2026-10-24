"""Подтверждение опасных действий.

Опасный шаг агент не выполняет сам: в поток уходит событие ``confirm`` с точной
командой, а ход ждёт ``POST /api/agent/confirm``. Нет ответа — отказ (fail
closed). Уровни риска и правила «разрешить всегда» живут в
:mod:`agenticseek.policy`.
"""
import threading
import time

import pytest

import agenticseek.policy as policy
import routes.agent as agent_routes
from tests.agent_helpers import events_of, model_text, ndjson, script


@pytest.fixture(autouse=True)
def _fresh_policy(monkeypatch):
    """Правила и режим подтверждений — глобальные: тесты не влияют друг на друга."""
    monkeypatch.delenv("AGENT_CONFIRM", raising=False)
    monkeypatch.setenv("AGENT_CONFIRM_TIMEOUT", "0.3")
    policy.reset_cache()
    policy.forget_always_allowed()
    try:
        yield
    finally:
        policy.forget_always_allowed()
        policy.reset_confirmations()
        policy.reset_cache()


def _mode(monkeypatch, value):
    monkeypatch.setenv("AGENT_CONFIRM", value)
    policy.reset_cache()


# ══════════════ оценка риска ══════════════

def test_risk_by_tool_and_command():
    assert policy.risk_of("web_search", "asyncio") == "safe"
    assert policy.risk_of("web_open", "https://example.org") == "safe"
    assert policy.risk_of("file_finder", "name=notes.md") == "safe"
    assert policy.risk_of("file_edit", '{"path": "a.py", "old": "a", "new": "b"}') == "write"
    assert policy.risk_of("bash", "ls -la") == "write"
    assert policy.risk_of("bash", "git push origin main") == "danger"
    assert policy.risk_of("bash", "pip install requests") == "danger"
    assert policy.risk_of("bash", "rm -rf build") == "danger"
    assert policy.risk_of("mcp_call", '{"tool": "create_issue"}') == "danger"
    assert policy.risk_of("mcp_call", '{"tool": "list_issues"}') == "safe"


def test_only_danger_asks_by_default(monkeypatch):
    _mode(monkeypatch, "danger")
    assert policy.needs_confirmation("web_search", "x") is False
    assert policy.needs_confirmation("bash", "ls") is False
    assert policy.needs_confirmation("bash", "git commit -m x") is False
    assert policy.needs_confirmation("bash", "git push origin main") is True


def test_modes_write_off(monkeypatch):
    _mode(monkeypatch, "write")
    assert policy.needs_confirmation("bash", "ls") is True
    assert policy.needs_confirmation("file_edit", "x") is True
    assert policy.needs_confirmation("web_search", "x") is False
    _mode(monkeypatch, "off")
    assert policy.needs_confirmation("bash", "git push") is False


def test_confirmation_record_describes_the_action():
    record = policy.register_confirmation("bash", "git push origin main")
    assert record["risk"] == "danger" and record["tool"] == "bash"
    assert "git push origin main" in record["description"]
    assert policy.await_confirmation(record, timeout=0.01) is False


def test_secrets_are_redacted_in_the_dialog():
    text = policy.describe_action("bash", "curl -H 'Authorization: Bearer abcdef123456' x")
    assert "abcdef123456" not in text and "***" in text


# ══════════════ ожидание ответа ══════════════

def test_approval_unblocks_the_wait():
    record = policy.register_confirmation("bash", "npm install left-pad")

    def worker():
        time.sleep(0.05)
        policy.resolve_confirmation(record["id"], True)

    threading.Thread(target=worker, daemon=True).start()
    assert policy.await_confirmation(record, timeout=5) is True


def test_no_answer_means_refusal(monkeypatch):
    monkeypatch.setenv("AGENT_CONFIRM_TIMEOUT", "0.05")
    record = policy.register_confirmation("bash", "npm install left-pad")
    started = time.time()
    assert policy.await_confirmation(record) is False
    assert time.time() - started < 5


def test_same_confirmation_cannot_be_answered_twice():
    record = policy.register_confirmation("bash", "git push")
    assert policy.resolve_confirmation(record["id"], True) == (True, "ok")
    assert policy.resolve_confirmation(record["id"], True) == (False, "already_resolved")
    assert policy.resolve_confirmation("нет-такого", True) == (False, "not_found")


# ══════════════ маршрут /api/agent/confirm ══════════════

def test_confirm_route_validates_input(admin_client):
    assert admin_client.post("/api/agent/confirm", json={}).status_code == 400
    assert admin_client.post("/api/agent/confirm",
                             json={"id": "abc", "approve": "yes"}).status_code == 400


def test_confirm_route_unknown_id(admin_client):
    response = admin_client.post("/api/agent/confirm", json={"id": "нет-такого", "approve": True})
    assert response.status_code == 409


def test_confirm_route_approves_and_rejects_repeat(admin_client):
    record = policy.register_confirmation("bash", "git push")

    first = admin_client.post("/api/agent/confirm", json={"id": record["id"], "approve": True})
    assert first.status_code == 200 and first.get_json()["approved"] is True

    second = admin_client.post("/api/agent/confirm", json={"id": record["id"], "approve": True})
    assert second.status_code == 409
    assert "обработано" in second.get_json()["error"]


def test_confirm_route_can_store_always(admin_client):
    record = policy.register_confirmation("bash", "npm install left-pad")
    response = admin_client.post("/api/agent/confirm",
                                 json={"id": record["id"], "approve": True, "always": True})
    assert response.status_code == 200 and response.get_json()["always"] is True
    assert policy.needs_confirmation("bash", "npm install left-pad") is False
    assert policy.needs_confirmation("bash", "npm install right-pad") is True


# ══════════════ «разрешить всегда» ══════════════

def test_always_rule_is_narrow(monkeypatch):
    _mode(monkeypatch, "danger")
    record = policy.register_confirmation("bash", "git push origin main")
    policy.resolve_confirmation(record["id"], True, always=True)

    assert policy.needs_confirmation("bash", "git push origin dev") is False
    assert policy.needs_confirmation("bash", "rm -rf build") is True


def test_always_rule_is_not_saved_on_deny(monkeypatch):
    _mode(monkeypatch, "danger")
    record = policy.register_confirmation("bash", "git push origin main")
    policy.resolve_confirmation(record["id"], False, always=True)
    assert policy.needs_confirmation("bash", "git push origin main") is True


def test_always_rule_can_be_revoked(monkeypatch):
    _mode(monkeypatch, "danger")
    record = policy.register_confirmation("bash", "pip install requests")
    policy.resolve_confirmation(record["id"], True, always=True)
    assert policy.always_allowed("bash", "pip install requests") is True

    policy.forget_always_allowed()
    assert policy.always_allowed("bash", "pip install requests") is False


def test_permissions_endpoint_shows_rules(admin_client):
    policy.mark_always_allowed("bash", "git push origin main")
    data = admin_client.get("/api/agent/permissions").get_json()
    assert data["mode"] in ("off", "write", "danger")
    assert {"tool": "bash", "match": "git push origin"} in data["rules"]

    admin_client.delete("/api/agent/permissions")
    assert admin_client.get("/api/agent/permissions").get_json()["rules"] == []


# ══════════════ в потоке агента ══════════════

def test_dangerous_command_asks_and_runs_after_approval(admin_client, sandbox, monkeypatch):
    _mode(monkeypatch, "danger")
    monkeypatch.setattr(policy, "await_confirmation", lambda record, timeout=None: True)
    script(monkeypatch, ["```bash\ngit push origin main\n```", "Отправил"])

    events = ndjson(admin_client.post("/api/agent/stream",
                                      json={"message": "запусти git push origin main"}))
    confirm = events_of(events, "confirm")[0]
    assert confirm["tool"] == "bash" and confirm["risk"] == "danger"
    assert "git push origin main" in confirm["command"]
    assert events_of(events, "confirm_result")[0]["approved"] is True
    assert events_of(events, "tool")[0]["code"] in (0, 1)      # команда дошла до песочницы


def test_denied_command_is_not_executed(admin_client, sandbox, monkeypatch):
    _mode(monkeypatch, "danger")
    monkeypatch.setattr(policy, "await_confirmation", lambda record, timeout=None: False)
    seen = []
    script(monkeypatch, ["```bash\ngit push origin main\n```", "Не отправлял"], seen)

    events = ndjson(admin_client.post("/api/agent/stream",
                                      json={"message": "запусти git push origin main"}))
    assert events_of(events, "confirm_result")[0]["approved"] is False
    assert events_of(events, "tool") == []
    assert "запретил" in model_text(seen, -1).lower()
    # Отказ объяснён: иначе «🚫 Запрещено» выглядело как поломка.
    steps = [event.get("text", "") for event in events_of(events, "step")]
    assert any("разрешение не получено" in step for step in steps)


def test_safe_actions_are_never_confirmed(admin_client, sandbox, monkeypatch):
    _mode(monkeypatch, "write")
    results = [{"title": "asyncio", "url": "https://example.org/a", "snippet": "…",
                "host": "example.org"}]
    monkeypatch.setattr(agent_routes, "nova_search",
                        lambda query, limit=6: (results, {"backend": "test"}))
    script(monkeypatch, ["```web_search\nasyncio\n```", "Готово"])

    events = ndjson(admin_client.post("/api/agent/stream", json={"message": "найди asyncio"}))
    assert events_of(events, "confirm") == []
    assert events_of(events, "tool")[0]["name"] == "web_search"


def test_confirm_off_skips_the_question(admin_client, sandbox, monkeypatch):
    _mode(monkeypatch, "off")
    script(monkeypatch, ["```bash\ngit push origin main\n```", "Отправил"])
    events = ndjson(admin_client.post("/api/agent/stream",
                                      json={"message": "запусти git push origin main"}))
    assert events_of(events, "confirm") == []
    assert events_of(events, "tool")[0]["name"] == "bash"


# ══════════════ блок bash из одних комментариев ══════════════

def test_comment_only_bash_block_does_not_ask(monkeypatch):
    """Пользователь не должен видеть «Разрешить», после которого ничего не будет."""
    _mode(monkeypatch, "write")
    assert policy.needs_confirmation("bash", "# в корне проекта") is False
    assert policy.needs_confirmation("bash", "\n  \n") is False
    assert policy.needs_confirmation("bash", "# комментарий\nls -la") is True


def test_confirmation_card_shows_the_command_not_the_comment(monkeypatch):
    _mode(monkeypatch, "danger")
    record = policy.register_confirmation("bash", "# создам окружение\ngit push origin main")
    assert record["command"] == "git push origin main"
    assert "git push origin main" in record["description"]
    assert "# создам окружение" not in record["command"]


def test_always_rule_ignores_leading_comment(monkeypatch):
    _mode(monkeypatch, "danger")
    policy.mark_always_allowed("bash", "# шаг\ngit push origin main")
    assert policy.needs_confirmation("bash", "git push origin main") is False
    assert policy.needs_confirmation("bash", "rm -rf build") is True
