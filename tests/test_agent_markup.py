"""Модель отвечает тегами `<bash>` вместо блоков — движок должен их понять.

Реальный случай с телефона: на просьбу «создай интернет-магазин» модель
ответила `<bash>pip install flask_sqlalchemy</bash>`, движок команду не
выполнил, а теги и придуманный «вывод» попали в ответ пользователю. Здесь
проверяется, что такие ответы нормализуются, выполняются и не протекают в
текст для пользователя.
"""

import json

from agenticseek.core.markup import has_legacy_tags, normalize_answer, sanitize_answer
from tests.agent_helpers import answer_of, events_of, model_text, ndjson, script


def _post(client, message):
    return ndjson(client.post("/api/agent/stream", json={"message": message}))


# ══════════════ чистые функции ══════════════

def test_legacy_bash_tags_become_blocks():
    text = "Ставлю пакет.<bash>\npip install flask\n</bash>Готово."
    normalized = normalize_answer(text)
    assert "```bash\npip install flask\n```" in normalized
    assert "<bash>" not in normalized


def test_invented_output_is_dropped():
    """`<bash code_output>` — выдумка модели, показывать её нельзя."""
    text = ("<bash>pip install x</bash>"
            "<bash code_output>Successfully installed x-1.0</bash_output>"
            "Готово.")
    assert "Successfully installed" not in sanitize_answer(text)
    assert "Successfully installed" not in normalize_answer(text)


def test_plain_answers_are_untouched():
    plain = "```bash\nls -la\n```\nСписок файлов выше."
    assert normalize_answer(plain) == plain
    assert sanitize_answer(plain) == plain
    assert has_legacy_tags(plain) is False


def test_tags_are_on_their_own_lines():
    """Склейка «Ставлю пакет.```bash» ломает разбор блока."""
    normalized = normalize_answer("Ставлю.<bash>\nls\n</bash>Готово.")
    assert "Ставлю.\n```bash\nls\n```\nГотово." == normalized.strip()


# ══════════════ в потоке агента ══════════════

def test_legacy_bash_tag_actually_runs(admin_client, sandbox, monkeypatch):
    seen = []
    script(monkeypatch, [
        "Записываю файл.<bash>\nprintf 'shop' > legacy.txt\n</bash>",
        "Готово.",
    ], seen)

    events = _post(admin_client, "создай сайт на flask")

    tool = events_of(events, "tool")[0]
    assert tool["name"] == "bash" and "printf" in tool["command"]
    assert (sandbox / "legacy.txt").read_text(encoding="utf-8") == "shop"
    # Пользователь не видит XML-протокол.
    assert "<bash" not in answer_of(events)


def test_invented_output_never_reaches_the_user(admin_client, sandbox, monkeypatch):
    script(monkeypatch, [
        'Устанавливаю.<bash>\necho real\n</bash>'
        '<bash code_output>Successfully installed flask</bash_output>',
        "Готово: пакет установлен.",
    ])

    events = _post(admin_client, "поставь flask")

    reply = answer_of(events)
    assert "Successfully installed" not in reply
    assert "bash_output" not in reply and "<bash" not in reply
    assert events_of(events, "tool")[0]["name"] == "bash"


def test_written_python_file_is_not_executed(admin_client, sandbox, monkeypatch):
    """`python:app.py` — запись файла. Запуск отдельным блоком, иначе сервер висит."""
    answered = []
    script(monkeypatch, [
        "```python:app.py\n"
        "from flask import Flask\napp = Flask(__name__)\n"
        "print('этот print не должен выполниться')\n"
        "if __name__ == '__main__':\n    app.run()\n"
        "```",
        "Создал app.py",
    ], answered)

    events = _post(admin_client, "создай сайт на flask")

    assert (sandbox / "app.py").is_file()
    tool = events_of(events, "tool")[0]
    assert "не выполнялся" in tool["output"]
    # Приложение не запускалось: сервер не занимал терминал.
    assert "этот print не должен выполниться" not in tool["output"]
    assert "test client" in model_text(answered, 1)


def test_empty_python_output_gets_a_hint(admin_client, sandbox, monkeypatch):
    """Блок из одних import — «успех» без результата; модель должна узнать почему."""
    answered = []
    script(monkeypatch, [
        "```python\nfrom flask import Flask, render_template\n```",
        "Готово.",
    ], answered)

    _post(admin_client, "создай сайт на flask")

    feedback = model_text(answered, 1)
    assert "ничего не вывел" in feedback
    assert "python:app.py" in feedback
