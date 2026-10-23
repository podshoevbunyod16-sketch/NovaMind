"""usage_stats.py — счётчик токенов и стоимости (бонус Б3).

Что делает:

  * считает токены хода и чата — из `usage` провайдера, а если его нет
    (поток, офлайн-модель), оценивает по символам;
  * переводит токены в деньги по маленькой таблице цен, всё неизвестное
    честно показывает как «цену неизвестна», а не как ноль;
  * отдаёт остаток лимитов Groq-ключа, чтобы предупредить заранее.

Никаких сетевых вызовов и зависимостей: только stdlib.
"""
from __future__ import annotations

import re

import database

# Цены в долларах за 1 млн токенов: (вход, выход).
# Значения ориентировочные — их задача показать порядок расхода, а не
# выставить счёт. Обновлять при смене тарифов у провайдера.
PRICES = {
    "groq": (0.0, 0.0),                       # бесплатный уровень
    "pollinations": (0.0, 0.0),
    "local_demo": (0.0, 0.0),
    "google_ai_studio": (0.10, 0.40),         # gemini flash, ориентир
    "gemini": (0.10, 0.40),
    "openrouter": (0.0, 0.0),                 # :free-модели по умолчанию
    "cerebras": (0.0, 0.0),
}

# Отдельные исключения по модели (подстрока в имени модели).
MODEL_PRICES = {
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "claude-3-5-haiku": (0.80, 4.00),
    "claude-sonnet": (3.00, 15.00),
    "gemini-2.5-pro": (1.25, 10.00),
    "deepseek": (0.27, 1.10),
}

CHARS_PER_TOKEN = 4.0


def estimate_tokens(text) -> int:
    """Грубая оценка токенов по длине текста (1 токен ≈ 4 символа)."""
    if not text:
        return 0
    return max(1, int(len(str(text)) / CHARS_PER_TOKEN + 0.5))


def _usage_value(usage, *keys):
    if not isinstance(usage, dict):
        return None
    for key in keys:
        value = usage.get(key)
        if isinstance(value, (int, float)) and value >= 0:
            return int(value)
    return None


def tokens_from_usage(usage):
    """(prompt, completion) из ответа провайдера или (None, None)."""
    prompt = _usage_value(usage, "prompt_tokens", "input_tokens", "promptTokenCount")
    completion = _usage_value(usage, "completion_tokens", "output_tokens",
                              "candidatesTokenCount")
    if prompt is None and completion is None:
        total = _usage_value(usage, "total_tokens", "total")
        return (None, None) if total is None else (total, 0)
    return prompt or 0, completion or 0


def price_for(provider, model):
    """(цена_входа, цена_выхода) за 1 млн токенов или None, если неизвестно."""
    name = str(model or "").lower()
    for hint, price in MODEL_PRICES.items():
        if hint in name:
            return price
    return PRICES.get(str(provider or "").lower())


def cost_of(provider, model, prompt_tokens, completion_tokens):
    """Стоимость в долларах (0.0, если провайдер бесплатный)."""
    price = price_for(provider, model)
    if not price:
        return None
    price_in, price_out = price
    return round((prompt_tokens * price_in + completion_tokens * price_out) / 1_000_000, 6)


def record_turn(chat_id, provider, model, prompt_text="", answer_text="", usage=None):
    """Считает расход хода и пишет его в базу.

    Возвращает словарь для потока агента: токены хода, цена и итоги чата.
    """
    prompt_tokens, completion_tokens = tokens_from_usage(usage)
    # Провайдер может не отдавать usage: ноль — это «неизвестно», а не «бесплатно».
    if not prompt_tokens:
        prompt_tokens = estimate_tokens(prompt_text)
    if not completion_tokens:
        completion_tokens = estimate_tokens(answer_text)
    cost = cost_of(provider, model, prompt_tokens, completion_tokens)
    row = database.add_usage(chat_id, provider, model, prompt_tokens,
                             completion_tokens, cost or 0.0)
    row["cost_known"] = cost is not None
    row["chat"] = database.usage_totals(chat_id)
    return row


def chat_summary(chat_id):
    """Итоги чата для интерфейса: токены, деньги и подписи для показа."""
    totals = database.usage_totals(chat_id)
    totals["tokens_label"] = format_tokens(totals["total_tokens"])
    totals["cost_label"] = format_cost(totals["cost_usd"])
    return totals


def format_tokens(count) -> str:
    """1234 → «1.2 тыс.», 1234567 → «1.2 млн»."""
    count = int(count or 0)
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f} млн"
    if count >= 1_000:
        return f"{count / 1_000:.1f} тыс."
    return str(count)


def format_cost(cost) -> str:
    """Деньги: до долей цента показываем «< $0.01»."""
    if cost is None:
        return "цена неизвестна"
    if cost <= 0:
        return "бесплатно"
    if cost < 0.01:
        return "< $0.01"
    return f"${cost:.2f}"


def groq_limits():
    """Остаток лимитов Groq-ключей + текст предупреждения."""
    import groq_rotation
    status = groq_rotation.limits_status()
    return {"keys": status, "warning": groq_rotation.limits_warning(status)}


def parse_retry_after(text) -> int:
    """Достаёт «try again in 12.5s» из ошибки провайдера (для сообщений)."""
    match = re.search(r"try again in ([0-9.]+)\s*(ms|s|m)", str(text or ""), re.I)
    if not match:
        return 0
    value = float(match.group(1))
    unit = match.group(2).lower()
    if unit == "ms":
        return max(1, int(value / 1000))
    if unit == "m":
        return int(value * 60)
    return max(1, int(value))
