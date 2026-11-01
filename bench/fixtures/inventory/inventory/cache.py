"""Small memoization helpers for hot inventory lookups."""
_CACHE = {}


def memoize_by_sku(fn):
    def wrapper(warehouse, sku):
        key = sku
        if key not in _CACHE:
            _CACHE[key] = fn(warehouse, sku)
        return _CACHE[key]

    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


def invalidate(sku):
    _CACHE.pop(sku, None)


def clear():
    _CACHE.clear()
