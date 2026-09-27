"""
test_search.py — Тесты для routes/search.py (функции без Flask контекста)
Запуск: python test_search.py
"""
import sys, os, unittest
sys.path.insert(0, os.path.dirname(__file__))

# Тестируем только чистые функции без Flask
from routes.search import fetch_page_text, search_web

class TestFetchPageText(unittest.TestCase):
    def test_fetch_real_page(self):
        text, err = fetch_page_text("https://example.com", max_chars=2000)
        if err:
            self.skipTest(f"Нет сети: {err}")
        self.assertIsNotNone(text)
        self.assertGreater(len(text), 50)

    def test_fetch_nonexistent(self):
        text, err = fetch_page_text("https://thisdoesnotexist.novamind.ai")
        self.assertIsNone(text)
        self.assertIsNotNone(err)

    def test_no_html_tags_in_result(self):
        text, err = fetch_page_text("https://example.com", max_chars=5000)
        if err:
            self.skipTest(f"Нет сети: {err}")
        import re
        tags = re.findall(r"<[a-zA-Z][^>]*>", text or "")
        self.assertEqual(len(tags), 0, f"Найдены HTML теги: {tags[:3]}")

class TestSearchWeb(unittest.TestCase):
    def test_duckduckgo_fallback(self):
        # DuckDuckGo без ключа
        os.environ.pop("APILAYER_KEY", None)
        os.environ.pop("SERPER_KEY", None)
        results, err = search_web("Python programming", num=3)
        # Результаты могут быть пустыми без сети, но не должно падать
        self.assertIsInstance(results, list)
        self.assertIsInstance(err, (str, type(None)))

if __name__ == "__main__":
    result = unittest.main(verbosity=2, exit=False)
    sys.exit(0 if result.result.wasSuccessful() else 1)
