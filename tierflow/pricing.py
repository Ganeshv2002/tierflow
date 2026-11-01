"""Price token usage from the configured table. Unknown models are reported, never guessed."""
from __future__ import annotations

from .config import Config


def rates_for(model: str, cfg: Config) -> dict | None:
    table = cfg.section("pricing")
    best = None
    for prefix in table:
        if model.startswith(prefix) and (best is None or len(prefix) > len(best)):
            best = prefix
    return table.get(best) if best else None


def cost_of(model: str, u: dict, cfg: Config) -> float | None:
    """u keys: input, output, cache_read, cache_write_5m, cache_write_1h (token counts).
    Long-context surcharges are applied per request when the table declares a threshold, so callers
    pass per-request usage when they want that precision (usage.py does)."""
    r = rates_for(model, cfg)
    if r is None:
        return None
    inp, out = r["input"], r["output"]
    thr = r.get("long_context_threshold")
    total_in = u.get("input", 0) + u.get("cache_read", 0) + u.get("cache_write_5m", 0) + u.get("cache_write_1h", 0)
    if thr and total_in > thr:
        inp, out = r.get("long_context_input", inp), r.get("long_context_output", out)
    scale = inp / r["input"] if r["input"] else 1.0
    return (
        u.get("input", 0) * inp
        + u.get("output", 0) * out
        + u.get("cache_read", 0) * r.get("cache_read", r["input"] * 0.1) * scale
        + u.get("cache_write_5m", 0) * r.get("cache_write_5m", r["input"] * 1.25) * scale
        + u.get("cache_write_1h", 0) * r.get("cache_write_1h", r["input"] * 2.0) * scale
    ) / 1_000_000
