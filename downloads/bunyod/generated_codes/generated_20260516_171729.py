import os
import requests
from PIL import Image
import numpy as np
import uuid

# Создаем папку, если ее нет
os.makedirs('generated_images', exist_ok=True)

def generate_random_image(width=256, height=256):
    # Генерируем случайное изображение (RGB)
    random_array = np.random.randint(0, 256, (height, width, 3), dtype=np.uint8)
    return Image.fromarray(random_array)

def save_image(image):
    # Генерируем уникальное имя файла
    filename = f"{uuid.uuid4()}.png"
    filepath = os.path.join('generated_images', filename)
    image.save(filepath)
    return filepath

def upload_to_polyanation(filepath):
    # Замените URL на реальный API endpoint полинации
    url = "https://api.polyanation.com/upload"  
    with open(filepath, 'rb') as f:
        files = {'file': f}
        response = requests.post(url, files=files)
    return response.json()

# Основной процесс
if __name__ == "__main__":
    # Генерируем случайное изображение
    img = generate_random_image()
    
    # Сохраняем локально
    saved_path = save_image(img)
    print(f"Изображение сохранено: {saved_path}")
    
    # Пытаемся отправить (закомментировано, так как URL примерный)
    # response = upload_to_polyanation(saved_path)
    # print("Ответ сервера:", response)