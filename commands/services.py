import requests
import re

def run(args):
    if not args:
        return "Использование: /services [weather <город>|currency <валюта1> <валюта2>|wiki <запрос>]"

    service = args[0]
    params = args[1:]

    if service == "weather":
        return handle_weather(params)
    elif service == "currency":
        return handle_currency(params)
    elif service == "wiki":
        return handle_wiki(params)
    else:
        return "Неизвестный сервис. Используйте weather/currency/wiki"


def handle_weather(params):
    if not params:
        return "Укажите город: /services weather Москва"
    city = " ".join(params)

    try:
        # Используем Open-Meteo API — бесплатный, без ключа, поддерживает русские города
        # Сначала ищем координаты города
        geo_url = "https://geocoding-api.open-meteo.com/v1/search"
        geo_resp = requests.get(geo_url, params={"name": city, "count": 1, "language": "ru"}, timeout=10)
        geo_data = geo_resp.json()

        if not geo_data.get("results"):
            return f"Город '{city}' не найден. Попробуйте написать название латиницей."

        location = geo_data["results"][0]
        lat = location["latitude"]
        lon = location["longitude"]
        city_name = location.get("name", city)
        country = location.get("country", "")

        # Получаем погоду
        weather_url = "https://api.open-meteo.com/v1/forecast"
        weather_params = {
            "latitude": lat,
            "longitude": lon,
            "current": ["temperature_2m", "relative_humidity_2m", "weather_code", "wind_speed_10m"],
            "timezone": "auto",
            "language": "ru"
        }
        weather_resp = requests.get(weather_url, params=weather_params, timeout=10)
        weather_data = weather_resp.json()

        current = weather_data.get("current", {})
        temp = current.get("temperature_2m", "?")
        humidity = current.get("relative_humidity_2m", "?")
        wind = current.get("wind_speed_10m", "?")
        code = current.get("weather_code", 0)

        # Расшифровка weather_code
        weather_desc = get_weather_description(code)

        result = f"🌤 Погода в {city_name}"
        if country:
            result += f", {country}"
        result += f"\n🌡 Температура: {temp}°C"
        result += f"\n💧 Влажность: {humidity}%"
        result += f"\n💨 Ветер: {wind} км/ч"
        result += f"\n☁ {weather_desc}"
        return result

    except Exception as e:
        return f"Ошибка погоды: {e}"


def get_weather_description(code):
    """Расшифровка кода погоды Open-Meteo"""
    codes = {
        0: "Ясно",
        1: "Преимущественно ясно", 2: "Переменная облачность", 3: "Пасмурно",
        45: "Туман", 48: "Изморозь",
        51: "Морось слабая", 53: "Морось умеренная", 55: "Морось сильная",
        56: "Ледяная морось слабая", 57: "Ледяная морось сильная",
        61: "Дождь слабый", 63: "Дождь умеренный", 65: "Дождь сильный",
        66: "Ледяной дождь слабый", 67: "Ледяной дождь сильный",
        71: "Снегопад слабый", 73: "Снегопад умеренный", 75: "Снегопад сильный",
        77: "Снежные зерна",
        80: "Ливневый дождь слабый", 81: "Ливневый дождь умеренный", 82: "Ливневый дождь сильный",
        85: "Снегопад слабый", 86: "Снегопад сильный",
        95: "Гроза", 96: "Гроза с градом слабым", 99: "Гроза с градом сильным"
    }
    return codes.get(code, "Неизвестно")


def handle_currency(params):
    if len(params) < 2:
        return "Укажите две валюты: /services currency USD RUB"

    base_input = params[0].upper()
    target_input = params[1].upper()

    # Словарь валют: русское название/сокращение -> ISO код
    currency_map = {
        # Русские названия
        "ДОЛЛАР": "USD", "ДОЛЛАРЫ": "USD", "ДОЛЛАРОВ": "USD",
        "ЕВРО": "EUR",
        "РУБЛЬ": "RUB", "РУБЛИ": "RUB", "РУБЛЕЙ": "RUB",
        "ФУНТ": "GBP", "ФУНТЫ": "GBP", "ФУНТОВ": "GBP",
        "ЙЕНА": "JPY", "ЙЕНЫ": "JPY", "ЙЕН": "JPY",
        "ЮАНЬ": "CNY", "ЮАНИ": "CNY", "ЮАНЕЙ": "CNY",
        "ТЕНГЕ": "KZT",
        "ФРАНК": "CHF", "ФРАНКИ": "CHF", "ФРАНКОВ": "CHF",
        "КРОНА": "CZK", "КРОНЫ": "CZK", "КРОН": "CZK",
        "ЗЛОТЫЙ": "PLN", "ЗЛОТЫЕ": "PLN", "ЗЛОТЫХ": "PLN",
        "ЛЕВ": "BGN", "ЛЕВЫ": "BGN", "ЛЕВОВ": "BGN",
        "ЛИРА": "TRY", "ЛИРЫ": "TRY", "ЛИР": "TRY",
        "ВОН": "KRW", "ВОНА": "KRW", "ВОНЫ": "KRW",
        "РУПИЯ": "INR", "РУПИИ": "INR", "РУПИЙ": "INR",
        "РИАЛ": "SAR", "РИАЛЫ": "SAR", "РИАЛОВ": "SAR",
        "ДИРХАМ": "AED", "ДИРХАМЫ": "AED", "ДИРХАМОВ": "AED",
        "БАТ": "THB", "БАТЫ": "THB", "БАТОВ": "THB",
        "ПЕСО": "MXN", "ПЕСО": "MXN",
        "КАНАДСКИЙ ДОЛЛАР": "CAD", "КАНАДСКИХ ДОЛЛАРОВ": "CAD",
        "АВСТРАЛИЙСКИЙ ДОЛЛАР": "AUD", "АВСТРАЛИЙСКИХ ДОЛЛАРОВ": "AUD",
        "ШВЕЙЦАРСКИЙ ФРАНК": "CHF", "ШВЕЙЦАРСКИХ ФРАНКОВ": "CHF",
        "НОРВЕЖСКАЯ КРОНА": "NOK", "НОРВЕЖСКИХ КРОН": "NOK",
        "ШВЕДСКАЯ КРОНА": "SEK", "ШВЕДСКИХ КРОН": "SEK",
        "ДАТСКАЯ КРОНА": "DKK", "ДАТСКИХ КРОН": "DKK",
        "ГРИВНА": "UAH", "ГРИВНЫ": "UAH", "ГРИВЕН": "UAH",
        "БЕЛОРУССКИЙ РУБЛЬ": "BYN", "БЕЛОРУССКИХ РУБЛЕЙ": "BYN",
        "МАНАТ": "AZN", "МАНАТЫ": "AZN", "МАНАТОВ": "AZN",
        "ДРАМ": "AMD", "ДРАМЫ": "AMD", "ДРАМОВ": "AMD",
        "ЛАРИ": "GEL", "ЛАРИ": "GEL",
        "СОМ": "KGS", "СОМА": "KGS", "СОМОВ": "KGS",
        "СУМ": "UZS", "СУМА": "UZS", "СУМОВ": "UZS",
        "ТАЙСКИЙ БАТ": "THB", "ТАЙСКИХ БАТОВ": "THB",
        "СИНГАПУРСКИЙ ДОЛЛАР": "SGD", "СИНГАПУРСКИХ ДОЛЛАРОВ": "SGD",
        "ГОНКОНГСКИЙ ДОЛЛАР": "HKD", "ГОНКОНГСКИХ ДОЛЛАРОВ": "HKD",
        "ВОН": "KRW", "ВОНА": "KRW", "ВОНЫ": "KRW",
        "РУПИЯ": "INR", "РУПИИ": "INR", "РУПИЙ": "INR",
        "БИТКОИН": "BTC", "БИТКОИНЫ": "BTC", "БИТКОИНОВ": "BTC",
        "ЭФИР": "ETH", "ЭФИРЫ": "ETH", "ЭФИРОВ": "ETH",
        # ISO коды (пропускаем как есть)
        "USD": "USD", "EUR": "EUR", "RUB": "RUB", "GBP": "GBP",
        "JPY": "JPY", "CNY": "CNY", "KZT": "KZT", "CHF": "CHF",
        "CZK": "CZK", "PLN": "PLN", "BGN": "BGN", "TRY": "TRY",
        "KRW": "KRW", "INR": "INR", "SAR": "SAR", "AED": "AED",
        "THB": "THB", "MXN": "MXN", "CAD": "CAD", "AUD": "AUD",
        "NOK": "NOK", "SEK": "SEK", "DKK": "DKK", "UAH": "UAH",
        "BYN": "BYN", "AZN": "AZN", "AMD": "AMD", "GEL": "GEL",
        "KGS": "KGS", "UZS": "UZS", "SGD": "SGD", "HKD": "HKD",
        "BTC": "BTC", "ETH": "ETH",
        # Символы
        "$": "USD", "€": "EUR", "₽": "RUB", "£": "GBP", "¥": "JPY",
        "₸": "KZT", "₴": "UAH", "₾": "GEL", "〒": "KZT",
    }

    base = currency_map.get(base_input, base_input)
    target = currency_map.get(target_input, target_input)

    # Если ввели что-то неизвестное — пробуем найти по частичному совпадению
    if base == base_input and len(base_input) > 2:
        for key, val in currency_map.items():
            if base_input in key or key in base_input:
                base = val
                break
    if target == target_input and len(target_input) > 2:
        for key, val in currency_map.items():
            if target_input in key or key in target_input:
                target = val
                break

    try:
        # Используем exchangerate-api.com (бесплатный)
        r = requests.get(f"https://api.exchangerate-api.com/v4/latest/{base}", timeout=10)
        data = r.json()
        rate = data["rates"].get(target)
        if rate is None:
            return f"Валюта {target} не найдена. Доступные: {', '.join(list(data['rates'].keys())[:20])}..."

        # Форматируем красиво
        if rate >= 1:
            rate_str = f"{rate:.2f}"
        else:
            rate_str = f"{rate:.4f}"

        # Обратный курс
        reverse_rate = 1 / rate if rate != 0 else 0
        if reverse_rate >= 1:
            reverse_str = f"{reverse_rate:.2f}"
        else:
            reverse_str = f"{reverse_rate:.4f}"

        return (
            f"💱 Курс валют\n"
            f"1 {base} = {rate_str} {target}\n"
            f"1 {target} = {reverse_str} {base}\n"
            f"📅 Дата: {data.get('date', 'сегодня')}"
        )
    except Exception as e:
        return f"Ошибка валют: {e}"


def handle_wiki(params):
    if not params:
        return "Укажите запрос: /services wiki Python"
    query = " ".join(params)

    try:
        # Используем MediaWiki API для поиска
        # Сначала ищем страницу
        search_url = "https://ru.wikipedia.org/w/api.php"
        search_params = {
            "action": "query",
            "list": "search",
            "srsearch": query,
            "format": "json",
            "srlimit": 1,
            "utf8": 1
        }
        search_resp = requests.get(search_url, params=search_params, timeout=10)
        search_data = search_resp.json()

        search_results = search_data.get("query", {}).get("search", [])
        if not search_results:
            # Пробуем английскую Википедию
            search_params["srsearch"] = query
            search_resp = requests.get("https://en.wikipedia.org/w/api.php", params=search_params, timeout=10)
            search_data = search_resp.json()
            search_results = search_data.get("query", {}).get("search", [])
            if not search_results:
                return f"По запросу '{query}' ничего не найдено."
            # Английская версия
            page_title = search_results[0]["title"]
            summary_url = f"https://en.wikipedia.org/api/rest_v1/page/summary/{requests.utils.quote(page_title)}"
            summary_resp = requests.get(summary_url, timeout=10)
            summary_data = summary_resp.json()
            extract = summary_data.get("extract", "Нет описания")
            url = summary_data.get("content_urls", {}).get("desktop", {}).get("page", "")
            return f"📚 {page_title} (en)\n{extract[:800]}\n🔗 {url}" if url else f"📚 {page_title} (en)\n{extract[:800]}"

        # Русская версия
        page_title = search_results[0]["title"]

        # Получаем summary через REST API
        summary_url = f"https://ru.wikipedia.org/api/rest_v1/page/summary/{requests.utils.quote(page_title)}"
        summary_resp = requests.get(summary_url, timeout=10)
        summary_data = summary_resp.json()

        extract = summary_data.get("extract", "Нет описания")
        url = summary_data.get("content_urls", {}).get("desktop", {}).get("page", "")

        # Ограничиваем длину
        if len(extract) > 1000:
            extract = extract[:997] + "..."

        result = f"📚 {page_title}\n{extract}"
        if url:
            result += f"\n🔗 {url}"
        return result

    except Exception as e:
        return f"Ошибка Википедии: {e}"