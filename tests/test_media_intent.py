"""Когда запрос — это генерация медиа, а когда — работа с кодом.

Два случая из журнала с телефона:

* «Запусти этот код в Linux-окружении и покажи, что он выводит» + код turtle →
  NovaMind сгенерировал картинку Pollinations и ничего не запустил (слово
  «покажи» считалось генерацией, а `draw_square` внутри кода — «нарисуй»);
* «создай приложение с красивой анимации» → ушло в генерацию видео из-за слова
  «анимация».
"""

import pytest

import routes.agent as agent_routes

TURTLE_REQUEST = """Запусти этот код в Linux-окружении и покажи, что он выводит. Если есть ошибки — исправь и запусти снова.

```python
import turtle
screen = turtle.Screen()

def draw_square(size):
    for _ in range(4):
        pen.forward(size)
        pen.right(90)

while True:
    draw_square(100)
    screen.update()
```
"""


@pytest.mark.parametrize("text", [
    TURTLE_REQUEST,
    "Запусти этот код в Linux-окружении и покажи, что он выводит",
    "Запусти мой код и покажи вывод",
    "покажи, что выводит эта программа",
    "исправь ошибку в app.py",
    "запусти тесты",
    "Создай приложение с красивой анимации",
    "создай приложение с красивой анимацией на python",
    "сделай сайт с анимацией",
    "напиши игру с красивой анимацией",
    "Создай интернет-магазин на Flask",
    "Привет, как дела?",
    "https://github.com/podshoevbunyod16-sketch/NovaMind дай мне его структура в виде дерево",
    "https://novamind.onrender.com проверь сайт",
    "задеплой сервер на render",
    "открой sketch.py и разбери функции",
    "покажи дерево файлов репозитория",
])
def test_code_requests_are_not_media(text):
    assert agent_routes.requested_media_kind(text) is None


@pytest.mark.parametrize("text,kind", [
    ("нарисуй кота", "image"),
    ("нарисуй картинку заката", "image"),
    ("покажи изображение гор", "image"),
    ("сгенерируй картинку с драконом", "image"),
    ("Создай красивую анимацию заката", "video"),
    ("сгенерируй видео с волнами", "video"),
    ("озвучь приветствие", "audio"),
    ("сделай аудио с приветствием", "audio"),
    ("прочитай вслух этот текст", "audio"),
])
def test_explicit_media_requests_still_work(text, kind):
    assert agent_routes.requested_media_kind(text) == kind


def test_code_inside_fence_does_not_trigger_drawing():
    """`draw_square(`, `render()`, `show()` в коде — не просьба нарисовать."""
    for call in ("draw_square(100)", "render()", "show()", "drawSq(1)"):
        assert agent_routes.requested_media_kind(f"запусти код: {call}") is None, call


def test_media_request_does_not_run_the_code_agent(admin_client, sandbox, monkeypatch):
    """«Создай приложение с анимацией» — шаги агента, а не картинка."""
    from tests.agent_helpers import events_of, ndjson, script

    script(monkeypatch, ["Готово: приложение создано."])
    events = ndjson(admin_client.post(
        "/api/agent/stream", json={"message": "Создай приложение с красивой анимации"}))
    assert events_of(events, "media") == []
    assert events_of(events, "stage")[0]["scene"] != "media"
    assert "приложение создано" in str(events_of(events, "result")[0]["reply"])
