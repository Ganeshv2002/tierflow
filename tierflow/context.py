"""Deterministic context builder: finds the few code locations a task needs.

Pipeline: codebase map -> explicit paths -> symbol definitions (language-aware regex via ripgrep)
-> term/filename matches -> rank -> merge overlapping snippets -> cut to a token budget.
Everything returned carries path:line so an agent can verify it against the source.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import proc
from .codemap import discover, stem, terms_of
from .config import Config, home_dir

CODE_EXT = ["ts", "tsx", "js", "jsx", "mjs", "cjs", "cs", "cshtml", "razor", "sql", "py", "java", "go",
            "vue", "svelte", "fs", "vb", "kt", "rb", "php"]
PROJECT_EXT = ["csproj", "sln", "json", "toml", "yaml", "yml", "xml", "props", "targets"]

IDENT = re.compile(r"`([^`]+)`|\b([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)\b")
PATH_RX = re.compile(r"[\w./\\-]+\.[A-Za-z]{1,6}\b")


def est_tokens(text: str) -> int:
    """Rough estimate (chars/4). Real tokenizers differ by model; used only for budgeting."""
    return max(1, len(text) // 4)


def identifiers_of(task: str) -> list[str]:
    """Identifiers worth a definition search: code spans, camelCase, PascalCase, snake_case, dotted."""
    out: list[str] = []
    for m in IDENT.finditer(task):
        tok = (m.group(1) or m.group(2) or "").strip()
        if not tok or PATH_RX.fullmatch(tok):
            continue
        parts = [tok, *tok.split(".")] if "." in tok else [tok]
        for p in parts:
            p = p.strip("()[]{}<>;:,")
            if not re.fullmatch(r"[A-Za-z_]\w{2,}", p):
                continue
            is_code = bool(m.group(1)) or "_" in p or re.search(r"[a-z][A-Z]", p) or re.fullmatch(r"[A-Z][a-z]+[A-Z]\w*", p) \
                or re.fullmatch(r"I[A-Z]\w+|usp_\w+|[A-Z]{2,}\w*[a-z]\w*", p)
            if is_code and p not in out:
                out.append(p)
    return out[:12]


def definition_regex(name: str) -> str:
    n = re.escape(name)
    return (
        rf"\b(class|interface|record|struct|enum|type|function|def|namespace|module)\s+{n}\b"            # most languages
        rf"|\b(const|let|var|export)\s+(const\s+|let\s+|async\s+function\s+)?{n}\s*[=:(<]"              # TS/JS bindings
        rf"|(?i:\bcreate\s+(or\s+alter\s+)?(procedure|proc|table|view|function|trigger|type)\s+(\[?\w+\]?\.)?\[?{n}\b)"  # T-SQL
        rf"|^\s*(public|private|protected|internal|static|async|override|virtual|abstract|sealed|readonly|export|get|set|\s)*"
        rf"[\w<>\[\],.?]*\s*{n}\s*(<[^>]*>)?\s*\([^;]*$"                                               # C#/TS methods
        rf"|^\s*{n}\s*(:\s*[\w<>\[\]|, ]+)?\s*=\s*(async\s*)?\(?[\w\s,:]*\)?\s*=>"                        # arrow functions
    )


@dataclass
class Hit:
    path: str
    line: int
    text: str
    kind: str  # def | ref | path | map


class Searcher:
    """Backends in order: ripgrep (TIERFLOW_RG or PATH) -> `git grep -P` (tracked + untracked,
    respects .gitignore) -> pure Python (slow on big repos, same results)."""

    def __init__(self, root: Path, exclude_dirs: list[str], timeout: float = 60):
        self.root = root
        self.exclude = set(exclude_dirs)
        self.rg = os.environ.get("TIERFLOW_RG") or proc.which("rg")
        self.git = None
        if not self.rg and proc.which("git"):
            r = proc.run(["git", "rev-parse", "--is-inside-work-tree"], cwd=root, timeout=20)
            if r.ok and r.stdout.strip() == "true":
                self.git = "git"
        self.backend = "rg" if self.rg else ("git" if self.git else "python")
        self.timeout = timeout
        self._files: list[str] | None = None

    def _pathspecs(self, exts: list[str]) -> list[str]:
        # git's default pathspec is fnmatch-like ('*' crosses '/'), so exclude both top-level and nested dirs
        excl = [s for d in sorted(self.exclude) for s in (f":(exclude){d}/*", f":(exclude)*/{d}/*")]
        return [f"*.{e}" for e in exts] + excl

    def _git_files(self, exts: list[str]) -> list[str]:
        r = proc.run(["git", "-c", "core.quotepath=off", "ls-files", "-co", "--exclude-standard", "--",
                      *self._pathspecs(exts)], cwd=self.root, timeout=self.timeout)
        return sorted(set(r.stdout.splitlines())) if r.ok else []

    def _git_grep(self, pattern: str, ignore_case: bool, max_count: int, exts: list[str]) -> list[Hit]:
        argv = ["git", "-c", "core.quotepath=off", "grep", "-n", "-I", "-P", "--untracked", "--max-count", str(max_count)]
        if ignore_case:
            argv.append("-i")
        argv += ["-e", pattern, "--", *self._pathspecs(exts)]
        r = proc.run(argv, cwd=self.root, timeout=self.timeout)
        if r.returncode not in (0, 1):
            raise RuntimeError(f"git grep failed: {r.stderr.strip()[:300]}")
        hits = []
        for line in r.stdout.splitlines():
            parts = line.split(":", 2)
            if len(parts) == 3 and parts[1].isdigit():
                hits.append(Hit(parts[0], int(parts[1]), parts[2].strip()[:300], "ref"))
        return hits

    def _globs(self, exts: list[str]) -> list[str]:
        args = []
        for e in exts:
            args += ["-g", f"*.{e}"]
        for d in self.exclude:
            args += ["-g", f"!**/{d}/**"]
        return args

    def files(self, exts: list[str] | None = None) -> list[str]:
        exts = exts or CODE_EXT
        if self._files is not None and exts is CODE_EXT:
            return self._files
        if self.rg:
            r = proc.run([self.rg, "--files", "--path-separator", "/", *self._globs(exts)], cwd=self.root, timeout=self.timeout)
            files = sorted(r.stdout.splitlines()) if r.returncode in (0, 1) else []
        elif self.git:
            files = self._git_files(exts)
        else:
            files = []
            for dp, dns, fns in os.walk(self.root):
                dns[:] = [d for d in dns if d not in self.exclude and not d.startswith(".")]
                for fn in fns:
                    if fn.rsplit(".", 1)[-1].lower() in exts:
                        files.append(Path(dp, fn).relative_to(self.root).as_posix())
            files.sort()
        if exts is CODE_EXT:
            self._files = files
        return files

    def grep(self, pattern: str, *, ignore_case=False, max_count: int = 50, exts: list[str] | None = None) -> list[Hit]:
        exts = exts or CODE_EXT
        if self.rg:
            argv = [self.rg, "-n", "--no-heading", "--path-separator", "/", "--max-count", str(max_count),
                    "--max-columns", "300", "-e", pattern, *self._globs(exts)]
            if ignore_case:
                argv.insert(1, "-i")
            r = proc.run(argv, cwd=self.root, timeout=self.timeout)
            if r.returncode not in (0, 1):
                raise RuntimeError(f"rg failed: {r.stderr.strip()[:300]}")
            hits = []
            for line in r.stdout.splitlines():
                parts = line.split(":", 2)
                if len(parts) == 3 and parts[1].isdigit():
                    hits.append(Hit(parts[0], int(parts[1]), parts[2].strip()[:300], "ref"))
            return hits
        if self.git:
            return self._git_grep(pattern, ignore_case, max_count, exts)
        rx = re.compile(pattern, re.I if ignore_case else 0)
        hits = []
        for f in self.files(exts):
            try:
                with open(self.root / f, encoding="utf-8", errors="replace") as fh:
                    n = 0
                    for i, line in enumerate(fh, 1):
                        if rx.search(line):
                            hits.append(Hit(f, i, line.strip()[:300], "ref"))
                            n += 1
                            if n >= max_count:
                                break
            except OSError:
                continue
        return hits


@dataclass
class Snippet:
    start: int
    end: int
    text: str


@dataclass
class FileCtx:
    path: str
    score: float
    reasons: list[str] = field(default_factory=list)
    snippets: list[Snippet] = field(default_factory=list)
    anchor_lines: list[int] = field(default_factory=list)


@dataclass
class ContextPack:
    query: str
    root: str
    map_source: str | None
    map_hits: list[dict]
    files: list[FileCtx]
    suggested_tests: list[str]
    tokens_est: int
    truncated: bool
    cached: bool = False
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    def render(self) -> str:
        out = [f"# context for: {self.query[:200]}", "_paths relative to the repo root; token counts are estimates_"]
        if self.map_hits:
            out.append(f"\n## codebase map ({self.map_source})")
            for h in self.map_hits:
                loc = f":{h['line']}" if h.get("line") else ""
                out.append(f"- {h['kind']} **{h['name']}** ({h['source']}{loc})")
                if h.get("entry_points"):
                    out.append(f"  entry: {', '.join(h['entry_points'][:6])}")
                if h.get("tests"):
                    out.append(f"  tests: {', '.join(h['tests'][:4])}")
        out.append("\n## locations")
        for f in self.files:
            out.append(f"\n### {f.path}  (score {f.score:.1f}; {'; '.join(f.reasons[:3])})")
            for s in f.snippets:
                out.append(f"```{Path(f.path).suffix.lstrip('.')} L{s.start}-{s.end}\n{s.text}\n```")
            if not f.snippets and f.anchor_lines:
                out.append(f"lines: {', '.join(map(str, f.anchor_lines[:8]))}")
        if self.suggested_tests:
            out.append("\n## tests likely affected\n" + "\n".join(f"- {t}" for t in self.suggested_tests))
        if self.truncated:
            out.append("\n_truncated to the token budget; ask for a narrower query or read the files directly_")
        for n in self.notes:
            out.append(f"_note: {n}_")
        return "\n".join(out)


def _git_state(root: Path) -> str:
    head = proc.run(["git", "rev-parse", "HEAD"], cwd=root, timeout=20)
    dirty = proc.run(["git", "status", "--porcelain"], cwd=root, timeout=60)
    if not head.ok:
        return ""
    stamp = []
    for line in dirty.stdout.splitlines():
        p = root / line[3:].strip().strip('"')
        try:
            stamp.append(f"{line}:{p.stat().st_mtime_ns}")
        except OSError:
            stamp.append(line)
    return head.stdout.strip() + "|" + "|".join(stamp)


def _cache_path(root: Path, key: str) -> Path:
    rh = hashlib.sha1(str(root).lower().encode()).hexdigest()[:12]
    return home_dir() / "cache" / rh / f"ctx-{key}.json"


def _read_lines(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []


def build_context(task: str, root: str | Path, cfg: Config, *, budget_tokens: int | None = None,
                  use_cache: bool = True) -> ContextPack:
    root = Path(root).resolve()
    ccfg = cfg.section("context")
    budget = budget_tokens or ccfg.get("max_context_tokens", 6000)

    state = _git_state(root) if use_cache else ""
    key = hashlib.sha1(json.dumps([task, budget, state, ccfg], sort_keys=True, default=str).encode()).hexdigest()[:16]
    cpath = _cache_path(root, key)
    if use_cache and state and cpath.is_file():
        try:
            data = json.loads(cpath.read_text(encoding="utf-8"))
            files = [FileCtx(**{**f, "snippets": [Snippet(**s) for s in f["snippets"]]}) for f in data.pop("files")]
            pack = ContextPack(files=files, **{k: v for k, v in data.items() if k != "cached"})
            pack.cached = True
            return pack
        except (OSError, ValueError, TypeError, KeyError):
            pass

    searcher = Searcher(root, ccfg.get("exclude_dirs", []))
    scores: dict[str, FileCtx] = {}
    notes: list[str] = []
    if searcher.backend == "python":
        notes.append("no ripgrep or git repo: using the slow Python search (install ripgrep or set TIERFLOW_RG)")

    def bump(path: str, pts: float, reason: str, line: int | None = None):
        fc = scores.setdefault(path, FileCtx(path, 0.0))
        fc.score += pts
        if reason not in fc.reasons:
            fc.reasons.append(reason)
        if line and line not in fc.anchor_lines:
            fc.anchor_lines.append(line)

    # 1. codebase map
    map_hits, map_source, tests = [], None, []
    if ccfg.get("reuse_codebase_map", True):
        cm = discover(root, ccfg.get("extra_map_paths"))
        if cm.found:
            map_source = cm.describe()
            for h in cm.query(task):
                map_hits.append(h.to_dict())
                tests += h.tests
                for ep in h.entry_points:
                    sym, _, p = ep.partition("@")
                    if p and (root / p).is_file():
                        bump(p, 6 + h.score / 2, f"map entry {sym}")
                        name = sym.split(".")[-1]
                        if re.fullmatch(r"\w+", name):
                            for i, line in enumerate(_read_lines(root / p), 1):
                                if re.search(definition_regex(name), line):
                                    bump(p, 0, f"map entry {sym}", i)
                                    break

    # 2. explicit paths in the task
    for m in PATH_RX.finditer(task):
        rel = m.group(0).replace("\\", "/").lstrip("./")
        if (root / rel).is_file():
            bump(rel, 12, "named in task")
        else:
            base = rel.rsplit("/", 1)[-1]
            for f in searcher.files(CODE_EXT + PROJECT_EXT):
                if f.endswith("/" + base) or f == base:
                    bump(f, 8, f"matches named file {base}")

    # 3. symbol definitions + reference counts
    idents = identifiers_of(task)
    found_defs = False
    for ident in idents:
        try:
            defs = searcher.grep(definition_regex(ident), max_count=5)
        except RuntimeError as e:
            notes.append(str(e))
            defs = []
        for h in defs[:20]:
            bump(h.path, 10, f"defines {ident}", h.line)
            found_defs = True
        try:
            refs = searcher.grep(rf"\b{re.escape(ident)}\b", max_count=3)
        except RuntimeError:
            refs = []
        for h in refs[:60]:
            bump(h.path, 0.75, f"uses {ident}", h.line)

    # 4. plain terms -> filename + content matches (only when identifiers didn't already pin it)
    terms = [t for t in terms_of(task) if t.lower() not in {i.lower() for i in idents}][:8]
    files = searcher.files()
    for t in terms:
        if len(t) < 4:
            continue
        for f in files:
            if t in f.lower():
                bump(f, 2.5, f"path ~ {t}")
    if idents and not found_defs:
        notes.append(f"no definition found for: {', '.join(idents)}; fell back to term search")
    if (not found_defs) and terms:
        for t in [stem(t) for t in terms if len(t) >= 4][:5]:
            try:
                hits = searcher.grep(rf"\b{re.escape(t)}", ignore_case=True, max_count=2)[:80]
            except RuntimeError:
                continue
            nfiles = len({h.path for h in hits})
            weight = 2.0 if nfiles <= 3 else 1.0 if nfiles <= 10 else 0.3   # rare terms are informative
            for h in hits:
                bump(h.path, weight, f"mentions {t}", h.line)

    # 5. rank + snippets within budget
    ranked = sorted(scores.values(), key=lambda f: -f.score)
    ranked = [f for f in ranked if f.score >= 1.0][: ccfg.get("max_files", 12)]
    n = ccfg.get("snippet_lines", 8)
    used = 0
    truncated = False
    for fc in ranked:
        lines = _read_lines(root / fc.path)
        anchors = sorted(fc.anchor_lines, key=lambda l: l)[: ccfg.get("max_hits_per_file", 4)]
        ranges: list[list[int]] = []
        for a in anchors:
            s, e = max(1, a - 2), min(len(lines), a + n)
            if ranges and s <= ranges[-1][1] + 1:      # merge overlapping/adjacent ranges (dedup)
                ranges[-1][1] = max(ranges[-1][1], e)
            else:
                ranges.append([s, e])
        for s, e in ranges:
            text = "\n".join(lines[s - 1:e])
            cost = est_tokens(text) + 10
            if used + cost > budget:
                truncated = True
                break
            fc.snippets.append(Snippet(s, e, text))
            used += cost
        used += 15
        if truncated:
            break

    for f in ranked:
        low = f.path.lower()
        if any(k in low for k in ("test", "spec")) and f.path not in tests:
            tests.append(f.path)

    pack = ContextPack(task, str(root), map_source, map_hits, ranked, list(dict.fromkeys(tests))[:10],
                       used, truncated, notes=notes)
    # enforce the budget on what is actually rendered (headers and map lines included)
    while est_tokens(pack.render()) > budget and pack.files:
        last = pack.files[-1]
        if last.snippets:
            last.snippets.pop()
        else:
            pack.files.pop()
        pack.truncated = True
    pack.tokens_est = est_tokens(pack.render())
    if use_cache and state:
        try:
            cpath.parent.mkdir(parents=True, exist_ok=True)
            cpath.write_text(json.dumps(pack.to_dict()), encoding="utf-8")
        except OSError:
            pass
    return pack
