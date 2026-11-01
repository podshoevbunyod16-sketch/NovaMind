"""Runtime transports for MCP connections beyond Streamable HTTP.

The registry persists configuration; this module owns only live STDIO/SSE
sessions.  Calls are serialized per server because JSON-RPC responses share one
stream.  No shell is used for STDIO commands.
"""
from __future__ import annotations

import atexit
import json
import os
import queue
import shlex
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urljoin, urlparse

import requests


class TransportError(RuntimeError):
    pass


def _json_id() -> int:
    return int(time.time_ns() % 2_000_000_000)


def _payload(method: str, params: dict[str, Any] | None) -> dict[str, Any]:
    value: dict[str, Any] = {"jsonrpc": "2.0", "method": method, "params": params or {}}
    if not method.startswith("notifications/"):
        value["id"] = _json_id()
    return value


@dataclass
class StdioSession:
    server_id: str
    item: dict[str, Any]
    timeout: float
    process: subprocess.Popen | None = None
    responses: "queue.Queue[dict[str, Any]]" = field(default_factory=queue.Queue)
    errors: "queue.Queue[str]" = field(default_factory=queue.Queue)
    lock: threading.RLock = field(default_factory=threading.RLock)

    def start(self) -> None:
        if self.process and self.process.poll() is None:
            return
        if str(os.getenv("MCP_STDIO_ENABLED", "0")).lower() not in {"1", "true", "yes", "on"}:
            raise TransportError("STDIO MCP выключен: задайте MCP_STDIO_ENABLED=1")
        command = str(self.item.get("command") or "").strip()
        if not command:
            raise TransportError("Для STDIO MCP требуется command")
        argv = [command, *[str(arg) for arg in (self.item.get("args") or [])]]
        # Explicit argv + shell=False prevents command injection.  A deployment
        # can additionally constrain executable names.
        allowed = {x.strip() for x in os.getenv("MCP_STDIO_COMMANDS", "node,npx,python,python3,uvx").split(",") if x.strip()}
        executable = os.path.basename(command)
        if allowed and executable not in allowed:
            raise TransportError(f"STDIO command '{executable}' не разрешён (MCP_STDIO_COMMANDS)")
        # Never leak NovaMind provider/session secrets into an untrusted MCP
        # child. Only process essentials plus explicitly configured MCP env.
        env = {key: os.environ[key] for key in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR")
               if os.environ.get(key)}
        for key, value in dict(self.item.get("env") or {}).items():
            key = str(key)
            if key and key.replace("_", "").isalnum() and len(key) <= 80:
                env[key] = str(value)
        self.process = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            cwd=str(self.item.get("cwd") or os.getcwd()), env=env, shell=False,
        )
        threading.Thread(target=self._read_stdout, daemon=True,
                         name=f"mcp-stdio-{self.server_id}").start()
        threading.Thread(target=self._read_stderr, daemon=True,
                         name=f"mcp-stdio-err-{self.server_id}").start()

    def _read_stdout(self) -> None:
        assert self.process and self.process.stdout
        for line in self.process.stdout:
            text = line.strip()
            if not text:
                continue
            try:
                value = json.loads(text)
            except ValueError:
                continue
            if isinstance(value, dict):
                self.responses.put(value)

    def _read_stderr(self) -> None:
        assert self.process and self.process.stderr
        for line in self.process.stderr:
            text = line.strip()
            if text:
                self.errors.put(text[:1000])

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        with self.lock:
            self.start()
            assert self.process and self.process.stdin
            value = _payload(method, params)
            try:
                self.process.stdin.write(json.dumps(value, ensure_ascii=False) + "\n")
                self.process.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                self.close()
                raise TransportError(f"STDIO MCP завершился: {exc}") from exc
            if "id" not in value:
                return {}
            deadline = time.monotonic() + self.timeout
            skipped: list[dict[str, Any]] = []
            try:
                while time.monotonic() < deadline:
                    try:
                        response = self.responses.get(timeout=max(0.01, deadline - time.monotonic()))
                    except queue.Empty:
                        break
                    if response.get("id") == value["id"]:
                        return response
                    skipped.append(response)
            finally:
                for response in skipped:
                    self.responses.put(response)
            hint = ""
            while not self.errors.empty():
                hint = self.errors.get_nowait()
            raise TimeoutError(f"STDIO MCP не ответил за {self.timeout:g} с" + (f": {hint}" if hint else ""))

    def close(self) -> None:
        process, self.process = self.process, None
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()


@dataclass
class SseSession:
    server_id: str
    item: dict[str, Any]
    headers_factory: Callable[[dict[str, Any]], dict[str, str]]
    timeout: float
    endpoint: str = ""
    responses: "queue.Queue[dict[str, Any]]" = field(default_factory=queue.Queue)
    errors: "queue.Queue[Exception]" = field(default_factory=queue.Queue)
    ready: threading.Event = field(default_factory=threading.Event)
    stop: threading.Event = field(default_factory=threading.Event)
    lock: threading.RLock = field(default_factory=threading.RLock)
    thread: threading.Thread | None = None
    response: requests.Response | None = None

    def start(self) -> None:
        if self.thread and self.thread.is_alive() and self.endpoint:
            return
        self.stop.clear(); self.ready.clear(); self.endpoint = ""
        self.thread = threading.Thread(target=self._listen, daemon=True,
                                       name=f"mcp-sse-{self.server_id}")
        self.thread.start()
        if not self.ready.wait(self.timeout):
            self.close()
            raise TimeoutError(f"SSE MCP не прислал endpoint за {self.timeout:g} с")
        if not self.errors.empty():
            raise TransportError(str(self.errors.get_nowait()))

    def _listen(self) -> None:
        event = "message"
        data_lines: list[str] = []
        try:
            headers = self.headers_factory(self.item)
            headers.pop("Content-Type", None)
            self.response = requests.get(self.item["url"], headers=headers, stream=True,
                                         timeout=(self.timeout, None))
            self.response.raise_for_status()
            for raw in self.response.iter_lines(chunk_size=1, decode_unicode=True):
                if self.stop.is_set():
                    break
                line = str(raw or "")
                if line.startswith("event:"):
                    event = line[6:].strip()
                elif line.startswith("data:"):
                    data_lines.append(line[5:].strip())
                elif not line and data_lines:
                    self._event(event, "\n".join(data_lines)); event = "message"; data_lines = []
        except Exception as exc:  # noqa: BLE001
            self.errors.put(exc)
            self.ready.set()

    def _event(self, event: str, data: str) -> None:
        if event == "endpoint":
            endpoint = urljoin(self.item["url"], data)
            # Never follow a server-provided message endpoint to another host.
            if urlparse(endpoint).netloc != urlparse(self.item["url"]).netloc:
                self.errors.put(TransportError("SSE endpoint сменил host/port")); self.ready.set(); return
            self.endpoint = endpoint; self.ready.set(); return
        try:
            value = json.loads(data)
        except ValueError:
            return
        if isinstance(value, dict):
            self.responses.put(value)

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        with self.lock:
            self.start()
            value = _payload(method, params)
            response = requests.post(self.endpoint, headers=self.headers_factory(self.item),
                                     json=value, timeout=self.timeout)
            response.raise_for_status()
            if "id" not in value:
                return {}
            deadline = time.monotonic() + self.timeout
            while time.monotonic() < deadline:
                try:
                    result = self.responses.get(timeout=max(.01, deadline - time.monotonic()))
                except queue.Empty:
                    break
                if result.get("id") == value["id"]:
                    return result
            raise TimeoutError(f"SSE MCP не ответил за {self.timeout:g} с")

    def close(self) -> None:
        self.stop.set()
        if self.response is not None:
            self.response.close()
        self.endpoint = ""; self.ready.clear()


_LOCK = threading.RLock()
_SESSIONS: dict[tuple[str, str], Any] = {}


def request_transport(server_id: str, item: dict[str, Any], method: str,
                      params: dict[str, Any] | None, headers_factory,
                      timeout: float) -> dict[str, Any]:
    transport = str(item.get("transport") or "http").lower()
    key = (transport, server_id)
    with _LOCK:
        session = _SESSIONS.get(key)
        if session is None:
            if transport == "stdio":
                session = StdioSession(server_id, item, timeout)
            elif transport == "sse":
                session = SseSession(server_id, item, headers_factory, timeout)
            else:
                raise TransportError(f"Неизвестный MCP transport: {transport}")
            _SESSIONS[key] = session
    return session.call(method, params)


def close_transport(server_id: str) -> None:
    with _LOCK:
        keys = [key for key in _SESSIONS if key[1] == server_id]
        sessions = [_SESSIONS.pop(key) for key in keys]
    for session in sessions:
        try:
            session.close()
        except Exception:
            pass


def close_all() -> None:
    with _LOCK:
        ids = {key[1] for key in _SESSIONS}
    for server_id in ids:
        close_transport(server_id)


atexit.register(close_all)
