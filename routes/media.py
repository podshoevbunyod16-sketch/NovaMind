"""
routes/media.py — Загрузка файлов, изображений, TTS, STT, видео
"""
from flask import Blueprint, request, jsonify, send_from_directory, send_file, session
import os
import uuid
import mimetypes
import base64
import zipfile
import xml.etree.ElementTree as ET
import requests
from ai_providers import groq_request_with_rotation
from groq_rotation import get_groq_key, GROQ_KEYS
import config
import media_generation as mg

media_bp = Blueprint("media", __name__)

# ========== ОТДАЧА ИЗОБРАЖЕНИЙ ==========

@media_bp.route('/generated_image')
def generated_image():
    filename = request.args.get("file", "")
    if not filename:
        return jsonify({"error": "No filename"}), 400
    safe_name = os.path.basename(filename.replace("..", ""))
    img_dir = mg.IMAGE_DIR
    filepath = os.path.join(img_dir, safe_name)
    if not os.path.exists(filepath):
        return jsonify({"error": "File not found"}), 404
    return send_file(filepath, mimetype=mimetypes.guess_type(filepath)[0] or 'image/png')


# ========== ЗАГРУЗКА ФАЙЛОВ ==========


@media_bp.route('/upload_image', methods=['POST'])
def upload_image():
    return analyze_attachment()

@media_bp.route('/upload_file', methods=['POST'])
def upload_file():
    return analyze_attachment()

@media_bp.route('/api/attachments/analyze', methods=['POST'])
def analyze_attachment():
    """Universal analyzer for images and common documents."""
    global contents
    uploaded = request.files.get('file') or request.files.get('image')
    if not uploaded or not uploaded.filename:
        return jsonify({'error': 'Файл не выбран'}), 400

    filename = os.path.basename(uploaded.filename)
    ext = os.path.splitext(filename)[1].lower()
    content_type = uploaded.mimetype or mimetypes.guess_type(filename)[0] or 'application/octet-stream'
    user_desc = (request.form.get('description') or '').strip()

    upload_dir = os.path.join(os.path.dirname(__file__), "uploads")
    os.makedirs(upload_dir, exist_ok=True)
    filepath = os.path.join(upload_dir, f"{uuid.uuid4().hex}_{filename}")
    uploaded.save(filepath)

    try:
        if os.path.getsize(filepath) > 25 * 1024 * 1024:
            return jsonify({'error': 'Файл слишком большой. Максимальный размер анализа — 25 МБ.'}), 413

        is_image = content_type.startswith('image/') or ext in {'.jpg','.jpeg','.png','.webp','.gif','.bmp'}
        if is_image:
            return _analyze_image_file(filepath, filename, content_type, user_desc)

        text = _extract_attachment_text(filepath, ext, content_type)
        if not text.strip():
            return jsonify({'error': f'Файл {filename} пустой или из него не удалось извлечь текст.'}), 415

        instruction = user_desc or _default_file_instruction(ext)
        prompt = (
            f"Файл: {filename}\n\nЗадача: {instruction}\n\n"
            f"Содержимое:\n{text[:120000]}\n\n"
            "Отвечай на русском языке в Markdown. Не выдумывай данные. "
            "Для кода указывай конкретные ошибки и исправления."
        )
        reply, error = _call_selected_text_ai(prompt)
        if error:
            return jsonify({'error': error}), 502

        return jsonify({
            'success': True,
            'filename': filename,
            'type': content_type,
            'result': f'📁 **Анализ файла {filename}:**\n\n{reply}'
        })
    except Exception as exc:
        print(f"[attachments] error: {exc}")
        return jsonify({'error': f'Ошибка обработки файла: {exc}'}), 500


def _default_file_instruction(ext):
    if ext == '.py':
        return 'Проанализируй Python-код, объясни его работу, найди ошибки и предложи улучшения.'
    if ext in {'.js','.ts','.jsx','.tsx'}:
        return 'Проанализируй JavaScript/TypeScript-код, найди ошибки и предложи улучшения.'
    if ext in {'.html','.css'}:
        return 'Проанализируй веб-файл, объясни структуру и найди проблемы.'
    if ext == '.json':
        return 'Проанализируй структуру JSON и объясни важные данные.'
    if ext in {'.csv','.tsv'}:
        return 'Проанализируй таблицу, колонки, данные и важные закономерности.'
    if ext == '.md':
        return 'Сделай структурированное резюме документа и выдели главное.'
    return 'Подробно проанализируй файл и объясни его содержимое.'


def _extract_attachment_text(filepath, ext, content_type):
    text_exts = {
        '.txt','.json','.csv','.tsv','.py','.js','.ts','.jsx','.tsx','.html','.htm',
        '.css','.md','.xml','.yaml','.yml','.log','.ini','.cfg','.conf','.sql',
        '.sh','.bash','.java','.c','.cpp','.h','.hpp','.go','.rs','.php'
    }
    if ext in text_exts or content_type.startswith('text/'):
        with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
            return f.read()

    if ext == '.pdf':
        try:
            from pypdf import PdfReader
            return '\n\n'.join((page.extract_text() or '') for page in PdfReader(filepath).pages)
        except ImportError:
            raise RuntimeError('PDF: зависимость pypdf не установлена.')

    if ext == '.docx':
        try:
            from docx import Document
            doc = Document(filepath)
            parts = [p.text for p in doc.paragraphs if p.text.strip()]
            for table in doc.tables:
                for row in table.rows:
                    parts.append(' | '.join(cell.text.strip() for cell in row.cells))
            return '\n'.join(parts)
        except ImportError:
            raise RuntimeError('DOCX: зависимость python-docx не установлена.')

    if ext in {'.xlsx','.xlsm'}:
        try:
            from openpyxl import load_workbook
            wb = load_workbook(filepath, read_only=True, data_only=True)
            parts = []
            for ws in wb.worksheets:
                parts.append(f'### Лист: {ws.title}')
                for row in ws.iter_rows(values_only=True):
                    values = [str(v) if v is not None else '' for v in row]
                    if any(values):
                        parts.append(' | '.join(values))
            return '\n'.join(parts)
        except ImportError:
            raise RuntimeError('XLSX: зависимость openpyxl не установлена.')

    if ext == '.pptx':
        with zipfile.ZipFile(filepath) as z:
            parts = []
            ns = {'a': 'http://schemas.openxmlformats.org/drawingml/2006/main'}
            for name in sorted(z.namelist()):
                if name.startswith('ppt/slides/slide') and name.endswith('.xml'):
                    root = ET.fromstring(z.read(name))
                    parts.extend(t.text for t in root.findall('.//a:t', ns) if t.text)
            return '\n'.join(parts)

    raise RuntimeError(
        f'Формат {ext or content_type} не поддерживается. '
        'Поддерживаются изображения, TXT/код/JSON/CSV, PDF, DOCX, XLSX/XLSM и PPTX.'
    )


def _analyze_image_file(filepath, filename, content_type, user_desc):
    instruction = user_desc or 'Подробно опиши изображение: объекты, текст, структуру и важные детали.'

    with open(filepath, 'rb') as f:
        encoded = base64.b64encode(f.read()).decode('ascii')

    # 1) Google AI Studio / Gemini Vision — универсальный и простой путь.
    gemini_key = (os.getenv('GEMINI_API_KEY') or os.getenv('GOOGLE_AI_STUDIO_KEY') or '').strip()
    if gemini_key:
        model = os.getenv('VISION_MODEL', 'gemini-2.5-flash')
        body = {
            'contents': [{'role': 'user', 'parts': [
                {'text': instruction},
                {'inline_data': {'mime_type': content_type, 'data': encoded}}
            ]}],
            'generationConfig': {'temperature': 0.2, 'maxOutputTokens': 4096}
        }
        try:
            r = requests.post(
                f'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent',
                params={'key': gemini_key}, json=body,
                headers={'Content-Type': 'application/json'}, timeout=120
            )
            if r.ok:
                parts = []
                for candidate in r.json().get('candidates', []):
                    for part in (candidate.get('content') or {}).get('parts', []):
                        if part.get('text'):
                            parts.append(part['text'])
                if parts:
                    return jsonify({
                        'success': True,
                        'filename': filename,
                        'result': f'📷 **Анализ изображения {filename} (Google AI Studio):**\\n\\n{"".join(parts)}'
                    })
            print(f"[attachments] Gemini Vision HTTP {r.status_code}: {r.text[:500]}")
        except (requests.RequestException, ValueError) as exc:
            print(f"[attachments] Gemini Vision error: {exc}")

    # 2) OpenAI-compatible Vision.
    #    This allows OpenRouter and other compatible providers to work from the
    #    same attachment button. Set VISION_MODEL if the selected chat model is
    #    text-only.
    vision_provider = (os.getenv('VISION_PROVIDER') or config.current_provider or '').strip()
    vision_model = (os.getenv('VISION_MODEL') or config.current_model or '').strip()
    provider = config.PROVIDERS.get(vision_provider)
    if provider and vision_model:
        url = provider.get('url', '')
        if url and 'generativelanguage.googleapis.com' not in url:
            headers = provider.get('headers', {}).copy()
            if vision_provider == 'openrouter':
                key = os.getenv('OPENROUTER_API_KEY', '').strip()
                if key:
                    headers['Authorization'] = f'Bearer {key}'
                    headers['HTTP-Referer'] = 'http://localhost:5000'
                    headers['X-Title'] = 'NovaMind AI'
            elif vision_provider == 'groq':
                key = get_groq_key()
                if key:
                    headers['Authorization'] = f'Bearer {key}'
            elif vision_provider == 'cerebras':
                key = os.getenv('CEREBRAS_API_KEY', '').strip()
                if key:
                    headers['Authorization'] = f'Bearer {key}'

            if headers.get('Authorization') or vision_provider == 'openai_compatible':
                payload = {
                    'model': vision_model,
                    'messages': [{
                        'role': 'user',
                        'content': [
                            {'type': 'text', 'text': instruction},
                            {'type': 'image_url', 'image_url': {
                                'url': f'data:{content_type};base64,{encoded}'
                            }}
                        ]
                    }],
                    'temperature': 0.2,
                    'max_tokens': 4096
                }
                try:
                    r = requests.post(url, headers=headers, json=payload, timeout=120)
                    if r.ok:
                        data = r.json()
                        reply = ((data.get('choices') or [{}])[0].get('message') or {}).get('content')
                        if reply:
                            return jsonify({
                                'success': True,
                                'filename': filename,
                                'result': f'📷 **Анализ изображения {filename} ({vision_provider}):**\\n\\n{reply}'
                            })
                    print(f"[attachments] {vision_provider} Vision HTTP {r.status_code}: {r.text[:500]}")
                except (requests.RequestException, ValueError, KeyError, IndexError, TypeError) as exc:
                    print(f"[attachments] {vision_provider} Vision error: {exc}")

    return jsonify({
        'error': 'Не удалось распознать изображение. Добавьте GEMINI_API_KEY/GOOGLE_AI_STUDIO_KEY или выберите Vision-модель/настройте VISION_MODEL и VISION_PROVIDER.'
    }), 503

def _call_selected_text_ai(prompt):
    provider = config.PROVIDERS.get(config.current_provider)
    if not provider:
        return None, f'Неизвестный AI-провайдер: {config.current_provider}'
    payload = {
        'model': config.current_model,
        'messages': [
            {'role': 'system', 'content': config.system_prompt},
            {'role': 'user', 'content': prompt}
        ],
        'temperature': 0.3,
        'max_tokens': min(provider.get('max_tokens', 8192), 8192)
    }
    data, error = groq_request_with_rotation(
        provider.get('url',''), payload, provider.get('headers',{}).copy(), timeout=120
    )
    if error:
        return None, error
    try:
        return data['choices'][0]['message']['content'], None
    except (KeyError, IndexError, TypeError):
        return None, 'AI вернул неожиданный формат ответа.'


# ========== МУЛЬТИМЕДИА ==========
# Общий каталог медиафайлов (репозиторий/generated_media, в .gitignore).
MEDIA_DIR = mg.MEDIA_DIR
mg.ensure_media_dir()

@media_bp.route('/media/<path:filename>')
def media_file(filename):
    safe_name = os.path.basename(filename)
    filepath = os.path.join(MEDIA_DIR, safe_name)
    if not os.path.isfile(filepath):
        return jsonify({"error": "Медиафайл не найден"}), 404
    as_attachment = request.args.get("download") == "1"
    return send_file(
        filepath,
        mimetype=mimetypes.guess_type(filepath)[0] or "application/octet-stream",
        as_attachment=as_attachment,
        download_name=safe_name if as_attachment else None,
    )

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
        if os.getenv("POLLINATIONS_API_KEY") or os.getenv("POLLINATIONS_KEY") or os.getenv("POLLINATIONS_TOKEN"):
            endpoint = "https://gen.pollinations.ai/v1/audio/transcriptions"
            api_key = api_key or os.getenv("POLLINATIONS_API_KEY") or os.getenv("POLLINATIONS_KEY") or os.getenv("POLLINATIONS_TOKEN")
        elif os.getenv("GROQ_API_KEY") or GROQ_KEYS:
            endpoint = "https://api.groq.com/openai/v1/audio/transcriptions"
            api_key = api_key or get_groq_key()
        elif os.getenv("OPENAI_API_KEY"):
            endpoint = "https://api.openai.com/v1/audio/transcriptions"
            api_key = api_key or os.getenv("OPENAI_API_KEY", "")
    if not endpoint:
        return jsonify({"error": "Настройте AUDIO_TRANSCRIPTION_URL/AUDIO_API_KEY или Pollinations/Groq/OpenAI ключ."}), 503
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        model = os.getenv("AUDIO_TRANSCRIPTION_MODEL", "")
        if "gen.pollinations.ai" in endpoint and not model:
            model = "openai/gpt-audio-mini"
        if not model:
            model = "whisper-large-v3-turbo"
        response = requests.post(endpoint, headers=headers, files={"file": (media.filename, media.stream, media.mimetype)}, data={"model": model, "language": request.form.get("language", "ru")}, timeout=180)
        response.raise_for_status()
        result = response.json()
        return jsonify({"success": True, "text": result.get("text", ""), "language": result.get("language")})
    except (requests.RequestException, ValueError) as exc:
        return jsonify({"error": f"Ошибка расшифровки аудио: {exc}"}), 502


# ======================================================================
# ЕДИНЫЙ КАТАЛОГ И ВЫБОР МЕДИА-МОДЕЛЕЙ
# ======================================================================
def _resolve_requested_model(data: dict):
    """
    Разрешает модель из запроса клиента.

    Клиент может прислать {provider, model} явно либо опустить их —
    тогда используется выбранная в панели «Медиа» модель.
    Платные модели и модели с неподтверждённой ценой требуют явного подтверждения.
    """
    model, confirm, error = mg.resolve_selection(
        data.get("provider"), data.get("model"), data.get("confirm")
    )
    if error:
        return None, confirm, error
    return model, confirm, None


@media_bp.route('/api/media/providers', methods=['GET'])
def media_providers():
    """Статус подключения медиа-провайдеров."""
    statuses = mg.provider_statuses()
    return jsonify({
        "providers": [
            {"id": pid, "name": name, "connected": statuses.get(pid, False)}
            for pid, name in mg.MEDIA_PROVIDERS.items()
        ],
        "selection": config.media_selection or None,
    })


@media_bp.route('/api/media/models', methods=['GET'])
def media_models():
    """
    Единый поиск и выбор моделей генерации изображений, аудио и видео.

    ?type=image|audio|video|all  &provider=…  &q=поиск
    &include_paid=1  &include_unknown=1  &refresh=1
    """
    media_type = (request.args.get("type") or "all").strip().lower()
    provider = (request.args.get("provider") or "all").strip().lower()
    query = (request.args.get("q") or "").strip()
    include_paid = request.args.get("include_paid") in {"1", "true", "yes"}
    include_unknown = request.args.get("include_unknown") in {"1", "true", "yes"}
    include_trial = request.args.get("include_trial", "1") in {"1", "true", "yes"}
    refresh = request.args.get("refresh") in {"1", "true", "yes"}

    if media_type not in {"all", *mg.MEDIA_TYPES}:
        return jsonify({"error": "Неизвестный тип медиа"}), 400
    if provider != "all" and provider not in mg.MEDIA_PROVIDERS:
        return jsonify({"error": "Неизвестный провайдер медиа"}), 400

    models = mg.media_catalog(
        media_type=media_type, provider=provider, query=query,
        include_paid=include_paid, include_unknown=include_unknown,
        include_trial=include_trial, refresh=refresh,
    )
    counts = {t: 0 for t in mg.MEDIA_TYPES}
    for item in models:
        counts[item.get("media_type")] = counts.get(item.get("media_type"), 0) + 1
    return jsonify({
        "models": models,
        "type": media_type,
        "provider": provider,
        "query": query,
        "count": len(models),
        "counts": counts,
        "free_count": sum(1 for m in models if m.get("pricing_status") == mg.PRICING_FREE),
        "trial_count": sum(1 for m in models if m.get("pricing_status") == mg.PRICING_TRIAL),
        "providers": mg.provider_statuses(),
        "selection": config.media_selection or None,
        "pricing_verified_at": mg.PRICING_VERIFIED_AT,
    })


@media_bp.route('/api/media/selection', methods=['GET'])
def media_selection_get():
    return jsonify({"selection": config.media_selection or None})


@media_bp.route('/api/media/select', methods=['POST'])
def media_select():
    """
    Выбор медиа-модели. Автоматически выбираются только модели
    с подтверждённым бесплатным API; пробные кредиты и платные модели
    требуют явного подтверждения пользователя.
    """
    data = request.get_json(silent=True) or {}
    if data.get("clear"):
        config.save_selected_media_model({})
        return jsonify({"success": True, "selection": None})

    provider = (data.get("provider") or "").strip()
    model_id = (data.get("model") or "").strip()
    confirm = data.get("confirm") or {}
    model, error = mg.find_media_model(
        provider, model_id,
        include_paid=True, include_unknown=True,
    )
    if error:
        return jsonify({"error": error}), 404
    problem = mg.validate_selection(model, confirm)
    if problem:
        return jsonify({"error": problem, "requires": mg.required_confirmations(model),
                        "model": mg.selection_payload(model)}), 409
    selection = mg.selection_payload(model)
    selection["confirmed"] = {
        "paid": bool(confirm.get("paid")),
        "trial": bool(confirm.get("trial")),
        "unknown": bool(confirm.get("unknown")),
    }
    config.save_selected_media_model(selection)
    return jsonify({"success": True, "selection": selection})


@media_bp.route('/api/media/generate', methods=['POST'])
def media_generate():
    """Генерация по выбранной модели. Статусы — реальные шаги запроса."""
    data = request.get_json(silent=True) or {}
    prompt = (data.get("prompt") or "").strip()
    if not prompt:
        return jsonify({"error": "Пустой промпт: опишите, что нужно создать."}), 400

    model, confirm, error = _resolve_requested_model(data)
    if error:
        return jsonify({"error": error}), 400

    # Платные/пробные модели без явного подтверждения отклоняем до запроса к провайдеру.
    problem = mg.validate_selection(model, confirm)
    if problem:
        return jsonify({"error": problem, "status": "refused",
                        "requires": mg.required_confirmations(model),
                        "model": mg.selection_payload(model)}), 400

    events = []
    result, error = mg.generate_media(
        model, prompt, options=data.get("options") or {},
        on_status=lambda message: events.append(message), confirm=confirm,
    )
    if error:
        return jsonify({"error": error, "status": "error", "events": events,
                        "model": mg.selection_payload(model)}), 502
    return jsonify({
        "status": "queued" if result.get("job_id") else "done",
        "media": result,
        "model": mg.selection_payload(model),
        "events": events,
        "elapsed_ms": result.get("elapsed_ms"),
    })


@media_bp.route('/api/media/jobs/<path:job_id>', methods=['GET'])
def media_job(job_id):
    """Реальный статус асинхронной задачи у провайдера (без выдуманного прогресса)."""
    record = mg.poll_job(job_id)
    if record is None:
        return jsonify({"error": "Задача не найдена"}), 404
    return jsonify(mg.job_public(record))


# ======================================================================
# LEGACY ENDPOINTS (оставлены для совместимости, работают через движок)
# ======================================================================
def _legacy_generate(default_backend_model: dict, prompt: str, options: dict):
    result, error = mg.generate_media(
        default_backend_model, prompt, options=options,
        confirm={"paid": True, "trial": True, "unknown": True},
    )
    if error:
        return jsonify({"error": error}), 502
    return jsonify({"success": True, "url": result.get("url"), "provider": result.get("provider"),
                    "media": result})


def _legacy_model(provider: str, model_id: str, media_type: str, backend: str, options=None):
    return {
        "id": model_id, "name": model_id, "provider": provider,
        "provider_name": mg.MEDIA_PROVIDERS.get(provider, provider),
        "media_type": media_type, "backend": backend,
        "pricing_status": mg.PRICING_UNKNOWN, "pricing_label": mg.PRICING_LABELS[mg.PRICING_UNKNOWN],
        "provider_connected": True, "in_provider_catalog": None,
        "options": options or {},
    }


@media_bp.route('/api/media/tts', methods=['POST'])
def media_tts():
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "Введите текст для озвучивания"}), 400
    endpoint = os.getenv("AUDIO_TTS_URL", "")
    model_id = data.get("model") or os.getenv("AUDIO_TTS_MODEL") or "gemini-3.8-flash-lite-tts"
    if endpoint:
        model = _legacy_model("custom", model_id, "audio", "custom_tts")
    elif mg.provider_connected("google_ai_studio"):
        model = _legacy_model("google_ai_studio", model_id, "audio", "google_tts",
                              {"voice": data.get("voice") or "Kore"})
    elif mg.provider_connected("groq"):
        model = _legacy_model("groq", "playai-tts", "audio", "groq_tts",
                              {"voice": data.get("voice") or "Fritz-PlayAI"})
    else:
        model = _legacy_model("pollinations", "openai-audio", "audio", "pollinations_audio",
                              {"voice": data.get("voice") or "nova"})
    return _legacy_generate(model, text, {"voice": data.get("voice")})


@media_bp.route('/api/media/image', methods=['POST'])
def media_image():
    data = request.get_json(silent=True) or {}
    prompt = (data.get("prompt") or "").strip()
    if not prompt:
        return jsonify({"error": "Введите описание изображения"}), 400
    options = {"width": data.get("width") or 1024, "height": data.get("height") or 1024,
               "size": data.get("size") or "1024x1024"}
    if os.getenv("IMAGE_GENERATION_URL"):
        model = _legacy_model("custom", os.getenv("IMAGE_MODEL", "custom-image"), "image", "custom_image")
    elif mg.provider_connected("google_ai_studio"):
        model = _legacy_model("google_ai_studio", "gemini-3.1-flash-image", "image", "google_image")
    elif mg.provider_connected("openrouter"):
        model = _legacy_model("openrouter", os.getenv("IMAGE_MODEL", "google/gemini-2.5-flash-image"),
                              "image", "openrouter_image")
    else:
        model = _legacy_model("pollinations", "flux", "image", "pollinations_image")
    return _legacy_generate(model, prompt, options)


@media_bp.route('/api/media/video', methods=['POST'])
def media_video():
    data = request.get_json(silent=True) or {}
    prompt = (data.get("prompt") or "").strip()
    if not prompt:
        return jsonify({"error": "Введите описание видео"}), 400
    options = {"duration": data.get("duration") or 5, "aspect_ratio": data.get("aspect_ratio") or "16:9"}
    if os.getenv("VIDEO_API_URL"):
        model = _legacy_model("custom", os.getenv("VIDEO_MODEL", "custom-video"), "video", "custom_video")
    elif mg.provider_connected("google_ai_studio"):
        model = _legacy_model("google_ai_studio", "veo-3.1-generate-preview", "video", "google_video")
    else:
        return jsonify({"error": (
            "Нет подключённого видео-провайдера с подтверждённым бесплатным API. "
            "Укажите VIDEO_API_URL, VIDEO_API_KEY и VIDEO_MODEL в .env "
            "или подключите GEMINI_API_KEY (Veo — платный тариф)."
        )}), 503
    return _legacy_generate(model, prompt, options)

# ========== МОДЕЛИ ==========

