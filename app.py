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
import logging
from flask import Flask, send_from_directory, redirect

from config import ADMIN_SESSION_KEY, ADMIN_CODE, ADMIN_CODE_IS_DEFAULT, load_plugins, PORT
from database import init_db
from routes import ALL_BLUEPRINTS

# ---------- Flask ----------
app = Flask(__name__, static_folder="static", template_folder="templates")
app.secret_key = ADMIN_SESSION_KEY
from datetime import timedelta as _timedelta  # noqa: E402
app.permanent_session_lifetime = _timedelta(days=365)   # id браузера - основа изоляции данных
app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024  # 500 MB

# ---------- Регистрация Blueprint'ов ----------
for bp in ALL_BLUEPRINTS:
    app.register_blueprint(bp)

# ---------- Инициализация ----------
init_db()
load_plugins()

# ---------- Планировщик (Б4) ----------
# В тестах и при SCHEDULER_ENABLED=0 поток не поднимается: он не должен
# влиять ни на прогон pytest, ни на отладку.
def _start_scheduler():
    import sys
    if os.getenv("SCHEDULER_ENABLED", "1") != "1":
        return False
    if "pytest" in sys.modules or os.getenv("PYTEST_CURRENT_TEST"):
        return False
    try:
        import scheduler
        return scheduler.start(app)
    except Exception:
        logging.getLogger(__name__).warning("Планировщик не запустился", exc_info=True)
        return False


_start_scheduler()

# ---------- Идентичность браузера ----------
@app.before_request
def _ensure_browser_identity():
    """Закрепляем browser id на первом же запросе.

    Раньше ``nova_sid`` создавался лениво внутри конкретного запроса: соседние
    запросы одной и той же freshly-opened вкладки могли получить разные id, а
    значит разные личные рабочие папки (файл записан в одну, читается из другой).
    """
    import identity
    identity.browser_id()


# ---------- Статика ----------
@app.route('/static/<path:filename>')
def static_files(filename):
    return send_from_directory('static', filename)

@app.route('/')
def index():
    return redirect('/chat')

# ========== ЗАПУСК ==========
if __name__ == '__main__':
    port = PORT
    debug = os.getenv("FLASK_DEBUG", "false").lower() == "true"
    logging.getLogger(__name__).info("Запуск NovaMind на порту %s, debug=%s", port, debug)
    if ADMIN_CODE_IS_DEFAULT:
        logging.getLogger(__name__).warning(
            "ADMIN_CODE не задан — сгенерирован одноразовый код администратора: %s "
            "(задайте ADMIN_CODE в .env, чтобы код был постоянным)", ADMIN_CODE)
    app.run(host="0.0.0.0", port=port, debug=debug)
