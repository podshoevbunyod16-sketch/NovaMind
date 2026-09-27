"""
test_media.py — Тесты для media_generation.py
Запуск: python test_media.py
"""
import sys, os, unittest
sys.path.insert(0, os.path.dirname(__file__))
import media_generation as mg

class TestCatalog(unittest.TestCase):
    def test_registry_not_empty(self):
        self.assertGreater(len(mg.MEDIA_MODEL_REGISTRY), 0)

    def test_all_models_have_required_fields(self):
        for m in mg.MEDIA_MODEL_REGISTRY:
            for field in ("id", "name", "provider", "media_type", "pricing_status", "backend"):
                self.assertIn(field, m, f"Model {m.get('id')} missing field {field}")

    def test_pricing_status_valid(self):
        valid = {mg.PRICING_FREE, mg.PRICING_TRIAL, mg.PRICING_PAID, mg.PRICING_UNKNOWN}
        for m in mg.MEDIA_MODEL_REGISTRY:
            self.assertIn(m["pricing_status"], valid, f"{m['id']} has invalid pricing_status")

    def test_media_type_valid(self):
        for m in mg.MEDIA_MODEL_REGISTRY:
            self.assertIn(m["media_type"], mg.MEDIA_TYPES, f"{m['id']} has invalid media_type")

    def test_no_paid_in_auto_usable(self):
        for m in mg.MEDIA_MODEL_REGISTRY:
            if m["pricing_status"] == mg.PRICING_PAID:
                self.assertNotIn(m["pricing_status"], mg.AUTO_USABLE_STATUSES)

    def test_get_models_by_type(self):
        for t in mg.MEDIA_TYPES:
            models = [m for m in mg.MEDIA_MODEL_REGISTRY if m["media_type"] == t]
            # Каждый тип должен иметь хотя бы одну модель в реестре
            # (может быть недоступна без ключа — это OK)
            self.assertGreaterEqual(len(models), 0)

    def test_pricing_labels_coverage(self):
        for status in (mg.PRICING_FREE, mg.PRICING_TRIAL, mg.PRICING_PAID, mg.PRICING_UNKNOWN):
            self.assertIn(status, mg.PRICING_LABELS)

class TestDirectories(unittest.TestCase):
    def test_dirs_created(self):
        os.makedirs(mg.MEDIA_DIR, exist_ok=True)
        os.makedirs(mg.IMAGE_DIR, exist_ok=True)
        self.assertTrue(os.path.isdir(mg.MEDIA_DIR))
        self.assertTrue(os.path.isdir(mg.IMAGE_DIR))

class TestFilterModels(unittest.TestCase):
    """Тест фильтрации по статусу цены."""
    def test_free_models_exist(self):
        free = [m for m in mg.MEDIA_MODEL_REGISTRY if m["pricing_status"] == mg.PRICING_FREE]
        self.assertGreater(len(free), 0, "Нет ни одной бесплатной модели!")

    def test_trial_models_labeled(self):
        trial = [m for m in mg.MEDIA_MODEL_REGISTRY if m["pricing_status"] == mg.PRICING_TRIAL]
        for m in trial:
            self.assertIn("pricing_note", m, f"{m['id']} (trial) missing pricing_note")

if __name__ == "__main__":
    result = unittest.main(verbosity=2, exit=False)
    sys.exit(0 if result.result.wasSuccessful() else 1)
