"""Console helpers.

Adapted from AgenticSeek ``sources/utility.py`` (GPL-3.0, see NOTICE.md).

The original prints coloured text to a terminal and runs a spinner thread.
NovaMind is a web server (and often runs on Termux), so:

* colours are optional — used only when ``colorama`` happens to be installed
  and stderr is a TTY, otherwise plain text;
* the spinner is gone (:func:`animate_thinking` is a no-op that only records
  the status message), because terminal animation in a request thread is noise;
* :func:`set_output_sink` lets the engine forward agent status lines to the
  NDJSON stream instead of stdout.

The functions keep the original names and signatures so vendored agent code
does not change.
"""

from __future__ import annotations

import logging
import platform
import time

logger = logging.getLogger("agenticseek.utility")

_COLOR_MAP = {
    "success": "green",
    "failure": "red",
    "status": "light_green",
    "code": "light_blue",
    "warning": "yellow",
    "output": "cyan",
    "info": "cyan",
}

#: Optional callback ``sink(text, color)`` used by the engine to stream status.
_output_sink = None


def set_output_sink(sink) -> None:
    """Route :func:`pretty_print` output to ``sink(text, color)`` (or back to None)."""
    global _output_sink
    _output_sink = sink


def get_output_sink():
    return _output_sink


def get_color_map():
    return dict(_COLOR_MAP)


def _colored(text: str, color: str) -> str:
    try:  # termcolor is optional in NovaMind
        from termcolor import colored
    except Exception:  # noqa: BLE001 — cosmetics must never break a run
        return text
    if platform.system().lower() == "windows" and color == "info":
        color = "cyan"
    try:
        return colored(text, color)
    except Exception:  # noqa: BLE001
        return text


def stop_animation():
    """Kept for API compatibility with the original module."""
    return None


def pretty_print(text, color="info", no_newline=False):
    """Report a status line: to the engine sink, the logger and stdout.

    Args:
        text (str): text to report.
        color (str, optional): semantic colour: success/failure/status/code/
            warning/output/info. Defaults to "info".
        no_newline (bool, optional): keep the raw print without a newline.
    """
    text = "" if text is None else str(text)
    if _output_sink is not None:
        try:
            _output_sink(text, color)
        except Exception:  # noqa: BLE001 — a broken sink must not abort the agent
            logger.debug("output sink failed", exc_info=True)
    if color in ("failure", "warning"):
        logger.warning(text)
    elif color == "success":
        logger.info(text)
    else:
        logger.debug(text)
    if _output_sink is not None:
        # A web request already forwards the line to the UI; printing it again
        # would only duplicate it in the server log.
        return
    try:
        print(_colored(text, _COLOR_MAP.get(color, "cyan")),
              end="" if no_newline else "\n", flush=True)
    except (UnicodeEncodeError, OSError):  # console without the glyphs / closed pipe
        pass


def animate_thinking(text, color="status", duration=120):
    """Record a status message (no terminal animation in a server process)."""
    if _output_sink is not None:
        try:
            _output_sink(str(text), color)
        except Exception:  # noqa: BLE001
            pass
    else:
        logger.debug("thinking: %s", text)


def timer_decorator(func):
    """Measure the execution time of a function (kept from the original module)."""
    def wrapper(*args, **kwargs):
        start = time.time()
        result = func(*args, **kwargs)
        logger.info("%s took %.2f seconds", func.__name__, time.time() - start)
        return result
    return wrapper
