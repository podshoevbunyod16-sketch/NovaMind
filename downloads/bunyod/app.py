import os
import sys
import json
import requests
import subprocess
import tempfile
import base64
from datetime import datetime
from flask import Flask, request, jsonify, render_template, send_file, session, redirect, url_for
from functools import wraps
from werkzeug.utils import secure_filename

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
            {"id": "meta-llama/llama-4-scout-17b-16e-instruct", "name": "Llama 4 Scout"},
            {"id": "qwen/qwen3-32b", "name": "Qwen 3 32B"},
            {"id": "llama-3.1-8b-instant", "name": "Llama 3.1 8B"},
            {"id": "groq/compound", "name": "Compound (автопоиск)"},
            {"id": "groq/compound-mini", "name": "Compound Mini (быстрый)"}
        ]
    },
    "cerebras": {
        "url": "https://api.cerebras.ai/v1/chat/completions",
        "headers": {
            "Authorization": f"Bearer {os.getenv('CEREBRAS_API_KEY')}",
            "Content-Type": "application/json"
        },
        "models": [
            {"id": "qwen3.6-plus-480b", "name": "Qwen3.6-Plus-480B (SWE-bench 71.2%)"},
            {"id": "llama-3.1-70b", "name": "Llama 3.1 70B"},
            {"id": "llama-3.3-70b", "name": "Llama 3.3 70B"},
            {"id": "llama-3.1-8b", "name": "Llama 3.1 8B"},
            {"id": "qwen3-32b", "name": "Qwen 3 32B"},
        ]
    },
    "openrouter": {

        "url": "https://openrouter.ai/api/v1/chat/completions",
        "headers": {
            "Authorization": f"Bearer {os.getenv('OPENROUTER_API_KEY')}",
            "Content-Type": "application/json",
            "HTTP-Referer": "http://localhost:5000", # OpenRouter просит указывать ваш сайт
            "X-Title": "My Awesome AI Assistant"      # Название вашего приложения
        },
        "models": [
            {"id": "mistralai/mistral-small-3.1-24b-instruct-2503", "name": "Mistral Small 3.1 (Free)"},
            {"id": "google/gemini-2.0-flash-001", "name": "Gemini 2.0 Flash (Free)"},
            {"id": "deepseek/deepseek-chat-v3-0324", "name": "DeepSeek R1 (Free)"},
            # Сюда можно добавлять любые модели с OpenRouter
        ]
    }
}


# ---------- Конфиг для генерации кода (всегда Cerebras) ----------
CODE_PROVIDER = "openrouter"
CODE_MODEL_DEFAULT = "deepseek/deepseek-chat-v3-0324"

current_provider = "openrouter"
current_model = "google/gemini-2.0-flash-001"

system_prompt = "Ты мой оичный ии ассистент всегда отвечай на русском языке, четкое объяснение, пошаговое обсуждение."
contents = []  # история сообщений
plugins = {}

# ---------- Админ конфиг ----------
ADMIN_CREDENTIALS = {
    "admin": ("007"),
    "user": ("008")
}
ADMIN_SESSION_KEY = ("007")

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

# ---------- Загрузка плагинов ----------
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
                print(f"Ошибка загрузки плагина {modname}: {e}")


load_plugins()

# ---------- Flask app ----------
app = Flask(__name__)
app.secret_key = ADMIN_SESSION_KEY

# ---------- Admin decorators ----------
def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("admin_logged_in"):
            return redirect("/admin/login")
        return f(*args, **kwargs)
    return decorated


def admin_api_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("admin_logged_in"):
            return jsonify({"error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated


# ---------- DuckDuckGo поиск ----------
def search_web(query):
    try:
        url = "https://api.duckduckgo.com/"
        params = {"q": query, "format": "json", "no_html": 1, "skip_disambig": 1, "t": "termux_assistant"}
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
    except Exception as e:
        print(f"Ошибка поиска: {e}")
        return None


# ---------- System prompt helper ----------
def ensure_system_prompt():
    """Убеждается, что system_prompt присутствует в contents[0]"""
    global contents, system_prompt
    if not contents:
        contents.insert(0, {"role": "system", "content": system_prompt})
        return
    if contents[0].get("role") == "system":
        contents[0]["content"] = system_prompt
    else:
        contents.insert(0, {"role": "system", "content": system_prompt})


def update_system_prompt(new_prompt):
    """Обновляет системный промпт и синхронизирует его в contents"""
    global system_prompt, contents
    system_prompt = new_prompt
    ensure_system_prompt()


# ---------- LLM helper (общий) ----------
def ask_llm(prompt, sys_prompt=None, temperature=0.7, max_tokens=3000, provider_key=None, model_id=None):
    """
    Универсальный helper для запросов к LLM.
    provider_key: если None — используется current_provider
    model_id: если None — используется current_model
    """
    pk = provider_key or current_provider
    mid = model_id or current_model
    
    if pk not in PROVIDERS:
        return f"Ошибка: провайдер {pk} не найден"
    
    provider = PROVIDERS[pk]
    messages = []
    if sys_prompt:
        messages.append({"role": "system", "content": sys_prompt})
    messages.append({"role": "user", "content": prompt})
    
    payload = {
        "model": mid,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    try:
        resp = requests.post(provider["url"], json=payload, headers=provider["headers"], timeout=60)
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]
    except Exception as e:
        return f"Ошибка LLM: {e}"


# ---------- Кастомные команды ----------
def exec_custom_command(cmd, args):
    global contents
    if cmd not in custom_commands:
        return None
    cc = custom_commands[cmd]
    if cc["type"] == "plugin":
        plugin_name = cc["plugin"]
        if plugin_name not in plugins:
            return f"Ошибка: плагин {plugin_name} не найден."
        full_args = list(cc.get("args_template", [])) + args
        try:
            result = plugins[plugin_name](full_args)
            return str(result) if result else "OK"
        except Exception as e:
            return f"Ошибка плагина: {e}"
    elif cc["type"] == "llm":
        prompt_template = cc.get("prompt", "{query}")
        query = " ".join(args) if args else ""
        rendered_prompt = prompt_template.replace("{query}", query)
        ensure_system_prompt()
        contents.append({"role": "user", "content": rendered_prompt})
        provider = PROVIDERS[current_provider]
        payload = {
            "model": current_model,
            "messages": contents,
            "temperature": 0.7,
            "max_tokens": 3000,
        }
        try:
            resp = requests.post(provider["url"], json=payload, headers=provider["headers"])
            resp.raise_for_status()
            data = resp.json()
            reply = data["choices"][0]["message"]["content"]
            contents.append({"role": "assistant", "content": reply})
            if len(contents) > 20:
                contents = contents[-20:]
            return reply
        except Exception as e:
            if contents and contents[-1]["role"] == "user":
                contents.pop()
            return f"Ошибка LLM: {e}"
    else:
        return f"Неизвестный тип алиаса: {cc['type']}"


# ========== ROUTES ==========

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/admin/login")
def admin_login_page():
    return render_template("admin.html")


@app.route("/admin")
@admin_required
def admin_dashboard():
    return render_template("admin.html")


# ---------- Admin API ----------

@app.route("/api/admin/login", methods=["POST"])
def admin_login():
    data = request.get_json() or {}
    username = data.get("username", "").strip()
    code = data.get("code", "").strip()
    if not username or not code:
        return jsonify({"error": "Введите логин и код"}), 400
    if username not in ADMIN_CREDENTIALS:
        return jsonify({"error": "Неверный логин"}), 401
    if ADMIN_CREDENTIALS[username] != code:
        return jsonify({"error": "Неверный код"}), 401
    session["admin_logged_in"] = True
    session["admin_username"] = username
    session.permanent = True
    return jsonify({"success": True, "username": username, "role": "admin" if username == "admin" else "user"})


@app.route("/api/admin/logout", methods=["POST"])
def admin_logout():
    session.clear()
    return jsonify({"success": True})


@app.route("/api/admin/check")
def admin_check():
    if session.get("admin_logged_in"):
        return jsonify({"logged_in": True, "username": session.get("admin_username"), "role": "admin" if session.get("admin_username") == "admin" else "user"})
    return jsonify({"logged_in": False})


@app.route("/api/admin/stats")
@admin_api_required
def admin_stats():
    return jsonify({
        "models": PROVIDERS,
        "current_provider": current_provider,
        "current_model": current_model,
        "code_provider": CODE_PROVIDER,
        "code_model": CODE_MODEL_DEFAULT,
        "history_messages": len(contents),
        "plugins_loaded": list(plugins.keys()),
        "custom_commands": list(custom_commands.keys()),
        "system_prompt": system_prompt,
        "voice_enabled": os.environ.get("ASSISTANT_VOICE_REPLY", "0") == "1"
    })


@app.route("/api/admin/settings", methods=["POST"])
@admin_api_required
def admin_settings():
    global system_prompt, current_provider, current_model
    data = request.get_json() or {}
    if "system_prompt" in data:
        update_system_prompt(data["system_prompt"])
    if "provider" in data and data["provider"] in PROVIDERS:
        current_provider = data["provider"]
    if "model" in data:
        for prov, pdata in PROVIDERS.items():
            for m in pdata["models"]:
                if m["id"] == data["model"]:
                    current_provider = prov
                    current_model = data["model"]
                    break
    if "voice" in data:
        os.environ["ASSISTANT_VOICE_REPLY"] = "1" if data["voice"] else "0"
    return jsonify({"success": True, "current_provider": current_provider, "current_model": current_model, "system_prompt": system_prompt})


@app.route("/api/admin/history", methods=["GET", "DELETE"])
@admin_api_required
def admin_history():
    global contents
    if request.method == "DELETE":
        contents.clear()
        ensure_system_prompt()
        return jsonify({"success": True, "message": "История очищена"})
    return jsonify({"history": contents})


@app.route("/api/admin/export")
@admin_api_required
def admin_export():
    export_data = {
        "history": contents,
        "settings": {
            "provider": current_provider,
            "model": current_model,
            "code_provider": CODE_PROVIDER,
            "code_model": CODE_MODEL_DEFAULT,
            "system_prompt": system_prompt,
            "voice_enabled": os.environ.get("ASSISTANT_VOICE_REPLY", "0") == "1"
        },
        "custom_commands": custom_commands,
        "plugins": list(plugins.keys())
    }
    return jsonify(export_data)


# ---------- Chat API ----------

@app.route("/models_list")
def models_list():
    providers_list = []
    for provider_key, provider_data in PROVIDERS.items():
        providers_list.append({
            "provider": provider_key,
            "list": provider_data["models"]
        })
    return jsonify({"models": providers_list, "current": current_model})


@app.route("/switch_model")
def switch_model():
    global current_provider, current_model
    model_id = request.args.get("model_id", "")
    for provider_key, provider_data in PROVIDERS.items():
        for model in provider_data["models"]:
            if model["id"] == model_id:
                current_provider = provider_key
                current_model = model_id
                return jsonify({"success": True, "provider": provider_key, "model": model_id})
    return jsonify({"error": "Модель не найдена"}), 400


@app.route("/commands_list")
def commands_list():
    cmds = []
    for name, cc in custom_commands.items():
        cmds.append({"command": f"/{name}", "label": f"{name}"})
    if "monitor" in plugins:
        cmds.append({"command": "/monitor battery", "label": "Батарея"})
        cmds.append({"command": "/monitor memory", "label": "Память"})
    if "services" in plugins:
        cmds.append({"command": "/services weather Москва", "label": "Погода"})
        cmds.append({"command": "/services currency USD RUB", "label": "Валюта"})
        cmds.append({"command": "/services wiki Linux", "label": "Wiki"})
    if "snippet" in plugins:
        cmds.append({"command": "/snippet list", "label": "Сниппеты"})
    if "git" in plugins:
        cmds.append({"command": "/git /sdcard/Documents", "label": "GitHub"})
    if "voice" in plugins:
        cmds.append({"command": "/voice on", "label": "Озвучка вкл"})
        cmds.append({"command": "/voice off", "label": "Озвучка выкл"})
    cmds.append({"command": "/search ", "label": "Поиск в интернете"})
    cmds.extend([
        {"command": "/code ", "label": "Генератор кода (Cerebras)"},
        {"command": "/clear", "label": "Очистить"},
        {"command": "/save", "label": "Сохранить"},
        {"command": "/load", "label": "Загрузить"},
        {"command": "/history", "label": "История"},
        {"command": "/help", "label": "Помощь"},
        {"command": "/alias", "label": "Мои команды"},
    ])
    if "analyze_image" in plugins:
        cmds.append({"command": "/analyze ", "label": "Анализ фото"})
    return jsonify({"commands": cmds})


@app.route("/send", methods=["POST"])
def send():
    global contents
    data = request.get_json()
    message = data.get("message", "").strip()
    if not message:
        return jsonify({"error": "Пустое сообщение"})

    ensure_system_prompt()
    contents.append({"role": "user", "content": message})
    provider = PROVIDERS[current_provider]
    payload = {
        "model": current_model,
        "messages": contents,
        "temperature": 0.7,
        "max_tokens": 3000,
    }
    try:
        resp = requests.post(provider["url"], json=payload, headers=provider["headers"])
        resp.raise_for_status()
        data_resp = resp.json()
        reply = data_resp["choices"][0]["message"]["content"]
        contents.append({"role": "assistant", "content": reply})
        if len(contents) > 20:
            contents = contents[-20:]
        voice_enabled = os.environ.get("ASSISTANT_VOICE_REPLY", "0") == "1"
        return jsonify({"reply": reply, "voice_enabled": voice_enabled})
    except Exception as e:
        if contents and contents[-1]["role"] == "user":
            contents.pop()
        return jsonify({"error": str(e)})


@app.route("/command", methods=["POST"])
def handle_command():
    global contents, system_prompt
    data = request.get_json()
    cmd_line = data.get("command", "").strip()
    if not cmd_line.startswith("/"):
        return jsonify({"error": "Команда должна начинаться с /"})

    parts = cmd_line[1:].split(maxsplit=1)
    cmd = parts[0].lower()
    args = parts[1].split() if len(parts) > 1 else []

    # Кастомные команды (алиасы)
    custom_result = exec_custom_command(cmd, args)
    if custom_result is not None:
        return jsonify({"result": custom_result})

    # Управление алиасами
    if cmd == "alias":
        if not args:
            if not custom_commands:
                return jsonify({"result": "Нет пользовательских команд. Добавьте через /alias add <имя> plugin <плагин> [аргументы] или /alias add <имя> llm <промпт>."})
            info = "Ваши команды:\n"
            for name, cc in custom_commands.items():
                if cc["type"] == "plugin":
                    info += f"/{name} -> плагин {cc['plugin']} {cc.get('args_template', [])}\n"
                else:
                    info += f"/{name} -> свой промпт\n"
            return jsonify({"result": info})
        subcmd = args[0].lower()
        if subcmd == "add":
            if len(args) < 3:
                return jsonify({"error": "Формат: /alias add <имя> plugin <имя_плагина> [аргументы] или /alias add <имя> llm <промпт>"})
            name = args[1]
            type_ = args[2].lower()
            if type_ not in ("plugin", "llm"):
                return jsonify({"error": "Тип должен быть plugin или llm"})
            if type_ == "plugin":
                if len(args) < 4:
                    return jsonify({"error": "Укажите имя плагина после plugin"})
                plugin_name = args[3]
                preset_args = args[4:] if len(args) > 4 else []
                custom_commands[name] = {"type": "plugin", "plugin": plugin_name, "args_template": preset_args}
            else:
                prompt = " ".join(args[3:]) if len(args) > 3 else "{query}"
                custom_commands[name] = {"type": "llm", "prompt": prompt}
            save_custom_commands()
            return jsonify({"result": f"Команда /{name} добавлена. Перезагрузите страницу для обновления меню."})
        elif subcmd == "remove":
            if len(args) < 2:
                return jsonify({"error": "Укажите имя команды для удаления"})
            name = args[1]
            if name in custom_commands:
                del custom_commands[name]
                save_custom_commands()
                return jsonify({"result": f"Команда /{name} удалена."})
            else:
                return jsonify({"error": f"Команда /{name} не найдена."})
        else:
            return jsonify({"error": "Используйте: /alias [add|remove] <имя> ..."})

    # Плагины
    if cmd in plugins:
        try:
            result = plugins[cmd](args)
            return jsonify({"result": result if result else "OK"})
        except Exception as e:
            return jsonify({"error": str(e)})

    # Встроенные команды
    if cmd == "help":
        return jsonify({"result": "Используйте меню Функции или пишите запросы. /alias для своих команд. /code для генерации кода через Cerebras."})
    elif cmd == "clear":
        contents.clear()
        ensure_system_prompt()
        return jsonify({"result": "История очищена."})
    elif cmd == "history":
        if not contents:
            return jsonify({"result": "История пуста."})
        hist = "\n\n".join([f"**{msg['role']}**: {msg['content']}" for msg in contents])
        return jsonify({"result": hist})
    elif cmd == "save":
        filename = args[0] if args else "chat_history.json"
        try:
            with open(filename, "w", encoding="utf-8") as f:
                json.dump(contents, f, ensure_ascii=False, indent=2)
            return jsonify({"result": f"Сохранено в {filename}"})
        except Exception as e:
            return jsonify({"error": str(e)})
    elif cmd == "load":
        filename = args[0] if args else "chat_history.json"
        if not os.path.exists(filename):
            return jsonify({"error": "Файл не найден"})
        try:
            with open(filename, "r", encoding="utf-8") as f:
                contents = json.load(f)
            ensure_system_prompt()
            return jsonify({"result": f"Загружено из {filename}"})
        except Exception as e:
            return jsonify({"error": str(e)})
    elif cmd == "prompt":
        if args:
            update_system_prompt(" ".join(args))
            return jsonify({"result": "Промпт обновлён."})
        else:
            return jsonify({"result": f"Текущий: {system_prompt}"})
    elif cmd == "system":
        command = " ".join(args)
        if not command:
            return jsonify({"error": "Укажите команду"})
        try:
            res = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=30)
            return jsonify({"result": res.stdout + res.stderr})
        except subprocess.TimeoutExpired:
            return jsonify({"error": "Превышено время"})
        except Exception as e:
            return jsonify({"error": str(e)})
    elif cmd == "voice":
        if not args:
            return jsonify({"error": "Используйте /voice on/off"})
        subcmd = args[0].lower()
        if subcmd == "on":
            os.environ["ASSISTANT_VOICE_REPLY"] = "1"
            return jsonify({"result": "Озвучка включена"})
        elif subcmd == "off":
            os.environ["ASSISTANT_VOICE_REPLY"] = "0"
            return jsonify({"result": "Озвучка отключена"})
        else:
            return jsonify({"error": "Неизвестная подкоманда"})
    elif cmd == "search":
        search_query = " ".join(args)
        if not search_query:
            return jsonify({"error": "Укажите запрос"})
        search_context = search_web(search_query)
        ensure_system_prompt()
        if search_context:
            contents.append({"role": "system", "content": f"Контекст из интернета:\n{search_context}"})
            contents.append({"role": "user", "content": f"Ответь на вопрос, используя контекст: {search_query}"})
        else:
            contents.append({"role": "user", "content": search_query})
        provider = PROVIDERS[current_provider]
        payload = {"model": current_model, "messages": contents, "temperature": 0.7, "max_tokens": 3000}
        try:
            resp = requests.post(provider["url"], json=payload, headers=provider["headers"])
            resp.raise_for_status()
            data_resp = resp.json()
            reply = data_resp["choices"][0]["message"]["content"]
            contents.append({"role": "assistant", "content": reply})
            if len(contents) > 20:
                contents = contents[-20:]
            return jsonify({"result": reply, "voice_enabled": os.environ.get("ASSISTANT_VOICE_REPLY", "0") == "1"})
        except Exception as e:
            if contents and contents[-1]["role"] == "user":
                contents.pop()
            if contents and contents[-1]["role"] == "system":
                contents.pop()
            return jsonify({"error": str(e)})
    elif cmd == "code":
        query = " ".join(args)
        if not query:
            return jsonify({"error": "Укажите, какой код нужно создать. Например: /code функция сортировки на Python"})
        ensure_system_prompt()
        contents.append({"role": "user", "content": f"/code {query}"})

        code_sys_prompt = (
            "Ты опытный программист. Пиши только чистый код без лишних пояснений. "
            "Добавь комментарии на русском. Если просят конкретный язык — используй его. "
            "Выводи код в блоке ```язык ... ```"
        )
        
        # ===== ИСПОЛЬЗУЕМ CEREBRAS ДЛЯ ГЕНЕРАЦИИ КОДА =====
        generated = ask_llm(
            f"Напиши код: {query}",
            sys_prompt=code_sys_prompt,
            temperature=0.3,
            max_tokens=3000,
            provider_key=CODE_PROVIDER,
            model_id=CODE_MODEL_DEFAULT
        )

        # Извлекаем код из markdown блока
        code = generated
        if "```" in generated:
            parts = generated.split("```")
            for i, p in enumerate(parts):
                if i > 0 and p.strip():
                    lines = p.strip().split("\n")
                    if lines and (lines[0].isalpha() or (" " in lines[0] and len(lines[0]) < 20)):
                        lines = lines[1:]
                    code = "\n".join(lines).strip()
                    break

        # Сохраняем во временный файл
        tmp_dir = os.path.join(os.path.dirname(__file__), "generated_codes")
        os.makedirs(tmp_dir, exist_ok=True)

        ext = ".py"
        q_lower = query.lower()
        if "javascript" in q_lower or " js" in q_lower:
            ext = ".js"
        elif "typescript" in q_lower or " ts" in q_lower:
            ext = ".ts"
        elif "html" in q_lower:
            ext = ".html"
        elif "css" in q_lower:
            ext = ".css"
        elif "rust" in q_lower or " rs" in q_lower:
            ext = ".rs"
        elif "go" in q_lower or "golang" in q_lower:
            ext = ".go"
        elif "c++" in q_lower or "cpp" in q_lower:
            ext = ".cpp"
        elif "java" in q_lower:
            ext = ".java"
        elif "bash" in q_lower or "shell" in q_lower:
            ext = ".sh"
        elif "json" in q_lower:
            ext = ".json"
        elif "sql" in q_lower:
            ext = ".sql"

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"generated_{timestamp}{ext}"
        filepath = os.path.join(tmp_dir, filename)
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(code)

        contents.append({"role": "assistant", "content": generated})
        if len(contents) > 20:
            contents = contents[-20:]

        return jsonify({
            "result": generated,
            "code": code,
            "filename": filename,
            "filepath": filepath,
            "download_url": f"/download?file={filename}",
            "model_used": f"{CODE_PROVIDER}/{CODE_MODEL_DEFAULT}"
        })
    else:
        return jsonify({"error": f"Неизвестная команда: /{cmd}"})


# ---------- Download endpoint ----------

@app.route("/download")
def download_file():
    filename = request.args.get("file", "")
    if not filename:
        return jsonify({"error": "No filename"}), 400

    safe_name = secure_filename(filename)
    tmp_dir = os.path.join(os.path.dirname(__file__), "generated_codes")
    filepath = os.path.join(tmp_dir, safe_name)

    if not os.path.abspath(filepath).startswith(os.path.abspath(tmp_dir)):
        return jsonify({"error": "Invalid filename"}), 400

    if not os.path.exists(filepath):
        return jsonify({"error": "File not found"}), 404

    return send_file(filepath, as_attachment=True, download_name=safe_name)

# ---------- Локальный запуск кода ----------

@app.route("/run_code", methods=["POST"])
def run_code():
    data = request.get_json()
    code = data.get("code", "")
    language = data.get("language", "python")
    
    if not code:
        return jsonify({"error": "Нет кода для выполнения"}), 400
    
    import tempfile
    import os
    
    try:
        # Сохраняем код во временный файл
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write(code)
            temp_path = f.name
        
        # Запускаем локально через python3
        result = subprocess.run(
            ["python3", temp_path],
            capture_output=True,
            text=True,
            timeout=10
        )
        
        # Удаляем временный файл
        os.unlink(temp_path)
        
        # Собираем вывод
        output = ""
        if result.stdout:
            output += result.stdout
        if result.stderr:
            if output:
                output += "\n"
            output += "STDERR: " + result.stderr
        
        if not output:
            output = "Код выполнен без вывода"
        
        return jsonify({"result": output})
        
    except subprocess.TimeoutExpired:
        try:
            os.unlink(temp_path)
        except:
            pass
        return jsonify({"error": "Превышено время выполнения (10 секунд)"}), 408
    except Exception as e:
        try:
            os.unlink(temp_path)
        except:
            pass
        return jsonify({"error": f"Ошибка: {str(e)}"}), 500
#генерация изображения
@app.route("/generated_image")
def generated_image():
    filename = request.args.get("file", "")
    if not filename:
        return jsonify({"error": "No filename"}), 400

    safe_name = secure_filename(filename)
    img_dir = os.path.join(os.path.dirname(__file__), "generated_images")
    filepath = os.path.join(img_dir, safe_name)

    if not os.path.exists(filepath):
        return jsonify({"error": "File not found"}), 404

    return send_file(filepath, mimetype='image/png')
# ---------- Admin Code Editor ----------

@app.route("/api/admin/save_code", methods=["POST"])
@admin_api_required
def admin_save_code():
    data = request.get_json() or {}
    filename = data.get("filename", "script.py")
    code = data.get("code", "")
    
    if not code:
        return jsonify({"error": "Нет кода"}), 400
    
    safe_name = secure_filename(filename)
    if not safe_name.endswith(".py"):
        safe_name += ".py"
    
    code_dir = os.path.join(os.path.dirname(__file__), "saved_codes")
    os.makedirs(code_dir, exist_ok=True)
    
    filepath = os.path.join(code_dir, safe_name)
    
    try:
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(code)
        return jsonify({"success": True, "filepath": filepath, "filename": safe_name})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/admin/run_saved_code", methods=["POST"])
@admin_api_required
def admin_run_saved_code():
    data = request.get_json() or {}
    filename = data.get("filename", "script.py")
    
    safe_name = secure_filename(filename)
    code_dir = os.path.join(os.path.dirname(__file__), "saved_codes")
    filepath = os.path.join(code_dir, safe_name)
    
    if not os.path.exists(filepath):
        return jsonify({"error": "Файл не найден"}), 404
    
    try:
        result = subprocess.run(
            ["python3", filepath],
            capture_output=True,
            text=True,
            timeout=10
        )
        output = result.stdout
        if result.stderr:
            if output:
                output += "\n"
            output += "STDERR: " + result.stderr
        return jsonify({"result": output or "Нет вывода"})
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Превышено время выполнения (10 сек)"}), 408
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/admin/saved_codes")
@admin_api_required
def admin_saved_codes():
    code_dir = os.path.join(os.path.dirname(__file__), "saved_codes")
    if not os.path.exists(code_dir):
        return jsonify({"files": []})
    files = sorted([f for f in os.listdir(code_dir) if f.endswith(".py")])
    return jsonify({"files": files})


@app.route("/api/admin/load_code")
@admin_api_required
def admin_load_code():
    filename = request.args.get("file", "")
    safe_name = secure_filename(filename)
    code_dir = os.path.join(os.path.dirname(__file__), "saved_codes")
    filepath = os.path.join(code_dir, safe_name)
    
    if not os.path.exists(filepath):
        return jsonify({"error": "Файл не найден"}), 404
    
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            code = f.read()
        return jsonify({"code": code, "filename": safe_name})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
#загрузка изображений и анализ
@app.route("/upload_image", methods=["POST"])
def upload_image():
    if 'image' not in request.files:
        return jsonify({"error": "Нет файла"}), 400
    
    file = request.files['image']
    if file.filename == '':
        return jsonify({"error": "Файл не выбран"}), 400
    
    safe_name = secure_filename(file.filename)
    upload_dir = os.path.join(os.path.dirname(__file__), "uploads")
    os.makedirs(upload_dir, exist_ok=True)
    
    filepath = os.path.join(upload_dir, safe_name)
    file.save(filepath)
    
    return jsonify({"success": True, "filepath": filepath})
# ---------- Share file to chat ----------

@app.route("/share_to_chat", methods=["POST"])
def share_to_chat():
    data = request.get_json()
    filename = data.get("filename", "")
    if not filename:
        return jsonify({"error": "No filename"}), 400

    safe_name = secure_filename(filename)
    tmp_dir = os.path.join(os.path.dirname(__file__), "generated_codes")
    filepath = os.path.join(tmp_dir, safe_name)

    if not os.path.abspath(filepath).startswith(os.path.abspath(tmp_dir)):
        return jsonify({"error": "Invalid filename"}), 400

    if not os.path.exists(filepath):
        return jsonify({"error": "File not found"}), 404

    with open(filepath, "r", encoding="utf-8") as f:
        code = f.read()

    return jsonify({
        "filename": safe_name,
        "code": code,
        "message": f"Файл `{safe_name}` готов к отправке в чат"
    })


if __name__ == "__main__":
    print("=" * 50)
    print("AI Assistant Server запущен")
    print("Frontend: http://localhost:5000")
    print("Admin:    http://localhost:5000/admin/login")
    print("=" * 50)
    app.run(host="0.0.0.0", port=5000, debug=False)
