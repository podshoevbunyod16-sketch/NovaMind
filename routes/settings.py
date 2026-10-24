"""
routes/settings.py — Настройки провайдеров и моделей
"""
from flask import Blueprint, request, jsonify, render_template, session
import zipfile
from ai_providers import fetch_available_models, provider_status, PROVIDER_LABELS, MODEL_ERRORS, media_models_catalog
from config import PROVIDERS, save_selected_model, current_provider, current_model, GOOGLE_CLIENT_ID
import mcp_registry
import skills_registry

settings_bp = Blueprint("settings", __name__)


def _admin_only():
    """Подключать/удалять MCP-серверы может только админ (MCP_ADMIN_ONLY=0 отключает проверку)."""
    import os
    if os.getenv("MCP_ADMIN_ONLY", "1") == "0":
        return None
    from routes.terminal import is_admin
    if not is_admin():
        return jsonify({"error": "Только администратор"}), 403
    return None


@settings_bp.route('/')
def index():
    return render_template('auth.html', google_client_id=GOOGLE_CLIENT_ID)

@settings_bp.route('/chat')
def chat_page():
    return render_template('index.html', google_client_id=GOOGLE_CLIENT_ID)

@settings_bp.route('/settings')
def settings_page():
    return render_template('settings.html')


@settings_bp.route('/api/settings/skills')
def settings_skills():
    return jsonify({"skills": skills_registry.list_skills()})


@settings_bp.route('/api/settings/skills/<skill_id>')
def settings_skill(skill_id):
    try:
        return jsonify(skills_registry.get_skill(skill_id))
    except (ValueError, FileNotFoundError) as exc:
        return jsonify({"error": str(exc)}), 404


@settings_bp.route('/api/settings/skills/text', methods=['POST'])
def settings_skill_text():
    data = request.get_json(silent=True) or {}
    try:
        item = skills_registry.save_text(data.get('id') or data.get('name'), data.get('content') or '')
        return jsonify({"success": True, "skill": item})
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@settings_bp.route('/api/settings/skills/zip', methods=['POST'])
def settings_skill_zip():
    try:
        item = skills_registry.import_zip(request.files.get('file'))
        return jsonify({"success": True, "skill": item})
    except (ValueError, zipfile.BadZipFile) as exc:
        return jsonify({"error": str(exc)}), 400


@settings_bp.route('/api/settings/skills/<skill_id>/enabled', methods=['POST'])
def settings_skill_enabled(skill_id):
    """Включить или выключить навык. Выключенный навык не попадает в промпт агента."""
    data = request.get_json(silent=True) or {}
    if 'enabled' not in data:
        return jsonify({"error": "Укажите enabled: true или false"}), 400
    try:
        item = skills_registry.set_enabled(skill_id, bool(data.get('enabled')))
        return jsonify({"success": True, "skill": item})
    except FileNotFoundError as exc:
        return jsonify({"error": str(exc)}), 404
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@settings_bp.route('/api/settings/skills/<skill_id>', methods=['DELETE'])
def settings_skill_delete(skill_id):
    try:
        skills_registry.delete_skill(skill_id)
        return jsonify({"success": True})
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

PROVIDER_HINTS = {
    "groq": "Бесплатный тариф, нужен GROQ_API_KEY",
    "cerebras": "Нужен CEREBRAS_API_KEY",
    "openrouter": "Нужен OPENROUTER_API_KEY",
    "google_ai_studio": "Нужен GEMINI_API_KEY",
    "pollinations": "Каталог публичный; для генерации нужен POLLINATIONS_API_KEY",
    "openai_compatible": "Локальный llama-server / Ollama, ключ не обязателен",
    "local_demo": "Встроенная офлайн-модель: работает всегда, без сети и ключей",
}


@settings_bp.route('/api/settings/providers')
def settings_providers():
    import config
    statuses = provider_status()
    return jsonify({
        "providers": [
            {
                "id": provider,
                "name": PROVIDER_LABELS.get(provider, provider),
                "configured": statuses.get(provider, False),
                "url": data.get("url", "").split("/chat/completions")[0],
                "hint": PROVIDER_HINTS.get(provider, ""),
                "offline": provider == "local_demo",
            }
            for provider, data in PROVIDERS.items()
        ],
        "current_provider": config.current_provider,
        "current_model": config.current_model,
    })

@settings_bp.route('/api/settings/models')
def settings_models():
    import config
    provider = request.args.get("provider", config.current_provider)
    force = request.args.get("refresh", "0") == "1"
    if provider not in PROVIDERS:
        return jsonify({"error": "Неизвестный провайдер"}), 400
    # Каталог Pollinations публичный: показываем модели даже без ключа.
    if not provider_status().get(provider, False) and provider != "pollinations":
        return jsonify({"provider": provider, "models": [], "configured": False,
                        "error": "API ключ провайдера не найден в .env"})
    models = fetch_available_models(provider, force=force)
    error = None if models else (MODEL_ERRORS.get(provider) or "Не удалось получить каталог моделей.")
    return jsonify({"provider": provider, "models": models, "configured": True,
                    "error": error, "current_model": config.current_model if provider == config.current_provider else None})

@settings_bp.route('/api/settings/media-models')
def settings_media_models():
    media_type = request.args.get("type", "all").strip().lower()
    provider = request.args.get("provider", "all").strip().lower()
    force = request.args.get("refresh", "0") == "1"
    if media_type not in {"all", "image", "audio", "video"}:
        return jsonify({"error":"Неизвестный тип media-модели"}), 400
    if provider not in {"all", "openrouter", "google_ai_studio", "pollinations"}:
        return jsonify({"error":"Неизвестный media-провайдер"}), 400
    try:
        models = media_models_catalog(media_type, provider, force=force)
        return jsonify({"models":models, "type":media_type, "provider":provider, "count":len(models)})
    except Exception as exc:
        return jsonify({"models":[], "error":str(exc)}), 500


@settings_bp.route('/api/settings/mcp')
def settings_mcp_catalog():
    """Return public MCP catalog and masked local connection metadata."""
    return jsonify({"catalog": mcp_registry.catalog(), "connections": mcp_registry.public_connections()})


@settings_bp.route('/api/settings/mcp/connect', methods=['POST'])
def settings_mcp_connect():
    denied = _admin_only()
    if denied:
        return denied
    try:
        server_id = mcp_registry.save_connection(request.get_json(silent=True) or {})
        result = mcp_registry.test_connection(server_id)
        if not result.get("connected"):
            # Не оставляем в каталоге разовые неудачные попытки подключения.
            # Подробная причина уже возвращается клиенту в result["error"].
            mcp_registry.remove_connection(server_id)
        return jsonify({"id": server_id, **result}), (200 if result.get("connected") else 502)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@settings_bp.route('/api/settings/mcp/<server_id>/test', methods=['POST'])
def settings_mcp_test(server_id):
    denied = _admin_only()
    if denied:
        return denied
    try:
        result = mcp_registry.test_connection(server_id)
        return jsonify(result), (200 if result.get("connected") else 502)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 404


@settings_bp.route('/api/settings/mcp/<server_id>/tools')
def settings_mcp_tools(server_id):
    try:
        return jsonify(mcp_registry.request(server_id, "tools/list"))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 404
    except Exception as exc:
        return jsonify({"error": f"MCP: {exc}"}), 502


@settings_bp.route('/api/settings/mcp/<server_id>', methods=['DELETE'])
def settings_mcp_delete(server_id):
    denied = _admin_only()
    if denied:
        return denied
    mcp_registry.remove_connection(server_id)
    return jsonify({"success": True})

@settings_bp.route('/api/settings/select', methods=['POST'])
def settings_select():
    import config
    data = request.get_json() or {}
    provider = data.get("provider", "")
    model = data.get("model", "").strip()
    if provider not in PROVIDERS or not model:
        return jsonify({"error": "Укажите провайдера и модель"}), 400
    models = fetch_available_models(provider)
    if not any(item["id"] == model for item in models):
        return jsonify({"error": "Модель не найдена в каталоге провайдера."}), 400
    config.current_provider = provider
    config.current_model = model
    save_selected_model(provider, model)
    return jsonify({"success": True, "provider": provider, "model": model,
                    "provider_name": PROVIDER_LABELS.get(provider, provider),
                    "message": f"Выбрано: {model}"})

SUPPORTED_LANGUAGES = ("tg", "ru", "en")


@settings_bp.route('/api/settings/lang')
def get_ui_language():
    import config
    return jsonify({"language": config.ui_language,
                    "default": "tg",
                    "supported": list(SUPPORTED_LANGUAGES)})


@settings_bp.route('/api/settings/lang', methods=['POST'])
def set_ui_language():
    import config
    data = request.get_json() or {}
    lang = (data.get("language") or "").strip().lower()
    if lang not in SUPPORTED_LANGUAGES:
        return jsonify({"error": "Язык не поддерживается. Доступны: tg, ru, en"}), 400
    config.save_ui_language(lang)
    return jsonify({"success": True, "language": config.ui_language})


@settings_bp.route('/admin/login')
def admin_login_page():
    return render_template('admin.html')
