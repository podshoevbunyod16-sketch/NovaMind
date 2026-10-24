"""Web search and page reading (block tags ``web_search`` and ``web_open``).

AgenticSeek's web agent uses ``searxSearch`` (a SearXNG scrape, now requiring an
API key) and a Selenium browser. NovaMind already has several search backends
(DuckDuckGo, Tavily, Exa, SearXNG, Groq …) in ``routes/search.py`` and a page
reader on top of ``httpx`` + ``BeautifulSoup``. Re-implementing them here would
duplicate logic and split configuration, so these tools call the NovaMind
implementation through injected callables:

* ``search(query, limit) -> (results, meta)`` — ``routes.terminal.nova_search``;
* ``read_page(url, max_chars) -> (text, error)`` — ``routes.search.fetch_page_text``.

What stays from AgenticSeek is the block protocol and the "results as numbered
list, then read the page" workflow the browser prompt is written for.

Page content is untrusted input: it goes back to the model inside the
``[success]`` feedback of the tool, and the agent prompt forbids following
instructions found in tool output.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from typing import Callable, Optional
from urllib.parse import urlparse

from agenticseek.core.agent import clip
from agenticseek.tools.tools import Tools

MAX_LINKS = 20
MAX_PAGE_CHARS = 6000


def is_public_url(url: str) -> bool:
    """Reject localhost, private networks and non-http schemes (SSRF guard)."""
    try:
        parsed = urlparse(str(url or "").strip())
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    host = parsed.hostname.strip("[]").lower()
    if host in ("localhost", "localhost.localdomain") or host.endswith(".local"):
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        try:
            address = ipaddress.ip_address(socket.gethostbyname(host))
        except (socket.gaierror, ValueError, OSError):
            return True     # let the reader report the DNS error
    return not (address.is_private or address.is_loopback or address.is_link_local
                or address.is_reserved or address.is_multicast or address.is_unspecified)


class WebSearchTool(Tools):
    tag = "web_search"
    name = "WebSearch"
    description = "Найти информацию в интернете: строка запроса."
    risk = "safe"

    def __init__(self, backend: Optional[Callable[..., tuple]] = None, state=None,
                 limit: int = 6):
        super().__init__()
        self.backend = backend
        self.state = state
        self.limit = limit

    def execute(self, payload: str) -> str:
        query = str(payload or "").strip().splitlines()[0][:300] if str(payload or "").strip() else ""
        if not query:
            return "[failure] Пустой поисковый запрос."
        if query.strip(" \"'«»`").lower() in {"query", "запрос", "search query",
                                              "your query", "..."}:
            return ("[failure] «query» — это шаблон, а не запрос. Напиши реальный "
                    "запрос или ответь пользователю по уже найденным данным.")
        if self.backend is None:
            return "[failure] Поиск недоступен: не настроен ни один бэкенд."
        try:
            results, meta = self.backend(query, self.limit)
        except Exception as error:  # noqa: BLE001 — report, do not crash the run
            return f"[failure] Поиск «{query}» не удался: {type(error).__name__}: {error}"
        results = list(results or [])
        if not results:
            backend = (meta or {}).get("backend") if isinstance(meta, dict) else ""
            hint = f" (бэкенд: {backend})" if backend else ""
            return f"[failure] Поиск «{query}» ничего не нашёл{hint}."
        lines = []
        for index, item in enumerate(results[: self.limit], 1):
            title = str(item.get("title") or "").strip() or "без названия"
            url = str(item.get("url") or "").strip()
            snippet = clip(str(item.get("snippet") or item.get("description") or "").strip(), 300)
            if not url:
                continue
            lines.append(f"{index}. {title}\n   {url}\n   {snippet}".rstrip())
            if self.state is not None:
                self.state.add_source(url, title)
        if not lines:
            return f"[failure] Поиск «{query}» вернул результаты без ссылок."
        return ("[success] Результаты поиска (открой подходящий через ```web_open```):\n"
                + "\n".join(lines))


class WebOpenTool(Tools):
    tag = "web_open"
    name = "WebOpen"
    description = "Прочитать страницу по URL (текст без скриптов)."
    risk = "safe"

    def __init__(self, reader: Optional[Callable[..., tuple]] = None, state=None,
                 max_chars: int = MAX_PAGE_CHARS):
        super().__init__()
        self.reader = reader
        self.state = state
        self.max_chars = max_chars

    def execute(self, payload: str) -> str:
        url = str(payload or "").strip().splitlines()[0].strip() if str(payload or "").strip() else ""
        if not url:
            return "[failure] Нужен URL страницы."
        if not is_public_url(url):
            return f"[failure] Адрес «{url}» не разрешён (локальная сеть или неверная схема)."
        if self.reader is None:
            return "[failure] Чтение страниц недоступно: не настроен зачитыватель."
        try:
            text, error = self.reader(url, self.max_chars)
        except Exception as exc:  # noqa: BLE001
            return f"[failure] Страница «{url}» не открылась: {type(exc).__name__}: {exc}"
        if not text:
            return f"[failure] Страница «{url}» не открылась: {error or 'пустой текст'}"
        if self.state is not None:
            self.state.add_source(url, url)
        links = _extract_links(text)
        suffix = ""
        if links:
            suffix = "\n\nСсылки на странице:\n" + "\n".join(f"- {link}" for link in links[:MAX_LINKS])
        return f"[success] {url}:\n{clip(text, self.max_chars)}{suffix}"


_URL_IN_TEXT = re.compile(r"https?://[^\s\)\]\"'<>]+")


def _extract_links(text: str) -> list[str]:
    """Pull absolute URLs out of already-extracted text (degraded navigation)."""
    seen, links = set(), []
    for match in _URL_IN_TEXT.findall(str(text or "")):
        link = match.rstrip(".,;:")
        if link not in seen:
            seen.add(link)
            links.append(link)
        if len(links) >= MAX_LINKS:
            break
    return links
