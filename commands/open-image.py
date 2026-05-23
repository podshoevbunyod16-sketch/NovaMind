import os
import sys
import base64
import requests
import re
from datetime import datetime

def run(args):
    if not args:
        return "Использование: /openrouter_image <описание изображения>"

    PROVIDER_URL = "https://openrouter.ai/api/v1/chat/completions"
    API_KEY = os.environ.get("OPENROUTER_IMAGE_KEY") or os.environ.get("OPENROUTER_API_KEY")
    
    if not API_KEY:
        return "Ошибка: OPENROUTER_IMAGE_KEY или OPENROUTER_API_KEY не задан в .env"

    MODEL = os.environ.get("IMAGE_MODEL", "google/gemini-2.5-flash-image")
    HTTP_REFERER = os.environ.get("SITE_URL", "http://localhost:5000")
    APP_TITLE = os.environ.get("APP_TITLE", "My AI Assistant")

    prompt = " ".join(args)

    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": HTTP_REFERER,
        "X-Title": APP_TITLE
    }

    payload = {
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "modalities": ["image", "text"]
    }

    try:
        resp = requests.post(PROVIDER_URL, json=payload, headers=headers, timeout=120)
        resp.raise_for_status()
        data = resp.json()

        message = data.get("choices", [{}])[0].get("message", {})
        images = message.get("images", [])

        if not images:
            return "Модель не вернула изображение."

        image_b64 = images[0]["image_url"]["url"]
        if "base64," in image_b64:
            image_b64 = image_b64.split("base64,", 1)[1]
        image_b64 = re.sub(r'\s+', '', image_b64)
        missing_padding = len(image_b64) % 4
        if missing_padding:
            image_b64 += '=' * (4 - missing_padding)

        image_data = base64.b64decode(image_b64)

        img_dir = os.path.join(os.path.dirname(__file__), "..", "generated_images")
        os.makedirs(img_dir, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"image_{timestamp}.png"
        filepath = os.path.join(img_dir, filename)

        with open(filepath, "wb") as f:
            f.write(image_data)

        # Возвращаем HTML-тег для отображения в чате
        image_url = f"/generated_image?file={filename}"
        return f'✅ Изображение сгенерировано:<br><img src="{image_url}" alt="Generated Image" style="max-width:100%; border-radius:12px; margin-top:8px;">'

    except Exception as e:
        return f"Ошибка генерации изображения: {e}"
