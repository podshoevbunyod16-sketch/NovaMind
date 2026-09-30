"""
commands/sites_search.py — плагин прямого поиска по сайтам объявлений Таджикистана.

Зачем: универсальные поисковые бэкенды (DDG, SearXNG и др.) плохо находят
конкретные объявления на somon.tj — запросы вроде «ноутбук 16 ГБ до 3000
сомони» заканчивались ответом «поиск не дал результатов». Этот плагин
добавляет бэкенд "sites": он ходит прямо на сайт объявлений и возвращает
реальные объявления — заголовок, цену и ссылку.

Подключение: config.load_plugins() импортирует все модули из commands/,
поэтому установка происходит при импорте — бэкенд регистрируется в
routes.search.BACKENDS и ставится первым в SEARCH_BACKENDS.

Если запрос не похож на поиск товара или сайт закрыл доступ — бэкенд
мгновенно возвращает пустой результат, остальные бэкенды продолжают работу.
"""
import os
import re
from urllib.parse import quote_plus

import requests

TIMEOUT = float(os.getenv("SITES_SEARCH_TIMEOUT", "12"))

_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Linux; Android 12; Pixel 6) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"),
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
}

# Запросы, похожие на поиск товара или объявления.
_SHOP_HINTS = (
    "сомон", "somon", "olx", "таджик", "душанбе", "худжанд", "кулоб", "бохтар",
    "купить", "куплю", "продаю", "продажа", "б/у", "объявлен", "цена",
    "ноутбук", "компьютер", "телефон", "смартфон", "iphone", "айфон", "samsung",
    "стиральн", "холодильник", "телевизор", "авто", "машин", "квартир", "техник",
    "генератор", "мотоцикл", "велосипед", "мебель", "диван", "матрас", "одежд",
)

# Категории somon.tj со стабильной вёрсткой: если в запросе есть товар,
# читаем страницу категории напрямую.
_CATEGORY_PAGES = (
    (re.compile(r"ноутбук|нетбук", re.I), "https://somon.tj/kompyuteryi-i-orgtehnika/noutbuki/"),
    (re.compile(r"компьютер|моноблок|системный блок", re.I), "https://somon.tj/kompyuteryi-i-orgtehnika/"),
)

_SEARCH_URLS = (
    "https://somon.tj/search/?text={q}",
    "https://somon.tj/search/?query={q}",
    "https://somon.tj/search/?q={q}",
    "https://somon.tj/?q={q}",
)

_AD_RE = re.compile(r'<a[^>]+href="(/adv/[^"#?]+)"[^>]*>(.*?)</a>', re.DOTALL | re.IGNORECASE)
_PRICE_RE = re.compile(r"(\d[\d\u00a0\u202f .,]{0,12})\s*(сомони|сомон|смн|tjs|sm)\b", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")


def _clean(text):
    text = _TAG_RE.sub(" ", text or "")
    return re.sub(r"\s{2,}", " ", text).strip()


def _looks_like_shopping(query):
    lowered = (query or "").lower()
    return any(hint in lowered for hint in _SHOP_HINTS)


def _fetch(url):
    """Прямой запрос; при отказе — Jina Reader, который обходит защиту сайтов."""
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=TIMEOUT, allow_redirects=True)
        if resp.status_code == 200 and len(resp.text or "") > 500:
            return resp.text
    except requests.RequestException:
        pass
    try:
        resp = requests.get("https://r.jina.ai/" + url,
                            headers={"User-Agent": "Mozilla/5.0", "Accept": "text/plain,*/*"},
                            timeout=TIMEOUT)
        if resp.status_code == 200 and len(resp.text or "") > 300:
            return resp.text
    except requests.RequestException:
        pass
    return None


def _parse_ads(html, num=8):
    """Достаёт объявления: ссылки вида /adv/<id>_<slug>, заголовок и цену рядом."""
    results, seen = [], set()
    for match in _AD_RE.finditer(html):
        path, raw_title = match.group(1), match.group(2)
        title = _clean(raw_title)
        if not title or len(title) < 8 or path in seen:
            continue
        seen.add(path)
        window = html[match.end():match.end() + 700]
        price_match = _PRICE_RE.search(window)
        price = _clean(price_match.group(0)) if price_match else ""
        line = (title + " — " + price) if price else title
        results.append({
            "title": line[:220],
            "url": "https://somon.tj" + path,
            "snippet": line[:600],
            "host": "somon.tj",
        })
        if len(results) >= num:
            break
    return results


def search_web_sites(query, num=8):
    """Бэкенд для routes.search.BACKENDS: объявления somon.tj по запросу."""
    query = (query or "").strip()
    if not query or not _looks_like_shopping(query):
        return [], "запрос не похож на поиск объявлений"
    urls = [tpl.format(q=quote_plus(query)) for tpl in _SEARCH_URLS]
    for pattern, category_url in _CATEGORY_PAGES:
        if pattern.search(query):
            urls.insert(0, category_url)
            break
    for url in urls:
        html = _fetch(url)
        if not html:
            continue
        ads = _parse_ads(html, num)
        if ads:
            return ads, None
    return [], "на сайтах объявлений ничего не найдено или доступ закрыт"


def _install():
    try:
        import routes.search as rs
    except Exception as exc:  # приложение запущено без поискового модуля
        print("[sites_search] Пропуск установки: %s" % exc)
        return
    try:
        if "sites" not in rs.BACKENDS:
            rs.BACKENDS["sites"] = search_web_sites
        configured = os.getenv("SEARCH_BACKENDS", "").strip()
        if not configured:
            os.environ["SEARCH_BACKENDS"] = "sites," + ",".join(rs.DEFAULT_BACKENDS)
        elif "sites" not in [name.strip() for name in configured.split(",")]:
            os.environ["SEARCH_BACKENDS"] = "sites," + configured
        print("[sites_search] Бэкенд 'sites' (somon.tj) подключён первым")
    except Exception as exc:
        print("[sites_search] Ошибка установки: %s" % exc)


def run(**_kwargs):  # чтобы плагин был виден в списке плагинов config
    return "sites_search: прямой поиск объявлений на somon.tj"


_install()
