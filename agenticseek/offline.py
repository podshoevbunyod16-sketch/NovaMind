"""Offline turn: what the agent can do when there is no language model.

NovaMind ships ``local_llm`` — a tiny deterministic responder used when no
provider key is configured (``provider == "local_demo"``). It can do arithmetic,
units, dates, base64/hash/JSON and a few well-known answers, but it cannot emit
tool blocks, so the AgenticSeek loop cannot run on it.

Upstream AgenticSeek simply has no offline mode. Keeping NovaMind usable without
keys is a product requirement (Termux, first start, no internet), so this module
implements a small deterministic turn:

* explicit generation requests still call the real media path once, and report
  the real error when nothing is configured;
* search-like questions go to the real search backends and cite the results;
* everything else is answered by ``local_llm``, which states honestly that there
  is no model.

This is not a second agent: there is no model to plan with, no tool choice and
no loop, only a fixed order of attempts. The engine calls it instead of routing.
"""

from __future__ import annotations

import logging
import re

log = logging.getLogger("agenticseek.offline")

_SEARCH_HINTS = re.compile(
    r"(найди|поищи|поиск|погугл|в интернете|в сети|что нового|новости|"
    r"погода|курс валют|курс доллара|сколько стоит|search|google)", re.I)


class OfflineTurn:
    """Answers one request without a language model.

    Args:
        goal: user request.
        search: ``search(query, limit) -> (results, meta)`` or None.
        media: callable ``(kind, prompt, params) -> (result, model, errors)``.
        media_kind: detected media kind (image/audio/video) or "".
        local_model: callable answering plain text (``local_llm.complete``).
        emit: event sink.
        state: :class:`agenticseek.state.RunState`.
    """

    def __init__(self, goal, search=None, media=None, media_kind="", local_model=None,
                 emit=None, state=None, force_search=False, history=None):
        self.goal = str(goal or "")
        self.search = search
        self.media = media
        self.media_kind = media_kind
        self.local_model = local_model
        self.emit = emit or (lambda event: None)
        self.state = state
        self.force_search = bool(force_search)
        #: Предыдущие реплики диалога — без них уточнение («а сколько он
        #: весит?») уходит в офлайн-модель вслепую и ответ получается ни о чём.
        self.history = list(history or [])

    def _messages(self) -> list:
        """История (хвост, с ограничением длины) + текущий запрос."""
        messages = []
        for item in self.history[-6:]:
            if not isinstance(item, dict):
                continue
            content = str(item.get("content") or "").strip()
            if not content:
                continue
            role = item.get("role") or "user"
            messages.append({"role": role, "content": content[:1200]})
        messages.append({"role": "user", "content": self.goal})
        return messages

    def run(self) -> str:
        if self.media_kind:
            answer = self._media_answer()
            if answer:
                return answer
        if self.search is not None and (self.force_search or _SEARCH_HINTS.search(self.goal)):
            return self._search_answer()
        return self._plain_answer()

    # ── steps ─────────────────────────────────────────────────────────────
    def _media_answer(self) -> str:
        if self.media is None:
            return ""
        prompt = _media_prompt(self.goal)
        self.emit({"type": "stage", "scene": "media", "title": "Рисую медиа",
                   "text": f"Генерация {self.media_kind}"})
        try:
            result, model, errors = self.media(self.media_kind, prompt, {})
        except Exception as error:  # noqa: BLE001 — reported to the user
            result, model, errors = None, None, [f"{type(error).__name__}: {error}"]
        if result:
            if self.state is not None:
                self.state.add_media(result, model)
            self.emit({"type": "media", "media": result, "model": model})
            return f"Готово: {self.media_kind} создан и показан выше."
        reason = "; ".join(str(item) for item in (errors or [])[-2:]) or "провайдер не ответил"
        self.emit({"type": "step", "icon": "⚠️",
                   "text": f"Не получилось создать {self.media_kind}: {reason}"})
        return (f"Не получилось создать {self.media_kind}: {reason}.\n\n"
                "Проверьте настройки медиа в /settings или подключите ключ провайдера.")

    def _search_answer(self) -> str:
        query = _search_query(self.goal)
        # Уточнение («а какой у неё размер?») без контекста даёт мусорный
        # запрос — добавляем предыдущую реплику пользователя в поиск.
        for item in reversed(self.history):
            if isinstance(item, dict) and item.get("role") == "user":
                context = str(item.get("content") or "").strip()[:150]
                if context and context.lower() not in query.lower():
                    query = f"{context} {query}"
                break
        self.emit({"type": "stage", "scene": "search", "title": "Поиск в интернете",
                   "text": query})
        try:
            results, meta = self.search(query, 6)
        except Exception as error:  # noqa: BLE001
            results, meta = [], {"backend": None}
            log.warning("offline search failed: %s", type(error).__name__)
        results = list(results or [])
        if not results:
            backend = (meta or {}).get("backend")
            self.emit({"type": "step", "icon": "⚠️",
                       "text": f"Поиск «{query}» ничего не дал"
                               + (f" (бэкенд: {backend})" if backend else "")})
            return self._plain_answer()
        for item in results:
            if self.state is not None and item.get("url"):
                self.state.add_source(item["url"], item.get("title") or item["url"])
        self.emit({"type": "tool", "name": "web_search", "command": query, "code": 0,
                   "output": "\n".join(str(item.get("title") or item.get("url"))
                                       for item in results[:6])})
        lines = [f"Нашёл по запросу «{query}» ({len(results)}):", ""]
        for index, item in enumerate(results[:6], 1):
            title = item.get("title") or item.get("host") or item.get("url")
            lines.append(f"{index}. {title}")
            if item.get("url"):
                lines.append(f"   {item['url']}")
            if item.get("snippet"):
                lines.append(f"   {str(item['snippet'])[:240]}")
        lines.append("")
        lines.append("Это результаты поиска: подключите языковую модель, "
                     "чтобы получить разбор и вывод.")
        return "\n".join(lines)

    def _plain_answer(self) -> str:
        if self.local_model is None:
            return "Офлайн-ответ недоступен."
        try:
            return str(self.local_model(self._messages()) or "").strip()
        except Exception as error:  # noqa: BLE001
            log.warning("local model failed: %s", type(error).__name__)
            return "Офлайн-ответ недоступен: локальная модель вернула ошибку."


_SEARCH_VERB = re.compile(
    r"(?iu)^\s*(?:пожалуйста,?\s*)?(?:найди|поищи|поиск|погугли|search|google)\s+"
    r"(?:в\s+интернете\s+|в\s+сети\s+|мне\s+)?")


def _search_query(goal: str) -> str:
    """«найди asyncio» → «asyncio»: бэкенды не любят глаголы в запросе."""
    text = _SEARCH_VERB.sub("", str(goal or "").strip())
    return (text or str(goal or "")).strip()[:300]


def _media_prompt(goal: str) -> str:
    """Strip the request verb so the generator gets a clean description."""
    text = str(goal or "").strip()
    text = re.sub(r"(?iu)^\s*(сгенерируй|сгенери|создай|сделай|нарисуй|покажи|озвучь|"
                  r"прочитай вслух|спой|generate|create|draw|make)\s+", "", text)
    text = re.sub(r"(?iu)^\s*(картинку|картинка|изображение|фото|видео|ролик|аудио|голос)\s+",
                  "", text)
    return text.strip() or str(goal or "").strip()
