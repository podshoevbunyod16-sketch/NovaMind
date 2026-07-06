"""
NovaMind MCP Server
Экспонирует встроенные команды NovaMind как MCP tools.
Это позволяет подключить NovaMind к Claude Desktop, Cursor, Cline и другим MCP-клиентам.
"""

import os
import sys
import json
import asyncio
import requests
from typing import Any

from mcp.server import Server
from mcp.types import Tool, TextContent
from mcp.server.stdio import stdio_server

# Добавляем commands/ в путь, чтобы можно было импортировать плагины
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "commands"))


def _load_commands():
    """Загрузить все команды NovaMind из commands/*.py"""
    import importlib.util
    import pathlib

    commands_dir = pathlib.Path(__file__).parent / "commands"
    commands = {}

    for f in commands_dir.glob("*.py"):
        if f.name.startswith("_"):
            continue
        spec = importlib.util.spec_from_file_location(f.stem, f)
        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
            if hasattr(module, "run"):
                commands[f.stem] = module.run
        except Exception as e:
            print(f"[NovaMind MCP] Ошибка загрузки {f.name}: {e}")

    return commands


# ---------- Описания tools (inputSchema в формате JSON Schema) ----------
TOOL_DEFINITIONS = {
    "image": {
        "description": "Сгенерировать изображение по текстовому описанию (через Pollinations.ai)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "Описание изображения"},
                "width": {"type": "integer", "default": 1024},
                "height": {"type": "integer", "default": 1024},
            },
            "required": ["prompt"],
        },
    },
    "services": {
        "description": "Получить погоду, курс валют или информацию из Wikipedia",
        "inputSchema": {
            "type": "object",
            "properties": {
                "service": {"type": "string", "enum": ["weather", "currency", "wiki"]},
                "query": {"type": "string", "description": "город / валюты / поисковый запрос"},
            },
            "required": ["service", "query"],
        },
    },
    "message": {
        "description": "Открыть WhatsApp или Telegram с готовым сообщением (нужен Android/Termux)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "platform": {"type": "string", "enum": ["wa", "tg"]},
                "recipient": {"type": "string", "description": "номер телефона или @username"},
                "text": {"type": "string"},
            },
            "required": ["platform", "recipient", "text"],
        },
    },
    "analyze": {
        "description": "Анализ изображения через Groq Vision (LLM)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "image_url": {"type": "string"},
                "question": {"type": "string", "default": "Что на изображении?"},
            },
            "required": ["image_url"],
        },
    },
    "voice": {
        "description": "Озвучить текст (Text-to-Speech)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "voice": {"type": "string", "default": "ru-RU"},
            },
            "required": ["text"],
        },
    },
    "downloads": {
        "description": "Скачать файл по URL",
        "inputSchema": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
    },
    "versus": {
        "description": "Сравнить две вещи через AI",
        "inputSchema": {
            "type": "object",
            "properties": {
                "a": {"type": "string"},
                "b": {"type": "string"},
            },
            "required": ["a", "b"],
        },
    },
    "composio": {
        "description": "Вызвать интеграцию Composio (GitHub, Gmail, Notion и др.)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string"},
                "params": {"type": "object"},
            },
            "required": ["action"],
        },
    },
}


def create_server() -> Server:
    """Создать MCP сервер с инструментами NovaMind"""
    server = Server("novamind")
    commands = _load_commands()

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return [
            Tool(
                name=name,
                description=meta["description"],
                inputSchema=meta["inputSchema"],
            )
            for name, meta in TOOL_DEFINITIONS.items()
            if name in commands
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[TextContent]:
        if name not in commands:
            return [TextContent(type="text", text=f"Unknown tool: {name}")]

        cmd_fn = commands[name]

        # Маппинг MCP-стиля аргументов в формат команд NovaMind
        args_list = []
        if name == "image":
            args_list = [arguments["prompt"]]
        elif name == "services":
            args_list = [arguments["service"], arguments["query"]]
        elif name == "message":
            args_list = [arguments["platform"], arguments["recipient"], arguments["text"]]
        elif name == "analyze":
            args_list = [arguments["image_url"], arguments.get("question", "")]
        elif name == "voice":
            args_list = [arguments["text"]]
        elif name == "downloads":
            args_list = [arguments["url"]]
        elif name == "versus":
            args_list = [arguments["a"], arguments["b"]]
        elif name == "composio":
            action = arguments["action"]
            params = arguments.get("params", {})
            args_list = [action, json.dumps(params, ensure_ascii=False)]

        # Запускаем (команды могут быть синхронными)
        try:
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(None, cmd_fn, args_list)
            return [TextContent(type="text", text=str(result))]
        except Exception as e:
            return [TextContent(type="text", text=f"Error: {e}")]

    return server


async def main():
    """Точка входа для запуска как MCP сервер"""
    server = create_server()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())