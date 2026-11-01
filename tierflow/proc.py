"""Safe subprocess execution: argument lists only (never a shell), timeouts, process-tree kill."""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

IS_WINDOWS = sys.platform.startswith("win")


@dataclass
class ProcResult:
    argv: list[str]
    returncode: int | None
    stdout: str
    stderr: str
    duration_s: float
    timed_out: bool = False
    error: str | None = None      # could not start (missing executable, etc.)

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out and self.error is None


def which(name: str) -> str | None:
    return shutil.which(name)


def _kill_tree(proc: subprocess.Popen) -> None:
    try:
        if IS_WINDOWS:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        proc.kill()


def run(argv: list[str], cwd: str | Path | None = None, timeout: float = 600,
        env: dict[str, str] | None = None, stdin_text: str | None = None) -> ProcResult:
    if not argv:
        raise ValueError("empty argv")
    exe = which(argv[0]) or argv[0]
    full = [exe, *argv[1:]]
    start = time.monotonic()
    kwargs: dict = dict(cwd=str(cwd) if cwd else None, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
                        env={**os.environ, **(env or {})})
    if IS_WINDOWS:
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen(full, **kwargs)
    except OSError as e:
        return ProcResult(argv, None, "", "", time.monotonic() - start, error=f"{type(e).__name__}: {e}")
    try:
        out, err = proc.communicate(input=stdin_text.encode() if stdin_text is not None else None, timeout=timeout)
        timed_out = False
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        try:
            out, err = proc.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            out, err = b"", b""
        timed_out = True
    except KeyboardInterrupt:
        _kill_tree(proc)
        raise
    dec = lambda b: (b or b"").decode("utf-8", errors="replace")
    return ProcResult(argv, proc.returncode, dec(out), dec(err), time.monotonic() - start, timed_out)
