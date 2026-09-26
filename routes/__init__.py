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

ALL_BLUEPRINTS = [
    chat_bp,
    commands_bp,
    settings_bp,
    admin_bp,
    media_bp,
    search_bp,
    composio_bp,
]
