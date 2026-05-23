import urllib.parse

def run(args):
    if not args:
        return "Использование: /versus <название товара>"

    query = " ".join(args)
    # Формируем ссылку на Versus с поисковым запросом
    encoded_query = urllib.parse.quote(query)
    url = f"https://versus.com/ru/search?q={encoded_query}"
    
    return f"🔍 Откройте сравнение на Versus:\n{url}\n\n_Совет: Versus лучше всего подходит для сравнения популярных моделей телефонов, ноутбуков и другой электроники._"
