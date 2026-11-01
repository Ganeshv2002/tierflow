import unittest

from inventory.models import Item, Warehouse
from inventory.reports import to_csv


class CsvTests(unittest.TestCase):
    def test_csv(self):
        wh = Warehouse("A")
        wh.add_item(Item("S2", "Nut", 50, 0.1))
        wh.add_item(Item("S1", "Bolt", 3, 0.5))
        lines = to_csv(wh).strip().splitlines()
        self.assertEqual(lines[0].strip(), "sku,name,quantity,unit_price")
        self.assertEqual(lines[1].strip(), "S1,Bolt,3,0.50")
        self.assertEqual(lines[2].strip(), "S2,Nut,50,0.10")
        self.assertEqual(len(lines), 3)

    def test_empty(self):
        self.assertEqual(to_csv(Warehouse("B")).strip(), "sku,name,quantity,unit_price")
