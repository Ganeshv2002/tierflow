"""Aggregate the metrics log into a usage/cost report."""
from __future__ import annotations

from collections import Counter, defaultdict

from .ledger import read_metrics


def build_report(since_days: float | None = None, session: str | None = None) -> dict:
    events = [e for e in read_metrics(since_days) if not session or e.get("session") == session]
    last_turn: dict[str, dict] = {}
    for e in events:
        if e["event"] == "turn":
            last_turn[e["session"]] = e
    models: dict[str, Counter] = defaultdict(Counter)
    model_cost: dict[str, float | None] = {}
    for t in last_turn.values():
        for m, u in (t.get("models") or {}).items():
            for k in ("requests", "input", "output", "cache_read", "cache_write"):
                models[m][k] += u.get(k) or 0
            if u.get("thinking") is not None:
                models[m]["thinking"] += u["thinking"]
            c = u.get("cost_usd")
            model_cost[m] = None if (c is None or model_cost.get(m, 0.0) is None) else model_cost.get(m, 0.0) + c

    by_agent: dict[str, Counter] = defaultdict(Counter)
    agent_cost: dict[str, float] = defaultdict(float)
    for e in events:
        if e["event"] == "delegation":
            by_agent[e.get("agent", "?")]["delegations"] += 1
            if e.get("decision") in ("ask", "deny"):
                by_agent[e.get("agent", "?")][f"gated_{e['decision']}"] += 1
        elif e["event"] == "agent_result":
            a = by_agent[e.get("agent", "?")]
            a[f"status_{e.get('status')}"] += 1
            if e.get("advice") in ("retry", "escalate", "reassess", "stop"):
                a[e["advice"]] += 1
        elif e["event"] == "subagent_usage" and e.get("usage"):
            agent_cost[e.get("agent") or "?"] += e["usage"].get("cost_usd") or 0.0
            by_agent[e.get("agent") or "?"]["requests"] += sum(m.get("requests", 0) for m in e["usage"].get("models", {}).values())

    # per task: main-session cost = growth of cost_main between consecutive turns; steps = agent results
    tasks: dict[tuple, dict] = {}
    prev_main: dict[str, float] = {}
    for e in events:
        sess = e.get("session")
        if e["event"] == "prompt":
            tasks.setdefault((sess, e.get("task")), {"category": e.get("category"), "tier": e.get("tier"),
                                                     "main_usd": 0.0, "steps": []})
        elif e["event"] == "turn":
            t = tasks.setdefault((sess, e.get("task")), {"category": e.get("category"), "tier": None, "main_usd": 0.0, "steps": []})
            cur = e.get("cost_main") or 0.0
            t["main_usd"] += max(0.0, cur - prev_main.get(sess, 0.0))
            prev_main[sess] = cur
        elif e["event"] == "agent_result" and e.get("task"):
            t = tasks.setdefault((sess, e["task"]), {"category": None, "tier": None, "main_usd": 0.0, "steps": []})
            t["steps"].append({k: e.get(k) for k in ("agent", "tier", "status", "advice", "cost_usd", "duration_ms")})

    categories = Counter(e.get("category") for e in events if e["event"] == "prompt")
    tiers = Counter(e.get("tier") for e in events if e["event"] == "prompt")
    validations = Counter("pass" if e.get("ok") else "fail" for e in events if e["event"] == "validation")
    return {
        "sessions": len(last_turn),
        "cost_total_usd": round(sum(t.get("cost_total", 0) for t in last_turn.values()), 4),
        "cost_main_usd": round(sum(t.get("cost_main", 0) for t in last_turn.values()), 4),
        "cost_subagents_usd": round(sum(t.get("cost_subagents", 0) for t in last_turn.values()), 4),
        "models": {m: {**dict(c), "cost_usd": None if model_cost.get(m) is None else round(model_cost[m], 4)} for m, c in models.items()},
        "agents": {a: {**dict(c), "cost_usd": round(agent_cost.get(a, 0.0), 4)} for a, c in by_agent.items()},
        "prompt_categories": dict(categories),
        "suggested_tiers": dict(tiers),
        "validations": dict(validations),
        "unknown_models": sorted({m for t in last_turn.values() for m in t.get("unknown_models", [])}),
        "tasks": [{"session": s, "task": k, **v, "main_usd": round(v["main_usd"], 4),
                   "steps_usd": round(sum(st.get("cost_usd") or 0 for st in v["steps"]), 4)} for (s, k), v in tasks.items()],
    }


def _step_text(s: dict) -> str:
    name = (s.get("agent") or "?").rsplit(":", 1)[-1]
    cost = "?" if s.get("cost_usd") is None else f"{s['cost_usd']:.3f}"
    secs = (s.get("duration_ms") or 0) / 1000
    return f"{name}: {s.get('status')} ${cost} {secs:.0f}s"


def render(rep: dict) -> str:
    out = [f"sessions: {rep['sessions']}   cost (API list-price estimate): ${rep['cost_total_usd']:.2f} "
           f"(main ${rep['cost_main_usd']:.2f}, subagents ${rep['cost_subagents_usd']:.2f})"]
    if rep["models"]:
        out.append("\nmodel                     requests      input     output   cache_rd   cache_wr   thinking     cost")
        for m, u in sorted(rep["models"].items(), key=lambda kv: -(kv[1].get("cost_usd") or 0)):
            th = u.get("thinking")
            cost = "unknown" if u.get("cost_usd") is None else f"${u['cost_usd']:.2f}"
            out.append(f"{m[:25]:25} {u.get('requests', 0):8} {u.get('input', 0):10} {u.get('output', 0):10} "
                       f"{u.get('cache_read', 0):10} {u.get('cache_write', 0):10} {th if th is not None else 'n/a':>10} {cost:>8}")
    if rep["agents"]:
        out.append("\nagent                      delegations  ok  failed  retries  escalations  gated   cost")
        for a, c in sorted(rep["agents"].items()):
            ok = c.get("status_done", 0)
            failed = sum(v for k, v in c.items() if k.startswith("status_") and k not in ("status_done", "status_unknown"))
            gated = c.get("gated_ask", 0) + c.get("gated_deny", 0)
            out.append(f"{a[:26]:26} {c.get('delegations', 0):11} {ok:3} {failed:7} {c.get('retry', 0):8} "
                       f"{c.get('escalate', 0) + c.get('reassess', 0):12} {gated:6}  ${c.get('cost_usd', 0):.2f}")
    if rep.get("tasks"):
        out.append("\ntask                         class        main $   steps $   steps (agent: status $ s)")
        for t in rep["tasks"][-20:]:
            steps = ", ".join(_step_text(s) for s in t["steps"]) or "-"
            out.append(f"{(str(t['session'])[:8] + '/' + str(t['task']))[:28]:28} {str(t['category'])[:12]:12} "
                       f"{t['main_usd']:7.3f}  {t['steps_usd']:8.3f}   {steps}")
    if rep["prompt_categories"]:
        out.append(f"\nprompt classes: {rep['prompt_categories']}   suggested tiers: {rep['suggested_tiers']}")
    if rep["validations"]:
        out.append(f"validations: {rep['validations']}")
    if rep["unknown_models"]:
        out.append(f"models without a price (cost excluded): {', '.join(rep['unknown_models'])}")
    return "\n".join(out)
