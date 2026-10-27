"""Stable per-browser/account identity for data isolation."""
import uuid
from flask import has_request_context, session


def browser_id(sess=None):
    current = session if sess is None else sess
    sid = current.get("nova_sid")
    if not sid:
        sid = uuid.uuid4().hex
        current["nova_sid"] = sid
    try:
        current.permanent = True
    except Exception:
        pass
    return sid


def chat_owner(sess=None):
    if sess is None:
        if not has_request_context():
            return ""
        sess = session
    email = str(sess.get("nova_user_email") or "").strip().lower()
    if email and sess.get("nova_google_login"):
        return ("email:" + email)[:200]
    if sess.get("admin_logged_in"):
        name = str(sess.get("admin_username") or "admin").strip().lower()
        return ("admin:" + name)[:200]
    return "browser:" + browser_id(sess)
