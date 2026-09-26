"""
routes/search.py — Веб-поиск (APILayer, Groq + search)
"""
from flask import Blueprint, request, jsonify, session
import requests
import json
import os
from ai_providers import groq_request_with_rotation
from groq_rotation import get_groq_key

search_bp = Blueprint("search", __name__)

# ---------- Поиск через APILayer Google Search ----------
APILAYER_KEY = os.getenv("APILAYER_KEY", "")

def search_web(query):
    try:
        url = "https://api.apilayer.com/google_search"
        headers = {"apikey": APILAYER_KEY}
        params = {"q": query, "hl": "ru", "gl": "ru", "num": 5}
        resp = requests.get(url, headers=headers, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        parts = []
        for item in data.get("organic_results", [])[:5]:
            title = item.get("title", "")
            snippet = item.get("snippet", "")
            link = item.get("link", "")
            if snippet:
                parts.append(f"📌 **{title}**\n{snippet}\n🔗 {link}")

        answer = data.get("answer_box", {})
        if answer:
            answer_text = answer.get("answer") or answer.get("snippet") or ""
            if answer_text:
                parts.insert(0, f"✅ **Быстрый ответ:** {answer_text}")

        if parts:
            return "Результаты поиска Google:\n\n" + "\n\n".join(parts)
        return None
    except Exception as e:
        print(f"search_web error: {e}")
        return None


# ============================================================
# ========== АВТО ПОИСК (Auto Search) ========================
# ============================================================

@search_bp.route('/api/auto_search', methods=['POST'])
def auto_search():
    """
    Авто поиск: определяет, нужен ли поиск в интернете,
    ищет через APILayer Google Search, обрабатывает через Groq.
    """
    global contents

    data = request.get_json() or {}
    user_message = data.get('message', '').strip()

    if not user_message:
        return jsonify({'error': 'Пустое сообщение'})

    provider = PROVIDERS[current_provider]

    # Шаг 1: активная модель решает, нужен ли поиск
    decision_prompt = f"""Ты — интеллектуальный фильтр для AI-ассистента.

Пользователь написал: "{user_message}"

Твоя задача: определить, нужны ли актуальные данные из интернета для ответа на этот запрос.

Запросы, требующие поиска (новости, актуальные данные, события, цены, погода, спорт, технологии, политика, наука, кино, и т.д.):
- "Какая сегодня погода?"
- "Последние новости про ..."
- "Курс доллара"
- "Кто выиграл матч вчера?"
- "Новейшие технологии ..."
- "Какие фильмы вышли в 2026?"

Запросы, НЕ требующие поиска (теория, логика, код, общие знания):
- "Объясни рекурсию"
- "Напиши код на Python"
- "Как работает нейросеть"
- "Переведи текст"
- "Реши уравнение"

Ответь ТОЛЬКО одним словом: SEARCH или DIRECT."""

    decision_payload = {
        "model": current_model,
        "messages": [{"role": "user", "content": decision_prompt}],
        "temperature": 0.1,
        "max_tokens": 300,
    }
    data_decision, error = groq_request_with_rotation(
        provider["url"], decision_payload, provider["headers"].copy(), timeout=15
    )
    if error:
        print(f"Auto search decision error: {error}")
        decision = "DIRECT"
    else:
        decision = data_decision["choices"][0]["message"]["content"].strip().upper()

    if "SEARCH" not in decision:
        return jsonify({'needs_search': False, 'reply': None})

    # Шаг 2: Генерируем поисковый запрос
    search_query_prompt = f"""Пользователь написал: "{user_message}"

Сформулируй краткий поисковый запрос для Google (1-5 слов), который поможет найти актуальную информацию.

Ответь ТОЛЬКО поисковым запросом, без кавычек и пояснений."""

    query_payload = {
        "model": current_model,
        "messages": [{"role": "user", "content": search_query_prompt}],
        "temperature": 0.1,
        "max_tokens": 128000,
    }
    data_query, error = groq_request_with_rotation(
        provider["url"], query_payload, provider["headers"].copy(), timeout=15
    )
    if error:
        print(f"Auto search query generation error: {error}")
        search_query = user_message
    else:
        search_query = data_query["choices"][0]["message"]["content"].strip()
        search_query = search_query.strip('"').strip("'")

    # Шаг 3: Поиск через APILayer
    search_result = search_web(search_query)
    if not search_result:
        search_result = "Поиск не дал результатов."

    # Шаг 4: Финальный ответ через Groq
    final_prompt = f"""Пользователь спросил: "{user_message}"

Вот актуальная информация из интернета (Google Search):
{search_result}

Твоя задача:
- Дай подробный, полезный ответ на вопрос пользователя
- Используй информацию из поиска
- Добавь своё объяснение и понимание
- Отвечай на русском языке
- Используй Markdown: заголовки, списки, таблицы если нужно
- В конце напиши: "🔍 Ответ на основе поиска Google" """

    final_payload = {
        "model": current_model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": final_prompt}
        ],
        "temperature": 0.5,
        "max_tokens": 128000,
    }
    data_final, error = groq_request_with_rotation(
        provider["url"], final_payload, provider["headers"].copy(), timeout=90
    )
    if error:
        return jsonify({
            'needs_search': True,
            'search_query': search_query,
            'reply': f'🔍 Результаты по Google "{search_query}":\n\n{search_result}\n\n_(Groq недоступен: {error})_'
        })

    reply = data_final["choices"][0]["message"]["content"]
    contents.append({"role": "user", "content": f"[Авто поиск] {user_message}"})
    contents.append({"role": "assistant", "content": reply})
    if len(contents) > 20:
        contents = contents[-20:]

    return jsonify({
        'needs_search': True,
        'search_query': search_query,
        'reply': reply
    })


# ---------- Endpoint: Поиск + Groq ----------
@search_bp.route('/api/web_search_groq', methods=['POST'])
def web_search_groq():
    """Поиск через APILayer Google + обработка через Groq"""
    global contents

    data = request.get_json() or {}
    query = data.get('query', '').strip()

    if not query:
        return jsonify({'error': 'Пустой запрос'})

    search_result = search_web(query)
    if not search_result:
        search_result = "Поиск не дал результатов. Отвечай на основе своих знаний."

    provider = PROVIDERS[current_provider]
    groq_prompt = f"""Пользователь спросил: "{query}"

Вот результаты из Google:
{search_result}

Твоя задача:
- Дай подробный, полезный ответ на вопрос пользователя
- Используй информацию из поиска
- Добавь своё объяснение и понимание
- Отвечай на русском языке
- Используй Markdown: заголовки, списки, таблицы если нужно
- В конце напиши: "🔍 *Ответ на основе поиска Google*" """

    payload = {
        "model": current_model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": groq_prompt}
        ],
        "temperature": 0.5,
        "max_tokens": 12000,
    }
    data_resp, error = groq_request_with_rotation(
        provider["url"], payload, provider["headers"].copy(), timeout=90
    )
    if error:
        return jsonify({
            'reply': f'🔍 **Результаты Google по запросу "{query}":**\n\n{search_result}\n\n*Groq недоступен: {error}*'
        })

    reply = data_resp["choices"][0]["message"]["content"]
    contents.append({"role": "user", "content": f"[Поиск Google] {query}"})
    contents.append({"role": "assistant", "content": reply})
    if len(contents) > 20:
        contents = contents[-20:]

    return jsonify({'reply': reply})

