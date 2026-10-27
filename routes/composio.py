"""
routes/composio.py — Интеграция с Composio (внешние сервисы)
"""
from flask import Blueprint, request, jsonify, render_template, session
import requests
import os

composio_bp = Blueprint("composio", __name__)

@composio_bp.route('/composio')
def composio_page():
    return render_template('composio.html')

@composio_bp.route('/api/composio/connect', methods=['POST'])
def composio_connect():
    if not session.get('admin_logged_in'):
        return jsonify({'error': 'Только администратор может настроить общий API-ключ Composio'}), 403
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

@composio_bp.route('/api/composio/integrations', methods=['GET'])
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

@composio_bp.route('/api/composio/connected_accounts', methods=['GET'])
def composio_connected_accounts():
    api_key = os.getenv('COMPOSIO_API_KEY', '')
    if not api_key:
        return jsonify({'error': 'Composio не подключен'}), 400
    try:
        headers = {'x-api-key': api_key, 'Content-Type': 'application/json'}
        resp = requests.get('https://backend.composio.dev/api/v3.1/connected_accounts', headers=headers, params={'user_id': _composio_user_id()}, timeout=10)
        resp.raise_for_status()
        payload = resp.json()
        records = payload if isinstance(payload, list) else payload.get('items', payload.get('connected_accounts', [])) if isinstance(payload, dict) else []
        uid = _composio_user_id()
        own = [item for item in records if isinstance(item, dict) and str(item.get('user_id') or item.get('user_uid') or '') == uid]
        return jsonify({'items': own, 'connected_accounts': own})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@composio_bp.route('/api/composio/connect_account', methods=['POST'])
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
        session_resp = requests.post('https://backend.composio.dev/api/v3.1/tool_router/session', headers=headers, json={"user_id": _composio_user_id()}, timeout=10)
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

@composio_bp.route('/api/composio/execute', methods=['POST'])
def composio_execute():
    api_key = os.getenv('COMPOSIO_API_KEY', '')
    if not api_key:
        return jsonify({'error': 'Composio не подключен'}), 400
    data = request.get_json() or {}
    action_name = data.get('action', '')
    params = data.get('params', {})
    connected_account_id = data.get('connected_account_id', '')
    if connected_account_id:
        try:
            check_headers = {'x-api-key': api_key, 'Content-Type': 'application/json'}
            check = requests.get('https://backend.composio.dev/api/v3.1/connected_accounts', headers=check_headers, params={'user_id': _composio_user_id()}, timeout=10)
            check.raise_for_status()
            payload = check.json()
            records = payload if isinstance(payload, list) else payload.get('items', payload.get('connected_accounts', [])) if isinstance(payload, dict) else []
            allowed_ids = {str(item.get('id') or item.get('connected_account_id') or '') for item in records if isinstance(item, dict) and str(item.get('user_id') or item.get('user_uid') or '') == _composio_user_id()}
            if str(connected_account_id) not in allowed_ids:
                return jsonify({'error': 'Подключение не найдено для текущего пользователя'}), 404
        except Exception:
            return jsonify({'error': 'Не удалось проверить владельца подключения; действие заблокировано'}), 502
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

@composio_bp.route('/api/composio/actions', methods=['GET'])
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



# ========== LEGACY HISTORY API ==========

# >>> NOVA-USER-ISOLATION
def _composio_user_id():
    """Stable Composio user ID derived from the authenticated NovaMind identity."""
    import hashlib
    import identity
    if not (session.get("nova_google_login") or session.get("admin_logged_in") or session.get("nova_signed_in")):
        return ""
    return "nova_" + hashlib.sha256(identity.chat_owner().encode("utf-8")).hexdigest()[:40]


@composio_bp.before_request
def _nova_composio_auth_guard():
    if request.path in ("/api/composio/connected_accounts", "/api/composio/connect_account", "/api/composio/execute"):
        if not _composio_user_id():
            return jsonify({"error": "Войдите в аккаунт для доступа к Composio"}), 401
    return None
# <<< NOVA-USER-ISOLATION
