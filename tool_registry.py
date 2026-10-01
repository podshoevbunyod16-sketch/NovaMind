"""Unified tool registry for NovaMind agents."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Callable, Mapping
import logging, time

log = logging.getLogger("novamind.agent")

@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    schema: Mapping[str, Any]
    execute: Callable[..., dict]
    permissions: tuple[str, ...] = ()
    timeout: float = 30.0

    def call(self, **kwargs) -> dict:
        started = time.monotonic()
        try:
            result = self.execute(**kwargs)
            if not isinstance(result, dict):
                result = {"ok": True, "result": result}
            result.setdefault("ok", True)
            result.setdefault("tool", self.name)
            return result
        except Exception as exc:
            log.exception("tool=%s failed", self.name)
            return {"ok": False, "tool": self.name,
                    "error": {"type": exc.__class__.__name__, "message": str(exc),
                              "retryable": isinstance(exc, (TimeoutError, ConnectionError))}}
        finally:
            log.info("tool=%s duration_ms=%d", self.name,
                     int((time.monotonic()-started)*1000))

_REGISTRY: dict[str, ToolSpec] = {}

def register_tool(spec: ToolSpec) -> ToolSpec:
    _REGISTRY[spec.name] = spec
    return spec

def get_tool(name: str) -> ToolSpec | None:
    return _REGISTRY.get(name)

def list_tools() -> list[ToolSpec]:
    return list(_REGISTRY.values())

def tool_schemas() -> list[dict]:
    return [{"name": t.name, "description": t.description, "schema": dict(t.schema),
             "permissions": list(t.permissions), "timeout": t.timeout} for t in list_tools()]

def call_tool(name: str, **kwargs) -> dict:
    spec = get_tool(name)
    if not spec:
        return {"ok": False, "error": {"type": "unknown_tool", "message": f"Unknown tool: {name}", "retryable": False}}
    return spec.call(**kwargs)

def bootstrap_default_tools():
    if get_tool("web_search") is None:
        from routes.search import search_web, fetch_page_text
        register_tool(ToolSpec(
            "web_search", "Search the public web and return normalized ranked sources.",
            {"type":"object","properties":{"query":{"type":"string"},"limit":{"type":"integer"}},"required":["query"]},
            lambda query, limit=8: _search_result(search_web(query, num=min(int(limit),12))),
            permissions=("network",), timeout=20))
        register_tool(ToolSpec(
            "web_open", "Open a public HTTP(S) page and extract readable text.",
            {"type":"object","properties":{"url":{"type":"string"},"max_chars":{"type":"integer"}},"required":["url"]},
            lambda url, max_chars=6000: _open_result(fetch_page_text(url, max_chars=min(int(max_chars),8000))),
            permissions=("network",), timeout=15))
    if get_tool("shell") is None:
        from routes.terminal import run_agent
        register_tool(ToolSpec(
            "shell", "Run a policy-checked command inside the workspace.",
            {"type":"object","properties":{"command":{"type":"string"},"timeout":{"type":"number"}},"required":["command"]},
            lambda command, timeout=12: _shell_result(run_agent(command, timeout=min(float(timeout),30))),
            permissions=("workspace",), timeout=30))
    if get_tool("file_read") is None:
        from routes.terminal import safe_path
        def read_file(path, max_chars=60000):
            import os
            p=safe_path(path)
            if not os.path.isfile(p):
                return {"ok":False,"error":{"type":"not_found","message":"File not found","retryable":False}}
            with open(p,"r",encoding="utf-8",errors="replace") as f:
                data=f.read(int(max_chars))
            return {"ok":True,"path":path,"content":data}
        register_tool(ToolSpec(
            "file_read", "Read a text file inside workspace.",
            {"type":"object","properties":{"path":{"type":"string"},"max_chars":{"type":"integer"}},"required":["path"]},
            read_file, permissions=("workspace",), timeout=5))

    if get_tool("file_write") is None:
        from routes.terminal import safe_path, relative_to_workspace
        def write_file(path, content=""):
            import os
            p = safe_path(path)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                f.write(str(content))
            return {"ok": True, "path": relative_to_workspace(p),
                    "bytes": len(str(content).encode("utf-8"))}
        register_tool(ToolSpec(
            "file_write", "Write a UTF-8 text file inside workspace.",
            {"type":"object","properties":{"path":{"type":"string"},"content":{"type":"string"}},"required":["path","content"]},
            write_file, permissions=("workspace",), timeout=5))

    if get_tool("git") is None:
        from routes.terminal import run_agent
        def git(command="status --short", timeout=12):
            cmd = str(command).strip()
            if not cmd.startswith("git"):
                cmd = "git " + cmd
            return _shell_result(run_agent(cmd, timeout=min(float(timeout), 30)))
        register_tool(ToolSpec(
            "git", "Run a Git command inside the workspace.",
            {"type":"object","properties":{"command":{"type":"string"},"timeout":{"type":"number"}},"required":["command"]},
            git, permissions=("workspace",), timeout=30))

def _search_result(pair):
    results, trace = pair
    return {"ok": bool(results), "results": results, "trace": trace, "count": len(results)}

def _open_result(pair):
    text, error = pair
    return {"ok": bool(text), "content": text or "",
            "error": None if text else {"type":"fetch_failed","message":error or "empty page","retryable":True}}

def _shell_result(result):
    ok = int(result.get("exit_code", result.get("code",-1))) == 0 and not result.get("timed_out")
    return {**result, "ok": ok, "exit_code": result.get("exit_code", result.get("code",-1)),
            "error": None if ok else {"type":"command_failed" if not result.get("timed_out") else "timeout",
                                      "message": result.get("stderr") or "command failed",
                                      "retryable": bool(result.get("timed_out"))}}
