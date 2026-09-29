"""
routes/admin.py — Административные маршруты
"""
from flask import Blueprint, request, jsonify, session, render_template, redirect
import subprocess
import json
import os
import requests
from urllib.parse import quote
import config
import groq_rotation
from groq_rotation import GROQ_KEYS, GROQ_KEY_COOLDOWN, get_groq_key_status
from config import ADMIN_CREDENTIALS, PROVIDERS, GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET

# Папка с сохранённым кодом лежит в корне проекта (а не в routes/) — как и generated_*
SAVED_CODES_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "saved_codes")

admin_bp = Blueprint("admin", __name__)

@admin_bp.route('/admin/login')
def admin_login_page():
    return render_template('admin.html')

# ========== GOOGLE OAUTH ==========

@admin_bp.route('/auth/google/callback')
def auth_google_callback():
    code = request.args.get('code')
    error = request.args.get('error')

    if error:
        return f'Ошибка авторизации: {error}', 400
    if not code:
        return 'Не получен код авторизации', 400

    token_url = "https://oauth2.googleapis.com/token"
    token_data = {
        "code": code,
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "redirect_uri": "http://localhost:5000/auth/google/callback",
        "grant_type": "authorization_code"
    }

    try:
        token_resp = requests.post(token_url, data=token_data, timeout=10)
        token_resp.raise_for_status()
        token_json = token_resp.json()

        id_token_jwt = token_json.get("id_token")
        if not id_token_jwt:
            return 'Не получен ID токен', 400

        import base64 as b64
        payload = id_token_jwt.split('.')[1]
        payload += '=' * (4 - len(payload) % 4)
        user_info = json.loads(b64.urlsafe_b64decode(payload).decode('utf-8'))

        nick = user_info.get('name', user_info.get('email', 'User').split('@')[0])
        email = user_info.get('email', '')
        picture = user_info.get('picture', '')

        session['nova_user_nick'] = nick
        session['nova_user_email'] = email
        session['nova_user_avatar'] = picture
        session['nova_google_login'] = True
        session['nova_is_admin'] = False

        return redirect(f'/chat?nick={quote(nick)}&email={quote(email)}')
    except Exception as e:
        return f'Ошибка авторизации: {str(e)}', 500

# ========== API АВТОРИЗАЦИИ ==========

@admin_bp.route('/api/admin/login', methods=['POST'])
def admin_login():
    data = request.get_json() or {}
    username = data.get('username', 'admin').strip()
    code = data.get('code', '').strip()

    if username not in ADMIN_CREDENTIALS:
        return jsonify({'success': False, 'error': 'Неверный логин'})
    if ADMIN_CREDENTIALS[username] != code:
        return jsonify({'success': False, 'error': 'Неверный код'})

    session['admin_logged_in'] = True
    session['admin_username'] = username
    return jsonify({'success': True, 'username': username})

@admin_bp.route('/api/session/login', methods=['POST'])
def session_login():
    """Серверный вход по нику.

    Google кладёт флаги в сессию сам (см. выше), а быстрый вход по нику живёт
    только в localStorage — без этого шага сервер не знает, что человек вошёл,
    и не пускает его в рабочую папку и агентный режим.
    """
    data = request.get_json(silent=True) or {}
    nick = (data.get('nick') or data.get('username') or '').strip()[:64]
    if not nick:
        return jsonify({'success': False, 'error': 'Пустой ник'}), 400
    session['nova_user_nick'] = nick
    session['nova_signed_in'] = True
    return jsonify({'success': True, 'nick': nick})


@admin_bp.route('/api/session/check')
def session_check():
    """Кто вошёл — интерфейсу нужно знать, открывать ли рабочую папку."""
    return jsonify({
        'signed_in': bool(session.get('admin_logged_in') or session.get('nova_google_login')
                          or session.get('nova_signed_in') or session.get('nova_user_nick')
                          or session.get('username')),
        'nick': session.get('admin_username') or session.get('nova_user_nick') or '',
        'admin': bool(session.get('admin_logged_in')),
    })


@admin_bp.route('/api/session/logout', methods=['POST'])
def session_logout():
    for key in ('nova_user_nick', 'nova_signed_in'):
        session.pop(key, None)
    return jsonify({'success': True})


@admin_bp.route('/api/admin/logout', methods=['POST'])
def admin_logout():
    session.clear()
    return jsonify({'success': True})

@admin_bp.route('/api/admin/check')
def admin_check():
    if session.get('admin_logged_in'):
        return jsonify({'logged_in': True, 'username': session.get('admin_username', 'admin')})
    return jsonify({'logged_in': False})

# ========== ЧАТ ==========# ========== АДМИН API ==========

@admin_bp.route('/api/admin/stats')
def admin_stats():
    from routes import chat as chat_routes
    return jsonify({
        "models": PROVIDERS,
        "current_provider": config.current_provider,
        "current_model": config.current_model,
        "history_messages": len(chat_routes.contents),
        "plugins_loaded": list(config.plugins.keys()),
        "custom_commands": list(config.CUSTOM_ALIASES.keys()),
        "system_prompt": config.system_prompt,
        "voice_enabled": os.environ.get("ASSISTANT_VOICE_REPLY", "0") == "1"
    })

@admin_bp.route('/api/admin/settings', methods=['POST'])
def admin_settings():
    data = request.get_json() or {}
    # Настройки живут в config — их читают ai_providers и чат; локальные копии ничего не меняли
    if "system_prompt" in data:
        config.system_prompt = data["system_prompt"]
    if "provider" in data and data["provider"] in PROVIDERS:
        config.current_provider = data["provider"]
    if "model" in data:
        for key, pdata in PROVIDERS.items():
            for m in pdata["models"]:
                if m["id"] == data["model"]:
                    config.current_provider = key
                    config.current_model = data["model"]
                    break
    return jsonify({"success": True})

@admin_bp.route('/api/admin/save_code', methods=['POST'])
def admin_save_code():
    data = request.get_json() or {}
    filename = os.path.basename(str(data.get("filename", "script.py"))) or "script.py"
    code = data.get("code", "")
    if not code:
        return jsonify({"error": "Нет кода"}), 400
    code_dir = SAVED_CODES_DIR
    os.makedirs(code_dir, exist_ok=True)
    filepath = os.path.join(code_dir, filename)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(code)
    return jsonify({"success": True, "filepath": filepath})

@admin_bp.route('/api/admin/run_saved_code', methods=['POST'])
def admin_run_saved_code():
    data = request.get_json() or {}
    filename = os.path.basename(str(data.get("filename", "script.py"))) or "script.py"
    code_dir = SAVED_CODES_DIR
    filepath = os.path.join(code_dir, filename)
    if not os.path.exists(filepath):
        return jsonify({"error": "Файл не найден"}), 404
    try:
        result = subprocess.run(["python3", filepath], capture_output=True, text=True, timeout=10)
        output = result.stdout
        if result.stderr:
            output += "\nSTDERR: " + result.stderr
        return jsonify({"result": output or "Нет вывода"})
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Превышено время (10 сек)"}), 408
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@admin_bp.route('/api/admin/saved_codes')
def admin_saved_codes():
    code_dir = SAVED_CODES_DIR
    if not os.path.exists(code_dir):
        return jsonify({"files": []})
    files = sorted([f for f in os.listdir(code_dir) if f.endswith(".py")])
    return jsonify({"files": files})

@admin_bp.route('/api/admin/load_code')
def admin_load_code():
    filename = os.path.basename(request.args.get("file", ""))
    code_dir = SAVED_CODES_DIR
    filepath = os.path.join(code_dir, filename)
    if not os.path.exists(filepath):
        return jsonify({"error": "Файл не найден"}), 404
    with open(filepath, "r", encoding="utf-8") as f:
        code = f.read()
    return jsonify({"code": code, "filename": filename})


# ========== СТАТУС КЛЮЧЕЙ GROQ (админка) ==========

@admin_bp.route('/api/admin/groq_keys')
def admin_groq_keys():
    """Статус всех Groq API ключей"""
    return jsonify({
        "keys": get_groq_key_status(),
        "current_key_index": groq_rotation.groq_key_index,
        "total_keys": len(GROQ_KEYS),
        "cooldown_hours": GROQ_KEY_COOLDOWN / 3600
    })

@admin_bp.route('/api/admin/groq_keys/reset', methods=['POST'])
def admin_reset_groq_keys():
    """Сбросить все cooldown'ы (для админа)"""
    with groq_rotation._groq_lock:
        for key_info in GROQ_KEYS:
            key_info["exhausted_at"] = None
        groq_rotation.groq_key_index = 0
    return jsonify({"success": True, "message": "Все ключи сброшены"})

