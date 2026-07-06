"""
MCP Client for NovaMind
Поддержка подключения к MCP-серверам через stdio транспорт.
Совместимо с любыми серверами, которые умеет Claude Desktop / Claude Code.

Использует выделенный event loop в отдельном потоке для корректной работы
с async context manager (stdio_client спавнит subprocess).
"""

import os
import sys
import json
import asyncio
import threading
import subprocess
import shutil
from pathlib import Path
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field
from concurrent.futures import Future

try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from mcp.types import Tool as MCPTool, TextContent, ImageContent
    MCP_AVAILABLE = True
except ImportError:
    MCP_AVAILABLE = False


CONFIG_PATH = Path(__file__).parent / "mcp_servers.json"


@dataclass
class MCPServerConfig:
    """Конфигурация одного MCP-сервера (формат совместим с Claude Desktop)"""
    name: str
    command: str
    args: List[str] = field(default_factory=list)
    env: Dict[str, str] = field(default_factory=dict)
    enabled: bool = True
    description: str = ""

    @classmethod
    def from_dict(cls, name: str, data: dict) -> "MCPServerConfig":
        return cls(
            name=name,
            command=data.get("command", ""),
            args=data.get("args", []),
            env=data.get("env", {}),
            enabled=data.get("enabled", True),
            description=data.get("description", ""),
        )

    def to_dict(self) -> dict:
        return {
            "command": self.command,
            "args": self.args,
            "env": self.env,
            "enabled": self.enabled,
            "description": self.description,
        }


@dataclass
class MCPToolInfo:
    """Информация о tool из MCP-сервера"""
    server_name: str
    name: str
    description: str
    input_schema: dict


class _AsyncRunner:
    """Выделенный event loop в отдельном потоке для async операций MCP."""

    def __init__(self):
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.thread: Optional[threading.Thread] = None
        self._ready = threading.Event()

    def start(self):
        if self.thread is not None:
            return
        self.thread = threading.Thread(target=self._run_loop, daemon=True, name="mcp-loop")
        self.thread.start()
        self._ready.wait(timeout=5)

    def _run_loop(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self._ready.set()
        try:
            self.loop.run_forever()
        finally:
            self.loop.close()

    def run(self, coro, timeout: float = 60.0):
        """Запустить coroutine в loop потока, вернуть результат синхронно."""
        self.start()
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        try:
            return future.result(timeout=timeout)
        except Exception as e:
            raise


class MCPClient:
    """Клиент для подключения к нескольким MCP-серверам одновременно"""

    def __init__(self, config_path: Path = CONFIG_PATH):
        self.config_path = config_path
        self.servers: Dict[str, MCPServerConfig] = {}
        self._sessions: Dict[str, ClientSession] = {}
        self._tools: Dict[str, MCPToolInfo] = {}  # key: "server.tool"
        self._runner = _AsyncRunner()
        self._streams_contexts: Dict[str, Any] = {}

        self._load_config()

    # ---------- Конфиг ----------
    def _load_config(self):
        """Загрузить mcp_servers.json (формат Claude Desktop)"""
        if not self.config_path.exists():
            self._save_default_config()
            # После создания конфига — загружаем его сразу
            self._load_config()
            return

        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            # Формат Claude Desktop: {"mcpServers": {"name": {...}, ...}}
            servers_dict = data.get("mcpServers", data)
            for name, cfg in servers_dict.items():
                self.servers[name] = MCPServerConfig.from_dict(name, cfg)

            print(f"[MCP] Загружено {len(self.servers)} серверов из {self.config_path.name}")
        except Exception as e:
            print(f"[MCP] Ошибка чтения конфига: {e}")

    def _save_default_config(self):
        """Создать дефолтный конфиг с примерами"""
        default = {
            "mcpServers": {
                "filesystem": {
                    "command": "npx",
                    "args": ["-y", "@modelcontextprotocol/server-filesystem", str(Path.home())],
                    "enabled": False,
                    "description": "Доступ к файлам на компьютере (отключён по умолчанию)"
                },
                "github": {
                    "command": "npx",
                    "args": ["-y", "@modelcontextprotocol/server-github"],
                    "env": {"GITHUB_TOKEN": ""},
                    "enabled": False,
                    "description": "Работа с GitHub: PR, issues, репозитории"
                },
                "fetch": {
                    "command": "uvx",
                    "args": ["mcp-server-fetch"],
                    "enabled": False,
                    "description": "HTTP-запросы к любым URL"
                }
            }
        }
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump(default, f, ensure_ascii=False, indent=2)
        print(f"[MCP] Создан дефолтный конфиг: {self.config_path}")

    def save_config(self):
        """Сохранить текущий конфиг"""
        data = {"mcpServers": {name: srv.to_dict() for name, srv in self.servers.items()}}
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    # ---------- Подключение ----------
    async def connect_server(self, name: str) -> bool:
        """Подключиться к одному MCP-серверу"""
        if not MCP_AVAILABLE:
            print(f"[MCP] SDK не установлен: pip install mcp")
            return False

        cfg = self.servers.get(name)
        if not cfg:
            print(f"[MCP] Сервер {name!r} не найден в конфиге")
            return False

        if not cfg.enabled:
            print(f"[MCP] Сервер {name!r} отключён в конфиге")
            return False

        if name in self._sessions:
            print(f"[MCP] Сервер {name!r} уже подключён")
            return True

        # Подставляем переменные окружения (поддержка ${VAR} и $VAR)
        env = self._expand_env(cfg.env)

        params = StdioServerParameters(
            command=cfg.command,
            args=cfg.args,
            env=env if env else None,
        )

        try:
            # stdio_client — async context manager, спавнит subprocess
            # Используем AsyncExitStack чтобы корректно закрыть и session, и streams
            from contextlib import AsyncExitStack
            stack = AsyncExitStack()
            try:
                read_stream, write_stream = await stack.enter_async_context(stdio_client(params))
                session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
                await session.initialize()

                self._sessions[name] = session
                self._streams_contexts[name] = stack

                # Получаем список tools
                tools_result = await session.list_tools()
                for tool in tools_result.tools:
                    key = f"{name}.{tool.name}"
                    self._tools[key] = MCPToolInfo(
                        server_name=name,
                        name=tool.name,
                        description=tool.description or "",
                        input_schema=tool.inputSchema or {},
                    )

                print(f"[MCP] ✓ {name}: подключён, {len(tools_result.tools)} tools")
                return True
            except BaseException:
                await stack.aclose()
                raise

        except FileNotFoundError as e:
            print(f"[MCP] ✗ {name}: команда не найдена ({e})")
            return False
        except Exception as e:
            print(f"[MCP] ✗ {name}: ошибка подключения: {e}")
            return False

    async def disconnect_server(self, name: str):
        """Отключиться от сервера"""
        self._sessions.pop(name, None)
        stack = self._streams_contexts.pop(name, None)
        if stack:
            try:
                await stack.aclose()
            except Exception as e:
                print(f"[MCP] Ошибка при отключении {name}: {e}")

        # Удаляем tools этого сервера
        self._tools = {k: v for k, v in self._tools.items() if v.server_name != name}

    async def connect_all(self):
        """Подключиться ко всем enabled серверам"""
        if not MCP_AVAILABLE:
            print("[MCP] SDK не установлен")
            return

        self._streams_contexts = {}
        tasks = [self.connect_server(name) for name, cfg in self.servers.items() if cfg.enabled]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        success = sum(1 for r in results if r is True)
        print(f"[MCP] Подключено {success}/{len(tasks)} серверов")

    async def disconnect_all(self):
        """Отключиться от всех"""
        for name in list(self._sessions.keys()):
            await self.disconnect_server(name)

    # ---------- Tool calls ----------
    async def call_tool(self, tool_key: str, arguments: dict) -> str:
        """
        Вызвать tool по ключу "server.tool_name".
        Возвращает текстовый результат (или JSON если не текст).
        """
        info = self._tools.get(tool_key)
        if not info:
            return f"Tool {tool_key!r} не найден. Доступные: {', '.join(self._tools.keys())}"

        session = self._sessions.get(info.server_name)
        if not session:
            return f"Сервер {info.server_name!r} не подключён"

        try:
            result = await session.call_tool(info.name, arguments)

            # result.content — список TextContent / ImageContent / EmbeddedResource
            parts = []
            for item in result.content:
                if isinstance(item, TextContent):
                    parts.append(item.text)
                elif hasattr(item, "data"):  # ImageContent
                    parts.append(f"[Image: {len(item.data)} bytes]")
                else:
                    parts.append(str(item))

            text = "\n".join(parts) if parts else "(пустой ответ)"

            # Если слишком длинный — обрезаем
            if len(text) > 5000:
                original_len = len(text)
                text = text[:5000] + f"\n\n... (обрезано, всего {original_len} символов)"

            return text

        except Exception as e:
            return f"Ошибка вызова {tool_key}: {e}"

    def list_tools(self) -> List[MCPToolInfo]:
        """Список всех доступных tools со всех серверов"""
        return list(self._tools.values())

    def tools_for_ai(self) -> List[dict]:
        """Tools в формате, который AI может использовать (как function calling)"""
        return [
            {
                "name": key.replace(".", "__"),  # server__tool для AI
                "description": f"[{info.server_name}] {info.description}",
                "input_schema": info.input_schema,
            }
            for key, info in self._tools.items()
        ]

    # ---------- Утилиты ----------
    def _expand_env(self, env: Dict[str, str]) -> Dict[str, str]:
        """Развернуть ${VAR} из текущего окружения + взять base env"""
        result = dict(os.environ)
        for k, v in env.items():
            result[k] = os.path.expandvars(v) if v else ""
        return result

    def get_status(self) -> dict:
        """Статус всех серверов для UI"""
        return {
            "available": MCP_AVAILABLE,
            "config_path": str(self.config_path),
            "servers": {
                name: {
                    "enabled": cfg.enabled,
                    "command": cfg.command,
                    "args": cfg.args,
                    "description": cfg.description,
                    "connected": name in self._sessions,
                    "tools_count": sum(1 for t in self._tools.values() if t.server_name == name),
                }
                for name, cfg in self.servers.items()
            },
            "total_tools": len(self._tools),
        }


# ---------- Синхронная обёртка для Flask ----------
_singleton: Optional[MCPClient] = None


def get_client() -> MCPClient:
    """Получить глобальный MCP клиент (лениво создаёт)"""
    global _singleton
    if _singleton is None:
        _singleton = MCPClient()
        _singleton._runner.start()
    return _singleton


def sync_call_tool(tool_key: str, arguments: dict) -> str:
    """Синхронный вызов из Flask endpoint."""
    client = get_client()
    if not client._sessions:
        return "MCP: нет подключённых серверов. Зайди в /mcp и подключи."
    return client._runner.run(client.call_tool(tool_key, arguments), timeout=60)


def sync_connect_all() -> dict:
    """Подключиться ко всем enabled серверам (синхронно)"""
    client = get_client()
    client._runner.run(client.connect_all(), timeout=120)
    return client.get_status()


def sync_disconnect_all() -> dict:
    """Отключиться от всех"""
    client = get_client()
    client._runner.run(client.disconnect_all(), timeout=30)
    return client.get_status()


# ========== CLI для тестирования ==========
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="NovaMind MCP Client")
    parser.add_argument("cmd", choices=["list", "status", "connect", "disconnect", "call"],
                       nargs="?", default="status")
    parser.add_argument("--tool", help="tool_key в формате server.tool")
    parser.add_argument("--args", help="JSON аргументы", default="{}")

    args = parser.parse_args()
    client = get_client()

    if args.cmd == "list":
        print("Доступные серверы в конфиге:")
        for name, cfg in client.servers.items():
            print(f"  {'✓' if cfg.enabled else '○'} {name}: {cfg.command} {' '.join(cfg.args)}")
    elif args.cmd == "status":
        status = client.get_status()
        print(json.dumps(status, ensure_ascii=False, indent=2))
    elif args.cmd == "connect":
        status = sync_connect_all()
        print(json.dumps(status, ensure_ascii=False, indent=2))
    elif args.cmd == "disconnect":
        status = sync_disconnect_all()
        print(json.dumps(status, ensure_ascii=False, indent=2))
    elif args.cmd == "call":
        if not args.tool:
            print("--tool обязателен")
            sys.exit(1)
        arguments = json.loads(args.args)
        print(sync_call_tool(args.tool, arguments))