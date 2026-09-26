"""
routes/settings.py — Настройки провайдеров и моделей
"""
from flask import Blueprint, request, jsonify, render_template, session
from ai_providers import fetch_available_models, provider_status
from config import PROVIDERS, save_selected_model

settings_bp = Blueprint("settings", __name__)

# ========== СТРАНИЦЫ ==========

@settings_bp.route('/')
def index():
    return render_template('auth.html', google_client_id=GOOGLE_CLIENT_ID)

@settings_bp.route('/chat')
def chat_page():
    return render_template('index.html')

@settings_bp.route('/settings')
def settings_page():
    return render_template('settings.html')

@settings_bp.route('/api/settings/providers')
def settings_providers():
    statuses = provider_status()
    return jsonify({
        "providers": [
            {
                "id": provider,
                "name": PROVIDER_LABELS.get(provider, provider),
                "configured": statuses.get(provider, False),
                "url": data.get("configured_url") or data.get("url", "").split("/chat/completions")[0],
            }
            for provider, data in PROVIDERS.items()
        ],
        "current_provider": current_provider,
        "current_model": current_model,
    })

@settings_bp.route('/api/settings/models')
def settings_models():
    provider = request.args.get("provider", current_provider)
    force = request.args.get("refresh", "0") == "1"
    if provider not in PROVIDERS:
        return jsonify({"error": "Неизвестный провайдер"}), 400
    if not provider_status().get(provider, False):
        return jsonify({"provider": provider, "models": [], "configured": False, "error": "API ключ провайдера не найден в .env"})
    models = fetch_available_models(provider, force=force)
    error = None if models else (MODEL_ERRORS.get(provider) or "Не удалось получить каталог моделей. Проверьте API ключ и доступ к интернету.")
    return jsonify({"provider": provider, "models": models, "configured": True, "error": error, "current_model": current_model if provider == current_provider else None})

@settings_bp.route('/api/settings/select', methods=['POST'])
def settings_select():
    global current_provider, current_model
    data = request.get_json() or {}
    provider = data.get("provider", "")
    model = data.get("model", "").strip()
    if provider not in PROVIDERS or not model:
        return jsonify({"error": "Укажите провайдера и модель"}), 400
    models = fetch_available_models(provider)
    if not any(item["id"] == model for item in models):
        return jsonify({"error": "Модель не найдена в доступном каталоге провайдера. Обновите список моделей."}), 400
    current_provider = provider
    current_model = model
    save_selected_model(current_provider, current_model)
    return jsonify({"success": True, "provider": current_provider, "model": current_model, "message": f"Выбрано: {model}"})

@settings_bp.route('/admin/login')
def admin_login_page():
    return render_template('admin.html')
