"""
routes/chat.py — Маршруты чата: /send, /send_stream, /api/chats/*, /api/history/*
"""
from flask import Blueprint, request, jsonify, session, Response, stream_with_context
import json
import queue
import threading
import time
import os
import requests

from database import (get_chat_history, add_message, trim_messages,
                      get_or_create_session_chat, create_chat,
                      list_chats, delete_chat)
from ai_providers import gemini_stream_request, gemini_request
from ai_providers import groq_request_with_rotation
from groq_rotation import get_groq_key, mark_groq_key_exhausted, GROQ_KEYS
import config
import media_generation as mg

# In-memory compatibility cache. Persistent chat history is stored in the database.
contents = []

chat_bp = Blueprint("chat", __name__)

# Маркер медиарезультата в истории: тот же приём, что COMPOSIO_CARDS: в app.js.
MEDIA_RESULT_PREFIX = "MEDIA_RESULT:"


def media_result_marker(result: dict, model: dict) -> str:
    """Строка для БД, по которой фронтенд снова отрисует медиа."""
    payload = {
        "kind": result.get("kind"),
        "url": result.get("url"),
        "filename": result.get("filename"),
        "mime": result.get("mime"),
        "title": result.get("model_name") or result.get("model"),
        "provider": result.get("provider_name") or result.get("provider"),
        "pricing_status": result.get("pricing_status"),
        "job_id": result.get("job_id"),
        "state": result.get("state"),
        "elapsed_ms": result.get("elapsed_ms"),
        "model": mg.selection_payload(model) if model else None,
    }
    return MEDIA_RESULT_PREFIX + json.dumps(payload, ensure_ascii=False)


def media_summary_text(result: dict) -> str:
    """Человекочитаемое описание результата (видно в списке чатов и в логах)."""
    if result.get("job_id"):
        return (f"⏳ Задача {result.get('kind')} поставлена в очередь провайдера "
                f"({result.get('job_id')}).")
    kind = {"image": "Изображение", "audio": "Аудио", "video": "Видео"}.get(result.get("kind"), "Файл")
    return f"✅ {kind} готово: {result.get('filename') or result.get('url')}"


def media_generation_context(data: dict):
    """
    Возвращает (model, confirm, error) если сообщение нужно отправить
    в медиа-движок, иначе (None, None, None).
    """
    if not data.get('media'):
        return None, None, None
    return mg.resolve_selection(data.get('provider'), data.get('model'), data.get('confirm'))


def _media_stream_response(model, prompt, confirm, options, chat_id):
    """
    Потоковый ответ генерации медиа.

    В поток уходят только реальные события: шаги запроса к провайдеру,
    ответ провайдера, сохранение файла или дословная ошибка.
    Процентов и выдуманного прогресса здесь нет.
    """
    global contents
    events: "queue.Queue" = queue.Queue()
    started = time.time()

    def worker():
        result, error = mg.generate_media(
            model, prompt, options=options, confirm=confirm,
            on_status=lambda message: events.put(("status", message)),
        )
        events.put(("done", (result, error)))

    thread = threading.Thread(target=worker, name="media-generation", daemon=True)

    def emit(payload: dict) -> str:
        return json.dumps(payload, ensure_ascii=False) + "\n"

    @stream_with_context
    def generate():
        thread.start()
        yield emit({
            "status": "started",
            "message": f"Медиа-модель: {model.get('name')} ({model.get('provider_name')})",
            "model": mg.selection_payload(model),
            "elapsed_ms": 0,
        })
        result = None
        error = None
        while True:
            kind, payload = events.get()
            elapsed = int((time.time() - started) * 1000)
            if kind == "status":
                yield emit({"status": "progress", "message": payload, "elapsed_ms": elapsed})
                continue
            result, error = payload
            break

        if error:
            add_message(chat_id, "assistant", f"❌ {error}")
            yield emit({"error": error, "elapsed_ms": int((time.time() - started) * 1000)})
            return

        marker = media_result_marker(result, model)
        add_message(chat_id, "assistant", marker)
        contents.append({"role": "assistant", "content": marker})
        if len(contents) > 20:
            del contents[:-20]
        yield emit({
            "status": "done",
            "media": result,
            "message": media_summary_text(result),
            "model": mg.selection_payload(model),
            "elapsed_ms": result.get("elapsed_ms") or int((time.time() - started) * 1000),
        })
        yield emit({"done": True})

    return Response(
        generate(),
        mimetype="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


@chat_bp.route('/send', methods=['POST'])
def send():
    """Отправка сообщения к ИИ с сохранением в БД"""
    global contents
    data = request.get_json()
    message = data.get('message', '').strip()
    reasoning = data.get('reasoning', False)
    chat_id = data.get('chat_id') or get_or_create_session_chat()

    if not message:
        return jsonify({'error': 'Пустое сообщение'})

    # FIX: загружаем историю из БД
    history = get_chat_history(chat_id, limit=50)
    add_message(chat_id, "user", message)

    # ===== Генерация медиа выбранной моделью вместо текстового ответа =====
    model, confirm, error = media_generation_context(data)
    if error:
        return jsonify({'error': error}), 400
    if model is not None:
        result, error = mg.generate_media(
            model, message, options=data.get('options') or {}, confirm=confirm
        )
        if error:
            add_message(chat_id, "assistant", f"❌ {error}")
            return jsonify({'error': error}), 502
        marker = media_result_marker(result, model)
        add_message(chat_id, "assistant", marker)
        trim_messages(chat_id, max_messages=100)
        contents = get_chat_history(chat_id, limit=20)
        return jsonify({
            'reply': media_summary_text(result),
            'media': result,
            'model': mg.selection_payload(model),
            'chat_id': chat_id,
        })

    if reasoning and os.getenv("CEREBRAS_API_KEY"):
        provider = config.PROVIDERS["cerebras"]
        model = "zai-glm-4.7"
    else:
        provider = config.PROVIDERS[config.current_provider]
        model = config.current_model

    # Берём max_tokens из настроек провайдера (у каждого свой аппаратный лимит)
    provider_max = provider.get("max_tokens", 8192)
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": config.system_prompt}] + history + [{"role": "user", "content": message}],
        "temperature": 0.7,
        "max_tokens": provider_max,
    }

    data_resp, error = groq_request_with_rotation(
        provider["url"], payload, provider["headers"].copy(), timeout=90
    )
    if error:
        return jsonify({'error': error})

    reply = data_resp["choices"][0]["message"]["content"]
    add_message(chat_id, "assistant", reply)
    trim_messages(chat_id, max_messages=100)

    # Совместимость: обновляем contents для других endpoint'ов
    contents = get_chat_history(chat_id, limit=20)

    return jsonify({'reply': reply, 'chat_id': chat_id})


@chat_bp.route('/send_stream', methods=['POST'])
def send_stream():
    """Потоковый чат с надёжной обработкой Groq rate-limit/auth ошибок."""
    global contents
    data = request.get_json() or {}
    message = data.get('message', '').strip()
    reasoning = data.get('reasoning', False)

    if not message:
        return jsonify({'error': 'Пустое сообщение'}), 400

    contents.append({"role": "user", "content": message})

    if reasoning and os.getenv("CEREBRAS_API_KEY"):
        provider = config.PROVIDERS["cerebras"]
        model = "zai-glm-4.7"
    else:
        provider = config.PROVIDERS[config.current_provider]
        model = config.current_model

    # FIX: загружаем историю из БД для send_stream тоже
    _chat_id_stream = get_or_create_session_chat()
    _history_stream = get_chat_history(_chat_id_stream, limit=50)
    add_message(_chat_id_stream, "user", message)

    # ===== Генерация медиа: поток реальных статусов запроса =====
    model, confirm, media_error = media_generation_context(data)
    if media_error:
        if contents and contents[-1]["role"] == "user":
            contents.pop()
        return jsonify({'error': media_error}), 400
    if model is not None:
        return _media_stream_response(
            model, message, confirm, data.get('options') or {}, _chat_id_stream
        )

    # Берём max_tokens из настроек провайдера (у каждого свой аппаратный лимит)
    _provider_max = provider.get("max_tokens", 8192)
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": config.system_prompt}] + _history_stream + [{"role": "user", "content": message}],
        "temperature": 0.7,
        "max_tokens": _provider_max,
        "stream": True,
    }

    # Для Groq делаем несколько попыток с ротацией ключей.
    upstream = None
    last_error = None
    is_gemini = provider.get("url","").startswith("https://generativelanguage.googleapis.com")
    if is_gemini:
        upstream, gemini_error = gemini_stream_request(model, payload, timeout=90)

        # Некоторые сети/прокси могут блокировать SSE, хотя обычный generateContent
        # работает. В таком случае не отдаём 502: выполняем обычный Gemini-запрос
        # и возвращаем его как один потоковый token.
        if upstream is None:
            print(f"[Gemini stream] {gemini_error}")
            fallback_data, fallback_error = gemini_request(model, payload, timeout=90, max_retries=2)
            if fallback_error:
                if contents and contents[-1]["role"] == "user":
                    contents.pop()
                return jsonify({
                    "error": f"{gemini_error}. Fallback generateContent: {fallback_error}"
                }), 502

            fallback_reply=((fallback_data.get("choices") or [{}])[0].get("message") or {}).get("content","")
            if contents and contents[-1]["role"] == "user":
                contents.pop()

            def generate_fallback():
                if fallback_reply:
                    yield json.dumps({"token": fallback_reply}, ensure_ascii=False) + "\n"
                    contents.append({"role":"assistant","content":fallback_reply})
                    if len(contents) > 20:
                        del contents[:-20]
                yield json.dumps({"done":True}, ensure_ascii=False) + "\n"

            return Response(
                generate_fallback(),
                mimetype="application/x-ndjson",
                headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no-cache"}
            )
    else:
        upstream = None
    max_attempts = max(1, min(len(GROQ_KEYS), 9)) if "api.groq.com" in provider["url"] else 1

    for _ in range(0 if upstream is not None else max_attempts):
        headers = provider["headers"].copy()
        if "api.groq.com" in provider["url"]:
            key = get_groq_key()
            if not key:
                last_error = "Нет доступных Groq API ключей"
                break
            headers["Authorization"] = f"Bearer {key}"

        try:
            candidate = requests.post(
                provider["url"], json=payload, headers=headers, timeout=90, stream=True
            )
            if candidate.status_code in (401, 429) and "api.groq.com" in provider["url"]:
                last_error = f"Groq HTTP {candidate.status_code}"
                mark_groq_key_exhausted()
                candidate.close()
                continue
            candidate.raise_for_status()
            candidate.encoding = "utf-8"
            upstream = candidate
            break
        except requests.exceptions.RequestException as exc:
            last_error = str(exc)
            if "api.groq.com" in provider["url"]:
                mark_groq_key_exhausted()
                continue
            break

    if upstream is None:
        if contents and contents[-1]["role"] == "user":
            contents.pop()
        return jsonify({'error': f'Ошибка подключения к AI: {last_error or "неизвестная ошибка"}'}), 502

    @stream_with_context
    def generate():
        full_reply = ""
        try:
            for raw_line in upstream.iter_lines(decode_unicode=True):
                if not raw_line:
                    continue
                line = raw_line.strip()
                if line.startswith("data:"):
                    line = line[5:].strip()
                if line == "[DONE]":
                    break
                try:
                    chunk = json.loads(line)
                except (TypeError, json.JSONDecodeError):
                    continue

                if is_gemini:
                    for candidate in chunk.get("candidates", []):
                        for part in (candidate.get("content") or {}).get("parts", []):
                            token = part.get("text") or ""
                            if token:
                                full_reply += token
                                yield json.dumps({"token": token}, ensure_ascii=False) + "\n"
                    continue

                choice = (chunk.get("choices") or [{}])[0]
                delta = choice.get("delta") or {}
                token = delta.get("content") or choice.get("text") or ""

                # GPT-OSS может отдавать reasoning отдельно; пользователю нужен content.
                if token:
                    full_reply += token
                    yield json.dumps({"token": token}, ensure_ascii=False) + "\n"

            if full_reply:
                contents.append({"role": "assistant", "content": full_reply})
                if len(contents) > 20:
                    del contents[:-20]

            yield json.dumps({"done": True}, ensure_ascii=False) + "\n"
        except Exception as exc:
            yield json.dumps({"error": f"Ошибка потокового ответа: {exc}"}, ensure_ascii=False) + "\n"
        finally:
            if upstream is not None:
                upstream.close()

    return Response(
        generate(),
        mimetype="application/x-ndjson",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )

# ========== КОМАНДЫ ==========

# ========== LEGACY HISTORY API ==========
# ========== LEGACY HISTORY API ==========

@chat_bp.route('/api/history/clear', methods=['DELETE', 'POST'])
def api_history_clear():
    global contents
    new_chat_id = create_chat()
    session['chat_id'] = new_chat_id
    contents = []
    return jsonify({'success': True, 'new_chat_id': new_chat_id})

@chat_bp.route('/api/history', methods=['GET'])
def api_history_get():
    chat_id = get_or_create_session_chat()
    messages = get_chat_history(chat_id, limit=200)
    return jsonify({'history': messages, 'chat_id': chat_id})

# ========== API ЧАТОВ (ПАМЯТЬ) ==========

@chat_bp.route('/api/chats', methods=['GET'])
def api_list_chats():
    """Список всех чатов пользователя."""
    chats = list_chats(limit=100)
    return jsonify({'chats': chats})

@chat_bp.route('/api/chats', methods=['POST'])
def api_create_chat():
    """Создать новый чат."""
    data = request.get_json() or {}
    title = data.get('title', 'Новый чат')
    chat_id = create_chat(title)
    session['chat_id'] = chat_id
    return jsonify({'chat_id': chat_id, 'title': title})

@chat_bp.route('/api/chats/<chat_id>', methods=['GET'])
def api_get_chat(chat_id):
    """Получить историю сообщений чата."""
    messages = get_chat_history(chat_id, limit=200)
    return jsonify({'chat_id': chat_id, 'messages': messages})

@chat_bp.route('/api/chats/<chat_id>', methods=['DELETE'])
def api_delete_chat(chat_id):
    """Удалить чат."""
    delete_chat(chat_id)
    if session.get('chat_id') == chat_id:
        session.pop('chat_id', None)
    return jsonify({'success': True})

@chat_bp.route('/api/chats/<chat_id>/switch', methods=['POST'])
def api_switch_chat(chat_id):
    """Переключиться на другой чат."""
    global contents
    session['chat_id'] = chat_id
    contents = get_chat_history(chat_id, limit=20)
    return jsonify({'success': True, 'chat_id': chat_id})

@chat_bp.route('/api/chats/current', methods=['GET'])
def api_current_chat():
    """Текущий активный чат и его история."""
    chat_id = get_or_create_session_chat()
    messages = get_chat_history(chat_id, limit=200)
    return jsonify({'chat_id': chat_id, 'messages': messages})

@chat_bp.route('/api/chats/clear', methods=['POST'])
def api_clear_current_chat():
    """Очистить текущий чат (создаёт новый)."""
    global contents
    session['chat_id'] = create_chat()
    contents = []
    return jsonify({'success': True, 'new_chat_id': session['chat_id']})
