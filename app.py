import os
import sys
import json
import requests
import subprocess
from datetime import datetime
from flask import Flask, request, jsonify, render_template, send_file, session, send_from_directory, redirect

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
        "Authorization": "Bearer {CEREBRAS_API_KEY}",
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

system_prompt = """ Ты — продвинутый AI ассистент NovaMind, ориентированный на практическую пользу.

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
- Сам предложи 2–3 варианта интерпретации
- Выбери наиболее вероятный и продолжи

3. Всегда оптимизируй под результат:
- Код → рабочий, современный, без мусора
- Идеи → с реализацией
- Ответ → без лишней теории

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
- Улучши её сам
- Предложи более эффективный подход

6. Никогда:
- не пиши общие фразы
- не дублируй очевидное
- не растягивай ответ

7. Всегда:
- думай как инженер
- отвечай как эксперт. """
contents = []

# ---------- Админ ----------
ADMIN_CREDENTIALS = {
    "admin": os.getenv("ADMIN_CODE", "007"),
}
ADMIN_SESSION_KEY = os.getenv("SESSION_SECRET", "nova-secret-key")

# ---------- Кастомные алиасы ----------
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

# ========== GOOGLE OAUTH ==========

@app.route('/auth/google/callback')
def auth_google_callback():
    """Обработка ответа от Google OAuth"""
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
        
        return redirect(f'/chat?nick={nick}&email={email}')
        
    except Exception as e:
        return f'Ошибка авторизации: {str(e)}', 500

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
    reasoning = data.get('reasoning', False)  # ← получаем флаг
    
    if not message:
        return jsonify({'error': 'Пустое сообщение'})

    contents.append({"role": "user", "content": message})
    
    # Если включён режим рассуждения — используем DeepSeek R1
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
        "max_tokens": 4000,  # больше токенов для рассуждений
    }

    try:
        resp = requests.post(provider["url"], json=payload, headers=provider["headers"], timeout=90)
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
    """Обработка команд (/search, /code, /image, плагины, алиасы)"""
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

    # Генерация изображений (Pollinations.ai)
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
            
            # Сохраняем в папку generated_images
            img_dir = os.path.join(os.path.dirname(__file__), "generated_images")
            os.makedirs(img_dir, exist_ok=True)
            
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"image_{timestamp}.png"
            filepath = os.path.join(img_dir, filename)
            
            with open(filepath, "wb") as f:
                f.write(img_resp.content)
            
            # Возвращаем HTML с отображением картинки
            image_url = f"/generated_image?file={filename}"
            return jsonify({
                'result': f'✅ Изображение сгенерировано:\n\n![Image]({image_url})'
            })
        except Exception as e:
            return jsonify({'error': f'Ошибка генерации: {e}'})

    # Плагины
    if cmd in plugins:
        try:
            result = plugins[cmd](args)
            return jsonify({'result': result if result else 'OK'})
        except Exception as e:
            return jsonify({'error': str(e)})

    # Кастомные алиасы
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
            contents.append({"role": "user", "content": rendered_prompt})
            provider = PROVIDERS[current_provider]
            payload = {
                "model": current_model,
                "messages": [{"role": "system", "content": system_prompt}] + contents,
                "temperature": 0.7,
                "max_tokens": 3000,
            }
            try:
                resp = requests.post(provider["url"], json=payload, headers=provider["headers"])
                resp.raise_for_status()
                data_resp = resp.json()
                reply = data_resp["choices"][0]["message"]["content"]
                contents.append({"role": "assistant", "content": reply})
                return jsonify({'result': reply})
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

    # Управление алиасами
    if cmd == "alias":
        if not args:
            if not custom_commands:
                return jsonify({'result': 'Нет пользовательских команд. Добавьте через /alias add <имя> plugin <плагин> или /alias add <имя> llm <промпт>'})
            info = "Ваши команды:\n"
            for name, cc in custom_commands.items():
                info += f"/{name} → {cc['type']}\n"
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

    # ========== ГЛУБОКОЕ ИССЛЕДОВАНИЕ ==========
    if cmd == "research":
        query = " ".join(args)
        if not query:
            return jsonify({'error': 'Укажите вопрос. Пример: /research Как работает нейросеть'})
        
        # Шаг 1: Поиск в интернете
        search_result = search_web(query)
        if not search_result:
            search_result = "Информация не найдена в интернете."
        
        # Шаг 2: Анализ через Groq
        provider = PROVIDERS["groq"]
        analysis_prompt = f"""Проанализируй следующую информацию и выдели 3-5 ключевых фактов по вопросу: "{query}"

Информация из интернета:
{search_result}

Выдели только ключевые факты, коротко."""
        
        try:
            analysis_payload = {
                "model": "openai/gpt-oss-120b",
                "messages": [{"role": "user", "content": analysis_prompt}],
                "temperature": 0.3,
                "max_tokens": 1000,
            }
            analysis_resp = requests.post(
                provider["url"], 
                json=analysis_payload, 
                headers=provider["headers"], 
                timeout=60
            )
            analysis_resp.raise_for_status()
            analysis = analysis_resp.json()["choices"][0]["message"]["content"]
        except Exception as e:
            analysis = f"Анализ не удался: {str(e)}.\n\nИспользую сырой поиск:\n{search_result[:1000]}"
        
        # Шаг 3: Финальный ответ (с таблицами!)
        final_prompt = f"""На основе анализа напиши подробный, структурированный ответ на вопрос: "{query}"

Анализ:
{analysis}

Требования к ответу:
- Подробный (3-5 абзацев)
- Если есть сравнения, характеристики, данные, списки — ОБЯЗАТЕЛЬНО используй Markdown-таблицы
- Структурированный (с маркированными списками где уместно)
- На русском языке
- Укажи источники, если они есть в анализе

Пример таблицы:
| Характеристика | Значение |
|---------------|----------|
| Скорость      | 100 км/ч |
| Вес           | 10 кг    |"""
        
        try:
            final_payload = {
                "model": "openai/gpt-oss-120b",
                "messages": [{"role": "user", "content": final_prompt}],
                "temperature": 0.5,
                "max_tokens": 4000,
            }
            final_resp = requests.post(
                provider["url"], 
                json=final_payload, 
                headers=provider["headers"], 
                timeout=90
            )
            final_resp.raise_for_status()
            final_answer = final_resp.json()["choices"][0]["message"]["content"]
        except Exception as e:
            final_answer = f"**🔍 Результаты поиска:**\n\n{search_result}\n\n**📊 Анализ:**\n\n{analysis}\n\n_(Финальный ответ не удалось сгенерировать: {str(e)})_"
        
        return jsonify({'result': final_answer})




    return jsonify({'error': f'Неизвестная команда: /{cmd}'})

# ========== ОТДАЧА ИЗОБРАЖЕНИЙ ==========

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


# ========== ЗАГРУЗКА ФАЙЛОВ ==========

@app.route('/upload_image', methods=['POST'])
def upload_image():
    """Загрузка и анализ изображения"""
    if 'image' not in request.files:
        return jsonify({'error': 'Нет файла'}), 400
    
    file = request.files['image']
    if file.filename == '':
        return jsonify({'error': 'Файл не выбран'}), 400
    
    # Сохраняем файл
    upload_dir = os.path.join(os.path.dirname(__file__), "uploads")
    os.makedirs(upload_dir, exist_ok=True)
    
    import base64 as b64
    filename = file.filename
    filepath = os.path.join(upload_dir, filename)
    file.save(filepath)
    
    # Анализируем через OpenRouter Gemini Vision (бесплатно)
    try:
        with open(filepath, "rb") as f:
            image_data = b64.b64encode(f.read()).decode('utf-8')
        
        mime_type = "image/jpeg"
        if filename.lower().endswith(".png"):
            mime_type = "image/png"
        elif filename.lower().endswith(".webp"):
            mime_type = "image/webp"
        
        data_url = f"data:{mime_type};base64,{image_data}"
        
        api_key = os.getenv("OPENROUTER_API_KEY") or os.getenv("GROQ_API_KEY")
        if api_key:
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json"
            }
            payload = {
                "model": "google/gemini-2.0-flash-001",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "Подробно опиши, что изображено на этой картинке. Опиши объекты, цвета, настроение."},
                            {"type": "image_url", "image_url": {"url": data_url}}
                        ]
                    }
                ],
                "max_tokens": 500
            }
            
            resp = requests.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=payload, timeout=30)
            resp.raise_for_status()
            result = resp.json()
            description = result["choices"][0]["message"]["content"]
            
            return jsonify({'result': f'📷 **Анализ изображения:**\n\n{description}', 'filepath': filepath})
    
    except:
        pass
    
    return jsonify({'result': f'✅ Изображение сохранено: {filepath}', 'filepath': filepath})


@app.route('/upload_file', methods=['POST'])
def upload_file():
    """Загрузка файла с предпросмотром содержимого"""
    if 'file' not in request.files:
        return jsonify({'error': 'Нет файла'}), 400
    
    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'Файл не выбран'}), 400
    
    upload_dir = os.path.join(os.path.dirname(__file__), "uploads")
    os.makedirs(upload_dir, exist_ok=True)
    
    filename = file.filename
    filepath = os.path.join(upload_dir, filename)
    file.save(filepath)
    
    # Пробуем прочитать содержимое текстовых файлов
    preview = ""
    try:
        text_extensions = ['.txt', '.json', '.csv', '.py', '.js', '.html', '.css', '.md', '.xml', '.yaml', '.yml']
        ext = os.path.splitext(filename)[1].lower()
        
        if ext in text_extensions:
            with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()[:3000]
            preview = f"```\n{content}\n```"
    except:
        pass
    
    return jsonify({
        'result': f'✅ Файл сохранён: {filepath}',
        'filepath': filepath,
        'filename': filename,
        'preview': preview
    })


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
        "custom_commands": list(custom_commands.keys()),
        "system_prompt": system_prompt,
        "voice_enabled": os.environ.get("ASSISTANT_VOICE_REPLY", "0") == "1"
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


@app.route('/composio')
def composio_page():
    return render_template('composio.html')


@app.route('/api/composio/connect', methods=['POST'])
def composio_connect():
    data = request.get_json() or {}
    api_key = data.get('api_key', '')
    if not api_key:
        return jsonify({'error': 'API ключ не указан'}), 400

    # Проверяем ключ — запрашиваем apps
    try:
        headers = {
            'x-api-key': api_key,
            'Content-Type': 'application/json'
        }
        resp = requests.get(
            'https://backend.composio.dev/api/v3.1/toolkits?limit=5',
            headers=headers,
            timeout=10
        )
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
        headers = {
            'x-api-key': api_key,
            'Content-Type': 'application/json'
        }
        # Получаем список тулкитов
        resp = requests.get(
            'https://backend.composio.dev/api/v3.1/toolkits?limit=50',
            headers=headers,
            timeout=10
        )
        resp.raise_for_status()
        return jsonify({'integrations': resp.json()})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/composio/connected_accounts', methods=['GET'])
def composio_connected_accounts():
    """Список подключённых аккаунтов пользователя"""
    api_key = os.getenv('COMPOSIO_API_KEY', '')
    if not api_key:
        return jsonify({'error': 'Composio не подключен'}), 400
    try:
        headers = {'x-api-key': api_key, 'Content-Type': 'application/json'}
        resp = requests.get(
            'https://backend.composio.dev/api/v3.1/connected_accounts',
            headers=headers,
            timeout=10
        )
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
        headers = {
            'x-api-key': api_key,
            'Content-Type': 'application/json'
        }

        # ШАГ 1: Создаём Tool Router сессию
        session_resp = requests.post(
            'https://backend.composio.dev/api/v3.1/tool_router/session',
            headers=headers,
            json={"user_id": "novauser"},
            timeout=10
        )
        session_resp.raise_for_status()
        session_data = session_resp.json()
        session_id = session_data.get('session_id', '')

        if not session_id:
            return jsonify({'error': 'Не удалось создать сессию'}), 500

        # ШАГ 2: Через сессию получаем OAuth ссылку для тулкита
        link_resp = requests.post(
            f'https://backend.composio.dev/api/v3/tool_router/session/{session_id}/link',
            headers=headers,
            json={"toolkit": toolkit},
            timeout=10
        )
        link_resp.raise_for_status()
        link_data = link_resp.json()

        redirect_url = link_data.get('redirect_url', '')
        return jsonify({
            'success': True,
            'redirect_url': redirect_url,
            'connected_account_id': link_data.get('connected_account_id', '')
        })

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
        payload = {
            "input": params,
            "allow_tracing": True
        }
        if connected_account_id:
            payload["connected_account_id"] = connected_account_id

        resp = requests.post(
            f'https://backend.composio.dev/api/v2/actions/{action_name}/execute',
            json=payload,
            headers=headers,
            timeout=30
        )
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
# ========== ЗАПУСК ==========

if __name__ == '__main__':
    print("=" * 50)
    print("NovaMind AI Assistant запущен")
    print("http://localhost:5000")
    print("=" * 50)
    app.run(host='0.0.0.0', port=5000)
