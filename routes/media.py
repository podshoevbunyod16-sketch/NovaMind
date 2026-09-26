"""
routes/media.py — Загрузка файлов, изображений, TTS, STT, видео
"""
from flask import Blueprint, request, jsonify, send_from_directory, session
import os
import uuid
import mimetypes
from groq_rotation import groq_request_with_rotation, get_groq_key

media_bp = Blueprint("media", __name__)

# ========== ОТДАЧА ИЗОБРАЖЕНИЙ ==========

@media_bp.route('/generated_image')
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

@media_bp.route('/upload_image', methods=['POST'])
def upload_image():
    """Загрузка и анализ изображения через Groq Vision"""
    global contents

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

        contents.append({"role": "user", "content": f"[Изображение: {filename}] {user_desc}"})
        contents.append({"role": "assistant", "content": description})
        if len(contents) > 20:
            contents = contents[-20:]

        return jsonify({
            'result': f'''📷 **Анализ изображения {filename} (Groq Vision):**

{description}'''
        })

    except Exception as e:
        print(f"upload_image error: {e}")
        return jsonify({'error': f'Ошибка анализа изображения: {str(e)}'})


@media_bp.route('/upload_file', methods=['POST'])
def upload_file():
    """Загрузка файла + анализ через Groq или Cerebras"""
    global contents

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
            'result': f'''⚠️ Формат {ext} не поддерживается.

Поддерживаются: txt, json, csv, py, js, html, css, md, xml, yaml, log'''
        })

    try:
        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            file_content = f.read()[:5000]
    except Exception as e:
        return jsonify({'error': f'Ошибка чтения файла: {str(e)}'})

    if not file_content.strip():
        return jsonify({'result': f'⚠️ Файл {filename} пустой.'})

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
            user_desc = 'Опиши структуру этого JSON и объясни что в нём хранится.'
        elif ext == '.csv':
            user_desc = 'Проанализируй эти CSV данные: опиши колонки и содержимое.'
        elif ext == '.md':
            user_desc = 'Сделай резюме этого Markdown документа и выдели главное.'
        else:
            user_desc = 'Подробно проанализируй содержимое этого файла и объясни что в нём.'

    analysis_prompt = f"""Пользователь загрузил файл: {filename}

Задача: {user_desc}

Содержимое файла:
{file_content}

Отвечай на русском языке. Используй Markdown форматирование."""

    groq_error = None
    try:
        provider = PROVIDERS[current_provider]
        payload = {
            "model": current_model,
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

        contents.append({"role": "user", "content": f"[Файл: {filename}] {user_desc}"})
        contents.append({"role": "assistant", "content": reply})
        if len(contents) > 20:
            contents = contents[-20:]

        return jsonify({
            'result': f'''📁 **Анализ файла {filename}:**

{reply}'''
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

        contents.append({"role": "user", "content": f"[Файл: {filename}] {user_desc}"})
        contents.append({"role": "assistant", "content": reply})
        if len(contents) > 20:
            contents = contents[-20:]

        return jsonify({
            'result': f'''📁 **Анализ файла {filename} (Cerebras):**

{reply}'''
        })

    except Exception as e2:
        print(f"Cerebras upload_file error: {e2}")
        return jsonify({
            'result': f'''⚠️ AI недоступен (Groq: {groq_error}, Cerebras: {str(e2)})

**Содержимое файла {filename}:**

```
{file_content[:2000]}
```'''
        })


# ========== МУЛЬТИМЕДИА ==========
MEDIA_DIR = os.path.join(os.path.dirname(__file__), "generated_media")
os.makedirs(MEDIA_DIR, exist_ok=True)

@media_bp.route('/media/<path:filename>')
def media_file(filename):
    safe_name = os.path.basename(filename)
    filepath = os.path.join(MEDIA_DIR, safe_name)
    if not os.path.isfile(filepath):
        return jsonify({"error": "Медиафайл не найден"}), 404
    return send_file(filepath, mimetype=mimetypes.guess_type(filepath)[0] or "application/octet-stream")

@media_bp.route('/api/media/upload', methods=['POST'])
def media_upload():
    media = request.files.get('file')
    if not media or not media.filename:
        return jsonify({"error": "Файл не выбран"}), 400
    content_type = media.mimetype or mimetypes.guess_type(media.filename)[0] or "application/octet-stream"
    if not (content_type.startswith("audio/") or content_type.startswith("video/") or content_type.startswith("image/")):
        return jsonify({"error": "Поддерживаются только аудио, видео и изображения"}), 415
    extension = os.path.splitext(media.filename)[1].lower() or mimetypes.guess_extension(content_type) or ""
    saved_name = f"upload_{uuid.uuid4().hex}{extension}"
    media.save(os.path.join(MEDIA_DIR, saved_name))
    return jsonify({"success": True, "filename": media.filename, "type": content_type, "url": f"/media/{saved_name}"})

@media_bp.route('/api/media/transcribe', methods=['POST'])
def media_transcribe():
    media = request.files.get('file')
    if not media or not media.filename:
        return jsonify({"error": "Аудиофайл не выбран"}), 400
    endpoint = os.getenv("AUDIO_TRANSCRIPTION_URL", "")
    api_key = os.getenv("AUDIO_API_KEY", "")
    if not endpoint:
        if os.getenv("GROQ_API_KEY") or GROQ_KEYS:
            endpoint = "https://api.groq.com/openai/v1/audio/transcriptions"
            api_key = api_key or get_groq_key()
        elif os.getenv("OPENAI_API_KEY"):
            endpoint = "https://api.openai.com/v1/audio/transcriptions"
            api_key = api_key or os.getenv("OPENAI_API_KEY", "")
    if not endpoint:
        return jsonify({"error": "Настройте AUDIO_TRANSCRIPTION_URL и AUDIO_API_KEY либо добавьте GROQ_API_KEY/OPENAI_API_KEY в .env"}), 503
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        response = requests.post(endpoint, headers=headers, files={"file": (media.filename, media.stream, media.mimetype)}, data={"model": os.getenv("AUDIO_TRANSCRIPTION_MODEL", "whisper-large-v3-turbo"), "language": request.form.get("language", "ru")}, timeout=180)
        response.raise_for_status()
        result = response.json()
        return jsonify({"success": True, "text": result.get("text", ""), "language": result.get("language")})
    except (requests.RequestException, ValueError) as exc:
        return jsonify({"error": f"Ошибка расшифровки аудио: {exc}"}), 502

@media_bp.route('/api/media/tts', methods=['POST'])
def media_tts():
    data = request.get_json() or {}
    text = data.get("text", "").strip()
    if not text:
        return jsonify({"error": "Введите текст для озвучивания"}), 400
    endpoint = os.getenv("AUDIO_TTS_URL", "")
    api_key = os.getenv("AUDIO_API_KEY", "")
    model = os.getenv("AUDIO_TTS_MODEL", "tts-1")
    if not endpoint and os.getenv("OPENAI_API_KEY"):
        endpoint, api_key = "https://api.openai.com/v1/audio/speech", api_key or os.getenv("OPENAI_API_KEY", "")
    if not endpoint:
        return jsonify({"error": "Настройте AUDIO_TTS_URL и AUDIO_API_KEY либо добавьте OPENAI_API_KEY в .env"}), 503
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"} if api_key else {"Content-Type": "application/json"}
    try:
        response = requests.post(endpoint, headers=headers, json={"model": model, "input": text, "voice": data.get("voice", "alloy"), "response_format": "mp3"}, timeout=180)
        response.raise_for_status()
        filename = f"speech_{uuid.uuid4().hex}.mp3"
        with open(os.path.join(MEDIA_DIR, filename), "wb") as audio_file:
            audio_file.write(response.content)
        return jsonify({"success": True, "url": f"/media/{filename}", "filename": filename})
    except requests.RequestException as exc:
        return jsonify({"error": f"Ошибка генерации аудио: {exc}"}), 502

@media_bp.route('/api/media/image', methods=['POST'])
def media_image():
    data = request.get_json() or {}
    prompt = data.get("prompt", "").strip()
    if not prompt:
        return jsonify({"error": "Введите описание изображения"}), 400
    endpoint = os.getenv("IMAGE_GENERATION_URL", "")
    api_key = os.getenv("IMAGE_API_KEY", "") or os.getenv("OPENAI_API_KEY", "")
    if not endpoint and os.getenv("OPENAI_API_KEY"):
        endpoint = "https://api.openai.com/v1/images/generations"
    if not endpoint:
        from urllib.parse import quote
        width, height = int(data.get("width", 1024)), int(data.get("height", 1024))
        url = f"https://image.pollinations.ai/prompt/{quote(prompt)}?width={width}&height={height}&nologo=True"
        return jsonify({"success": True, "url": url, "provider": "pollinations"})
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"} if api_key else {"Content-Type": "application/json"}
    try:
        response = requests.post(endpoint, headers=headers, json={"model": os.getenv("IMAGE_MODEL", "gpt-image-1"), "prompt": prompt, "size": data.get("size", "1024x1024"), "n": 1}, timeout=240)
        response.raise_for_status()
        result = response.json().get("data", [{}])[0]
        if result.get("url"):
            return jsonify({"success": True, "url": result["url"], "provider": "configured"})
        if result.get("b64_json"):
            import base64
            filename = f"image_{uuid.uuid4().hex}.png"
            with open(os.path.join(MEDIA_DIR, filename), "wb") as image_file:
                image_file.write(base64.b64decode(result["b64_json"]))
            return jsonify({"success": True, "url": f"/media/{filename}", "provider": "configured"})
        return jsonify({"error": "Провайдер не вернул изображение"}), 502
    except (requests.RequestException, ValueError, IndexError) as exc:
        return jsonify({"error": f"Ошибка генерации изображения: {exc}"}), 502

@media_bp.route('/api/media/video', methods=['POST'])
def media_video():
    data = request.get_json() or {}
    prompt = data.get("prompt", "").strip()
    endpoint = os.getenv("VIDEO_API_URL", "")
    if not prompt:
        return jsonify({"error": "Введите описание видео"}), 400
    if not endpoint:
        return jsonify({"error": "Для видео укажите VIDEO_API_URL, VIDEO_API_KEY и VIDEO_MODEL в .env. У видео-провайдеров разные API, поэтому endpoint настраивается явно."}), 503
    api_key = os.getenv("VIDEO_API_KEY", "") or os.getenv("OPENAI_API_KEY", "")
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"} if api_key else {"Content-Type": "application/json"}
    payload = {"model": os.getenv("VIDEO_MODEL", ""), "prompt": prompt, "duration": data.get("duration", 5), "aspect_ratio": data.get("aspect_ratio", "16:9")}
    try:
        response = requests.post(endpoint, headers=headers, json=payload, timeout=240)
        response.raise_for_status()
        result = response.json()
        return jsonify({"success": True, "url": result.get("video_url") or result.get("url"), "job_id": result.get("id") or result.get("job_id"), "status_url": result.get("status_url"), "raw": result})
    except (requests.RequestException, ValueError) as exc:
        return jsonify({"error": f"Ошибка генерации видео: {exc}"}), 502

# ========== МОДЕЛИ ==========
