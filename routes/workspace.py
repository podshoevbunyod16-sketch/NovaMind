"""Workspace API; paths are always scoped to the authenticated chat owner."""
from __future__ import annotations

import io
import os
import zipfile
from pathlib import Path

from flask import Blueprint, jsonify, request, send_file, session

import database
import identity
from agent import workspace as workspaces

workspace_bp = Blueprint("agent_workspace", __name__)


def _root():
    chat_id = str(request.args.get("chat_id") or session.get("chat_id") or "").strip()
    if not chat_id or not database.chat_allowed(chat_id):
        return None, (jsonify({"ok": False, "error": "Сессия не найдена"}), 404)
    return workspaces.workspace_root(identity.chat_owner(), chat_id), None


@workspace_bp.get("/api/agent/workspace/tree")
def workspace_tree():
    root, error = _root()
    if error:
        return error
    return jsonify({"ok": True, "files": workspaces.tree(root)})


@workspace_bp.get("/api/agent/workspace/file")
def workspace_file():
    root, error = _root()
    if error:
        return error
    try:
        path = workspaces.safe_path(root, request.args.get("path", ""))
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    if not path.is_file() or path.is_symlink():
        return jsonify({"ok": False, "error": "Файл не найден"}), 404
    if path.stat().st_size > 2_000_000:
        return jsonify({"ok": False, "error": "Файл слишком большой для просмотра"}), 413
    try:
        content = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return jsonify({"ok": False, "error": "Файл не является UTF-8 текстом"}), 415
    return jsonify({"ok": True, "path": path.relative_to(root).as_posix(), "content": content})


@workspace_bp.get("/api/agent/workspace/zip")
def workspace_zip():
    root, error = _root()
    if error:
        return error
    memory = io.BytesIO()
    with zipfile.ZipFile(memory, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in root.rglob("*"):
            if path.is_symlink() or not path.is_file():
                continue
            try:
                rel = path.resolve().relative_to(root).as_posix()
            except ValueError:
                continue
            archive.write(path, rel)
    memory.seek(0)
    return send_file(memory, mimetype="application/zip", as_attachment=True,
                     download_name="novamind-workspace.zip")
