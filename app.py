"""
app.py — Точка входа NovaMind. Только Flask init + регистрация Blueprint'ов.
Вся логика разделена по модулям:
  config.py         — настройки, провайдеры, переменные
  groq_rotation.py  — ротация Groq API ключей
  ai_providers.py   — запросы к AI (Groq, Gemini, OpenAI-compatible)
  database.py       — SQLite история чатов
  routes/
    chat.py         — /send, /send_stream, /api/chats/*, /api/history/*
    commands.py     — /command
    settings.py     — /settings, /api/settings/*
    admin.py        — /admin/*, /api/admin/*
    media.py        — /upload_*, /api/media/*
    search.py       — /api/auto_search, /api/web_search_groq
    composio.py     — /composio, /api/composio/*
"""
import os
from flask import Flask, send_from_directory, redirect

from config import ADMIN_SESSION_KEY, load_plugins
from database import init_db
from routes import ALL_BLUEPRINTS

# ---------- Flask ----------
app = Flask(__name__, static_folder="static", template_folder="templates")
app.secret_key = ADMIN_SESSION_KEY
app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024  # 500 MB

# ---------- Регистрация Blueprint'ов ----------
for bp in ALL_BLUEPRINTS:
    app.register_blueprint(bp)

# ---------- Инициализация ----------
init_db()
load_plugins()

# ---------- Статика ----------
@app.route('/static/<path:filename>')
def static_files(filename):
    return send_from_directory('static', filename)

@app.route('/')
def index():
    return redirect('/chat')

# ========== ЗАПУСК ==========
if __name__ == '__main__':
    port = int(os.getenv("PORT", 5000))
    debug = os.getenv("FLASK_DEBUG", "false").lower() == "true"
    print(f"[NovaMind] Запуск на порту {port}, debug={debug}")
    app.run(host="0.0.0.0", port=port, debug=debug)
