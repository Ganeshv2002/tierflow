"""Live dashboard: a read-only view of what Claude Code is doing right now and what it has cost.

The source of truth is the transcripts under ~/.claude/projects (every session, with or without tierflow's
hooks): main sessions are `<project>/<session>.jsonl`, subagents are
`<project>/<session>/subagents/agent-<id>.jsonl` with an `agent-<id>.meta.json` beside them (agent type,
description, `finished`). Files are parsed incrementally and kept in memory, so refreshing every few seconds
costs only the new lines. Nothing is written anywhere and the server only listens on 127.0.0.1.

State of an agent: `finished` (meta says so, or it handed its report back), `running` (unfinished and its
transcript changed in the last RUNNING_S seconds) or `stalled` (unfinished and silent: interrupted, or a very
long tool call). Both are inferred from files, so they are labelled as such in the page.
"""
from __future__ import annotations

import json
import os
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .classifier import strip_attachments
from .config import Config
from .pricing import cost_of
from .usage import long_path, normalize_usage

BUCKET_S = 300              # raw time resolution kept per file
RUNNING_S = 180             # unfinished agent that wrote this recently = running
WORKING_S = 120             # main session that wrote this recently = working
IDLE_S = 1800               # ... within this = idle, otherwise inactive
WINDOWS = {"1h": 3600, "24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400}
FAMILIES = ("opus", "sonnet", "haiku")      # the three colours that validate together; the rest is "other"
SKIP_PROMPTS = ("<local-command", "<command-", "<bash-", "[Request interrupted")


def projects_dir() -> Path:
    return Path(os.environ.get("TIERFLOW_CLAUDE_PROJECTS") or Path.home() / ".claude" / "projects")


def family(model: str | None) -> str:
    m = (model or "").lower()
    return next((f for f in FAMILIES if f in m), "other")


def parse_window(name: str | None) -> int:
    return WINDOWS.get(name or "24h", WINDOWS["24h"])


def _epoch(ts: str | None) -> float | None:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() if ts else None
    except ValueError:
        return None


def _align(e: float, step: int) -> float:
    if step <= BUCKET_S:
        return e // step * step
    dt = datetime.fromtimestamp(e)
    dt = dt.replace(minute=0, second=0, microsecond=0) if step == 3600 else dt.replace(hour=0, minute=0, second=0, microsecond=0)
    return dt.timestamp()


class FileScan:
    """Incremental, in-memory reader of one transcript. Counts each model response once (several lines
    share a message id; some builds write a growing snapshot, so the per-field maximum wins), like
    usage.scan_file, which tests/test_dashboard.py checks it against."""

    def __init__(self, path: Path, cfg: Config):
        self.path, self.cfg = path, cfg
        self.offset = 0
        self.pending: dict | None = None
        self.recent: list[str] = []
        self.buckets: dict[tuple[int, str], list] = {}        # (bucket start, model) -> [req,in,out,cr,cw,cost,unpriced]
        self.first_ts = self.last_ts = self.cwd = self.started = None
        self.first_prompt = self.last_prompt = None
        self.last_model = self.last_ctx = self.last_action = None
        self.handed_back = False

    def refresh(self) -> None:
        try:
            size = self.path.stat().st_size
        except OSError:
            return
        if size < self.offset:                                 # rewritten: start over
            self.__init__(self.path, self.cfg)
        if size == self.offset:
            return
        with open(long_path(self.path), "rb") as fh:
            fh.seek(self.offset)
            for raw in fh:
                if not raw.endswith(b"\n"):                    # partial line still being written
                    break
                self.offset += len(raw)
                if b'"assistant"' in raw or b'"user"' in raw:
                    try:
                        self._line(json.loads(raw))
                    except ValueError:
                        continue

    # -- lines ------------------------------------------------------------------------------------
    def _line(self, d: dict) -> None:
        if self.cwd is None and d.get("cwd"):
            self.cwd = d["cwd"]
        if self.started is None:
            self.started = _epoch(d.get("timestamp"))          # the first line, not the first model response
        if d.get("type") == "user":
            self._user(d)
        elif d.get("type") == "assistant":
            self._assistant(d)

    def _user(self, d: dict) -> None:
        c = (d.get("message") or {}).get("content")
        text = c if isinstance(c, str) else next(
            (b.get("text") for b in (c if isinstance(c, list) else []) if isinstance(b, dict) and b.get("type") == "text"), None)
        if not text or d.get("isMeta"):
            return
        text = " ".join(strip_attachments(text).split())
        if not text or text.startswith(SKIP_PROMPTS):
            return
        self.first_prompt = self.first_prompt or text[:300]
        self.last_prompt = text[:300]

    def _assistant(self, d: dict) -> None:
        msg = d.get("message") or {}
        mid, model, usage = msg.get("id"), msg.get("model"), msg.get("usage")
        if not usage or not model or model == "<synthetic>":
            return
        for b in msg.get("content") or []:
            if isinstance(b, dict) and b.get("type") == "tool_use":
                inp = b.get("input") or {}
                arg = next((str(inp[k]) for k in ("file_path", "path", "pattern", "command", "description", "url", "query")
                            if inp.get(k)), "")
                self.last_action = f"{b.get('name', '')} {' '.join(arg.split())}".strip()[:90]
                self.handed_back = b.get("name") == "SubagentHandback"
        u = normalize_usage(usage)
        self.last_model = model
        self.last_ctx = u["input"] + u["cache_read"] + u["cache_write_5m"] + u["cache_write_1h"] + u["output"]
        key = mid or d.get("requestId") or d.get("uuid")
        if key in self.recent:
            return
        ts = d.get("timestamp")
        if self.pending and self.pending["key"] == key:
            for f, v in u.items():
                old = self.pending["usage"].get(f)
                self.pending["usage"][f] = v if old is None else (old if v is None else max(old, v))
            self.pending["ts"] = ts or self.pending.get("ts")
        else:
            self._commit(self.pending)
            self.pending = {"key": key, "model": model, "usage": u, "ts": ts}

    def _commit(self, p: dict | None) -> None:
        if not p or p["key"] in self.recent:
            return
        self.recent = (self.recent + [p["key"]])[-64:]
        e = _epoch(p["ts"])
        if e is not None:
            self.first_ts = self.first_ts or e
            self.last_ts = e
        slot = self.buckets.setdefault((int((e if e is not None else self.last_ts or 0) // BUCKET_S * BUCKET_S), p["model"]),
                                       [0, 0, 0, 0, 0, 0.0, 0])
        _add(slot, p["usage"], cost_of(p["model"], p["usage"], self.cfg))

    def items(self):
        """(bucket start, model, values) including the response still being written."""
        yield from ((k[0], k[1], v) for k, v in self.buckets.items())
        p = self.pending
        if p and p["key"] not in self.recent:
            e = _epoch(p["ts"]) or self.last_ts or 0
            slot = [0, 0, 0, 0, 0, 0.0, 0]
            _add(slot, p["usage"], cost_of(p["model"], p["usage"], self.cfg))
            yield int(e // BUCKET_S * BUCKET_S), p["model"], slot

    def mtime(self) -> float:
        try:
            return self.path.stat().st_mtime
        except OSError:
            return 0.0


def _add(slot: list, u: dict, cost: float | None) -> None:
    slot[0] += 1
    slot[1] += u.get("input", 0)
    slot[2] += u.get("output", 0)
    slot[3] += u.get("cache_read", 0)
    slot[4] += u.get("cache_write_5m", 0) + u.get("cache_write_1h", 0)
    if cost is None:
        slot[6] += 1
    else:
        slot[5] += cost


def _sum(scans) -> dict:
    """Per-model totals over everything the given scans have seen."""
    out: dict[str, list] = {}
    for s in scans:
        for _, model, v in s.items():
            t = out.setdefault(model, [0, 0, 0, 0, 0, 0.0, 0])
            for i in range(7):
                t[i] += v[i]
    return out


def _model_row(model: str, v: list) -> dict:
    return {"model": model, "family": family(model), "requests": v[0], "input": v[1], "output": v[2],
            "cache_read": v[3], "cache_write": v[4], "cost": round(v[5], 6), "priced": v[6] == 0,
            "tokens": v[1] + v[2] + v[3] + v[4]}


class Dashboard:
    def __init__(self, cfg: Config, projects: Path | str | None = None):
        self.cfg = cfg
        self.root = Path(projects) if projects else projects_dir()
        self.scans: dict[Path, FileScan] = {}
        self.lock = threading.Lock()

    def _scan(self, path: Path) -> FileScan:
        s = self.scans.get(path)
        if s is None:
            s = self.scans[path] = FileScan(path, self.cfg)
        s.refresh()
        return s

    def snapshot(self, window_s: int = 86400, include_bench: bool = False, now: float | None = None) -> dict:
        with self.lock:
            return self._snapshot(window_s, include_bench, now if now is not None else time.time())

    def _snapshot(self, window_s: int, include_bench: bool, now: float) -> dict:
        cutoff = now - window_s
        mains = [p for p in self.root.glob("*/*.jsonl")]
        subs_by_session: dict[Path, list[Path]] = {}
        for p in self.root.glob("*/*/subagents/agent-*.jsonl"):
            subs_by_session.setdefault(p.parents[1], []).append(p)

        def bench(cwd: str | None) -> bool:
            c = (cwd or "").lower().replace("\\", "/")
            return "/tierflow-bench/" in c or "/bench/results/" in c

        sessions, agents, in_window = [], [], []
        for mp in mains:
            sdir = mp.parent / mp.stem
            sub_files = subs_by_session.get(sdir, [])
            try:
                touched = max([mp.stat().st_mtime] + [f.stat().st_mtime for f in sub_files])
            except OSError:
                continue
            if touched < cutoff:
                continue
            main = self._scan(mp)
            if bench(main.cwd) and not include_bench:
                continue
            subs = [(f, self._scan(f)) for f in sub_files]
            session_agents = []
            for f, s in subs:
                meta = {}
                try:
                    meta = json.loads((f.parent / (f.name[:-6] + ".meta.json")).read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    pass
                m = s.mtime()
                finished = bool(meta.get("finished")) or s.handed_back
                state = "finished" if finished else ("running" if now - m < RUNNING_S else "stalled")
                if m < cutoff and state != "running":
                    continue
                per_model = {k: _model_row(k, v) for k, v in _sum([s]).items()}
                row = {"id": f.stem.replace("agent-", ""), "session": mp.stem[:8], "project": _project(main.cwd or s.cwd, mp.parent.name),
                       "type": meta.get("agentType") or "agent", "description": meta.get("description") or "",
                       "task": s.first_prompt or "", "doing": s.last_action if state != "finished" else "",
                       "state": state, "model": family_primary(per_model, s.last_model), "models": sorted(per_model.values(), key=lambda r: -r["requests"]),
                       "started": s.started or s.first_ts, "last": m, "requests": sum(r["requests"] for r in per_model.values()),
                       "tokens": sum(r["tokens"] for r in per_model.values()),
                       "cost": round(sum(r["cost"] for r in per_model.values()), 6), "priced": all(r["priced"] for r in per_model.values())}
                begin = s.started or s.first_ts
                row["elapsed"] = round((m if state == "finished" else now) - begin) if begin else None
                session_agents.append(row)
            mm = main.mtime()
            running = sum(a["state"] == "running" for a in session_agents)
            state = "working" if (now - mm < WORKING_S or running) else ("idle" if now - mm < IDLE_S else "inactive")
            total = _sum([main] + [s for _, s in subs])
            rows = [_model_row(k, v) for k, v in total.items()]
            sessions.append({"id": mp.stem[:8], "project": _project(main.cwd, mp.parent.name), "task": main.last_prompt or "",
                             "doing": main.last_action or "", "model": main.last_model, "family": family(main.last_model),
                             "context": main.last_ctx, "state": state, "last": mm,
                             "agents_running": running, "agents_total": len(session_agents),
                             "cost": round(sum(r["cost"] for r in rows), 6), "tokens": sum(r["tokens"] for r in rows),
                             "priced": all(r["priced"] for r in rows)})
            agents += session_agents
            in_window += [main] + [s for _, s in subs]

        # usage inside the window, by model and over time
        step = BUCKET_S if window_s <= 7200 else (3600 if window_s <= 2 * 86400 else 86400)
        by_model: dict[str, list] = {}
        series: dict[float, dict[str, list]] = {}
        for s in in_window:
            for b, model, v in s.items():
                if b + BUCKET_S <= cutoff:
                    continue
                t = by_model.setdefault(model, [0, 0, 0, 0, 0, 0.0, 0])
                f = series.setdefault(_align(b, step), {}).setdefault(family(model), [0, 0, 0, 0, 0, 0.0, 0])
                for i in range(7):
                    t[i] += v[i]
                    f[i] += v[i]
        starts, t = [], _align(cutoff, step)
        while t <= now:
            starts.append(t)
            nxt = _align(t + step * 1.01, step)
            t = nxt if nxt > t else t + step
        models = sorted((_model_row(k, v) for k, v in by_model.items()), key=lambda r: -r["cost"])
        total = {k: sum(r[k] for r in models) for k in ("requests", "input", "output", "cache_read", "cache_write", "tokens", "cost")}
        total["cost"] = round(total["cost"], 6)
        agents.sort(key=lambda a: ({"running": 0, "stalled": 1, "finished": 2}[a["state"]], -(a["last"] or 0)))
        sessions.sort(key=lambda s: ({"working": 0, "idle": 1, "inactive": 2}[s["state"]], -(s["last"] or 0)))
        return {
            "generated": now, "window_s": window_s, "step_s": step, "include_bench": include_bench,
            "kpi": {"agents_running": sum(a["state"] == "running" for a in agents),
                    "agents_stalled": sum(a["state"] == "stalled" for a in agents),
                    "agents_finished": sum(a["state"] == "finished" for a in agents),
                    "sessions_working": sum(s["state"] == "working" for s in sessions), "sessions": len(sessions)},
            "total": total, "models": models, "unpriced": [r["model"] for r in models if not r["priced"]],
            "series": [{"t": s, **{f: {"cost": round(v[5], 6), "tokens": v[1] + v[2] + v[3] + v[4], "requests": v[0]}
                                   for f, v in series.get(s, {}).items()}} for s in starts],
            "agents": agents[:60], "sessions": sessions[:40],
            "warn_context_tokens": int(self.cfg.section("session").get("warn_context_tokens", 0) or 0),
            "files": len(self.scans),
        }


def family_primary(per_model: dict, last_model: str | None) -> str | None:
    """The model an agent mainly ran on: the one with most requests (ties: the latest)."""
    if not per_model:
        return last_model
    best = max(per_model.values(), key=lambda r: (r["requests"], r["model"] == last_model))
    return best["model"]


def _project(cwd: str | None, folder: str) -> str:
    if cwd:
        return cwd.replace("\\", "/").rstrip("/").split("/")[-1] or cwd
    return folder.split("-")[-1] or folder


# ---- local server ------------------------------------------------------------------------------

def make_server(cfg: Config, port: int = 8765, projects: Path | str | None = None) -> ThreadingHTTPServer:
    dash = Dashboard(cfg, projects)
    html = Path(__file__).with_name("dashboard.html").read_bytes()
    cache: dict = {}

    class Handler(BaseHTTPRequestHandler):
        server_version = "tierflow-dashboard"

        def log_message(self, *a):                    # keep the terminal quiet
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'; img-src data:")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            host = (self.headers.get("Host") or "").lower()
            allowed = {f"127.0.0.1:{self.server.server_address[1]}", f"localhost:{self.server.server_address[1]}"}
            if host not in allowed:                    # blocks DNS-rebinding: only the loopback names are served
                return self._send(403, b"forbidden host", "text/plain")
            url = urlparse(self.path)
            try:
                if url.path in ("/", "/index.html"):
                    return self._send(200, html, "text/html; charset=utf-8")
                if url.path == "/api/snapshot":
                    q = parse_qs(url.query)
                    window = parse_window((q.get("window") or [None])[0])
                    bench = (q.get("bench") or ["0"])[0] == "1"
                    hit = cache.get((window, bench))
                    if not hit or time.time() - hit[0] > 1.0:       # several tabs share one scan
                        hit = cache[(window, bench)] = (time.time(), json.dumps(dash.snapshot(window, bench)).encode())
                    return self._send(200, hit[1], "application/json")
                self._send(404, b"not found", "text/plain")
            except (BrokenPipeError, ConnectionResetError):
                pass

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def serve(cfg: Config, port: int = 8765, open_browser: bool = False) -> int:
    try:
        httpd = make_server(cfg, port)
    except OSError as e:
        print(f"cannot listen on 127.0.0.1:{port}: {e}")
        return 2
    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    print(f"tierflow dashboard: {url}   (read-only, local only; Ctrl+C to stop)", flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0
