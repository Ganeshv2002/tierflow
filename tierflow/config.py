"""Layered configuration.

Order: plugin defaults -> user config (~/.claude/tierflow/config.toml) -> project config
(<repo>/.claude/tierflow.toml). The project file lives in the repository, so it is treated as
untrusted: it may only *tighten* policy (lower budgets, stricter enforcement, extra context paths
and validation commands). Anything else it sets is ignored and reported in `Config.warnings`.
"""
from __future__ import annotations

import copy
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = PACKAGE_ROOT / "config" / "default.toml"

MODES = ("economy", "balanced", "quality")
TIERS = ("light", "standard", "reasoning")
ENFORCEMENT_ORDER = ["off", "warn", "ask", "deny"]  # later = stricter


def home_dir() -> Path:
    """State + user config directory. Shared by hooks and the CLI (the Bash tool does not receive
    CLAUDE_PLUGIN_DATA, so both sides use this fixed location)."""
    env = os.environ.get("TIERFLOW_HOME")
    return Path(env) if env else Path.home() / ".claude" / "tierflow"


def user_config_path() -> Path:
    return home_dir() / "config.toml"


def project_config_path(project_dir: Path) -> Path:
    return project_dir / ".claude" / "tierflow.toml"


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _load_toml(path: Path) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def _apply_project(data: dict, project: dict, warnings: list[str]) -> dict:
    """Merge project config, allowing only tightening changes."""
    out = copy.deepcopy(data)
    for section, values in project.items():
        if not isinstance(values, dict):
            warnings.append(f"project config: ignored top-level key '{section}'")
            continue
        if section == "context":
            for k, v in values.items():
                if k in ("extra_map_paths", "exclude_dirs") and isinstance(v, list):
                    out["context"][k] = list(dict.fromkeys(out["context"].get(k, []) + v))
                elif k in ("max_context_tokens", "max_files", "snippet_lines", "max_hits_per_file") and isinstance(v, int):
                    out["context"][k] = min(out["context"][k], v)
                else:
                    warnings.append(f"project config: ignored context.{k}")
        elif section == "validation":
            for k, v in values.items():
                if k == "commands" and isinstance(v, list):
                    out["validation"]["commands"] = v
                elif k in ("run_lint", "run_typecheck", "test_affected_modules", "verify_downstream_dependencies") and isinstance(v, bool):
                    out["validation"][k] = v
                elif k == "timeout_seconds" and isinstance(v, (int, float)):
                    out["validation"][k] = min(out["validation"][k], v)
                else:
                    warnings.append(f"project config: ignored validation.{k}")
        elif section == "budgets":
            for k, v in values.items():
                if k.startswith("max_cost") and isinstance(v, (int, float)) and k in out["budgets"]:
                    out["budgets"][k] = min(out["budgets"][k], float(v))
                elif k == "enforcement" and v in ENFORCEMENT_ORDER:
                    cur = out["budgets"]["enforcement"]
                    if ENFORCEMENT_ORDER.index(v) > ENFORCEMENT_ORDER.index(cur):
                        out["budgets"]["enforcement"] = v
                    else:
                        warnings.append("project config: cannot loosen budgets.enforcement")
                else:
                    warnings.append(f"project config: ignored budgets.{k}")
        else:
            warnings.append(f"project config: section [{section}] can only be set in user config")
    return out


@dataclass
class Config:
    data: dict[str, Any]
    warnings: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)

    # convenience accessors -------------------------------------------------
    @property
    def mode(self) -> str:
        return self.data["orchestrator"]["mode"]

    def section(self, name: str) -> dict[str, Any]:
        return self.data.get(name, {})

    def model_alias(self, tier: str) -> str:
        return self.data["models"].get(tier, tier)

    def agent_info(self, agent_type: str | None) -> dict[str, Any] | None:
        """Look up an agent by full ('tierflow:scout') or short ('scout') name."""
        if not agent_type:
            return None
        agents = self.data.get("agents", {})
        if agent_type in agents:
            return agents[agent_type]
        short = agent_type.rsplit(":", 1)[-1]
        return agents.get(short)

    def opus_gate(self) -> str:
        gate = self.data["routing"].get("opus_gate", "auto")
        if gate != "auto":
            return gate
        return {"economy": "ask", "balanced": "warn", "quality": "allow"}[self.mode]


def tuned_config_path() -> Path:
    return home_dir() / "tuned.toml"


# The only keys `orch tune --apply` may set. Anything else in tuned.toml is ignored, so the file can never
# change modes, pricing, gates or budgets' limits.
TUNABLE = {
    ("session", "warn_context_tokens"): int,
    ("session", "rewarn_every_tokens"): int,
    ("routing", "hint_categories_off"): list,
    **{("budgets.reservation_usd", t): float for t in ("light", "standard", "reasoning")},
}


def _apply_tuned(data: dict, tuned: dict, warnings: list[str]) -> dict:
    out = copy.deepcopy(data)
    flat = {}
    for section, values in tuned.items():
        if section == "budgets" and isinstance(values, dict):
            for k, v in (values.get("reservation_usd") or {}).items():
                flat[("budgets.reservation_usd", k)] = v
            continue
        for k, v in (values.items() if isinstance(values, dict) else []):
            flat[(section, k)] = v
    for (section, key), v in flat.items():
        typ = TUNABLE.get((section, key))
        ok = typ is not None and (isinstance(v, (int, float)) and not isinstance(v, bool) if typ in (int, float)
                                  else isinstance(v, list) and all(isinstance(x, str) for x in v))
        if not ok:
            warnings.append(f"tuned config: ignored {section}.{key}")
            continue
        node = out
        for part in section.split("."):
            node = node.setdefault(part, {})
        node[key] = typ(v) if typ in (int, float) else list(v)
    return out


def _validate(data: dict, warnings: list[str]) -> None:
    if data["orchestrator"].get("mode") not in MODES:
        warnings.append(f"invalid mode {data['orchestrator'].get('mode')!r}; using 'balanced'")
        data["orchestrator"]["mode"] = "balanced"
    if data["budgets"].get("enforcement") not in ENFORCEMENT_ORDER:
        warnings.append("invalid budgets.enforcement; using 'ask'")
        data["budgets"]["enforcement"] = "ask"
    for tier in TIERS:
        data["budgets"].setdefault("reservation_usd", {}).setdefault(tier, 0.0)
    data["validation"].setdefault("commands", [])


def load_config(project_dir: str | Path | None = None, *, mode_override: str | None = None) -> Config:
    warnings: list[str] = []
    sources = [str(DEFAULT_CONFIG)]
    data = _load_toml(DEFAULT_CONFIG)

    # learned values (orch tune --apply) sit between the defaults and your own config: yours always wins
    tpath = tuned_config_path()
    if tpath.is_file():
        try:
            data = _apply_tuned(data, _load_toml(tpath), warnings)
            sources.append(str(tpath))
        except (tomllib.TOMLDecodeError, OSError) as e:
            warnings.append(f"tuned config unreadable ({tpath}): {e}")

    upath = user_config_path()
    if upath.is_file():
        try:
            data = _deep_merge(data, _load_toml(upath))
            sources.append(str(upath))
        except (tomllib.TOMLDecodeError, OSError) as e:
            warnings.append(f"user config unreadable ({upath}): {e}")

    if project_dir:
        ppath = project_config_path(Path(project_dir))
        if ppath.is_file():
            try:
                data = _apply_project(data, _load_toml(ppath), warnings)
                sources.append(str(ppath))
            except (tomllib.TOMLDecodeError, OSError) as e:
                warnings.append(f"project config unreadable ({ppath}): {e}")

    env_mode = os.environ.get("TIERFLOW_MODE")
    for m in (env_mode, mode_override):
        if m:
            if m in MODES:
                data["orchestrator"]["mode"] = m
            else:
                warnings.append(f"ignored unknown mode {m!r}")
    _validate(data, warnings)
    return Config(data=data, warnings=warnings, sources=sources)
