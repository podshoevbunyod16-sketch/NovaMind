import requests
import argparse
import json
import sys
import base64
from io import BytesIO
from urllib.parse import quote

# Конфигурация для бесплатных API
# Вариант 1: Pollinations.ai (Не требует ключа, качественно, быстро) - РЕКОМЕНДУЮ
POLLINATIONS_API_URL = "https://image.pollinations.ai/prompt/{prompt}"

# Вариант 2: Hugging Face (Требуется бесплатный токен, доступ к новейшим моделям вроде FLUX)
# Раскомментируйте, если хотите использовать HF. Бесплатный токен можно получить на huggingface.co/settings/tokens
HF_API_URL = "https://api-inference.huggingface.co/models/black-forest-labs/FLUX.1-dev"
HF_TOKEN = "" # Вставьте ваш токен сюда или передайте через аргумент --hf_token

def generate_image_pollinations(prompt, width=1024, height=1024):
    """
    Генерирует изображение используя бесплатный API Pollinations.ai (Flux модель).
    Возвращает URL изображения, который можно вставить в чат.
    """
    # Кодируем промпт для URL
    encoded_prompt = quote(prompt)
    # Формируем URL с параметрами качества
    # Добавляем параметры ширины, высоты и отключаем nsfw фильтр, если нужно
    url = f"https://image.pollinations.ai/prompt/{encoded_prompt}?width={width}&height={height}&nologo=True&seed={abs(hash(prompt)) % 10000}"
    
    # Делаем GET запрос для получения картинки
    # Важно: stream=True, чтобы не грузить всё в память сразу, если изображение большое
    response = requests.get(url, stream=True)
    
    if response.status_code == 200:
        # Вместо сохранения на диск, возвращаем готовый URL
        # Большинство фронтендов умеют показывать картинки по прямой ссылке
        # Либо конвертируем в base64 для вставки прямо в текст
        return {
            "success": True,
            "url": url, 
            "raw_data": response.content # На случай, если фронтенд хочет base64
        }
    else:
        return {"success": False, "error": f"API Pollinations вернул ошибку: {response.status_code}"}

def generate_image_huggingface(prompt, token, width=1024, height=1024):
    """
    Генерация через Hugging Face Inference API (качественно, но медленнее первого варианта).
    """
    if not token:
        return {"success": False, "error": "Требуется HF_TOKEN для этого метода генерации."}
        
    headers = {"Authorization": f"Bearer {token}"}
    # Некоторые модели поддерживают параметры в payload
    payload = {
        "inputs": prompt,
        "parameters": {
            "width": width,
            "height": height,
            "guidance_scale": 7.5
        }
    }
    
    try:
        response = requests.post(HF_API_URL, headers=headers, json=payload)
        
        if response.status_code == 200:
            # Конвертируем бинарные данные изображения в base64 для вставки в JSON ответ
            img_base64 = base64.b64encode(response.content).decode('utf-8')
            return {
                "success": True,
                "base64": img_base64,
                "url": "data:image/png;base64," + img_base64
            }
        else:
            return {"success": False, "error": f"HF API Error: {response.status_code} - {response.text}"}
    except Exception as e:
        return {"success": False, "error": str(e)}

if __name__ == "__main__":
    # --- Настройка парсера аргументов (Run Args) ---
    parser = argparse.ArgumentParser(description='Мощный бесплатный плагин для генерации изображений')
    parser.add_argument('--prompt', type=str, required=True, help='Описание изображения (text prompt)')
    parser.add_argument('--width', type=int, default=1024, help='Ширина изображения (по умолч. 1024)')
    parser.add_argument('--height', type=int, default=1024, help='Высота изображения (по умолч. 1024)')
    parser.add_argument('--provider', type=str, default='pollinations', choices=['pollinations', 'huggingface'], 
                        help='Выбор провайдера (pollinations - без ключа, huggingface - нужен токен)')
    parser.add_argument('--hf_token', type=str, default='', help='Токен Hugging Face (нужен если provider=huggingface)')
    
    args = parser.parse_args()
    
    # --- Логика генерации ---
    print(f"🎨 Генерируем изображение по запросу: '{args.prompt}' (Размер: {args.width}x{args.height})")
    
    result = None
    if args.provider == 'pollinations':
        result = generate_image_pollinations(args.prompt, args.width, args.height)
    elif args.provider == 'huggingface':
        token = args.hf_token if args.hf_token else HF_TOKEN
        result = generate_image_huggingface(args.prompt, token, args.width, args.height)
    
    # --- Вывод результата в формате JSON для легкого чтения чат-ботом или веб-клиентом ---
    if result and result.get("success"):
        output = {
            "status": "success",
            "message": f"✅ Изображение успешно сгенерировано!",
            "image_url": result.get("url"), # Эту ссылку веб-клиент покажет как картинку
            "prompt": args.prompt,
            "dimensions": f"{args.width}x{args.height}"
        }
        print(json.dumps(output, indent=2, ensure_ascii=False))
        
        # Дополнительно: если нужен вывод ссылки в чистом виде для копирования
        print(f"\n🔗 Прямая ссылка на изображение: {result.get('url')}")
        
    else:
        error_msg = result.get("error", "Неизвестная ошибка") if result else "Не удалось получить результат"
        output = {
            "status": "error",
            "message": f"❌ Ошибка генерации: {error_msg}"
        }
        print(json.dumps(output, indent=2, ensure_ascii=False))
        sys.exit(1)
