"""Тот же вращающийся квадрат, но исправленный для машины с экраном.

Что было не так в исходном коде (и почему он не работал даже с tkinter):

1. `screen.update()` ничего не делает, если не включён ручной режим:
   нужен `screen.tracer(0)`. Иначе turtle перерисовывает окно на каждый
   `forward`, анимация дёргается и тормозит;
2. `while True` крутится вечно и забирает весь поток: окно не закрыть,
   `turtle.done()` после цикла никогда не выполняется (мёртвый код);
3. нет выхода из цикла: квадрат вращается даже когда окно закрыли, и Python
   падает на «application has been destroyed».

Здесь анимация идёт по таймеру (`screen.ontimer`), за один полный оборот,
потом останавливается. Esc или клик по окну — выход.

Запуск (нужен экран и tkinter):

    python3 turtle_spin.py

Если tkinter нет — `sudo apt install python3-tk` (Debian/Ubuntu),
`pkg install python-tkinter` (Termux), `brew install python-tk` (macOS).
"""

import turtle

size = 100
step = 5                      # градусов за кадр


def main() -> None:
    screen = turtle.Screen()
    screen.title("Вращающийся квадрат")
    screen.bgcolor("black")
    screen.tracer(0)          # ручная перерисовка: кадры рисуем сами
    screen.setup(width=420, height=420)

    pen = turtle.Turtle()
    pen.hideturtle()
    pen.speed(0)
    pen.pensize(3)
    pen.color("cyan")
    pen.penup()
    pen.goto(0, -size / 2)    # квадрат вращается вокруг своего центра
    pen.pendown()

    def draw_square() -> None:
        for _ in range(4):
            pen.forward(size)
            pen.right(90)

    def frame(angle: int) -> None:
        pen.clear()
        pen.setheading(angle)
        draw_square()
        screen.update()
        if angle + step < 360:
            screen.ontimer(lambda: frame(angle + step), 30)
        else:
            screen.ontimer(screen.bye, 800)   # оборот закончен — закрываем окно

    screen.onkeypress(screen.bye, "Escape")
    screen.listen()
    frame(0)
    turtle.done()             # цикл событий tkinter: окно живёт без while True


if __name__ == "__main__":
    main()
