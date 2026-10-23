"""Resolve the vendored prompt files.

Upstream keeps prompts in ``prompts/base/<agent>.txt`` and builds the path from
the project root. NovaMind ships them inside the package, so the path is derived
from ``__file__`` and cannot depend on the current working directory (Flask may
be started from anywhere).
"""

from __future__ import annotations

import os

PROMPT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "prompts", "base")


def prompt_path(name: str) -> str:
    """Path of ``<name>.txt``; ``name`` may include the ``.txt`` suffix."""
    name = str(name or "").strip()
    if not name:
        raise ValueError("Empty prompt name")
    if not name.endswith(".txt"):
        name = f"{name}.txt"
    return os.path.join(PROMPT_DIR, name)


def available() -> list[str]:
    try:
        return sorted(item for item in os.listdir(PROMPT_DIR) if item.endswith(".txt"))
    except OSError:
        return []
