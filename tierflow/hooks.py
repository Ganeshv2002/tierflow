"""Claude Code hook handlers. Invoked as: python scripts/hook.py <event>  (JSON on stdin).

Rules:
  * stdout carries only the JSON object (or nothing);
  * hooks never return "allow" (that would bypass the user's permission prompts) - only ask/deny or
    extra context;
  * internal errors exit 1 with a one-line stderr message (Claude Code shows it as a notice), plus a
    traceback in ~/.claude/tierflow/errors.log. They are never silently swallowed. Gates fail open.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from .budget import check as budget_check
from .classifier import classify, render_hint, strip_attachments
from .codemap import discover
from .config import PACKAGE_ROOT, Config, home_dir, load_config
from .escalation import advise, parse_outcome, tier_of_model
from .ledger import FOLLOW_UP, Ledger, emit, now_iso, prior_failures, set_active, start_or_continue_task
from .pricing import rates_for
from .usage import last_context, last_model, long_path, scan_file, session_usage

ORCH_CMD = f'python "{(PACKAGE_ROOT / "orch.py").as_posix()}"'
STALE_SECONDS = 3600
STRICTNESS = ["allow", "warn", "ask", "deny"]


def _project(payload: dict) -> str:
    return os.environ.get("CLAUDE_PROJECT_DIR") or payload.get("cwd") or os.getcwd()


def _session_cost(payload: dict, cfg: Config) -> float:
    tp = payload.get("transcript_path")
    if not tp or not Path(tp).is_file():
        return 0.0
    return session_usage(tp, cfg).total.cost_usd


def _age_s(iso: str | None) -> float:
    try:
        return time.time() - datetime.fromisoformat(iso).timestamp()
    except (TypeError, ValueError):
        return 1e9


def _norm(p: str, root: str) -> str:
    p = p.strip().strip("`'\"").replace("\\", "/")
    rootp = Path(root).resolve().as_posix().lower()
    if p.lower().startswith(rootp):
        p = p[len(rootp):]
    return p.lstrip("./").lstrip("/").lower()


def claim_files(prompt: str, root: str) -> list[str]:
    """Files an editing agent is allowed to touch, from a 'FILES: a, b' line in its brief."""
    out: list[str] = []
    lines = (prompt or "").splitlines()
    for i, line in enumerate(lines):
        m = re.match(r"^\s*\**\s*FILES?\s*\**\s*:\s*\**\s*(.*)$", line, re.I)
        if not m:
            continue
        chunk = [m.group(1)]
        # also accept a bullet list on the following lines (seen in live briefs), until a blank line or next KEY:
        for nxt in lines[i + 1:]:
            if not nxt.strip() or re.match(r"^\s*[A-Z][A-Z _-]{2,}\s*:", nxt):
                break
            if re.match(r"^\s*([-*•]|\d+[.)])\s+", nxt):
                chunk.append(re.sub(r"^\s*([-*•]|\d+[.)])\s+", "", nxt))
            else:
                break
        for part in chunk:
            for f in re.split(r"[,;]\s*|\s+(?=[\w./\\-]+\.\w+)", part):
                f = re.sub(r"\s+[-(—].*$", "", f.strip())          # drop trailing notes: "a.py (definition)"
                if f and f.lower() not in ("none", "n/a", "-") and re.search(r"[\w-]\.[\w]+$|/$", f.strip("`'\"")):
                    out.append(_norm(f, root))
    return list(dict.fromkeys(out))


def _overlap(a: str, b: str) -> bool:
    a, b = a.rstrip("/"), b.rstrip("/")
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


def _response_text(resp) -> str:
    if isinstance(resp, str):
        return resp
    parts: list[str] = []

    def walk(x):
        if isinstance(x, dict):
            if isinstance(x.get("text"), str):
                parts.append(x["text"])
            for k, v in x.items():
                if k != "text":
                    walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    walk(resp)
    return "\n".join(parts)


def _ours(agent_type: str | None) -> bool:
    return bool(agent_type) and (agent_type.startswith("tierflow:") or agent_type in ("scout", "mechanic", "implementer", "reviewer", "architect"))


# ---- handlers --------------------------------------------------------------------------------

def on_prompt(payload: dict) -> dict | None:
    project = _project(payload)
    cfg = load_config(project)
    sid = payload.get("session_id") or "unknown"
    tp = payload.get("transcript_path")
    prompt = payload.get("prompt") or ""
    set_active(project, sid, tp)
    user_text = strip_attachments(prompt).strip()
    if not user_text:
        # a notification, another session's message or a subagent report: nothing the user typed, so no
        # hint and no new task (a hint here is pure token cost, and a new task breaks follow-up detection)
        return None
    cost = _session_cost(payload, cfg)

    with Ledger(sid).update() as data:
        data["project_dir"], data["transcript_path"] = project, tp
        # a new user prompt means foreground agents of the previous turn are finished
        for d in data["delegations"].values():
            if d.get("status") == "running" and (not d.get("background") or _age_s(d.get("started")) > STALE_SECONDS):
                d["status"] = "abandoned"
        follow_up = bool(data.get("task") and FOLLOW_UP.search(prompt))
        pf = prior_failures(data) if follow_up else 0
        # a follow-up ("still failing") is about the original task: classify that, escalated by evidence
        c = classify(data["task"]["summary"] if follow_up else prompt, cfg, prior_failures=pf)
        summary = {k: v for k, v in c.to_dict().items() if k != "signals"}
        task, is_new = start_or_continue_task(data, prompt, summary, cost)
        if not is_new:
            task["classification"] = summary
        data["last_cost"] = cost
        task_spent = cost - task.get("cost_at_start", 0.0)
        ctx_model, ctx_tokens = last_context(tp) if tp else (None, None)
        main_model = ctx_model or data.get("main_model")
        if main_model:
            data["main_model"] = main_model
        main_tier = tier_of_model(main_model, cfg)
        warning = context_warning(cfg, data, ctx_tokens, main_model)

    bd = budget_check(cfg, cost, task_spent, None)
    hint = _prompt_hint(cfg, c, bd, user_text, main_tier, project)
    emit("prompt", session=sid, task=task["key"], new_task=is_new, category=c.category, complexity=c.complexity,
         risk=c.risk, tier=c.tier, session_cost=round(cost, 4), project=project, main_model=main_model,
         context_tokens=ctx_tokens, follow_up=follow_up, hint=bool(hint), hinted_agent=c.agent if hint else None,
         context_warning=bool(warning))
    out: dict = {}
    if hint:
        out["hookSpecificOutput"] = {"hookEventName": "UserPromptSubmit", "additionalContext": hint}
    if warning:
        out["systemMessage"] = warning      # shown to the user only: costs no model tokens
    return out or None


def context_warning(cfg: Config, data: dict, ctx_tokens: int | None, model: str | None) -> str | None:
    """One line for the user when the session's context is large; repeated only after it grew further."""
    s = cfg.section("session")
    threshold = int(s.get("warn_context_tokens", 0) or 0)
    if not threshold or not ctx_tokens:
        return None
    if ctx_tokens < threshold:
        data.pop("context_warned_at", None)          # e.g. after /compact: warn again next time it grows
        return None
    step = int(s.get("rewarn_every_tokens", 0) or 0)
    last = data.get("context_warned_at")
    if last and (not step or ctx_tokens < last + step):
        return None
    data["context_warned_at"] = ctx_tokens
    r = rates_for(model, cfg) if model else None
    per = (f" (at least ${ctx_tokens * r['cache_read'] / 1e6:.2f} per model request on {model}, even from cache)"
           if r and r.get("cache_read") else "")
    return (f"tierflow: this session's context is about {ctx_tokens // 1000}k tokens and every model request "
            f"re-reads it{per}. If the current task is done, start a fresh session with a short hand-over note, "
            "or run /compact.")


def _prompt_hint(cfg: Config, c, bd, user_text: str, main_tier: str | None, project: str) -> str | None:
    rcfg = cfg.section("routing")
    over = bd.action != "allow"
    if (c.category == "chat" or not rcfg.get("inject_hint", True)
            or c.category in (rcfg.get("hint_categories_off") or [])
            or len(user_text) < rcfg.get("min_prompt_chars_for_hint", 15)):
        return f"[tierflow] {bd.reason}. New delegations need approval." if over else None

    # Main session already on the right tier for routine work: say nothing (every hint token is paid for).
    if main_tier == "standard" and c.tier == "standard" and not c.plan_first and not over:
        return None
    extra = [f"OVER BUDGET: {bd.reason}. Delegations will ask for approval."] if over else []
    map_line = None
    if c.category == "discovery" and cfg.section("context").get("reuse_codebase_map", True):
        cm = discover(project, cfg.section("context").get("extra_map_paths"))
        if cm.found:
            map_line = f"codebase map exists ({cm.describe()}): tell the scout to start there"
    return render_hint(c, cfg, budget_line=bd.line(cfg) if over else None, map_line=map_line, extra=extra,
                       main_tier=main_tier)


def on_pre_agent(payload: dict) -> dict | None:
    project = _project(payload)
    cfg = load_config(project)
    sid = payload.get("session_id") or "unknown"
    ti = payload.get("tool_input") or {}
    agent = ti.get("subagent_type") or "general-purpose"
    info = cfg.agent_info(agent) or {}
    tier = tier_of_model(ti.get("model"), cfg) or info.get("tier")
    if tier in (None, "inherit"):
        tier = tier_of_model(last_model(payload.get("transcript_path") or ""), cfg) or "standard"

    action, reasons, ctx = "allow", [], []

    def escalate_action(a: str, why: str):
        nonlocal action
        if STRICTNESS.index(a) > STRICTNESS.index(action):
            action = a
        if a in ("ask", "deny"):
            reasons.append(why)
        elif a == "warn":
            ctx.append(why)

    cost = _session_cost(payload, cfg)
    led = Ledger(sid)
    with led.update() as data:
        task = data.get("task") or {}
        cls = task.get("classification") or {}
        if tier == "reasoning":
            justified = cls.get("tier") == "reasoning" or cls.get("risk") == "high" or task.get("escalations") or task.get("failures")
            if not justified:
                gate = cfg.opus_gate()
                if gate != "allow":
                    escalate_action(gate, f"tierflow: Opus-tier delegation to {agent} but the task was classified "
                                          f"{cls.get('complexity', '?')}/{cls.get('risk', '?')} risk with no failed attempts (mode={cfg.mode})")
        bd = budget_check(cfg, cost, cost - task.get("cost_at_start", cost), tier)
        if bd.action != "allow":
            escalate_action(bd.action, bd.reason)

        files: list[str] = []
        if info.get("edits"):
            files = claim_files(ti.get("prompt", ""), project)
            for tid, d in data["delegations"].items():
                if d.get("status") != "running" or not d.get("files") or _age_s(d.get("started")) > STALE_SECONDS:
                    continue
                clash = [f for f in files for g in d["files"] if _overlap(f, g)]
                if clash:
                    escalate_action("deny", f"tierflow: {d['agent']} is still editing {', '.join(sorted(set(clash))[:5])}; "
                                            "run edits on overlapping files sequentially (or use isolation: worktree)")
            if not files and (agent.endswith("mechanic")):
                ctx.append("mechanic brief has no 'FILES:' line, so its edit scope is not enforced")
        if action != "deny":
            data["delegations"][payload.get("tool_use_id") or f"anon-{time.time()}"] = {
                "agent": agent, "tier": tier, "model_param": ti.get("model"), "started": now_iso(),
                "status": "running", "files": files, "task": task.get("key"),
                "background": bool(ti.get("run_in_background")), "description": (ti.get("description") or "")[:80]}

    emit("delegation", session=sid, task=task.get("key"), agent=agent, tier=tier, decision=action,
         reasons=reasons, files=len(files))
    if action in ("ask", "deny"):
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": action,
                                       "permissionDecisionReason": "; ".join(reasons)}}
    if ctx:
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": "[tierflow] " + " | ".join(ctx)}}
    return None


def on_post_agent(payload: dict) -> dict | None:
    project = _project(payload)
    cfg = load_config(project)
    sid = payload.get("session_id") or "unknown"
    ti = payload.get("tool_input") or {}
    resp = payload.get("tool_response")
    text = _response_text(resp)
    outcome = parse_outcome(text)
    agent = ti.get("subagent_type") or "general-purpose"
    info = cfg.agent_info(agent) or {}
    notes: list[str] = []

    # --- fallbacks: the agent crashed, was cut off, or returned nothing usable --------------------
    run_status = resp.get("status") if isinstance(resp, dict) else None
    if run_status and run_status != "completed":
        outcome.status, outcome.reason = "failed", f"agent run ended with status '{run_status}'"
    elif not text.strip():
        outcome.status, outcome.reason = "failed", "agent returned an empty result"
    elif _ours(agent) and info.get("edits") and (outcome.status == "unknown" or (outcome.validation or "").lower().startswith("not")):
        # no STATUS line, or it skipped validation: check deterministically here instead of trusting it or
        # spending the main model's tokens on re-checking
        verdict = _validate_claimed(payload, outcome, cfg)
        if verdict is not None:
            ok, digest = verdict
            outcome.validation = "pass" if ok else "fail"
            if outcome.status == "unknown":
                outcome.status = "done" if ok else "failed"
            notes.append(("validated by tierflow (agent did not): " + digest) if not ok else
                          "tierflow ran validation for the agent: PASS")
        elif outcome.status == "unknown":
            notes.append("agent gave no STATUS line and no files to validate; treat its result as unverified")

    if _ours(agent) and agent.endswith("architect") and outcome.status in ("done", "unknown") and text.strip():
        plan_path = _save_plan(payload, text)
        if plan_path:
            notes.append(f"plan saved to {plan_path}. Brief tierflow:implementer with `PLAN: {plan_path}` "
                         "(it reads the file) instead of retyping the plan.")

    advice_msg, advice_action = "", "none"
    step = {}
    with Ledger(sid).update() as data:
        d = data["delegations"].get(payload.get("tool_use_id") or "", {})
        tier = d.get("tier") or info.get("tier") or "standard"
        agent_id = str(resp.get("agentId")) if isinstance(resp, dict) and resp.get("agentId") else None
        if agent_id:
            data["agent_ids"][agent_id] = agent
        usage = data.get("agent_usage", {}).get(agent_id or "", {})   # filled by SubagentStop (fires first)
        step = {"cost_usd": usage.get("cost_usd"), "models": list((usage.get("models") or {}).keys()),
                "tokens": (resp.get("usage") if isinstance(resp, dict) else None),
                "duration_ms": resp.get("totalDurationMs") if isinstance(resp, dict) else None,
                "tool_uses": resp.get("totalToolUseCount") if isinstance(resp, dict) else None}
        if d:
            d.update(status="finished", ended=now_iso(), outcome=outcome.status, cost_usd=step["cost_usd"],
                     duration_ms=step["duration_ms"])
        task = data.get("task")
        if task is not None and (_ours(agent) or outcome.status != "unknown"):
            adv = advise(task, tier, outcome, cfg)
            advice_msg, advice_action = adv.message, adv.action
            task.setdefault("steps", []).append({"agent": agent, "tier": tier, "outcome": outcome.status,
                                                 "cost_usd": step["cost_usd"], "duration_ms": step["duration_ms"]})
    emit("agent_result", session=sid, task=(task or {}).get("key"), agent=agent, tier=tier, status=outcome.status,
         validation=outcome.validation, confidence=outcome.confidence, advice=advice_action, reason=outcome.reason,
         run_status=run_status, **step)
    msg = " | ".join([*notes, advice_msg] if advice_msg else notes)
    if msg:
        return {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": "[tierflow] " + msg}}
    return None


def _validate_claimed(payload: dict, outcome, cfg: Config) -> tuple[bool, str] | None:
    """Run local validation for the files an editing agent touched (from its STATUS FILES or its brief)."""
    from .validation import execute, plan
    project = _project(payload)
    files = outcome.files or claim_files((payload.get("tool_input") or {}).get("prompt", ""), project)
    files = [f for f in files if (Path(project) / f).is_file()]
    if not files:
        return None
    rep = execute(plan(project, cfg, files), cfg)
    if not rep.results:
        return None
    digest = rep.digest()
    return rep.ok, digest[:1500]


def _save_plan(payload: dict, text: str) -> str | None:
    """Keep the architect's plan as a file so the main session passes a path, not a retyped plan.
    Stored under <project>/.claude/tierflow/plans/ with a .gitignore so it is never committed."""
    try:
        d = Path(_project(payload)) / ".claude" / "tierflow" / "plans"
        d.mkdir(parents=True, exist_ok=True)
        gi = d.parent / ".gitignore"
        if not gi.exists():
            gi.write_text("*\n", encoding="utf-8")
        name = f"{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-{(payload.get('tool_use_id') or 'plan')[-8:]}.md"
        (d / name).write_text(text, encoding="utf-8")
        return (d / name).relative_to(Path(_project(payload))).as_posix()
    except OSError:
        return None


def on_pre_edit(payload: dict) -> dict | None:
    """Scope gate for scope-locked agents (default: mechanic). Main-session edits are untouched."""
    agent = payload.get("agent_type")
    if not agent:
        return None
    project = _project(payload)
    cfg = load_config(project)
    info = cfg.agent_info(agent) or {}
    if not info.get("scope_locked"):
        return None
    ti = payload.get("tool_input") or {}
    target = ti.get("file_path") or ti.get("notebook_path")
    if not target:
        return None
    data = Ledger(payload.get("session_id") or "unknown").read()
    allowed: list[str] = []
    for d in data.get("delegations", {}).values():
        if d.get("status") == "running" and d.get("agent", "").rsplit(":", 1)[-1] == agent.rsplit(":", 1)[-1]:
            allowed += d.get("files") or []
    if not allowed:
        return None
    t = _norm(target, project)
    if any(_overlap(t, a) for a in allowed):
        return None
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "ask",
                                   "permissionDecisionReason": f"tierflow: {agent} was briefed for {', '.join(allowed[:5])} "
                                                               f"but is editing {target}"}}


def on_subagent_stop(payload: dict) -> dict | None:
    cfg = load_config(_project(payload))
    sid = payload.get("session_id") or "unknown"
    agent_id, agent_type = payload.get("agent_id"), payload.get("agent_type")
    tpath = payload.get("agent_transcript_path")
    if not tpath and agent_id and payload.get("transcript_path"):
        p = Path(payload["transcript_path"])
        tpath = str(p.parent / p.stem / "subagents" / f"agent-{agent_id}.jsonl")
    totals = scan_file(tpath, cfg) if tpath and long_path(tpath).is_file() else None
    if agent_id:
        with Ledger(sid).update() as data:
            if agent_type:
                data["agent_ids"][str(agent_id)] = agent_type
            if totals:
                data.setdefault("agent_usage", {})[str(agent_id)] = totals.to_dict()
    emit("subagent_usage", session=sid, agent=agent_type, agent_id=agent_id,
         usage=totals.to_dict() if totals else None, missing_transcript=totals is None)
    return None


def on_stop(payload: dict) -> dict | None:
    cfg = load_config(_project(payload))
    sid = payload.get("session_id") or "unknown"
    tp = payload.get("transcript_path")
    if not tp or not Path(tp).is_file():
        return None
    su = session_usage(tp, cfg)
    total = su.total
    data = Ledger(sid).read()
    task = data.get("task") or {}
    model, ctx_tokens = last_context(tp)
    emit("turn", session=sid, task=task.get("key"), category=(task.get("classification") or {}).get("category"),
         main_model=model, context_tokens=ctx_tokens, cost_total=round(total.cost_usd, 6), cost_main=round(su.main.cost_usd, 6),
         cost_subagents=round(total.cost_usd - su.main.cost_usd, 6), requests=total.requests,
         models={k: {"requests": m.requests, "input": m.input, "output": m.output, "cache_read": m.cache_read,
                     "cache_write": m.cache_write_5m + m.cache_write_1h, "thinking": m.thinking,
                     "cost_usd": None if m.cost_usd is None else round(m.cost_usd, 6)} for k, m in total.models.items()},
         unknown_models=total.unknown_models, escalations=task.get("escalations", 0),
         attempts=task.get("attempts", {}), project=data.get("project_dir"))
    return None


HANDLERS = {"prompt": on_prompt, "pre-agent": on_pre_agent, "post-agent": on_post_agent, "pre-edit": on_pre_edit,
            "subagent-stop": on_subagent_stop, "stop": on_stop}


def main(argv: list[str]) -> int:
    event = argv[1] if len(argv) > 1 else ""
    handler = HANDLERS.get(event)
    if not handler:
        sys.stderr.write(f"tierflow: unknown hook event {event!r}\n")
        return 1
    try:
        raw = sys.stdin.buffer.read().decode("utf-8", errors="replace")
        payload = json.loads(raw) if raw.strip() else {}
        if os.environ.get("TIERFLOW_DEBUG_PAYLOADS"):          # opt-in: record raw hook input for debugging
            home_dir().mkdir(parents=True, exist_ok=True)
            with open(home_dir() / "payloads.jsonl", "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"event": event, "payload": payload})[:20000] + "\n")
        out = handler(payload)
        if out:
            sys.stdout.write(json.dumps(out))
        return 0
    except Exception as e:  # noqa: BLE001 - reported, logged, and the action proceeds (fail open)
        try:
            log = home_dir() / "errors.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            with open(log, "a", encoding="utf-8") as fh:
                fh.write(f"--- {now_iso()} {event}\n{traceback.format_exc()}\n")
        except OSError:
            pass
        sys.stderr.write(f"tierflow {event} hook error: {type(e).__name__}: {e} (see ~/.claude/tierflow/errors.log)\n")
        return 1
