# YouTubegopro
# 🧠 NovaMind - AI-Powered Web Assistant

![Python](https://img.shields.io/badge/Python-3.8+-blue)
![Flask](https://img.shields.io/badge/Flask-2.0+-green)
![License](https://img.shields.io/badge/License-MIT-yellow)
![Status](https://img.shields.io/badge/Status-Development-orange)

Умный веб-помощник на базе ИИ с возможностью генерации кода, изображений и управления файлами. Работает на локальном сервере с полной историей чата и кастомизацией команд.

---

## ✨ Основные возможности

- 🤖 **AI Чат-интерфейс** — общение с ИИ-помощником в браузере
- 💻 **Генерация кода** — создание кода на разных языках программирования
- 🖼️ **Генерация изображений** — создание визуального контента
- 💾 **История чатов** — сохранение и загрузка предыдущих разговоров
- ⚙️ **Пользовательские команды** — создание собственных команд и скриптов
- 📁 **Управление файлами** — загрузка, скачивание, сохранение результатов
- 🌍 **Веб-интерфейс** — простой и интуитивный дизайн

---

## 📋 Требования

- **Python**: 3.8 или выше
- **ОС**: Linux, macOS, Windows, Android (Termux)
- **Браузер**: Chrome, Firefox, Safari, Edge
- **Оперативная память**: 512 MB минимум
- **Интернет**: Для работы с API ИИ

---

## 🚀 Установка

### 1️⃣ На Linux/macOS/Windows

```bash
# Клонирование репозитория
git clone https://github.com/podshoevbunyod16-sketch/NovaMind.git
cd NovaMind

# Создание виртуального окружения (рекомендуется)
python3 -m venv venv
source venv/bin/activate  # Linux/macOS
# или для Windows:
# venv\Scripts\activate

# Установка зависимостей
pip install -r requirements.txt

# Запуск приложения
python app.py
```

### 2️⃣ На Android (Termux)

```bash
# Обновление пакетов Termux
pkg update && pkg upgrade

# Установка Python и Git
pkg install python git

# Клонирование репозитория
git clone https://github.com/podshoevbunyod16-sketch/NovaMind.git
cd NovaMind

# Установка зависимостей
pip install -r requirements.txt

# Запуск приложения
python app.py
```

### 3️⃣ Первый запуск

После выполнения команды `python app.py` увидишь:

```
 * Running on http://127.0.0.1:5000
 * Press CTRL+C to quit
```

Открой браузер и перейди на **http://localhost:5000** или **http://127.0.0.1:5000**

---

## 💻 Использование

### Основной интерфейс

1. **Чат** — введи вопрос и получи ответ от ИИ
2. **Генерация кода** — напиши `@code` + описание
3. **Генерация изображений** — напиши `@image` + описание
4. **Сохранение** — нажми кнопку "Сохранить" для скачивания

Голосовой ввод запускается кнопкой микрофона в поле сообщения. Распознавание работает через Web Speech API браузера: промежуточный текст появляется сразу, финальные фрагменты накапливаются до окончания речи или повторного нажатия кнопки и остаются в поле для проверки перед отправкой.

Обычные ответы чата выводятся потоково через endpoint `/send_stream`: текст появляется в сообщении по мере генерации OpenAI-compatible сервером. Для потоковой выдачи сервер должен поддерживать стандартный OpenAI SSE-формат (`data: {...}` и `data: [DONE]`).

### Примеры команд

```
# Простой вопрос
Как работает Python?

# Генерация кода
@code напиши калькулятор на Python с GUI

# Генерация изображения
@image красивая природа закат

# Получение истории
/history

# Очистка чата
/clear
```

---

## 📁 Структура проекта

```
NovaMind/
├── app.py                    # Главное приложение Flask
├── requirements.txt          # Зависимости проекта
├── README.md                 # Этот файл
├── DOCUMENTATION.md          # Полная техническая документация
│
├── templates/                # HTML шаблоны
│   ├── index.html           # Главная страница
│   ├── chat.html            # Чат-интерфейс
│   └── settings.html        # Настройки
│
├── static/                   # Статические файлы
│   ├── css/
│   │   └── style.css        # Стили приложения
│   ├── js/
│   │   └── script.js        # JavaScript функции
│   └── images/              # Изображения и иконки
│
├── uploads/                  # Загруженные пользователем файлы
├── downloads/                # Скачиваемые файлы
├── generated_codes/          # Сгенерированный код
├── generated_images/         # Сгенерированные изображения
├── saved_codes/              # Сохранённый пользователем код
│
├── commands/                 # Модули команд
│   ├── code_generator.py    # Генератор кода
│   ├── image_generator.py   # Генератор изображений
│   └── file_manager.py      # Управление файлами
│
├── chat_history.json         # История чатов (JSON)
├── custom_commands.json      # Пользовательские команды
└── .gitignore              # Игнорируемые файлы для Git
```

---

## ⚙️ Конфигурация

### Переменные окружения

Создай файл `.env` в корне проекта:

```env
# Flask
FLASK_ENV=development
FLASK_DEBUG=True
SECRET_KEY=your_secret_key_here

# Cloud API ключи (необязательно)
# GROQ_API_KEY=your_key_here
# CEREBRAS_API_KEY=your_key_here
# OPENROUTER_API_KEY=your_key_here

# OpenAI-compatible сервер (Ollama, LM Studio, vLLM, LocalAI или удалённый URL)
# Если cloud-ключей нет, приложение использует этот endpoint.
OPENAI_BASE_URL=http://127.0.0.1:11434/v1
OPENAI_API_KEY=
OPENAI_MODEL=llama3.2

# Можно использовать альтернативные имена:
# AI_BASE_URL=https://your-host.example.com/v1
# AI_API_KEY=your_key
# AI_MODEL=your_model
# LOCAL_LLM_URL=http://127.0.0.1:1234/v1
# LOCAL_LLM_MODEL=local-model

# Параметры сервера
HOST=0.0.0.0
PORT=5000
```

Если `GROQ_API_KEY`, `CEREBRAS_API_KEY` и `OPENROUTER_API_KEY` не заданы, чат автоматически работает через `OPENAI_BASE_URL`. Для Ollama достаточно запустить модель локально и оставить URL по умолчанию. Для удалённого OpenAI-compatible сервера укажи его базовый URL, например `https://your-host.example.com/v1`, и при необходимости API-ключ. Модель выбирается только через `OPENAI_MODEL`/`AI_MODEL` и больше не выбирается в главном экране чата.

### Настройки провайдеров и моделей

Окно настроек доступно по адресу `/settings`, через кнопку-шестерёнку в верхней панели или через аватар пользователя. В нём можно выбрать OpenRouter, Groq, Cerebras или OpenAI-compatible/local endpoint. Для OpenRouter, Groq и Cerebras каталог моделей запрашивается напрямую backend через ключ из `.env`; ключи в браузер не передаются.

Каталог поддерживает фильтры по бесплатным/платным моделям, поиску, модальности text/vision, минимальному контексту, а также сортировку по рекомендуемым моделям, контексту, размеру весов, стоимости и дате. Выбранные провайдер и модель сохраняются в локальном `runtime_settings.json`, который не попадает в Git.

### Аудио, видео и изображения

В меню чата доступны загрузка аудио и видео, а также Media Studio. Аудиофайлы можно расшифровывать через OpenAI/Groq-compatible Whisper endpoint, а текст — озвучивать через OpenAI-compatible TTS. Генерация изображений использует настроенный `IMAGE_GENERATION_URL`, а если он не задан — бесплатный Pollinations.ai. Для видео предусмотрен универсальный `VIDEO_API_URL`, потому что у разных video-провайдеров отличаются формат запроса, модели и асинхронная обработка.

Основные API-маршруты: `POST /api/media/upload`, `POST /api/media/transcribe`, `POST /api/media/tts`, `POST /api/media/image`, `POST /api/media/video` и `GET /media/<filename>`. Для конкретного видео-провайдера укажи `VIDEO_API_URL`, `VIDEO_API_KEY` и `VIDEO_MODEL` в `.env`.

### Максимальный размер файла

В `app.py`:
```python
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB
```

---

## 📊 API Эндпоинты

| Метод | URL | Описание |
|-------|-----|---------|
| GET | `/` | Главная страница |
| POST | `/api/chat` | Отправка сообщения в чат |
| POST | `/api/generate/code` | Генерация кода |
| POST | `/api/generate/image` | Генерация изображения |
| GET | `/api/history` | Получение истории чатов |
| POST | `/api/history/save` | Сохранение чата |
| DELETE | `/api/history/clear` | Очистка истории |
| POST | `/api/upload` | Загрузка файла |
| GET | `/api/download/<filename>` | Скачивание файла |

---

## 🔧 Разработка

### Добавление новой команды

1. Создай новый файл в папке `commands/`:
```python
# commands/my_command.py
def execute(user_input):
    return "Результат выполнения"
```

2. Зарегистрируй в `app.py`:
```python
from commands import my_command
app.route('/api/my-command', methods=['POST'])(my_command.execute)
```

### Запуск в режиме отладки

```bash
export FLASK_ENV=development
export FLASK_DEBUG=True
python app.py
```

---

## 🐛 Решение проблем

### Ошибка: "Port 5000 is already in use"
```bash
# Найти процесс
lsof -i :5000

# Или использовать другой порт
python app.py --port 5001
```

### Ошибка при импорте модулей
```bash
# Убедись что виртуальное окружение активировано
source venv/bin/activate

# Переустанови зависимости
pip install --upgrade -r requirements.txt
```

### На Termux файлы не сохраняются
```bash
# Проверь права доступа
chmod -R 755 NovaMind/
chmod -R 755 uploads/ downloads/ generated_codes/ generated_images/
```

---

## 🔐 Безопасность

⚠️ **ВАЖНО:**
- Никогда не коммитай `.env` файл с API ключами
- Не делись ссылкой на localhost публично
- Используй HTTPS в продакшене
- Регулярно обновляй зависимости: `pip install --upgrade -r requirements.txt`

---

## 📝 Лицензия

MIT License - Используй свободно в личных и коммерческих проектах.

---

## 👨‍💻 Автор

**Bunyod** — Студент Таджикского технического университета имени М.С. Осими, факультет Информационных технологий.

- GitHub: [@podshoevbunyod16-sketch](https://github.com/podshoevbunyod16-sketch)
- Специализация: Разработка ПО, ИИ, Веб-приложения

---

## 🤝 Поддержка

Если нашёл баг или у тебя есть идея улучшения:
1. Открой [Issue](https://github.com/podshoevbunyod16-sketch/NovaMind/issues)
2. Опиши проблему детально
3. Прикрепи скриншоты если нужно

---

## 📚 Дополнительные ресурсы

- [Полная документация](./DOCUMENTATION.md)
- [Flask документация](https://flask.palletsprojects.com/)
- [Python документация](https://docs.python.org/3/)
- [API документация](./docs/api.md)

---

**Последнее обновление:** май 2026  
**Версия:** 1.0.0-beta

🌟 Если проект понравился, поставь звезду на GitHub!
