"""Сквозная проверка агентского конвейера: «модель» строит магазин, он работает.

Здесь проверяется не модель, а путь, которым проходит её ответ:

1. ответ модели — блоки ```` ```python:shop/app.py ````, ```` ```python:shop/templates/base.html ````;
2. движок пишет файлы в рабочую папку (и **не запускает** их: `app.run()` не
   должен занимать терминал);
3. следующий блок ```bash``` запускает проверку в песочнице;
4. приложение из этих файлов действительно отвечает на запросы.

Источниками файлов служит `examples/flask_shop` — тот же магазин, что проверяют
`tests/test_flask_shop.py`. Так тест ловит регрессии конвейера (пути, запись,
запуск, таймауты), не завися от живой модели.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

import routes.terminal as term_routes
from agenticseek.core import workspace as workspace_module
from agenticseek.core.coder_agent import CoderAgent
from agenticseek.core.prompt_paths import prompt_path
from agenticseek.tools import sandbox

SHOP_DIR = Path(__file__).resolve().parent.parent / "examples" / "flask_shop"

SMOKE = '''"""Проверка магазина без запуска сервера."""
import sys
sys.path.insert(0, "shop")

from app import create_app

client = create_app("shop/data/smoke.db").test_client()

assert client.get("/").status_code == 200, "каталог"
assert b"\\xd0\\x9a\\xd0\\xbe\\xd1\\x84\\xd0\\xb5" in client.get("/").data, "товар на витрине"
assert client.get("/healthz").get_json()["products"] >= 3, "база"

client.post("/cart/add/1", data={"quantity": 2})
assert b"1 580" in client.get("/cart").data, "корзина и сумма"

order = client.post("/checkout", data={"customer": "Тест", "phone": "1", "address": "2"})
assert order.status_code == 302 and "/order/" in order.headers["Location"], "заказ"
assert b"\\xd0\\x97\\xd0\\xb0\\xd0\\xba\\xd0\\xb0\\xd0\\xb7" in client.get(order.headers["Location"]).data

assert client.get("/admin").status_code == 403, "админка закрыта"

print("SMOKE OK")
'''


@pytest.fixture()
def pipeline(tmp_path, monkeypatch):
    """Рабочая папка песочницы + агент с настоящими инструментами."""
    work = tmp_path / "ws"
    work.mkdir()
    monkeied = {
        "TERMINAL_DIR": str(work),
        "TERMINAL_ENABLED": "1",
        "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ.get('PATH', '')}",
    }
    for key, value in monkeied.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(term_routes, "WORKSPACE", str(work))
    workspace_module.set_work_dir(str(work))
    sandbox.set_runner(lambda command, timeout=None:
                       term_routes.run_command(command, timeout=timeout))

    agent = CoderAgent("pipeline", prompt_path("coder_agent"), provider=None)
    agent.attach(events=lambda event: None)
    yield agent, work
    sandbox.set_runner(None)
    workspace_module.set_work_dir("")


def _write(agent, relative: str, text: str):
    """То же, что делает движок для блока ```` ```python:relative ````."""
    ok, feedback = agent.run_tool("python", text, save_path=relative)
    assert ok, feedback
    return feedback


def test_agent_writes_project_and_smoke_test_passes(pipeline):
    agent, work = pipeline

    # 1. «Модель» присылает файлы проекта — движок их записывает.
    _write(agent, "shop/app.py", (SHOP_DIR / "app.py").read_text(encoding="utf-8"))
    for template in sorted((SHOP_DIR / "templates").glob("*.html")):
        _write(agent, f"shop/templates/{template.name}",
               template.read_text(encoding="utf-8"))
    _write(agent, "shop/smoke.py", SMOKE)

    assert (work / "shop" / "app.py").is_file()
    assert len(list((work / "shop" / "templates").glob("*.html"))) >= 6

    # 2. Запись файла не выполняет код: сервер не может занять терминал.
    shown = agent.blocks_result[-1]
    assert "не выполнялся" in str(shown["output"]), shown["output"]

    # 3. Проверка запускается отдельной командой в песочнице.
    ok, feedback = agent.run_tool("bash", "python3 shop/smoke.py")
    assert ok, feedback
    assert "SMOKE OK" in feedback


def test_agent_cannot_silently_overwrite_a_file(pipeline):
    """Повторная запись существующего файла — отказ с подсказкой про file_edit."""
    agent, work = pipeline
    _write(agent, "shop/notes.txt", "первая версия")

    ok, feedback = agent.run_tool("python", "вторая версия", save_path="shop/notes.txt")
    assert ok is False
    assert "file_edit" in feedback
    assert (work / "shop" / "notes.txt").read_text(encoding="utf-8") == "первая версия"


def test_agent_bash_sees_written_files(pipeline):
    agent, work = pipeline
    _write(agent, "app.py", "print('shop')\n")
    ok, feedback = agent.run_tool("bash", "ls -1")
    assert ok and "app.py" in feedback


def test_agent_rejects_absolute_paths_in_commands(pipeline):
    """Команда с абсолютным путём получает объяснение, а не «Path traversal»."""
    agent, _work = pipeline
    ok, feedback = agent.run_tool("bash", "ls /etc")
    assert ok is False
    assert "относительн" in feedback
    assert "Path traversal" not in feedback


def test_full_turn_builds_the_shop_in_one_answer(admin_client, sandbox, monkeypatch):
    """Один ответ модели = весь проект: несколько блоков ```` ```python:path ````.

    Так выглядит реальный запрос «создай интернет-магазин на Flask». Раньше в
    ответе побеждал последний путь (все файлы писались в один), а `python:path`
    ещё и выполнялся — сервер занимал терминал. Теперь на диске весь проект,
    а проверка запускается отдельной командой в песочнице.
    """
    from tests.agent_helpers import answer_of, events_of, ndjson, script

    blocks = [f"```python:shop/app.py\n{(SHOP_DIR / 'app.py').read_text(encoding='utf-8')}\n```"]
    for template in sorted((SHOP_DIR / "templates").glob("*.html")):
        blocks.append(f"```python:shop/templates/{template.name}\n"
                      f"{template.read_text(encoding='utf-8')}\n```")
    blocks.append(f"```python:shop/smoke.py\n{SMOKE}\n```")

    script(monkeypatch, [
        "\n".join(blocks),
        "```bash\npython3 shop/smoke.py\n```",
        "Готово: магазин собран, проверка прошла.",
    ])

    events = ndjson(admin_client.post("/api/agent/stream", json={
        "message": "Создай веб сайт интернет магазина с помощью питона и фласк"}))

    # Все файлы на месте — а не только последний.
    assert (sandbox / "shop" / "app.py").is_file()
    assert len(list((sandbox / "shop" / "templates").glob("*.html"))) >= 6
    assert (sandbox / "shop" / "smoke.py").is_file()

    tools = events_of(events, "tool")
    assert all(tool["code"] == 0 for tool in tools), tools
    assert tools[-1]["name"] == "terminal" and "SMOKE OK" in tools[-1]["output"]
    assert "магазин собран" in answer_of(events)


def test_model_sees_its_error_and_fixes_it(admin_client, sandbox, monkeypatch):
    """Петля обратной связи: ошибка → `[failure]` у модели → правка → проверка.

    Ровно то, чего не хватало в журнале с телефона: шаг падал, а модель
    продолжала уверять, что всё готово.
    """
    from tests.agent_helpers import answer_of, events_of, ndjson, script

    app_source = (SHOP_DIR / "app.py").read_text(encoding="utf-8")
    broken = app_source.replace("from flask import (Flask, abort, flash, g, redirect, render_template, request,\n                   session, url_for)",
                                "from flask import (Flask, abort, flash, g, redirect, render_template, request,")
    assert broken != app_source, "нужен настоящий SyntaxError"

    blocks = [f"```python:shop/app.py\n{broken}\n```"]
    for template in sorted((SHOP_DIR / "templates").glob("*.html")):
        blocks.append(f"```python:shop/templates/{template.name}\n"
                      f"{template.read_text(encoding='utf-8')}\n```")
    blocks.append(f"```python:shop/smoke.py\n{SMOKE}\n```")

    fix = (
        '```file_edit\n'
        + '{"path": "shop/app.py", "old": "from flask import (Flask, abort, flash, g, redirect, render_template, request,", '
        + '"new": "from flask import (Flask, abort, flash, g, redirect, render_template, request,\\n                   session, url_for)"}\n'
        + '```'
    )
    seen = []
    script(monkeypatch, [
        "\n".join(blocks),
        "```bash\npython3 shop/smoke.py\n```",
        fix,
        "```bash\npython3 shop/smoke.py\n```",
        "Исправил импорт, проверка проходит.",
    ], seen)

    events = ndjson(admin_client.post("/api/agent/stream", json={"message": "создай интернет-магазин на flask"}))
    tools = events_of(events, "tool")

    smoke_runs = [tool for tool in tools if tool["name"] == "terminal"]
    # Первая проверка упала и модель это увидела.
    assert len(smoke_runs) == 2, smoke_runs
    assert smoke_runs[0]["code"] == 1 and "SyntaxError" in smoke_runs[0]["output"]
    assert "SyntaxError" in seen[2]["messages"][-1]["content"]
    # Правка применилась, вторая проверка прошла.
    edits = [tool for tool in tools if tool["name"] == "file_edit"]
    assert edits and edits[0]["code"] == 0
    assert smoke_runs[1]["code"] == 0 and "SMOKE OK" in smoke_runs[1]["output"]
    assert "проверка проходит" in answer_of(events)
