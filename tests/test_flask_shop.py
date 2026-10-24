"""Сквозная проверка примера магазина: каталог → корзина → заказ → админка.

Сервер не запускается: используется ``app.test_client()``, поэтому тест быстрый
и работает там же, где и остальные (без портов и фоновых процессов).
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

SHOP_DIR = Path(__file__).resolve().parent.parent / "examples" / "flask_shop"


@pytest.fixture()
def shop(tmp_path, monkeypatch):
    """Приложение с отдельной базой на каждый тест."""
    monkeypatch.setenv("SHOP_ADMIN_PASSWORD", "secret")
    # Модуль подгружаем по пути: examples/ — не пакет.
    sys.path.insert(0, str(SHOP_DIR))
    try:
        spec = importlib.util.spec_from_file_location("shop_app", SHOP_DIR / "app.py")
        module = importlib.util.module_from_spec(spec)
        # Flask ищет шаблоны относительно модуля — на время создания приложения
        # он должен быть в sys.modules, иначе root_path = текущая папка.
        sys.modules["shop_app"] = module
        try:
            spec.loader.exec_module(module)
            app = module.create_app(tmp_path / "shop.db")
        finally:
            sys.modules.pop("shop_app", None)
        app.config.update(TESTING=True)
        return app
    finally:
        sys.path.pop(0)


def _client(shop):
    return shop.test_client()


def _products(db_path: Path):
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        return connection.execute("SELECT * FROM products ORDER BY id").fetchall()


def test_catalog_lists_seeded_products(shop):
    page = _client(shop).get("/")
    body = page.get_data(as_text=True)
    assert page.status_code == 200
    assert "Каталог" in body
    assert "Кофе Ethiopia" in body, "демо-товары должны быть засеяны"
    assert "1 450 c." in body, "цены форматируются с пробелом и валютой"


def test_catalog_filter_and_search(shop):
    client = _client(shop)
    by_category = client.get("/?category=посуда").get_data(as_text=True)
    assert "Чайник" in by_category and "Кофе Ethiopia" not in by_category

    by_query = client.get("/?q=кофе").get_data(as_text=True)
    assert "Кофе Ethiopia" in by_query and "Кружка" not in by_query

    empty = client.get("/?q=нет-такого").get_data(as_text=True)
    assert "Ничего не найдено" in empty


def test_product_page_and_404(shop):
    client = _client(shop)
    page = client.get("/product/1")
    assert page.status_code == 200
    assert "Добавить в корзину" in page.get_data(as_text=True)
    assert client.get("/product/9999").status_code == 404


def test_cart_add_update_and_remove(shop):
    client = _client(shop)
    client.post("/cart/add/1", data={"quantity": "2"})

    page = client.get("/cart").get_data(as_text=True)
    assert "Кофе Ethiopia" in page
    assert "1 580 c." in page, "2 × 790 = 1580"

    client.post("/cart/update/1", data={"quantity": "1"})
    assert "790 c." in client.get("/cart").get_data(as_text=True)

    client.post("/cart/remove/1")
    assert "Корзина пуста" in client.get("/cart").get_data(as_text=True)


def test_quantity_is_capped_by_stock(shop):
    client = _client(shop)
    # У чайника склад 5 — больше в корзину не попадёт.
    client.post("/cart/add/2", data={"quantity": "50"})
    body = client.get("/cart").get_data(as_text=True)
    assert "7 250 c." in body, "5 × 1450"


def test_checkout_creates_order_and_clears_cart(shop):
    client = _client(shop)
    client.post("/cart/add/1", data={"quantity": "1"})
    client.post("/cart/add/3", data={"quantity": "2"})

    page = client.post("/checkout", data={
        "customer": "Азиз", "phone": "+992 900 000 000", "address": "Худжанд, ул. Ленина 1",
    }, follow_redirects=False)
    assert page.status_code == 302
    location = page.headers["Location"]
    assert "/order/" in location

    order_page = client.get(location).get_data(as_text=True)
    assert "Заказ №1 принят" in order_page
    assert "Азиз" in order_page and "Худжанд" in order_page
    assert "1 970 c." in order_page, "790 + 2 × 590"

    # Корзина очищена, а заказ и позиции остались в базе.
    assert "Корзина пуста" in client.get("/cart").get_data(as_text=True)
    with sqlite3.connect(shop.config["DATABASE"]) as connection:
        order = connection.execute("SELECT * FROM orders").fetchone()
        items = connection.execute("SELECT * FROM order_items").fetchall()
    assert order[4] == 1970
    assert len(items) == 2


def test_checkout_requires_fields(shop):
    client = _client(shop)
    client.post("/cart/add/1")
    response = client.post("/checkout", data={"customer": "Азиз"})
    assert response.status_code == 400
    assert "Заполните" in response.get_data(as_text=True)


def test_checkout_with_empty_cart_redirects(shop):
    response = _client(shop).get("/checkout")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/")


def test_admin_requires_password(shop):
    client = _client(shop)
    assert client.get("/admin").status_code == 403
    assert client.post("/admin/products", data={"title": "Хак"}).status_code == 403

    assert client.post("/admin/login", data={"password": "неверный"}).status_code == 403
    login = client.post("/admin/login", data={"password": "secret"})
    assert login.status_code == 302
    assert client.get("/admin").status_code == 200


def test_admin_add_and_delete_product(shop):
    client = _client(shop)
    client.post("/admin/login", data={"password": "secret"})
    client.post("/admin/products", data={
        "title": "Термокружка", "category": "посуда", "price": "1200",
        "description": "Держит тепло 6 часов", "stock": "3",
    })
    titles = [row["title"] for row in _products(shop.config["DATABASE"])]
    assert "Термокружка" in titles
    assert "Термокружка" in client.get("/?category=посуда").get_data(as_text=True)

    with sqlite3.connect(shop.config["DATABASE"]) as connection:
        new_id = connection.execute(
            "SELECT id FROM products WHERE title = 'Термокружка'").fetchone()[0]
    client.post(f"/admin/products/{new_id}/delete")
    assert "Термокружка" not in [row["title"] for row in _products(shop.config["DATABASE"])]


def test_admin_sees_orders(shop):
    client = _client(shop)
    client.post("/cart/add/1")
    client.post("/checkout", data={"customer": "Гулнора", "phone": "999", "address": "Душанбе"})
    client.post("/admin/login", data={"password": "secret"})
    assert "Гулнора" in client.get("/admin").get_data(as_text=True)


def test_healthz_reports_database(shop):
    payload = _client(shop).get("/healthz").get_json()
    assert payload["status"] == "ok" and payload["products"] >= 3


def test_pages_have_no_unrendered_template_syntax(shop):
    """Дешёвая проверка «все шаблоны компилируются и рендерятся»."""
    client = _client(shop)
    client.post("/cart/add/1")
    client.post("/admin/login", data={"password": "secret"})
    for url in ("/", "/cart", "/checkout", "/product/1", "/admin", "/healthz"):
        body = client.get(url).get_data(as_text=True)
        assert "{{" not in body and "{%" not in body, f"незакрытый шаблон на {url}"
