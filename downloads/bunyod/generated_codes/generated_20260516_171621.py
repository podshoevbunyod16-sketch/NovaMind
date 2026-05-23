import os
import numpy as np
from PIL import Image
import random

# Создаем папку для сохранения изображений, если ее нет
if not os.path.exists('generated_images'):
    os.makedirs('generated_images')

def generate_random_image(width=256, height=256):
    """
    Генерирует случайное изображение заданного размера.
    Возвращает объект PIL.Image.
    """
    # Создаем массив случайных значений (0-255)
    random_array = np.random.randint(0, 256, (height, width, 3), dtype=np.uint8)
    # Конвертируем в изображение
    return Image.fromarray(random_array)

def save_image(image, folder='generated_images', prefix='img'):
    """
    Сохраняет изображение в указанную папку с уникальным именем.
    """
    # Генерируем уникальное имя файла
    filename = f"{prefix}_{random.randint(1000, 9999)}.png"
    filepath = os.path.join(folder, filename)
    # Сохраняем изображение
    image.save(filepath)
    return filepath

# Генерируем и сохраняем случайное изображение
random_image = generate_random_image()
saved_path = save_image(random_image)

print(f"Изображение сохранено: {saved_path}")