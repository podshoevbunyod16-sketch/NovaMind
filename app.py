import os
import sys
import json
import requests
import subprocess
from datetime import datetime
from flask import Flask, request, jsonify, render_template, send_file, session, send_from_directory

# ---------- Загрузка .env ----------
env_path = os.path.join(os.path.dirname(__file__), ".env")
if os.path.exists(env_path):
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                os.environ[k.strip()] = v.strip()

# ---------- Google OAuth ----------
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")

# ---------- Провайдеры ----------
PROVIDERS = {
    "groq": {
        "url": "https://api.groq.com/openai/v1/chat/completions",
        "headers": {
            "Authorization": f"Bearer {os.getenv('GROQ_API_KEY')}",
            "Content-Type": "application/json"
        },
        "models": [
            {"id": "openai/gpt-oss-120b", "name": "GPT-OSS 120B"},
            {"id": "llama-3.3-70b-versatile", "name": "Llama 3.3 70B"},
            {"id": "llama-3.1-8b-instant", "name": "Llama 3.1 8B"},
        ]
    },
    "cerebras": {
        "url": "https://api.cerebras.ai/v1/chat/completions",
        "headers": {
            "Authorization": f"Bearer {os.getenv('CEREBRAS_API_KEY')}",
            "Content-Type": "application/json"
        },
        "models": [
            {"id": "llama-3.3-70b", "name": "Llama 3.3 70B"},
            {"id": "llama-3.1-8b", "name": "Llama 3.1 8B"},
        ]
    },
    "openrouter": {
        "url": "https://openrouter.ai/api/v1/chat/completions",
        "headers": {
            "Authorization": f"Bearer {os.getenv('OPENROUTER_API_KEY')}",
            "Content-Type": "application/json",
            "HTTP-Referer": "http://localhost:5000",
            "X-Title": "NovaMind AI"
        },
        "models": [
            {"id": "google/gemini-2.0-flash-001", "name": "Gemini 2.0 Flash (Free)"},
            {"id": "deepseek/deepseek-chat-v3-0324", "name": "DeepSeek R1 (Free)"},
        ]
    }
}

current_provider = "groq"
current_model = "openai/gpt-oss-120b"

system_prompt = "Ты NovaMind — мощный AI-ассистент. Отвечай на русском языке, чётко и по делу."
contents = []

# ---------- Админ ----------
ADMIN_CREDENTIALS = {
    "admin": os.getenv("ADMIN_CODE", "007"),
}
ADMIN_SESSION_KEY = os.getenv("SESSION_SECRET", "nova-secret-key")

# ---------- Плагины ----------
plugins = {}
def load_plugins():
    plugin_dir = os.path.join(os.path.dirname(__file__), "commands")
    if not os.path.isdir(plugin_dir):
        return
    sys.path.insert(0, plugin_dir)
    for fname in os.listdir(plugin_dir):
        if fname.endswith(".py") and not fname.startswith("_"):
            modname = fname[:-3]
            try:
                mod = __import__(modname)
                if hasattr(mod, "run"):
                    plugins[modname] = mod.run
                    print(f"Плагин загружен: {modname}")
            except Exception as e:
                print(f"Ошибка загрузки {modname}: {e}")

load_plugins()

# ---------- Flask ----------
app = Flask(__name__)
app.secret_key = ADMIN_SESSION_KEY

# ---------- Поиск DuckDuckGo ----------
def search_web(query):
    try:
        url = "https://api.duckduckgo.com/"
        params = {"q": query, "format": "json", "no_html": 1, "skip_disambig": 1}
        resp = requests.get(url, params=params, timeout=10)
        data = resp.json()
        parts = []
        if data.get("AbstractText"):
            parts.append(data["AbstractText"])
        for topic in data.get("RelatedTopics", [])[:5]:
            if "Text" in topic:
                parts.append(topic["Text"])
        if not parts:
            return None
        return "Результаты поиска:\n" + "\n".join(f"- {p}" for p in parts)
    except:
        return None

# ========== СТРАНИЦЫ ==========

@app.route('/')
def index():
    """Страница входа с Google OAuth"""
    return render_template('auth.html', google_client_id=GOOGLE_CLIENT_ID)

@app.route('/chat')
def chat_page():
    """Основной интерфейс чата"""
    return render_template('index.html')

@app.route('/admin/login')
def admin_login_page():
    """Админ-панель"""
    return render_template('admin.html')

# ========== API АВТОРИЗАЦИИ ==========

@app.route('/api/admin/login', methods=['POST'])
def admin_login():
    """Проверка кода администратора"""
    data = request.get_json() or {}
    username = data.get('username', 'admin').strip()
    code = data.get('code', '').strip()

    if username not in ADMIN_CREDENTIALS:
        return jsonify({'success': False, 'error': 'Неверный логин'})

    if ADMIN_CREDENTIALS[username] != code:
        return jsonify({'success': False, 'error': 'Неверный код'})

    session['admin_logged_in'] = True
    return jsonify({'success': True, 'username': username})

@app.route('/api/admin/logout', methods=['POST'])
def admin_logout():
    """Выход из админ-панели"""
    session.clear()
    return jsonify({'success': True})

@app.route('/api/admin/check')
def admin_check():
    """Проверка авторизации админа"""
    if session.get('admin_logged_in'):
        return jsonify({'logged_in': True, 'username': session.get('admin_username', 'admin')})
    return jsonify({'logged_in': False})

# ========== ЧАТ ==========

@app.route('/send', methods=['POST'])
def send():
    """Отправка сообщения к ИИ"""
    global contents
    data = request.get_json()
    message = data.get('message', '').strip()
    if not message:
        return jsonify({'error': 'Пустое сообщение'})

    contents.append({"role": "user", "content": message})
    provider = PROVIDERS[current_provider]

    payload = {
        "model": current_model,
        "messages": [{"role": "system", "content": system_prompt}] + contents,
        "temperature": 0.7,
        "max_tokens": 3000,
    }

    try:
        resp = requests.post(provider["url"], json=payload, headers=provider["headers"], timeout=60)
        resp.raise_for_status()
        data = resp.json()
        reply = data["choices"][0]["message"]["content"]
        contents.append({"role": "assistant", "content": reply})
        if len(contents) > 20:
            contents = contents[-20:]
        return jsonify({'reply': reply})
    except Exception as e:
        if contents and contents[-1]["role"] == "user":
            contents.pop()
        return jsonify({'error': str(e)})

# ========== КОМАНДЫ ==========

@app.route('/command', methods=['POST'])
def handle_command():
    """Обработка команд (/search, /code, /image, плагины)"""
    global contents
    data = request.get_json()
    cmd_line = data.get('command', '').strip()
    if not cmd_line.startswith('/'):
        return jsonify({'error': 'Команда должна начинаться с /'})

    parts = cmd_line[1:].split(maxsplit=1)
    cmd = parts[0].lower()
    args = parts[1].split() if len(parts) > 1 else []

    # Поиск в интернете
    if cmd == "search":
        query = " ".join(args)
        if not query:
            return jsonify({'error': 'Укажите запрос'})
        results = search_web(query)
        return jsonify({'result': results or 'Ничего не найдено'})

    # Плагины
    if cmd in plugins:
        try:
            result = plugins[cmd](args)
            return jsonify({'result': result if result else 'OK'})
        except Exception as e:
            return jsonify({'error': str(e)})

    # Встроенные команды
    if cmd == "clear":
        contents.clear()
        return jsonify({'result': 'История очищена'})

    if cmd == "history":
        if not contents:
            return jsonify({'result': 'История пуста'})
        hist = "\n\n".join([f"**{msg['role']}**: {msg['content']}" for msg in contents])
        return jsonify({'result': hist})

    if cmd == "code":
        query = " ".join(args)
        if not query:
            return jsonify({'error': 'Укажите, какой код создать'})
        
        provider = PROVIDERS[current_provider]
        payload = {
            "model": current_model,
            "messages": [
                {"role": "system", "content": "Ты программист. Пиши чистый код с комментариями."},
                {"role": "user", "content": f"Напиши код: {query}"}
            ],
            "temperature": 0.3,
            "max_tokens": 3000,
        }
        try:
            resp = requests.post(provider["url"], json=payload, headers=provider["headers"], timeout=60)
            resp.raise_for_status()
            data = resp.json()
            reply = data["choices"][0]["message"]["content"]
            return jsonify({'result': reply})
        except Exception as e:
            return jsonify({'error': str(e)})

    return jsonify({'error': f'Неизвестная команда: /{cmd}'})

# ========== МОДЕЛИ ==========

@app.route('/models_list')
def models_list():
    """Список доступных моделей"""
    providers_list = []
    for key, data in PROVIDERS.items():
        providers_list.append({"provider": key, "list": data["models"]})
    return jsonify({"models": providers_list, "current": current_model})

@app.route('/switch_model')
def switch_model():
    """Переключение модели"""
    global current_provider, current_model
    model_id = request.args.get('model_id', '')
    for key, data in PROVIDERS.items():
        for m in data["models"]:
            if m["id"] == model_id:
                current_provider = key
                current_model = model_id
                return jsonify({"success": True, "provider": key})
    return jsonify({'error': 'Модель не найдена'}), 400

# ========== АДМИН API ==========

@app.route('/api/admin/stats')
def admin_stats():
    """Статистика для админ-панели"""
    return jsonify({
        "models": PROVIDERS,
        "current_provider": current_provider,
        "current_model": current_model,
        "history_messages": len(contents),
        "plugins_loaded": list(plugins.keys()),
        "system_prompt": system_prompt
    })

@app.route('/api/admin/settings', methods=['POST'])
def admin_settings():
    """Сохранение настроек"""
    global system_prompt, current_provider, current_model
    data = request.get_json() or {}
    if "system_prompt" in data:
        system_prompt = data["system_prompt"]
    if "provider" in data and data["provider"] in PROVIDERS:
        current_provider = data["provider"]
    if "model" in data:
        for key, pdata in PROVIDERS.items():
            for m in pdata["models"]:
                if m["id"] == data["model"]:
                    current_provider = key
                    current_model = data["model"]
                    break
    return jsonify({"success": True})

@app.route('/api/admin/save_code', methods=['POST'])
def admin_save_code():
    """Сохранение кода из админ-панели"""
    data = request.get_json() or {}
    filename = data.get("filename", "script.py")
    code = data.get("code", "")
    if not code:
        return jsonify({"error": "Нет кода"}), 400
    code_dir = os.path.join(os.path.dirname(__file__), "saved_codes")
    os.makedirs(code_dir, exist_ok=True)
    filepath = os.path.join(code_dir, filename)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(code)
    return jsonify({"success": True, "filepath": filepath})

@app.route('/api/admin/run_saved_code', methods=['POST'])
def admin_run_saved_code():
    """Запуск сохранённого кода"""
    data = request.get_json() or {}
    filename = data.get("filename", "script.py")
    code_dir = os.path.join(os.path.dirname(__file__), "saved_codes")
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

@app.route('/api/admin/saved_codes')
def admin_saved_codes():
    """Список сохранённых файлов"""
    code_dir = os.path.join(os.path.dirname(__file__), "saved_codes")
    if not os.path.exists(code_dir):
        return jsonify({"files": []})
    files = sorted([f for f in os.listdir(code_dir) if f.endswith(".py")])
    return jsonify({"files": files})

@app.route('/api/admin/load_code')
def admin_load_code():
    """Загрузка кода из файла"""
    filename = request.args.get("file", "")
    code_dir = os.path.join(os.path.dirname(__file__), "saved_codes")
    filepath = os.path.join(code_dir, filename)
    if not os.path.exists(filepath):
        return jsonify({"error": "Файл не найден"}), 404
    with open(filepath, "r", encoding="utf-8") as f:
        code = f.read()
    return jsonify({"code": code, "filename": filename})

# ========== СТАТИКА ==========

@app.route('/static/<path:filename>')
def static_files(filename):
    return send_from_directory('static', filename)
@app.route('/auth/google/callback')
def auth_google_callback():
    """Обработка ответа от Google OAuth"""
    code = request.args.get('code')
    error = request.args.get('error')
    
    if error:
        return f'Ошибка авторизации: {error}', 400
    
    if not code:
        return 'Не получен код авторизации', 400
    
    # Обмениваем code на токен
    token_url = "https://oauth2.googleapis.com/token"
    token_data = {
        "code": code,
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": os.getenv("GOOGLE_CLIENT_SECRET", ""),
        "redirect_uri": "http://localhost:5000/auth/google/callback",
        "grant_type": "authorization_code"
    }
    
    try:
        # Получаем токен
        token_resp = requests.post(token_url, data=token_data, timeout=10)
        token_resp.raise_for_status()
        token_json = token_resp.json()
        
        id_token_jwt = token_json.get("id_token")
        if not id_token_jwt:
            return 'Не получен ID токен', 400
        
        # Декодируем JWT
        payload = id_token_jwt.split('.')[1]
        # Добавляем padding
        payload += '=' * (4 - len(payload) % 4)
        import base64
        user_info = json.loads(base64.urlsafe_b64decode(payload).decode('utf-8'))
        
        nick = user_info.get('name', user_info.get('email', 'User').split('@')[0])
        email = user_info.get('email', '')
        picture = user_info.get('picture', '')
        
        # Сохраняем в сессии (или возвращаем на фронтенд)
        session['nova_user_nick'] = nick
        session['nova_user_email'] = email
        session['nova_user_avatar'] = picture
        session['nova_google_login'] = True
        session['nova_is_admin'] = False
        
        # Перенаправляем в чат с параметрами
        return redirect(f'/chat?nick={nick}&email={email}')
        
    except Exception as e:
        return f'Ошибка авторизации: {str(e)}', 500
# ========== ЗАПУСК ==========

if __name__ == '__main__':
    print("=" * 50)
    print("NovaMind AI Assistant запущен")
    print("http://localhost:5000")
    print("=" * 50)
    app.run(host='0.0.0.0', port=5000)
