"""Python tool (block tag ``python``).

Derived from AgenticSeek ``sources/tools/PyInterpreter.py`` (GPL-3.0, see
NOTICE.md): same tag, same feedback format, same "no interactive code" rule.

NovaMind adaptations:

* the code runs through the NovaMind sandbox as ``python3 .agent-tmp/<file>.py``
  — no direct ``subprocess`` and no free-form ``python3 -c`` (the sandbox
  validates the file path);
* the scratch file lives in the workspace and is removed afterwards;
* interactive input is refused up front: the sandbox has no stdin, so
  ``input()`` would block until the timeout and waste the step.
"""

from __future__ import annotations

import os
import re
import uuid

from agenticseek.core.agent import clip
from agenticseek.tools import sandbox
from agenticseek.tools.safety import is_unsafe
from agenticseek.tools.tools import Tools

DEFAULT_TIMEOUT = float(os.getenv("AGENTICSEEK_PYTHON_TIMEOUT", "15"))
SCRATCH_DIR = ".agent-tmp"
_INTERACTIVE = re.compile(r"(?m)^\s*(input\s*\(|breakpoint\s*\(|exit\s*\(|quit\s*\(|"
                          r"import\s+curses|curses\.|sys\.stdin\.read)")


class PythonTool(Tools):
    tag = "python"
    name = "Python"
    description = "Выполнить Python-код в рабочей папке (без ввода с клавиатуры)."
    risk = "write"

    def __init__(self, timeout: float | None = None):
        super().__init__()
        self.timeout = max(1.0, timeout or DEFAULT_TIMEOUT)

    def execute(self, payload: str) -> str:
        code = str(payload or "")
        if not code.strip():
            return "[failure] Пустой блок python."
        saved = str(getattr(self, "saved_path", "") or "")
        if saved:
            # Блок вида ```python:app.py``` — это создание файла. Выполнять его
            # здесь нельзя: Flask-приложение с app.run() займёт терминал до
            # таймаута, а скрипт-генератор отработает дважды. Запись и запуск
            # разделены — проверка отдельной командой.
            note = ""
            if "app.run(" in code or "app.run (" in code:
                note = ("\nЭто веб-приложение: сервер в песочнице не запускай — он "
                        "занимает терминал. Проверяй через test client, например "
                        "`python3 -c \"from app import app; c=app.test_client(); "
                        "print(c.get('/').status_code)\"`.")
            return (f"[success] Код записан в {saved} и не выполнялся.\n"
                    f"Проверь его отдельной командой в блоке bash: "
                    f"`python3 -m py_compile {saved}` или `python3 {saved}`.{note}")
        if _INTERACTIVE.search(code):
            return ("[failure] Код ждёт ввода с клавиатуры (input/curses/stdin) — "
                    "он зависнет. Перепиши так, чтобы все значения были в переменных.")
        if is_unsafe(code):
            return ("[failure] Код содержит опасную команду (удаление, сеть, установка). "
                    "Разрешены только безопасные вычисления и работа с файлами в рабочей папке.")

        directory = os.path.join(self.work_dir, SCRATCH_DIR)
        os.makedirs(directory, exist_ok=True)
        name = f"snippet_{uuid.uuid4().hex[:10]}.py"
        relative = f"{SCRATCH_DIR}/{name}"
        try:
            with open(os.path.join(directory, name), "w", encoding="utf-8") as handle:
                handle.write(code)
            result = sandbox.run(f"python3 {relative}", timeout=self.timeout)
        except Exception as error:  # noqa: BLE001 — the model must see the reason
            return f"[failure] Не удалось выполнить Python: {error}"
        finally:
            try:
                os.remove(os.path.join(directory, name))
            except OSError:
                pass

        if result.get("cancelled"):
            return "[failure] Выполнение отменено пользователем."
        text = (result.get("stdout") or "") + (result.get("stderr") or "")
        text = text.strip()
        if result["code"] != 0:
            return (f"[failure] Python завершился с кодом {result['code']}:\n"
                    f"{clip(text or '(нет вывода)', 3000)}")
        if not text:
            # Так выглядит блок из одних import/def: он «прошёл», но ничего не
            # сделал. Модель должна понять, что файл так не создаётся.
            return ("[success] Python выполнен, но ничего не вывел. Если цель — "
                    "создать файл проекта, укажи путь сразу после тега: "
                    "```python:app.py```. Блок ```python``` — это запуск, он не "
                    "сохраняет код в проект.")
        return f"[success] Python выполнен:\n{clip(text, 3000)}"
