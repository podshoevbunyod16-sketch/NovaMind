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
    """Path of ``<name>.txt``; ``name`` may include the ``.txt`` suffix.

    С версии 2026-10-28 путь разрешает :mod:`prompt_registry`: он умеет
    закреплять конкретную версию промпта (``PROMPT_VERSION_<NAME>``) и хранит
    архив изменений. Если реестр недоступен, работает прежний путь — движок не
    должен падать из-за версионирования.
    """
    name = str(name or "").strip()
    if not name:
        raise ValueError("Empty prompt name")
    if name.endswith(".txt"):
        name = name[:-4]
    try:
        from prompt_registry import resolve
        return resolve(name)
    except Exception:                                      # noqa: BLE001
        return os.path.join(PROMPT_DIR, f"{name}.txt")


def available() -> list[str]:
    try:
        return sorted(item for item in os.listdir(PROMPT_DIR) if item.endswith(".txt"))
    except OSError:
        return []
