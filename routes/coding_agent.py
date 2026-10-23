"""Admin-only read-first coding agent for inspecting the NovaMind source tree.

The exploration loop itself lives in the agent engine
(``agenticseek.core.project_agent`` + ``agenticseek.tools.project_tools``).
This route only enforces the access rules and streams the engine events:

* admin session required;
* the agent may read the project, check syntax and run one test file;
* changes are proposals, never applied — the previous route worked the same way,
  and a server that edits itself is not something a web request may do.
"""
from __future__ import annotations

import logging
import os

from flask import Blueprint, Response, jsonify, request, session, stream_with_context

from ai_providers import chat_completion, resolve_target
from agenticseek.engine import AgentEngine, EngineContext

coding_agent_bp = Blueprint("coding_agent", __name__)
log = logging.getLogger("novamind.coding_agent")

TIMEOUT = max(15, min(int(os.getenv("CODING_AGENT_TIMEOUT", "150")), 240))


def _agent_enabled():
    """Единое правило: агент включён, если не выключен явно."""
    try:
        from routes.agent import is_enabled
        return bool(is_enabled())
    except Exception:  # noqa: BLE001
        return os.getenv("AGENT_ENABLED", "0") == "1"


def _event(payload):
    import json
    return json.dumps(payload, ensure_ascii=False) + "\n"


@coding_agent_bp.route("/api/agent/coding/status")
def coding_status():
    return jsonify({"enabled": _agent_enabled(),
                    "admin_required": True,
                    "engine": "agenticseek",
                    "available": bool(_agent_enabled() and session.get("admin_logged_in"))})


@coding_agent_bp.route("/api/agent/coding/stream", methods=["POST"])
def coding_stream():
    data = request.get_json(silent=True) or {}
    goal = str(data.get("message") or "").strip()
    if not goal:
        return jsonify({"error": "Пустое сообщение."}), 400
    if not _agent_enabled():
        return jsonify({"error": "Auto Coding Agent выключен: включите AGENT_ENABLED=1."}), 403
    if not session.get("admin_logged_in"):
        return jsonify({"error": "Auto Coding Agent доступен только администратору."}), 403

    provider, model, _cfg = resolve_target()

    @stream_with_context
    def generate():
        engine = AgentEngine(EngineContext(
            goal=goal, complete=chat_completion, emit=lambda event: None,
            provider=provider, model=model, profile="coding",
            workspace=False, timeout=TIMEOUT))
        proposals: list = []
        try:
            for event in engine.stream():
                if isinstance(event, dict) and event.get("type") == "patch":
                    proposals.append(event.get("patch"))
                yield _event(event)
        finally:
            pass
        answer = engine.answer
        if proposals:
            extra = []
            for item in proposals:
                extra.append(f"Предложенный патч для `{item.get('path')}` "
                             "(НЕ применён):\n"
                             f"```diff\n{item.get('patch')}\n```")
            answer = (answer + "\n\n" if answer else "") + "\n\n".join(extra)
            for offset in range(0, len(extra), 180):
                yield _event({"type": "token", "token": extra[offset:offset + 180]})
        yield _event({"type": "result", "steps": engine.state.steps, "linux": False,
                      "coding": True, "provider": provider, "model": model,
                      "status": "ok" if engine.state.status == "ok" else engine.state.status,
                      "engine": "agenticseek"})
        yield _event({"type": "done"})

    return Response(generate(), mimetype="application/x-ndjson",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
