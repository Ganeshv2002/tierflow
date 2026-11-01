from dataclasses import dataclass, field


@dataclass
class Item:
    sku: str
    name: str
    quantity: int
    unit_price: float
    reorder_level: int = 5


@dataclass
class Warehouse:
    code: str
    items: dict = field(default_factory=dict)

    def add_item(self, item: Item) -> None:
        self.items[item.sku] = item

    def get(self, sku: str) -> Item:
        return self.items[sku]
