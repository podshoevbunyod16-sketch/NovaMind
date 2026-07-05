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

# API ключи (если используются)
OPENAI_API_KEY=your_key_here
ANTHROPIC_API_KEY=your_key_here

# Параметры сервера
HOST=0.0.0.0
PORT=5000
```

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
