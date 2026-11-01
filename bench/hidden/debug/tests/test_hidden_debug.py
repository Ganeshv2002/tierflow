import unittest

from inventory import cache
from inventory.models import Item, Warehouse
from inventory.reports import inventory_report
from inventory.stock import add_stock, quantity_on_hand


class MultiWarehouseTests(unittest.TestCase):
    def setUp(self):
        cache.clear()
        self.a, self.b = Warehouse("A"), Warehouse("B")
        self.a.add_item(Item("S1", "Bolt", 3, 1.0))
        self.b.add_item(Item("S1", "Bolt", 40, 1.0))

    def test_quantities_per_warehouse(self):
        self.assertEqual(quantity_on_hand(self.a, "S1"), 3)
        self.assertEqual(quantity_on_hand(self.b, "S1"), 40)
        self.assertIn("qty=40", inventory_report(self.b))

    def test_invalidation_still_works(self):
        self.assertEqual(quantity_on_hand(self.a, "S1"), 3)
        add_stock(self.a, "S1", 2)
        self.assertEqual(quantity_on_hand(self.a, "S1"), 5)
        self.assertEqual(quantity_on_hand(self.b, "S1"), 40)
