# ⚡ NovaMind - Быстрый Старт (5 минут)

Самый быстрый способ запустить приложение.

---

## 🚀 Установка за 30 секунд

### Вариант 1: Linux/macOS

```bash
git clone https://github.com/podshoevbunyod16-sketch/NovaMind.git
cd NovaMind
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python app.py
```

Затем открой: **http://localhost:5000**

### Вариант 2: Windows

```cmd
git clone https://github.com/podshoevbunyod16-sketch/NovaMind.git
cd NovaMind
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

Затем открой: **http://localhost:5000**

### Вариант 3: Android (Termux)

```bash
pkg update && pkg upgrade -y
pkg install python git
git clone https://github.com/podshoevbunyod16-sketch/NovaMind.git
cd NovaMind
pip install -r requirements.txt
python app.py
```

Затем открой браузер: **http://127.0.0.1:5000**

📖 [Полное руководство для Termux](./INSTALL_TERMUX.md)

---

## 📝 Первый тест

После запуска приложения (ты увидишь сообщение `Running on http://127.0.0.1:5000`):

1. Открой браузер
2. Введи: `http://localhost:5000`
3. Попробуй первую команду:
   ```
   Привет! Кто ты?
   ```

**Готово!** ✅

---

## 🎮 Примеры использования

### Чат с ИИ
```
Как работает Python?
```

### Генерация кода
```
@code напиши функцию для сортировки массива на Python
```

### Генерация изображения
```
@image красивый закат над морем
```

### Просмотр истории
```
/history
```

### Очистить чат
```
/clear
```

---

## 🔧 Основные файлы

| Файл | Что это |
|------|--------|
| `app.py` | Главное приложение |
| `requirements.txt` | Зависимости |
| `templates/` | HTML страницы |
| `static/` | CSS, JavaScript, медиа |
| `commands/` | Модули команд |
| `chat_history.json` | История чатов |

---

## 📊 API примеры (для продвинутых)

### Отправить сообщение
```bash
curl -X POST http://localhost:5000/api/chat \
  -H "Content-Type: application/json" \
  -d '{"message":"Привет"}'
```

### Сгенерировать код
```bash
curl -X POST http://localhost:5000/api/generate/code \
  -H "Content-Type: application/json" \
  -d '{
    "description":"Напиши калькулятор",
    "language":"python"
  }'
```

### Получить историю
```bash
curl http://localhost:5000/api/history
```

---

## 🐛 Если что-то не работает

### Ошибка при установке
```bash
# Переустанови Python
pip install --upgrade pip
pip install -r requirements.txt
```

### Порт 5000 занят
```bash
# Используй другой порт
python app.py --port 8000
```

### Модули не установились
```bash
# Проверь версию Python
python --version  # должно быть 3.8+

# Переустанови всё с нуля
pip install --upgrade -r requirements.txt
```

---

## 📚 Дальнейшее обучение

1. **Читай README.md** — полная информация про проект
2. **Смотри DOCUMENTATION.md** — техническая документация
3. **Исправляй баги** — открывай Issues на GitHub
4. **Добавляй функции** — делай Pull Requests

---

## 💾 Где сохраняются данные

```
NovaMind/
├── chat_history.json      ← История чатов
├── custom_commands.json   ← Твои команды
├── uploads/               ← Загруженные файлы
├── generated_codes/       ← Сгенерированный код
└── generated_images/      ← Сгенерированные картинки
```

---

## 🌐 Доступ с другого компьютера

Если хочешь открыть приложение с другого компьютера в сети:

```bash
# Узнай IP адрес
ipconfig  # Windows
ifconfig  # Linux/macOS

# Запусти приложение со всеми интерфейсами
python app.py --host 0.0.0.0

# На другом компьютере открой
http://192.168.1.100:5000  # замени на свой IP
```

---

## 🔒 Важно для безопасности

⚠️ **Не коммитай:**
- `.env` файл с API ключами
- `.ssh` папку
- `chat_history.json` если содержит личные данные
- `.cache/` и `__pycache__/`

Используй `.gitignore`:
```bash
echo ".env .ssh chat_history.json" >> .gitignore
```

---

## 📱 На Android (Termux)

```bash
# Обновить пакеты
pkg update

# Установить зависимости
pip install -r requirements.txt

# Запустить
python app.py

# В браузере открыть
http://127.0.0.1:5000
```

Больше деталей: [INSTALL_TERMUX.md](./INSTALL_TERMUX.md)

---

## ✨ Советы для быстрого старта

✅ Используй виртуальное окружение (`venv`)  
✅ Читай сообщения об ошибках  
✅ Проверь что используешь Python 3.8+  
✅ На Termux установи компилятор: `pkg install clang`  
✅ Если зависает - перезагрузи Termux  

---

## 🎯 Что дальше?

После успешного запуска:

1. **Модифицируй интерфейс** — редактируй `templates/index.html`
2. **Добавляй команды** — создавай новые файлы в `commands/`
3. **Интегрируй свой ИИ** — замени OpenAI на Anthropic/Gemini
4. **Развёртывай** — запусти на сервере (Heroku, DigitalOcean и т.д.)

---

## 📞 Помощь

- 📖 [README.md](./README.md) — полная документация
- 📚 [DOCUMENTATION.md](./DOCUMENTATION.md) — техническая информация
- 🐛 [GitHub Issues](https://github.com/podshoevbunyod16-sketch/NovaMind/issues) — сообщи об ошибке
- 📱 [Для Termux](./INSTALL_TERMUX.md) — специальное руководство

---

**⏱️ Время читания: 5 минут  
🚀 Время установки: 2-5 минут (в зависимости от интернета)  
✅ Сложность: Легко**

🎉 **Успехов в разработке!**
