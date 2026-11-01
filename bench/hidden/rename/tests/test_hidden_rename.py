import pathlib
import unittest

from inventory import pricing, reports


class RenameTests(unittest.TestCase):
    def test_new_name_exists(self):
        self.assertTrue(callable(getattr(pricing, "calculate_total", None)))
        self.assertFalse(hasattr(pricing, "calc_total"))
        self.assertIs(reports.calculate_total, pricing.calculate_total)

    def test_no_old_references(self):
        root = pathlib.Path(__file__).resolve().parents[1]
        files = list((root / "inventory").rglob("*.py")) + list((root / "tests").glob("test_*.py"))
        offenders = [str(p) for p in files if "hidden" not in p.name and "calc_total" in p.read_text(encoding="utf-8")]
        self.assertEqual(offenders, [])
