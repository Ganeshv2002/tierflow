"""Validation engine: build / typecheck / test only what a change can affect, with a compact digest.

Plans are computed from changed files + dependency analysis, then executed sequentially with
timeouts. Nothing here calls a model. `--dry-run` shows the plan without running it.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import proc
from .config import Config
from .deps import analyze, is_test_project

FAIL_LINE = re.compile(
    r"(error\s+[A-Z]{2,}\d+|\berror\b|\bFAIL(ED)?\b|\bFailed\b|AssertionError|Assert\.|Traceback|Exception:|"
    r"✕|●|^E\s{2,}|panic:|TS\d{4}:|expected .* (to|but)|Expected:|Actual:|\bfailures?\b)", re.I)
SQL_COMMENT = re.compile(r"--[^\n]*|/\*.*?\*/", re.S)


def sql_risks(text: str) -> list[str]:
    """Statement-level scan for destructive SQL. Heuristic: flags for human review, never blocks."""
    risks = []
    for stmt in re.split(r";|\bGO\b", SQL_COMMENT.sub(" ", text), flags=re.I):
        s = " ".join(stmt.split()).lower()
        if re.search(r"\bdrop\s+(table|database|schema|column|procedure|proc|view|index)\b", s):
            risks.append("DROP statement")
        if re.search(r"\btruncate\s+table\b", s):
            risks.append("TRUNCATE TABLE")
        if re.search(r"\bdelete\s+(from\s+)?[\w.\[\]#@]+", s) and " where " not in f" {s} ":
            risks.append("DELETE without WHERE")
        if re.search(r"\bupdate\s+[\w.\[\]#@]+\s+set\b", s) and " where " not in f" {s} ":
            risks.append("UPDATE without WHERE")
    return list(dict.fromkeys(risks))


def uses_testing_platform(root: Path) -> bool:
    try:
        data = json.loads((root / "global.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return str((data.get("test") or {}).get("runner", "")).lower() == "microsoft.testing.platform"


def project_python(root: Path) -> str:
    """Prefer the project's virtualenv interpreter so tests see its dependencies."""
    for cand in (".venv/Scripts/python.exe", "venv/Scripts/python.exe", ".venv/bin/python", "venv/bin/python"):
        if (root / cand).is_file():
            return str(root / cand)
    return sys.executable


@dataclass
class Step:
    name: str
    argv: list[str]
    cwd: str
    kind: str                     # build | typecheck | test | lint | custom
    project: str | None = None
    reason: str = ""


@dataclass
class StepResult:
    name: str
    kind: str
    ok: bool
    returncode: int | None
    duration_s: float
    timed_out: bool = False
    skipped: str | None = None
    excerpt: list[str] = field(default_factory=list)


@dataclass
class ValidationReport:
    root: str
    changed: list[str]
    steps: list[Step]
    results: list[StepResult] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    dependents: dict[str, list[str]] = field(default_factory=dict)
    dry_run: bool = False

    @property
    def ok(self) -> bool:
        return all(r.ok or r.skipped for r in self.results)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["ok"] = self.ok
        return d

    def digest(self) -> str:
        """Short text for the model: what ran, what failed, the relevant lines only."""
        out = [f"validation: {'PASS' if self.ok else 'FAIL'} ({len(self.results)} step(s), changed {len(self.changed)} file(s))"]
        if self.dry_run:
            out = [f"validation plan ({len(self.steps)} step(s)):"]
            for s in self.steps:
                out.append(f"- [{s.kind}] {' '.join(s.argv)}  (cwd {s.cwd}) - {s.reason}")
        for r in self.results:
            status = "skipped: " + r.skipped if r.skipped else ("ok" if r.ok else ("TIMEOUT" if r.timed_out else f"exit {r.returncode}"))
            out.append(f"- [{r.kind}] {r.name}: {status} ({r.duration_s:.1f}s)")
            if not r.ok and not r.skipped:
                out += [f"    {line}" for line in r.excerpt]
        if self.dependents:
            downstream = sorted({d for ds in self.dependents.values() for d in ds})
            if downstream:
                out.append(f"downstream files to keep in mind: {', '.join(downstream[:10])}" + (" ..." if len(downstream) > 10 else ""))
        out += [f"warning: {w}" for w in self.warnings]
        return "\n".join(out)


def changed_files(root: Path) -> list[str]:
    files: list[str] = []
    for argv in (["git", "diff", "--name-only", "HEAD"], ["git", "ls-files", "--others", "--exclude-standard"]):
        r = proc.run(argv, cwd=root, timeout=60)
        if r.ok:
            files += [l.strip() for l in r.stdout.splitlines() if l.strip()]
    return list(dict.fromkeys(files))


def _excerpt(text: str, max_lines: int) -> list[str]:
    lines = [l.rstrip() for l in text.splitlines() if l.strip()]
    picked = [l for l in lines if FAIL_LINE.search(l)]
    if not picked:
        picked = lines[-max_lines:]
    dedup = list(dict.fromkeys(l[:240] for l in picked))
    return dedup[:max_lines]


def _node_info(pkg_json: Path) -> dict:
    try:
        return json.loads(pkg_json.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def plan(root: str | Path, cfg: Config, changed: list[str] | None = None) -> ValidationReport:
    root = Path(root).resolve()
    vcfg = cfg.section("validation")
    changed = [c.replace("\\", "/") for c in (changed if changed is not None else changed_files(root))]
    rep = ValidationReport(str(root), changed, [])
    if not changed:
        rep.warnings.append("no changed files detected (git diff HEAD + untracked)")
        return rep
    deps = analyze(root, changed, cfg.section("context").get("exclude_dirs", []))
    rep.dependents = deps.dependents if vcfg.get("verify_downstream_dependencies", True) else {}
    rep.warnings += deps.notes
    seen: set[tuple] = set()

    def add(step: Step):
        key = (tuple(step.argv), step.cwd)
        if key not in seen:
            seen.add(key)
            rep.steps.append(step)

    by_project: dict[str, list[str]] = {}
    for f in changed:
        proj = deps.projects.get(f)
        if proj:
            by_project.setdefault(proj, []).append(f)
        if f.lower().endswith(".sql") and (root / f).is_file():
            for label in sql_risks((root / f).read_text(encoding="utf-8", errors="replace")):
                rep.warnings.append(f"{f}: {label} - high risk, needs explicit human approval")

    # ---- .NET -----------------------------------------------------------------------------
    cs_projects = sorted(p for p in by_project if p.endswith(".csproj"))
    if cs_projects:
        downstream = deps.downstream_projects if vcfg.get("verify_downstream_dependencies", True) else []
        for p in cs_projects:
            add(Step(f"dotnet build {p}", ["dotnet", "build", p, "--nologo", "-v", "q", "-clp:ErrorsOnly"], str(root), "build", p,
                     "owning project of changed files"))
        if vcfg.get("test_affected_modules", True):
            tests = [p for p in cs_projects + downstream if is_test_project(root, p)]
            mtp = uses_testing_platform(root)
            for p in tests:
                # .NET 10 repos that opt into Microsoft.Testing.Platform (global.json "test.runner") need
                # `--project`; the classic form builds the project and then runs zero tests (exit 5).
                argv = ["dotnet", "test", "--project", p] if mtp else ["dotnet", "test", p, "--nologo", "-v", "q"]
                add(Step(f"dotnet test {p}", argv, str(root), "test", p,
                         "test project covering changed code" if p not in cs_projects else "changed test project"))
            if not tests:
                rep.warnings.append("no .NET test project references the changed projects")
        for p in downstream:
            if not is_test_project(root, p):
                add(Step(f"dotnet build {p}", ["dotnet", "build", p, "--nologo", "-v", "q", "-clp:ErrorsOnly"], str(root), "build", p,
                         "downstream project (references a changed project)"))

    # ---- Node / TypeScript ----------------------------------------------------------------
    for p in sorted(x for x in by_project if x.endswith("package.json")):
        pdir = (root / p).parent
        info = _node_info(root / p)
        deps_all = {**info.get("dependencies", {}), **info.get("devDependencies", {})}
        rel = lambda f: Path(os.path.relpath(root / f, pdir)).as_posix()
        files_abs = [rel(f) for f in by_project[p]]
        related = list(dict.fromkeys(files_abs + [rel(d) for f in by_project[p] for d in deps.dependents.get(f, [])]))
        if vcfg.get("run_typecheck", True) and (pdir / "tsconfig.json").is_file() and "typescript" in deps_all:
            add(Step(f"tsc --noEmit ({p})", ["npx", "--no-install", "tsc", "--noEmit", "-p", str(pdir)], str(pdir), "typecheck", p, "TypeScript project"))
        if vcfg.get("run_lint") and "eslint" in deps_all:
            add(Step(f"eslint ({p})", ["npx", "--no-install", "eslint", *files_abs], str(pdir), "lint", p, "lint changed files"))
        if vcfg.get("test_affected_modules", True):
            if "jest" in deps_all:
                add(Step(f"jest related ({p})", ["npx", "--no-install", "jest", "--findRelatedTests", *related, "--passWithNoTests"],
                         str(pdir), "test", p, "tests importing changed files"))
            elif "vitest" in deps_all:
                add(Step(f"vitest related ({p})", ["npx", "--no-install", "vitest", "related", "--run", *related], str(pdir), "test", p,
                         "tests importing changed files"))
            elif "test" in info.get("scripts", {}):
                add(Step(f"npm test ({p})", ["npm", "test", "--silent"], str(pdir), "test", p, "project test script (no related-test runner found)"))

    # ---- Python ---------------------------------------------------------------------------
    py_changed = [f for f in changed if f.endswith(".py")]
    if py_changed and vcfg.get("test_affected_modules", True):
        tests = []
        for f in py_changed:
            name = Path(f).name
            if name.startswith("test_") or name.endswith("_test.py"):
                tests.append(f)
            for d in deps.dependents.get(f, []):
                dn = Path(d).name
                if dn.startswith("test_") or dn.endswith("_test.py") or "/tests/" in f"/{d}":
                    tests.append(d)
            mod = Path(f).stem
            for cand in (root / "tests").glob(f"test_{mod}*.py") if (root / "tests").is_dir() else []:
                tests.append(cand.relative_to(root).as_posix())
        tests = [t for t in dict.fromkeys(tests) if (root / t).is_file()]
        py = project_python(root)
        has_pytest = proc.run([py, "-c", "import pytest"], timeout=60).ok
        if tests:
            argv = [py, "-m", "pytest", "-q", *tests] if has_pytest else \
                   [py, "-m", "unittest", *[t[:-3].replace("/", ".") for t in tests]]
            add(Step("python tests (related)", argv, str(root), "test", None, "tests importing or named after changed modules"))
        else:
            argv = [py, "-m", "pytest", "-q"] if has_pytest else [py, "-m", "unittest", "discover", "-q"]
            add(Step("python tests (all)", argv, str(root), "test", None, "no related tests found; running the suite"))

    # ---- custom commands (project config) -------------------------------------------------
    for c in vcfg.get("commands", []):
        globs = c.get("when", ["*"])
        if any(fnmatch.fnmatch(f, g) for f in changed for g in globs) and isinstance(c.get("argv"), list):
            add(Step(c.get("name", " ".join(c["argv"])), [str(a) for a in c["argv"]], str(root / c.get("cwd", ".")), "custom", None,
                     "project validation command"))

    if not rep.steps:
        rep.warnings.append("no validation steps apply to these files (docs/config only?)")
    return rep


def execute(rep: ValidationReport, cfg: Config) -> ValidationReport:
    vcfg = cfg.section("validation")
    timeout = vcfg.get("timeout_seconds", 900)
    max_lines = vcfg.get("max_failure_lines", 40)
    failed_builds: set[str] = set()
    for s in rep.steps:
        if s.kind == "test" and s.project and s.project in failed_builds:
            rep.results.append(StepResult(s.name, s.kind, False, None, 0.0, skipped="its project failed to build"))
            continue
        r = proc.run(s.argv, cwd=s.cwd, timeout=timeout)
        if r.error:
            rep.results.append(StepResult(s.name, s.kind, False, None, r.duration_s, excerpt=[f"could not start: {r.error}"]))
            continue
        res = StepResult(s.name, s.kind, r.ok, r.returncode, r.duration_s, r.timed_out)
        if not r.ok:
            res.excerpt = _excerpt(r.stdout + "\n" + r.stderr, max_lines)
            if s.argv[:2] == ["dotnet", "test"] and r.returncode == 5:
                res.excerpt.insert(0, "zero tests ran (exit 5): the test command found no tests - check the runner/filter, "
                                      "this is not a test failure in the code")
            if s.kind == "build" and s.project:
                failed_builds.add(s.project)
        rep.results.append(res)
    return rep
