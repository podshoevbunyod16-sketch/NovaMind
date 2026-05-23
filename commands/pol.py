import requests
from datetime import datetime
import os

def run(args):
    if not args:
        return "Использование: /pollinations_image <описание изображения>"

    prompt = " ".join(args)
    # Правильный прямой URL для получения изображения
    image_url = f"https://image.pollinations.ai/prompt/{requests.utils.quote(prompt)}?width=1024&height=1024&nologo=true"

    try:
        # Скачиваем изображение (Pollinations генерирует на лету)
        resp = requests.get(image_url, timeout=60)
        resp.raise_for_status()

        # Проверяем, что это действительно изображение
        content_type = resp.headers.get("Content-Type", "")
        if not content_type.startswith("image/"):
            return f"Ошибка: получен неверный тип содержимого ({content_type}). Возможно, сервис недоступен."

        # Сохраняем
        img_dir = os.path.join(os.path.dirname(__file__), "..", "generated_images")
        os.makedirs(img_dir, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"pollinations_{timestamp}.png"
        filepath = os.path.join(img_dir, filename)

        with open(filepath, "wb") as f:
            f.write(resp.content)

        # Возвращаем HTML с отображением в чате
        image_view_url = f"/generated_image?file={filename}"
        return f'✅ Изображение сгенерировано (Pollinations.AI):<br><img src="{image_view_url}" alt="Generated Image" style="max-width:100%; border-radius:12px; margin-top:8px;">'

    except Exception as e:
        return f"Ошибка генерации изображения: {e}"
