"""Dependency-aware expansion: given changed files, find what depends on them.

File level  : TS/JS import specifiers, Python imports, C# type references, SQL object references.
Project level: .NET ProjectReference graph (transitive), nearest package.json / pyproject.toml.
Regex-based on purpose (fast, no build needed); results are candidates for validation, not proof.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .context import Searcher

CS_TYPE = re.compile(r"\b(?:class|interface|record|struct|enum)\s+([A-Z]\w*)")
SQL_OBJ = re.compile(r"(?i)\bcreate\s+(?:or\s+alter\s+)?(?:procedure|proc|table|view|function|trigger|type)\s+(?:\[?\w+\]?\.)?\[?(\w+)\]?")
PROJECT_REF = re.compile(r'<ProjectReference\s+Include="([^"]+)"', re.I)
TEST_PROJECT_HINT = re.compile(r"Microsoft\.NET\.Test\.Sdk|xunit|nunit|MSTest", re.I)


@dataclass
class DepReport:
    changed: list[str]
    dependents: dict[str, list[str]] = field(default_factory=dict)   # changed file -> files that use it
    projects: dict[str, str] = field(default_factory=dict)           # changed file -> owning project file
    downstream_projects: list[str] = field(default_factory=list)     # projects depending on owning ones
    notes: list[str] = field(default_factory=list)

    def all_files(self) -> list[str]:
        out = list(self.changed)
        for deps in self.dependents.values():
            out += deps
        return list(dict.fromkeys(out))


def owning_project(root: Path, rel: str) -> str | None:
    """Nearest .csproj / package.json / pyproject.toml (or setup.cfg) walking up from the file."""
    p = (root / rel).parent
    while True:
        try:
            p.relative_to(root)
        except ValueError:
            return None
        csprojs = sorted(p.glob("*.csproj"))
        if csprojs:
            return csprojs[0].relative_to(root).as_posix()
        for marker in ("package.json", "pyproject.toml", "setup.cfg", "setup.py"):
            if (p / marker).is_file():
                return (p / marker).relative_to(root).as_posix()
        if p == root:
            return None
        p = p.parent


def csproj_graph(root: Path, searcher: Searcher) -> dict[str, set[str]]:
    """csproj -> set of csproj files it references (normalised, relative to root)."""
    graph: dict[str, set[str]] = {}
    for proj in searcher.files(["csproj"]):
        try:
            text = (root / proj).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        refs = set()
        for inc in PROJECT_REF.findall(text):
            target = ((root / proj).parent / inc.replace("\\", "/")).resolve()
            try:
                refs.add(target.relative_to(root).as_posix())
            except ValueError:
                continue
        graph[proj] = refs
    return graph


def reverse_closure(graph: dict[str, set[str]], starts: set[str]) -> set[str]:
    rev: dict[str, set[str]] = {}
    for proj, refs in graph.items():
        for r in refs:
            rev.setdefault(r, set()).add(proj)
    out, todo = set(), list(starts)
    while todo:
        cur = todo.pop()
        for dep in rev.get(cur, ()):
            if dep not in out:
                out.add(dep)
                todo.append(dep)
    return out - starts


def is_test_project(root: Path, proj: str) -> bool:
    if re.search(r"test", Path(proj).stem, re.I):
        return True
    try:
        return bool(TEST_PROJECT_HINT.search((root / proj).read_text(encoding="utf-8", errors="replace")))
    except OSError:
        return False


def _file_dependents(root: Path, rel: str, searcher: Searcher, limit: int) -> list[str]:
    path = Path(rel)
    ext = path.suffix.lower().lstrip(".")
    hits = []
    try:
        if ext in ("ts", "tsx", "js", "jsx", "mjs", "cjs", "vue", "svelte"):
            name = path.stem if path.stem != "index" else path.parent.name
            spec = rf"""(from\s+|require\(\s*|import\(\s*|import\s+)['"][^'"]*\b{re.escape(name)}(\.\w+)?['"]"""
            hits = searcher.grep(spec, max_count=3, exts=["ts", "tsx", "js", "jsx", "mjs", "cjs", "vue", "svelte"])
        elif ext == "py":
            mod = path.stem if path.stem != "__init__" else path.parent.name
            spec = rf"^\s*(from\s+[\w.]*\b{re.escape(mod)}\b\s+import|import\s+[\w.]*\b{re.escape(mod)}\b|from\s+[\w.]+\s+import\s+.*\b{re.escape(mod)}\b)"
            hits = searcher.grep(spec, max_count=3, exts=["py"])
        elif ext == "cs":
            text = (root / rel).read_text(encoding="utf-8", errors="replace")
            types = list(dict.fromkeys(CS_TYPE.findall(text)))[:8]
            if types:
                spec = r"\b(" + "|".join(map(re.escape, types)) + r")\b"
                hits = searcher.grep(spec, max_count=2, exts=["cs", "cshtml", "razor"])
        elif ext == "sql":
            text = (root / rel).read_text(encoding="utf-8", errors="replace")
            objs = list(dict.fromkeys(SQL_OBJ.findall(text)))[:8]
            if objs:
                spec = r"\b(" + "|".join(map(re.escape, objs)) + r")\b"
                hits = searcher.grep(spec, max_count=2, exts=["cs", "sql", "ts", "py"])
    except (OSError, RuntimeError):
        return []
    files = [h.path for h in hits if h.path != rel]
    return list(dict.fromkeys(files))[:limit]


def analyze(root: str | Path, changed: list[str], exclude_dirs: list[str], limit: int = 25) -> DepReport:
    root = Path(root).resolve()
    searcher = Searcher(root, exclude_dirs)
    rep = DepReport(changed=[c.replace("\\", "/") for c in changed])
    for rel in rep.changed:
        if not (root / rel).is_file():
            rep.notes.append(f"{rel}: not found (deleted?) - dependents not computed")
            continue
        rep.dependents[rel] = _file_dependents(root, rel, searcher, limit)
        proj = owning_project(root, rel)
        if proj:
            rep.projects[rel] = proj
    owning_cs = {p for p in rep.projects.values() if p.endswith(".csproj")}
    if owning_cs:
        rep.downstream_projects = sorted(reverse_closure(csproj_graph(root, searcher), owning_cs))
    return rep
