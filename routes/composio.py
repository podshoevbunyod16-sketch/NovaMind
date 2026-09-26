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
        resp = requests.get('https://backend.composio.dev/api/v3.1/connected_accounts', headers=headers, timeout=10)
        resp.raise_for_status()
        return jsonify(resp.json())
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
        session_resp = requests.post('https://backend.composio.dev/api/v3.1/tool_router/session', headers=headers, json={"user_id": "novauser"}, timeout=10)
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
