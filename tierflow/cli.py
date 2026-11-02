"""`orch` command line. Deterministic helpers the main session (or you) can run; no model calls."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import __version__
from .budget import check as budget_check
from .classifier import classify, render_hint
from .codemap import discover
from .config import load_config
from .context import build_context
from .deps import analyze
from .ledger import Ledger, emit, get_active, prior_failures
from .report import build_report, render
from .usage import session_usage
from .validation import execute, plan


def _root(args) -> Path:
    return Path(args.root or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()).resolve()


def _print(obj, as_json: bool, text: str | None = None) -> None:
    if as_json:
        print(json.dumps(obj, indent=1, default=str))
    else:
        print(text if text is not None else obj)


def cmd_classify(args) -> int:
    cfg = load_config(_root(args), mode_override=args.mode)
    c = classify(args.task, cfg, prior_failures=args.failures)
    if args.json:
        _print(c.to_dict(), True)
    else:
        print(render_hint(c, cfg))
        if args.explain:
            print("\nsignals:", json.dumps(c.signals, indent=1))
    return 0


def cmd_context(args) -> int:
    root = _root(args)
    cfg = load_config(root)
    pack = build_context(args.task, root, cfg, budget_tokens=args.budget, use_cache=not args.no_cache)
    _print(pack.to_dict(), args.json, pack.render())
    return 0


def cmd_map(args) -> int:
    root = _root(args)
    cfg = load_config(root)
    cm = discover(root, cfg.section("context").get("extra_map_paths"))
    if not cm.found:
        print("no codebase map found (looked for codebase-map.json, CODEBASE_MAP.md, docs/ai-context/*, ARCHITECTURE.md ...)")
        return 0
    print(f"maps: {cm.describe()}")
    if cm.always_load():
        print(f"map loadPolicy.always: {', '.join(cm.always_load())}")
    if args.query:
        for h in cm.query(args.query, limit=8):
            print(f"- {h.kind} {h.name} ({h.source}{':' + str(h.line) if h.line else ''}) score={h.score:.1f}")
            if h.entry_points:
                print(f"  entry: {', '.join(h.entry_points[:8])}")
            if h.tests:
                print(f"  tests: {', '.join(h.tests[:6])}")
    return 0


def cmd_deps(args) -> int:
    root = _root(args)
    cfg = load_config(root)
    rep = analyze(root, args.files, cfg.section("context").get("exclude_dirs", []))
    if args.json:
        _print(rep.__dict__, True)
        return 0
    for f, ds in rep.dependents.items():
        print(f"{f}  (project: {rep.projects.get(f, '-')})")
        for d in ds:
            print(f"  <- {d}")
    if rep.downstream_projects:
        print("downstream projects: " + ", ".join(rep.downstream_projects))
    for n in rep.notes:
        print("note:", n)
    return 0


def cmd_validate(args) -> int:
    root = _root(args)
    cfg = load_config(root)
    rep = plan(root, cfg, args.files or None)
    if args.dry_run:
        rep.dry_run = True
    else:
        execute(rep, cfg)
        active = get_active(root)
        failed = [r.name for r in rep.results if not r.ok and not r.skipped]
        if active:
            with Ledger(active["session_id"]).update() as data:
                data["validations"].append({"ok": rep.ok, "failed": failed, "steps": len(rep.results)})
                data["validations"] = data["validations"][-50:]
        emit("validation", session=(active or {}).get("session_id"), ok=rep.ok, steps=len(rep.results), failed=failed)
    _print(rep.to_dict(), args.json, rep.digest())
    return 0 if (rep.dry_run or rep.ok) else 1


def _transcript(args, root: Path) -> tuple[str | None, str | None]:
    if args.transcript:
        return args.transcript, None
    active = get_active(root)
    if active:
        return active.get("transcript_path"), active.get("session_id")
    return None, None


def cmd_usage(args) -> int:
    root = _root(args)
    cfg = load_config(root)
    tp, sid = _transcript(args, root)
    if not tp or not Path(tp).is_file():
        print("no transcript found: pass --transcript or run inside a session with the tierflow plugin enabled")
        return 1
    su = session_usage(tp, cfg)
    if args.json:
        _print(su.to_dict(), True)
        return 0
    t = su.total
    print(f"session {sid or Path(tp).stem}: ${t.cost_usd:.4f} estimated at list price "
          f"(main ${su.main.cost_usd:.4f}, {len(su.subagents)} subagent transcript(s) ${t.cost_usd - su.main.cost_usd:.4f})")
    for m, u in t.models.items():
        cost = "unknown price" if u.cost_usd is None else f"${u.cost_usd:.4f}"
        print(f"  {m}: {u.requests} req, in {u.input}, out {u.output} (thinking {u.thinking if u.thinking is not None else 'n/a'}), "
              f"cache read {u.cache_read}, cache write {u.cache_write_5m}+{u.cache_write_1h}(1h) -> {cost}")
    return 0


def cmd_budget(args) -> int:
    root = _root(args)
    cfg = load_config(root)
    tp, sid = _transcript(args, root)
    if not sid:
        print("no active session for this project")
        return 1
    cost = session_usage(tp, cfg).total.cost_usd if tp and Path(tp).is_file() else 0.0
    data = Ledger(sid).read()
    task = data.get("task") or {}
    bd = budget_check(cfg, cost, cost - task.get("cost_at_start", 0.0), args.tier)
    print(bd.line(cfg))
    if bd.action != "allow":
        print(f"{bd.action}: {bd.reason}")
    print(f"task {task.get('key', '-')}: attempts {task.get('attempts', {})}, escalations {task.get('escalations', 0)}, "
          f"failures {prior_failures(data)}")
    return 0


def cmd_report(args) -> int:
    rep = build_report(args.since, args.session)
    _print(rep, args.json, render(rep))
    return 0


def cmd_tune(args) -> int:
    from . import tune
    if args.revert:
        print("removed tuned.toml" if tune.revert() else "nothing to revert (no tuned.toml)")
        return 0
    cfg = load_config(_root(args))
    rep = tune.run(cfg, args.since)
    if args.apply:
        try:
            done = tune.apply(rep, args.apply)
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return 2
        print(f"applied: {', '.join(done)} -> {tune.tuned_config_path()} (undo: orch tune --revert)")
        return 0
    if args.json:
        for sug in rep["suggestions"]:
            sug["patch"] = {f"{a}.{b}": v for (a, b), v in sug["patch"].items()}
    _print(rep, args.json, tune.render(rep))
    return 0


def cmd_config(args) -> int:
    cfg = load_config(_root(args), mode_override=args.mode)
    if args.json:
        _print(cfg.data, True)
    else:
        print(f"mode: {cfg.mode}   sources: {', '.join(cfg.sources)}")
        print(f"tiers: {cfg.data['models']}   opus gate: {cfg.opus_gate()}")
        b = cfg.section("budgets")
        print(f"budgets: task ${b['max_cost_per_task_usd']}, session ${b['max_cost_per_session_usd']}, enforcement {b['enforcement']}")
    for w in cfg.warnings:
        print("warning:", w, file=sys.stderr)
    return 0


def cmd_bench(args) -> int:
    from .bench import main as bench_main
    return bench_main(args.bench_args)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="orch", description="tierflow deterministic helpers (no model calls)")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp, json_flag=True):
        sp.add_argument("--root", help="repository root (default: CLAUDE_PROJECT_DIR or cwd)")
        if json_flag:
            sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("classify", help="classify a task and show the routing hint")
    sp.add_argument("task"); sp.add_argument("--mode"); sp.add_argument("--failures", type=int, default=0)
    sp.add_argument("--explain", action="store_true"); common(sp); sp.set_defaults(fn=cmd_classify)

    sp = sub.add_parser("context", help="relevant code locations for a task (map + symbols + snippets)")
    sp.add_argument("task"); sp.add_argument("--budget", type=int, help="token budget (estimate)")
    sp.add_argument("--no-cache", action="store_true"); common(sp); sp.set_defaults(fn=cmd_context)

    sp = sub.add_parser("map", help="show the existing codebase map and query it")
    sp.add_argument("--query"); common(sp, json_flag=False); sp.set_defaults(fn=cmd_map)

    sp = sub.add_parser("deps", help="files and projects that depend on the given files")
    sp.add_argument("files", nargs="+"); common(sp); sp.set_defaults(fn=cmd_deps)

    sp = sub.add_parser("validate", help="build/typecheck/test what the change affects; compact digest")
    sp.add_argument("--files", nargs="*", help="changed files (default: git diff HEAD + untracked)")
    sp.add_argument("--dry-run", action="store_true"); common(sp); sp.set_defaults(fn=cmd_validate)

    sp = sub.add_parser("usage", help="token usage and estimated cost of the current session")
    sp.add_argument("--transcript"); common(sp); sp.set_defaults(fn=cmd_usage)

    sp = sub.add_parser("budget", help="budget status of the current task/session")
    sp.add_argument("--tier", default=None); sp.add_argument("--transcript"); common(sp, json_flag=False)
    sp.set_defaults(fn=cmd_budget)

    sp = sub.add_parser("report", help="aggregate metrics across sessions")
    sp.add_argument("--since", type=float, help="days"); sp.add_argument("--session"); sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_report)

    sp = sub.add_parser("tune", help="learn from your own metrics: show evidence and suggested config changes")
    sp.add_argument("--since", type=float, help="days of metrics to use (default: all)")
    sp.add_argument("--apply", nargs="+", metavar="ID", help="write these suggestions to tuned.toml")
    sp.add_argument("--revert", action="store_true", help="delete tuned.toml")
    common(sp); sp.set_defaults(fn=cmd_tune)

    sp = sub.add_parser("config", help="show effective configuration")
    sp.add_argument("--mode"); common(sp); sp.set_defaults(fn=cmd_config)

    sp = sub.add_parser("bench", help="benchmark baseline vs tierflow (see docs/BENCHMARKING.md)", add_help=False)
    sp.add_argument("bench_args", nargs=argparse.REMAINDER); sp.set_defaults(fn=cmd_bench)
    return p


def main(argv: list[str]) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    return args.fn(args)
