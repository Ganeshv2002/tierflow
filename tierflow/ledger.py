"""Per-session state shared by hooks and the CLI, with a simple cross-process file lock."""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .config import home_dir

FOLLOW_UP = re.compile(
    r"^\s*(still|again|try again|retry|same (error|issue|problem)|that didn'?t work|didn'?t work|not working|it fails|"
    r"still (fails|failing|broken|wrong)|continue|keep going|go on|nope|no,|it'?s still)\b", re.I)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sessions_dir() -> Path:
    return home_dir() / "sessions"


def _safe_id(session_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", session_id)[:100] or "unknown"


@contextmanager
def _locked(path: Path, timeout: float = 5.0):
    lock = path.with_suffix(".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    fd = None
    while fd is None:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > 15:     # stale lock from a killed hook
                    lock.unlink(missing_ok=True)
                    continue
            except OSError:
                pass
            if time.monotonic() > deadline:
                raise TimeoutError(f"ledger lock busy: {lock}")
            time.sleep(0.02)
    try:
        yield
    finally:
        os.close(fd)
        lock.unlink(missing_ok=True)


def _empty(session_id: str) -> dict:
    return {"session_id": session_id, "created": now_iso(), "project_dir": None, "transcript_path": None,
            "task": None, "task_seq": 0, "delegations": {}, "agent_ids": {}, "validations": [], "last_cost": 0.0}


class Ledger:
    def __init__(self, session_id: str):
        self.session_id = session_id
        self.path = _sessions_dir() / f"{_safe_id(session_id)}.json"

    def read(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return _empty(self.session_id)

    @contextmanager
    def update(self):
        with _locked(self.path):
            data = self.read()
            yield data
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)


# ---- task lifecycle ------------------------------------------------------------------------

def start_or_continue_task(data: dict, prompt: str, classification: dict, cost_now: float) -> tuple[dict, bool]:
    """A new prompt starts a new task unless it is clearly a follow-up on the current one."""
    task = data.get("task")
    if task and FOLLOW_UP.search(prompt or ""):
        task["prompts"] += 1
        return task, False
    data["task_seq"] = data.get("task_seq", 0) + 1
    task = {"key": f"t{data['task_seq']}", "started": now_iso(), "cost_at_start": cost_now, "prompts": 1,
            "summary": (prompt or "").strip().replace("\n", " ")[:160], "classification": classification,
            "attempts": {}, "failures": [], "escalations": 0, "status": "open"}
    data["task"] = task
    return task, True


def prior_failures(data: dict) -> int:
    task = data.get("task") or {}
    return len(task.get("failures", []))


# ---- active session pointer (lets the CLI find the session without being told) -------------

def _pointer(project_dir: str | Path) -> Path:
    key = hashlib.sha1(str(Path(project_dir).resolve()).lower().encode()).hexdigest()[:16]
    return home_dir() / "active" / f"{key}.json"


def set_active(project_dir: str, session_id: str, transcript_path: str | None) -> None:
    p = _pointer(project_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"session_id": session_id, "transcript_path": transcript_path, "ts": now_iso(),
                             "project_dir": str(project_dir)}), encoding="utf-8")


def get_active(project_dir: str | Path) -> dict | None:
    """Walks up from project_dir so it also works from a subdirectory of the repo."""
    p = Path(project_dir).resolve()
    for cand in [p, *p.parents]:
        ptr = _pointer(cand)
        if ptr.is_file():
            try:
                return json.loads(ptr.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return None
    return None


# ---- metrics log ---------------------------------------------------------------------------

def emit(event: str, **fields) -> None:
    d = home_dir() / "metrics"
    d.mkdir(parents=True, exist_ok=True)
    rec = {"ts": now_iso(), "event": event, **fields}
    with open(d / f"{datetime.now(timezone.utc):%Y-%m}.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, default=str) + "\n")


def read_metrics(since_days: float | None = None) -> list[dict]:
    d = home_dir() / "metrics"
    if not d.is_dir():
        return []
    cutoff = time.time() - since_days * 86400 if since_days else None
    out = []
    for f in sorted(d.glob("*.jsonl")):
        with open(f, encoding="utf-8") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if cutoff:
                    try:
                        if datetime.fromisoformat(rec["ts"]).timestamp() < cutoff:
                            continue
                    except (KeyError, ValueError):
                        continue
                out.append(rec)
    return out
