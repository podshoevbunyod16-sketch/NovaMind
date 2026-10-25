"""
routes/admin.py — Административные маршруты
"""
from flask import Blueprint, request, jsonify, session, render_template, redirect
import secrets
import subprocess
import json
import os
import requests
from urllib.parse import quote
import config
import groq_rotation
import security
from groq_rotation import GROQ_KEYS, GROQ_KEY_COOLDOWN, get_groq_key_status
from config import (ADMIN_CREDENTIALS, ADMIN_CODE, ADMIN_EMAIL, ADMIN_USER,
                    PROVIDERS, GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET,
                    GOOGLE_REDIRECT_URI)

# Папка с сохранённым кодом лежит в корне проекта (а не в routes/) — как и generated_*
SAVED_CODES_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "saved_codes")

admin_bp = Blueprint("admin", __name__)

@admin_bp.route('/admin/login')
def admin_login_page():
    # Подсказываем логин администратора: если ADMIN_USER — это e-mail, люди
    # по привычке пишут «admin» и получают «Неверный логин».
    return render_template('admin.html', admin_user=ADMIN_USER,
                           admin_user_is_email='@' in ADMIN_USER)

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
        "redirect_uri": GOOGLE_REDIRECT_URI,
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
        # Владелец сервера входит своим обычным логином/почтой — сразу админ:
        # раньше здесь жёстко писалось False, и он оставался «обычным» даже
        # когда его e-mail указан в ADMIN_USER/ADMIN_EMAIL.
        session['nova_is_admin'] = security.is_admin_identity(email)
        security.grant_admin(email, via='google')

        return redirect(f'/chat?nick={quote(nick)}&email={quote(email)}')
    except Exception as e:
        return f'Ошибка авторизации: {str(e)}', 500

# ========== API АВТОРИЗАЦИИ ==========

@admin_bp.route('/api/admin/login', methods=['POST'])
def admin_login():
    """Вход в админку с защитой от перебора.

    5 неудачных попыток за 10 минут — блок отдельно по IP и по аккаунту
    (настраивается ADMIN_LOGIN_MAX_ATTEMPTS / ADMIN_LOGIN_WINDOW).
    """
    import security

    data = request.get_json() or {}
    username = (data.get('username') or 'admin').strip()
    code = data.get('code', '').strip()
    ip_key = 'login_ip:' + security.client_ip(request)
    user_key = 'login_user:' + username

    allowed, retry_after, _blocked = security.login_limiter.blocked(ip_key, user_key)
    if not allowed:
        minutes = max(1, retry_after // 60 + (1 if retry_after % 60 else 0))
        response = jsonify({
            'success': False,
            'error': f'Слишком много попыток входа. Повторите через {minutes} мин.',
            'retry_after': retry_after,
        })
        response.headers['Retry-After'] = str(retry_after)
        return response, 429

    # Логин сравниваем без учёта регистра и пробелов: «Admin», «admin@…» и
    # «Admin@…» — один и тот же человек, а не «Неверный логин».
    matched = ''
    for login_name in ADMIN_CREDENTIALS:
        if security.normalize_identity(login_name) == security.normalize_identity(username):
            matched = login_name
            break

    if not matched or not security.same_secret(ADMIN_CREDENTIALS[matched], code):
        security.login_limiter.fail(ip_key, user_key)
        if not matched:
            # Самая частая причина: ADMIN_USER — это e-mail, а форма входа
            # подписана «Логин» и в неё пишут «admin». Говорим об этом прямо,
            # не раскрывая сам код.
            hint = ADMIN_USER if "@" in ADMIN_USER else "тот, что в ADMIN_USER"
            error = f'Неверный логин. Для входа используйте логин из ADMIN_USER ({hint}).'
        else:
            error = 'Неверный код (ADMIN_CODE в .env)'
        return jsonify({'success': False, 'error': error,
                        'admin_login_is_email': '@' in ADMIN_USER})

    security.login_limiter.succeed(ip_key, user_key)
    session['admin_logged_in'] = True
    session['admin_username'] = matched
    session['admin_via'] = 'code'
    session['nova_is_admin'] = True
    return jsonify({'success': True, 'username': matched})

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
    email = str(data.get('email') or '').strip()[:180]
    if email:
        session['nova_user_email'] = email
    # Вошёл по нику, но ник/почта совпали с администратором — он админ.
    granted = security.grant_admin(nick, via='nick')
    if email and not granted:
        granted = security.grant_admin(email, via='nick')
    return jsonify({'success': True, 'nick': nick, 'admin': granted or security.is_admin()})


@admin_bp.route('/api/session/check')
def session_check():
    """Кто вошёл — интерфейсу нужно знать, открывать ли рабочую папку."""
    data = {
        'signed_in': bool(session.get('admin_logged_in') or session.get('nova_google_login')
                          or session.get('nova_signed_in') or session.get('nova_user_nick')
                          or session.get('username')),
        'nick': session.get('admin_username') or session.get('nova_user_nick') or '',
        'email': session.get('nova_user_email') or '',
    }
    data.update(security.admin_state())
    return jsonify(data)


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
    if security.is_admin():
        return jsonify({'logged_in': True,
                        'username': session.get('admin_username')
                        or session.get('nova_user_email')
                        or session.get('nova_user_nick') or 'admin'})
    return jsonify({'logged_in': False, 'admin_login_is_email': '@' in ADMIN_USER})

# ========== ЧАТ ==========# ========== АДМИН API ==========

@admin_bp.route('/api/admin/access')
def admin_access():
    """Почему та или иная функция недоступна — короткий честный разбор.

    Вопрос «я админ, но нет настроек и MCP» почти всегда про одно из четырёх:
    вход выполнен не тем логином; панель выключена настройкой; MCP-серверы не
    подключены; песочница/агент выключены в .env. Здесь это видно по пунктам.
    """
    import security
    import mcp_registry
    from routes import agent as agent_routes
    from routes import terminal as terminal_routes

    if not security.is_admin():
        # Логин администратора подсказываем только вошедшему человеку.
        signed = bool(security.session_identities())
        return jsonify({"admin": False, "error": "Только администратор",
                        "admin_login": ADMIN_USER if signed else ""}), 403
    try:
        mcp_servers = len(mcp_registry.connected_tools() or [])
    except Exception:  # noqa: BLE001 — диагностика не должна падать
        mcp_servers = 0
    provider, model, _ = __import__("ai_providers").resolve_target()
    try:
        provider_ready = bool(config.has_credentials(provider)) or provider == "local_demo"
    except Exception:  # noqa: BLE001
        provider_ready = False
    return jsonify({
        "admin": True,
        "signed_in_as": security.session_identities(),
        "admin_login": ADMIN_USER,
        "settings_panel": "доступна",
        "mcp": {"servers_connected": mcp_servers,
                "hint": "Подключите сервер в Настройках → MCP" if not mcp_servers else ""},
        "sandbox": {"enabled": terminal_routes.is_enabled(),
                    "hint": "" if terminal_routes.is_enabled()
                            else "TERMINAL_ENABLED=1 в .env и перезапуск"},
        "agent": {"enabled": bool(agent_routes.is_enabled()),
                  "hint": "" if agent_routes.is_enabled()
                          else "AGENT_ENABLED=1 в .env и перезапуск"},
        "provider": {"id": provider, "model": model, "configured": provider_ready},
    })


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

