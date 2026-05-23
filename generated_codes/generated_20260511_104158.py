import requests
import argparse
from PIL import Image
from io import BytesIO

def generate_image(width=800, height=600, save_path='generated_image.jpg'):
    """
    Генерирует случайное изображение с помощью Picsum API и сохраняет его.
    
    :param width: Ширина изображения
    :param height: Высота изображения
    :param save_path: Путь для сохранения файла
    """
    try:
        # Получаем случайное изображение от Picsum API
        response = requests.get(f'https://picsum.photos/{width}/{height}')
        
        if response.status_code == 200:
            # Открываем изображение с помощью PIL
            img = Image.open(BytesIO(response.content))
            
            # Сохраняем изображение
            img.save(save_path)
            print(f"Изображение успешно сохранено как {save_path}")
        else:
            print("Ошибка при получении изображения")
            
    except Exception as e:
        print(f"Произошла ошибка: {e}")

def run(args):
    """
    Точка входа для выполнения плагина с аргументами командной строки
    """
    parser = argparse.ArgumentParser(description='Генератор случайных изображений')
    parser.add_argument('--width', type=int, default=800, help='Ширина изображения')
    parser.add_argument('--height', type=int, default=600, help='Высота изображения')
    parser.add_argument('--output', type=str, default='generated_image.jpg', help='Путь для сохранения')
    
    parsed_args = parser.parse_args(args)
    generate_image(parsed_args.width, parsed_args.height, parsed_args.output)

if __name__ == "__main__":
    import sys
    run(sys.argv[1:])