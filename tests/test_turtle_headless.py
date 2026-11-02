"""Headless graphics fail honestly and leave a usable file-based alternative."""
import subprocess
import sys
from pathlib import Path

import pytest

from agent.gui import gui_hint, needs_display

EXAMPLES = Path(__file__).resolve().parent.parent / "examples" / "turtle_spin"


@pytest.mark.parametrize("text", [
    "ModuleNotFoundError: No module named 'tkinter'",
    "tkinter.TclError: no display name and no $DISPLAY environment variable",
    "pygame.error: No available video device",
])
def test_display_errors_are_recognised(text):
    assert needs_display(text)
    assert "графического экрана" in gui_hint(text)
    assert "SVG" in gui_hint(text)


@pytest.mark.parametrize("text", ["SyntaxError: invalid syntax",
                                  "ModuleNotFoundError: No module named 'flask'"])
def test_other_errors_get_no_hint(text):
    assert gui_hint(text) == ""


def test_first_party_shell_result_adds_headless_hint():
    import tool_registry

    outcome = tool_registry._shell_result({
        "code": 1, "stdout": "", "stderr": "ModuleNotFoundError: No module named 'tkinter'",
        "timed_out": False,
    })
    assert outcome["ok"] is False
    assert "tkinter" in outcome["error"]["message"]
    assert "графического экрана" in outcome["hint"]
    assert "SVG" in outcome["hint"]


def test_headless_version_renders_frames(tmp_path):
    """The standalone, non-GUI example still renders frames and an SVG preview."""
    script = EXAMPLES / "headless_spin.py"
    result = subprocess.run([sys.executable, str(script)], capture_output=True,
                            text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    assert "кадров: 72" in result.stdout
    assert "раскадровка: spin_frames.png" in result.stdout
    svg = (EXAMPLES / "spin.svg").read_text(encoding="utf-8")
    assert svg.count("<polyline") >= 10
    assert "animateTransform" in svg
    png = (EXAMPLES / "spin_frames.png").read_bytes()
    assert png.startswith(b"\x89PNG\r\n\x1a\n") and len(png) > 1000


def test_headless_geometry_matches_square():
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


def test_tcl_error_without_display_is_also_explained():
    import tool_registry

    outcome = tool_registry._shell_result({
        "code": 1, "stdout": "", "stderr": "tkinter.TclError: no display name and no $DISPLAY environment variable",
        "timed_out": False,
    })
    assert "графического экрана" in outcome["hint"] and "SVG" in outcome["hint"]


def test_gif_is_actually_animated():
    data = (EXAMPLES / "spin.gif").read_bytes()
    assert data.startswith(b"GIF89a")
    assert data.count(b"\x21\xf9\x04") >= 10
