"""Budget decisions.

Exact pre-call enforcement is impossible here (a subagent's cost is unknown until it finishes), so
each delegation reserves a conservative per-tier amount. The gate runs at delegation time
(PreToolUse on the Agent tool) and at prompt start (warning only). Main-session turns cannot be
stopped mid-turn by a hook; that limitation is documented in docs/ARCHITECTURE.md §7.
"""
from __future__ import annotations

from dataclasses import dataclass

from .config import Config


@dataclass
class BudgetDecision:
    action: str          # allow | warn | ask | deny
    reason: str
    session_spent: float
    task_spent: float
    reservation: float

    def line(self, cfg: Config) -> str:
        b = cfg.section("budgets")
        return (f"budget: task ${self.task_spent:.2f}/${b['max_cost_per_task_usd']:.2f}, "
                f"session ${self.session_spent:.2f}/${b['max_cost_per_session_usd']:.2f} (API list-price estimate)")


def check(cfg: Config, session_spent: float, task_spent: float, tier: str | None) -> BudgetDecision:
    b = cfg.section("budgets")
    reservation = float(b.get("reservation_usd", {}).get(tier or "", 0.0))
    enforcement = b.get("enforcement", "ask")
    over = []
    if task_spent + reservation > b["max_cost_per_task_usd"]:
        over.append(f"task ${task_spent:.2f} + reserve ${reservation:.2f} > limit ${b['max_cost_per_task_usd']:.2f}")
    if session_spent + reservation > b["max_cost_per_session_usd"]:
        over.append(f"session ${session_spent:.2f} + reserve ${reservation:.2f} > limit ${b['max_cost_per_session_usd']:.2f}")
    if not over or enforcement == "off":
        return BudgetDecision("allow", "", session_spent, task_spent, reservation)
    if tier in b.get("exempt_tiers", ["light"]) and enforcement in ("ask", "deny"):
        # blocking a cheap delegation would push the work back into the (pricier) main session
        return BudgetDecision("warn", "tierflow budget: " + "; ".join(over) + f" (cheap {tier} tier not blocked)",
                              session_spent, task_spent, reservation)
    return BudgetDecision(enforcement, "tierflow budget: " + "; ".join(over), session_spent, task_spent, reservation)
