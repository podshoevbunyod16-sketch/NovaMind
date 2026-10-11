"""
routes/__init__.py — Регистрация всех Blueprint'ов
"""
from .chat import chat_bp
from .commands import commands_bp
from .settings import settings_bp
from .admin import admin_bp
from .media import media_bp
from .search import search_bp
from .composio import composio_bp
from .terminal import terminal_bp, tasks_bp
from .agent import agent_bp
from .coding_agent import coding_agent_bp
from .diagnostics import diagnostics_bp

ALL_BLUEPRINTS = [
    chat_bp,
    commands_bp,
    settings_bp,
    admin_bp,
    media_bp,
    search_bp,
    composio_bp,
    terminal_bp,
    tasks_bp,
    agent_bp,
    coding_agent_bp,
    diagnostics_bp,
]
