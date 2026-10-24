# Вращающийся квадрат: две версии

Пользовательский код с `turtle` в Linux-песочнице не запускается — там нет
дисплея. Здесь две версии: одна для машины с экраном, одна для песочницы.

## Почему исходный скрипт не выводит ничего

```
$ python3 turtle_code.py
Traceback (most recent call last):
  File "turtle_code.py", line 1, in <module>
    import turtle
  File "/usr/lib/python3.11/turtle.py", line 107, in <module>
    import tkinter as TK
ModuleNotFoundError: No module named 'tkinter'
```

В песочнице, в Docker, по SSH и в CI нет ни `tkinter`, ни `$DISPLAY`. Даже с
установленным `tkinter` та же программа падает иначе:

```
_tkinter.TclError: no display name and no $DISPLAY environment variable
```

Это не ошибка логики: квадрат нарисовался бы, если бы было куда.

## Две версии

### `turtle_spin.py` — для машины с экраном

```bash
python3 turtle_spin.py
```

Исправлено по сравнению с исходником:

* `screen.tracer(0)` — без этого `screen.update()` ничего не делает, и turtle
  перерисовывает окно на каждый `forward` (анимация дёргается);
* нет `while True` — цикл занимал поток, окно не закрывалось, `turtle.done()`
  был мёртвым кодом. Кадры идут по `screen.ontimer`;
* выход: Esc или конец оборота; квадрат вращается вокруг своего центра.

Нужен `tkinter`: `sudo apt install python3-tk` (Debian/Ubuntu),
`pkg install python-tkinter` (Termux), `brew install python-tk` (macOS).

### `headless_spin.py` — без экрана и без зависимостей

```bash
python3 headless_spin.py
```

Тот же цикл (72 кадра по 5°, 30 мс), но кадры не рисуются в окне, а
записываются в файлы:

| Файл | Что это |
|---|---|
| `spin.gif` | анимация одного оборота (если есть ImageMagick или ffmpeg) |
| `spin.svg` | анимация на CSS — открывается любым браузером |
| `spin_frames.png` | раскадровка: видно поворот квадрата |

Внутри — свой `HeadlessTurtle` (координаты вместо окна), растеризатор линий и
запись PNG через `zlib`/`struct`: только стандартная библиотека.
