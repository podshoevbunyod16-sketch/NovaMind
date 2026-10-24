"""Модели иногда отвечают XML-тегами вместо блоков ```tag — учим движок понимать оба.

Протокол инструментов NovaMind (AgenticSeek) — огороженный блок::

    ```bash
    pip install flask
    ```

Но модели, обученные на старых AgenticSeek/Open Interpreter диалогах, регулярно
пишут то же самое тегами::

    <bash>pip install flask</bash>
    <bash code to run>pip install flask</bash>
    <bash code_output>Successfully installed ...</bash_output>

Раньше такой ответ не разбирался: команда **не выполнялась**, а теги попадали в
ответ пользователю как есть. В журнале с телефона это выглядело так: агент
«установил» пакет, которого не ставил, и выдал кусок XML вместо результата.

Здесь две функции:

* :func:`normalize_answer` — превращает теги в огороженные блоки, чтобы
  действие действительно выполнилось (и отбрасывает «вывод», который модель
  придумала сама: показывать фальшивый вывод нельзя);
* :func:`sanitize_answer` — тот же результат, но чистый: команда остаётся
  видимым блоком, лишние теги и «вывод» модели убираются.
"""

from __future__ import annotations

import re

#: Тег действия: `<bash>`, `<bash code>`, `<python>`, `<py>`. Варианты с
#: «output»/«result» в атрибутах — это сочинённый вывод, его не выполняем.
_ACTION_TAG = re.compile(
    r"<(bash|python|py)(?![^>]*(?:output|result|stdout|stderr))[^>]*>(.*?)</\1\s*>",
    re.S | re.I)
#: Секции «вывода», которые модель сочинила: <bash code_output>, <output>, <stdout>…
_FAKE_OUTPUT = re.compile(
    r"<(bash[\w ]*output|output|stdout|stderr|result)(?:\s[^>]*)?>.*?</[a-z_ ]*?>",
    re.S | re.I)
#: Осколки тегов после конвертации: `</bash>`, `<bash code to run>` …
_TAG_TOKEN = re.compile(r"</?(?:bash|python|py)[\w ]*>", re.I)


def _fence(tag: str, payload: str) -> str:
    """Огороженный блок; переводы строк внутри сохраняются."""
    body = str(payload or "").strip("\n")
    if not body.strip():
        return ""
    return "\n```" + tag + "\n" + body + "\n```\n"


def normalize_answer(text: str) -> str:
    """Теги `<bash>`/`<python>` → блоки ```bash / ```python (для выполнения)."""
    text = str(text or "")
    if not text:
        return text
    # Сначала выкидываем сочинённый «вывод», потом конвертируем действия.
    text = _FAKE_OUTPUT.sub("", text)
    text = _ACTION_TAG.sub(
        lambda match: _fence("python" if match.group(1).lower() in ("python", "py") else "bash",
                             match.group(2)),
        text)
    return text


def sanitize_answer(text: str) -> str:
    """Ответ без XML-разметки протокола (то, что видит пользователь)."""
    text = str(text or "")
    if not text:
        return text
    text = normalize_answer(text)
    text = _TAG_TOKEN.sub("", text)
    # Пустые строки после удаления секций — косметика.
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def has_legacy_tags(text: str) -> bool:
    """Есть ли в ответе теги протокола — нужно ли вообще нормализовать."""
    body = str(text or "")
    return bool(_ACTION_TAG.search(body) or _FAKE_OUTPUT.search(body)
                or _TAG_TOKEN.search(body))
