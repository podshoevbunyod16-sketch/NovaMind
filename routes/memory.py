"""routes/memory.py — панель «Память» (этап 4).

Список, поиск и удаление долгосрочных воспоминаний пользователя.
Гостям память недоступна: у них нет ни списка, ни сохранения.
"""
from flask import Blueprint, jsonify, request, session

import database
import memory
from routes.terminal import signed_in

memory_bp = Blueprint("memory", __name__)


def _key():
    return memory.user_key(session, signed_in())


def _guard():
    if not memory.memory_enabled():
        return jsonify({"ok": False, "error": "Память выключена (MEMORY_ENABLED=0)"}), 403
    if not _key():
        return jsonify({"ok": False, "error": "Память доступна только после входа"}), 403
    return None


@memory_bp.route("/api/memory", methods=["GET"])
def memory_list():
    """Список воспоминаний; ?q= — поиск по-русски."""
    denied = _guard()
    if denied:
        return denied
    query = (request.args.get("q") or "").strip()
    limit = request.args.get("limit", type=int) or 100
    items = memory.list_all(_key(), query, limit=max(1, min(limit, 300)))
    return jsonify({"ok": True, "items": items, "count": len(items),
                    "fts5": database.memory_fts5(), "kinds": list(memory.MEMORY_KINDS)})


@memory_bp.route("/api/memory", methods=["POST"])
def memory_create():
    """Ручное добавление: {"text": "...", "kind": "fact", "tags": "..."}."""
    denied = _guard()
    if denied:
        return denied
    data = request.get_json(silent=True) or {}
    try:
        item = memory.remember(_key(), data.get("text"),
                               kind=data.get("kind") or "fact",
                               tags=data.get("tags") or "",
                               importance=data.get("importance") or 1)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, "item": item}), 201


@memory_bp.route("/api/memory/<memory_id>", methods=["DELETE"])
def memory_delete(memory_id):
    """Удалить своё воспоминание по id."""
    denied = _guard()
    if denied:
        return denied
    if not memory.delete(_key(), memory_id):
        return jsonify({"ok": False, "error": "Запись не найдена"}), 404
    return jsonify({"ok": True, "id": memory_id})


@memory_bp.route("/api/memory/status")
def memory_status():
    """Доступна ли память этому пользователю (для панели и тестов)."""
    key = _key()
    return jsonify({"ok": True, "enabled": memory.memory_enabled(),
                    "available": bool(key), "fts5": database.memory_fts5(),
                    "count": database.count_memory(key) if key else 0})
