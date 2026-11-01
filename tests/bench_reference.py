"""Reference solutions for the bundled benchmark tasks. Used to prove the hidden checks are sound
(they fail on the untouched fixture and pass on a correct change) and by the fake claude CLI."""
from pathlib import Path


def _sub(path: Path, old: str, new: str, count: int = -1):
    text = path.read_text(encoding="utf-8")
    assert old in text, f"{old!r} not in {path}"
    path.write_text(text.replace(old, new, count), encoding="utf-8")


SEARCH_ANSWER = "low_stock_items in inventory/stock.py"


def apply(task_id: str, ws: Path) -> None:
    inv = ws / "inventory"
    if task_id == "rename":
        for p in [inv / "pricing.py", inv / "reports.py", ws / "tests" / "test_pricing.py"]:
            _sub(p, "calc_total", "calculate_total")
    elif task_id == "bugfix":
        _sub(inv / "pricing.py", "price * (1 - BULK_DISCOUNT) * (1 - BULK_DISCOUNT)", "price * (1 - BULK_DISCOUNT)")
    elif task_id == "feature":
        with open(inv / "reports.py", "a", encoding="utf-8") as fh:
            fh.write("\n\ndef to_csv(warehouse) -> str:\n    rows = ['sku,name,quantity,unit_price']\n"
                     "    for sku in sorted(warehouse.items):\n        i = warehouse.items[sku]\n"
                     "        rows.append(f'{i.sku},{i.name},{i.quantity},{i.unit_price:.2f}')\n    return '\\n'.join(rows)\n")
    elif task_id == "refactor":
        (inv / "formatting.py").write_text('def fmt_money(value: float) -> str:\n    return f"${value:,.2f}"\n', encoding="utf-8")
        for p in (inv / "stock.py", inv / "reports.py"):
            _sub(p, 'def fmt_money(value: float) -> str:\n    return f"${value:,.2f}"\n', "from .formatting import fmt_money\n")
    elif task_id == "debug":
        _sub(inv / "cache.py", "def wrapper(warehouse, sku):\n        key = sku", "def wrapper(warehouse, sku):\n        key = (warehouse.code, sku)")
        _sub(inv / "cache.py", "def invalidate(sku):\n    _CACHE.pop(sku, None)",
             "def invalidate(sku, warehouse_code=None):\n    for k in [k for k in _CACHE if k[1] == sku and warehouse_code in (None, k[0])]:\n        _CACHE.pop(k, None)")
    elif task_id == "architecture":
        (inv / "pricing.py").write_text(
            "BULK_THRESHOLD = 10\nBULK_DISCOUNT = 0.10\nMEMBER_DISCOUNT = 0.05\n\n\n"
            "class PricingEngine:\n    def __init__(self):\n        self.rules = []\n\n"
            "    def add_rule(self, rule):\n        self.rules.append(rule)\n        return self\n\n"
            "    def total(self, item, qty, member=False):\n        price = item.unit_price * qty\n"
            "        for rule in self.rules:\n            price = rule(item, qty, member, price)\n        return round(price, 2)\n\n\n"
            "def bulk_rule(item, qty, member, price):\n"
            "    return price * (1 - BULK_DISCOUNT) * (1 - BULK_DISCOUNT) if qty >= BULK_THRESHOLD else price\n\n\n"
            "def member_rule(item, qty, member, price):\n    return price * (1 - MEMBER_DISCOUNT) if member else price\n\n\n"
            "def default_engine():\n    return PricingEngine().add_rule(bulk_rule).add_rule(member_rule)\n\n\n"
            "def calc_total(item, qty, member=False):\n    return default_engine().total(item, qty, member)\n", encoding="utf-8")
