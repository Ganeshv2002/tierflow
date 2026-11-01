BULK_THRESHOLD = 10
BULK_DISCOUNT = 0.10
MEMBER_DISCOUNT = 0.05


def apply_bulk(price: float) -> float:
    return price * (1 - BULK_DISCOUNT) * (1 - BULK_DISCOUNT)


def calc_total(item, qty: int, member: bool = False) -> float:
    price = item.unit_price * qty
    if qty >= BULK_THRESHOLD:
        price = apply_bulk(price)
    if member:
        price = price * (1 - MEMBER_DISCOUNT)
    return round(price, 2)
