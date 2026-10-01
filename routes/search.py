"""
routes/search.py — Веб-поиск с реальными результатами и живым стримом шагов.

Порядок бэкендов (настраивается через SEARCH_BACKENDS в .env):
  apilayer → serper → searxng → ddg → ddg_lite → wiki

Ключевые отличия от прошлой версии:
  * нет привязки к Groq: ответы генерирует единый слой ai_providers.chat_*;
  * поток событий — NDJSON, чтобы фронт показывал сцену поиска, шаги,
    найденные источники и печатающийся ответ;
  * каждый бэкенд логируется (время, ошибка) → /api/search/health.
"""
from flask import Blueprint, request, jsonify, Response, stream_with_context
import json
import os
import re
import time
import threading
import concurrent.futures
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse, quote_plus

import requests

import config
import local_llm
from ai_providers import chat_completion, chat_stream, resolve_target, PROVIDER_LABELS

search_bp = Blueprint("search", __name__)

APILAYER_KEY = os.getenv("APILAYER_KEY", "").strip()
SERPER_KEY = os.getenv("SERPER_KEY", "").strip()

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Linux; Android 12; Pixel 6) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
}

DEFAULT_BACKENDS = ["apilayer", "serper", "bing", "searxng", "mojeek",
                     "ddg", "ddg_lite", "wiki"]

SEARXNG_INSTANCES = [
    item.strip()
    for item in os.getenv(
        "SEARXNG_INSTANCES",
        "https://searx.be,https://search.inetol.net,https://searxng.site,"
        "https://search.mdosch.de,https://priv.au,https://search.ononoki.org,"
        "https://baresearch.org,https://search.rhscz.eu",
    ).split(",")
    if item.strip()
]

SEARCH_TIMEOUT = float(os.getenv("SEARCH_TIMEOUT", "9"))
READER_TIMEOUT = float(os.getenv("READER_TIMEOUT", "10"))
# Сколько ждём модель после сбора источников. У reasoning-моделей первый токен
# приходит долго, поэтому времени заметно больше, чем у обычного поиска.
ANSWER_TIMEOUT = float(os.getenv("ANSWER_TIMEOUT", "180"))
# Сколько страниц успеваем прочитать перед ответом.
MAX_PAGES = int(os.getenv("SEARCH_MAX_PAGES", "3"))
# Таймаут служебных запросов (короткий поисковый запрос, «нужен ли поиск»).
QUERY_TIMEOUT = float(os.getenv("SEARCH_QUERY_TIMEOUT", "10"))


def search_backends():
    configured = os.getenv("SEARCH_BACKENDS", "").strip()
    if not configured:
        return list(DEFAULT_BACKENDS)
    return [name.strip() for name in configured.split(",") if name.strip()]


# ══════════════════════════════════════════════════════════════════
# БЭКЕНДЫ ПОИСКА
# ══════════════════════════════════════════════════════════════════

def _norm_result(title, url, snippet=""):
    return {
        "title": (title or "").strip()[:220],
        "url": (url or "").strip(),
        "snippet": (snippet or "").strip()[:600],
        "host": urlparse(url or "").netloc,
    }


def search_web_apilayer(query, num=8):
    if not APILAYER_KEY:
        return [], "нет ключа APILAYER_KEY"
    resp = requests.get(
        "https://api.apilayer.com/google_search",
        headers={"apikey": APILAYER_KEY},
        params={"q": query, "hl": "ru", "gl": "ru", "num": num},
        timeout=SEARCH_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    results = []
    box = (data.get("answer_box") or {}).get("snippet")
    if box:
        results.append({**_norm_result("Быстрый ответ", (data.get("answer_box") or {}).get("link", ""), box),
                        "quick": True})
    for item in (data.get("organic_results") or [])[:num]:
        results.append(_norm_result(item.get("title"), item.get("link"), item.get("snippet")))
    return [r for r in results if r["title"]], None


def search_web_serper(query, num=8):
    if not SERPER_KEY:
        return [], "нет ключа SERPER_KEY"
    resp = requests.post(
        "https://google.serper.dev/search",
        headers={"X-API-KEY": SERPER_KEY, "Content-Type": "application/json"},
        json={"q": query, "num": num, "gl": "ru", "hl": "ru"},
        timeout=SEARCH_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    results = []
    box = (data.get("answerBox") or {}).get("snippet")
    if box:
        results.append({**_norm_result("Быстрый ответ", (data.get("answerBox") or {}).get("link", ""), box),
                        "quick": True})
    for item in (data.get("organic") or [])[:num]:
        results.append(_norm_result(item.get("title"), item.get("link"), item.get("snippet")))
    return [r for r in results if r["title"]], None


def search_web_searxng(query, num=8):
    last_error = "нет доступных инстансов"
    for instance in SEARXNG_INSTANCES:
        try:
            resp = requests.get(
                f"{instance.rstrip('/')}/search",
                params={"q": query, "format": "json", "language": "ru-RU",
                        "categories": "general", "pageno": 1},
                headers={**_HEADERS, "Accept": "application/json"},
                timeout=SEARCH_TIMEOUT,
            )
        except requests.RequestException as exc:
            last_error = f"{instance}: {exc.__class__.__name__}"
            continue
        if resp.status_code != 200:
            last_error = f"{instance}: HTTP {resp.status_code}"
            continue
        try:
            data = resp.json()
        except ValueError:
            last_error = f"{instance}: ответ не JSON"
            continue
        results = [_norm_result(r.get("title"), r.get("url"),
                                r.get("content") or r.get("snippet") or "")
                   for r in (data.get("results") or [])[:num]]
        results = [r for r in results if r["title"] and r["url"]]
        if results:
            return results, None
        last_error = f"{instance}: пустая выдача"
    return [], last_error


def search_web_ddg(query, num=8):
    resp = requests.post(
        "https://html.duckduckgo.com/html/",
        data={"q": query, "kl": "ru-ru"},
        headers=_HEADERS,
        timeout=SEARCH_TIMEOUT,
        allow_redirects=True,
    )
    if resp.status_code != 200:
        return [], f"DDG HTTP {resp.status_code}"
    return _parse_ddg(resp.text, num)


def search_web_ddg_lite(query, num=8):
    resp = requests.post(
        "https://lite.duckduckgo.com/lite/",
        data={"q": query, "kl": "ru-ru"},
        headers=_HEADERS,
        timeout=SEARCH_TIMEOUT,
        allow_redirects=True,
    )
    if resp.status_code != 200:
        return [], f"DDG Lite HTTP {resp.status_code}"
    return _parse_ddg(resp.text, num)


def _parse_ddg(html, num=8):
    """Общий парсер HTML-выдачи DuckDuckGo (html и lite версии)."""
    links = re.findall(
        r'<a[^>]+class="[^"]*result-link[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
        html, re.DOTALL | re.IGNORECASE,
    )
    if not links:
        links = re.findall(
            r'<a[^>]+rel="nofollow"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
            html, re.DOTALL | re.IGNORECASE,
        )
    snippets = re.findall(r'class="[^"]*result-snippet[^"]*"[^>]*>(.*?)</(?:a|td|div)>',
                          html, re.DOTALL | re.IGNORECASE)
    clean_snippets = [re.sub(r"<[^>]+>", "", s).strip() for s in snippets]

    results = []
    for index, (raw_url, raw_title) in enumerate(links):
        url = _unwrap_ddg_url(raw_url)
        title = re.sub(r"<[^>]+>", "", raw_title).strip()
        if not url or not title or "duckduckgo.com/y.js" in url:
            continue
        results.append(_norm_result(title, url,
                                    clean_snippets[index] if index < len(clean_snippets) else ""))
        if len(results) >= num:
            break
    if not results:
        return [], "DDG: пустая выдача"
    return results, None


def _unwrap_ddg_url(raw_url):
    """DuckDuckGo отдаёт /l/?uddg=<encoded> — достаём настоящий адрес."""
    raw_url = raw_url.strip()
    if "uddg=" in raw_url:
        from urllib.parse import parse_qs, unquote
        query = urlparse(raw_url).query
        target = parse_qs(query).get("uddg", [""])[0]
        return unquote(target) if target else raw_url
    if raw_url.startswith("//"):
        return "https:" + raw_url
    return raw_url


def wiki_api_bases():
    """Базы MediaWiki API. Через WIKI_API_BASES можно указать свои (зеркало, тест)."""
    configured = os.getenv("WIKI_API_BASES", "").strip()
    if configured:
        return [item.strip() for item in configured.split(",") if item.strip()]
    return ["https://ru.wikipedia.org/w/api.php", "https://en.wikipedia.org/w/api.php"]


def _wiki_site_root(api_url: str) -> str:
    """https://ru.wikipedia.org/w/api.php → https://ru.wikipedia.org"""
    parsed = urlparse(api_url)
    path = parsed.path
    # Порядок важен: /w/api.php длиннее /api.php, иначе останется лишний /w
    # и ссылки на статьи поедут на https://ru.wikipedia.org/w/wiki/...
    for suffix in ("/w/api.php", "/w/rest.php", "/api.php", "/rest.php"):
        if path.endswith(suffix):
            path = path[: -len(suffix)]
            break
    return f"{parsed.scheme}://{parsed.netloc}{path}".rstrip("/")


def search_web_wiki(query, num=6):
    """Википедия: работает без ключа и почти всегда доступна."""
    results = []
    for api_url in wiki_api_bases():
        site_root = _wiki_site_root(api_url)
        try:
            resp = requests.get(
                api_url,
                params={"action": "query", "list": "search", "srsearch": query,
                        "srlimit": num, "format": "json", "utf8": 1},
                headers={"User-Agent": "NovaMind/1.0 (research assistant)"},
                timeout=SEARCH_TIMEOUT,
            )
            resp.raise_for_status()
            for item in (resp.json().get("query", {}).get("search") or [])[:num]:
                title = item.get("title")
                if not title:
                    continue
                snippet = re.sub(r"<[^>]+>", "", item.get("snippet") or "").strip()
                results.append(_norm_result(
                    f"{title} — Википедия",
                    f"{site_root}/wiki/{quote_plus(title.replace(' ', '_'))}",
                    snippet,
                ))
        except (requests.RequestException, ValueError):
            continue
        if results:
            break
    if not results:
        return [], "Википедия: ничего не найдено"
    return results, None


def _get_html_with_fallback(url):
    """HTML страницы: сначала напрямую, при блокировке — через Jina Reader."""
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=SEARCH_TIMEOUT,
                            allow_redirects=True)
        if resp.status_code == 200 and len(resp.text or "") > 500:
            return resp.text
    except requests.RequestException:
        pass
    try:
        resp = requests.get("https://r.jina.ai/" + url,
                            headers={"User-Agent": "Mozilla/5.0",
                                     "Accept": "text/plain,*/*"},
                            timeout=READER_TIMEOUT)
        if resp.status_code == 200 and len(resp.text or "") > 400:
            return resp.text
    except requests.RequestException:
        pass
    return None


def search_web_bing(query, num=8):
    """Bing HTML: работает без ключа; при блокировке — через Jina Reader.

    Бесплатный запасной бэкенд: у DDG и публичных SearXNG бывают блокировки
    и пустые выдачи, а Bing стабильно отдаёт органику по русским запросам.
    """
    html = _get_html_with_fallback(
        "https://www.bing.com/search?q=" + quote_plus(query) + "&setlang=ru")
    if not html:
        return [], "Bing недоступен"
    results, seen = [], set()
    pattern = re.compile(
        r'<li class="b_algo".*?<h2><a[^>]+href="([^"]+)"[^>]*>(.*?)</a></h2>',
        re.DOTALL | re.IGNORECASE)
    for match in pattern.finditer(html):
        link, raw_title = match.group(1), match.group(2)
        title = re.sub(r"<[^>]+>", "", raw_title).strip()
        if not title or link in seen or "bing.com" in link:
            continue
        seen.add(link)
        window = html[match.end():match.end() + 600]
        snip = re.search(r"<p[^>]*>(.*?)</p>", window, re.DOTALL | re.IGNORECASE)
        snippet = re.sub(r"<[^>]+>", "", snip.group(1)).strip() if snip else ""
        results.append(_norm_result(title, link, snippet))
        if len(results) >= num:
            break
    if not results:
        return [], "Bing: пустая выдача"
    return results, None


def search_web_mojeek(query, num=8):
    """Mojeek: независимый индексатор, без ключа, лоялен к роботам."""
    html = _get_html_with_fallback(
        "https://www.mojeek.com/search?q=" + quote_plus(query))
    if not html:
        return [], "Mojeek недоступен"
    results, seen = [], set()
    patterns = [
        re.compile(r'<a[^>]+class="title"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
                   re.DOTALL | re.IGNORECASE),
        re.compile(r'<h2[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
                   re.DOTALL | re.IGNORECASE),
    ]
    for pattern in patterns:
        for match in pattern.finditer(html):
            link, raw_title = match.group(1), match.group(2)
            title = re.sub(r"<[^>]+>", "", raw_title).strip()
            if not title or link in seen or "mojeek.com" in link:
                continue
            seen.add(link)
            results.append(_norm_result(title, link, ""))
            if len(results) >= num:
                break
        if results:
            break
    if not results:
        return [], "Mojeek: пустая выдача"
    return results, None


def _run_backend(name, query, num=8):
    """Один бэкенд в потоке: (результаты | None, запись trace)."""
    handler = BACKENDS.get(name)
    if handler is None:
        return None, {"backend": name, "ok": False, "count": 0, "ms": 0,
                      "error": "бэкенд не зарегистрирован"}
    started = time.time()
    try:
        results, error = handler(query, num)
    except Exception as exc:
        results, error = [], exc.__class__.__name__
    entry = {"backend": name, "ok": bool(results), "count": len(results),
             "ms": int((time.time() - started) * 1000),
             "error": None if results else (error or "пусто")}
    return (results if results else None), entry


def _parallel_search(query, emit):
    """Все бэкенды параллельно; побеждает первый непустой результат.

    Раньше бэкенды опрашивались по очереди: три мёртвых бэкенда по 9 секунд
    таймаута — и пользователь полминуты смотрел на «Ищу…». Теперь всё
    запускается одновременно, общее время равно самому быстрому бэкенду.
    """
    names = [name for name in search_backends() if name in BACKENDS]
    results, trace = [], []
    if not names:
        return results, trace
    yield emit(type="step", icon="🌐",
               text=f"Запускаю {len(names)} поисковых систем параллельно…")
    executor = ThreadPoolExecutor(max_workers=min(6, len(names)))
    futures = {}
    try:
        for name in names:
            futures[executor.submit(_run_backend, name, query, num=8)] = name
        for future in concurrent.futures.as_completed(futures):
            found, entry = future.result()
            trace.append(entry)
            if found:
                results = found
                yield emit(type="step", icon="✅",
                           text=f"{entry['backend']}: найдено {entry['count']} "
                                f"результатов за {entry['ms']} мс")
                break
            yield emit(type="step", icon="⚠️",
                       text=f"{entry['backend']}: {entry['error']}")
    finally:
        for future in futures:
            future.cancel()
        executor.shutdown(wait=False)
    return results, trace


BACKENDS = {
    "apilayer": search_web_apilayer,
    "serper": search_web_serper,
    "searxng": search_web_searxng,
    "bing": search_web_bing,
    "mojeek": search_web_mojeek,
    "ddg": search_web_ddg,
    "ddg_lite": search_web_ddg_lite,
    "wiki": search_web_wiki,
}


def search_web(query, num=8, backends=None, on_progress=None):
    """
    Перебирает бэкенды до первой успешной выдачи.
    Возвращает (results, trace) — trace нужен для UI и диагностики.
    """
    trace = []
    for name in (backends or search_backends()):
        handler = BACKENDS.get(name)
        if handler is None:
            continue
        started = time.time()
        try:
            results, error = handler(query, num)
        except requests.RequestException as exc:
            results, error = [], f"{exc.__class__.__name__}: {exc}"
        except Exception as exc:  # бэкенд не должен ронять весь поиск
            results, error = [], f"{exc.__class__.__name__}: {exc}"
        entry = {
            "backend": name,
            "ok": bool(results),
            "count": len(results),
            "ms": int((time.time() - started) * 1000),
            "error": None if results else (error or "пусто"),
        }
        trace.append(entry)
        if on_progress:
            try:
                on_progress(entry)
            except Exception:
                pass
        if results:
            return results, trace
    return [], trace


# ══════════════════════════════════════════════════════════════════
# ЧТЕНИЕ СТРАНИЦ
# ══════════════════════════════════════════════════════════════════

def _html_to_text(html):
    html = re.sub(r"<(script|style|svg|noscript|head)[^>]*>.*?</\1>", " ", html,
                  flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r"<br\s*/?>", "\n", html, flags=re.IGNORECASE)
    html = re.sub(r"</(p|div|li|h[1-6]|tr)>", "\n", html, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", html)
    text = (text.replace("&nbsp;", " ").replace("&amp;", "&")
                .replace("&lt;", "<").replace("&gt;", ">")
                .replace("&quot;", '"').replace("&#39;", "'"))
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def fetch_page_text(url, max_chars=6000):
    """Читает страницу: сначала Jina Reader (обходит защиту), потом напрямую."""
    if not url or not url.lower().startswith(("http://", "https://")):
        return None, "некорректный URL"

    try:
        resp = requests.get(
            f"https://r.jina.ai/{url}",
            headers={"User-Agent": "Mozilla/5.0", "Accept": "text/plain,*/*"},
            timeout=READER_TIMEOUT,
        )
        if resp.status_code == 200 and len(resp.text) > 200:
            text = re.sub(r"!\[.*?\]\(.*?\)", "", resp.text)
            text = re.sub(r"\n{3,}", "\n\n", text).strip()
            if text:
                return text[:max_chars], None
    except requests.RequestException:
        pass

    try:
        resp = requests.get(url, headers=_HEADERS, timeout=READER_TIMEOUT, allow_redirects=True)
        resp.raise_for_status()
        content_type = resp.headers.get("Content-Type", "")
        if "html" not in content_type and "text" not in content_type:
            return None, "не текстовая страница"
        text = _html_to_text(resp.text)
        if not text:
            return None, "пустая страница"
        return text[:max_chars], None
    except requests.RequestException as exc:
        return None, f"{exc.__class__.__name__}"


# ══════════════════════════════════════════════════════════════════
# ПРОМПТЫ
# ══════════════════════════════════════════════════════════════════

SEARCH_SYSTEM_PROMPT = """Ты — умный поисковый ассистент NovaMind. Отвечай ПОДРОБНО и КОНКРЕТНО на русском языке.

ПРАВИЛА:
1. Опирайся на данные из интернета, которые идут ниже вопроса.
2. Давай развёрнутые ответы с конкретными фактами, цифрами и датами.
3. Если вопрос о ценах — укажи диапазон цен, год, состояние.
4. Структурируй ответ: заголовки (##), списки (-), таблицы.
5. Цитируй источники как [1], [2] — номера соответствуют списку источников под ответом.
6. Если данных недостаточно — скажи, что известно, а что нет.
7. НЕ пиши «на основе предоставленных данных невозможно ответить» — давай максимум информации.
8. Если поиск не дал результатов — отвечай из своих знаний с пометкой (из базы знаний)."""

DIRECT_SYSTEM_PROMPT = ("Отвечай подробно, структурированно и на русском языке. "
                        "Давай конкретные факты, примеры и пошаговые объяснения.")


# ══════════════════════════════════════════════════════════════════
# ПОТОКОВЫЙ ПОИСК (NDJSON)
# ══════════════════════════════════════════════════════════════════

def _ndjson(payload):
    return json.dumps(payload, ensure_ascii=False) + "\n"


def _pump(source, emit):
    """Прокидывает события вложенного генератора через emit (с elapsed_ms)
    и возвращает итоговое значение вложенного генератора."""
    while True:
        try:
            event = next(source)
        except StopIteration as stop:
            return stop.value
        yield emit(**event)


def needs_web_search(user_message):
    """Быстрая эвристика + мнение модели: нужен ли интернет для вопроса."""
    text = (user_message or "").lower()
    strong = ("цена", "сколько стоит", "новости", "погода", "курс", "сегодня", "сейчас",
              "последни", "актуальн", "прогноз", "результат матча", "вышла", "релиз",
              "кто выиграл", "date", "price", "news", "weather", "today", "latest")
    if any(word in text for word in strong):
        return True
    provider, model, _ = resolve_target()
    answer, error = chat_completion(
        [{"role": "user", "content":
            f'"{user_message}"\nНужен ли поиск в интернете для ответа? Ответь одним словом: SEARCH или DIRECT.\n'
            'SEARCH — цены, новости, погода, события, текущий статус, свежие данные.\n'
            'DIRECT — теория, математика, код, перевод, объяснение.'}],
        provider=provider, model=model, temperature=0, max_tokens=8, timeout=QUERY_TIMEOUT,
    )
    if error:
        return True  # не уверены — лучше поискать
    verdict = (answer or "").upper()
    return "SEARCH" in verdict or "DIRECT" not in verdict


def build_search_query(user_message):
    """Короткий поисковый запрос из вопроса пользователя."""
    provider, model, _ = resolve_target()
    answer, error = chat_completion(
        [{"role": "user", "content":
            f'Сделай из вопроса короткий поисковый запрос (до 8 слов, без кавычек и пояснений).\n'
            f'Пиши запрос на том же языке, что и вопрос. Не переводи на английский.\n'
            f'Вопрос: "{user_message}"'}],
        provider=provider, model=model, temperature=0, max_tokens=40, timeout=QUERY_TIMEOUT,
    )
    if error:
        return user_message
    candidate = (answer or "").strip().strip('"').splitlines()[0] if answer else ""
    if candidate and len(candidate) < 120:
        return candidate
    return user_message


def search_pipeline(user_message, force_search=True, reasoning=False, max_pages=None):
    """
    Генератор событий поиска + ответа. Все события — словари для NDJSON.

    Сценарий (как в интерфейсе):
      stage(search) → шаги → sources → stage(write) → токены ответа → result → done
    """
    started = time.time()
    max_pages = MAX_PAGES if max_pages is None else max_pages
    provider, model, _ = resolve_target()

    def emit(**payload):
        payload.setdefault("elapsed_ms", int((time.time() - started) * 1000))
        return payload

    yield emit(type="stage", scene="search", title="Поищу в интернете",
               text="Подключаюсь к поисковым системам…")
    yield emit(type="step", icon="🧭", text="Анализирую запрос")

    if not force_search:
        yield emit(type="step", icon="🤔", text="Проверяю, нужен ли интернет…")
        if not needs_web_search(user_message):
            yield emit(type="stage", scene="write", title="Отвечаю без поиска",
                       text="Свежие данные не нужны — отвечаю по знаниям модели")
            answer, note = yield from _pump(
                _answer_with_context(user_message, "", [], provider, model, reasoning,
                                     system=DIRECT_SYSTEM_PROMPT,
                                     note="Интернет не нужен, отвечаю по базе знаний модели."),
                emit,
            )
            if note:
                yield emit(type="step", icon="⚠️", text=note)
            yield emit(type="result", reply=answer, searched=False, sources=[], query=None,
                       model=model, offline=provider == "local_demo")
            yield emit(type="done", searched=False)
            return

    query = build_search_query(user_message)
    yield emit(type="step", icon="🔎", text=f"Поисковый запрос: {query[:90]}")

    results, trace = yield from _parallel_search(query, emit)
    if not results:
        # Вторая попытка: исходная формулировка пользователя. Короткий запрос
        # от модели иногда хуже сырого вопроса — слишком «сжатый».
        yield emit(type="step", icon="🔁",
                   text="Первая волна пуста — пробую исходную формулировку…")
        second, trace2 = yield from _parallel_search(user_message, emit)
        trace.extend(trace2)
        if second:
            results = second

    sources = [{"title": r["title"], "url": r["url"], "snippet": r.get("snippet", ""),
                "host": r.get("host", "")} for r in results if r.get("url")]

    if not results:
        yield emit(type="stage", scene="write", title="Поиск не дал результатов",
                   text="Отвечаю из базы знаний модели")
        answer, note = yield from _pump(
            _answer_with_context(user_message, "", [], provider, model, reasoning,
                                 note="Поиск в интернете не дал результатов. Отвечай из своих "
                                      "знаний с пометкой (из базы знаний) и предупреди, что "
                                      "данные могут быть устаревшими."),
            emit,
        )
        if note:
            yield emit(type="step", icon="⚠️", text=note)
        yield emit(type="result", reply=answer, searched=False, sources=[], query=query,
                   model=model, offline=provider == "local_demo", trace=trace)
        yield emit(type="done", searched=False)
        return

    yield emit(type="sources", sources=sources)

    # Читаем страницы параллельно — заметно быстрее последовательного обхода.
    yield emit(type="step", icon="📄", text=f"Читаю {min(max_pages, len(sources))} страницы…")
    scraped = _read_pages(sources[:max_pages])
    for item in scraped:
        icon = "✅" if item["chars"] > 200 else "📝"
        label = f"{item['title'][:60]} — {item['chars']} симв."
        yield emit(type="step", icon=icon, text=label)

    context = "\n\n".join(
        f"### [{i + 1}] {item['title']}\nURL: {item['url']}\n{item['text']}"
        for i, item in enumerate(scraped) if item["text"]
    )
    if not context:
        context = "\n\n".join(
            f"### [{i + 1}] {s['title']}\nURL: {s['url']}\n{s.get('snippet', '')}"
            for i, s in enumerate(sources)
        )

    yield emit(type="stage", scene="write", title="Собираю ответ",
               text=f"Обрабатываю {len(scraped)} источника")

    answer, note = yield from _pump(
        _answer_with_context(user_message, context, sources, provider, model, reasoning),
        emit,
    )
    if note:
        yield emit(type="step", icon="⚠️", text=f"Ответ собран запасным путём: {note}")

    yield emit(type="result", reply=answer, searched=True, sources=sources, query=query,
               model=model, offline=provider == "local_demo", trace=trace)
    yield emit(type="done", searched=True)


def _read_pages(sources):
    """Параллельное чтение страниц с сохранением порядка."""
    if not sources:
        return []
    results = [None] * len(sources)

    def worker(index, source):
        text, _error = fetch_page_text(source.get("url", ""))
        body = text if text and len(text) > 100 else (source.get("snippet") or "")
        results[index] = {
            "title": source.get("title", ""),
            "url": source.get("url", ""),
            "text": body[:3000],
            "chars": len(body or ""),
        }

    threads = [threading.Thread(target=worker, args=(i, s), daemon=True)
               for i, s in enumerate(sources)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=READER_TIMEOUT + 4)
    return [item for item in results if item]


def _stream_answer(messages, system, provider, model, reasoning=False, timeout=None):
    """Потоковый ответ модели → события reasoning/token/error.

    Генератор возвращает кортеж (ошибка, текст): вызывающий код видит, что
    именно удалось получить, и решает — повторить запрос или собрать ответ
    из найденных источников.
    """
    timeout = timeout or ANSWER_TIMEOUT
    pieces = []
    failure = None
    try:
        for kind, chunk in chat_stream(messages, provider=provider, model=model,
                                       system=system, temperature=0.3,
                                       reasoning=reasoning, timeout=timeout):
            if kind == "reasoning":
                yield {"type": "reasoning", "token": chunk}
            elif kind == "token":
                pieces.append(chunk)
                yield {"type": "token", "token": chunk}
            elif kind == "error":
                failure = chunk
            elif kind == "done":
                break
    except Exception as exc:            # сеть или провайдер упали посреди потока
        failure = f"{exc.__class__.__name__}: {exc}"
    return failure, "".join(pieces).strip()


def _fallback_answer(user_message, sources, reason=""):
    """Ответ без модели: найденные ссылки и выдержки — чтобы данные не потерялись."""
    if not sources:
        return ("**Модель не ответила.** "
                + (f"Причина: {reason}. " if reason else "")
                + "Попробуйте спросить ещё раз или сформулировать вопрос иначе.")

    lines = [
        "**Модель не ответила по этим источникам** — ниже то, что нашёл поиск.",
        "",
        f"Запрос: _{user_message}_",
        "",
    ]
    if reason:
        lines.append(f"_Причина: {reason}_")
        lines.append("")
    for index, source in enumerate(sources[:6], 1):
        title = (source.get("title") or "Без названия").strip()
        url = source.get("url") or ""
        host = source.get("host") or (urlparse(url).netloc if url else "")
        snippet = " ".join((source.get("snippet") or "").split())[:320]
        lines.append(f"**[{index}] {title}**")
        lines.append(f"{host} — {url}")
        if snippet:
            lines.append(snippet)
        lines.append("")
    if len(sources) > 6:
        lines.append(f"_Ещё {len(sources) - 6} источников — в списке под ответом._")
    return "\n".join(lines).strip()


def _answer_with_context(user_message, context, sources, provider, model,
                         reasoning=False, system=SEARCH_SYSTEM_PROMPT, note=""):
    """Ответ модели по найденным данным — с тремя ступенями страховки.

    1. потоковый ответ;
    2. тот же запрос без стрима (если поток оборвался);
    3. текст из самих источников (если модель недоступна).

    Так найденные ссылки всегда доезжают до пользователя, даже когда
    генератор молчит или рвётся на середине.
    """
    body = f"Вопрос: {user_message}\n\nДанные из интернета:\n{context}"
    if note:
        body += f"\n\n[Примечание: {note}]"

    failure, text = yield from _stream_answer(
        [{"role": "user", "content": body}],
        system=system, provider=provider, model=model, reasoning=reasoning,
    )
    if text:
        return text, None

    reason = failure or "модель не вернула текст"
    yield {"type": "step", "icon": "🔁", "text": f"Поток прерван ({reason}) — повторяю запрос"}
    answer, error = chat_completion(
        [{"role": "user", "content": body}],
        provider=provider, model=model, system=system, temperature=0.3,
        timeout=ANSWER_TIMEOUT,
    )
    if answer and answer.strip():
        return answer.strip(), reason
    if isinstance(error, dict):
        error = ""
    return (_fallback_answer(user_message, sources,
                             f"{reason}; {error or 'повтор не удался'}".strip("; ")),
            reason)


@search_bp.route("/api/auto_search_stream", methods=["POST"])
def auto_search_stream():
    """Живой поток поиска: сцена → шаги → источники → печатающийся ответ."""
    data = request.get_json(silent=True) or {}
    user_message = (data.get("message") or "").strip()
    if not user_message:
        return jsonify({"error": "Пустое сообщение"}), 400

    force_search = bool(data.get("force", True))
