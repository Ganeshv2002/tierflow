import inspect
import unittest

from inventory import formatting, reports, stock
from inventory.models import Item


class FormattingRefactorTests(unittest.TestCase):
    def test_single_definition(self):
        self.assertEqual(formatting.fmt_money(1234.5), "$1,234.50")
        self.assertNotIn("def fmt_money", inspect.getsource(stock))
        self.assertNotIn("def fmt_money", inspect.getsource(reports))

    def test_behaviour_unchanged(self):
        self.assertEqual(stock.stock_value_line(Item("S1", "Bolt", 4, 2.5)), "S1: $10.00")
