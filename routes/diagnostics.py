from flask import Blueprint, jsonify
import os, shutil
import config

diagnostics_bp=Blueprint("diagnostics",__name__)

@diagnostics_bp.get("/api/system/health")
def system_health():
    provider, model, _ = __import__("ai_providers").resolve_target()
    try:
        from routes.search import search_backends
        backends=search_backends()
    except Exception:
        backends=[]
    try:
        from routes.terminal import WORKSPACE, ALLOWED_COMMANDS, is_enabled
        workspace_ok=os.path.isdir(WORKSPACE)
        commands={name:bool(shutil.which(name)) for name in ("bash","sh","ls","grep","find","sed","curl","python3","git")}
        linux={"enabled":is_enabled(),"workspace":WORKSPACE,"workspace_exists":workspace_ok,"commands":commands}
    except Exception as exc:
        linux={"enabled":False,"error":str(exc)}
    try:
        from tool_registry import list_tools, bootstrap_default_tools
        bootstrap_default_tools(); tools=[t.name for t in list_tools()]
    except Exception as exc:
        tools=[]
        linux["tool_registry_error"]=str(exc)
    try:
        from routes.agent import is_enabled as agent_enabled
        agent_on = bool(agent_enabled())
    except Exception:
        agent_on = os.getenv("AGENT_ENABLED", "1") == "1"
    return jsonify({"ai":{"provider":provider,"model":model,"configured":config.has_credentials(provider),
                          "offline":provider=="local_demo"},
                    "search":{"backends":backends,"configured":bool(backends)},
                    "linux":linux,
                    "agent":{"enabled":agent_on,"tools":tools,
                             "workspace":bool(linux.get("enabled"))}})
