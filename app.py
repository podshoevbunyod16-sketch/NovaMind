import os
import sys
import json
import requests
import subprocess
import time
from datetime import datetime
import hashlib
from flask import Flask, request, jsonify, render_template, send_file, session, send_from_directory, redirect, url_for

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
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")

# ============================================================
# ========== SUPABASE: ПОЛЬЗОВАТЕЛЬСКИЕ ИСТОРИИ ЧАТОВ =======
# ============================================================

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")

# Глобальная переменная для неавторизованных (временная сессия)
guest_contents = []

def supabase_request(method, endpoint, payload=None, headers_extra=None):
    """Универсальный HTTP-запрос к Supabase REST API"""
    if not SUPABASE_URL or not SUPABASE_KEY:
        return None, "SUPABASE_URL или SUPABASE_KEY не заданы в .env"

    url = f"{SUPABASE_URL}/rest/v1/{endpoint}"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation"
    }
    if headers_extra:
        headers.update(headers_extra)

    try:
        if method == "GET":
            resp = requests.get(url, headers=headers, params=payload, timeout=15)
        elif method == "POST":
            resp = requests.post(url, headers=headers, json=payload, timeout=15)
        elif method == "PATCH":
            resp = requests.patch(url, headers=headers, json=payload, timeout=15)
        elif method == "DELETE":
            resp = requests.delete(url, headers=headers, params=payload, timeout=15)
        else:
            return None, f"Неизвестный метод: {method}"

        if resp.status_code in [200, 201, 204]:
            if resp.text:
                return resp.json(), None
            return [], None
        return None, f"Supabase {resp.status_code}: {resp.text[:200]}"
    except Exception as e:
        return None, str(e)

def get_user_email():
    """Возвращает email текущего пользователя или None"""
    return session.get('nova_user_email')

def load_user_history(email=None):
    """Загружает историю чата пользователя из Supabase"""
    user = email or get_user_email()
    if not user:
        return []

    # Ищем запись пользователя
    result, error = supabase_request(
        "GET", 
        "chat_histories",
        {"email": f"eq.{user}", "select": "history"}
    )
    if error:
        print(f"[Supabase] Ошибка загрузки истории: {error}")
        return []

    if result and len(result) > 0 and result[0].get("history"):
        try:
            return json.loads(result[0]["history"])
        except:
            return []
    return []

def save_user_history(history, email=None):
    """Сохраняет историю чата пользователя в Supabase"""
    user = email or get_user_email()
    if not user:
        return False

    history_json = json.dumps(history, ensure_ascii=False)

    # Проверяем, есть ли уже запись
    result, error = supabase_request(
        "GET",
        "chat_histories",
        {"email": f"eq.{user}", "select": "id"}
    )
    if error:
        print(f"[Supabase] Ошибка проверки записи: {error}")
        return False

    if result and len(result) > 0:
        # Обновляем существующую запись
        _, error = supabase_request(
            "PATCH",
            f"chat_histories?email=eq.{user}",
            {"history": history_json, "updated_at": datetime.utcnow().isoformat()}
        )
    else:
        # Создаем новую запись
        _, error = supabase_request(
            "POST",
            "chat_histories",
            {
                "email": user,
                "history": history_json,
                "created_at": datetime.utcnow().isoformat(),
                "updated_at": datetime.utcnow().isoformat()
            }
        )

    if error:
        print(f"[Supabase] Ошибка сохранения истории: {error}")
        return False
    return True

def get_current_contents():
    """Возвращает текущую историю (пользователя или гостя)"""
    user_email = get_user_email()
    if user_email:
        return load_user_history(user_email)
    return guest_contents

def set_current_contents(new_contents):
    """Сохраняет текущую историю (пользователя или гостя)"""
    global guest_contents
    user_email = get_user_email()
    if user_email:
        save_user_history(new_contents, user_email)
    else:
        guest_contents = new_contents

def append_to_history(role, content):
    """Добавляет сообщение в историю и сохраняет"""
    history = get_current_contents()
    history.append({"role": role, "content": content})
    if len(history) > 50:
        history = history[-50:]
    set_current_contents(history)

def clear_history():
    """Очищает историю текущего пользователя"""
    user_email = get_user_email()
    if user_email:
        # Удаляем запись из Supabase
        supabase_request(
            "DELETE",
            f"chat_histories?email=eq.{user_email}"
        )
    else:
        global guest_contents
        guest_contents = []

# ============================================================
# ========== GROQ API KEY ROTATION SYSTEM ====================
# ============================================================

# --- Загрузка всех Groq ключей ---
GROQ_KEYS = []
for i in range(1, 10):
    k = os.getenv(f"GROQ_API_KEY_{i}", "")
    if k:
        GROQ_KEYS.append({"key": k, "index": i, "exhausted_at": None})

if not GROQ_KEYS:
    main_key = os.getenv("GROQ_API_KEY", "")
    if main_key:
        GROQ_KEYS.append({"key": main_key, "index": 0, "exhausted_at": None})

groq_key_index = 0
GROQ_KEY_COOLDOWN = 90000

def get_groq_key():
    global groq_key_index, GROQ_KEYS
    if not GROQ_KEYS:
        return ""
    now = time.time()
    for key_info in GROQ_KEYS:
        if key_info["exhausted_at"] and (now - key_info["exhausted_at"]) >= GROQ_KEY_COOLDOWN:
            key_info["exhausted_at"] = None
            print(f"[Groq Key] Ключ #{key_info['index']} восстановлен")
    for i in range(len(GROQ_KEYS)):
        idx = (groq_key_index + i) % len(GROQ_KEYS)
        if GROQ_KEYS[idx]["exhausted_at"] is None:
            groq_key_index = idx
            return GROQ_KEYS[idx]["key"]
    return GROQ_KEYS[groq_key_index]["key"]

def mark_groq_key_exhausted():
    global groq_key_index, GROQ_KEYS
    if not GROQ_KEYS:
        return
    current_key = GROQ_KEYS[groq_key_index]
    current_key["exhausted_at"] = time.time()
    print(f"[Groq Key] Ключ #{current_key['index']} исчерпан")
    found = False
    for i in range(1, len(GROQ_KEYS)):
        idx = (groq_key_index + i) % len(GROQ_KEYS)
        if GROQ_KEYS[idx]["exhausted_at"] is None:
            groq_key_index = idx
            found = True
            print(f"[Groq Key] Переключение на ключ #{GROQ_KEYS[idx]['index']}")
            break
    if not found:
        print("[Groq Key] ВСЕ ключи исчерпаны!")

def get_groq_key_status():
    now = time.time()
    status = []
    for key_info in GROQ_KEYS:
        if key_info["exhausted_at"] is None:
            status.append({
                "index": key_info["index"],
                "active": (GROQ_KEYS.index(key_info) == groq_key_index),
                "status": "active"
            })
        else:
            remaining = max(0, GROQ_KEY_COOLDOWN - (now - key_info["exhausted_at"]))
            status.append({
                "index": key_info["index"],
                "active": False,
                "status": "cooldown",
                "cooldown_remaining_sec": int(remaining),
                "cooldown_remaining_hr": round(remaining / 3600, 2)
            })
    return status

def groq_request_with_rotation(url, payload, headers, timeout=90, max_retries=3):
    for attempt in range(max_retries):
        current_key = get_groq_key()
        if not current_key:
            return None, "Нет доступных Groq API ключей"
        headers["Authorization"] = f"Bearer {current_key}"
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=timeout)
            if resp.status_code == 429:
                error_text = resp.text.lower()
                if "rate limit" in error_text or "quota" in error_text or "exceeded" in error_text:
                    mark_groq_key_exhausted()
                    continue
                else:
                    time.sleep(2 ** attempt)
                    continue
            if resp.status_code == 401:
                mark_groq_key_exhausted()
                continue
            resp.raise_for_status()
            return resp.json(), None
        except requests.exceptions.Timeout:
            if attempt < max_retries - 1:
                time.sleep(1)
                continue
            return None, "Таймаут запроса к Groq"
        except requests.exceptions.RequestException as e:
            error_str = str(e).lower()
            if "rate limit" in error_str or "quota" in error_str or "429" in error_str:
                mark_groq_key_exhausted()
                continue
            if attempt < max_retries - 1:
                time.sleep(1)
                continue
            return None, str(e)
    return None, "Все Groq ключи исчерпаны или недоступны"

# ============================================================
# ========== ПРОВАЙДЕРЫ =====================================
# ============================================================

PROVIDERS = {
    "groq": {
        "url": "https://api.groq.com/openai/v1/chat/completions",
        "headers": {"Content-Type": "application/json"},
        "models": [
            {"id": "openai/gpt-oss-120b", "name": "GPT-OSS 120B"},
            {"id": "llama-3.3-70b-versatile", "name": "Llama 3.3 70B"},
            {"id": "llama-3.1-8b-instant", "name": "Llama 3.1 8B"},
        ]
    },
    "cerebras": {
        "url": "https://api.cerebras.ai/v1/chat/completions",
        "headers": {
            "Authorization": f"Bearer {os.getenv('CEREBRAS_API_KEY', '')}",
            "Content-Type": "application/json"
        },
        "models": [
            {"id": "qwen-3-235b-a22b-instruct-2507", "name": "Qwen 3 235B"},
            {"id": "zai-glm-4.7", "name": "Z.ai GLM 4.7"},
            {"id": "deepseek-r1-distill-llama-70b", "name": "DeepSeek R1 Distill Llama 70B"}
        ]
    },
    "openrouter": {
        "url": "https://openrouter.ai/api/v1/chat/completions",
        "headers": {
            "Authorization": f"Bearer {os.getenv('OPENROUTER_API_KEY', '')}",
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

system_prompt = """ Ты - продвинутый AI ассистент NovaMind, ориентированный на практическую пользу.

Твоя цель:
- Давать максимально полезные, конкретные и применимые ответы
- Минимизировать воду и общие фразы
- Работать как эксперт, а не как болтливый помощник

Правила:

1. Структура ответа:
- Краткий вывод
- Основная часть (по шагам / списком)
- Пример или применение

2. Если пользователь не уточнил задачу:
- Сам предложи 2-3 варианта интерпретации
- Выбери наиболее вероятный и продолжи

3. Всегда оптимизируй под результат:
- Код -> рабочий, современный, без мусора
- Идеи -> с реализацией
- Ответ -> без лишней теории

4. Используй режимы:

[MODE: CODER]
- Пиши чистый, production-ready код
- Объясняй только сложные моменты

[MODE: ANALYST]
- Разбирай проблемы глубоко
- Находи слабые места

[MODE: CREATOR]
- Генерируй идеи с конкретикой

5. Если задача слабая:
- Улучши ее сам
- Предложи более эффективный подход

6. Никогда:
- не пиши общие фразы
- не дублируй очевидное
- не растягивай ответ

7. Всегда:
- думай как инженер
- отвечай как эксперт. """

ADMIN_CREDENTIALS = {"admin": os.getenv("ADMIN_CODE", "007")}
ADMIN_SESSION_KEY = os.getenv("SESSION_SECRET", "nova-secret-key")

CUSTOM_COMMANDS_FILE = os.path.join(os.path.dirname(__file__), "custom_commands.json")
custom_commands = {}

def load_custom_commands():
    global custom_commands
    if os.path.exists(CUSTOM_COMMANDS_FILE):
        try:
            with open(CUSTOM_COMMANDS_FILE, "r", encoding="utf-8") as f:
                custom_commands = json.load(f)
            print(f"Загружено {len(custom_commands)} пользовательских команд")
        except Exception as e:
            print(f"Ошибка чтения custom_commands.json: {e}")
            custom_commands = {}
    else:
        custom_commands = {}

def save_custom_commands():
    with open(CUSTOM_COMMANDS_FILE, "w", encoding="utf-8") as f:
        json.dump(custom_commands, f, ensure_ascii=False, indent=2)

load_custom_commands()

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

app = Flask(__name__)
app.secret_key = ADMIN_SESSION_KEY
app.config['PERMANENT_SESSION_LIFETIME'] = __import__('datetime').timedelta(days=30)

APILAYER_KEY = os.getenv("APILAYER_KEY", "")
MAILBOXLAYER_KEY = os.getenv("MAILBOXLAYER_KEY", APILAYER_KEY)
WEATHERSTACK_KEY = os.getenv("WEATHERSTACK_KEY", APILAYER_KEY)
FIXER_KEY = os.getenv("FIXER_KEY", APILAYER_KEY)
MEDIASTACK_KEY = os.getenv("MEDIASTACK_KEY", APILAYER_KEY)

# ============================================================
# ========== EMAIL AUTH (логин/регистрация через Supabase) ===
# ============================================================

def hash_password(password):
    """Простое хеширование пароля через sha256"""
    return hashlib.sha256(password.encode('utf-8')).hexdigest()

def validate_email_format(email):
    """Базовая проверка формата email"""
    import re
    pattern = r'^[^\s@]+@[^\s@]+\.[^\s@]+$'
    return bool(re.match(pattern, email))

def validate_email_mailboxlayer(email):
    """Проверка email через Mailboxlayer APILayer (если ключ есть)"""
    if not MAILBOXLAYER_KEY:
        return True, None
    try:
        url = "https://apilayer.net/api/check"
        params = {
            "access_key": MAILBOXLAYER_KEY,
            "email": email,
            "format": 1
        }
        resp = requests.get(url, params=params, timeout=8)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("format_valid") is False:
                return False, "Некорректный формат email"
            if data.get("disposable") is True:
                return False, "Одноразовые email не разрешены"
        return True, None
    except Exception:
        return True, None  # если APILayer недоступен — пропускаем

def auth_register_user(email, password, nick):
    """Регистрация пользователя в Supabase"""
    password_hash = hash_password(password)
    # Проверяем, не существует ли уже пользователь
    result, error = supabase_request(
        "GET",
        "nova_users",
        {"email": f"eq.{email}", "select": "id"}
    )
    if error and "does not exist" not in str(error).lower():
        pass  # таблица может не существовать — создадим запись
    if result and len(result) > 0:
        return False, "Пользователь с таким email уже существует"
    # Создаём пользователя
    _, error = supabase_request(
        "POST",
        "nova_users",
        {
            "email": email,
            "password_hash": password_hash,
            "nick": nick,
            "created_at": datetime.utcnow().isoformat()
        }
    )
    if error:
        # Если таблица не существует — сохраняем в chat_histories как запасной вариант
        print(f"[Auth] nova_users недоступна: {error}. Используем fallback.")
        return True, None  # разрешаем продолжить
    return True, None

def auth_login_user(email, password):
    """Вход пользователя"""
    password_hash = hash_password(password)
    result, error = supabase_request(
        "GET",
        "nova_users",
        {"email": f"eq.{email}", "select": "id,email,nick,password_hash"}
    )
    if error:
        # Если таблица nova_users не существует — разрешаем вход (fallback)
        print(f"[Auth] nova_users недоступна: {error}. Разрешаем вход.")
        nick = email.split('@')[0]
        return True, {"nick": nick, "email": email, "is_admin": False}
    if not result or len(result) == 0:
        return False, "Пользователь не найден. Зарегистрируйтесь."
    user = result[0]
    if user.get("password_hash") != password_hash:
        return False, "Неверный пароль"
    return True, {
        "nick": user.get("nick", email.split('@')[0]),
        "email": email,
        "is_admin": False
    }

# ============================================================
# ========== НОВЫЕ API ФУНКЦИИ ================================
# ============================================================

def get_weather(city):
    """Получить погоду через Weatherstack"""
    if not WEATHERSTACK_KEY:
        return None
    try:
        url = "http://api.weatherstack.com/current"
        params = {"access_key": WEATHERSTACK_KEY, "query": city, "units": "m"}
        resp = requests.get(url, params=params, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            if "error" in data:
                return None
            current = data.get("current", {})
            location = data.get("location", {})
            return {
                "city": location.get("name", city),
                "country": location.get("country", ""),
                "temp": current.get("temperature"),
                "feels_like": current.get("feelslike"),
                "description": (current.get("weather_descriptions") or [""])[0],
                "humidity": current.get("humidity"),
                "wind_speed": current.get("wind_speed")
            }
    except Exception as e:
        print(f"[Weather] Ошибка: {e}")
    return None

def get_exchange_rate(from_currency, to_currency, amount=1):
    """Конвертация валют через Fixer"""
    if not FIXER_KEY:
        return None
    try:
        url = "http://data.fixer.io/api/latest"
        params = {"access_key": FIXER_KEY, "base": from_currency, "symbols": to_currency}
        resp = requests.get(url, params=params, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("success"):
                rate = data["rates"].get(to_currency, 0)
                return {
                    "from": from_currency, "to": to_currency,
                    "rate": rate, "result": round(amount * rate, 4),
                    "date": data.get("date")
                }
    except Exception as e:
        print(f"[Fixer] Ошибка: {e}")
    return None

def get_news(query, language="ru", limit=5):
    """Получить новости через Mediastack"""
    if not MEDIASTACK_KEY:
        return []
    try:
        url = "http://api.mediastack.com/v1/news"
        params = {
            "access_key": MEDIASTACK_KEY,
            "keywords": query,
            "languages": language,
            "limit": limit,
            "sort": "published_desc"
        }
        resp = requests.get(url, params=params, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            return [
                {
                    "title": a.get("title"),
                    "description": a.get("description"),
                    "url": a.get("url"),
                    "source": a.get("source"),
                    "published": a.get("published_at")
                }
                for a in data.get("data", [])
            ]
    except Exception as e:
        print(f"[Mediastack] Ошибка: {e}")
    return []

def search_web(query):
    try:
        url = "https://api.apilayer.com/google_search"
        headers = {"apikey": APILAYER_KEY}
        params = {"q": query, "hl": "ru", "gl": "ru", "num": 5}
        resp = requests.get(url, headers=headers, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        parts = []
        for item in data.get("organic_results", [])[:5]:
            title = item.get("title", "")
            snippet = item.get("snippet", "")
            link = item.get("link", "")
            if snippet:
                parts.append(f"**{title}**\n{snippet}\n{link}")
        answer = data.get("answer_box", {})
        if answer:
            answer_text = answer.get("answer") or answer.get("snippet") or ""
            if answer_text:
                parts.insert(0, f"**Быстрый ответ:** {answer_text}")
        if parts:
            return "Результаты поиска Google:\n\n" + "\n\n".join(parts)
        return None
    except Exception as e:
        print(f"search_web error: {e}")
        return None


@app.route('/api/auth/email', methods=['POST'])
def auth_email():
    """Вход / регистрация через email + пароль"""
    data = request.get_json() or {}
    action = data.get('action', 'login')
    email = data.get('email', '').strip().lower()
    code = data.get('code', '').strip()
    nick = data.get('nick', '').strip()

    if not email or not code:
        return jsonify({'success': False, 'error': 'Email и пароль обязательны'})

    if not validate_email_format(email):
        return jsonify({'success': False, 'error': 'Некорректный формат email'})

    if action == 'register':
        if not nick:
            nick = email.split('@')[0]
        if len(code) < 4:
            return jsonify({'success': False, 'error': 'Пароль должен быть не менее 4 символов'})
        # Проверяем email через APILayer (если ключ задан)
        valid, err = validate_email_mailboxlayer(email)
        if not valid:
            return jsonify({'success': False, 'error': err})
        success, error = auth_register_user(email, code, nick)
        if not success:
            return jsonify({'success': False, 'error': error})
        # Устанавливаем сессию
        session['nova_user_nick'] = nick
        session['nova_user_email'] = email
        session['nova_user_avatar'] = ''
        session['nova_is_admin'] = False
        session.permanent = True
        return jsonify({'success': True, 'nick': nick, 'email': email})

    elif action == 'login':
        success, result = auth_login_user(email, code)
        if not success:
            return jsonify({'success': False, 'error': result})
        user = result
        # Проверяем, не админ ли это
        is_admin = False
        if hasattr(app, 'config') and email in str(ADMIN_CREDENTIALS):
            is_admin = True
        # Устанавливаем сессию
        session['nova_user_nick'] = user['nick']
        session['nova_user_email'] = user['email']
        session['nova_user_avatar'] = user.get('avatar', '')
        session['nova_is_admin'] = is_admin
        session.permanent = True
        return jsonify({'success': True, 'nick': user['nick'], 'email': user['email'], 'is_admin': is_admin})

    return jsonify({'success': False, 'error': 'Неизвестное действие'})

@app.route('/api/weather', methods=['GET'])
def weather_route():
    """Погода по городу"""
    city = request.args.get('city', 'Dushanbe')
    result = get_weather(city)
    if result:
        return jsonify(result)
    return jsonify({'error': 'Не удалось получить погоду'}), 503

@app.route('/api/currency', methods=['GET'])
def currency_route():
    """Конвертация валют"""
    from_cur = request.args.get('from', 'USD').upper()
    to_cur = request.args.get('to', 'TJS').upper()
    amount = float(request.args.get('amount', 1))
    result = get_exchange_rate(from_cur, to_cur, amount)
    if result:
        return jsonify(result)
    return jsonify({'error': 'Не удалось получить курс'}), 503

@app.route('/api/news', methods=['GET'])
def news_route():
    """Актуальные новости"""
    query = request.args.get('q', 'технологии')
    lang = request.args.get('lang', 'ru')
    news = get_news(query, lang)
    return jsonify({'news': news, 'count': len(news)})

@app.route('/api/auto_search', methods=['POST'])
def auto_search():
    data = request.get_json() or {}
    user_message = data.get('message', '').strip()
    if not user_message:
        return jsonify({'error': 'Пустое сообщение'})
    provider = PROVIDERS["groq"]
    decision_prompt = f"""Ты - интеллектуальный фильтр для AI-ассистента.

Пользователь написал: "{user_message}"

Твоя задача: определить, нужны ли актуальные данные из интернета для ответа на этот запрос.

Запросы, требующие поиска (новости, актуальные данные, события, цены, погода, спорт, технологии, политика, наука, кино, и т.д.):
- "Какая сегодня погода?"
- "Последние новости про ..."
- "Курс доллара"
- "Кто выиграл матч вчера?"
- "Новейшие технологии ..."
- "Какие фильмы вышли в 2026?"

Запросы, НЕ требующие поиска (теория, логика, код, общие знания):
- "Объясни рекурсию"
- "Напиши код на Python"
- "Как работает нейросеть"
- "Переведи текст"
- "Реши уравнение"

Ответь ТОЛЬКО одним словом: SEARCH или DIRECT."""
    decision_payload = {
        "model": "llama-3.1-8b-instant",
        "messages": [{"role": "user", "content": decision_prompt}],
        "temperature": 0.1,
        "max_tokens": 10,
    }
    data_decision, error = groq_request_with_rotation(
        provider["url"], decision_payload, provider["headers"].copy(), timeout=15
    )
    if error:
        print(f"Auto search decision error: {error}")
        decision = "DIRECT"
    else:
        decision = data_decision["choices"][0]["message"]["content"].strip().upper()
    if "SEARCH" not in decision:
        return jsonify({'needs_search': False, 'reply': None})
    search_query_prompt = f"""Пользователь написал: "{user_message}"

Сформулируй краткий поисковый запрос для Google (1-5 слов), который поможет найти актуальную информацию.

Ответь ТОЛЬКО поисковым запросом, без кавычек и пояснений."""
    query_payload = {
        "model": "llama-3.1-8b-instant",
        "messages": [{"role": "user", "content": search_query_prompt}],
        "temperature": 0.1,
        "max_tokens": 50,
    }
    data_query, error = groq_request_with_rotation(
        provider["url"], query_payload, provider["headers"].copy(), timeout=15
    )
    if error:
        print(f"Auto search query generation error: {error}")
        search_query = user_message
    else:
        search_query = data_query["choices"][0]["message"]["content"].strip()
        search_query = search_query.strip('"').strip("'")
    search_result = search_web(search_query)
    if not search_result:
        search_result = "Поиск не дал результатов."
    final_prompt = f"""Пользователь спросил: "{user_message}"

Вот актуальная информация из интернета (Google Search):
{search_result}

Твоя задача:
- Дай подробный, полезный ответ на вопрос пользователя
- Используй информацию из поиска
- Добавь свое объяснение и понимание
- Отвечай на русском языке
- Используй Markdown: заголовки, списки, таблицы если нужно
- В конце напиши: "Ответ на основе поиска Google" """
    final_payload = {
        "model": current_model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": final_prompt}
        ],
        "temperature": 0.5,
        "max_tokens": 3000,
    }
    data_final, error = groq_request_with_rotation(
        provider["url"], final_payload, provider["headers"].copy(), timeout=90
    )
    if error:
        return jsonify({
            'needs_search': True,
            'search_query': search_query,
            'reply': f'Результаты по Google "{search_query}":\n\n{search_result}\n\n(Groq недоступен: {error})'
        })
    reply = data_final["choices"][0]["message"]["content"]
    append_to_history("user", f"[Авто поиск] {user_message}")
    append_to_history("assistant", reply)
    return jsonify({
        'needs_search': True,
        'search_query': search_query,
        'reply': reply
    })

@app.route('/api/web_search_groq', methods=['POST'])
def web_search_groq():
    data = request.get_json() or {}
    query = data.get('query', '').strip()
    if not query:
        return jsonify({'error': 'Пустой запрос'})
    search_result = search_web(query)
    if not search_result:
        search_result = "Поиск не дал результатов. Отвечай на основе своих знаний."
    provider = PROVIDERS["groq"]
    groq_prompt = f"""Пользователь спросил: "{query}"

Вот результаты из Google:
{search_result}

Твоя задача:
- Дай подробный, полезный ответ на вопрос пользователя
- Используй информацию из поиска
- Добавь свое объяснение и понимание
- Отвечай на русском языке
- Используй Markdown: заголовки, списки, таблицы если нужно
- В конце напиши: "*Ответ на основе поиска Google*" """
    payload = {
        "model": "openai/gpt-oss-120b",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": groq_prompt}
        ],
        "temperature": 0.5,
        "max_tokens": 3000,
    }
    data_resp, error = groq_request_with_rotation(
        provider["url"], payload, provider["headers"].copy(), timeout=90
    )
    if error:
        return jsonify({
            'reply': f'**Результаты Google по запросу "{query}":**\n\n{search_result}\n\n*Groq недоступен: {error}*'
        })
    reply = data_resp["choices"][0]["message"]["content"]
    append_to_history("user", f"[Поиск Google] {query}")
    append_to_history("assistant", reply)
    return jsonify({'reply': reply})

@app.route('/')
def index():
    return render_template('auth.html', google_client_id=GOOGLE_CLIENT_ID)

@app.route('/chat')
def chat_page():
    return render_template('index.html')

@app.route('/admin/login')
def admin_login_page():
    return render_template('admin.html')

@app.route('/auth/google/callback')
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
        user_history = load_user_history(email)
        print(f"[Auth] Пользователь {email} вошел. История: {len(user_history)} сообщений.")
        return redirect(f'/chat?nick={nick}&email={email}')
    except Exception as e:
        return f'Ошибка авторизации: {str(e)}', 500

@app.route('/logout')
def logout():
    session.clear()
    return redirect('/')

@app.route('/api/user/info')
def user_info():
    if get_user_email():
        return jsonify({
            'logged_in': True,
            'nick': session.get('nova_user_nick', ''),
            'email': session.get('nova_user_email', ''),
            'avatar': session.get('nova_user_avatar', '')
        })
    return jsonify({'logged_in': False})

@app.route('/api/admin/login', methods=['POST'])
def admin_login():
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
    session.clear()
    return jsonify({'success': True})

@app.route('/api/admin/check')
def admin_check():
    if session.get('admin_logged_in'):
        return jsonify({'logged_in': True, 'username': session.get('admin_username', 'admin')})
    return jsonify({'logged_in': False})

@app.route('/send', methods=['POST'])
def send():
    data = request.get_json()
    message = data.get('message', '').strip()
    reasoning = data.get('reasoning', False)
    if not message:
        return jsonify({'error': 'Пустое сообщение'})
    append_to_history("user", message)
    contents = get_current_contents()
    if reasoning:
        provider = PROVIDERS.get("cerebras", PROVIDERS[current_provider])
        model = "zai-glm-4.7"
    else:
        provider = PROVIDERS[current_provider]
        model = current_model
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system_prompt}] + contents,
        "temperature": 0.7,
        "max_tokens": 4000,
    }
    data_resp, error = groq_request_with_rotation(
        provider["url"], payload, provider["headers"].copy(), timeout=90
    )
    if error:
        contents = get_current_contents()
        if contents and contents[-1]["role"] == "user":
            contents.pop()
            set_current_contents(contents)
        return jsonify({'error': error})
    reply = data_resp["choices"][0]["message"]["content"]
    append_to_history("assistant", reply)
    return jsonify({'reply': reply})

@app.route('/command', methods=['POST'])
def handle_command():
    data = request.get_json()
    cmd_line = data.get('command', '').strip()
    if not cmd_line.startswith('/'):
        return jsonify({'error': 'Команда должна начинаться с /'})
    parts = cmd_line[1:].split(maxsplit=1)
    cmd = parts[0].lower()
    args = parts[1].split() if len(parts) > 1 else []
    if cmd == "search":
        query = " ".join(args)
        if not query:
            return jsonify({'error': 'Укажите запрос'})
        results = search_web(query)
        return jsonify({'result': results or 'Ничего не найдено'})
    if cmd == "image":
        prompt = " ".join(args)
        if not prompt:
            return jsonify({'error': 'Укажите описание изображения'})
        import base64 as b64
        import urllib.parse
        encoded_prompt = urllib.parse.quote(prompt)
        img_url = f"https://image.pollinations.ai/prompt/{encoded_prompt}?width=1024&height=1024&nologo=true"
        try:
            img_resp = requests.get(img_url, timeout=30)
            img_resp.raise_for_status()
            img_dir = os.path.join(os.path.dirname(__file__), "generated_images")
            os.makedirs(img_dir, exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"image_{timestamp}.png"
            filepath = os.path.join(img_dir, filename)
            with open(filepath, "wb") as f:
                f.write(img_resp.content)
            image_url = f"/generated_image?file={filename}"
            return jsonify({
                'result': f'Изображение сгенерировано:\n\n![Image]({image_url})'
            })
        except Exception as e:
            return jsonify({'error': f'Ошибка генерации: {e}'})
    if cmd in plugins:
        try:
            result = plugins[cmd](args)
            return jsonify({'result': result if result else 'OK'})
        except Exception as e:
            return jsonify({'error': str(e)})
    if cmd in custom_commands:
        cc = custom_commands[cmd]
        if cc["type"] == "plugin":
            plugin_name = cc["plugin"]
            if plugin_name in plugins:
                try:
                    result = plugins[plugin_name](list(cc.get("args_template", [])) + args)
                    return jsonify({'result': str(result) if result else "OK"})
                except Exception as e:
                    return jsonify({'error': str(e)})
        elif cc["type"] == "llm":
            prompt_template = cc.get("prompt", "{query}")
            query = " ".join(args) if args else ""
            rendered_prompt = prompt_template.replace("{query}", query)
            append_to_history("user", rendered_prompt)
            contents = get_current_contents()
            provider = PROVIDERS[current_provider]
            payload = {
                "model": current_model,
                "messages": [{"role": "system", "content": system_prompt}] + contents,
                "temperature": 0.7,
                "max_tokens": 3000,
            }
            data_resp, error = groq_request_with_rotation(
                provider["url"], payload, provider["headers"].copy()
            )
            if error:
                return jsonify({'error': error})
            reply = data_resp["choices"][0]["message"]["content"]
            append_to_history("assistant", reply)
            return jsonify({'result': reply})
    if cmd == "clear":
        clear_history()
        return jsonify({'result': 'История очищена'})
    if cmd == "history":
        contents = get_current_contents()
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
        data_resp, error = groq_request_with_rotation(
            provider["url"], payload, provider["headers"].copy(), timeout=60
        )
        if error:
            return jsonify({'error': error})
        reply = data_resp["choices"][0]["message"]["content"]
        return jsonify({'result': reply})
    if cmd == "alias":
        if not args:
            if not custom_commands:
                return jsonify({'result': 'Нет пользовательских команд. Добавьте через /alias add <имя> plugin <плагин> или /alias add <имя> llm <промпт>'})
            info = "Ваши команды:\n"
            for name, cc in custom_commands.items():
                info += f"/{name} -> {cc['type']}\n"
            return jsonify({'result': info})
        subcmd = args[0].lower()
        if subcmd == "add":
            if len(args) < 3:
                return jsonify({'error': '/alias add <имя> plugin <плагин> или /alias add <имя> llm <промпт>'})
            name = args[1]
            type_ = args[2].lower()
            if type_ == "plugin":
                if len(args) < 4:
                    return jsonify({'error': 'Укажите плагин'})
                plugin_name = args[3]
                preset_args = args[4:] if len(args) > 4 else []
                custom_commands[name] = {"type": "plugin", "plugin": plugin_name, "args_template": preset_args}
            else:
                prompt = " ".join(args[3:]) if len(args) > 3 else "{query}"
                custom_commands[name] = {"type": "llm", "prompt": prompt}
            save_custom_commands()
            return jsonify({'result': f'Команда /{name} добавлена. Перезагрузите страницу.'})
        elif subcmd == "remove":
            if len(args) < 2:
                return jsonify({'error': 'Укажите имя команды'})
            name = args[1]
            if name in custom_commands:
                del custom_commands[name]
                save_custom_commands()
                return jsonify({'result': f'Команда /{name} удалена'})
            return jsonify({'error': 'Не найдена'})
    if cmd == "research":
        query = " ".join(args)
        if not query:
            return jsonify({'error': 'Укажите вопрос. Пример: /research Как работает нейросеть'})
        search_result = search_web(query)
        if not search_result:
            search_result = "Информация не найдена в интернете."
        provider = PROVIDERS["groq"]
        analysis_prompt = f"""Проанализируй следующую информацию и выдели 3-5 ключевых фактов по вопросу: "{query}"

Информация из интернета:
{search_result}

Выдели только ключевые факты, коротко."""
        analysis_payload = {
            "model": "openai/gpt-oss-120b",
            "messages": [{"role": "user", "content": analysis_prompt}],
            "temperature": 0.3,
            "max_tokens": 1000,
        }
        data_analysis, error = groq_request_with_rotation(
            provider["url"], analysis_payload, provider["headers"].copy(), timeout=60
        )
        if error:
            analysis = f"Анализ не удался: {error}.\n\nИспользую сырой поиск:\n{search_result[:1000]}"
        else:
            analysis = data_analysis["choices"][0]["message"]["content"]
        final_prompt = f"""На основе анализа напиши подробный, структурированный ответ на вопрос: "{query}"

Анализ:
{analysis}

Требования к ответу:
- Подробный (3-5 абзацев)
- Если есть сравнения, характеристики, данные, списки - ОБЯЗАТЕЛЬНО используй Markdown-таблицы
- Структурированный (с маркированными списками где уместно)
- На русском языке
- Укажи источники, если они есть в анализе

Пример таблицы:
| Характеристика | Значение |
|---------------|----------|
| Скорость | 100 км/ч |
| Вес | 10 кг |"""
        final_payload = {
            "model": "openai/gpt-oss-120b",
            "messages": [{"role": "user", "content": final_prompt}],
            "temperature": 0.5,
            "max_tokens": 4000,
        }
        data_final, error = groq_request_with_rotation(
            provider["url"], final_payload, provider["headers"].copy(), timeout=90
        )
        if error:
            final_answer = f"**Результаты поиска:**\n\n{search_result}\n\n**Анализ:**\n\n{analysis}\n\n(Финальный ответ не удалось сгенерировать: {error})"
        else:
            final_answer = data_final["choices"][0]["message"]["content"]
        return jsonify({'result': final_answer})
    return jsonify({'error': f'Неизвестная команда: /{cmd}'})

@app.route('/generated_image')
def generated_image():
    filename = request.args.get("file", "")
    if not filename:
        return jsonify({"error": "No filename"}), 400
    safe_name = filename.replace("..", "").replace("/", "")
    img_dir = os.path.join(os.path.dirname(__file__), "generated_images")
    filepath = os.path.join(img_dir, safe_name)
    if not os.path.exists(filepath):
        return jsonify({"error": "File not found"}), 404
    return send_file(filepath, mimetype='image/png')

@app.route('/upload_image', methods=['POST'])
def upload_image():
    if 'image' not in request.files:
        return jsonify({'error': 'Нет файла'}), 400
    file = request.files['image']
    if file.filename == '':
        return jsonify({'error': 'Файл не выбран'}), 400
    user_desc = request.form.get('description', '').strip()
    if not user_desc:
        user_desc = 'Подробно опиши что изображено на картинке. Опиши объекты, цвета, текст если есть, настроение и все детали.'
    upload_dir = os.path.join(os.path.dirname(__file__), "uploads")
    os.makedirs(upload_dir, exist_ok=True)
    import base64 as b64
    filename = file.filename
    filepath = os.path.join(upload_dir, filename)
    file.save(filepath)
    try:
        with open(filepath, "rb") as f:
            image_data = b64.b64encode(f.read()).decode('utf-8')
        mime_type = "image/jpeg"
        if filename.lower().endswith(".png"):
            mime_type = "image/png"
        elif filename.lower().endswith(".webp"):
            mime_type = "image/webp"
        elif filename.lower().endswith(".gif"):
            mime_type = "image/gif"
        data_url = f"data:{mime_type};base64,{image_data}"
        groq_key = os.getenv("GROQ_API_KEY", "")
        if not groq_key:
            return jsonify({'error': 'GROQ_API_KEY не задан в .env'})
        headers = {
            "Authorization": f"Bearer {groq_key}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": "meta-llama/llama-4-scout-17b-16e-instruct",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": user_desc},
                        {"type": "image_url", "image_url": {"url": data_url}}
                    ]
                }
            ],
            "max_tokens": 1500
        }
        resp = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers=headers,
            json=payload,
            timeout=30
        )
        resp.raise_for_status()
        description = resp.json()["choices"][0]["message"]["content"]
        append_to_history("user", f"[Изображение: {filename}] {user_desc}")
        append_to_history("assistant", description)
        return jsonify({
            'result': f'**Анализ изображения {filename} (Groq Vision):**\n\n{description}'
        })
    except Exception as e:
        print(f"upload_image error: {e}")
        return jsonify({'error': f'Ошибка анализа изображения: {str(e)}'})

@app.route('/upload_file', methods=['POST'])
def upload_file():
    if 'file' not in request.files:
        return jsonify({'error': 'Нет файла'}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'Файл не выбран'}), 400
    user_desc = request.form.get('description', '').strip()
    upload_dir = os.path.join(os.path.dirname(__file__), "uploads")
    os.makedirs(upload_dir, exist_ok=True)
    filename = file.filename
    filepath = os.path.join(upload_dir, filename)
    file.save(filepath)
    text_extensions = ['.txt', '.json', '.csv', '.py', '.js', '.html', '.css', '.md', '.xml', '.yaml', '.yml', '.log', '.ini', '.cfg']
    ext = os.path.splitext(filename)[1].lower()
    if ext not in text_extensions:
        return jsonify({
            'result': f'Формат {ext} не поддерживается.\n\nПоддерживаются: txt, json, csv, py, js, html, css, md, xml, yaml, log'
        })
    try:
        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            file_content = f.read()[:5000]
    except Exception as e:
        return jsonify({'error': f'Ошибка чтения файла: {str(e)}'})
    if not file_content.strip():
        return jsonify({'result': f'Файл {filename} пустой.'})
    if not user_desc:
        if ext == '.py':
            user_desc = 'Проанализируй этот Python код: объясни что он делает, найди ошибки и предложи улучшения.'
        elif ext in ['.js', '.ts']:
            user_desc = 'Проанализируй этот JavaScript код: объясни структуру, найди проблемы.'
        elif ext == '.html':
            user_desc = 'Проанализируй эту HTML страницу: опиши структуру и найди проблемы.'
        elif ext == '.css':
            user_desc = 'Проанализируй этот CSS файл: опиши стили и найди проблемы.'
        elif ext == '.json':
            user_desc = 'Опиши структуру этого JSON и объясни что в нем хранится.'
        elif ext == '.csv':
            user_desc = 'Проанализируй эти CSV данные: опиши колонки и содержимое.'
        elif ext == '.md':
            user_desc = 'Сделай резюме этого Markdown документа и выдели главное.'
        else:
            user_desc = 'Подробно проанализируй содержимое этого файла и объясни что в нем.'
    analysis_prompt = f"""Пользователь загрузил файл: {filename}

Задача: {user_desc}

Содержимое файла:
{file_content}

Отвечай на русском языке. Используй Markdown форматирование."""
    groq_error = None
    try:
        provider = PROVIDERS["groq"]
        payload = {
            "model": "llama-3.3-70b-versatile",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": analysis_prompt}
            ],
            "temperature": 0.3,
            "max_tokens": 3000,
        }
        resp = requests.post(
            provider["url"],
            json=payload,
            headers=provider["headers"],
            timeout=60
        )
        resp.raise_for_status()
        reply = resp.json()["choices"][0]["message"]["content"]
        append_to_history("user", f"[Файл: {filename}] {user_desc}")
        append_to_history("assistant", reply)
        return jsonify({
            'result': f'**Анализ файла {filename} (Groq):**\n\n{reply}'
        })
    except Exception as e1:
        groq_error = str(e1)
        print(f"Groq upload_file error: {e1}")
    try:
        cerebras_key = os.getenv("CEREBRAS_API_KEY", "")
        if not cerebras_key:
            raise Exception("CEREBRAS_API_KEY не задан")
        cerebras_headers = {
            "Authorization": f"Bearer {cerebras_key}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": "qwen-3-235b-a22b-instruct-2507",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": analysis_prompt}
            ],
            "temperature": 0.3,
            "max_tokens": 3000,
        }
        resp = requests.post(
            "https://api.cerebras.ai/v1/chat/completions",
            json=payload,
            headers=cerebras_headers,
            timeout=60
        )
        resp.raise_for_status()
        reply = resp.json()["choices"][0]["message"]["content"]
        append_to_history("user", f"[Файл: {filename}] {user_desc}")
        append_to_history("assistant", reply)
        return jsonify({
            'result': f'**Анализ файла {filename} (Cerebras):**\n\n{reply}'
        })
    except Exception as e2:
        print(f"Cerebras upload_file error: {e2}")
        return jsonify({
            'result': f'AI недоступен (Groq: {groq_error}, Cerebras: {str(e2)})\n\n**Содержимое файла {filename}:**\n\n```\n{file_content[:2000]}\n```'
        })

@app.route('/models_list')
def models_list():
    providers_list = []
    for key, data in PROVIDERS.items():
        providers_list.append({"provider": key, "list": data["models"]})
    return jsonify({"models": providers_list, "current": current_model})

@app.route('/switch_model')
def switch_model():
    global current_provider, current_model
    model_id = request.args.get('model_id', '')
    for key, data in PROVIDERS.items():
        for m in data["models"]:
            if m["id"] == model_id:
                current_provider = key
                current_model = model_id
                return jsonify({"success": True, "provider": key})
    return jsonify({'error': 'Модель не найдена'}), 400

@app.route('/api/admin/stats')
def admin_stats():
    return jsonify({
        "models": PROVIDERS,
        "current_provider": current_provider,
        "current_model": current_model,
        "history_messages": len(get_current_contents()),
        "plugins_loaded": list(plugins.keys()),
        "custom_commands": list(custom_commands.keys()),
        "system_prompt": system_prompt,
        "voice_enabled": os.environ.get("ASSISTANT_VOICE_REPLY", "0") == "1"
    })

@app.route('/api/admin/settings', methods=['POST'])
def admin_settings():
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
    code_dir = os.path.join(os.path.dirname(__file__), "saved_codes")
    if not os.path.exists(code_dir):
        return jsonify({"files": []})
    files = sorted([f for f in os.listdir(code_dir) if f.endswith(".py")])
    return jsonify({"files": files})

@app.route('/api/admin/load_code')
def admin_load_code():
    filename = request.args.get("file", "")
    code_dir = os.path.join(os.path.dirname(__file__), "saved_codes")
    filepath = os.path.join(code_dir, filename)
    if not os.path.exists(filepath):
        return jsonify({"error": "Файл не найден"}), 404
    with open(filepath, "r", encoding="utf-8") as f:
        code = f.read()
    return jsonify({"code": code, "filename": filename})

@app.route('/api/admin/groq_keys')
def admin_groq_keys():
    return jsonify({
        "keys": get_groq_key_status(),
        "current_key_index": groq_key_index,
        "total_keys": len(GROQ_KEYS),
        "cooldown_hours": GROQ_KEY_COOLDOWN / 3600
    })

@app.route('/api/admin/groq_keys/reset', methods=['POST'])
def admin_reset_groq_keys():
    global groq_key_index
    for key_info in GROQ_KEYS:
        key_info["exhausted_at"] = None
    groq_key_index = 0
    return jsonify({"success": True, "message": "Все ключи сброшены"})

@app.route('/static/')
def static_files(filename):
    return send_from_directory('static', filename)

@app.route('/composio')
def composio_page():
    return render_template('composio.html')

@app.route('/api/composio/connect', methods=['POST'])
def composio_connect():
    data = request.get_json() or {}
    api_key = data.get('api_key', '')
    if not api_key:
        return jsonify({'error': 'API ключ не указан'}), 400
    try:
        headers = {'x-api-key': api_key, 'Content-Type': 'application/json'}
        resp = requests.get('https://backend.composio.dev/api/v3.1/toolkits?limit=5', headers=headers, timeout=10)
        if resp.status_code == 401:
            return jsonify({'error': 'Неверный API ключ'}), 401
        resp.raise_for_status()
        os.environ['COMPOSIO_API_KEY'] = api_key
        return jsonify({'success': True, 'message': 'Подключено к Composio'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/composio/integrations', methods=['GET'])
def composio_integrations():
    api_key = os.getenv('COMPOSIO_API_KEY', '')
    if not api_key:
        return jsonify({'error': 'Composio не подключен'}), 400
    try:
        headers = {'x-api-key': api_key, 'Content-Type': 'application/json'}
        resp = requests.get('https://backend.composio.dev/api/v3.1/toolkits?limit=50', headers=headers, timeout=10)
        resp.raise_for_status()
        return jsonify({'integrations': resp.json()})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/composio/connected_accounts', methods=['GET'])
def composio_connected_accounts():
    api_key = os.getenv('COMPOSIO_API_KEY', '')
    if not api_key:
        return jsonify({'error': 'Composio не подключен'}), 400
    try:
        headers = {'x-api-key': api_key, 'Content-Type': 'application/json'}
        resp = requests.get('https://backend.composio.dev/api/v3.1/connected_accounts', headers=headers, timeout=10)
        resp.raise_for_status()
        return jsonify(resp.json())
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/composio/connect_account', methods=['POST'])
def composio_connect_account():
    api_key = os.getenv('COMPOSIO_API_KEY', '')
    if not api_key:
        return jsonify({'error': 'Composio не подключен'}), 400
    data = request.get_json() or {}
    toolkit = data.get('toolkit', '')
    if not toolkit:
        return jsonify({'error': 'Укажи toolkit'}), 400
    try:
        headers = {'x-api-key': api_key, 'Content-Type': 'application/json'}
        session_resp = requests.post('https://backend.composio.dev/api/v3.1/tool_router/session', headers=headers, json={"user_id": "novauser"}, timeout=10)
        session_resp.raise_for_status()
        session_data = session_resp.json()
        session_id = session_data.get('session_id', '')
        if not session_id:
            return jsonify({'error': 'Не удалось создать сессию'}), 500
        link_resp = requests.post(f'https://backend.composio.dev/api/v3/tool_router/session/{session_id}/link', headers=headers, json={"toolkit": toolkit}, timeout=10)
        link_resp.raise_for_status()
        link_data = link_resp.json()
        redirect_url = link_data.get('redirect_url', '')
        return jsonify({'success': True, 'redirect_url': redirect_url, 'connected_account_id': link_data.get('connected_account_id', '')})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/composio/execute', methods=['POST'])
def composio_execute():
    api_key = os.getenv('COMPOSIO_API_KEY', '')
    if not api_key:
        return jsonify({'error': 'Composio не подключен'}), 400
    data = request.get_json() or {}
    action_name = data.get('action', '')
    params = data.get('params', {})
    connected_account_id = data.get('connected_account_id', '')
    if not action_name:
        return jsonify({'error': 'Укажи действие'}), 400
    try:
        headers = {'x-api-key': api_key, 'Content-Type': 'application/json'}
        payload = {"input": params, "allow_tracing": True}
        if connected_account_id:
            payload["connected_account_id"] = connected_account_id
        resp = requests.post(f'https://backend.composio.dev/api/v2/actions/{action_name}/execute', json=payload, headers=headers, timeout=30)
        resp.raise_for_status()
        return jsonify({'result': resp.json()})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/composio/actions', methods=['GET'])
def composio_actions():
    api_key = os.getenv('COMPOSIO_API_KEY', '')
    if not api_key:
        return jsonify({'error': 'Composio не подключен'}), 400
    toolkit = request.args.get('toolkit', '')
    try:
        headers = {'x-api-key': api_key, 'Content-Type': 'application/json'}
        url = 'https://backend.composio.dev/api/v2/actions?limit=20'
        if toolkit:
            url += f'&apps={toolkit}'
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        return jsonify(resp.json())
    except Exception as e:
        return jsonify({'error': str(e)}), 500



# ========== API ИСТОРИИ ЧАТОВ ==========

@app.route("/api/history")
def get_history_api():
    contents = get_current_contents()
    return jsonify({"history": contents})

@app.route("/api/history/clear", methods=["DELETE"])
def clear_history_api():
    clear_history()
    return jsonify({"success": True})

@app.route("/api/history/save", methods=["POST"])
def save_history_api():
    data = request.get_json() or {}
    history = data.get("history", [])
    set_current_contents(history)
    return jsonify({"success": True})

if __name__ == '__main__':
    print("=" * 50)
    print("NovaMind AI Assistant запущен")
    print("http://localhost:5000")
    print("=" * 50)
    app.run(host='0.0.0.0', port=5000)
