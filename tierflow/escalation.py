"""Evidence-based retry / escalation policy.

Inputs are facts: the agent's STATUS line, validation results, attempts per tier. A model saying it
is "unsure" is not enough to escalate; a failed validation or a declared blocker is.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .config import Config

TIER_ORDER = ["light", "standard", "reasoning"]
NEXT_AGENT = {"light": "tierflow:implementer (sonnet)", "standard": "tierflow:architect (opus) to re-plan, then implementer",
              "reasoning": None}

STATUS_RX = re.compile(r"STATUS:\s*(?P<status>done|success|ok|partial|blocked|failed|needs[_ -]?info|scope[_ -]?exceeded)", re.I)
FIELD_RX = re.compile(r"\b(CONFIDENCE|REASON|FILES|VALIDATION|NEXT):\s*([^\n;]+)", re.I)

FAILURE_REPORT_TEMPLATE = """FAILURE REPORT
goal: <one line>
attempt: <n> on <tier/agent>
tried: <approach in 1-2 lines>
evidence: <failing test names / error lines, max 10 lines - copy from `orch validate` digest>
files: <paths touched or suspected, with symbols>
ruled out: <hypotheses already disproved>
question for next agent: <the specific decision or missing piece>"""


@dataclass
class AgentOutcome:
    status: str                 # done | partial | blocked | failed | needs_info | scope_exceeded | unknown
    confidence: str | None
    reason: str | None
    files: list[str]
    validation: str | None      # pass | fail | not-run | None

    @property
    def failed(self) -> bool:
        return self.status in ("blocked", "failed", "partial") or (self.validation or "").lower().startswith("fail")


def parse_outcome(text: str) -> AgentOutcome:
    m = None
    for m in STATUS_RX.finditer(text or ""):
        pass                    # last STATUS line wins
    status = "unknown"
    if m:
        s = m.group("status").lower().replace(" ", "_").replace("-", "_")
        status = {"success": "done", "ok": "done", "needsinfo": "needs_info", "scopeexceeded": "scope_exceeded"}.get(s, s)
    fields = {k.upper(): v.strip() for k, v in FIELD_RX.findall(text or "")}
    files = [f.strip() for f in re.split(r"[,\s]+", fields.get("FILES", "")) if f.strip() and f.strip().lower() not in ("none", "-", "n/a")]
    return AgentOutcome(status, fields.get("CONFIDENCE"), fields.get("REASON"), files, fields.get("VALIDATION"))


@dataclass
class Advice:
    action: str        # none | retry | escalate | reassess | ask_user | stop
    message: str


def advise(task: dict, tier: str, outcome: AgentOutcome, cfg: Config) -> Advice:
    """Update task attempts in place and return the next step."""
    ecfg = cfg.section("escalation")
    if not ecfg.get("enabled", True):
        return Advice("none", "")
    if outcome.status == "done" and not outcome.failed:
        return Advice("none", "")
    if outcome.status == "unknown":
        return Advice("none", "agent returned no STATUS line; check its result and run `orch validate` before trusting it")
    if outcome.status == "needs_info":
        return Advice("ask_user", f"agent needs information, not a stronger model: {outcome.reason or 'see its report'}. "
                                  "Ask the user; do not escalate.")
    if outcome.status == "scope_exceeded":
        task["escalations"] = task.get("escalations", 0) + 1
        return Advice("reassess", "change is wider than planned (more files/services). Stop editing and reassess with "
                                  "tierflow:architect using a compact failure report:\n" + FAILURE_REPORT_TEMPLATE)

    attempts = task.setdefault("attempts", {})
    attempts[tier] = attempts.get(tier, 0) + 1
    task.setdefault("failures", []).append({"tier": tier, "status": outcome.status, "reason": (outcome.reason or "")[:200]})
    total = sum(attempts.values())
    max_retries = ecfg.get("max_retries", 2)
    if total >= ecfg.get("max_total_attempts", 5):
        return Advice("stop", f"{total} attempts used on this task. Stop and report to the user with:\n" + FAILURE_REPORT_TEMPLATE)
    if attempts[tier] < max_retries:
        return Advice("retry", f"attempt {attempts[tier]}/{max_retries} at {tier} failed. Retry once at the same tier with the "
                               "validation digest and a different approach; do not repeat the same edit.")
    nxt = NEXT_AGENT.get(tier)
    if not nxt:
        return Advice("stop", "reasoning tier already failed its retries. Stop and report to the user with:\n" + FAILURE_REPORT_TEMPLATE)
    task["escalations"] = task.get("escalations", 0) + 1
    return Advice("escalate", f"{tier} failed {attempts[tier]}x -> escalate to {nxt}. Hand over a compact failure report, "
                              "not the conversation:\n" + FAILURE_REPORT_TEMPLATE)


def tier_of_model(model: str | None, cfg: Config) -> str | None:
    """Map an alias or full model id to a tier ('opus' -> reasoning)."""
    if not model:
        return None
    m = model.lower()
    for tier, alias in cfg.section("models").items():
        if m == alias.lower():
            return tier
    if "haiku" in m:
        return "light"
    if "sonnet" in m:
        return "standard"
    if "opus" in m or "fable" in m or "mythos" in m:
        return "reasoning"
    return None
