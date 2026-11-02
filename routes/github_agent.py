"""GitHub copy-on-workspace endpoints. Publication always requires confirmation."""
from __future__ import annotations

import os
import shutil
import subprocess

from flask import Blueprint, jsonify, request

from agent import github as git_agent
from routes.workspace import _root

github_agent_bp = Blueprint("agent_github", __name__)


@github_agent_bp.post("/api/agent/github/clone")
def clone_github_repository():
    root, error = _root()
    if error:
        return error
    payload = request.get_json(silent=True) or {}
    try:
        result = git_agent.clone_repository(payload.get("repository"), root,
                                            payload.get("run_id"), allow_local=False)
        return jsonify({"ok": True, **result})
    except git_agent.GitWorkspaceError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@github_agent_bp.get("/api/agent/github/diff")
def github_diff():
    root, error = _root()
    if error:
        return error
    try:
        return jsonify({"ok": True, **git_agent.diff(root)})
    except git_agent.GitWorkspaceError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404


@github_agent_bp.post("/api/agent/github/commit")
def github_commit():
    root, error = _root()
    if error:
        return error
    payload = request.get_json(silent=True) or {}
    try:
        return jsonify({"ok": True, **git_agent.commit(root, payload.get("message", "Изменения NovaMind"))})
    except git_agent.GitWorkspaceError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@github_agent_bp.post("/api/agent/github/publish")
def github_publish():
    root, error = _root()
    if error:
        return error
    payload = request.get_json(silent=True) or {}
    try:
        pushed = git_agent.publish(root, confirmed=payload.get("confirmed") is True,
                                   base=str(payload.get("base") or "main"))
    except git_agent.GitWorkspaceError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 403
    # PR creation is delegated to the existing authenticated GitHub CLI
    # installation; no token is accepted from the browser or passed in argv.
    if shutil.which("gh"):
        repo = git_agent.repository_path(root)
        title = str(payload.get("title") or "Изменения NovaMind")[:200]
        body = str(payload.get("body") or "Изменения подготовлены агентом NovaMind.")[:4000]
        child_env = {k: v for k, v in os.environ.items()
                     if k in {"PATH", "HOME", "USER", "TMPDIR", "LANG", "LC_ALL"}}
        result = subprocess.run(["gh", "pr", "create", "--base", pushed["base"],
                                 "--head", pushed["branch"], "--title", title,
                                 "--body", body], cwd=str(repo), env=child_env,
                                capture_output=True, text=True, timeout=90)
        pushed["pull_request"] = result.stdout.strip() if result.returncode == 0 else ""
        if result.returncode:
            pushed["pull_request_error"] = (result.stderr or "gh pr create failed")[-1000:]
    else:
        pushed["pull_request_error"] = "GitHub CLI gh недоступен; ветка отправлена, PR не создан"
    return jsonify({"ok": True, **pushed})
