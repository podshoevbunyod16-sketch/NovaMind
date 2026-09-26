"""
routes/commands.py — Маршрут /command (кастомные команды, AI команды)
"""
from flask import Blueprint, request, jsonify, session
import json
import os
import re
import time
from database import get_or_create_session_chat, get_chat_history, add_message
from ai_providers import groq_request_with_rotation
from groq_rotation import get_groq_key

commands_bp = Blueprint("commands", __name__)

@commands_bp.route('/command', methods=['POST'])
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

            img_dir = os.path.join(os.path.dirname(__file__), "generated_images")
            os.makedirs(img_dir, exist_ok=True)

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"image_{timestamp}.png"
            filepath = os.path.join(img_dir, filename)

            with open(filepath, "wb") as f:
                f.write(img_resp.content)

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
            data_resp, error = groq_request_with_rotation(
                provider["url"], payload, provider["headers"].copy()
            )
            if error:
                return jsonify({'error': error})
            reply = data_resp["choices"][0]["message"]["content"]
            contents.append({"role": "assistant", "content": reply})
            return jsonify({'result': reply})

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
        data_resp, error = groq_request_with_rotation(
            provider["url"], payload, provider["headers"].copy(), timeout=60
        )
        if error:
            return jsonify({'error': error})
        reply = data_resp["choices"][0]["message"]["content"]
        return jsonify({'result': reply})

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

        search_result = search_web(query)
        if not search_result:
            search_result = "Информация не найдена в интернете."

        provider = PROVIDERS[current_provider]
        analysis_prompt = f"""Проанализируй следующую информацию и выдели 3-5 ключевых фактов по вопросу: "{query}"

Информация из интернета:
{search_result}

Выдели только ключевые факты, коротко."""

        analysis_payload = {
            "model": current_model,
            "messages": [{"role": "user", "content": analysis_prompt}],
            "temperature": 0.3,
            "max_tokens": 1000000,
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
- Если есть сравнения, характеристики, данные, списки — ОБЯЗАТЕЛЬНО используй Markdown-таблицы
- Структурированный (с маркированными списками где уместно)
- На русском языке
- Укажи источники, если они есть в анализе

Пример таблицы:
| Характеристика | Значение |
|---------------|----------|
| Скорость | 100 км/ч |
| Вес | 10 кг |"""

        final_payload = {
            "model": current_model,
            "messages": [{"role": "user", "content": final_prompt}],
            "temperature": 0.5,
            "max_tokens": 1000000,
        }
        data_final, error = groq_request_with_rotation(
            provider["url"], final_payload, provider["headers"].copy(), timeout=90
        )
        if error:
            final_answer = f"**🔍 Результаты поиска:**\n\n{search_result}\n\n**📊 Анализ:**\n\n{analysis}\n\n_(Финальный ответ не удалось сгенерировать: {error})_"
        else:
            final_answer = data_final["choices"][0]["message"]["content"]

        return jsonify({'result': final_answer})

    return jsonify({'error': f'Неизвестная команда: /{cmd}'})

