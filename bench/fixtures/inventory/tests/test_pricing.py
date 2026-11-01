import unittest

from inventory.models import Item
from inventory.pricing import calc_total


class PricingTests(unittest.TestCase):
    def test_simple(self):
        self.assertEqual(calc_total(Item("S1", "Bolt", 0, 2.0), 3), 6.0)

    def test_member(self):
        self.assertEqual(calc_total(Item("S1", "Bolt", 0, 2.0), 5, member=True), 9.5)

    def test_bulk_discount(self):
        # 10 units at $2.00 with a 10% bulk discount
        self.assertEqual(calc_total(Item("S1", "Bolt", 0, 2.0), 10), 18.0)
