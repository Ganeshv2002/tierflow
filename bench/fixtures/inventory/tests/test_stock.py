import unittest

from inventory import cache
from inventory.models import Item, Warehouse
from inventory.stock import add_stock, low_stock_items, remove_stock


class StockTests(unittest.TestCase):
    def setUp(self):
        cache.clear()
        self.wh = Warehouse("A")
        self.wh.add_item(Item("S1", "Bolt", 3, 0.5))
        self.wh.add_item(Item("S2", "Nut", 50, 0.1))

    def test_low_stock(self):
        self.assertEqual([i.sku for i in low_stock_items(self.wh)], ["S1"])

    def test_add_remove(self):
        add_stock(self.wh, "S1", 5)
        remove_stock(self.wh, "S1", 2)
        self.assertEqual(self.wh.get("S1").quantity, 6)

    def test_remove_too_much(self):
        with self.assertRaises(ValueError):
            remove_stock(self.wh, "S2", 999)
