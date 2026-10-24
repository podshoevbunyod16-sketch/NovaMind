"""Подсказки к ошибкам, которые в песочнице неизбежны, а код при этом верный.

Главная — графические библиотеки. `turtle`, `tkinter`, `pygame`, `pyglet`,
`matplotlib.show()` требуют дисплея. В песочнице NovaMind его нет, поэтому
верный код падает на `import turtle`:

    ModuleNotFoundError: No module named 'tkinter'      (tkinter не установлен)
    TclError: no display name and no $DISPLAY           (есть tkinter, нет экрана)
    pygame.error: No available video device

Без пояснения модель начинает «чинить» рабочий код: ставит пакеты, меняет
импорты и в итоге переписывает программу целиком. Подсказка говорит, что код
верный, а запускать его надо иначе.
"""

from __future__ import annotations

import re

_DISPLAY_ERRORS = re.compile(
    r"(no module named '?tkinter|no display name|couldn't connect to display|"
    r"cannot open display|no available video device|DISPLAY environment variable|"
    r"SDL_VIDEODRIVER|pygame\.error: No available|tkinter\.TclError|"
    r"X connection to .* broken|Could not initialize SDL)", re.I)

HINT = (
    "Код не выполнился только из-за отсутствия экрана: в песочнице нет дисплея "
    "(и часто нет tkinter). Ошибка не в логике программы.\n"
    "Что делать вместо запуска окна:\n"
    "1) посчитай кадры/координаты и сохрани результат в файл — SVG, PNG или GIF "
    "(для SVG/PNG хватает стандартной библиотеки: math, struct, zlib);\n"
    "2) проверь алгоритм без окна: assert на координатах после N кадров;\n"
    "3) оставь версию с окном для запуска у пользователя и скажи, какой командой "
    "её запускать и что для этого нужно (tkinter, экран).\n"
    "Не устанавливай пакеты и не переписывай рабочий код «наугад»."
)


def needs_display(output: str) -> bool:
    """Похоже ли сообщение об ошибке на «нет экрана»."""
    return bool(_DISPLAY_ERRORS.search(str(output or "")))


def gui_hint(output: str) -> str:
    """Подсказка для вывода инструмента: пустая строка, если это не GUI-ошибка."""
    return HINT if needs_display(output) else ""
