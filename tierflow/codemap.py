"""Discover and query an existing codebase map. Read-only: this module never writes a map.

Supported formats (both seen in real projects):
  * JSON routing map, e.g. codebase-map.json:
      {"modules": {"answering": {"index": "...md", "entryPoints": ["Symbol@path"], "keywords": [...],
                                 "tests": [...], "data": [...]}}, "loadPolicy": {"always": [...]}}
  * Markdown maps, e.g. docs/ai-context/CODEBASE_MAP.md, REPO_INDEX.md, ARCHITECTURE.md:
      matched by heading sections whose text mentions the query terms.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

JSON_CANDIDATES = [
    "codebase-map.json", ".claude/codebase-map.json", "docs/codebase-map.json", "docs/ai-context/codebase-map.json",
]
MD_CANDIDATES = [
    "CODEBASE_MAP.md", "codebase-map.md", "CODEMAP.md", "REPO_MAP.md", "REPO_INDEX.md", "INDEX.md",
    "docs/CODEBASE_MAP.md", "docs/codebase-map.md", "docs/ai-context/CODEBASE_MAP.md",
    "docs/ai-context/REPO_INDEX.md", ".claude/codebase-map.md", "ARCHITECTURE.md", "docs/ARCHITECTURE.md",
    "docs/architecture.md", "docs/ai-context/ARCHITECTURE.md", "docs/architecture-index.md",
]

WORD = re.compile(r"[A-Za-z][A-Za-z0-9_]{2,}")
STOP = set("""the and for with that this from into what where which when how does are was were have has
should would could will can not but all any add fix make use using update change file files code please
find show list explain implement create write test tests make sure about also then there their them
applied apply used uses work works working defined called calls happen happens handled done does doing
need needs want wants get gets set sets like just only some more most other into onto them they code""".split())


def stem(term: str) -> str:
    """Tiny suffix stripper for search terms ('freshness' -> 'fresh', 'filters' -> 'filter')."""
    for suf in ("ness", "ments", "ment", "ings", "ing", "ies", "ed", "es", "s", "ly"):
        if term.endswith(suf) and len(term) - len(suf) >= 4:
            return term[: -len(suf)]
    return term


def terms_of(text: str) -> list[str]:
    """Lowercase search terms; splits camelCase and snake_case so 'getUserById' finds 'user'."""
    out: list[str] = []
    for w in WORD.findall(text):
        parts = re.findall(r"[A-Z]?[a-z0-9]+|[A-Z]+(?![a-z])", w) or [w]
        for p in [w, *parts]:
            p = p.lower()
            if len(p) >= 3 and p not in STOP and p not in out:
                out.append(p)
    return out


@dataclass
class MapHit:
    source: str           # map file (relative)
    kind: str             # module | section
    name: str
    score: float
    entry_points: list[str] = field(default_factory=list)   # "Symbol@path"
    files: list[str] = field(default_factory=list)          # index/tests/data docs to read if needed
    tests: list[str] = field(default_factory=list)
    line: int | None = None                                  # for markdown sections

    def to_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class CodeMap:
    root: Path
    json_maps: list[Path] = field(default_factory=list)
    md_maps: list[Path] = field(default_factory=list)

    @property
    def found(self) -> bool:
        return bool(self.json_maps or self.md_maps)

    def describe(self) -> str:
        names = [p.relative_to(self.root).as_posix() for p in (self.json_maps + self.md_maps)]
        return ", ".join(names[:4]) + (" ..." if len(names) > 4 else "")

    def always_load(self) -> list[str]:
        out: list[str] = []
        for p in self.json_maps:
            data = _read_json(p)
            out += list((data.get("loadPolicy") or {}).get("always") or [])
        return out

    # ------------------------------------------------------------------------------------------
    def query(self, text: str, limit: int = 5) -> list[MapHit]:
        terms = terms_of(text)
        if not terms:
            return []
        hits: list[MapHit] = []
        for p in self.json_maps:
            hits += _query_json(p, self.root, terms, text.lower())
        for p in self.md_maps:
            hits += _query_md(p, self.root, terms)
        hits.sort(key=lambda h: -h.score)
        return hits[:limit]


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _query_json(path: Path, root: Path, terms: list[str], lowered: str) -> list[MapHit]:
    data = _read_json(path)
    modules = data.get("modules")
    if not isinstance(modules, dict):
        return []
    rel = path.relative_to(root).as_posix()
    hits = []
    for name, mod in modules.items():
        if not isinstance(mod, dict):
            continue
        score = 0.0
        for kw in mod.get("keywords") or []:
            kw_l = str(kw).lower()
            if kw_l in lowered:
                score += 3 if " " in kw_l else 2       # multi-word keyword phrase = strong signal
            elif any(t == kw_l or t in kw_l.split() for t in terms):
                score += 1
        if name.lower() in lowered or any(t in name.lower().split("-") for t in terms):
            score += 2
        eps = [str(e) for e in mod.get("entryPoints") or []]
        for ep in eps:
            sym = ep.split("@")[0].lower()
            if any(t in sym for t in terms if len(t) >= 4):
                score += 1.5
        if score <= 0:
            continue
        files = [f for f in [mod.get("index"), *(mod.get("data") or [])] if f]
        hits.append(MapHit(rel, "module", name, score, eps, files, list(mod.get("tests") or [])))
    return hits


HEADING = re.compile(r"^(#{1,4})\s+(.*)$")


def _query_md(path: Path, root: Path, terms: list[str]) -> list[MapHit]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    rel = path.relative_to(root).as_posix()
    sections: list[tuple[int, str, list[str]]] = []
    cur_line, cur_title, buf = 1, path.name, []
    for i, line in enumerate(lines, 1):
        m = HEADING.match(line)
        if m:
            sections.append((cur_line, cur_title, buf))
            cur_line, cur_title, buf = i, m.group(2).strip(), []
        else:
            buf.append(line)
    sections.append((cur_line, cur_title, buf))
    hits = []
    for line_no, title, body in sections:
        blob = (title + "\n" + "\n".join(body)).lower()
        score = sum(2 if t in title.lower() else 0.5 for t in terms if t in blob)
        if score >= 1.5:
            paths = re.findall(r"`([\w./-]+\.[A-Za-z]{1,5})`", "\n".join(body))
            hits.append(MapHit(rel, "section", title, score, [], sorted(set(paths))[:8], [], line_no))
    return hits


def discover(root: str | Path, extra: list[str] | None = None) -> CodeMap:
    root = Path(root).resolve()
    cm = CodeMap(root)
    seen: set[Path] = set()
    for rel in [*(extra or []), *JSON_CANDIDATES, *MD_CANDIDATES]:
        p = (root / rel).resolve()
        if p in seen or not p.is_file():
            continue
        try:
            p.relative_to(root)
        except ValueError:          # never follow a configured path outside the repo
            continue
        seen.add(p)
        (cm.json_maps if p.suffix == ".json" else cm.md_maps).append(p)
    return cm
