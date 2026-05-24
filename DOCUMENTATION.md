# 📚 NovaMind - Полная Техническая Документация

Содержание:
1. [Архитектура приложения](#архитектура-приложения)
2. [Структура базы данных](#структура-базы-данных)
3. [API Документация](#api-документация)
4. [Модули и функции](#модули-и-функции)
5. [Конфигурация](#конфигурация)
6. [Развёртывание](#развёртывание)
7. [FAQ](#faq)

---

## 🏗️ Архитектура приложения

```
┌─────────────────────────────────────────────────┐
│          БРАУЗЕР ПОЛЬЗОВАТЕЛЯ                   │
│       (HTML/CSS/JavaScript)                     │
└────────────────────┬────────────────────────────┘
                     │ HTTP/WebSocket
┌────────────────────▼────────────────────────────┐
│        FLASK WEB SERVER (app.py)                │
│  ├─ Routing (маршруты)                         │
│  ├─ Request Handler (обработка)                │
│  └─ Response Handler (ответ)                   │
└────────────────────┬────────────────────────────┘
                     │
        ┌────────────┼────────────┐
        │            │            │
   ┌────▼──┐  ┌─────▼──┐  ┌─────▼──┐
   │ ИИ    │  │ Файлы  │  │ JSON   │
   │ API   │  │ Manager│  │ Data   │
   └───────┘  └────────┘  └────────┘
```

### Компоненты системы:

| Компонент | Описание | Файл |
|-----------|---------|------|
| **Frontend** | HTML/CSS/JS интерфейс | `templates/`, `static/` |
| **Backend** | Flask приложение | `app.py` |
| **Commands** | Модули команд | `commands/` |
| **Data Storage** | JSON файлы и кэш | `*.json` |
| **File Manager** | Управление файлами | `uploads/`, `downloads/` |

---

## 📊 Структура базы данных

### chat_history.json
```json
{
  "sessions": [
    {
      "id": "session_123",
      "created_at": "2026-05-24T10:30:00Z",
      "updated_at": "2026-05-24T11:30:00Z",
      "messages": [
        {
          "id": "msg_001",
          "role": "user",
          "content": "Привет!",
          "timestamp": "2026-05-24T10:30:00Z"
        },
        {
          "id": "msg_002",
          "role": "assistant",
          "content": "Привет! Чем я могу помочь?",
          "timestamp": "2026-05-24T10:30:01Z"
        }
      ],
      "metadata": {
        "model": "gpt-4",
        "temperature": 0.7,
        "tokens_used": 150
      }
    }
  ]
}
```

### custom_commands.json
```json
{
  "commands": [
    {
      "name": "calculate",
      "trigger": "@calc",
      "description": "Калькулятор",
      "function": "calculate_expression",
      "active": true
    },
    {
      "name": "weather",
      "trigger": "@weather",
      "description": "Прогноз погоды",
      "function": "get_weather",
      "active": true
    }
  ]
}
```

---

## 🔌 API Документация

### 1. Чат (Chat API)

#### POST /api/chat
Отправить сообщение в чат и получить ответ от ИИ.

**Request:**
```json
{
  "message": "Как работает машинное обучение?",
  "session_id": "session_123",
  "model": "gpt-4",
  "temperature": 0.7,
  "max_tokens": 2000
}
```

**Response (200 OK):**
```json
{
  "success": true,
  "response": "Машинное обучение - это...",
  "message_id": "msg_002",
  "tokens_used": 245,
  "processing_time": 2.5
}
```

**Error Response (400):**
```json
{
  "success": false,
  "error": "Invalid request",
  "details": "Message field is required"
}
```

**Параметры:**
| Параметр | Тип | Требуется | Описание |
|----------|-----|----------|---------|
| message | string | ✅ | Текст сообщения |
| session_id | string | ❌ | ID сессии (создаётся автоматически) |
| model | string | ❌ | ИИ модель (default: gpt-4) |
| temperature | float | ❌ | 0.0-1.0 (default: 0.7) |
| max_tokens | int | ❌ | Макс. токенов (default: 2000) |

---

### 2. Генерация кода (Code Generation)

#### POST /api/generate/code
Генерирует код на основе описания.

**Request:**
```json
{
  "description": "напиши функцию для сортировки массива на Python",
  "language": "python",
  "style": "clean"
}
```

**Response (200 OK):**
```json
{
  "success": true,
  "code": "def sort_array(arr):\n    return sorted(arr)",
  "language": "python",
  "file_name": "code_2026_05_24_103000.py"
}
```

**Поддерживаемые языки:**
- `python`, `javascript`, `java`, `cpp`, `csharp`, `php`, `ruby`, `go`, `rust`, `typescript`

---

### 3. Генерация изображений (Image Generation)

#### POST /api/generate/image
Генерирует изображение на основе описания.

**Request:**
```json
{
  "description": "красивый закат над горами",
  "style": "realistic",
  "size": "512x512"
}
```

**Response (200 OK):**
```json
{
  "success": true,
  "image_url": "/generated_images/image_2026_05_24_103000.png",
  "image_file": "image_2026_05_24_103000.png"
}
```

**Поддерживаемые стили:**
- `realistic` — реалистичный
- `cartoon` — мультяшный
- `artistic` — художественный
- `sketch` — эскиз
- `digital_art` — цифровое искусство

---

### 4. История чатов (History API)

#### GET /api/history
Получить всю историю чатов пользователя.

**Response (200 OK):**
```json
{
  "success": true,
  "sessions": [
    {
      "id": "session_123",
      "created_at": "2026-05-24T10:30:00Z",
      "message_count": 15,
      "preview": "Как работает Python?..."
    }
  ],
  "total_sessions": 1
}
```

#### POST /api/history/save
Сохранить текущий чат.

**Request:**
```json
{
  "session_id": "session_123",
  "title": "Обучение Python"
}
```

**Response (200 OK):**
```json
{
  "success": true,
  "message": "Chat saved successfully",
  "saved_at": "2026-05-24T11:30:00Z"
}
```

#### DELETE /api/history/clear
Очистить всю историю.

**Response (200 OK):**
```json
{
  "success": true,
  "message": "History cleared",
  "deleted_sessions": 1
}
```

---

### 5. Управление файлами (File Manager API)

#### POST /api/upload
Загрузить файл на сервер.

**Request (multipart/form-data):**
```
file: <binary file>
folder: uploads
max_size: 50MB
```

**Response (200 OK):**
```json
{
  "success": true,
  "file_name": "document.pdf",
  "file_path": "/uploads/document.pdf",
  "file_size": 1024000,
  "upload_time": "2026-05-24T10:30:00Z"
}
```

#### GET /api/download/<filename>
Скачать файл с сервера.

**Response (200):**
```
Content-Type: application/octet-stream
Content-Disposition: attachment; filename="file.txt"
<binary file content>
```

#### GET /api/files/<folder>
Получить список файлов в папке.

**Response (200 OK):**
```json
{
  "success": true,
  "folder": "uploads",
  "files": [
    {
      "name": "document.pdf",
      "size": 1024000,
      "modified": "2026-05-24T10:30:00Z"
    }
  ]
}
```

---

## 🛠️ Модули и функции

### commands/code_generator.py

```python
def generate_code(description: str, language: str = "python") -> dict:
    """
    Генерирует код на основе описания.
    
    Args:
        description (str): Описание того, что нужно создать
        language (str): Язык программирования
        
    Returns:
        dict: {
            "success": bool,
            "code": str,
            "language": str,
            "file_name": str
        }
    """
    pass

def analyze_code(code: str) -> dict:
    """
    Анализирует существующий код.
    
    Returns:
        dict: {
            "language": str,
            "complexity": float,
            "issues": list,
            "suggestions": list
        }
    """
    pass

def optimize_code(code: str) -> dict:
    """
    Оптимизирует код.
    """
    pass
```

### commands/image_generator.py

```python
def generate_image(description: str, style: str = "realistic") -> dict:
    """
    Генерирует изображение на основе описания.
    
    Args:
        description (str): Описание изображения
        style (str): Стиль (realistic, cartoon, artistic, sketch)
        
    Returns:
        dict: {
            "success": bool,
            "image_url": str,
            "image_file": str
        }
    """
    pass

def enhance_image(image_path: str) -> dict:
    """
    Улучшает качество изображения.
    """
    pass
```

### commands/file_manager.py

```python
def upload_file(file, folder: str = "uploads") -> dict:
    """
    Загружает файл на сервер.
    """
    pass

def download_file(file_path: str) -> bytes:
    """
    Скачивает файл с сервера.
    """
    pass

def list_files(folder: str) -> list:
    """
    Получает список файлов в папке.
    """
    pass

def delete_file(file_path: str) -> dict:
    """
    Удаляет файл.
    """
    pass
```

---

## ⚙️ Конфигурация

### app.py основные параметры

```python
# Flask конфигурация
FLASK_ENV = "development"
FLASK_DEBUG = True
SECRET_KEY = "your-secret-key-here"

# Параметры сервера
HOST = "0.0.0.0"  # Доступен локально и по сети
PORT = 5000
THREADED = True

# Ограничения файлов
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB
ALLOWED_EXTENSIONS = {'txt', 'pdf', 'png', 'jpg', 'jpeg', 'gif', 'doc', 'docx', 'py', 'js', 'html', 'css'}

# ИИ параметры
AI_MODEL = "gpt-4"
MAX_TOKENS = 2000
TEMPERATURE = 0.7
REQUEST_TIMEOUT = 30

# Хранилище данных
HISTORY_FILE = "chat_history.json"
COMMANDS_FILE = "custom_commands.json"
```

### .env файл

```bash
# Flask
FLASK_ENV=development
FLASK_DEBUG=True
SECRET_KEY=super_secret_key_123456

# API Ключи
OPENAI_API_KEY=sk-xxxxxxxx
ANTHROPIC_API_KEY=sk-ant-xxxxxxxx
GOOGLE_API_KEY=xxxxxxxx

# Параметры сервера
HOST=0.0.0.0
PORT=5000

# Размеры файлов (в байтах)
MAX_FILE_SIZE=52428800  # 50MB

# Другие параметры
LOG_LEVEL=INFO
```

---

## 🚀 Развёртывание

### Локальная разработка

```bash
# 1. Клонирование
git clone https://github.com/podshoevbunyod16-sketch/NovaMind.git
cd NovaMind

# 2. Виртуальное окружение
python3 -m venv venv
source venv/bin/activate

# 3. Установка зависимостей
pip install -r requirements.txt

# 4. Запуск
python app.py
```

### На продакшене (Gunicorn)

```bash
# Установка Gunicorn
pip install gunicorn

# Запуск с 4 рабочих процессов
gunicorn -w 4 -b 0.0.0.0:5000 app:app

# С логированием
gunicorn -w 4 -b 0.0.0.0:5000 app:app --access-logfile logs/access.log
```

### На Heroku

```bash
# 1. Создать Procfile
echo "web: gunicorn app:app" > Procfile

# 2. Создать requirements.txt
pip freeze > requirements.txt

# 3. Инициализировать Git
git init
git add .
git commit -m "Initial commit"

# 4. Создать и развернуть приложение
heroku create novamind-app
git push heroku main
```

### На Docker

```dockerfile
FROM python:3.9-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 5000
CMD ["gunicorn", "-w", "4", "-b", "0.0.0.0:5000", "app:app"]
```

```bash
# Сборка и запуск
docker build -t novamind .
docker run -p 5000:5000 novamind
```

---

## 🐍 Python версии

Поддерживаемые версии Python:
- ✅ Python 3.8
- ✅ Python 3.9
- ✅ Python 3.10
- ✅ Python 3.11
- ✅ Python 3.12

Проверить версию:
```bash
python --version
python3 --version
```

---

## 🔍 Отладка

### Включение debug режима

```python
# app.py
if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
```

### Логирование

```python
import logging

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

logger.debug("Debug сообщение")
logger.info("Информационное сообщение")
logger.warning("Предупреждение")
logger.error("Ошибка")
```

### Тестирование API

```bash
# Тест GET запроса
curl http://localhost:5000/api/history

# Тест POST запроса с JSON
curl -X POST http://localhost:5000/api/chat \
  -H "Content-Type: application/json" \
  -d '{"message":"Привет"}'

# Использование Python requests
python
>>> import requests
>>> requests.post('http://localhost:5000/api/chat', json={'message': 'Привет'})
```

---

## ❓ FAQ

### Q: Как изменить порт приложения?
**A:** Отредактируй `app.py` или запусти:
```bash
python app.py --port 8000
```

### Q: Как сохранить API ключи безопасно?
**A:** Используй файл `.env` и добавь его в `.gitignore`:
```bash
echo ".env" >> .gitignore
```

### Q: Как увеличить максимальный размер файла?
**A:** В `app.py`:
```python
MAX_FILE_SIZE = 200 * 1024 * 1024  # 200 MB
```

### Q: На Termux не запускается?
**A:** Убедись что установлены:
```bash
pkg install python
pkg install git
pip install -r requirements.txt
```

### Q: Как сбросить историю чатов?
**A:** Удали `chat_history.json`:
```bash
rm chat_history.json
```

### Q: Как использовать свою ИИ модель?
**A:** Отредактируй `app.py`:
```python
# Вместо OpenAI используй Anthropic
from anthropic import Anthropic
client = Anthropic()
```

### Q: Ошибка SSL при работе с API?
**A:** Отключи проверку SSL (не для продакшена!):
```python
import ssl
ssl._create_default_https_context = ssl._create_unverified_context
```

---

## 📞 Получить помощь

- 📖 [Читай README.md](./README.md)
- 🐛 [Открой Issue на GitHub](https://github.com/podshoevbunyod16-sketch/NovaMind/issues)
- 💬 [Обсуди в Discussion](https://github.com/podshoevbunyod16-sketch/NovaMind/discussions)

---

**Версия документации:** 1.0.0  
**Последнее обновление:** май 2026  
**Статус:** Development
