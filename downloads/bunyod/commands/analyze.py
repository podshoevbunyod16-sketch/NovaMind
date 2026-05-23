import os
import sys
import requests
import base64

def run(args):
    if not args:
        return "Использование: /analyze_image <путь к файлу> или загрузите изображение через кнопку '+'"

    source = " ".join(args)
    
    try:
        # Пытаемся открыть как локальный файл
        filepath = os.path.expanduser(source)
        if os.path.exists(filepath):
            with open(filepath, "rb") as image_file:
                image_data = base64.b64encode(image_file.read()).decode('utf-8')
                mime_type = "image/jpeg"
                if filepath.lower().endswith(".png"):
                    mime_type = "image/png"
                elif filepath.lower().endswith(".webp"):
                    mime_type = "image/webp"
                elif filepath.lower().endswith(".gif"):
                    mime_type = "image/gif"
                data_url = f"data:{mime_type};base64,{image_data}"
        else:
            return f"Файл не найден: {filepath}"
    except Exception as e:
        return f"Ошибка чтения файла: {e}"

    # Используем OpenRouter с бесплатной Gemini
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        api_key = os.environ.get("OPENROUTER_IMAGE_KEY")
    if not api_key:
        return "Ошибка: OPENROUTER_API_KEY не найден в .env"

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }

    prompt = """Внимательно посмотри на это изображение и подробно опиши:
1. Что изображено (главные объекты, люди, животные, предметы)
2. Цветовая гамма и освещение
3. Настроение и атмосфера
4. Возможный контекст или место действия
5. Любые интересные детали или текст, если он есть

Отвечай на русском языке, развернуто, но без лишних слов."""

    payload = {
        "model": "google/gemini-2.5-flash",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": data_url}}
                ]
            }
        ],
        "max_tokens": 1000
    }

    try:
        response = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers=headers,
            json=payload,
            timeout=60
        )
        response.raise_for_status()
        result = response.json()
        description = result["choices"][0]["message"]["content"]
        return f"**Анализ изображения:**\n{description}"

    except Exception as e:
        error_msg = str(e)
        if "rate_limit" in error_msg.lower() or "429" in error_msg:
            return "Превышен лимит запросов. Попробуйте позже."
        return f"Ошибка при анализе изображения: {error_msg}"
