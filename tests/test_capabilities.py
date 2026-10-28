"""Криптографические права инструментов: подпись, срок, least-privilege."""
import time

import pytest

import capabilities


@pytest.fixture(autouse=True)
def clean_context():
    capabilities.deactivate()
    yield
    capabilities.deactivate()


def test_minted_token_verifies_and_carries_claims():
    token, cap = capabilities.mint("anon:abc", "run-1", role="guest",
                                   tools=["web_search", "web_open"])
    assert token.startswith("nm1.")
    verified = capabilities.verify(token, tool="web_search", run_id="run-1")
    assert verified.role == "guest"
    assert verified.tools == frozenset({"web_search", "web_open"})
    assert verified.jti == cap.jti
    assert verified.expires_at > time.time()


def test_token_is_not_plain_json():
    token, _ = capabilities.mint("anon:abc", "run-1", role="guest", tools=["web_search"])
    assert "web_search" not in token.split(".")[1]      # payload закодирован


def test_tampered_token_is_rejected():
    token, _ = capabilities.mint("anon:abc", "run-1", role="guest", tools=["web_search"])
    header, body, signature = token.split(".")
    forged = f"{header}.{body[:-2]}AA.{signature}"
    with pytest.raises(capabilities.CapabilityError) as error:
        capabilities.verify(forged)
    assert error.value.reason in ("bad_signature", "malformed")


def test_garbage_token_is_rejected():
    with pytest.raises(capabilities.CapabilityError):
        capabilities.verify("not-a-token")


def test_expired_token_is_rejected():
    token, _ = capabilities.mint("anon:abc", "run-1", role="guest",
                                 tools=["web_search"], ttl=30)
    with pytest.raises(capabilities.CapabilityError) as error:
        capabilities.verify(token, now=time.time() + 120)
    assert error.value.reason == "expired"


def test_token_bound_to_run_and_owner():
    token, _ = capabilities.mint("anon:abc", "run-1", role="guest", tools=["web_search"])
    with pytest.raises(capabilities.CapabilityError) as error:
        capabilities.verify(token, run_id="run-2")
    assert error.value.reason == "wrong_run"
    with pytest.raises(capabilities.CapabilityError) as error:
        capabilities.verify(token, owner="anon:other")
    assert error.value.reason == "wrong_owner"


def test_wrong_secret_is_rejected():
    token, _ = capabilities.mint("anon:abc", "run-1", role="guest",
                                 tools=["web_search"], secret=b"one")
    with pytest.raises(capabilities.CapabilityError) as error:
        capabilities.verify(token, secret=b"two")
    assert error.value.reason == "bad_signature"


def test_least_privilege_drops_unallowed_tools():
    _token, cap = capabilities.mint("anon:abc", "run-1", role="guest",
                                    tools=["web_search", "terminal", "file_write", "mcp_call"])
    assert cap.tools == frozenset({"web_search"})
    assert set(cap.dropped) == {"terminal", "file_write", "mcp_call"}


def test_user_role_has_workspace_but_not_mcp():
    _token, cap = capabilities.mint("email:a@b.c", "run-1", role="user",
                                    tools=["terminal", "file_read", "mcp_call", "schedule"])
    assert {"terminal", "file_read"} <= cap.tools
    assert "mcp_call" not in cap.tools
    assert "schedule" not in cap.tools


def test_admin_role_has_mcp_and_schedule():
    _token, cap = capabilities.mint("admin:root", "run-1", role="admin",
                                    tools=["terminal", "mcp_call", "schedule"])
    assert {"terminal", "mcp_call", "schedule"} <= cap.tools


def test_tool_not_granted_raises():
    token, _ = capabilities.mint("anon:abc", "run-1", role="guest", tools=["web_search"])
    with pytest.raises(capabilities.CapabilityError) as error:
        capabilities.verify(token, tool="terminal")
    assert error.value.reason == "tool_not_granted"


def test_aliases_are_normalized():
    token, cap = capabilities.mint("email:a@b.c", "run-1", role="user", tools=["shell"])
    assert capabilities.verify(token, tool="terminal").allows("shell")
    assert cap.allows("shell") and cap.allows("terminal")


def test_require_is_noop_without_active_capability():
    assert capabilities.require("terminal") is None


def test_require_denies_tool_outside_scope():
    _token, cap = capabilities.mint("anon:abc", "run-1", role="guest", tools=["web_search"])
    capabilities.activate(cap)
    assert capabilities.require("web_search") is None
    denied = capabilities.require("terminal")
    assert denied["ok"] is False
    assert denied["error"]["type"] == "tool_not_granted"


def test_require_denies_expired_capability(monkeypatch):
    _token, cap = capabilities.mint("anon:abc", "run-1", role="guest",
                                    tools=["web_search"], ttl=30)
    capabilities.activate(cap)
    monkeypatch.setattr(time, "time", lambda: cap.expires_at + 5)
    denied = capabilities.require("web_search")
    assert denied["error"]["type"] == "expired"


def test_enforcement_can_be_disabled(monkeypatch):
    _token, cap = capabilities.mint("anon:abc", "run-1", role="guest", tools=["web_search"])
    capabilities.activate(cap)
    monkeypatch.setenv("AGENT_CAPABILITIES", "0")
    assert capabilities.require("terminal") is None


def test_revoke_disables_token():
    token, cap = capabilities.mint("anon:abc", "run-1", role="guest", tools=["web_search"])
    assert capabilities.revoke(token) is True
    with pytest.raises(capabilities.CapabilityError) as error:
        capabilities.verify(token)
    assert error.value.reason == "revoked"


def test_event_does_not_leak_token():
    token, cap = capabilities.mint("anon:abc", "run-1", role="admin", tools=["web_search"])
    event = cap.event()
    assert token not in str(event)
    assert event["role"] == "admin"
    assert event["tools"] == ["web_search"]


def test_summary_hides_secret():
    data = capabilities.summary()
    assert data["enforcement"] is True
    assert "guest" in data["roles"]
    assert "secret" not in str(data).lower().replace("secret_source", "")
