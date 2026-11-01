import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class TempHomeCase(unittest.TestCase):
    """Isolates ~/.claude/tierflow state per test via TIERFLOW_HOME."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="tierflow-test-"))
        self.home = self.tmp / "home"
        self._old = {k: os.environ.get(k) for k in ("TIERFLOW_HOME", "TIERFLOW_MODE", "CLAUDE_PROJECT_DIR")}
        os.environ["TIERFLOW_HOME"] = str(self.home)
        os.environ.pop("TIERFLOW_MODE", None)
        os.environ.pop("CLAUDE_PROJECT_DIR", None)

    def tearDown(self):
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, rel: str, text: str, base: Path | None = None) -> Path:
        p = (base or self.tmp) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    def git_repo(self, name: str = "repo") -> Path:
        repo = self.tmp / name
        repo.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        return repo

    def commit(self, repo: Path):
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "x"], cwd=repo, check=True)

    def run_hook(self, event: str, payload: dict, env: dict | None = None):
        r = subprocess.run([sys.executable, str(ROOT / "scripts" / "hook.py"), event], input=json.dumps(payload).encode(),
                           capture_output=True, env={**os.environ, **(env or {})}, timeout=60)
        out = r.stdout.decode().strip()
        return r.returncode, (json.loads(out) if out else None), r.stderr.decode()


def assistant_line(mid: str, model: str, inp=10, out=100, cache_read=0, cw=0, cw1h=None, thinking=None, side=False) -> str:
    usage = {"input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": cache_read,
             "cache_creation_input_tokens": cw + (cw1h or 0)}
    if cw1h is not None:
        usage["cache_creation"] = {"ephemeral_5m_input_tokens": cw, "ephemeral_1h_input_tokens": cw1h}
    if thinking is not None:
        usage["output_tokens_details"] = {"thinking_tokens": thinking}
    return json.dumps({"type": "assistant", "isSidechain": side, "timestamp": "2026-10-31T10:00:00Z",
                       "message": {"id": mid, "model": model, "usage": usage}}) + "\n"
