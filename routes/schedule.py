"""routes/schedule.py — API заданий по расписанию (бонус Б4).

    GET    /api/schedule            — список заданий владельца
    POST   /api/schedule            — создать (prompt + cron | every)
    DELETE /api/schedule/<id>       — удалить
    POST   /api/schedule/<id>/run   — выполнить сейчас

Задание запускает того же агента и пишет результат в отдельный чат
«⏰ Расписание» — обычный разговор остаётся чистым.
"""
from flask import Blueprint, jsonify, request, session

import database
import scheduler
from routes.terminal import signed_in

schedule_bp = Blueprint("schedule", __name__)


def owner_key():
    """Кто владелец задания: ник, e-mail или пусто для гостя."""
    return str(session.get("admin_username") or session.get("nova_user_nick")
               or session.get("nova_user_email") or session.get("user_email")
               or session.get("username") or "").strip().lower()[:180]


def is_admin():
    return bool(session.get("admin_logged_in"))


@schedule_bp.route("/api/schedule", methods=["GET"])
def list_schedule():
    jobs = database.list_jobs(owner=owner_key() or None)
    if not signed_in():
        jobs = []
    return jsonify({"jobs": jobs, "tick": scheduler.TICK_SECONDS,
                    "signed_in": signed_in()})


@schedule_bp.route("/api/schedule", methods=["POST"])
def create_schedule():
    if not signed_in():
        return jsonify({"error": "Войдите, чтобы ставить задачи по расписанию"}), 403
    data = request.get_json(silent=True) or {}
    prompt = str(data.get("prompt") or "").strip()
    kind = str(data.get("kind") or ("cron" if data.get("cron") else "every")).lower()
    expr = str(data.get("cron") or data.get("every") or data.get("expr") or "").strip()
    try:
        job = scheduler.create_job(owner_key(), prompt, kind, expr,
                                   chat_id=str(session.get("chat_id") or ""),
                                   owner_admin=is_admin())
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    return jsonify({"job": job}), 201


@schedule_bp.route("/api/schedule/<job_id>", methods=["DELETE"])
def delete_schedule(job_id):
    job = database.get_job(job_id)
    if not job:
        return jsonify({"error": "Задание не найдено"}), 404
    if job.get("owner") != owner_key() and not is_admin():
        return jsonify({"error": "Это задание другого пользователя"}), 403
    database.delete_job(job_id)
    return jsonify({"success": True, "id": job_id})


@schedule_bp.route("/api/schedule/<job_id>/run", methods=["POST"])
def run_schedule_now(job_id):
    job = database.get_job(job_id)
    if not job:
        return jsonify({"error": "Задание не найдено"}), 404
    if job.get("owner") != owner_key() and not is_admin():
        return jsonify({"error": "Это задание другого пользователя"}), 403
    outcome = scheduler.run_job(job)
    return jsonify({"success": True, "result": outcome, "job": database.get_job(job_id)})
