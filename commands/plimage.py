import requests
from io import BytesIO

CHAT_API_URL = "https://ai-maker-b5v5.onrender.com/api/messages"  # Уточните endpoint

def run(args):
    if not args:
        return "Использование: /image <описание>"

    prompt = " ".join(args)
    # Формируем URL для генерации (Pollinations.ai)
    url = f"https://image.pollinations.ai/prompt/{requests.utils.quote(prompt)}?width=1024&height=1024&nologo=true"

    try:
        # Скачиваем изображение в память
        resp = requests.get(url)
        resp.raise_for_status()

        # Отправляем изображение прямо в чат без сохранения
        chat_response = requests.post(
            CHAT_API_URL,
            files={
                "image": ("generated_image.jpg", BytesIO(resp.content), "image/jpeg")
            },
            data={
                "prompt": prompt,
                "message": f"Сгенерировано изображение по запросу: {prompt}"
            }
        )
        
        if chat_response.status_code == 200:
            return f"✅ Изображение сгенерировано и отправлено в чат: {prompt}"
        else:
            return f"❌ Ошибка отправки в чат (статус {chat_response.status_code}): {chat_response.text}"
            
    except Exception as e:
        return f"Ошибка: {e}"