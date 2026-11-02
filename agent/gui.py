"""First-party hints for graphics programs running without a desktop display."""
from __future__ import annotations

import re


_DISPLAY_FAILURES = (
    re.compile(r"No module named ['\"]tkinter['\"]", re.I),
    re.compile(r"no display name and no \$DISPLAY", re.I),
    re.compile(r"No available video device", re.I),
    re.compile(r"couldn't connect to display", re.I),
)


def needs_display(error: str) -> bool:
    """Return true for common Tk/SDL errors that specifically require a screen."""
    text = str(error or "")
    return any(pattern.search(text) for pattern in _DISPLAY_FAILURES)


def gui_hint(error: str) -> str:
    """Explain headless failures and suggest file-based, renderable output."""
    if not needs_display(error):
        return ""
    return ("В рабочей среде нет графического экрана. Не устанавливайте GUI-пакеты ради запуска; "
            "сохраните результат в файл (например, SVG или PNG), который можно открыть отдельно.")
