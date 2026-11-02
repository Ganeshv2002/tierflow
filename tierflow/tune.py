"""orch tune: learn from tierflow's own metrics and suggest config changes.

Nothing changes on its own. `orch tune` shows the evidence and numbered suggestions; `orch tune --apply <id>`
writes the chosen ones to ~/.claude/tierflow/tuned.toml, which only accepts a short whitelist of keys
(config.TUNABLE) and sits below your own config.toml. `orch tune --revert` deletes it.

Comparisons are made like for like: cost per prompt depends far more on how big the session's context
already is than on the kind of task, so prompts are grouped by context size before anything is compared,
and a suggestion needs `tune.min_samples` prompts per group and a `tune.min_difference` cost gap.
"""
from __future__ import annotations

import json
import statistics
import tomllib
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from .config import TUNABLE, Config, home_dir, tuned_config_path
from .ledger import read_metrics

BUCKETS = ((50_000, "<50k"), (200_000, "50-200k"), (None, "200k+"))


def bucket(ctx: int | None) -> str:
    if ctx is None:
        return "unknown"
    for limit, name in BUCKETS:
        if limit is None or ctx < limit:
            return name
    return "200k+"


def _agent_name(a: str | None) -> str:
    return (a or "").split(":")[-1]


def prompt_rows(events: list[dict]) -> list[dict]:
    """One row per user prompt: what it was classified as, how big the context was when it started, what
    it cost until the next prompt (all Stop events in between), which agents it used and how it ended."""
    by_session: dict[str, list[dict]] = defaultdict(list)
    for e in events:
        if e.get("session"):
            by_session[e["session"]].append(e)
    rows: list[dict] = []
    for sid, evs in by_session.items():
        evs.sort(key=lambda e: e.get("ts", ""))
        last_total, cur, srows = None, None, []
        for e in evs:
            ev = e.get("event")
            if ev == "prompt":
                cur = {"session": sid, "task": e.get("task"), "category": e.get("category"),
                       "complexity": e.get("complexity"), "main_model": e.get("main_model"),
                       "context_tokens": e.get("context_tokens"), "follow_up": e.get("follow_up"),
                       "new_task": e.get("new_task"), "hint": e.get("hint"), "hinted_agent": e.get("hinted_agent"),
                       "cost": 0.0, "requests": 0, "turns": 0, "agents": [], "agent_failed": False,
                       "validation": None, "outcome": "unknown"}
                srows.append(cur)
                if last_total is None and isinstance(e.get("session_cost"), (int, float)):
                    # first prompt we saw in this session: its starting cost is the baseline (requests are
                    # only known from zero, i.e. for a session that starts here)
                    last_total = (float(e["session_cost"]), 0 if not e["session_cost"] else None)
            elif ev == "turn":
                total, req = e.get("cost_total"), e.get("requests") or 0
                if last_total is not None and cur is not None and total is not None:
                    cur["cost"] += max(0.0, total - last_total[0])
                    if last_total[1] is not None:
                        cur["requests"] += max(0, req - last_total[1])
                    cur["turns"] += 1
                if total is not None:
                    last_total = (total, req)
            elif cur is None:
                continue
            elif ev == "delegation" and e.get("decision") != "deny":
                cur["agents"].append(_agent_name(e.get("agent")))
            elif ev == "agent_result" and e.get("advice") in ("retry", "escalate", "reassess"):
                cur["agent_failed"] = True
            elif ev == "validation":
                cur["validation"] = bool(e.get("ok"))
        # outcome: evidence of failure beats evidence of success; "moved on" counts as success
        for i, r in enumerate(srows):
            nxt = srows[i + 1] if i + 1 < len(srows) else None
            if r["agent_failed"] or r["validation"] is False or (nxt and nxt.get("follow_up") and nxt["task"] == r["task"]):
                r["outcome"] = "failed"
            elif r["validation"] is True or (nxt and not nxt.get("follow_up")):
                r["outcome"] = "ok"
        rows += [r for r in srows if r["turns"]]          # prompts with no measured turn tell us nothing
    return rows


def _mean(xs):
    return statistics.mean(xs) if xs else None


def _success(rs):
    known = [r for r in rs if r["outcome"] != "unknown"]
    return (sum(r["outcome"] == "ok" for r in known) / len(known)) if known else None


def analyse(events: list[dict], cfg: Config) -> dict:
    t = cfg.section("tune")
    n_min, gap = int(t.get("min_samples", 10)), float(t.get("min_difference", 0.25))
    rows = prompt_rows(events)
    total_cost = sum(r["cost"] for r in rows) or 0.0
    out: dict = {"prompts": len(rows), "cost_usd": round(total_cost, 4), "min_samples": n_min,
                 "buckets": {}, "categories": {}, "suggestions": [], "notes": []}

    # 1. context size: where the money goes
    for name in [b for _, b in BUCKETS] + ["unknown"]:
        rs = [r for r in rows if bucket(r["context_tokens"]) == name]
        if rs:
            req = sum(r["requests"] for r in rs)
            out["buckets"][name] = {"prompts": len(rs), "cost_usd": round(sum(r["cost"] for r in rs), 4),
                                    "share": round(sum(r["cost"] for r in rs) / total_cost, 3) if total_cost else None,
                                    "cost_per_request": round(sum(r["cost"] for r in rs) / req, 4) if req else None}
    warn_at = int(cfg.section("session").get("warn_context_tokens", 0) or 0)
    big = out["buckets"].get("200k+")
    if big and big["prompts"] >= n_min and (big["share"] or 0) >= 0.5 and (not warn_at or warn_at > 150_000):
        out["suggestions"].append({
            "id": "context-threshold",
            "title": "Warn about context size earlier (150k instead of %s)" % (f"{warn_at // 1000}k" if warn_at else "off"),
            "evidence": f"{big['share']:.0%} of measured cost came from {big['prompts']} prompts that started with "
                        f"200k+ tokens of context (${big['cost_per_request']}/request vs "
                        f"${(out['buckets'].get('<50k') or {}).get('cost_per_request')} under 50k).",
            "patch": {("session", "warn_context_tokens"): 150_000}})

    # 2. per category: hints followed? delegation vs inline, compared inside one context bucket
    off = list(cfg.section("routing").get("hint_categories_off") or [])
    for cat in sorted({r["category"] for r in rows if r["category"]}):
        rs = [r for r in rows if r["category"] == cat]
        hinted = [r for r in rs if r["hint"] and r["hinted_agent"]]
        followed = [r for r in hinted if _agent_name(r["hinted_agent"]) in r["agents"]]
        info = {"prompts": len(rs), "mean_cost": round(_mean([r["cost"] for r in rs]) or 0, 4),
                "success": _success(rs), "hinted": len(hinted), "hint_followed": len(followed)}
        out["categories"][cat] = info
        if cat in off or cat == "chat":
            continue
        if len(hinted) >= n_min and len(followed) / len(hinted) < 0.2:
            out["suggestions"].append({
                "id": f"hint-off:{cat}",
                "title": f"Stop sending routing hints for '{cat}' prompts",
                "evidence": f"the hint named an agent {len(hinted)} times and was followed {len(followed)} times; "
                            "an ignored hint is pure token cost.",
                "patch": {("routing", "hint_categories_off"): sorted(set(off + [cat]))}})
            continue
        for b in [name for _, name in BUCKETS]:
            inb = [r for r in rs if bucket(r["context_tokens"]) == b]
            dele, inline = [r for r in inb if r["agents"]], [r for r in inb if not r["agents"]]
            if len(dele) < n_min or len(inline) < n_min:
                continue
            cd, ci = _mean([r["cost"] for r in dele]), _mean([r["cost"] for r in inline])
            sd, si = _success(dele), _success(inline)
            if ci and cd > ci * (1 + gap) and (sd is None or si is None or sd <= si):
                out["suggestions"].append({
                    "id": f"hint-off:{cat}",
                    "title": f"Let the main session do '{cat}' work itself (no routing hint)",
                    "evidence": f"context {b}: delegated prompts cost ${cd:.3f} on average (n={len(dele)}, success "
                                f"{_pct(sd)}) vs ${ci:.3f} inline (n={len(inline)}, success {_pct(si)}).",
                    "patch": {("routing", "hint_categories_off"): sorted(set(off + [cat]))}})
                break
            if cd and ci > cd * (1 + gap) and (sd or 0) >= (si or 0):
                out["notes"].append(f"'{cat}' at context {b}: delegation paid off (${cd:.3f} vs ${ci:.3f} inline, "
                                    f"n={len(dele)}/{len(inline)}); keep the hint.")

    # 3. budget reservations vs what delegations really cost
    by_tier: dict[str, list[float]] = defaultdict(list)
    for e in events:
        if e.get("event") == "agent_result" and isinstance(e.get("cost_usd"), (int, float)) and e.get("tier"):
            by_tier[e["tier"]].append(float(e["cost_usd"]))
    res = cfg.section("budgets").get("reservation_usd") or {}
    for tier, costs in sorted(by_tier.items()):
        if tier not in res or len(costs) < n_min:
            continue
        p75 = sorted(costs)[int(0.75 * (len(costs) - 1))]
        cur = float(res[tier])
        if p75 > 0 and (cur < p75 / 2 or cur > p75 * 2):
            out["suggestions"].append({
                "id": f"reservation:{tier}",
                "title": f"Reserve ${p75:.2f} instead of ${cur:.2f} per {tier}-tier delegation",
                "evidence": f"{len(costs)} {tier}-tier agent runs: median ${statistics.median(costs):.3f}, "
                            f"75th percentile ${p75:.3f}.",
                "patch": {("budgets.reservation_usd", tier): round(p75, 2)}})

    # 4. likely misclassifications (advice for the classifier, nothing to apply)
    heavy_chat = [r for r in rows if r["category"] == "chat" and (r["cost"] >= 1.0 or r["requests"] >= 15)]
    if heavy_chat:
        out["notes"].append(f"{len(heavy_chat)} prompt(s) classified as conversation went on to do real work "
                            f"(>= $1 or >= 15 requests): tasks {', '.join(sorted({r['task'] or '?' for r in heavy_chat})[:8])}.")
    light_work = [r for r in rows if r["category"] not in ("chat", None) and r["requests"] <= 1 and r["cost"] < 0.05]
    if len(light_work) >= 3:
        out["notes"].append(f"{len(light_work)} prompt(s) classified as work needed a single request: probably "
                            "conversation; their hints were wasted.")
    if not rows:
        out["notes"].append("No measured prompts yet: metrics come from the hooks while you work.")
    elif len(rows) < n_min:
        out["notes"].append(f"Only {len(rows)} measured prompts; suggestions need {n_min} per compared group.")
    return out


def _pct(x):
    return "n/a" if x is None else f"{x:.0%}"


def render(rep: dict) -> str:
    lines = [f"tierflow tune: {rep['prompts']} measured prompts, ${rep['cost_usd']:.2f} (list-price estimate)", "",
             "Cost by context size when the prompt started:"]
    for name, b in rep["buckets"].items():
        cpr = f"${b['cost_per_request']:.3f}/request" if b["cost_per_request"] is not None else "n/a"
        lines.append(f"  {name:>8}: {b['prompts']:3d} prompts, ${b['cost_usd']:.2f} ({_pct(b['share'])}), {cpr}")
    if rep["categories"]:
        lines += ["", "By category:"]
        for cat, c in rep["categories"].items():
            lines.append(f"  {cat:>11}: {c['prompts']:3d} prompts, mean ${c['mean_cost']:.3f}, success {_pct(c['success'])}, "
                         f"hint followed {c['hint_followed']}/{c['hinted']}")
    lines += ["", "Suggestions (apply with: orch tune --apply <id>):" if rep["suggestions"] else "Suggestions: none."]
    for s in rep["suggestions"]:
        lines += [f"  [{s['id']}] {s['title']}", f"      evidence: {s['evidence']}",
                  "      sets: " + ", ".join(f"{sec}.{k} = {v!r}" for (sec, k), v in s["patch"].items())]
    if rep["notes"]:
        lines += ["", "Notes:"] + [f"  - {n}" for n in rep["notes"]]
    return "\n".join(lines)


def _toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    return json.dumps(str(v))       # a JSON string is a valid TOML basic string


def apply(rep: dict, ids: list[str]) -> list[str]:
    """Write the chosen suggestions to tuned.toml (merged with what is already there) and log them."""
    chosen = [s for s in rep["suggestions"] if s["id"] in ids]
    missing = [i for i in ids if i not in {s["id"] for s in chosen}]
    if missing:
        raise ValueError(f"no current suggestion with id: {', '.join(missing)} (run `orch tune` to list them)")
    path = tuned_config_path()
    current: dict[tuple[str, str], object] = {}
    if path.is_file():
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        for section, values in data.items():
            if section == "budgets":
                for k, v in (values.get("reservation_usd") or {}).items():
                    current[("budgets.reservation_usd", k)] = v
            elif isinstance(values, dict):
                for k, v in values.items():
                    current[(section, k)] = v
    for s in chosen:
        for key, v in s["patch"].items():
            if key not in TUNABLE:
                raise ValueError(f"suggestion {s['id']} touches a key tune may not set: {key}")
            current[key] = v
    sections: dict[str, dict] = defaultdict(dict)
    for (section, k), v in sorted(current.items()):
        sections[section][k] = v
    text = ["# Written by `orch tune --apply`. Only whitelisted keys are read; your config.toml overrides this.",
            "# Undo everything: `orch tune --revert`. History: tune-history.jsonl", ""]
    for section, values in sections.items():
        text.append(f"[{section}]")
        text += [f"{k} = {_toml_value(v)}" for k, v in values.items()]
        text.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(text), encoding="utf-8")
    with open(home_dir() / "tune-history.jsonl", "a", encoding="utf-8") as fh:
        for s in chosen:
            fh.write(json.dumps({"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "id": s["id"],
                                 "title": s["title"], "evidence": s["evidence"],
                                 "set": {f"{a}.{b}": v for (a, b), v in s["patch"].items()}}) + "\n")
    return [s["id"] for s in chosen]


def revert() -> bool:
    p = tuned_config_path()
    if p.is_file():
        p.unlink()
        return True
    return False


def run(cfg: Config, since_days: float | None = None) -> dict:
    return analyse(read_metrics(since_days), cfg)
