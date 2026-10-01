"""Язык интерфейса на сервере: тоҷикӣ (по умолчанию), русский, English.

Браузер передаёт язык в теле запроса (``lang``) и в cookie ``nova_lang``
(его ставит static/i18n.js). Отсюда сервер берёт:
  • язык ответов ИИ — :func:`language_rule` дописывается к системному промпту;
  • язык статусов, которые видит пользователь (этапы, шаги, ошибки) — :func:`tr`.

Ключ перевода — исходная русская строка, как и во фронтенде: нет перевода —
показывается русский текст.
"""

from flask import g, has_request_context, request

LANGS = ("tg", "ru", "en")
DEFAULT_LANG = "tg"
COOKIE = "nova_lang"

# 'русская строка': (тоҷикӣ, English). Плейсхолдеры — str.format: {name}.
DICT = {
    # ── статус и ошибки агента ──
    "ИИ-агент выключен (AGENT_ENABLED=0) — чат отвечает без инструментов": (
        "Агенти AI хомӯш аст (AGENT_ENABLED=0) — чат бе абзорҳо ҷавоб медиҳад",
        "The AI agent is off (AGENT_ENABLED=0) — the chat answers without tools"),
    "Войдите в аккаунт, чтобы ИИ мог искать, генерировать медиа и работать в Linux": (
        "Ба ҳисоб ворид шавед, то AI тавонад ҷустуҷӯ кунад, медиа созад ва дар Linux кор кунад",
        "Sign in so the AI can search, generate media and work in Linux"),
    "ИИ ищет в интернете и генерирует медиа. Для Linux-окружения добавьте TERMINAL_ENABLED=1 в .env и перезапустите сервер": (
        "AI дар интернет меҷӯяд ва медиа месозад. Барои муҳити Linux TERMINAL_ENABLED=1-ро ба .env илова кунед ва серверро аз нав оғоз кунед",
        "The AI searches the web and generates media. For the Linux environment add TERMINAL_ENABLED=1 to .env and restart the server"),
    "Пустое сообщение": ("Паём холӣ аст", "Empty message"),
    "ИИ-агент выключен (AGENT_ENABLED=0)": ("Агенти AI хомӯш аст (AGENT_ENABLED=0)", "The AI agent is off (AGENT_ENABLED=0)"),
    "Войдите в аккаунт, чтобы ИИ мог работать с инструментами": (
        "Ба ҳисоб ворид шавед, то AI бо абзорҳо кор карда тавонад",
        "Sign in so the AI can use tools"),

    # ── шаги агента ──
    "Время вышло": ("Вақт тамом шуд", "Time is up"),
    "Шаги закончились": ("Қадамҳо тамом шуданд", "Out of steps"),
    "{note} — собираю ответ": ("{note} — ҷавобро ҷамъ мекунам", "{note} — putting the answer together"),
    "модель не ответила": ("модел ҷавоб надод", "the model did not answer"),
    "Это уже сделано — не повторяю": ("Ин аллакай иҷро шудааст — такрор намекунам", "Already done — not repeating"),
    "Работаю в Linux": ("Дар Linux кор мекунам", "Working in Linux"),
    "Работаю над задачей": ("Дар болои вазифа кор мекунам", "Working on the task"),
    "Иду к цели по шагам…": ("Қадам ба қадам ба ҳадаф меравам…", "Moving towards the goal step by step…"),
    "Не удалось получить ответ модели. Проверьте модель в настройках и повторите.": (
        "Ҷавоби модел гирифта нашуд. Моделро дар танзимот санҷед ва такрор кунед.",
        "Could not get a model answer. Check the model in settings and try again."),
    "Поищу в интернете": ("Дар интернет меҷӯям", "Searching the web"),
    "Ищу через Linux-окружение…": ("Тавассути муҳити Linux меҷӯям…", "Searching via the Linux environment…"),
    "Ищу источники…": ("Манбаъҳоро меҷӯям…", "Looking for sources…"),
    "Поиск ничего не дал — попробую по-другому": ("Ҷустуҷӯ натиҷа надод — тарзи дигар мекӯшам", "The search found nothing — trying another way"),
    "Нашёл {count} источников ({backend})": ("{count} манбаъ ёфтам ({backend})", "Found {count} sources ({backend})"),
    "поиск": ("ҷустуҷӯ", "search"),
    "Читаю {count} страницы…": ("{count} саҳифаро мехонам…", "Reading {count} pages…"),
    "Сохранил выдержки: {saved}": ("Иқтибосҳо нигоҳ дошта шуданд: {saved}", "Saved excerpts: {saved}"),
    "Разбираю найденное": ("Ёфтаҳоро таҳлил мекунам", "Going through the findings"),
    "Решаю, хватает ли данных": ("Муайян мекунам, ки маълумот басанда аст ё не", "Deciding whether there is enough data"),
    "Задача: {title}": ("Вазифа: {title}", "Task: {title}"),
    "Поиск «{query}» ничего не дал": ("Ҷустуҷӯи «{query}» натиҷа надод", "The search for “{query}” found nothing"),
    "Нашёл {count} по запросу «{query}» ({backend})": ("Барои «{query}» {count} натиҷа ёфтам ({backend})", "Found {count} for “{query}” ({backend})"),
    "Прочитал {url} ({ms} мс)": ("{url} хонда шуд ({ms} мс)", "Read {url} ({ms} ms)"),
    "страница пустая": ("саҳифа холӣ аст", "the page is empty"),
    "Смотрю файлы": ("Файлҳоро аз назар мегузаронам", "Looking at the files"),
    "Файла нет": ("Файл нест", "No such file"),
    "Читаю {path}": ("{path}-ро мехонам", "Reading {path}"),
    "Записал {path}": ("{path} навишта шуд", "Wrote {path}"),
    "{label} — код {code}": ("{label} — рамз {code}", "{label} — exit code {code}"),
    "Модель не смогла сформулировать итог, но вот что удалось найти:\n\n{links}\n\nПопробуйте спросить ещё раз или сменить модель в настройках.": (
        "Модел хулоса карда натавонист, аммо ин чизҳо ёфт шуданд:\n\n{links}\n\nБори дигар пурсед ё моделро дар танзимот иваз кунед.",
        "The model could not write a summary, but here is what was found:\n\n{links}\n\nTry asking again or switch the model in settings."),
    "Не удалось собрать ответ. Попробуйте ещё раз или смените модель в настройках.": (
        "Ҷавоб ҷамъ карда нашуд. Бори дигар кӯшиш кунед ё моделро дар танзимот иваз кунед.",
        "Could not put an answer together. Try again or switch the model in settings."),

    # ── медиа ──
    "Рисую изображение": ("Тасвир мекашам", "Drawing an image"),
    "Озвучиваю": ("Садо медиҳам", "Voicing"),
    "Делаю видео": ("Видео месозам", "Making a video"),
    "Смотрю, какие модели генерации доступны": ("Месанҷам, ки кадом моделҳои эҷод дастрасанд", "Checking which generation models are available"),
    "Не получилось: {reason}": ("Нашуд: {reason}", "Did not work: {reason}"),
    "неизвестная ошибка": ("хатои номаълум", "unknown error"),
    "Пробую {name} ({provider})": ("Кӯшиш мекунам: {name} ({provider})", "Trying {name} ({provider})"),
}

# Ответы ИИ: правило дописывается в конец системного промпта.
_LANGUAGE_RULES = {
    "tg": ("ЯЗЫК ОТВЕТА: интерфейс пользователя на таджикском. Всегда отвечай на таджикском языке "
           "(тоҷикӣ, кириллица: ӣ ӯ ҳ қ ғ ҷ), даже если инструкции выше на русском. Если пользователь "
           "сам пишет на другом языке или просит другой язык — отвечай на нём."),
    "ru": ("ЯЗЫК ОТВЕТА: отвечай по-русски. Если пользователь сам пишет на другом языке или просит "
           "другой язык — отвечай на нём."),
    "en": ("RESPONSE LANGUAGE: the user's interface is in English. Always reply in English, even though "
           "the instructions above are in Russian. If the user writes in another language or asks for "
           "one, reply in that language."),
}
_RULE_MARK = ("ЯЗЫК ОТВЕТА:", "RESPONSE LANGUAGE:")


def normalize(code):
    """'tg-TJ' → 'tg'; неизвестный язык → ''."""
    code = str(code or "").strip().lower()[:2]
    return code if code in LANGS else ""


def current_lang():
    """Язык текущего запроса: тело (lang) → форма/параметр → cookie → тоҷикӣ.

    Вне запроса (фоновые задачи, скрипты) — русский: так тексты совпадают с исходными.
    """
    if not has_request_context():
        return "ru"
    cached = getattr(g, "nova_lang", None)
    if cached:
        return cached
    lang = ""
    if request.is_json:
        data = request.get_json(silent=True)
        if isinstance(data, dict):
            lang = normalize(data.get("lang"))
    lang = (lang or normalize(request.form.get("lang")) or normalize(request.args.get("lang"))
            or normalize(request.cookies.get(COOKIE)) or DEFAULT_LANG)
    g.nova_lang = lang
    return lang


def tr(text, lang=None, **values):
    """Перевод строки интерфейса: tr('Читаю {path}', path='a.py')."""
    lang = normalize(lang) or current_lang()
    translated = text
    if lang != "ru":
        row = DICT.get(text)
        if row:
            translated = row[0 if lang == "tg" else 1]
    if values:
        try:
            return translated.format(**values)
        except (KeyError, IndexError, ValueError):
            return text.format(**values)
    return translated


def variants(text):
    """Все варианты строки на трёх языках (для распознавания своих же статусов)."""
    row = DICT.get(text)
    return (text,) + tuple(row) if row else (text,)


def language_rule(lang=None):
    """Правило языка ответа для системного промпта."""
    return _LANGUAGE_RULES[normalize(lang) or current_lang()]


def with_language(system, lang=None):
    """Дописать к системному промпту правило языка (один раз)."""
    system = system or ""
    if any(mark in system for mark in _RULE_MARK):
        return system
    rule = language_rule(lang)
    return f"{system}\n\n{rule}" if system else rule
