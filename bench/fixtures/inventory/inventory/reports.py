from .models import Warehouse
from .pricing import calc_total
from .stock import low_stock_items, quantity_on_hand


def fmt_money(value: float) -> str:
    return f"${value:,.2f}"


def inventory_report(warehouse: Warehouse) -> str:
    lines = [f"Warehouse {warehouse.code}"]
    for sku in sorted(warehouse.items):
        item = warehouse.get(sku)
        qty = quantity_on_hand(warehouse, sku)
        lines.append(f"{sku} {item.name} qty={qty} value={fmt_money(calc_total(item, qty))}")
    return "\n".join(lines)


def reorder_report(warehouse: Warehouse) -> str:
    items = low_stock_items(warehouse)
    if not items:
        return "Nothing to reorder"
    return "\n".join(f"REORDER {i.sku} ({i.quantity}/{i.reorder_level})" for i in items)
