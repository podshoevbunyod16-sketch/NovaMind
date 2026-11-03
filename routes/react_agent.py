"""Durable HTTP endpoints for the first-party ReAct runner."""
from __future__ import annotations

import json
import os
import re
import queue
import threading
import time
import uuid

from flask import Blueprint, Response, current_app, jsonify, request, session

import ai_providers
import capabilities
import database
import identity
import tool_registry
from agent.loop import AgentContext, run_react

react_bp = Blueprint("react_agent", __name__)
_ACTIVE_REACT: dict[str, threading.Event] = {}
_ACTIVE_REACT_LOCK = threading.Lock()


def _encode(events):
    return "".join(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n" for event in events)


def cancel_active_react_run(run_id):
    """Cancel an active first-party run from the compatibility stop endpoint."""
    with _ACTIVE_REACT_LOCK:
        cancel = _ACTIVE_REACT.get(str(run_id or ""))
        if cancel is None:
            return False
        cancel.set()
        return True


def _run(goal, record_id, *, chat_id="", history=None, observations=None,
         step_offset=0, resume=False, workspace=False,
         provider=None, model=None, run_id=None, approvals=None, tools_enabled=True,
         tool_names=None, cancel=None, rate_limit=None, rate_remaining=None, rate_scope="",
         search_requested=False):
    tool_registry.bootstrap_default_tools()
    chosen_provider, chosen_model, _cfg = ai_providers.resolve_target(provider, model)
    configured_tokens = max(256, int(os.getenv("AGENT_MAX_TOKENS", "4096")))
    try:
        provider_cap = int((_cfg or {}).get("max_tokens") or configured_tokens)
    except (TypeError, ValueError):
        provider_cap = configured_tokens
    max_tokens = max(256, min(configured_tokens, provider_cap))
    specs = tool_registry.list_tools() if tools_enabled else []
    mcp_only = bool(re.search(r"\bтолько\b.{0,60}\b(?:mcp|github)\b", str(goal or ""), re.I))
    if mcp_only:
        specs = [spec for spec in specs if spec.name not in {"shell", "bash", "python", "git"}]
    if not workspace:
        specs = [spec for spec in specs if "workspace" not in getattr(spec, "permissions", ())
                 and spec.name not in getattr(tool_registry, "WORKSPACE_TOOLS", set())]
    if tool_names is not None:
        requested = set(str(name) for name in tool_names)
        if "mcp_call" in requested:
            requested.add("mcp_list")
        specs = [spec for spec in specs if spec.name in requested]
    owner = identity.chat_owner()
    is_admin = identity.is_admin_owner(owner)
    role = "admin" if is_admin else ("user" if owner.startswith("email:") else "guest")
    memory_context = ""
    integration_context = ""
    try:
        if role == "admin" or str(os.getenv("MCP_ALLOW_USER_READ", "0")) == "1":
            import mcp_registry
            connected = mcp_registry.connected_tools()
            servers = mcp_registry.server_summaries(connected)
            tool_catalog = [{"server_id": item.get("server_id"), "server_name": item.get("server_name"),
                             "name": item.get("name"), "description": item.get("description"),
                             "inputSchema": item.get("inputSchema"), "annotations": item.get("annotations")}
                            for item in connected]
            integration_context = (
                "Подключённые MCP-серверы и обнаруженные инструменты: "
                + json.dumps({"servers": servers, "tools": tool_catalog}, ensure_ascii=False,
                             separators=(",", ":"))
                + "\nЕсли нужного инструмента нет в списке или контекст обрезан, вызови mcp_list с server_id; "
                "для большого каталога продолжай по next_offset. Выполняй только требуемое действие, "
                "проверяй isError/ошибку и не объявляй задачу успешной без полноценного итогового ответа."
            )
    except Exception:
        current_app.logger.warning("Не удалось перечислить MCP-инструменты", exc_info=True)
    try:
        import memory as memory_store
        from routes.terminal import signed_in
        memory_key = memory_store.user_key(session, bool(signed_in()))
        memory_context = memory_store.prompt_block(memory_key, goal)
    except Exception:
        current_app.logger.warning("Не удалось загрузить память для ReAct", exc_info=True)
    current_run_id = str(run_id or record_id)
    try:
        import prompt_registry
        prompt_versions = prompt_registry.record_use_all()
    except Exception:
        prompt_versions = {}
    import telemetry
    trace = telemetry.start_trace(
        "react.turn", run_id=current_run_id, chat_id=chat_id, provider=chosen_provider,
        model=chosen_model, role=role, engine="react", prompt_versions=prompt_versions,
        goal_chars=len(str(goal or "")))
    trace_id = trace.trace_id
    _cap_token, capability = capabilities.mint(owner=owner, run_id=current_run_id,
                                                role=role, tools=[spec.name for spec in specs])
    specs = [spec for spec in specs if capabilities.normalize_tool(spec.name) in capability.tools]
    events = queue.Queue()
    pending_events = []
    trace_events = []
    sources_found = False
    sources_results = []
    media_results = []
    step_sequence = max(0, int(step_offset or 0))
    workspace_used = False
    workspace_tool_names = {spec.name for spec in specs
                            if "workspace" in getattr(spec, "permissions", ())}
    cancel = cancel or threading.Event()
    def emit(event):
        nonlocal sources_found, sources_results, media_results, step_sequence, workspace_used
        if isinstance(event, dict):
            if event.get("type") == "tool":
                from agent.events import presentation_from_event
                event.update(presentation_from_event(event))
                step_sequence += 1
                try:
                    database.add_agent_run_step(
                        current_run_id, step_sequence, event.get("name"),
                        event.get("label") or event.get("name"),
                        ok=event.get("status") == "ok",
                        output=str(event.get("output") or "")[:4000])
                except Exception:
                    current_app.logger.warning("Не удалось сохранить шаг ReAct-запуска", exc_info=True)
            if event.get("type") in {"stage", "tool", "sources", "ask_user", "confirmation", "guard", "capability", "knowledge"}:
                span_name = ("tool:" + str(event.get("name")) if event.get("type") == "tool"
                             else str(event.get("type")))
                span = trace.span(span_name, kind="tool" if event.get("type") == "tool" else "event",
                                  attributes={"status": event.get("status", "ok"),
                                              "tool": event.get("name", "")})
                span.event(str(event.get("type")), {key: event.get(key) for key in ("scene", "name", "status")})
                span.finish("error" if event.get("status") == "error" else "ok")
            if event.get("type") in {"ask_user", "confirmation"}:
                pending_events.append(event)
            if event.get("type") in {"stage", "tool", "sources", "ask_user",
                                      "confirmation", "guard", "capability", "knowledge", "result"} and len(trace_events) < 200:
                snapshot = dict(event)
                snapshot["kind"] = ("tool" if event.get("type") == "tool" else
                                    "step" if event.get("type") in {"stage", "sources", "knowledge"} else
                                    str(event.get("type") or "event"))
                if event.get("type") == "tool" and isinstance(snapshot.get("command"), str):
                    try:
                        arguments = json.loads(snapshot["command"])
                        if isinstance(arguments, dict):
                            snapshot["command"] = str(arguments.get("command") or arguments.get("path") or
                                                       arguments.get("query") or arguments.get("url") or "")
                    except (ValueError, TypeError):
                        pass
                for key in ("text", "output", "command"):
                    if isinstance(snapshot.get(key), str):
                        snapshot[key] = snapshot[key][:4000]
                trace_events.append(snapshot)
            if event.get("type") == "sources" and event.get("sources"):
                sources_found = True
                known_urls = {str(item.get("url") or "") for item in sources_results}
                for source in event["sources"]:
                    if not isinstance(source, dict):
                        continue
                    url = str(source.get("url") or "")
                    if url and url not in known_urls:
                        sources_results.append(dict(source))
                        known_urls.add(url)
            if event.get("type") == "media" and isinstance(event.get("media"), dict):
                media_results.append(dict(event["media"]))
            if event.get("type") == "tool" and event.get("name") in workspace_tool_names:
                workspace_used = True
            if event.get("type") == "result":
                event.setdefault("chat_id", chat_id)
                event.setdefault("run_id", current_run_id)
                event.setdefault("provider", chosen_provider)
                event.setdefault("model", chosen_model)
                event.setdefault("linux", workspace_used)
                event.setdefault("searched", sources_found)
                event.setdefault("sources", sources_results[:30])
                if media_results:
                    event.setdefault("media", media_results)
                event.setdefault("offline", chosen_provider == "local_demo")
                event.setdefault("trace_id", trace_id)
                event.setdefault("role", role)
                event.setdefault("prompt_versions", prompt_versions)
                if chat_id and event.get("status") in {"completed", "paused"} and "usage" not in event:
                    try:
                        import usage_stats
                        usage = usage_stats.record_turn(
                            chat_id, chosen_provider, chosen_model, str(goal or ""),
                            str(event.get("reply") or ""), usage={})
                        event["usage"] = usage
                        events.put({"type": "usage", "usage": usage})
                    except Exception:
                        current_app.logger.warning("Не удалось записать токены ReAct", exc_info=True)
            if event.get("type") == "media" and chat_id:
                media_payload = event.get("media") if isinstance(event.get("media"), dict) else {}
                try:
                    from routes.chat import media_result_marker
                    model_name = str(media_payload.get("model_name") or media_payload.get("model") or "")
                    provider_id = str(media_payload.get("provider") or media_payload.get("provider_id") or "")
                    provider_name = str(media_payload.get("provider_name") or provider_id)
                    marker_model = ({"id": model_name, "name": model_name,
                                     "provider": provider_id, "provider_name": provider_name}
                                    if model_name and provider_id else {})
                    marker = media_result_marker(media_payload, marker_model)
                    database.add_message(chat_id, "assistant", marker)
                except Exception:
                    current_app.logger.warning("Не удалось сохранить маркер медиа ReAct-запуска",
                                               exc_info=True)
        events.put(event)
    context = AgentContext(
        goal=goal, complete=ai_providers.chat_completion, emit=emit,
        provider=chosen_provider, model=chosen_model, history=history or [],
        observations=observations or [], step_offset=int(step_offset or 0),
        workspace=bool(workspace), run_id=current_run_id, resume=resume,
        cancel=cancel, approvals=list(approvals or []), tools=specs,
        memory_context=memory_context, integration_context=integration_context,
        search_requested=bool(search_requested),
        max_steps=int(os.getenv("AGENT_MAX_STEPS", "20")),
        timeout=float(os.getenv("AGENT_TIMEOUT", "240")),
        max_tokens=max_tokens,
    )

    def worker():
        cap_token = capabilities.activate(capability)
        workspace_token = None
        final_status = "provider_error"
        try:
            emit({"type": "capability", "role": role,
                  "tools": sorted(capability.tools), "run_id": current_run_id})
            if workspace:
                from agent.workspace import bind_workspace, workspace_root
                workspace_token = bind_workspace(workspace_root(owner, chat_id or current_run_id))
            result = run_react(context)
            final_status = result.status
            database.update_agent_run(
                record_id, status=result.status, answer=result.answer,
                steps=result.steps, provider=chosen_provider, model=chosen_model,
                context=result.context or {"goal": goal, "messages": history or []},
                pending_question=pending_events[-1] if pending_events else "",
                finished_at=time.time() if result.status != "paused" else None)
            if result.status != "paused" and chat_id and result.answer:
                database.add_message(chat_id, "assistant", result.answer)
        except Exception as error:  # preserve an answer and close the run on unexpected errors
            fallback = f"Агент завершился с внутренней ошибкой ({type(error).__name__}). Задача не подтверждена как выполненная."
            database.finish_agent_run(record_id, "provider_error", answer=fallback,
                                      error=str(error)[:1000])
            events.put({"type": "token", "token": fallback})
            events.put({"type": "result", "reply": fallback, "status": "provider_error"})
            events.put({"type": "done"})
        finally:
            trace.set_attribute("tool_names", sorted(capability.tools))
            trace.finish("ok" if final_status == "completed" else final_status,
                         result={"run_id": current_run_id, "status": final_status,
                                 "tools": sorted(capability.tools)})
            telemetry.save_trace(trace)
            if chat_id and trace_events:
                try:
                    database.save_trace(chat_id, current_run_id, trace_events)
                except Exception:
                    current_app.logger.warning("Не удалось сохранить трассу ReAct-запуска",
                                               exc_info=True)
            if workspace_token is not None:
                from agent.workspace import reset_workspace
                reset_workspace(workspace_token)
            capabilities.deactivate(cap_token)
            with _ACTIVE_REACT_LOCK:
                _ACTIVE_REACT.pop(current_run_id, None)
            events.put(None)

    flask_app = current_app._get_current_object()
    request_environ = dict(request.environ)
    request_environ.pop("werkzeug.request", None)
    session_snapshot = dict(session)
    def worker_with_request_context():
        with flask_app.request_context(request_environ):
            session.update(session_snapshot)
            worker()
    thread = threading.Thread(target=worker_with_request_context,
                              name=f"react-{current_run_id[:8]}", daemon=True)
    thread.start()

    def stream():
        try:
            while True:
                event = events.get()
                if event is None:
                    break
                yield json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
        finally:
            if thread.is_alive():
                cancel.set()
            # Drain the worker before the request context and its backing
            # resources are released. A bounded join avoids leaking a daemon
            # thread when a client disconnects during a slow provider call.
            thread.join(timeout=1.0)

    response = Response(stream(), mimetype="application/x-ndjson")
    response.headers["X-Agent-Run-Id"] = current_run_id
    response.headers["X-Trace-Id"] = trace_id
    response.headers["X-Agent-Role"] = role
    if rate_limit is not None:
        response.headers["X-RateLimit-Limit"] = str(rate_limit)
    if rate_remaining is not None:
        response.headers["X-RateLimit-Remaining"] = str(rate_remaining)
    if rate_scope:
        response.headers["X-RateLimit-Scope"] = str(rate_scope)
    return response


@react_bp.post("/api/agent/react")
def start_react_run(payload=None):
    from agent.runtime import is_enabled
    if not is_enabled():
        return jsonify({"ok": False, "error": "ИИ-агент отключён настройкой AGENT_ENABLED=0"}), 403
    payload = payload if isinstance(payload, dict) else (request.get_json(silent=True) or {})
    import guardrails
    input_verdict = guardrails.check_input(str(payload.get("message") or ""))
    if not input_verdict.allowed:
        return jsonify({"ok": False, "error": "Запрос отклонён проверкой безопасности",
                        "guard": input_verdict.event("input")}), 400
    goal = input_verdict.text.strip()
    if not goal:
        return jsonify({"ok": False, "error": "Пустое сообщение"}), 400
    from agent.runtime import bind_client_chat
    if not bind_client_chat(payload):
        return jsonify({"ok": False, "error": "Сессия не найдена"}), 404
    chat_id = str(payload.get("chat_id") or session.get("chat_id") or "")
    if not chat_id:
        chat_id = database.get_or_create_session_chat()
    if not database.chat_allowed(chat_id):
        return jsonify({"ok": False, "error": "Сессия не найдена"}), 404
    import security
    from routes.terminal import can_use_workspace, signed_in
    from agent.runtime import parse_bool, request_tools_allowed
    signed = bool(signed_in())
    rate_key = str(session.get("agent_memory_key") or session.get("nova_user_email")
                   or session.get("user_email") or session.get("username") or "")
    if security.is_internal_run(request):
        allowed, retry_after, limit, remaining, scope = True, 0, 0, 0, "internal"
    else:
        allowed, retry_after, limit, remaining, scope = security.agent_rate_check(
            request, signed, rate_key)
    if not allowed:
        response = jsonify({"ok": False,
                            "error": "Слишком много запросов к агенту. Попробуйте позже.",
                            "retry_after": retry_after, "limit": limit,
                            "scope": scope})
        response.headers["Retry-After"] = str(retry_after)
        return response, 429
    prior = database.get_chat_history(chat_id, limit=30)
    database.add_message(chat_id, "user", goal)
    workspace = (parse_bool(payload.get("workspace"), default=False)
                 and str(os.getenv("TERMINAL_ENABLED", "0")) == "1"
                 and can_use_workspace())
    run_id = str(uuid.uuid4())
    cancel = threading.Event()
    with _ACTIVE_REACT_LOCK:
        _ACTIVE_REACT[run_id] = cancel
    record_id = database.create_agent_run(goal, chat_id=chat_id, run_id=run_id,
                                          provider=str(payload.get("provider") or ""),
                                          model=str(payload.get("model") or ""), engine="react")
    if not record_id:
        with _ACTIVE_REACT_LOCK:
            _ACTIVE_REACT.pop(run_id, None)
        return jsonify({"ok": False, "error": "Не удалось сохранить запуск"}), 500
    return _run(goal, record_id, chat_id=chat_id,
                history=prior, workspace=workspace, run_id=run_id,
                tools_enabled=request_tools_allowed(payload), cancel=cancel,
                rate_limit=limit, rate_remaining=remaining, rate_scope=scope,
                search_requested=parse_bool(payload.get("auto_search"), default=False))


@react_bp.post("/api/agent/react/stop")
def stop_react_run():
    payload = request.get_json(silent=True) or {}
    run_id = str(payload.get("run_id") or "")
    record = database.get_agent_run(run_id)
    if not record or not database.chat_allowed(record.get("chat_id") or ""):
        return jsonify({"ok": False, "error": "Запуск не найден"}), 404
    with _ACTIVE_REACT_LOCK:
        cancel = _ACTIVE_REACT.get(run_id)
        if cancel is None:
            return jsonify({"ok": False, "error": "Запуск уже завершён или приостановлен"}), 409
        cancel.set()
    return jsonify({"ok": True, "run_id": run_id, "stopping": True})


@react_bp.post("/api/agent/answer")
def answer_react_question():
    payload = request.get_json(silent=True) or {}
    record = database.get_agent_run(str(payload.get("run_id") or ""))
    if not record or not database.chat_allowed(record.get("chat_id") or ""):
        return jsonify({"ok": False, "error": "Запуск не найден"}), 404
    if record.get("status") != "paused":
        return jsonify({"ok": False, "error": "Запуск не ожидает ответа"}), 409
    answer = str(payload.get("answer") or "").strip()
    try:
        context = json.loads(record.get("context") or "{}")
    except (ValueError, TypeError):
        context = {}
    try:
        pending = json.loads(record.get("pending_question") or "")
    except (ValueError, TypeError):
        pending = {}
    if isinstance(pending, dict) and pending.get("type") == "confirmation":
        if not isinstance(payload.get("approved"), bool):
            return jsonify({"ok": False, "error": "Укажите approved: true или false"}), 400
    elif not answer:
        return jsonify({"ok": False, "error": "Введите ответ"}), 400
    messages = list(context.get("messages") or [])
    approvals = list(context.get("approvals") or [])
    if isinstance(pending, dict) and pending.get("type") == "confirmation":
        approved = payload.get("approved") is True
        tool_name = str(pending.get("tool") or "")
        if not approved:
            final = f"Действие «{tool_name}» отклонено пользователем и не выполнялось. Остальная задача не продолжена."
            database.finish_agent_run(record["id"], "abandoned", answer=final,
                                      error="Отклонено пользователем", steps=record.get("steps", 0))
            if record.get("chat_id"):
                database.add_message(record["chat_id"], "assistant", final)
            return Response(_encode([{"type": "token", "token": final},
                                     {"type": "result", "reply": final, "status": "abandoned"},
                                     {"type": "done"},
                                     {"type": "completion", "run_id": record.get("run_id"),
                                      "buttons": ["Готово", "Продолжай работать"]}]), mimetype="application/x-ndjson")
        approval_key = str(pending.get("approval_key") or "")
        if not approval_key and tool_name:
            approval_key = __import__("hashlib").sha256(json.dumps(
                [tool_name, pending.get("arguments") or {}], ensure_ascii=False,
                sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        if approval_key:
            approvals.append(approval_key)
        messages.append({"role": "user", "content": f"Пользователь разрешил действие {tool_name}. Выполни его и проверь результат."})
    else:
        messages.append({"role": "user", "content": "Ответ пользователя на уточнение: " + answer[:4000]})
    database.update_agent_run(record["id"], status="running", pending_question="")
    chat_id = record.get("chat_id") or ""
    if chat_id:
        database.add_message(chat_id, "user", ("Разрешение на действие: " + str(pending.get("tool") or ""))
                             if isinstance(pending, dict) and pending.get("type") == "confirmation"
                             else answer)
    run_id = str(record.get("run_id") or record["id"])
    cancel = threading.Event()
    with _ACTIVE_REACT_LOCK:
        _ACTIVE_REACT[run_id] = cancel
    return _run(context.get("goal") or record.get("goal") or "", record["id"],
                chat_id=chat_id, history=messages,
                observations=context.get("observations") or [],
                step_offset=int(context.get("steps") or 0), resume=True,
                workspace=bool(context.get("workspace")), provider=record.get("provider"),
                model=record.get("model"), run_id=run_id, approvals=approvals,
                tools_enabled=bool(context.get("tools_enabled", True)),
                tool_names=context.get("tool_names"), cancel=cancel)


@react_bp.post("/api/agent/finish")
def finish_react_run():
    payload = request.get_json(silent=True) or {}
    record = database.get_agent_run(str(payload.get("run_id") or ""))
    if not record or not database.chat_allowed(record.get("chat_id") or ""):
        return jsonify({"ok": False, "error": "Запуск не найден"}), 404
    outcome = str(payload.get("outcome") or "").lower()
    if outcome not in {"confirmed", "continued", "abandoned"}:
        return jsonify({"ok": False, "error": "Недопустимый исход"}), 400
    if record.get("status") in {"running", "paused"}:
        return jsonify({"ok": False, "error": "Запуск ещё не завершён"}), 409
    if record.get("status") in {"confirmed", "abandoned"}:
        return jsonify({"ok": False, "error": "Исход запуска уже зафиксирован"}), 409
    if outcome == "continued":
        try:
            context = json.loads(record.get("context") or "{}")
        except (ValueError, TypeError):
            context = {}
        history = list(context.get("messages") or [])
        history.append({"role": "user", "content": "Продолжай работу над исходной целью. Используй наблюдения и не повторяй уже выполненные действия."})
        database.update_agent_run(record["id"], status="running", outcome=outcome)
        run_id = str(record.get("run_id") or record["id"])
        cancel = threading.Event()
        with _ACTIVE_REACT_LOCK:
            _ACTIVE_REACT[run_id] = cancel
        return _run(context.get("goal") or record.get("goal") or "", record["id"],
                    chat_id=record.get("chat_id") or "", history=history,
                    observations=context.get("observations") or [],
                    step_offset=int(context.get("steps") or 0), resume=True,
                    workspace=bool(context.get("workspace")), provider=record.get("provider"),
                    model=record.get("model"), run_id=run_id,
                    tools_enabled=bool(context.get("tools_enabled", True)),
                    tool_names=context.get("tool_names"), cancel=cancel)
    database.update_agent_run(record["id"], outcome=outcome,
                              status="confirmed" if outcome == "confirmed" else "abandoned",
                              finished_at=time.time())
    return jsonify({"ok": True, "run_id": record.get("run_id"), "outcome": outcome})
