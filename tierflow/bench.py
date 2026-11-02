"""Benchmark: plain Claude Code (baseline) vs Claude Code + tierflow (optimized).

Each run gets a fresh copy of the same repository snapshot, the same prompt, the same main model and
the same hard cap (`--max-budget-usd`). After the run, hidden verification files are copied in and a
deterministic check decides correctness. Nothing is claimed without these measurements.

    python orch.py bench run bench/tasks.toml --repeats 2 [--main-model sonnet] [--tasks search,bugfix]
    python orch.py bench report <results-dir>
    python orch.py bench run bench/tasks.toml --dry-run      # show commands, spend nothing
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import sys
import time
import tomllib
from datetime import datetime
from pathlib import Path

from . import proc
from .config import PACKAGE_ROOT, load_config
from .usage import session_usage

DEFAULT_ALLOWED = ["Read", "Edit", "Write", "MultiEdit", "Grep", "Glob", "Agent", "Task", "TodoWrite",
                   "Bash(python:*)", "Bash(python3:*)", "Bash(pytest:*)", "Bash(git diff:*)", "Bash(git status:*)",
                   "Bash(git log:*)", "Bash(rg:*)", "Bash(grep:*)", "Bash(ls:*)", "Bash(cat:*)", "Bash(npx tsc:*)",
                   "Bash(dotnet build:*)", "Bash(dotnet test:*)", "Bash(npm test:*)", "Bash(npm run:*)",
                   "Bash(npx vitest:*)", "Bash(npx --no-install:*)", "Bash(.venv/Scripts/python:*)",
                   "Bash(./.venv/Scripts/python:*)", "Bash(.venv/bin/python:*)"]
ARMS = ("baseline", "optimized")


def find_claude(explicit: str | None) -> str | None:
    if explicit:
        return explicit
    for cand in ("claude", str(Path.home() / ".local" / "bin" / ("claude.exe" if os.name == "nt" else "claude"))):
        p = proc.which(cand) or (cand if Path(cand).is_file() else None)
        if p:
            return p
    return None


def load_suite(path: Path) -> dict:
    with open(path, "rb") as f:
        suite = tomllib.load(f)
    suite["_dir"] = path.parent.resolve()
    if not suite.get("task"):
        raise ValueError(f"{path}: no [[task]] entries")
    return suite


def expand(argv: list, suite: dict) -> list[str]:
    """Placeholders usable in setup/verify commands: {python}, {suite} (the suite file's folder)."""
    return [sys.executable if a == "{python}" else str(a).replace("{suite}", str(suite["_dir"])) for a in argv]


def _link_dir(src: Path, dst: Path) -> None:
    """Share a big dependency folder (node_modules) with the workspace without copying it.
    Windows: a directory junction (no admin rights needed); elsewhere: a symlink."""
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(str(src), str(dst))
    else:
        os.symlink(src, dst, target_is_directory=True)


def _snapshot_git(repo: Path, ref: str, ws: Path) -> None:
    """Export tracked files at `ref` only: no uncommitted work, no history, no ignored/secret files."""
    import tarfile
    ws.mkdir(parents=True)
    tar = ws.parent / "snapshot.tar"
    r = proc.run(["git", "-C", str(repo), "archive", "--format=tar", "-o", str(tar), ref], timeout=300)
    if not r.ok:
        raise RuntimeError(f"git archive failed: {r.stderr.strip()[:200]}")
    with tarfile.open(tar) as tf:
        tf.extractall(ws, filter="data")
    tar.unlink()


def prepare_workspace(suite: dict, dest: Path, task: dict | None = None) -> Path:
    s = suite["suite"]
    repo = (suite["_dir"] / s["repo"]).resolve()
    ws = dest / "repo"
    if s.get("snapshot") == "git":
        _snapshot_git(repo, s.get("ref", "HEAD"), ws)
    else:
        shutil.copytree(repo, ws, ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache", "node_modules", "bin", "obj"))
    for name in s.get("link", []):
        if (repo / name).is_dir() and not (ws / name).exists():
            _link_dir(repo / name, ws / name)
    # setup (suite-wide, then per task, e.g. injecting the bug) runs BEFORE the commit, so git history
    # in the workspace never reveals what was changed
    for argv in [*s.get("setup", []), *((task or {}).get("setup", []))]:
        r = proc.run(expand(argv, suite), cwd=ws, timeout=900)
        if not r.ok:
            raise RuntimeError(f"setup command failed: {argv}: {(r.stderr or r.stdout).strip()[:300]}")
    for argv in (["git", "init", "-q"], ["git", "add", "-A"],
                 ["git", "-c", "user.name=bench", "-c", "user.email=bench@localhost", "-c", "core.autocrlf=false",
                  "commit", "-q", "-m", "snapshot"]):
        r = proc.run(argv, cwd=ws, timeout=300)
        if not r.ok:
            raise RuntimeError(f"workspace setup failed: {' '.join(argv)}: {r.stderr.strip()[:200]}")
    return ws


def claude_argv(claude: str, task: dict, suite: dict, arm: str, args) -> list[str]:
    s = suite["suite"]
    budget = task.get("max_budget_usd", s.get("max_budget_usd", 2.0))
    exe = [sys.executable, claude] if claude.endswith(".py") else [claude]     # .py = test double
    argv = [*exe, "-p", task["prompt"], "--output-format", "json", "--max-budget-usd", str(budget),
            "--permission-mode", "acceptEdits", "--allowedTools", " ".join(s.get("allowed_tools", DEFAULT_ALLOWED))]
    model = args.main_model or s.get("main_model")
    if model:
        argv += ["--model", model]
    # An installed copy of tierflow is disabled in BOTH arms: the baseline must not get it, and the
    # optimized arm must run exactly this source tree (not an older installed version, and not twice).
    argv += ["--settings", json.dumps({"enabledPlugins": {"tierflow@tierflow-local": False}})]
    if arm == "optimized":
        argv += ["--plugin-dir", str(PACKAGE_ROOT)]
    return argv


def find_transcript(session_id: str | None) -> Path | None:
    if not session_id:
        return None
    hits = list((Path.home() / ".claude" / "projects").glob(f"*/{session_id}.jsonl"))
    return hits[0] if hits else None


EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}


def inside_claude_dir(path: Path) -> bool:
    """Claude Code treats everything under a `.claude` folder as sensitive and blocks edits there, so a
    workspace inside one turns every edit task into a silent failure."""
    return any(part.lower() == ".claude" for part in Path(path).resolve().parts)


def permission_denials(transcript: Path) -> dict:
    """Count tool calls the CLI refused, in the main transcript and its subagents. Edit-tool denials are
    harness faults (those tools are allowed); other denials (e.g. an unlisted shell command) are the model's
    problem to route around, so they are only counted."""
    from .usage import long_path, subagent_dir
    files = [Path(transcript)]
    sdir = long_path(subagent_dir(transcript))
    if sdir.is_dir():
        files += sorted(sdir.glob("*.jsonl"))
    tools: dict[str, str] = {}
    edit, other, example = 0, 0, ""
    for f in files:
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            if '"tool_use"' not in line and '"tool_result"' not in line:
                continue
            try:
                content = (json.loads(line).get("message") or {}).get("content")
            except ValueError:
                continue
            for b in content if isinstance(content, list) else []:
                if b.get("type") == "tool_use":
                    tools[b.get("id", "")] = b.get("name", "")
                elif b.get("type") == "tool_result" and b.get("is_error"):
                    c = b.get("content")
                    text = c if isinstance(c, str) else json.dumps(c)
                    if "requested permissions" not in text and "sensitive file" not in text:
                        continue
                    if tools.get(b.get("tool_use_id", "")) in EDIT_TOOLS or "sensitive file" in text:
                        edit += 1
                        example = example or text[:200]
                    else:
                        other += 1
    return {"edit": edit, "other": other, "example": example}


def verify(task: dict, suite: dict, ws: Path, result_text: str) -> dict:
    out = {"verify_ok": None, "answer_ok": None, "verify_excerpt": ""}
    hidden = task.get("hidden")
    if hidden:
        src = (suite["_dir"] / hidden).resolve()
        for f in src.rglob("*"):
            if f.is_file():
                dst = ws / f.relative_to(src)
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, dst)
    if task.get("verify"):
        oks, excerpts = [], []
        # verify may be one command or a list of commands; all must pass
        cmds = task["verify"] if isinstance(task["verify"][0], list) else [task["verify"]]
        for cmd in cmds:
            r = proc.run(expand(cmd, suite), cwd=ws, timeout=task.get("verify_timeout", 600))
            oks.append(r.ok)
            excerpts.append((r.stdout + r.stderr)[-1500:])
        out["verify_ok"] = all(oks)
        out["verify_excerpt"] = "\n".join(excerpts)[-3000:]
    if task.get("answer_contains"):
        low = (result_text or "").lower()
        out["answer_ok"] = all(s.lower() in low for s in task["answer_contains"])
    checks = [v for v in (out["verify_ok"], out["answer_ok"]) if v is not None]
    out["correct"] = all(checks) if checks else None
    return out


def run_one(claude: str, suite: dict, task: dict, arm: str, rep: int, run_dir: Path, args) -> dict:
    rdir = (run_dir / f"{task['id']}__{arm}__{rep}").resolve()     # absolute: hooks run with cwd = workspace
    rdir.mkdir(parents=True)
    ws = prepare_workspace(suite, rdir, task)
    argv = claude_argv(claude, task, suite, arm, args)
    env = {"TIERFLOW_HOME": str(rdir / "tierflow-home")}
    if arm == "optimized" and args.mode:
        env["TIERFLOW_MODE"] = args.mode
    rec = {"task": task["id"], "category": task.get("category"), "arm": arm, "rep": rep, "workspace": str(ws)}
    t0 = time.monotonic()
    r = proc.run(argv, cwd=ws, timeout=task.get("timeout", suite["suite"].get("timeout", 1800)), env=env)
    rec["wall_s"] = round(time.monotonic() - t0, 1)
    (rdir / "stdout.json").write_text(r.stdout, encoding="utf-8")
    (rdir / "stderr.txt").write_text(r.stderr, encoding="utf-8")
    try:
        out = json.loads(r.stdout.strip().splitlines()[-1]) if r.stdout.strip() else {}
    except (ValueError, IndexError):
        out = {}
    rec.update({
        "exit_code": r.returncode, "timed_out": r.timed_out, "start_error": r.error,
        "is_error": out.get("is_error"), "subtype": out.get("subtype"),
        "cost_usd_cli": out.get("total_cost_usd"), "num_turns": out.get("num_turns"),
        "duration_ms": out.get("duration_ms"), "session_id": out.get("session_id"),
        "model_usage_cli": out.get("modelUsage"),
    })
    tp = find_transcript(out.get("session_id"))
    if tp:
        su = session_usage(tp, load_config(), incremental=False)
        t = su.total
        rec["transcript"] = str(tp)
        rec["subagent_transcripts"] = len(su.subagents)
        rec["tokens"] = {m: {"requests": u.requests, "input": u.input, "output": u.output, "cache_read": u.cache_read,
                             "cache_write": u.cache_write_5m + u.cache_write_1h, "thinking": u.thinking,
                             "cost_usd": u.cost_usd} for m, u in t.models.items()}
        rec["cost_usd_transcript"] = round(t.cost_usd, 6)
        rec["model_calls"] = t.requests
        denied = permission_denials(tp)
        rec["permission_denials"] = denied["edit"] + denied["other"]
        if denied["edit"]:
            rec["harness_error"] = f"edit blocked by the CLI {denied['edit']}x (run excluded): {denied['example']}"
    tf_metrics = list((rdir / "tierflow-home" / "metrics").glob("*.jsonl"))
    if tf_metrics:
        events = [json.loads(l) for f in tf_metrics for l in f.read_text(encoding="utf-8").splitlines() if l.strip()]
        rec["delegations"] = sum(1 for e in events if e["event"] == "delegation")
        rec["escalations"] = sum(1 for e in events if e["event"] == "agent_result" and e.get("advice") in ("escalate", "reassess"))
        rec["retries"] = sum(1 for e in events if e["event"] == "agent_result" and e.get("advice") == "retry")
    rec.update(verify(task, suite, ws, out.get("result", "")))
    diff = proc.run(["git", "diff", "--shortstat", "HEAD"], cwd=ws, timeout=60)
    rec["diff"] = diff.stdout.strip()
    rec["needs_human"] = bool(rec.get("is_error") or rec.get("timed_out") or rec.get("correct") is False
                              or rec.get("harness_error"))
    if not args.keep_workspaces:
        remove_tree(ws, suite["suite"].get("link", []))
    return rec


def remove_tree(path: Path, links: list[str] | None = None) -> None:
    """rmtree that also handles git's read-only object files on Windows.
    Shared dependency links (junctions/symlinks) are removed FIRST, so deleting the workspace can never
    delete the original repo's node_modules."""
    import stat
    for name in links or []:
        p = path / name
        try:
            if p.is_symlink() or (os.name == "nt" and p.exists() and os.lstat(p).st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT):
                os.unlink(p) if p.is_symlink() else os.rmdir(p)     # removes the link, not the target
        except OSError as e:
            raise RuntimeError(f"refusing to delete workspace: could not remove link {p}: {e}")
        if p.exists():
            raise RuntimeError(f"refusing to delete workspace: {p} is still present")

    def onerror(func, p, _exc):
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except OSError:
            pass
    shutil.rmtree(path, onerror=onerror)


def _stats(vals: list[float]) -> str:
    vals = [v for v in vals if v is not None]
    if not vals:
        return "n/a"
    if len(vals) == 1:
        return f"{vals[0]:.3f}"
    return f"{statistics.mean(vals):.3f} ({min(vals):.3f}-{max(vals):.3f})"


def summarize(records: list[dict]) -> str:
    excluded = [r for r in records if r.get("harness_error")]
    records = [r for r in records if not r.get("harness_error")]
    tasks = list(dict.fromkeys(r["task"] for r in records))
    lines = ["# tierflow benchmark", "",
             f"{len(records)} runs. Cost = Claude Code's own `total_cost_usd` (list-price estimate); "
             "tokens from transcripts incl. subagents. Mean (min-max) over repeats.", ""]
    if excluded:
        lines += [f"**{len(excluded)} run(s) excluded for harness errors:**", ""]
        lines += [f"- {r['task']} / {r['arm']} / {r['rep']}: {str(r['harness_error'])[:160]}" for r in excluded]
        lines.append("")
    lines += [
             "| task | arm | cost $ | wall s | turns | model calls | correct | needs human |",
             "|---|---|---|---|---|---|---|---|"]
    for t in tasks:
        for arm in ARMS:
            rs = [r for r in records if r["task"] == t and r["arm"] == arm]
            if not rs:
                continue
            ok = sum(1 for r in rs if r.get("correct"))
            human = sum(1 for r in rs if r.get("needs_human"))
            lines.append(f"| {t} | {arm} | {_stats([r.get('cost_usd_cli') for r in rs])} | {_stats([r.get('wall_s') for r in rs])} | "
                         f"{_stats([r.get('num_turns') for r in rs])} | {_stats([r.get('model_calls') for r in rs])} | {ok}/{len(rs)} | {human}/{len(rs)} |")
    lines.append("")
    tot = {}
    for arm in ARMS:
        rs = [r for r in records if r["arm"] == arm]
        if rs:
            tot[arm] = (sum(r.get("cost_usd_cli") or 0 for r in rs), sum(1 for r in rs if r.get("correct")), len(rs),
                        sum(r.get("wall_s") or 0 for r in rs))
    for arm, (c, ok, n, w) in tot.items():
        lines.append(f"- **{arm}**: total ${c:.2f}, correct {ok}/{n}, wall {w:.0f}s")
    if len(tot) == 2 and tot["baseline"][0] > 0:
        d = (tot["optimized"][0] - tot["baseline"][0]) / tot["baseline"][0] * 100
        lines.append(f"- cost change: {d:+.1f}%  (correctness {tot['optimized'][1]}/{tot['optimized'][2]} vs "
                     f"{tot['baseline'][1]}/{tot['baseline'][2]}). With few repeats, treat differences under ~20% as noise.")
    lines.append("\nPer-model tokens are in results.jsonl (`tokens`).")
    return "\n".join(lines)


def cmd_run(args) -> int:
    suite = load_suite(Path(args.suite))
    claude = find_claude(args.claude)
    tasks = [t for t in suite["task"] if not args.tasks or t["id"] in args.tasks.split(",")]
    arms = args.arms.split(",")
    if args.dry_run:
        for t in tasks:
            for arm in arms:
                print(f"[{t['id']} / {arm}]", " ".join(claude_argv(claude or "claude", t, suite, arm, args)))
        total = sum(t.get("max_budget_usd", suite["suite"].get("max_budget_usd", 2.0)) for t in tasks) * len(arms) * args.repeats
        print(f"\nworst-case spend (sum of --max-budget-usd caps): ${total:.2f}")
        return 0
    if not claude:
        print("claude CLI not found; pass --claude <path>", file=sys.stderr)
        return 2
    run_dir = Path(args.out or (Path.home() / "tierflow-bench" / datetime.now().strftime("%Y%m%d-%H%M%S")))
    if inside_claude_dir(run_dir):
        print(f"refusing --out {run_dir}: it is inside a .claude folder, where Claude Code blocks edits, so every "
              "edit task would fail. Pick a folder outside .claude (and outside %TEMP% on Windows, where antivirus "
              "may delete build outputs).", file=sys.stderr)
        return 2
    run_dir.mkdir(parents=True, exist_ok=True)
    # the CLI version decides which models the aliases resolve to, so it is part of every result
    exe = [sys.executable, claude] if claude.endswith(".py") else [claude]
    v = proc.run([*exe, "--version"], timeout=60)
    cli_version = (v.stdout or "").strip().split(" ")[0] or None
    print(f"claude CLI: {claude} ({cli_version or 'version unknown'})", flush=True)
    results = run_dir / "results.jsonl"
    records = []
    for rep in range(args.repeats):
        for t in tasks:
            for arm in (arms if rep % 2 == 0 else list(reversed(arms))):   # alternate order to spread cache/time effects
                print(f"run {t['id']} / {arm} / {rep} ...", flush=True)
                try:
                    rec = run_one(claude, suite, t, arm, rep, run_dir, args)
                except (RuntimeError, OSError) as e:
                    rec = {"task": t["id"], "arm": arm, "rep": rep, "harness_error": str(e), "needs_human": True}
                rec["cli_version"] = cli_version
                records.append(rec)
                with open(results, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(rec, default=str) + "\n")
                print(f"   cost={rec.get('cost_usd_cli')} correct={rec.get('correct')} wall={rec.get('wall_s')}s "
                      f"{rec.get('harness_error') or ''}", flush=True)
    (run_dir / "report.md").write_text(summarize(records), encoding="utf-8")
    print(f"\nresults: {results}\nreport:  {run_dir / 'report.md'}")
    return 0


def cmd_report(args) -> int:
    path = Path(args.results)
    path = path / "results.jsonl" if path.is_dir() else path
    records = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    print(summarize(records))
    return 0


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="orch bench")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("suite")
    r.add_argument("--repeats", type=int, default=1)
    r.add_argument("--tasks", help="comma-separated task ids")
    r.add_argument("--arms", default="baseline,optimized")
    r.add_argument("--main-model", help="main-session model for BOTH arms (e.g. sonnet, opus)")
    r.add_argument("--mode", help="tierflow mode for the optimized arm")
    r.add_argument("--claude", help="path to the claude CLI")
    r.add_argument("--out", help="results directory")
    r.add_argument("--keep-workspaces", action="store_true")
    r.add_argument("--dry-run", action="store_true")
    r.set_defaults(fn=cmd_run)
    s = sub.add_parser("report")
    s.add_argument("results")
    s.set_defaults(fn=cmd_report)
    args = p.parse_args(argv)
    return args.fn(args)
