from __future__ import annotations

import unittest

from app.catalog import load_catalog


class CatalogTests(unittest.TestCase):
    def test_first_65_labels_are_loaded(self) -> None:
        labels = load_catalog()
        self.assertEqual(65, len(labels))
        self.assertEqual("H001", labels[0].id)
        self.assertEqual("H065", labels[-1].id)
        self.assertTrue(labels[0].category)
        self.assertTrue(labels[0].name)


if __name__ == "__main__":
    unittest.main()

