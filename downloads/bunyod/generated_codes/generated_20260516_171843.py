import os
import random
from PIL import Image, ImageDraw
import requests

def generate_random_image(width=256, height=256):
    """Генерация случайного изображения с помощью Pillow"""
    # Создаем новое изображение в режиме RGB
    image = Image.new('RGB', (width, height))
    draw = ImageDraw.Draw(image)
    
    # Заполняем изображение случайными пикселями
    for x in range(width):
        for y in range(height):
            # Генерируем случайный цвет (R, G, B)
            color = (random.randint(0, 255), random.randint(0, 255), random.randint(0, 255))
            draw.point((x, y), fill=color)
    
    return image

def save_image_from_url(url, save_path):
    """Скачивание изображения по URL и сохранение"""
    response = requests.get(url, stream=True)
    if response.status_code == 200:
        with open(save_path, 'wb') as f:
            for chunk in response:
                f.write(chunk)

def main():
    # Создаем папку для сохранения, если ее нет
    os.makedirs('generated_images', exist_ok=True)
    
    # Генерируем случайное изображение
    random_image = generate_random_image()
    random_image_path = os.path.join('generated_images', 'random_image.png')
    random_image.save(random_image_path)
    print(f"Случайное изображение сохранено: {random_image_path}")
    
    # Пример загрузки изображения по URL (раскомментируйте при необходимости)
    # image_url = "https://example.com/image.jpg"
    # downloaded_image_path = os.path.join('generated_images', 'downloaded_image.jpg')
    # save_image_from_url(image_url, downloaded_image_path)
    # print(f"Изображение загружено и сохранено: {downloaded_image_path}")

if __name__ == "__main__":
    main()