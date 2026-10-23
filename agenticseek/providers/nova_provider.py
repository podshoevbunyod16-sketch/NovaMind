"""Provider adapter: NovaMind's completion layer behind AgenticSeek's interface.

Derived from the shape of AgenticSeek ``sources/llm_provider.py`` (GPL-3.0, see
NOTICE.md), where every provider implements:

.. code-block:: python

    provider.respond(messages) -> str
    provider.get_model_name() -> str

NovaMind already has one completion function for all providers
(``ai_providers.chat_completion``: OpenAI-compatible endpoints, Google AI Studio,
Groq with key rotation, OpenRouter, local models, offline demo). This adapter
forwards to it, so the agent engine supports exactly the providers NovaMind
supports and no key or endpoint is duplicated here.

Notes:

* AgenticSeek's providers use *native* tool calling on some backends; NovaMind's
  agent protocol is text blocks, so a provider without tool support works
  identically. That is why the engine does not need per-provider tool schemas.
* Every failure raises :class:`AgenticSeekError` with the provider's own message:
  the agent must never invent an answer when the model is unreachable.
* The adapter counts calls and tokens. The engine enforces a per-run call cap, so
  a looping model cannot drain a free-tier key.
"""

from __future__ import annotations

import os
from typing import Callable, Optional

from agenticseek.core.errors import AgenticSeekError

DEFAULT_MAX_CALLS = max(1, int(os.getenv("AGENTICSEEK_MAX_LLM_CALLS", "30")))


class NovaProvider:
    """AgenticSeek-compatible provider over a NovaMind completion callable.

    Args:
        complete: ``(messages, system=..., temperature=..., max_tokens=...,
            timeout=...) -> (text, meta)``; ``text`` is None on failure and
            ``meta`` then carries the reason.
        provider: provider id (``groq``, ``google_ai_studio``, …) for logs.
        model: model id.
        max_calls: hard cap of model calls in one run.
    """

    def __init__(self, complete: Callable[..., tuple], provider: str = "", model: str = "",
                 temperature: float = 0.3, max_tokens: int = 1500, timeout: float = 90,
                 max_calls: Optional[int] = None):
        self.complete = complete
        self.provider = provider
        self.model = model
        self.temperature = float(temperature)
        self.max_tokens = int(max_tokens)
        self.timeout = float(timeout)
        self.max_calls = int(max_calls or DEFAULT_MAX_CALLS)
        self.calls = 0
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        self.last_meta: dict = {}

    # ── AgenticSeek interface ─────────────────────────────────────────────
    def respond(self, messages: list) -> str:
        """One completion over ``messages`` (a system message is allowed)."""
        if self.calls >= self.max_calls:
            raise AgenticSeekError(
                f"Model call limit reached ({self.max_calls})",
                "Закончился лимит обращений к модели за один запуск.")
        system, dialogue = _split_system(messages)
        if not dialogue:
            raise AgenticSeekError("Empty conversation", "Нечего отправлять модели.")
        self.calls += 1
        try:
            text, meta = self.complete(
                dialogue, system=system, temperature=self.temperature,
                max_tokens=self.max_tokens, timeout=self.timeout)
        except Exception as error:  # noqa: BLE001 — provider layer is external
            raise AgenticSeekError(
                f"{type(error).__name__}: {error}",
                "Ошибка при обращении к модели.") from error
        self.last_meta = meta if isinstance(meta, dict) else {}
        self._add_usage(self.last_meta)
        if text is None:
            reason = meta if isinstance(meta, str) else (self.last_meta or {}).get("error")
            raise AgenticSeekError(
                f"Provider {self.provider or '?'} returned no answer: {reason or 'unknown'}",
                "Модель не ответила. Проверьте ключ, лимиты и выбранную модель.",
                causes=[str(reason or "провайдер вернул пустой ответ")])
        return str(text)

    def get_model_name(self) -> str:
        return str(self.model or self.provider or "novamind")

    # ── accounting ────────────────────────────────────────────────────────
    def _add_usage(self, meta: dict) -> None:
        usage = meta.get("usage") if isinstance(meta, dict) else None
        if not isinstance(usage, dict):
            return
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            try:
                self.usage[key] += int(usage.get(key) or 0)
            except (TypeError, ValueError):
                pass

    def stats(self) -> dict:
        return {"provider": self.provider, "model": self.model,
                "calls": self.calls, "usage": dict(self.usage)}


def _split_system(messages) -> tuple[str, list]:
    """Separate the leading system message from the dialogue."""
    system, dialogue = "", []
    for index, item in enumerate(messages or []):
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        content = item.get("content")
        if content is None:
            continue
        if role == "system" and index == 0:
            system = str(content)
            continue
        if role in ("user", "assistant", "system"):
            dialogue.append({"role": role, "content": str(content)})
    return system, dialogue
