import inspect
import unittest

from inventory import pricing
from inventory.models import Item


class PricingEngineTests(unittest.TestCase):
    def setUp(self):
        self.item = Item("S1", "Bolt", 0, 2.0)

    def test_default_behaviour_kept(self):
        self.assertEqual(pricing.calc_total(self.item, 3), 6.0)
        self.assertEqual(pricing.calc_total(self.item, 5, member=True), 9.5)
        self.assertEqual(pricing.default_engine().total(self.item, 12), pricing.calc_total(self.item, 12))

    def test_custom_rule_in_order(self):
        engine = pricing.PricingEngine()
        engine.add_rule(lambda item, qty, member, price: price - 1)
        engine.add_rule(lambda item, qty, member, price: price * 2)
        self.assertEqual(engine.total(self.item, 3), 10.0)   # (6 - 1) * 2

    def test_no_hardcoded_branching_in_calc_total(self):
        src = inspect.getsource(pricing.calc_total)
        self.assertNotIn("BULK_THRESHOLD", src)
        self.assertNotIn("MEMBER_DISCOUNT", src)
