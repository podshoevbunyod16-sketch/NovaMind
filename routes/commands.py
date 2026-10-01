"""
routes/commands.py — Маршрут /command (кастомные команды, AI команды)
"""
from flask import Blueprint, request, jsonify, session
import json
import os
import re
import time
import requests
from datetime import datetime
from database import (get_or_create_session_chat, get_chat_history, add_message,
                      create_chat)
from ai_providers import groq_request_with_rotation
from groq_rotation import get_groq_key
from i18n import with_language
from routes.search import search_web
from config import PROVIDERS, plugins, save_custom_commands
import config
import media_generation as mg

# Кастомные алиасы живут в config.CUSTOM_ALIASES: обращаемся к тому же словарю,
# чтобы /alias add/remove сохраняли изменения.
custom_commands = config.CUSTOM_ALIASES

commands_bp = Blueprint("commands", __name__)


def _chat_contents(limit=50):
    """История текущего чата в формате messages (заменяет удалённый global contents)."""
    chat_id = get_or_create_session_chat()
    return chat_id, get_chat_history(chat_id, limit=limit)

@commands_bp.route('/command', methods=['POST'])
def handle_command():
    """Обработка команд (/search, /code, /image, плагины, алиасы)"""
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

    # Генерация изображений через единый медиа-движок
    if cmd == "image":
        prompt = " ".join(args)
        if not prompt:
            return jsonify({'error': 'Укажите описание изображения'})

        # Приоритет: выбранная в панели «Медиа» image-модель,
        # иначе бесплатная модель без ключа (Pollinations Flux).
        selection = mg.current_selection()
        model, error = (None, None)
        if selection.get("media_type") == "image":
            model, error = mg.find_media_model(selection.get("provider"), selection.get("model"),
                                               include_paid=True, include_unknown=True)
        if model is None:
            model = {
                "id": "flux", "name": "Pollinations Flux", "provider": "pollinations",
                "provider_name": mg.MEDIA_PROVIDERS["pollinations"], "media_type": "image",
                "backend": "pollinations_image", "pricing_status": mg.PRICING_FREE,
                "pricing_label": mg.PRICING_LABELS[mg.PRICING_FREE],
                "provider_connected": True, "in_provider_catalog": None,
            }
        result, error = mg.generate_media(model, prompt, confirm={"paid": True, "trial": True,
                                                                  "unknown": True})
        if error:
            return jsonify({'error': f'Ошибка генерации: {error}'}), 502
        return jsonify({'result': f'✅ Изображение сгенерировано:\n\n![Image]({result["url"]})'})

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
            chat_id, history = _chat_contents()
            add_message(chat_id, "user", rendered_prompt)
            provider = PROVIDERS[config.current_provider]
            payload = {
                "model": config.current_model,
                "messages": [{"role": "system", "content": with_language(config.system_prompt)}] + history +
                          [{"role": "user", "content": rendered_prompt}],
                "temperature": 0.7,
                "max_tokens": 3000,
            }
            data_resp, error = groq_request_with_rotation(
                provider["url"], payload, provider["headers"].copy()
            )
            if error:
                return jsonify({'error': error})
            reply = data_resp["choices"][0]["message"]["content"]
            add_message(chat_id, "assistant", reply)
            return jsonify({'result': reply})

    # Встроенные команды
    if cmd == "clear":
        session['chat_id'] = create_chat()
        return jsonify({'result': 'История очищена'})

    if cmd == "history":
        _chat_id, history = _chat_contents()
        if not history:
            return jsonify({'result': 'История пуста'})
        hist = "\n\n".join([f"**{msg['role']}**: {msg['content']}" for msg in history])
        return jsonify({'result': hist})

    if cmd == "code":
        query = " ".join(args)
        if not query:
            return jsonify({'error': 'Укажите, какой код создать'})

        provider = PROVIDERS[config.current_provider]
        payload = {
            "model": config.current_model,
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

        provider = PROVIDERS[config.current_provider]
        analysis_prompt = f"""Проанализируй следующую информацию и выдели 3-5 ключевых фактов по вопросу: "{query}"

Информация из интернета:
{search_result}

Выдели только ключевые факты, коротко."""

        analysis_payload = {
            "model": config.current_model,
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
            "model": config.current_model,
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

