"""Интернет-магазин на Flask — пример, который агент NovaMind умеет собрать целиком.

Запуск::

    pip install flask
    python3 app.py            # http://127.0.0.1:8000

Что умеет:

* каталог с категориями и поиском;
* карточка товара, корзина в сессии, изменение количества, удаление;
* оформление заказа (имя, телефон, адрес) и страница заказа;
* админка с паролем: добавление и удаление товаров, список заказов;
* хранение в SQLite (только стандартная библиотека, никаких других зависимостей).

Всё проверяется без запуска сервера: ``tests/test_flask_shop.py`` поднимает
``app.test_client()`` и проходит пути покупателя и администратора.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from flask import (Flask, abort, flash, g, redirect, render_template, request,
                   session, url_for)

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB = BASE_DIR / "data" / "shop.db"


def create_app(db_path: str | os.PathLike | None = None) -> Flask:
    """Создаёт приложение. ``db_path`` подменяется в тестах."""
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.getenv("SHOP_SECRET", "dev-secret-change-me")
    app.config["DATABASE"] = str(db_path or os.getenv("SHOP_DB") or DEFAULT_DB)
    app.config["ADMIN_PASSWORD"] = os.getenv("SHOP_ADMIN_PASSWORD", "admin")

    # ── база данных ───────────────────────────────────────────────────────
    def get_db() -> sqlite3.Connection:
        if "db" not in g:
            Path(app.config["DATABASE"]).parent.mkdir(parents=True, exist_ok=True)
            g.db = sqlite3.connect(app.config["DATABASE"])
            g.db.row_factory = sqlite3.Row
            g.db.execute("PRAGMA foreign_keys = ON")
        return g.db

    @app.teardown_appcontext
    def close_db(_error=None):
        db = g.pop("db", None)
        if db is not None:
            db.close()

    def init_db() -> None:
        db = get_db()
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT 'разное',
                price INTEGER NOT NULL DEFAULT 0,
                description TEXT NOT NULL DEFAULT '',
                stock INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                customer TEXT NOT NULL,
                phone TEXT NOT NULL,
                address TEXT NOT NULL,
                total INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS order_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
                product_id INTEGER NOT NULL REFERENCES products(id),
                title TEXT NOT NULL,
                price INTEGER NOT NULL,
                quantity INTEGER NOT NULL
            );
            """
        )
        if db.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 0:
            db.executemany(
                "INSERT INTO products (title, category, price, description, stock) "
                "VALUES (?, ?, ?, ?, ?)",
                [
                    ("Кофе Ethiopia 250 г", "напитки", 790,
                     "Светлая обжарка, ноты жасмина и персика.", 12),
                    ("Чайник стеклянный 1 л", "посуда", 1450,
                     "Боросиликатное стекло, крышка из бамбука.", 5),
                    ("Кружка керамическая", "посуда", 590,
                     "Ручная работа, 330 мл, можно в посудомойку.", 20),
                ],
            )
        db.commit()

    with app.app_context():
        init_db()

    # ── корзина в сессии ──────────────────────────────────────────────────
    def cart() -> dict[str, int]:
        raw = session.get("cart") or {}
        return {str(key): int(value) for key, value in raw.items() if int(value) > 0}

    def save_cart(items: dict[str, int]) -> None:
        session["cart"] = {key: value for key, value in items.items() if value > 0}
        session.modified = True

    def cart_details() -> tuple[list[sqlite3.Row], int]:
        items = cart()
        if not items:
            return [], 0
        db = get_db()
        placeholders = ",".join("?" for _ in items)
        rows = db.execute(f"SELECT * FROM products WHERE id IN ({placeholders})",
                          [int(key) for key in items]).fetchall()
        total = 0
        result = []
        for row in rows:
            quantity = items[str(row["id"])]
            total += row["price"] * quantity
            result.append({"product": row, "quantity": quantity,
                           "sum": row["price"] * quantity})
        return result, total

    @app.context_processor
    def inject_cart_size():
        return {"cart_size": sum(cart().values()), "cart_total": cart_details()[1]}

    @app.template_filter("money")
    def money(value) -> str:
        return f"{int(value or 0):,}".replace(",", " ") + " c."

    # ── каталог ───────────────────────────────────────────────────────────
    @app.get("/")
    def index():
        db = get_db()
        category = (request.args.get("category") or "").strip()
        query = (request.args.get("q") or "").strip()
        sql = "SELECT * FROM products WHERE 1=1"
        params: list[object] = []
        if category:
            sql += " AND category = ?"
            params.append(category)
        products = db.execute(sql + " ORDER BY id", params).fetchall()
        if query:
            # SQLite LIKE не знает регистра кириллицы («кофе» ≠ «Кофе»),
            # поэтому поиск по подстроке делаем в Python.
            needle = query.casefold()
            products = [row for row in products
                        if needle in f"{row['title']} {row['description']}".casefold()]
        categories = [row[0] for row in db.execute(
            "SELECT DISTINCT category FROM products ORDER BY category")]
        return render_template("index.html", products=products, categories=categories,
                               category=category, query=query)

    @app.get("/product/<int:product_id>")
    def product(product_id: int):
        row = get_db().execute("SELECT * FROM products WHERE id = ?",
                               (product_id,)).fetchone()
        if row is None:
            abort(404)
        return render_template("product.html", product=row)

    # ── корзина ───────────────────────────────────────────────────────────
    @app.get("/cart")
    def cart_page():
        items, total = cart_details()
        return render_template("cart.html", items=items, total=total)

    @app.post("/cart/add/<int:product_id>")
    def cart_add(product_id: int):
        row = get_db().execute("SELECT * FROM products WHERE id = ?",
                               (product_id,)).fetchone()
        if row is None:
            abort(404)
        quantity = max(1, int(request.form.get("quantity") or 1))
        items = cart()
        items[str(product_id)] = min(items.get(str(product_id), 0) + quantity,
                                     max(1, row["stock"] or 1))
        save_cart(items)
        flash(f"«{row['title']}» в корзине", "ok")
        return redirect(request.form.get("next") or url_for("cart_page"))

    @app.post("/cart/update/<int:product_id>")
    def cart_update(product_id: int):
        quantity = int(request.form.get("quantity") or 0)
        items = cart()
        if quantity <= 0:
            items.pop(str(product_id), None)
        else:
            items[str(product_id)] = quantity
        save_cart(items)
        return redirect(url_for("cart_page"))

    @app.post("/cart/remove/<int:product_id>")
    def cart_remove(product_id: int):
        items = cart()
        items.pop(str(product_id), None)
        save_cart(items)
        return redirect(url_for("cart_page"))

    # ── оформление заказа ─────────────────────────────────────────────────
    @app.route("/checkout", methods=["GET", "POST"])
    def checkout():
        items, total = cart_details()
        if not items:
            flash("Корзина пуста — сначала выберите товары.", "warn")
            return redirect(url_for("index"))
        if request.method == "POST":
            customer = (request.form.get("customer") or "").strip()
            phone = (request.form.get("phone") or "").strip()
            address = (request.form.get("address") or "").strip()
            if not customer or not phone or not address:
                flash("Заполните имя, телефон и адрес.", "error")
                return render_template("checkout.html", items=items, total=total,
                                       form=request.form), 400
            db = get_db()
            cursor = db.execute(
                "INSERT INTO orders (customer, phone, address, total) VALUES (?, ?, ?, ?)",
                (customer, phone, address, total))
            order_id = cursor.lastrowid
            db.executemany(
                "INSERT INTO order_items (order_id, product_id, title, price, quantity) "
                "VALUES (?, ?, ?, ?, ?)",
                [(order_id, item["product"]["id"], item["product"]["title"],
                  item["product"]["price"], item["quantity"]) for item in items])
            db.commit()
            save_cart({})
            return redirect(url_for("order_page", order_id=order_id))
        return render_template("checkout.html", items=items, total=total, form={})

    @app.get("/order/<int:order_id>")
    def order_page(order_id: int):
        db = get_db()
        order = db.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        if order is None:
            abort(404)
        items = db.execute("SELECT * FROM order_items WHERE order_id = ?",
                           (order_id,)).fetchall()
        return render_template("order.html", order=order, items=items)

    # ── админка ───────────────────────────────────────────────────────────
    def admin_required() -> None:
        if not session.get("is_admin"):
            abort(403)

    @app.route("/admin/login", methods=["GET", "POST"])
    def admin_login():
        if request.method == "POST":
            if request.form.get("password") == app.config["ADMIN_PASSWORD"]:
                session["is_admin"] = True
                return redirect(url_for("admin"))
            flash("Неверный пароль.", "error")
            return render_template("admin_login.html"), 403
        return render_template("admin_login.html")

    @app.post("/admin/logout")
    def admin_logout():
        session.pop("is_admin", None)
        return redirect(url_for("index"))

    @app.get("/admin")
    def admin():
        admin_required()
        db = get_db()
        products = db.execute("SELECT * FROM products ORDER BY id DESC").fetchall()
        orders = db.execute("SELECT * FROM orders ORDER BY id DESC").fetchall()
        return render_template("admin.html", products=products, orders=orders)

    @app.post("/admin/products")
    def admin_add_product():
        admin_required()
        title = (request.form.get("title") or "").strip()
        if not title:
            flash("Название товара обязательно.", "error")
            return redirect(url_for("admin"))
        get_db().execute(
            "INSERT INTO products (title, category, price, description, stock) "
            "VALUES (?, ?, ?, ?, ?)",
            (title, (request.form.get("category") or "разное").strip(),
             max(0, int(request.form.get("price") or 0)),
             (request.form.get("description") or "").strip(),
             max(0, int(request.form.get("stock") or 0))))
        get_db().commit()
        flash(f"Товар «{title}» добавлен", "ok")
        return redirect(url_for("admin"))

    @app.post("/admin/products/<int:product_id>/delete")
    def admin_delete_product(product_id: int):
        admin_required()
        db = get_db()
        db.execute("DELETE FROM products WHERE id = ?", (product_id,))
        db.commit()
        flash("Товар удалён", "ok")
        return redirect(url_for("admin"))

    @app.get("/healthz")
    def healthz():
        count = get_db().execute("SELECT COUNT(*) FROM products").fetchone()[0]
        return {"status": "ok", "products": count}

    return app


app = create_app()


if __name__ == "__main__":
    # HOST/PORT — для локального запуска и для превью в песочнице.
    app.run(host=os.getenv("HOST", "127.0.0.1"), port=int(os.getenv("PORT", "8000")),
            debug=False)
