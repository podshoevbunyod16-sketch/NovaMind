"""Вращающийся квадрат без экрана: тот же алгоритм, результат — animated SVG.

Зачем: `turtle` требует tkinter и настоящий дисплей. В песочнице NovaMind, в
Docker, по SSH и в CI их нет — код падает ещё на `import turtle`
(`ModuleNotFoundError: No module named 'tkinter'`, а с установленным tkinter —
`TclError: no display name and no $DISPLAY environment variable`).

Здесь нет ни графической библиотеки, ни зависимостей: квадрат считается тем же
циклом (72 кадра по 5°, `clear()` → `setheading()` → 4 × `forward/right`), а
кадры записываются в SVG с CSS-анимацией — его открывает любой браузер.

    python3 headless_spin.py          # пишет spin.svg рядом с собой

Чего здесь нет: интерактивного окна и черепашки, которую можно тащить мышью.
Для этого нужен второй файл — `turtle_spin.py` (с экраном).
"""

from __future__ import annotations

import math
import shutil
import struct
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path

SIZE = 100
STEP = 5                  # градусов за кадр
FRAME_MS = 30             # как в turtle_spin.py
OUT = Path(__file__).resolve().parent / "spin.svg"
STRIP = Path(__file__).resolve().parent / "spin_frames.png"
GIF = Path(__file__).resolve().parent / "spin.gif"
CELL = 150                 # сторона одной ячейки в пикселях
STRIP_COLUMNS = 4          # 8 кадров → 4 × 2


class HeadlessTurtle:
    """Минимальная замена turtle: считает координаты и запоминает линии."""

    def __init__(self) -> None:
        self.x = 0.0
        self.y = 0.0
        self.heading = 0.0             # градусы, 0 — вправо, против часовой
        self.segments: list[tuple[float, float, float, float]] = []

    def setheading(self, angle: float) -> None:
        self.heading = float(angle) % 360

    def right(self, angle: float) -> None:
        self.heading = (self.heading - float(angle)) % 360

    def forward(self, distance: float) -> None:
        radians = math.radians(self.heading)
        nx = self.x + distance * math.cos(radians)
        ny = self.y + distance * math.sin(radians)
        self.segments.append((self.x, self.y, nx, ny))
        self.x, self.y = nx, ny

    def clear(self) -> None:
        self.segments.clear()


def draw_square(pen: HeadlessTurtle, size: float) -> None:
    for _ in range(4):
        pen.forward(size)
        pen.right(90)


def render(frames: list[list[tuple[float, float, float, float]]],
           angles: list[int]) -> str:
    """SVG: фон, бледный «след» всех кадров и вращающийся квадрат."""
    span = SIZE * 3.2
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{-span / 2:.0f} {-span / 2:.0f} '
        f'{span:.0f} {span:.0f}" width="420" height="420">',
        '<rect x="-1000" y="-1000" width="2000" height="2000" fill="black"/>',
        '<g stroke="#0e5a5a" stroke-width="1" fill="none" opacity="0.55">',
    ]
    # След: каждый 8-й кадр — видно, что квадрат действительно прошёл круг.
    for index in range(0, len(frames), 8):
        points = " ".join(f"{x1:.2f},{y1:.2f} {x2:.2f},{y2:.2f}"
                          for x1, y1, x2, y2 in frames[index])
        parts.append(f'<polyline points="{points}"/>')
    parts.append("</g>")

    first = frames[0]
    points = " ".join(f"{x1:.2f},{y1:.2f} {x2:.2f},{y2:.2f}" for x1, y1, x2, y2 in first)
    # SVG вращает по часовой стрелке при положительном угле, наши координаты —
    # против: знак минус, чтобы анимация шла в ту же сторону, что и кадры.
    values = ";".join(str(-angle) for angle in angles + [360])
    duration = max(1.0, len(frames) * FRAME_MS / 1000)
    parts += [
        '<g style="transform-origin: 0 0">',
        f'<g stroke="cyan" stroke-width="3" stroke-linecap="round" fill="none">',
        f'<polyline points="{points}">',
        f'<animateTransform attributeName="transform" type="rotate" '
        f'values="{values}" dur="{duration:.2f}s" repeatCount="indefinite"/>',
        "</polyline></g></g>",
        "</svg>",
    ]
    return "\n".join(parts)


# ── картинка без библиотек: линии и PNG пишем сами ────────────────────────

def _canvas(width: int, height: int) -> list[bytearray]:
    return [bytearray(b"\x00\x00\x00" * width) for _ in range(height)]


def _dot(canvas: list[bytearray], x: int, y: int, color: tuple[int, int, int], radius: int) -> None:
    height, width = len(canvas), len(canvas[0]) // 3
    for dy in range(-radius, radius + 1):
        for dx in range(-radius, radius + 1):
            if dx * dx + dy * dy > radius * radius:
                continue
            px, py = x + dx, y + dy
            if 0 <= px < width and 0 <= py < height:
                canvas[py][px * 3:px * 3 + 3] = bytes(color)


def _line(canvas, x1: float, y1: float, x2: float, y2: float,
          color=(0, 255, 255), radius: int = 1) -> None:
    steps = int(max(abs(x2 - x1), abs(y2 - y1)) * 2) + 1
    for index in range(steps + 1):
        t = index / steps
        _dot(canvas, round(x1 + (x2 - x1) * t), round(y1 + (y2 - y1) * t), color, radius)


def write_png(path: Path, canvas: list[bytearray]) -> None:
    """PNG из готовых строк RGB — только стандартная библиотека."""
    height, width = len(canvas), len(canvas[0]) // 3
    raw = b"".join(b"\x00" + bytes(row) for row in canvas)

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))

    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw, 9))
           + chunk(b"IEND", b""))
    path.write_bytes(png)


def _bounds(frames):
    """Крайние точки всех кадров: по ним считаем общий масштаб."""
    xs = [value for frame in frames for x1, _y1, x2, _y2 in frame for value in (x1, x2)]
    ys = [value for frame in frames for _x1, y1, _x2, y2 in frame for value in (y1, y2)]
    return min(xs), max(xs), min(ys), max(ys)


def _cell(frame, cell: int, bounds, color=(0, 220, 220), radius: int = 1):
    """Один кадр в квадратной ячейке: общий масштаб, центр по рамке кадров."""
    left, right, bottom, top = bounds
    pad = 12
    scale = min((cell - 2 * pad) / max(1e-6, right - left),
                (cell - 2 * pad) / max(1e-6, top - bottom))
    cx, cy = (left + right) / 2, (bottom + top) / 2
    canvas = _canvas(cell, cell)
    for x1, y1, x2, y2 in frame:
        # координаты turtle (y вверх) → экран (y вниз)
        _line(canvas, cell / 2 + (x1 - cx) * scale, cell / 2 - (y1 - cy) * scale,
              cell / 2 + (x2 - cx) * scale, cell / 2 - (y2 - cy) * scale, color, radius)
    return canvas


def filmstrip(frames, step: int = 3) -> list[bytearray]:
    """Сетка кадров: видно, что квадрат действительно поворачивается."""
    chosen = frames[::step][:STRIP_COLUMNS * 2]
    bounds = _bounds(frames)
    rows = math.ceil(len(chosen) / STRIP_COLUMNS)
    canvas = _canvas(CELL * STRIP_COLUMNS, CELL * rows)
    for index, frame in enumerate(chosen):
        cell = _cell(frame, CELL, bounds)
        ox = (index % STRIP_COLUMNS) * CELL
        oy = (index // STRIP_COLUMNS) * CELL
        for row in range(CELL):
            canvas[oy + row][ox * 3:(ox + CELL) * 3] = cell[row]
    return canvas


def render_gif(frames, frame_ms: int = FRAME_MS) -> str:
    """Анимированный GIF через ImageMagick/ffmpeg — если они есть в системе.

    Обе утилиты необязательны: без них остаются SVG (анимация в браузере) и
    раскадровка PNG. GIF нужен, чтобы анимацию было видно в чате и превью.
    """
    convert = shutil.which("convert") or shutil.which("magick")
    ffmpeg = shutil.which("ffmpeg")
    if not (convert or ffmpeg):
        return "пропущено: нет convert/ffmpeg"
    bounds = _bounds(frames)
    with tempfile.TemporaryDirectory() as folder:
        paths = []
        for index, frame in enumerate(frames):
            path = Path(folder) / f"f{index:03d}.png"
            write_png(path, _cell(frame, 200, bounds, (0, 230, 230), 2))
            paths.append(path)
        if convert:
            command = [convert, "-delay", str(max(1, round(frame_ms / 10))), "-loop", "0",
                       *map(str, paths), str(GIF)]
        else:
            command = [ffmpeg, "-y", "-loglevel", "error", "-framerate",
                       f"{1000 / frame_ms:.2f}", "-i", str(Path(folder) / "f%03d.png"),
                       "-loop", "0", str(GIF)]
        result = subprocess.run(command, capture_output=True, text=True, timeout=180)
        if result.returncode != 0:
            return f"не удалось: {result.stderr.strip()[:120]}"
    return f"готово: {GIF.name} · {GIF.stat().st_size} байт"

def main() -> int:
    pen = HeadlessTurtle()
    # Квадрат вращается вокруг своего центра: если оставить черепашку в (0,0),
    # она описывает круг радиусом size/2 и кадр «уползает». Центр — в (0,0),
    # стартовый угол квадрата каждый кадр поворачиваем на angle.

    frames: list[list[tuple[float, float, float, float]]] = []
    angles: list[int] = []
    angle = 0
    while angle < 360:
        pen.clear()
        pen.setheading(angle)                      # курс черепашки = поворот квадрата
        corner = math.radians(angle + 135)         # стартовый угол относительно центра
        pen.x = (SIZE / 2) * math.sqrt(2) * math.cos(corner)
        pen.y = (SIZE / 2) * math.sqrt(2) * math.sin(corner)
        draw_square(pen, SIZE)
        frames.append(list(pen.segments))
        angles.append(angle)
        angle += STEP

    OUT.write_text(render(frames, angles), encoding="utf-8")
    write_png(STRIP, filmstrip(frames))
    gif_status = render_gif(frames)
    print(f"кадров: {len(frames)} (шаг {STEP}°, {FRAME_MS} мс на кадр)")
    print(f"линий в кадре: {len(frames[0])}, углы: {angles[0]}…{angles[-1]}°")
    print("первый кадр:", " ".join(f"({x1:.0f},{y1:.0f})->({x2:.0f},{y2:.0f})"
                                   for x1, y1, x2, y2 in frames[0]))
    print(f"готово: {OUT.name} · {OUT.stat().st_size} байт · один оборот за "
          f"{len(frames) * FRAME_MS / 1000:.2f} с")
    print(f"раскадровка: {STRIP.name} · {STRIP.stat().st_size} байт")
    print(f"анимация: {gif_status}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
