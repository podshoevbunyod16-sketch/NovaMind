# 🤝 Как внести вклад в NovaMind

Спасибо что хочешь помочь с развитием проекта! 🎉

Вот как ты можешь помочь:

---

## 🐛 Нашёл баг?

1. **Проверь** что это не дублирующаяся ошибка — посмотри [Issues](https://github.com/podshoevbunyod16-sketch/NovaMind/issues)

2. **Создай новый Issue** с описанием:
   ```
   Название: Описание проблемы в одну строку
   
   Описание:
   - Что происходит?
   - Что должно происходить?
   - Как повторить ошибку? (шаги)
   - Ошибка (полный текст):
   ```

3. **Включи информацию:**
   ```
   Python версия: 3.9
   ОС: Linux / Windows / macOS / Android (Termux)
   Браузер: Chrome / Firefox / Safari
   Версия приложения: 1.0.0
   ```

---

## 💡 Есть идея для улучшения?

1. **Открой Discussion** — обсудим идею
2. **Или создай Issue** с тегом `enhancement`
3. **Опиши:**
   - Какая проблема это решает?
   - Как это должно работать?
   - Примеры использования

---

## 🔧 Хочешь сам написать код?

### 1️⃣ Подготовка

```bash
# Клонируй репозиторий
git clone https://github.com/podshoevbunyod16-sketch/NovaMind.git
cd NovaMind

# Создай свою ветку
git checkout -b feature/название-фичи
# или для багфиксов
git checkout -b fix/название-бага
```

### 2️⃣ Разработка

**Правила кода:**

✅ **Используй Python 3.8+**
```python
# Хорошо
def process_message(user_input: str) -> str:
    """Обрабатывает сообщение пользователя."""
    return user_input.strip().lower()

# Плохо
def process(x):
    return x.strip().lower()
```

✅ **Добавляй docstrings**
```python
def generate_code(description: str, language: str = "python") -> dict:
    """
    Генерирует код на основе описания.
    
    Args:
        description: Описание того, что нужно создать
        language: Язык программирования (по умолчанию Python)
        
    Returns:
        Словарь с сгенерированным кодом:
        {
            "success": bool,
            "code": str,
            "language": str
        }
    """
    pass
```

✅ **Форматируй код с Black**
```bash
pip install black
black app.py commands/
```

✅ **Проверяй типы с Mypy**
```bash
pip install mypy
mypy app.py
```

✅ **Тестируй код**
```bash
pip install pytest
pytest tests/
```

### 3️⃣ Тестирование

```bash
# Локально всё тестируем
python app.py

# Проверяй в браузере
http://localhost:5000

# Тестируй API
curl -X POST http://localhost:5000/api/chat -d '{"message":"test"}'
```

### 4️⃣ Комит

```bash
# Читаемые коммиты
git add .
git commit -m "feat: добавил генерацию изображений"
# или
git commit -m "fix: исправил баг с сохранением истории"
```

**Форматы коммитов:**
- `feat:` — новая функция
- `fix:` — исправление бага
- `docs:` — документация
- `style:` — форматирование кода (без логики)
- `refactor:` — переработка кода
- `test:` — тесты
- `chore:` — обновление зависимостей

### 5️⃣ Push и Pull Request

```bash
git push origin feature/название-фичи
```

Затем на GitHub:
1. Нажми "Create Pull Request"
2. Опиши что ты сделал
3. Линкани на Issue если есть

---

## 📋 Чек-лист для Pull Request

- [ ] Код работает без ошибок
- [ ] Добавлены docstrings
- [ ] Код отформатирован (Black)
- [ ] Нет дублирования функций
- [ ] Обновлена документация
- [ ] Добавлены примеры использования
- [ ] Протестировано локально
- [ ] Коммиты с понятными сообщениями

---

## 📚 Структура проекта для участников

```
NovaMind/
├── app.py                  ← Главное приложение
├── commands/               ← Модули команд (сюда добавляй новые команды)
│   ├── code_generator.py
│   ├── image_generator.py
│   └── file_manager.py
├── templates/              ← HTML (улучшай интерфейс)
├── static/                 ← CSS, JS (стили и логика)
├── tests/                  ← Тесты (добавляй тесты для новых функций)
├── docs/                   ← Документация
└── requirements.txt        ← Зависимости
```

---

## 🎯 Популярные улучшения для начинающих

### 1. Улучшить интерфейс

**Файл:** `templates/index.html`, `static/css/style.css`

- Добавить тёмный режим
- Улучшить мобильный дизайн
- Добавить больше кнопок и иконок

### 2. Добавить новую команду

**Файл:** `commands/new_command.py`

```python
def translate_text(text: str, source: str = "ru", target: str = "en") -> dict:
    """Переводит текст на другой язык."""
    # Реализация
    return {"success": True, "translation": "..."}
```

### 3. Улучшить документацию

**Файлы:** `README.md`, `DOCUMENTATION.md`

- Исправить ошибки
- Добавить примеры
- Улучшить структуру

### 4. Добавить тесты

**Файл:** `tests/test_code_generator.py`

```python
import pytest
from commands.code_generator import generate_code

def test_generate_code_python():
    result = generate_code("напиши привет мир", "python")
    assert result["success"] == True
    assert "python" in result["language"].lower()
```

### 5. Оптимизировать производительность

- Кэширование результатов
- Ускорение загрузки страницы
- Оптимизация памяти

---

## 🚫 Что НЕ нужно делать

❌ **Не коммитить:**
- `.env` файлы с API ключами
- `.ssh` папку
- `__pycache__/`, `.pytest_cache/`
- Большие бинарные файлы

❌ **Не делать:**
- Переписывать весь код без обсуждения
- Добавлять сомнительные зависимости
- Менять лицензию или автора
- Коммитить к чужим веткам

---

## 📝 Типы улучшений и как их добавлять

### Новая ИИ модель

```python
# commands/llm_integration.py
from anthropic import Anthropic

def chat_with_claude(message: str) -> dict:
    """Использует Claude вместо OpenAI."""
    client = Anthropic()
    response = client.messages.create(
        model="claude-3-sonnet-20240229",
        max_tokens=2000,
        messages=[{"role": "user", "content": message}]
    )
    return {"success": True, "response": response.content[0].text}
```

### Новое хранилище данных

```python
# commands/database.py
import sqlite3

class ChatDatabase:
    def __init__(self, db_name: str = "chat.db"):
        self.conn = sqlite3.connect(db_name)
        self.cursor = self.conn.cursor()
    
    def save_message(self, user_id: str, message: str, role: str):
        """Сохраняет сообщение в БД."""
        self.cursor.execute(
            "INSERT INTO messages VALUES (?, ?, ?)",
            (user_id, message, role)
        )
        self.conn.commit()
```

---

## 🧪 Тестирование своего кода

```bash
# Установи pytest
pip install pytest pytest-cov

# Напиши тесты
# tests/test_my_feature.py
def test_my_function():
    from commands.my_module import my_function
    result = my_function("test")
    assert result == "expected"

# Запусти тесты
pytest

# С покрытием
pytest --cov=commands --cov-report=html
```

---

## 📊 Гайдлайны кода

### Размер функции
- ❌ Функция больше 50 строк? Разбей на несколько
- ✅ Одна функция = одна ответственность

### Имена переменных
- ❌ `x`, `y`, `tmp`
- ✅ `user_message`, `generated_code`, `api_response`

### Комментарии
```python
# Плохо: очевидные комменты
x = 5  # Присваиваем 5 к x

# Хорошо: объясняем почему
TIMEOUT_SECONDS = 30  # Timeout для API запросов, некоторые модели медленные
```

---

## 🐍 Специфика для разработки на Termux

Если разработчик использует Android (Termux):

```bash
# Установка компилятора
pkg install clang

# Установка всего необходимого
pkg install python git build-essential

# Виртуальное окружение на Termux
python -m venv venv
source venv/bin/activate

# Если медленно компилируется
# Отключи оптимизацию: CFLAGS="-O0" pip install ...
```

---

## 🎓 Полезные ресурсы

- [Flask документация](https://flask.palletsprojects.com/)
- [Python best practices](https://pep8.org/)
- [Git документация](https://git-scm.com/doc)
- [GitHub Гайд](https://guides.github.com/)

---

## 👥 Процесс review

1. **Я посмотрю** твой Pull Request
2. **Могу попросить** изменения
3. **После согласова́ния** мержу в main ветку
4. **Спасибо!** Ты в списке contributors

---

## 🎉 Спасибо!

Твой вклад помогает проекту расти! 🚀

**Вопросы?** Открой Issue или напиши в Discussions.

---

## 📞 Контакты

- **GitHub Issues:** Для багов и фичей
- **GitHub Discussions:** Для общих вопросов
- **Pull Requests:** Для своего кода

---

**Версия:** 1.0  
**Статус:** Открыт для участников  
🌟 **Спасибо за помощь в развитии NovaMind!**
