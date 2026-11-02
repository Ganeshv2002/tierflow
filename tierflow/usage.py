"""Token usage from Claude Code transcripts (JSONL), main session + subagents.

Facts this relies on (checked against real transcripts, Claude Code 2.1.x):
  * assistant entries carry message.id, message.model and message.usage;
  * one API response is written as several entries (one per content block) with identical usage,
    so usage is counted once per message.id;
  * usage may include cache_creation.{ephemeral_5m_input_tokens, ephemeral_1h_input_tokens} and
    output_tokens_details.thinking_tokens. When absent we report n/a, never an estimate;
  * subagent transcripts live in <project>/<session_id>/subagents/agent-<id>.jsonl.
Parsing is incremental: a per-file cursor (byte offset + running totals) is kept under
~/.claude/tierflow/usage-cursors so hooks stay fast on long sessions.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .config import Config, home_dir
from .pricing import cost_of

FIELDS = ("input", "output", "cache_read", "cache_write_5m", "cache_write_1h")


@dataclass
class ModelTotals:
    requests: int = 0
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write_5m: int = 0
    cache_write_1h: int = 0
    thinking: int | None = None      # None = never reported by the transcript
    cost_usd: float | None = 0.0     # None = model not in pricing table

    def add(self, u: dict, cost: float | None) -> None:
        self.requests += 1
        for f in FIELDS:
            setattr(self, f, getattr(self, f) + u.get(f, 0))
        if u.get("thinking") is not None:
            self.thinking = (self.thinking or 0) + u["thinking"]
        if cost is None or self.cost_usd is None:
            self.cost_usd = None
        else:
            self.cost_usd += cost

    def merge(self, o: "ModelTotals") -> None:
        self.requests += o.requests
        for f in FIELDS:
            setattr(self, f, getattr(self, f) + getattr(o, f))
        if o.thinking is not None:
            self.thinking = (self.thinking or 0) + o.thinking
        self.cost_usd = None if (self.cost_usd is None or o.cost_usd is None) else self.cost_usd + o.cost_usd


@dataclass
class Totals:
    models: dict[str, ModelTotals] = field(default_factory=dict)
    first_ts: str | None = None
    last_ts: str | None = None

    @property
    def cost_usd(self) -> float:
        return sum(m.cost_usd or 0.0 for m in self.models.values())

    @property
    def unknown_models(self) -> list[str]:
        return [k for k, m in self.models.items() if m.cost_usd is None]

    @property
    def requests(self) -> int:
        return sum(m.requests for m in self.models.values())

    def merge(self, o: "Totals") -> None:
        for k, m in o.models.items():
            self.models.setdefault(k, ModelTotals()).merge(m)
        if o.first_ts and (not self.first_ts or o.first_ts < self.first_ts):
            self.first_ts = o.first_ts
        if o.last_ts and (not self.last_ts or o.last_ts > self.last_ts):
            self.last_ts = o.last_ts

    def to_dict(self) -> dict:
        return {"models": {k: asdict(v) for k, v in self.models.items()}, "first_ts": self.first_ts,
                "last_ts": self.last_ts, "cost_usd": round(self.cost_usd, 6), "unknown_models": self.unknown_models}

    @classmethod
    def from_dict(cls, d: dict) -> "Totals":
        return cls({k: ModelTotals(**v) for k, v in d.get("models", {}).items()}, d.get("first_ts"), d.get("last_ts"))


def normalize_usage(usage: dict) -> dict:
    cc = usage.get("cache_creation") or {}
    total_cw = usage.get("cache_creation_input_tokens", 0) or 0
    if cc:
        cw5, cw1 = cc.get("ephemeral_5m_input_tokens", 0) or 0, cc.get("ephemeral_1h_input_tokens", 0) or 0
    else:
        cw5, cw1 = total_cw, 0          # no split reported: Claude Code's default 5-minute TTL assumed
    details = usage.get("output_tokens_details") or {}
    return {
        "input": usage.get("input_tokens", 0) or 0,
        "output": usage.get("output_tokens", 0) or 0,
        "cache_read": usage.get("cache_read_input_tokens", 0) or 0,
        "cache_write_5m": cw5,
        "cache_write_1h": cw1,
        "thinking": details.get("thinking_tokens"),
    }


def long_path(path: str | Path) -> Path:
    """Windows refuses paths over 260 chars unless they use the \\\\?\\ prefix; subagent transcripts
    (<project>/<session>/subagents/agent-<id>.jsonl) easily exceed that for deep project folders."""
    p = Path(path)
    if os.name == "nt":
        s = str(p.absolute())
        if len(s) >= 240 and not s.startswith("\\\\?\\"):
            return Path("\\\\?\\" + s)
    return p


def _cursor_path(path: Path) -> Path:
    return home_dir() / "usage-cursors" / (hashlib.sha1(str(path.resolve()).lower().encode()).hexdigest()[:20] + ".json")


def scan_file(path: str | Path, cfg: Config, *, incremental: bool = True) -> Totals:
    path = long_path(path)
    state = {"offset": 0, "recent_ids": [], "totals": {}}
    cpath = _cursor_path(path)
    if incremental and cpath.is_file():
        try:
            state = json.loads(cpath.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    try:
        size = path.stat().st_size
    except OSError:
        return Totals()
    if size < state["offset"]:                  # file rewritten: start over
        state = {"offset": 0, "recent_ids": [], "totals": {}}
    totals = Totals.from_dict(state["totals"])
    recent: list[str] = state["recent_ids"]
    offset = state["offset"]
    # One response is written as several lines; some builds repeat the same usage, others write a
    # growing snapshot (output_tokens 8 -> 253). Keep the per-field max and commit when the id changes.
    pending = state.get("pending")

    def commit(p):
        nonlocal recent
        if not p or p["key"] in recent:
            return
        recent = (recent + [p["key"]])[-64:]
        totals.models.setdefault(p["model"], ModelTotals()).add(p["usage"], cost_of(p["model"], p["usage"], cfg))
        if p.get("ts"):
            totals.first_ts = totals.first_ts or p["ts"]
            totals.last_ts = p["ts"]

    with open(path, "rb") as fh:
        fh.seek(offset)
        for raw in fh:
            if not raw.endswith(b"\n"):          # partial line still being written
                break
            offset += len(raw)
            if b'"assistant"' not in raw:
                continue
            try:
                d = json.loads(raw)
            except ValueError:
                continue
            if d.get("type") != "assistant":
                continue
            msg = d.get("message") or {}
            mid, model, usage = msg.get("id"), msg.get("model"), msg.get("usage")
            if not usage or not model or model == "<synthetic>":
                continue
            key = mid or d.get("requestId") or d.get("uuid")
            if key in recent:
                continue
            u = normalize_usage(usage)
            if pending and pending["key"] == key:
                for f, v in u.items():
                    old = pending["usage"].get(f)
                    pending["usage"][f] = v if old is None else (old if v is None else max(old, v))
                pending["ts"] = d.get("timestamp") or pending.get("ts")
            else:
                commit(pending)
                pending = {"key": key, "model": model, "usage": u, "ts": d.get("timestamp")}
    if incremental:
        try:
            cpath.parent.mkdir(parents=True, exist_ok=True)
            cpath.write_text(json.dumps({"offset": offset, "recent_ids": recent, "totals": totals.to_dict(),
                                         "pending": pending}), encoding="utf-8")
        except OSError:
            pass
    commit(pending)          # include the last response in what we return (state keeps it pending)
    return totals


@dataclass
class SessionUsage:
    main: Totals
    subagents: dict[str, Totals]

    @property
    def total(self) -> Totals:
        t = Totals()
        t.merge(self.main)
        for s in self.subagents.values():
            t.merge(s)
        return t

    def to_dict(self) -> dict:
        return {"main": self.main.to_dict(), "subagents": {k: v.to_dict() for k, v in self.subagents.items()},
                "total": self.total.to_dict()}


def subagent_dir(transcript_path: str | Path) -> Path:
    p = Path(transcript_path)
    return p.parent / p.stem / "subagents"


def session_usage(transcript_path: str | Path, cfg: Config, *, incremental: bool = True) -> SessionUsage:
    main = scan_file(transcript_path, cfg, incremental=incremental)
    subs: dict[str, Totals] = {}
    sdir = long_path(subagent_dir(transcript_path))
    if sdir.is_dir():
        for f in sorted(sdir.glob("*.jsonl")):
            subs[f.stem] = scan_file(f, cfg, incremental=incremental)
    return SessionUsage(main, subs)


def _last_main_response(transcript_path: str | Path, tail_bytes: int = 262_144) -> dict | None:
    """The most recent main-session model response (reads only the file tail)."""
    p = Path(transcript_path)
    try:
        with open(p, "rb") as fh:
            fh.seek(max(0, p.stat().st_size - tail_bytes))
            lines = fh.read().splitlines()
    except OSError:
        return None
    for raw in reversed(lines):
        if b'"assistant"' not in raw:
            continue
        try:
            d = json.loads(raw)
        except ValueError:
            continue
        msg = d.get("message") or {}
        m = msg.get("model")
        if d.get("type") == "assistant" and m and m != "<synthetic>" and not d.get("isSidechain"):
            return msg
    return None


def last_model(transcript_path: str | Path, tail_bytes: int = 262_144) -> str | None:
    """Model of the most recent main-session response (reads only the file tail)."""
    msg = _last_main_response(transcript_path, tail_bytes)
    return msg.get("model") if msg else None


def last_context(transcript_path: str | Path, tail_bytes: int = 262_144) -> tuple[str | None, int | None]:
    """(model, context tokens) of the most recent main-session request: everything it sent (fresh input +
    cache read + cache write) plus what it wrote, i.e. roughly what the next request has to re-read."""
    msg = _last_main_response(transcript_path, tail_bytes)
    if not msg or not msg.get("usage"):
        return (msg or {}).get("model"), None
    u = normalize_usage(msg["usage"])
    return msg.get("model"), u["input"] + u["cache_read"] + u["cache_write_5m"] + u["cache_write_1h"] + u["output"]
