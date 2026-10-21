"""Подтверждение опасных действий (этап 2).

Опасный шаг агент не выполняет сам: в поток уходит событие confirm с точной
командой, а ход ждёт POST /api/agent/confirm. Нет ответа — отказ по таймауту.
"""
import json
import threading
import time

import pytest

import routes.agent as agent_routes


def _ndjson(response):
    return [json.loads(line) for line in response.get_data(as_text=True).splitlines() if line.strip()]


def _drive(generator):
    """Прокручивает генератор до конца: (события, возвращённое наблюдение)."""
    events = []
    while True:
        try:
            events.append(json.loads(next(generator)))
        except StopIteration as stop:
            return events, stop.value


def _resolve_later(decision, delay=0.05):
    """Пользователь нажимает «Разрешить/Отклонить», пока агент ждёт."""
    def worker():
        time.sleep(delay)
        with agent_routes._CONFIRM_LOCK:
            ids = list(agent_routes._PENDING_CONFIRMS)
        if ids:
            agent_routes.resolve_confirmation(ids[-1], decision)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    return thread


@pytest.fixture
def executed(monkeypatch):
    """Подменяет выполнение шага, чтобы видеть, дошло ли до него дело."""
    calls = []

    def fake_perform(action, kind, use_tools, state, goal="", cancel=None):
        calls.append({"kind": kind, "action": dict(action)})
        return "ВЫПОЛНЕНО"
        yield  # noqa: unreachable — генератор, как настоящий _perform

    monkeypatch.setattr(agent_routes, "_perform", fake_perform)
    return calls


# ══════════════ оценка риска ══════════════

def test_action_risk_by_kind_and_command():
    assert agent_routes.action_risk("search", {"query": "x"}) == "safe"
    assert agent_routes.action_risk("read", {"path": "a.txt"}) == "safe"
    assert agent_routes.action_risk("write", {"path": "a.txt"}) == "write"
    assert agent_routes.action_risk("run", {"command": "ls -la"}) == "write"
    assert agent_routes.action_risk("run", {"command": "git push origin main"}) == "danger"
    assert agent_routes.action_risk("run", {"command": "pip install requests"}) == "danger"
    assert agent_routes.action_risk("mcp_call", {"server_id": "gh", "tool": "create_issue"}) == "danger"


def test_confirmation_required_only_for_danger_by_default(monkeypatch):
    monkeypatch.delenv("AGENT_CONFIRM", raising=False)
    assert agent_routes.confirmation_needed("read", {"path": "x"}) is False
    assert agent_routes.confirmation_needed("write", {"path": "x"}) is False
    assert agent_routes.confirmation_needed("run", {"command": "git commit -m x"}) is True


def test_confirmation_mode_write_and_off(monkeypatch):
    monkeypatch.setenv("AGENT_CONFIRM", "write")
    assert agent_routes.confirmation_needed("write", {"path": "x"}) is True
    monkeypatch.setenv("AGENT_CONFIRM", "off")
    assert agent_routes.confirmation_needed("run", {"command": "git push"}) is False


def test_confirmation_can_shown_command_and_arguments():
    record = agent_routes.register_confirmation("run", {"action": "run", "command": "git push origin main"})
    assert record["risk"] == "danger"
    assert record["command"] == "git push origin main"
    assert record["args"]["command"] == "git push origin main"
    agent_routes.await_confirmation(record, timeout=0.01)


# ══════════════ пауза и продолжение ══════════════

def test_dangerous_action_runs_after_approval(monkeypatch, executed):
    monkeypatch.setenv("AGENT_CONFIRM", "danger")
    state = {"notes": [], "sources": [], "tool_steps": 0}
    _resolve_later(True)

    events, observation = _drive(agent_routes._perform_guarded(
        {"action": "run", "command": "git push origin main"}, "run", True, state))

    confirm = next(event for event in events if event["type"] == "confirm")
    assert confirm["tool"] == "run" and confirm["risk"] == "danger"
    assert confirm["command"] == "git push origin main"
    assert next(event for event in events if event["type"] == "confirm_result")["approved"] is True
    assert observation == "ВЫПОЛНЕНО"
    assert [call["kind"] for call in executed] == ["run"]


def test_dangerous_action_skipped_when_denied(monkeypatch, executed):
    monkeypatch.setenv("AGENT_CONFIRM", "danger")
    state = {"notes": [], "sources": [], "tool_steps": 0}
    _resolve_later(False)

    events, observation = _drive(agent_routes._perform_guarded(
        {"action": "run", "command": "rm -rf notes"}, "run", True, state))

    assert next(event for event in events if event["type"] == "confirm_result")["approved"] is False
    assert "ОТКЛОНИЛ" in observation
    assert executed == []                       # ничего не выполнялось
    assert any(event["type"] == "step" and "отклонено" in event["text"] for event in events)


def test_dangerous_action_denied_by_timeout(monkeypatch, executed):
    monkeypatch.setenv("AGENT_CONFIRM", "danger")
    monkeypatch.setenv("AGENT_CONFIRM_TIMEOUT", "0.05")
    state = {"notes": [], "sources": [], "tool_steps": 0}
    started = time.time()

    events, observation = _drive(agent_routes._perform_guarded(
        {"action": "run", "command": "npm install left-pad"}, "run", True, state))

    assert time.time() - started < 5            # не висим 120 секунд
    assert next(event for event in events if event["type"] == "confirm_result")["approved"] is False
    assert "ОТКЛОНИЛ" in observation and executed == []


def test_approved_action_is_not_asked_twice_in_one_turn(monkeypatch, executed):
    monkeypatch.setenv("AGENT_CONFIRM", "danger")
    state = {"notes": [], "sources": [], "tool_steps": 0, "approved": set()}
    _resolve_later(True)
    _drive(agent_routes._perform_guarded(
        {"action": "run", "command": "git push"}, "run", True, state))

    events, observation = _drive(agent_routes._perform_guarded(
        {"action": "run", "command": "git push"}, "run", True, state))

    assert not [event for event in events if event["type"] == "confirm"]
    assert observation == "ВЫПОЛНЕНО"


def test_safe_actions_are_never_confirmed(monkeypatch, executed):
    monkeypatch.setenv("AGENT_CONFIRM", "write")
    state = {"notes": [], "sources": [], "tool_steps": 0}
    events, observation = _drive(agent_routes._perform_guarded(
        {"action": "search", "query": "asyncio"}, "search", True, state))
    assert not [event for event in events if event["type"] == "confirm"]
    assert observation == "ВЫПОЛНЕНО"


# ══════════════ маршрут /api/agent/confirm ══════════════

def test_confirm_route_validates_input(admin_client):
    assert admin_client.post("/api/agent/confirm", json={}).status_code == 400
    assert admin_client.post("/api/agent/confirm",
                             json={"id": "abc", "approve": "yes"}).status_code == 400


def test_confirm_route_unknown_id(admin_client):
    response = admin_client.post("/api/agent/confirm", json={"id": "нет-такого", "approve": True})
    assert response.status_code == 409


def test_confirm_route_approves_and_rejects_repeat(admin_client):
    record = agent_routes.register_confirmation("run", {"action": "run", "command": "git push"})

    first = admin_client.post("/api/agent/confirm", json={"id": record["id"], "approve": True})
    assert first.status_code == 200 and first.get_json()["approved"] is True

    # повторное подтверждение того же id не должно выполнить действие второй раз
    second = admin_client.post("/api/agent/confirm", json={"id": record["id"], "approve": True})
    assert second.status_code == 409
    assert "обработано" in second.get_json()["error"]
