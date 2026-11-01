# Codebase Map

| Path | What it is |
|---|---|
| `inventory/models.py` | `Item`, `Warehouse` dataclasses |
| `inventory/stock.py` | stock movements, low-stock query, cached quantity lookup |
| `inventory/pricing.py` | price calculation with bulk and member discounts |
| `inventory/reports.py` | text reports built from stock + pricing |
| `inventory/cache.py` | memoization used by stock lookups |
| `tests/` | unittest suite (`python -m unittest discover -s tests`) |
