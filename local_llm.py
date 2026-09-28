"""
local_llm.py — Встроенная офлайн-модель NovaMind.

Зачем: приложение должно отвечать всегда. Если в .env нет ни одного ключа
(Groq / Gemini / OpenRouter / Cerebras / Pollinations) и локальный
llama-server не поднят, чат всё равно работает: эта «модель» отвечает
без сети, честно помечая, что она офлайн и не имеет доступа в интернет.

Это НЕ замена большой модели, а аварийный контур + демо-режим для UI.
"""
from __future__ import annotations

import hashlib
import platform
import re
import time
from datetime import datetime

MODEL_ID = "nova-local-1"
MODEL_NAME = "Nova Local 1 (офлайн)"
PROVIDER_ID = "local_demo"
PROVIDER_NAME = "Локальная демо-модель"

DISCLAIMER = (
    "⚠️ Это встроенная **офлайн-модель NovaMind**: она работает без интернета и "
    "без API-ключей, поэтому не знает новостей и не ищет в сети.\n"
    "Чтобы получить полноценные ответы — добавьте ключ в `.env` "
    "(`GROQ_API_KEY`, `GEMINI_API_KEY`, `OPENROUTER_API_KEY`) или поднимите "
    "локальный llama-server и выберите провайдера `openai_compatible` в настройках."
)

_KNOWLEDGE = {
    "привет": "Привет! Я Khirad — ассистент NovaMind. Спросите что-нибудь или включите «Поиск в интернете».",
    "кто ты": "Я Khirad, интерфейс ассистента NovaMind. Сейчас отвечаю встроенной офлайн-моделью.",
    "что ты умеешь": (
        "**Возможности NovaMind:**\n\n"
        "- 💬 чат с ИИ (Groq, Gemini, OpenRouter, Cerebras, Pollinations, локальный llama.cpp)\n"
        "- 🔍 поиск в интернете с источниками под ответом\n"
        "- 🧠 режим рассуждений — видно ход мыслей модели\n"
        "- 🖼 генерация изображений, 🔊 аудио и 🎬 видео\n"
        "- 📎 анализ загруженных файлов и изображений\n"
        "- 🌤 погода, 💱 курсы валют, 📚 Википедия\n"
        "- 💾 история диалогов и команды `/help`"
    ),
    "помощь": "Введите `/help` — покажу список команд. `Ctrl+K` открывает командную панель.",
}

_MATH = re.compile(r"^\s*([-+*/().\d\s]+?)\s*[=?\s]*$")
_MATH_STRIP = re.compile(r"[\s=?]+$")


def _tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _math_answer(expr: str):
    """Безопасный калькулятор: только цифры и арифметика."""
    cleaned = _MATH_STRIP.sub("", (expr or "").strip())
    if not cleaned or not re.search(r"\d", cleaned) or not re.search(r"[-+*/]", cleaned):
        return None
    if not _MATH.match(cleaned):
        return None
    try:
        value = eval(cleaned, {"__builtins__": {}}, {})  # noqa: S307 — выражение ограничено regex'ом
    except Exception:
        return None
    if isinstance(value, (int, float)):
        rounded = round(float(value), 6)
        pretty = int(rounded) if abs(rounded - int(rounded)) < 1e-9 else rounded
        return f"{cleaned} = **{pretty}**"
    return None


def _digest(prompt: str) -> str:
    words = [w for w in re.findall(r"[^\W\d_]{4,}", prompt.lower())][:6]
    topic = ", ".join(words) if words else "запрос"
    fingerprint = hashlib.sha1(prompt.encode("utf-8")).hexdigest()[:8]
    return (
        f"**Что я понял:** {topic}\n\n"
        f"**Как думаю:**\n"
        f"1. Разбираю вопрос на части и проверяю, нужны ли свежие данные.\n"
        f"2. Свежие данные без интернета недоступны — отвечаю по встроенным знаниям.\n"
        f"3. Формирую короткий структурированный ответ и честно помечаю ограничения.\n\n"
        f"*идентификатор запроса: `{fingerprint}`*"
    )


_META_BRACKETS = re.compile(r"\[[^\]]{10,}\]", re.DOTALL)


def clean_prompt(text: str) -> str:
    """Убирает служебные вставки вида «[Поиск не дал результатов. Отвечай…]»,
    чтобы офлайн-модель не показывала их пользователю дословно."""
    cleaned = _META_BRACKETS.sub(" ", text or "")
    cleaned = re.sub(r"^(Вопрос|Запрос)\s*:\s*", "", cleaned.strip())
    cleaned = re.sub(r"(Данные из интернета|Результаты поиска)\s*:\s*.*$", "", cleaned, flags=re.DOTALL)
    return re.sub(r"\s{2,}", " ", cleaned).strip()


def complete(messages, **_kwargs) -> str:
    """Возвращает текст ответа для списка сообщений в OpenAI-формате."""
    last_user = ""
    for message in reversed(messages or []):
        if message.get("role") == "user":
            last_user = str(message.get("content") or "").strip()
            break

    prompt = clean_prompt(last_user) or clean_prompt(" ".join(
        str(m.get("content") or "") for m in (messages or []) if m.get("role") == "user"
    )) or "пустой запрос"
    lowered = prompt.lower()

    math = _math_answer(prompt)
    if math:
        return f"{math}\n\n{DISCLAIMER}"

    for key, answer in _KNOWLEDGE.items():
        if key in lowered:
            return f"{answer}\n\n{DISCLAIMER}"

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    body = (
        f"Вы спросили: «{prompt[:300]}{'…' if len(prompt) > 300 else ''}»\n\n"
        f"Коротко по сути:\n"
        f"- вопрос принят и разобран ({_tokens(prompt)} токенов во входе);\n"
        f"- свежих данных у офлайн-модели нет, поэтому факты с датами и ценами "
        f"проверяйте включённым поиском в интернете;\n"
        f"- сформулируйте задачу конкретнее — дам пошаговый план или код.\n\n"
        f"_Время ответа: {now} · платформа: {platform.system()} {platform.release()}_"
    )
    return f"{body}\n\n{DISCLAIMER}"


def reasoning(prompt: str) -> str:
    """Ход рассуждений для офлайн-модели (показывается в панели «Рассуждение»)."""
    return _digest(prompt)


def stream(messages, **kwargs):
    """Генератор (kind, text): reasoning-токены, затем токены ответа."""
    prompt = ""
    for message in reversed(messages or []):
        if message.get("role") == "user":
            prompt = clean_prompt(str(message.get("content") or ""))
            break

    if kwargs.get("reasoning"):
        for chunk in _split(reasoning(prompt)):
            yield "reasoning", chunk
            time.sleep(0.012)

    for chunk in _split(complete(messages)):
        yield "token", chunk
        time.sleep(0.012)


def _split(text: str, size: int = 18):
    """Режем текст на кусочки примерно по границам слов — для плавной печати."""
    text = text or ""
    for start in range(0, len(text), size):
        yield text[start:start + size]


def models_catalog():
    return [
        {
            "id": MODEL_ID,
            "name": MODEL_NAME,
            "description": "Встроенная офлайн-модель: работает без интернета и без ключей. "
                           "Не ищет в сети и не знает новостей.",
            "provider": PROVIDER_ID,
            "provider_name": PROVIDER_NAME,
            "context_length": 8192,
            "modality": "text",
            "pricing_status": "free",
            "price_prompt": "0",
            "price_completion": "0",
            "pricing_label": "бесплатно (офлайн)",
            "recommended": True,
            "free": True,
        }
    ]
