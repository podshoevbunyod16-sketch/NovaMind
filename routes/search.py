"""
routes/search.py — Веб-поиск с реальным скрапингом страниц.
Автопоиск отображает ход работы шаг за шагом через SSE.
"""
from flask import Blueprint, request, jsonify, session, Response
import requests
import json
import os
import re
import time

from ai_providers import groq_request_with_rotation
from groq_rotation import get_groq_key
import config

search_bp = Blueprint("search", __name__)

APILAYER_KEY = os.getenv("APILAYER_KEY", "")
SERPER_KEY   = os.getenv("SERPER_KEY", "")

# ---------- Поиск ----------

def search_web_apilayer(query, num=6):
    """Поиск через APILayer Google Search. Возвращает список результатов."""
    try:
        resp = requests.get(
            "https://api.apilayer.com/google_search",
            headers={"apikey": APILAYER_KEY},
            params={"q": query, "hl": "ru", "gl": "ru", "num": num},
            timeout=15
        )
        resp.raise_for_status()
        data = resp.json()
        results = []
        box = data.get("answer_box", {})
        if box:
            results.append({
                "title": "Быстрый ответ",
                "snippet": box.get("answer") or box.get("snippet", ""),
                "url": box.get("link", ""),
                "quick": True,
            })
        for item in data.get("organic_results", [])[:num]:
            results.append({
                "title": item.get("title", ""),
                "snippet": item.get("snippet", ""),
                "url": item.get("link", ""),
                "quick": False,
            })
        return results, None
    except Exception as e:
        return [], str(e)

def search_web_serper(query, num=6):
    """Запасной поиск через Serper.dev (если нет APILayer)."""
    try:
        resp = requests.post(
            "https://google.serper.dev/search",
            headers={"X-API-KEY": SERPER_KEY, "Content-Type": "application/json"},
            json={"q": query, "num": num, "gl": "ru", "hl": "ru"},
            timeout=15
        )
        resp.raise_for_status()
        data = resp.json()
        results = []
        if data.get("answerBox"):
            box = data["answerBox"]
            results.append({
                "title": "Быстрый ответ",
                "snippet": box.get("answer") or box.get("snippet", ""),
                "url": box.get("link", ""),
                "quick": True,
            })
        for item in data.get("organic", [])[:num]:
            results.append({
                "title": item.get("title", ""),
                "snippet": item.get("snippet", ""),
                "url": item.get("link", ""),
                "quick": False,
            })
        return results, None
    except Exception as e:
        return [], str(e)

def search_web(query, num=6):
    """Выбирает доступный поисковый бэкенд."""
    if APILAYER_KEY:
        results, err = search_web_apilayer(query, num)
        if results:
            return results, None
    if SERPER_KEY:
        results, err = search_web_serper(query, num)
        if results:
            return results, None
    # Fallback: DuckDuckGo без ключа
    try:
        resp = requests.get(
            "https://api.duckduckgo.com/",
            params={"q": query, "format": "json", "no_html": "1", "skip_disambig": "1"},
            timeout=10
        )
        data = resp.json()
        results = []
        if data.get("AbstractText"):
            results.append({
                "title": data.get("Heading", query),
                "snippet": data["AbstractText"],
                "url": data.get("AbstractURL", ""),
                "quick": True,
            })
        for r in (data.get("RelatedTopics") or [])[:4]:
            if isinstance(r, dict) and r.get("Text"):
                results.append({
                    "title": r.get("Text", "")[:80],
                    "snippet": r.get("Text", ""),
                    "url": r.get("FirstURL", ""),
                    "quick": False,
                })
        return results, None
    except Exception as e:
        return [], f"DuckDuckGo error: {e}"

# ---------- Реальный скрапинг страниц ----------

_SCRAPE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Linux; Android 10) AppleWebKit/537.36 Chrome/120 Safari/537.36",
    "Accept-Language": "ru,en;q=0.9",
}

def fetch_page_text(url, max_chars=8000):
    """Загружает страницу и возвращает чистый текст (без HTML тегов)."""
    try:
        resp = requests.get(url, headers=_SCRAPE_HEADERS, timeout=12, allow_redirects=True)
        resp.raise_for_status()
        ct = resp.headers.get("Content-Type", "")
        if "html" not in ct and "text" not in ct:
            return None, "Не текстовая страница"
        html = resp.text
        # Убираем скрипты, стили, SVG
        html = re.sub(r"<(script|style|svg|noscript)[^>]*>.*?</\1>", "", html, flags=re.DOTALL | re.IGNORECASE)
        # Убираем теги
        text = re.sub(r"<[^>]+>", " ", html)
        # Убираем множественные пробелы
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = text.strip()
        return text[:max_chars], None
    except Exception as e:
        return None, str(e)

# ---------- Авто-поиск с реальными шагами (SSE) ----------

@search_bp.route("/api/auto_search_stream", methods=["POST"])
def auto_search_stream():
    """
    SSE endpoint: возвращает ход поиска в реальном времени.
    Клиент слушает события: step, result, error, done.
    """
    data = request.get_json() or {}
    user_message = data.get("message", "").strip()
    if not user_message:
        return jsonify({"error": "Пустое сообщение"}), 400

    def generate():
        def send(event, payload):
            return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"

        # ШАГ 1: Определяем нужен ли поиск
        yield send("step", {"icon": "🤔", "text": "Анализирую запрос..."})

        provider = config.PROVIDERS[config.current_provider]
        headers = {**provider["headers"]}
        if "groq" in provider["url"]:
            key = get_groq_key()
            if key:
                headers["Authorization"] = f"Bearer {key}"

        decision_payload = {
            "model": config.current_model,
            "messages": [{
                "role": "user",
                "content": (
                    f'Пользователь: "{user_message}"\n\n'
                    "Нужен ли поиск в интернете? Отвечай ТОЛЬКО: SEARCH или DIRECT.\n"
                    "SEARCH — новости, цены, события, погода, спорт, актуальные данные.\n"
                    "DIRECT — теория, код, объяснения, математика, переводы."
                )
            }],
            "max_tokens": 10,
            "temperature": 0,
        }
        resp_data, err = groq_request_with_rotation(provider["url"], decision_payload, headers, timeout=15)

        needs_search = False
        if not err:
            decision = (resp_data.get("choices", [{}])[0].get("message", {}).get("content", "") or "").strip().upper()
            needs_search = "SEARCH" in decision

        if not needs_search:
            # Прямой ответ без поиска
            yield send("step", {"icon": "💬", "text": "Поиск не нужен, отвечаю напрямую..."})
            answer_payload = {
                "model": config.current_model,
                "messages": [
                    {"role": "system", "content": "Отвечай чётко и по делу на русском языке."},
                    {"role": "user", "content": user_message},
                ],
                "max_tokens": provider.get("max_tokens", 8192),
                "temperature": 0.7,
            }
            ans_data, ans_err = groq_request_with_rotation(provider["url"], answer_payload, headers, timeout=60)
            if ans_err:
                yield send("error", {"text": f"Ошибка AI: {ans_err}"})
            else:
                reply = ans_data["choices"][0]["message"]["content"]
                yield send("result", {"reply": reply, "searched": False, "sources": []})
            yield send("done", {})
            return

        # ШАГ 2: Формируем поисковый запрос
        yield send("step", {"icon": "🔍", "text": "Формирую поисковый запрос..."})

        query_payload = {
            "model": config.current_model,
            "messages": [{
                "role": "user",
                "content": f'Сформулируй поисковый запрос для Google по теме: "{user_message}". Только запрос, без лишних слов.',
            }],
            "max_tokens": 30,
            "temperature": 0,
        }
        q_data, q_err = groq_request_with_rotation(provider["url"], query_payload, headers, timeout=15)
        search_query = user_message
        if not q_err:
            q_text = (q_data.get("choices", [{}])[0].get("message", {}).get("content", "") or "").strip()
            if q_text and len(q_text) < 120:
                search_query = q_text

        yield send("step", {"icon": "🌐", "text": f'Ищу: "{search_query}"...'})

        # ШАГ 3: Поиск
        results, search_err = search_web(search_query)
        if search_err and not results:
            yield send("error", {"text": f"Ошибка поиска: {search_err}"})
            yield send("done", {})
            return

        yield send("step", {"icon": "📋", "text": f"Найдено {len(results)} результатов. Читаю страницы..."})

        # ШАГ 4: Скрапим топ-3 страницы
        scraped = []
        for i, res in enumerate(results[:3]):
            url = res.get("url", "")
            if not url or res.get("quick"):
                scraped.append({"url": url, "title": res["title"], "text": res["snippet"]})
                continue
            yield send("step", {"icon": "📄", "text": f'Открываю: {res["title"][:50]}...'})
            page_text, page_err = fetch_page_text(url)
            if page_text:
                scraped.append({"url": url, "title": res["title"], "text": page_text})
                yield send("step", {"icon": "✅", "text": f'Прочитал: {res["title"][:50]}'})
            else:
                scraped.append({"url": url, "title": res["title"], "text": res["snippet"]})
                yield send("step", {"icon": "⚠️", "text": f'Не удалось открыть: {page_err or "нет контента"}'})

        # ШАГ 5: Синтез ответа
        yield send("step", {"icon": "🧠", "text": "Анализирую и формирую ответ..."})

        context_parts = []
        sources = []
        for s in scraped:
            if s.get("text"):
                context_parts.append(f'### {s["title"]}\n{s["text"][:3000]}')
                if s.get("url"):
                    sources.append({"title": s["title"], "url": s["url"]})

        context = "\n\n".join(context_parts)
        final_payload = {
            "model": config.current_model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Ты — умный поисковый ассистент. Используй ТОЛЬКО предоставленные данные из интернета. "
                        "Не выдумывай. Отвечай на русском языке в Markdown. "
                        "В конце укажи источники как [1], [2] и т.д."
                    ),
                },
                {
                    "role": "user",
                    "content": f"Вопрос: {user_message}\n\nДанные из интернета:\n{context}",
                },
            ],
            "max_tokens": provider.get("max_tokens", 8192),
            "temperature": 0.3,
        }
        final_data, final_err = groq_request_with_rotation(provider["url"], final_payload, headers, timeout=90)
        if final_err:
            yield send("error", {"text": f"Ошибка AI: {final_err}"})
        else:
            reply = final_data["choices"][0]["message"]["content"]
            yield send("result", {"reply": reply, "searched": True, "sources": sources, "query": search_query})

        yield send("done", {})

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------- Обычный авто-поиск (JSON, для совместимости) ----------

@search_bp.route('/api/auto_search', methods=['POST'])
def auto_search():
    data = request.get_json() or {}
    user_message = data.get('message', '').strip()
    if not user_message:
        return jsonify({'error': 'Пустое сообщение'})

    provider = config.PROVIDERS[config.current_provider]
    headers = {**provider["headers"]}
    if "groq" in provider["url"]:
        key = get_groq_key()
        if key:
            headers["Authorization"] = f"Bearer {key}"

    decision_payload = {
        "model": config.current_model,
        "messages": [{"role": "user", "content": f'"{user_message}"\nSEARCH или DIRECT?'}],
        "max_tokens": 10, "temperature": 0,
    }
    resp_data, err = groq_request_with_rotation(provider["url"], decision_payload, headers, timeout=15)
    needs_search = False
    if not err:
        dec = (resp_data.get("choices", [{}])[0].get("message", {}).get("content", "") or "").upper()
        needs_search = "SEARCH" in dec

    if not needs_search:
        return jsonify({"needs_search": False, "search_query": "", "reply": "", "sources": []})

    results, _ = search_web(user_message)
    context = "\n\n".join(
        f'### {r["title"]}\n{r["snippet"]}' for r in results if r.get("snippet")
    )
    final_payload = {
        "model": config.current_model,
        "messages": [
            {"role": "system", "content": "Отвечай на русском, используй только данные ниже."},
            {"role": "user", "content": f"Вопрос: {user_message}\n\nДанные:\n{context}"},
        ],
        "max_tokens": provider.get("max_tokens", 8192), "temperature": 0.3,
    }
    final_data, final_err = groq_request_with_rotation(provider["url"], final_payload, headers, timeout=90)
    if final_err:
        return jsonify({"error": final_err})
    reply = final_data["choices"][0]["message"]["content"]
    sources = [{"title": r["title"], "url": r["url"]} for r in results if r.get("url")]
    return jsonify({"needs_search": True, "search_query": user_message, "reply": reply, "sources": sources})


# ---------- Ручной поиск Groq ----------

@search_bp.route('/api/web_search_groq', methods=['POST'])
def web_search_groq():
    data = request.get_json() or {}
    user_message = data.get('message', '').strip()
    if not user_message:
        return jsonify({'error': 'Пустое сообщение'})

    results, err = search_web(user_message)
    if err and not results:
        return jsonify({"error": f"Поиск недоступен: {err}"})

    context = "\n\n".join(
        f'📌 **{r["title"]}**\n{r["snippet"]}\n🔗 {r["url"]}' for r in results if r.get("snippet")
    )

    provider = config.PROVIDERS[config.current_provider]
    headers = {**provider["headers"]}
    if "groq" in provider["url"]:
        key = get_groq_key()
        if key:
            headers["Authorization"] = f"Bearer {key}"

    payload = {
        "model": config.current_model,
        "messages": [
            {"role": "system", "content": "Ты — поисковый ассистент. Отвечай на русском, используй только данные."},
            {"role": "user", "content": f"Вопрос: {user_message}\n\nРезультаты поиска:\n{context}"},
        ],
        "max_tokens": provider.get("max_tokens", 8192), "temperature": 0.3,
    }
    resp_data, resp_err = groq_request_with_rotation(provider["url"], payload, headers, timeout=90)
    if resp_err:
        return jsonify({"error": resp_err})
    reply = resp_data["choices"][0]["message"]["content"]
    sources = [{"title": r["title"], "url": r["url"]} for r in results if r.get("url")]
    return jsonify({"reply": reply, "sources": sources})
