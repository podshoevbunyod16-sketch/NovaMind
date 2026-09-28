"""
routes/search.py — Веб-поиск с реальными результатами.
Приоритет: Groq compound-beta → SearXNG → DDG HTML → AI из знаний
"""
from flask import Blueprint, request, jsonify, session, Response
import requests
import json
import os
import re
import time

from ai_providers import groq_request_with_rotation
from groq_rotation import get_groq_key, GROQ_KEYS
import config

search_bp = Blueprint("search", __name__)

APILAYER_KEY = os.getenv("APILAYER_KEY", "")
SERPER_KEY   = os.getenv("SERPER_KEY", "")

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Linux; Android 12; Pixel 6) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
}

# ═══ Groq compound-beta — встроенный поиск (бесплатно!) ═══

def groq_web_search(query, max_tokens=8192):
    """
    Использует Groq compound-beta-mini с встроенным web search.
    Возвращает (answer, sources_list) или (None, error).
    """
    key = get_groq_key()
    if not key:
        return None, "Нет Groq ключа"
    try:
        resp = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={
                "model": "compound-beta-mini",
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "Ты — поисковый ассистент. Отвечай ПОДРОБНО на русском языке. "
                            "Давай конкретные цифры, факты, источники. "
                            "Если вопрос о ценах — указывай диапазон, год выпуска, состояние авто. "
                            "Структурируй ответ: заголовки, списки, конкретные данные. "
                            "В конце всегда перечисляй источники."
                        )
                    },
                    {"role": "user", "content": query}
                ],
                "max_tokens": min(max_tokens, 8192),
                "temperature": 0.3,
                "tools": [{"type": "web_search_preview"}],
            },
            timeout=40
        )
        if resp.status_code == 200:
            data = resp.json()
            choice = data.get("choices", [{}])[0]
            answer = choice.get("message", {}).get("content", "")
            # Собираем источники из tool_calls если есть
            sources = []
            for msg in data.get("choices", [{}])[0].get("message", {}).get("tool_calls", []):
                if isinstance(msg, dict):
                    inp = msg.get("function", {}).get("arguments", "{}")
                    try:
                        args = json.loads(inp)
                        if args.get("url"):
                            sources.append({"title": args.get("query", ""), "url": args["url"]})
                    except Exception:
                        pass
            return answer, sources
        elif resp.status_code == 422:
            # compound-beta не поддерживается для этого ключа
            return None, f"compound-beta недоступен: {resp.status_code}"
        else:
            return None, f"Groq compound error: {resp.status_code} {resp.text[:100]}"
    except Exception as e:
        return None, str(e)

# ═══ SearXNG — поиск без ключа ═══

SEARXNG_INSTANCES = [
    "https://searx.be",
    "https://search.inetol.net",
    "https://searxng.site",
    "https://search.mdosch.de",
    "https://priv.au",
    "https://search.ononoki.org",
]

def searxng_search(query, num=8):
    """Пробует несколько SearXNG инстансов по очереди."""
    for inst in SEARXNG_INSTANCES:
        try:
            resp = requests.get(
                f"{inst}/search",
                params={"q": query, "format": "json", "language": "ru-RU",
                        "time_range": "", "categories": "general", "pageno": 1},
                headers={**_HEADERS, "Accept": "application/json"},
                timeout=8
            )
            if resp.status_code == 200:
                try:
                    data = resp.json()
                    results = data.get("results", [])
                    if results:
                        return [
                            {
                                "title": r.get("title", ""),
                                "snippet": r.get("content", r.get("snippet", ""))[:500],
                                "url": r.get("url", ""),
                            }
                            for r in results[:num] if r.get("title")
                        ], None
                except Exception:
                    continue
        except Exception:
            continue
    return [], "SearXNG: все инстансы недоступны"

# ═══ DuckDuckGo HTML ═══

def ddg_html_search(query, num=6):
    """DuckDuckGo через HTML без API."""
    try:
        resp = requests.post(
            "https://html.duckduckgo.com/html/",
            data={"q": query, "kl": "ru-ru"},
            headers=_HEADERS,
            timeout=10,
            allow_redirects=True
        )
        if resp.status_code != 200:
            return [], f"DDG HTTP {resp.status_code}"
        html = resp.text
        results = []
        # Парсим заголовки
        titles  = re.findall(r'class="result__a"[^>]*href="([^"]+)"[^>]*>([^<]+)', html)
        snippets = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', html, re.DOTALL)
        clean_snip = [re.sub(r'<[^>]+>', '', s).strip() for s in snippets]
        for i, (url, title) in enumerate(titles[:num]):
            results.append({
                "title": title.strip(),
                "snippet": clean_snip[i] if i < len(clean_snip) else "",
                "url": url,
            })
        return results, None if results else "DDG: пустой ответ"
    except Exception as e:
        return [], f"DDG error: {e}"

# ═══ APILayer / Serper ═══

def search_web_apilayer(query, num=6):
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
        if data.get("answer_box", {}).get("snippet"):
            box = data["answer_box"]
            results.append({"title": "Быстрый ответ", "snippet": box.get("snippet",""),
                             "url": box.get("link",""), "quick": True})
        for item in data.get("organic_results", [])[:num]:
            results.append({"title": item.get("title",""), "snippet": item.get("snippet",""),
                             "url": item.get("link","")})
        return results, None
    except Exception as e:
        return [], str(e)

def search_web_serper(query, num=6):
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
        if data.get("answerBox", {}).get("snippet"):
            box = data["answerBox"]
            results.append({"title": "Быстрый ответ", "snippet": box.get("snippet",""),
                             "url": box.get("link",""), "quick": True})
        for item in data.get("organic", [])[:num]:
            results.append({"title": item.get("title",""), "snippet": item.get("snippet",""),
                             "url": item.get("link","")})
        return results, None
    except Exception as e:
        return [], str(e)

def search_web(query, num=8):
    """Основная функция: перебирает бэкенды до первого успеха."""
    if APILAYER_KEY:
        r, e = search_web_apilayer(query, num)
        if r:
            return r, None
    if SERPER_KEY:
        r, e = search_web_serper(query, num)
        if r:
            return r, None
    r, e = searxng_search(query, num)
    if r:
        return r, None
    r, e = ddg_html_search(query, num)
    if r:
        return r, None
    return [], "Все поисковые бэкенды недоступны"

# ═══ Скрапинг страниц через Jina Reader ═══

def fetch_page_text(url, max_chars=6000):
    """Читает страницу через Jina AI Reader (обходит блокировки)."""
    # Сначала пробуем Jina (надёжнее обходит защиту)
    try:
        jina_url = f"https://r.jina.ai/{url}"
        resp = requests.get(jina_url,
            headers={"User-Agent": "Mozilla/5.0", "Accept": "text/plain,*/*"},
            timeout=15)
        if resp.status_code == 200 and len(resp.text) > 200:
            text = resp.text
            # Убираем markdown изображения
            text = re.sub(r'!\[.*?\]\(.*?\)', '', text)
            text = re.sub(r'\n{3,}', '\n\n', text).strip()
            return text[:max_chars], None
    except Exception:
        pass
    # Fallback: прямой запрос
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=12, allow_redirects=True)
        resp.raise_for_status()
        ct = resp.headers.get("Content-Type", "")
        if "html" not in ct and "text" not in ct:
            return None, "Не текстовая страница"
        html = resp.text
        html = re.sub(r'<(script|style|svg|noscript)[^>]*>.*?</\1>', '', html,
                      flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<[^>]+>', ' ', html)
        text = re.sub(r'[ \t]+', ' ', text)
        text = re.sub(r'\n{3,}', '\n\n', text).strip()
        return text[:max_chars], None
    except Exception as e:
        return None, str(e)

# ═══ ДЕТАЛЬНЫЙ СИСТЕМНЫЙ ПРОМПТ ═══

SEARCH_SYSTEM_PROMPT = """Ты — умный поисковый ассистент NovaMind. Отвечай ПОДРОБНО и КОНКРЕТНО на русском языке.

ПРАВИЛА:
1. Давай развёрнутые ответы с конкретными фактами, цифрами, датами
2. Если вопрос о ценах — укажи диапазон цен, год выпуска, состояние, пробег
3. Если вопрос о событиях — укажи дату, место, участников
4. Структурируй ответ: используй заголовки (##), списки (-), таблицы
5. Цитируй источники как [1], [2] в тексте
6. Если данных недостаточно — скажи что известно и что нет
7. НЕ говори "на основе предоставленных данных невозможно ответить" — всегда давай максимум информации
8. Если поиск не дал результатов — отвечай из своих знаний с пометкой (из базы знаний)"""

# ═══ SSE авто-поиск ═══

@search_bp.route("/api/auto_search_stream", methods=["POST"])
def auto_search_stream():
    data = request.get_json() or {}
    user_message = data.get("message", "").strip()
    if not user_message:
        return jsonify({"error": "Пустое сообщение"}), 400

    def generate():
        def send(event, payload):
            return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"

        yield send("step", {"icon": "🤔", "text": "Анализирую запрос..."})

        provider = config.PROVIDERS[config.current_provider]
        p_headers = {**provider["headers"]}
        key = get_groq_key()
        if key and "groq" in provider["url"]:
            p_headers["Authorization"] = f"Bearer {key}"

        # Определяем нужен ли поиск
        dec_payload = {
            "model": config.current_model,
            "messages": [{"role": "user", "content":
                f'"{user_message}"\nНужен поиск в интернете? ТОЛЬКО: SEARCH или DIRECT\n'
                'SEARCH: цены, новости, погода, события, текущий статус\n'
                'DIRECT: теория, математика, код, переводы, объяснения'}],
            "max_tokens": 10, "temperature": 0,
        }
        dec_data, dec_err = groq_request_with_rotation(provider["url"], dec_payload, p_headers, timeout=10)
        needs_search = True  # По умолчанию ищем
        if not dec_err:
            dec = (dec_data.get("choices",[{}])[0].get("message",{}).get("content","") or "").upper()
            needs_search = "SEARCH" in dec or "DIRECT" not in dec

        if not needs_search:
            yield send("step", {"icon": "💬", "text": "Готовлю подробный ответ..."})
            ans_payload = {
                "model": config.current_model,
                "messages": [
                    {"role": "system", "content": "Отвечай подробно, структурированно, на русском. Давай конкретные факты."},
                    {"role": "user", "content": user_message},
                ],
                "max_tokens": provider.get("max_tokens", 8192),
                "temperature": 0.5,
            }
            ans, ans_err = groq_request_with_rotation(provider["url"], ans_payload, p_headers, timeout=60)
            if ans_err:
                yield send("error", {"text": f"Ошибка: {ans_err}"})
            else:
                reply = ans["choices"][0]["message"]["content"]
                yield send("result", {"reply": reply, "searched": False, "sources": []})
            yield send("done", {})
            return

        # === ПОИСК ===
        # Попытка 1: Groq compound-beta (встроенный поиск)
        yield send("step", {"icon": "🔍", "text": "Ищу через Groq Web Search..."})
        compound_answer, compound_sources = groq_web_search(
            user_message,
            max_tokens=provider.get("max_tokens", 8192)
        )

        if compound_answer and len(compound_answer) > 100:
            yield send("step", {"icon": "✅", "text": "Groq Web Search вернул результаты!"})
            sources = compound_sources if isinstance(compound_sources, list) else []
            yield send("result", {"reply": compound_answer, "searched": True, "sources": sources, "query": user_message})
            yield send("done", {})
            return

        # Попытка 2: SearXNG + скрапинг
        yield send("step", {"icon": "🌐", "text": "Ищу через SearXNG..."})

        # Формируем поисковый запрос
        q_payload = {
            "model": config.current_model,
            "messages": [{"role": "user", "content":
                f'Запрос для Google: "{user_message}". Только запрос на русском/английском, без лишних слов.'}],
            "max_tokens": 30, "temperature": 0,
        }
        q_data, _ = groq_request_with_rotation(provider["url"], q_payload, p_headers, timeout=10)
        search_query = user_message
        if not _:
            q = (q_data.get("choices",[{}])[0].get("message",{}).get("content","") or "").strip()
            if q and len(q) < 100:
                search_query = q

        results, search_err = search_web(search_query)

        if not results:
            yield send("step", {"icon": "⚠️", "text": f"Поиск не дал результатов. Отвечаю из знаний..."})
            # Отвечаем из базы знаний с пометкой
            ans_payload = {
                "model": config.current_model,
                "messages": [
                    {"role": "system", "content": SEARCH_SYSTEM_PROMPT},
                    {"role": "user", "content":
                        f"{user_message}\n\n[Поиск в интернете не дал результатов. "
                        f"Отвечай из своих знаний с пометкой (из базы знаний) и укажи что данные могут быть устаревшими.]"},
                ],
                "max_tokens": provider.get("max_tokens", 8192),
                "temperature": 0.5,
            }
            ans, ans_err = groq_request_with_rotation(provider["url"], ans_payload, p_headers, timeout=60)
            reply = ans["choices"][0]["message"]["content"] if not ans_err else f"❌ {ans_err}"
            yield send("result", {"reply": reply, "searched": False, "sources": [], "query": search_query})
            yield send("done", {})
            return

        yield send("step", {"icon": "📋", "text": f"Найдено {len(results)} результатов. Читаю страницы..."})

        # Скрапим топ-3
        scraped = []
        for res in results[:4]:
            url_to_scrape = res.get("url", "")
            if not url_to_scrape:
                scraped.append({"title": res["title"], "text": res["snippet"], "url": ""})
                continue
            yield send("step", {"icon": "📄", "text": f"Открываю: {res['title'][:55]}..."})
            page_text, page_err = fetch_page_text(url_to_scrape)
            if page_text and len(page_text) > 100:
                scraped.append({"title": res["title"], "text": page_text, "url": url_to_scrape})
                yield send("step", {"icon": "✅", "text": f"Прочитал ({len(page_text)} символов): {res['title'][:45]}"})
            else:
                # Используем snippet
                scraped.append({"title": res["title"], "text": res.get("snippet",""), "url": url_to_scrape})
                yield send("step", {"icon": "📝", "text": f"Использую сниппет: {res['title'][:45]}"})

        yield send("step", {"icon": "🧠", "text": "Анализирую и пишу подробный ответ..."})

        sources = [{"title": s["title"], "url": s["url"]} for s in scraped if s.get("url")]
        context = "\n\n".join(
            f"### [{i+1}] {s['title']}\nURL: {s['url']}\n{s['text'][:3000]}"
            for i, s in enumerate(scraped) if s.get("text")
        )

        final_payload = {
            "model": config.current_model,
            "messages": [
                {"role": "system", "content": SEARCH_SYSTEM_PROMPT},
                {"role": "user", "content":
                    f"Вопрос: {user_message}\n\nДанные из интернета:\n{context}"},
            ],
            "max_tokens": provider.get("max_tokens", 8192),
            "temperature": 0.3,
        }
        final_data, final_err = groq_request_with_rotation(provider["url"], final_payload, p_headers, timeout=90)
        if final_err:
            yield send("error", {"text": f"Ошибка AI: {final_err}"})
        else:
            reply = final_data["choices"][0]["message"]["content"]
            yield send("result", {"reply": reply, "searched": True, "sources": sources, "query": search_query})
        yield send("done", {})

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ═══ /api/auto_search (JSON) ═══

@search_bp.route("/api/auto_search", methods=["POST"])
def auto_search():
    data = request.get_json() or {}
    user_message = data.get("message", "").strip()
    if not user_message:
        return jsonify({"error": "Пустое сообщение"})

    # Groq compound-beta первым
    answer, sources = groq_web_search(user_message)
    if answer and len(answer) > 50:
        return jsonify({"needs_search": True, "reply": answer,
                        "sources": sources if isinstance(sources, list) else [],
                        "search_query": user_message})

    results, _ = search_web(user_message)
    context = "\n\n".join(
        f"### {r['title']}\n{r['snippet']}" for r in results if r.get("snippet")
    )
    provider = config.PROVIDERS[config.current_provider]
    p_headers = {**provider["headers"]}
    key = get_groq_key()
    if key and "groq" in provider["url"]:
        p_headers["Authorization"] = f"Bearer {key}"

    payload = {
        "model": config.current_model,
        "messages": [
            {"role": "system", "content": SEARCH_SYSTEM_PROMPT},
            {"role": "user", "content": f"Вопрос: {user_message}\n\nДанные:\n{context}" if context
             else f"{user_message}\n[Поиск не дал результатов. Отвечай из своих знаний.]"},
        ],
        "max_tokens": provider.get("max_tokens", 8192),
        "temperature": 0.3,
    }
    resp_data, resp_err = groq_request_with_rotation(provider["url"], payload, p_headers, timeout=90)
    if resp_err:
        return jsonify({"error": resp_err})
    reply = resp_data["choices"][0]["message"]["content"]
    src = [{"title": r["title"], "url": r["url"]} for r in results if r.get("url")]
    return jsonify({"needs_search": bool(results), "reply": reply,
                    "sources": src, "search_query": user_message})


# ═══ /api/web_search_groq ═══

@search_bp.route("/api/web_search_groq", methods=["POST"])
def web_search_groq():
    data = request.get_json() or {}
    user_message = data.get("message", "").strip()
    if not user_message:
        return jsonify({"error": "Пустое сообщение"})

    # Groq compound-beta
    answer, sources = groq_web_search(user_message)
    if answer and len(answer) > 50:
        src = sources if isinstance(sources, list) else []
        return jsonify({"reply": answer, "sources": src})

    # Fallback поиск
    results, err = search_web(user_message)
    context = "\n\n".join(
        f"📌 **{r['title']}**\n{r['snippet']}\n🔗 {r['url']}"
        for r in results if r.get("snippet")
    )
    provider = config.PROVIDERS[config.current_provider]
    p_headers = {**provider["headers"]}
    key = get_groq_key()
    if key and "groq" in provider["url"]:
        p_headers["Authorization"] = f"Bearer {key}"

    payload = {
        "model": config.current_model,
        "messages": [
            {"role": "system", "content": SEARCH_SYSTEM_PROMPT},
            {"role": "user", "content":
                f"Вопрос: {user_message}\n\nРезультаты:\n{context}" if context
                else f"{user_message}\n[Поиск не дал результатов. Отвечай из своих знаний с пометкой.]"},
        ],
        "max_tokens": provider.get("max_tokens", 8192),
        "temperature": 0.3,
    }
    resp_data, resp_err = groq_request_with_rotation(provider["url"], payload, p_headers, timeout=90)
    if resp_err:
        return jsonify({"error": resp_err})
    reply = resp_data["choices"][0]["message"]["content"]
    src = [{"title": r["title"], "url": r["url"]} for r in results if r.get("url")]
    return jsonify({"reply": reply, "sources": src})
