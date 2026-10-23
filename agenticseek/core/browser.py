"""Browser adapters for the web agent.

Adapted from AgenticSeek ``sources/browser.py`` (GPL-3.0, see NOTICE.md).

Upstream drives a real Chrome through Selenium 4 + ``undetected_chromedriver``
and takes screenshots the CLI can open. A web server has no display and often
runs on a phone, so this module offers two adapters behind the same small
interface the browser agent uses:

``SeleniumBrowser``
    Real Chrome (headless). Needs ``selenium`` plus a Chrome/Chromium binary and
    a matching driver — class B on a server, class C on most Termux setups.
    The stealth/driver-autoinstall extras of upstream are not vendored: they
    download executables at runtime, which is exactly what a sandboxed server
    must not do.

``HTTPBrowser``
    One page at a time over ``httpx`` + ``BeautifulSoup`` (through NovaMind's
    existing page reader). It can navigate and read, extract links and plain
    text, and it reports honestly that form interaction and screenshots are not
    available. This is the default when Selenium is missing, which keeps the web
    agent useful on Termux instead of failing outright.

Neither adapter solves CAPTCHAs, bypasses logins or hides its identity: pages
that require authentication are reported as such.
"""

from __future__ import annotations

import logging
import re
from typing import Callable, Optional
from urllib.parse import urljoin, urlparse

from agenticseek.core.errors import AgenticSeekError
from agenticseek.tools.web_tools import is_public_url

log = logging.getLogger("agenticseek.browser")

MAX_TEXT_CHARS = 6000
MAX_LINKS = 30
PAGE_LOAD_TIMEOUT = 25


class BrowserError(AgenticSeekError):
    """Navigation or page interaction failed."""

    def __init__(self, message: str, user_message: str = ""):
        super().__init__(message, user_message or "Не удалось открыть страницу.",
                         fixes=["Проверьте адрес или попробуйте другой источник."])


class BaseBrowser:
    """Interface used by :class:`agenticseek.core.browser_agent.BrowserAgent`."""

    kind = "base"
    supports_forms = False
    supports_screenshot = False

    def __init__(self):
        self.current_url = ""

    # Navigation
    def go_to(self, url: str) -> str:
        raise NotImplementedError

    def get_text(self) -> str:
        raise NotImplementedError

    def get_navigable(self) -> list[str]:
        return []

    def get_form_inputs(self) -> list[dict]:
        return []

    def fill_form(self, selector: str, value: str) -> bool:
        raise BrowserError("Form interaction is not supported by this browser",
                           "Этот браузер не умеет заполнять формы.")

    def screenshot(self) -> Optional[str]:
        return None

    def close(self) -> None:
        return None


class HTTPBrowser(BaseBrowser):
    """Read-only browser built on the NovaMind page reader.

    Args:
        read_page: callable ``(url, max_chars) -> (text, error)``, normally
            ``routes.search.fetch_page_text``.
    """

    kind = "http"

    def __init__(self, read_page: Callable[..., tuple]):
        super().__init__()
        self.read_page = read_page
        self.text = ""
        self.links: list[str] = []

    def go_to(self, url: str) -> str:
        url = str(url or "").strip()
        if not is_public_url(url):
            raise BrowserError(f"Refusing to open {url!r}",
                               "Этот адрес открывать нельзя (локальная сеть или неверная схема).")
        try:
            text, error = self.read_page(url, MAX_TEXT_CHARS)
        except Exception as failure:  # noqa: BLE001 — reported to the model
            raise BrowserError(f"Page {url} failed: {type(failure).__name__}: {failure}") from failure
        if not text:
            raise BrowserError(f"Page {url} could not be read: {error or 'empty page'}")
        self.current_url = url
        self.text = str(text)
        self.links = self._links_from_text(self.text)
        return self.text

    def get_text(self) -> str:
        return self.text

    def get_navigable(self) -> list[str]:
        return list(self.links)

    def _links_from_text(self, text: str) -> list[str]:
        links, seen = [], set()
        for match in re.findall(r"https?://[^\s\)\]\"'<>]+", text or ""):
            link = match.rstrip(".,;:")
            if link not in seen and is_public_url(link):
                seen.add(link)
                links.append(link)
            if len(links) >= MAX_LINKS:
                break
        return links

    def get_form_inputs(self) -> list[dict]:
        return []

    def fill_form(self, selector: str, value: str) -> bool:
        raise BrowserError("HTTP mode cannot fill forms",
                           "Формы недоступны без Selenium: страница только читается.")

    def close(self) -> None:
        self.text = ""
        self.links = []


class SeleniumBrowser(BaseBrowser):
    """Headless Chrome through Selenium 4.

    Raises:
        BrowserError: when ``selenium`` or a Chrome driver is not available.
            The message says what to install, so the user can decide.
    """

    kind = "selenium"
    supports_forms = True
    supports_screenshot = True

    def __init__(self, driver=None, headless: bool = True):
        super().__init__()
        self.driver = driver or self._create_driver(headless)
        self.text = ""
        self.links: list[str] = []

    @staticmethod
    def _create_driver(headless: bool = True):
        try:
            from selenium import webdriver
            from selenium.webdriver.chrome.options import Options
        except ImportError as error:
            raise BrowserError(
                "selenium is not installed",
                "Полноценный браузер недоступен: не установлен selenium.",
                causes=["pip install selenium"],
                fixes=["Установите selenium и Chrome/Chromium, либо оставьте HTTP-режим "
                       "(AGENTICSEEK_BROWSER=http)."]) from error
        options = Options()
        if headless:
            options.add_argument("--headless=new")
        for argument in ("--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
                         "--disable-notifications", "--disable-popup-blocking",
                         "--window-size=1280,900"):
            options.add_argument(argument)
        options.add_argument("--user-agent=NovaMind/1.0 (agent; +https://github.com/podshoevbunyod16-sketch/NovaMind)")
        try:
            return webdriver.Chrome(options=options)
        except Exception as error:  # noqa: BLE001 — driver/binary problems
            raise BrowserError(
                f"Chrome could not start: {type(error).__name__}: {error}",
                "Chrome не запустился. Проверьте chromedriver или включите HTTP-режим.",
                causes=["Chrome/Chromium не установлен", "версия chromedriver не совпадает"],
                fixes=["Установите Chrome и chromedriver", "AGENTICSEEK_BROWSER=http"]) from error

    def go_to(self, url: str) -> str:
        url = str(url or "").strip()
        if not is_public_url(url):
            raise BrowserError(f"Refusing to open {url!r}",
                               "Этот адрес открывать нельзя (локальная сеть или неверная схема).")
        from selenium.common.exceptions import WebDriverException
        try:
            self.driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT)
            self.driver.get(url)
        except WebDriverException as error:
            raise BrowserError(f"Navigation to {url} failed: {error}") from error
        self.current_url = self.driver.current_url or url
        self.text = self._read_text()
        self.links = self._read_links()
        return self.text

    def _read_text(self) -> str:
        from selenium.common.exceptions import WebDriverException
        try:
            body = self.driver.find_element("tag name", "body")
            text = body.text or ""
        except WebDriverException:
            return ""
        try:
            source = self.driver.page_source or ""
        except WebDriverException:
            source = ""
        if "captcha" in source.lower():
            text += "\n\n(на странице есть CAPTCHA — агент её не обходит)"
        return text[:MAX_TEXT_CHARS]

    def _read_links(self) -> list[str]:
        from selenium.common.exceptions import WebDriverException
        links, seen = [], set()
        try:
            elements = self.driver.find_elements("tag name", "a")
        except WebDriverException:
            return []
        for element in elements:
            try:
                href = element.get_attribute("href") or ""
            except WebDriverException:
                continue
            if not href.startswith(("http://", "https://")):
                continue
            absolute = urljoin(self.current_url, href)
            if absolute in seen or not is_public_url(absolute):
                continue
            seen.add(absolute)
            label = (element.text or "").strip()
            links.append(f"{absolute} — {label[:80]}" if label else absolute)
            if len(links) >= MAX_LINKS:
                break
        return links

    def get_text(self) -> str:
        return self.text

    def get_navigable(self) -> list[str]:
        return list(self.links)

    def get_form_inputs(self) -> list[dict]:
        from selenium.common.exceptions import WebDriverException
        inputs = []
        try:
            for element in self.driver.find_elements(
                    "css selector", "input, textarea, select, button"):
                inputs.append({
                    "tag": element.tag_name,
                    "type": element.get_attribute("type") or "",
                    "name": element.get_attribute("name") or element.get_attribute("id") or "",
                    "placeholder": element.get_attribute("placeholder") or "",
                    "label": (element.get_attribute("aria-label") or element.text or "")[:60],
                })
        except WebDriverException:
            return []
        return inputs[:30]

    def fill_form(self, selector: str, value: str) -> bool:
        from selenium.common.exceptions import WebDriverException
        try:
            element = self.driver.find_element("css selector", selector)
            element.clear()
            element.send_keys(value)
            return True
        except WebDriverException as error:
            raise BrowserError(f"Cannot fill {selector!r}: {error}") from error

    def screenshot(self) -> Optional[str]:
        """Save a screenshot into the runtime directory (no display needed)."""
        import os
        import time

        from agenticseek.core.workspace import runtime_subdir
        try:
            directory = runtime_subdir("screenshots")
            path = os.path.join(directory, f"page_{int(time.time())}.png")
            self.driver.save_screenshot(path)
            return path
        except Exception:  # noqa: BLE001 — a screenshot is a nicety, not a result
            log.debug("screenshot failed", exc_info=True)
            return None

    def close(self) -> None:
        try:
            self.driver.quit()
        except Exception:  # noqa: BLE001
            pass


def selenium_available() -> bool:
    """Whether a real browser can be created right now."""
    try:
        import selenium  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


def create_browser(mode: str = "auto", read_page: Optional[Callable[..., tuple]] = None,
                   headless: bool = True) -> Optional[BaseBrowser]:
    """Build the best available browser.

    Args:
        mode: ``auto`` (Selenium if importable, else HTTP), ``selenium`` (must be
            real), ``http`` (reader only) or ``off`` (no browser at all).
        read_page: page reader used by the HTTP adapter.
        headless: pass ``False`` only for local debugging.
    """
    mode = str(mode or "auto").strip().lower()
    if mode in ("off", "none", "disabled"):
        return None
    if mode == "http":
        return HTTPBrowser(read_page) if read_page else None
    if mode == "selenium":
        return SeleniumBrowser(headless=headless)
    if selenium_available():
        try:
            return SeleniumBrowser(headless=headless)
        except BrowserError as error:
            log.info("falling back to the HTTP browser: %s", error.message)
    return HTTPBrowser(read_page) if read_page else None
