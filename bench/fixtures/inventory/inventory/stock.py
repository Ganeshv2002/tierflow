from . import cache
from .models import Item, Warehouse


def fmt_money(value: float) -> str:
    return f"${value:,.2f}"


def add_stock(warehouse: Warehouse, sku: str, qty: int) -> None:
    if qty <= 0:
        raise ValueError("qty must be positive")
    warehouse.get(sku).quantity += qty
    cache.invalidate(sku)


def remove_stock(warehouse: Warehouse, sku: str, qty: int) -> None:
    item = warehouse.get(sku)
    if qty > item.quantity:
        raise ValueError(f"insufficient stock for {sku}")
    item.quantity -= qty
    cache.invalidate(sku)


def low_stock_items(warehouse: Warehouse) -> list:
    """Items whose quantity is below their reorder level, sorted by SKU."""
    return sorted((i for i in warehouse.items.values() if i.quantity < i.reorder_level), key=lambda i: i.sku)


@cache.memoize_by_sku
def quantity_on_hand(warehouse: Warehouse, sku: str) -> int:
    return warehouse.get(sku).quantity


def stock_value_line(item: Item) -> str:
    return f"{item.sku}: {fmt_money(item.quantity * item.unit_price)}"
