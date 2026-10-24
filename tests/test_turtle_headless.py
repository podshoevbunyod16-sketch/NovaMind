"""Код с окном: в песочнице объясняем простой экрана, а не «чиним» рабочий код.

Пользователь прислал программу с turtle и попросил запустить её. Песочница без
дисплея: `import turtle` падает. Раньше модель получала голый traceback и
начинала ставить пакеты; теперь получает объяснение и путь к результату в файле.
"""

import subprocess
import sys
from pathlib import Path

import pytest

from agenticseek.tools.gui_hint import gui_hint, needs_display
from agenticseek.tools.py_interpreter import PythonTool

EXAMPLES = Path(__file__).resolve().parent.parent / "examples" / "turtle_spin"


@pytest.mark.parametrize("text", [
    "ModuleNotFoundError: No module named 'tkinter'",
    "tkinter.TclError: no display name and no $DISPLAY environment variable",
    "pygame.error: No available video device",
])
def test_display_errors_are_recognised(text):
    assert needs_display(text)
    assert "экрана" in gui_hint(text)


@pytest.mark.parametrize("text", ["SyntaxError: invalid syntax",
                                  "ModuleNotFoundError: No module named 'flask'"])
def test_other_errors_get_no_hint(text):
    assert gui_hint(text) == ""


def test_python_tool_adds_the_hint(monkeypatch, sandbox):
    from agenticseek.tools import sandbox as sandbox_module

    monkeypatch.setattr(sandbox_module, "run", lambda command, timeout=None: {
        "code": 1, "stdout": "",
        "stderr": "ModuleNotFoundError: No module named 'tkinter'", "timed_out": False})
    tool = PythonTool()
    output = tool.execute("import turtle\n")
    assert output.startswith("[failure]")
    assert "No module named 'tkinter'" in output
    assert "экрана" in output          # «в песочнице нет дисплея» — подсказка
    assert "SVG" in output


def test_headless_version_renders_frames(tmp_path):
    """Тот же алгоритм без экрана: 72 кадра, SVG и раскадровка PNG."""
    script = EXAMPLES / "headless_spin.py"
    result = subprocess.run([sys.executable, str(script)], capture_output=True,
                            text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    assert "кадров: 72" in result.stdout
    assert "раскадровка: spin_frames.png" in result.stdout

    svg = (EXAMPLES / "spin.svg").read_text(encoding="utf-8")
    assert svg.count("<polyline") >= 10, "след кадров и сам квадрат"
    assert "animateTransform" in svg
    png = (EXAMPLES / "spin_frames.png").read_bytes()
    assert png.startswith(b"\x89PNG\r\n\x1a\n") and len(png) > 1000


def test_headless_geometry_matches_turtle_square(tmp_path):
    """Первый кадр — квадрат 100×100 от (0,0): та же геометрия, что у turtle."""
    sys.path.insert(0, str(EXAMPLES))
    try:
        import headless_spin
    finally:
        sys.path.pop(0)

    pen = headless_spin.HeadlessTurtle()
    headless_spin.draw_square(pen, 100)
    assert len(pen.segments) == 4
    for got, want in zip(pen.segments, [(0, 0, 100, 0), (100, 0, 100, -100),
                                        (100, -100, 0, -100), (0, -100, 0, 0)]):
        assert got == pytest.approx(want, abs=1e-9)


def test_tcl_error_without_display_is_also_explained(monkeypatch, sandbox):
    """tkinter есть, но экрана нет: `no display name and no $DISPLAY`."""
    from agenticseek.tools import sandbox as sandbox_module

    monkeypatch.setattr(sandbox_module, "run", lambda command, timeout=None: {
        "code": 1, "stdout": "",
        "stderr": "tkinter.TclError: no display name and no $DISPLAY environment variable",
        "timed_out": False})
    output = PythonTool().execute("import turtle\nturtle.done()\n")
    assert "экрана" in output and "SVG" in output


def test_gif_is_actually_animated():
    """`spin.gif` — настоящая анимация: несколько кадров, а не один слайд."""
    data = (EXAMPLES / "spin.gif").read_bytes()
    assert data.startswith(b"GIF89a")
    assert data.count(b"\x21\xf9\x04") >= 10, "графические блоки кадров"
