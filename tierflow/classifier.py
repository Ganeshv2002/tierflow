"""Deterministic task classifier (zero model tokens).

It looks for *kinds of work* (verbs, risk words, scope words, explicit targets), not prompt length.
Output is advisory: the main session still decides, and `explain()` shows every signal used.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from .config import Config

# --- signal vocab ---------------------------------------------------------------------------
CHAT = re.compile(r"^\s*(hi|hello|hey|thanks|thank you|thx|ok|okay|yes|no|yep|nope|cool|great|nice|sure|go ahead|continue|lgtm)\b[\s.!?]*$", re.I)

DISCOVERY = re.compile(
    r"\b(where (is|are|do|does)|find|locate|search|list|show me|what (is|are|does|do)|which (file|function|class|method|component|module)s?"
    r"|how does|how do|explain|summari[sz]e|describe|walk me through|overview|understand|look up|grep|who calls|usages? of|references? to"
    r"|signature|interface of|read)\b", re.I)

# Lookups that are about code by their nature ("where is", "who calls"), versus general questions
# ("explain", "how is X compared to Y") that are only about code when they name code.
CODE_LOOKUP = re.compile(
    # bare "find"/"locate" are everyday words ("the issues you find"); they count only beside a code target
    r"\b(where (is|are|do|does)|grep|who calls|usages? of|references? to|which (file|function|class|method|component|module)s?"
    r"|show me the (code|file|function|class))\b", re.I)
CODE_NOUN = re.compile(
    r"\b(code|codebase|repo|repository|function|method|class|file|module|component|endpoint|api|controller|service|table"
    r"|stored procedure|query|schema|tests?|bug|error|exception|stack trace|config|variable|import|implemented|defined|called"
    r"|parser|handler|helper|util|utility|hook|middleware|model|route|router|script|package|library|dependency|regex|algorithm)s?\b", re.I)

MECHANICAL = re.compile(
    r"\b(rename|re-?format|format|prettier|sort imports|organi[sz]e imports|add (an? )?import|remove unused|delete unused|unused imports"
    r"|typo|spelling|docstrings?|add comments?|xml doc|jsdoc|bump (the )?version|move (the )?(file|class|function)"
    r"|replace .{1,40} with|change .{1,40} to|lint(ing)? (errors?|warnings?)|fix (the )?lint|indent(ation)?|camel ?case|snake ?case"
    r"|summari[sz]e (the )?(logs?|test (results|output)))\b", re.I)
# Writing new code (boilerplate, stubs, DTOs, tests) is NOT mechanical: it stays on Sonnet or better, because a
# cheap model's hallucinated calls cost more to debug than they save.

STANDARD = re.compile(
    r"\b(fix|bug|broken|error|exception|crash|fails?|failing|implement|add|create|build|write|refactor|extract|split|merge"
    r"|integrate|wire( up)?|hook up|endpoint|api|controller|service|component|feature|support|handle|validate|validation"
    r"|update|modify|change|improve|optimi[sz]e|tests?|unit tests?|integration tests?|review|migrate|port|convert|upgrade)\b", re.I)

COMPLEX = re.compile(
    r"\b(architect(ure|ural)?|system design|redesign|re-?architect|intermittent(ly)?|flaky|race condition|deadlock"
    r"|memory leak|concurren(t|cy)|thread[- ]safe(ty)?|scalab(le|ility)|performance regression|bottleneck|trade-?offs?"
    r"|which approach|compare (the )?approaches|competing|best way to (structure|design|model)|cross[- ]service|distributed"
    r"|eventual consistency|consistency model|state management|state machine|event sourcing|saga|idempoten(t|cy)"
    r"|ambiguous|unclear requirements?|not sure (how|what|why|whether)|should we|evaluate (options|alternatives)"
    r"|heisenbug|non-?deterministic"
    r"|why does .{0,60}(sometimes|occasionally|randomly)|high[- ]impact|breaking change across)\b", re.I)

# Weak signals: "root cause" or "still failing" alone describe ordinary debugging. They mean complex only together
# with multi-service scope or risk; repeated failure is counted from evidence (the ledger) instead.
WEAK_COMPLEX = re.compile(r"\b(root[- ]cause|keeps? failing|still (fails|failing|broken)|tried .{0,40}(didn'?t|doesn'?t|not) work)\b", re.I)

RISK_HIGH = re.compile(
    # "token" alone is ambiguous (LLM token usage); only security tokens count
    r"\b(auth(entication|orization|z|n)?|oauth|oidc|jwt|sso|login|password|secrets?|credentials?|api[- ]?keys?"
    r"|(access|refresh|bearer|auth|session|csrf|id|api|reset) tokens?"
    r"|encrypt(ion)?|decrypt|crypto(graphy)?|hash(ing)? passwords?|security|vulnerab(le|ility)|cve|injection|xss|csrf|cors"
    r"|payments?|billing|invoice|pii|gdpr|hipaa|permissions?|rbac|access control|drop (table|column|database)|truncate"
    r"|delete from|data loss|production (data|database|db)|prod (data|db)|rollback|backfill)\b", re.I)

RISK_MEDIUM = re.compile(
    r"\b(database|db|schema|sql|stored procs?|stored procedures?|t-?sql|ef core|entity framework|migrations?|ddl|index(es)?"
    r"|public api|api contract|breaking|backward[- ]compat(ible|ibility)?|shared (library|lib|package)|nuget|npm package"
    r"|deploy(ment)?|ci/?cd|pipeline|infrastructure|terraform|dockerfile|kubernetes|k8s|config(uration)?|feature flags?"
    r"|caching|cache invalidation|transactions?|locking|queue|message bus|kafka|rabbitmq|service bus)\b", re.I)

WIDE_SCOPE = re.compile(
    r"\b(all|every|each|entire|whole|across|everywhere|throughout|codebase[- ]wide|repo[- ]wide|global(ly)?|multiple (files|services|modules|projects)"
    r"|all (the )?(services|projects|modules|files|microservices)|microservices?|monorepo)\b", re.I)

# verbs that mean real engineering judgement even when a mechanical word is also present
STRONG_STANDARD = re.compile(
    r"\b(fix(es|ing)? (the )?(bug|issue|error|crash|failing)|bug|implement|refactor|feature|integrate|endpoint|migrate"
    r"|optimi[sz]e|crash|exception|regression|redesign|business logic|workflow)\b", re.I)

# mechanical jobs that need no explicit target because a tool does them deterministically
NO_TARGET_MECHANICAL = re.compile(
    r"\b(re-?format|format|prettier|sort imports|organi[sz]e imports|unused imports|indent(ation)?|fix (the )?lint"
    r"|lint(ing)? (errors?|warnings?)|summari[sz]e (the )?(logs?|test (results|output)))\b", re.I)

CODE_SPAN = re.compile(r"`[^`]*`")

MULTI_SERVICE = re.compile(r"\b(services|microservices?|cross[- ]service|multiple (services|projects|apis)|downstream services?)\b", re.I)

PATH_LIKE = re.compile(r"[\w./\\-]+\.(tsx?|jsx?|cs|csproj|sln|sql|py|json|ya?ml|md|razor|cshtml|css|scss|html|java|go|rb|php)\b", re.I)
IDENT_LIKE = re.compile(r"`[^`]+`|'[A-Za-z_][\w.]*'|\"[A-Za-z_][\w.]*\"|\b[a-z]+[A-Z]\w*\b|\b[A-Z][a-z]+[A-Z]\w*\b|\b\w+_\w+\b")

DETERMINISTIC_HINTS = [
    (re.compile(r"\b(re-?format|format(ting)?|prettier|indent(ation)?)\b", re.I),
     "run the formatter (prettier / dotnet format / ruff format) - no model needed"),
    (re.compile(r"\b(sort|organi[sz]e) imports|unused imports\b", re.I),
     "formatter/analyzer fix (eslint --fix, dotnet format analyzers, ruff --fix)"),
    (re.compile(r"\blint\b", re.I), "run the linter with --fix first; send only what remains to a model"),
    (re.compile(r"\brename\b", re.I),
     "IDE/LSP rename (tsserver / Roslyn) if available; else `rg -nw <old>` to enumerate, then targeted edits"),
    (re.compile(r"\b(where (is|are)|find|locate|usages? of|references? to|who calls|list)\b", re.I),
     "`orch context \"<terms>\"` or `rg -n` answers this without an agent"),
    (re.compile(r"\b(run|summari[sz]e) (the )?(tests?|test results|logs?)\b", re.I),
     "`orch validate` returns a compact failure digest"),
]

LEVELS = ["trivial", "simple", "moderate", "complex"]


@dataclass
class Classification:
    category: str            # chat | discovery | mechanical | standard | complex
    complexity: str          # trivial | simple | moderate | complex
    risk: str                # low | medium | high
    tier: str                # none | light | standard | reasoning
    agent: str | None        # suggested first agent
    plan_first: bool         # architect (or explicit plan) before edits
    then: str | None = None  # agent that implements an approved plan
    deterministic: str | None = None
    reasons: list[str] = field(default_factory=list)
    signals: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def _bump(level: str, steps: int = 1) -> str:
    return LEVELS[min(len(LEVELS) - 1, LEVELS.index(level) + steps)]


def _hits(rx: re.Pattern, text: str) -> list[str]:
    return sorted({m.group(0).lower() for m in rx.finditer(text)})


ATTACHED_BLOCK = re.compile(
    r"<(artifact-view-context|system-reminder|task-notification|ide_selection|ide_opened_file|cross-session-message"
    r"|agent-message|side-sessions-event|ci-monitor-event)\b[^>]*>.*?</\1(?:\s[^>]*)?>"
    r"|<artifact-content-authored-by-others\s*/>"
    r"|^\(The JSON line above is this viewer's live state.*?\)\s*$", re.S | re.M | re.I)


def strip_attachments(prompt: str) -> str:
    """Remove context blocks the app attaches to a user message (viewer state, reminders) and turns that
    are not from the user at all (task notifications, messages from other sessions, subagent hand-backs).
    They are not the user's words, and classifying them produced false 'high-risk: permissions' labels."""
    return ATTACHED_BLOCK.sub(" ", prompt or "")


def classify(prompt: str, cfg: Config, *, prior_failures: int = 0) -> Classification:
    text = strip_attachments(prompt).strip()
    reasons: list[str] = []
    if not text or CHAT.match(text):
        return Classification("chat", "trivial", "low", "none", None, False, reasons=["conversational"])

    # keyword matching runs on prose only: paths and code spans ("src/api/x.ts", `getUser`) are data
    prose = CODE_SPAN.sub(" ", PATH_LIKE.sub(" ", text))
    sig = {
        "discovery": _hits(DISCOVERY, prose),
        "mechanical": _hits(MECHANICAL, prose),
        "standard": _hits(STANDARD, prose),
        "strong_standard": _hits(STRONG_STANDARD, prose),
        "complex": _hits(COMPLEX, prose),
        "weak_complex": _hits(WEAK_COMPLEX, prose),
        "risk_high": _hits(RISK_HIGH, prose),
        "risk_medium": _hits(RISK_MEDIUM, prose),
        "wide_scope": _hits(WIDE_SCOPE, prose),
        "multi_service": _hits(MULTI_SERVICE, prose),
        "no_target_needed": _hits(NO_TARGET_MECHANICAL, prose),
        "code_lookup": _hits(CODE_LOOKUP, prose),
        "paths": sorted({m.group(0) for m in PATH_LIKE.finditer(text)}),
        "identifiers": len(IDENT_LIKE.findall(text)),
        "prior_failures": prior_failures,
    }
    sig["code_ref"] = bool(sig["paths"] or sig["identifiers"] or CODE_NOUN.search(prose))
    is_question = text.rstrip().endswith("?")

    # --- base category ---------------------------------------------------------------------
    if sig["complex"]:
        category, level = "complex", "complex"
        reasons.append(f"complex signals: {', '.join(sig['complex'][:3])}")
    elif sig["mechanical"] and not sig["strong_standard"]:
        explicit = bool(sig["paths"] or sig["identifiers"] or sig["no_target_needed"])
        if explicit:
            category, level = "mechanical", "simple"
            reasons.append(f"mechanical with explicit target: {', '.join(sig['mechanical'][:3])}")
        else:
            category, level = "standard", "moderate"
            reasons.append("mechanical verb but no explicit target -> needs judgement")
    elif sig["standard"] and not (is_question and sig["discovery"] and not sig["mechanical"]):
        category, level = "standard", "moderate"
        reasons.append(f"engineering verbs: {', '.join(sig['standard'][:3])}")
    elif sig["weak_complex"]:
        category, level = "standard", "moderate"
        reasons.append(f"debugging: {sig['weak_complex'][0]}")
    elif (sig["discovery"] or is_question) and (sig["code_lookup"] or sig["code_ref"]):
        category, level = "discovery", "simple"
        reasons.append("code lookup")
    else:
        # a question or remark with no engineering verb and no code target is conversation, not work:
        # say nothing rather than send it to an agent
        return Classification("chat", "trivial", "low", "none", None, False,
                              reasons=["question without a code target" if (sig["discovery"] or is_question)
                                       else "no engineering signal"], signals=sig)

    # --- risk ------------------------------------------------------------------------------
    if sig["risk_high"]:
        risk = "high"
        reasons.append(f"high-risk area: {', '.join(sig['risk_high'][:3])}")
    elif sig["risk_medium"]:
        risk = "medium"
        reasons.append(f"medium-risk area: {', '.join(sig['risk_medium'][:3])}")
    else:
        risk = "low"
    if category == "discovery":
        # reading code about auth is not risky; only edits are
        risk = "low" if risk != "high" else "medium"

    # --- scope -----------------------------------------------------------------------------
    if category != "discovery":
        if sig["wide_scope"] and level == "simple":
            level = "moderate"
            reasons.append("wide scope -> moderate")
        elif level == "moderate" and sig["multi_service"] and (sig["wide_scope"] or risk != "low"):
            level = "complex"
            reasons.append("multiple services affected -> complex")
        if len(sig["paths"]) >= 5 and level == "moderate":
            level = "complex"
            reasons.append(f"{len(sig['paths'])} files named -> complex")
        if level == "moderate" and sig["weak_complex"] and (sig["multi_service"] or risk == "high"):
            level = "complex"
            reasons.append(f"{sig['weak_complex'][0]} + {'multiple services' if sig['multi_service'] else 'high risk'} -> complex")

    # --- failures (evidence-based escalation) -----------------------------------------------
    if prior_failures:
        level = _bump(level)
        reasons.append(f"{prior_failures} failed attempt(s) recorded -> escalate one level")
        if category in ("mechanical", "discovery") and level in ("moderate", "complex"):
            category = "standard" if level == "moderate" else "complex"   # escalated work leaves the cheap tier

    # --- tier by mode ----------------------------------------------------------------------
    mode = cfg.mode
    plan_first = False
    then = None
    if category == "discovery":
        tier, agent = "light", "scout"
    elif level in ("trivial", "simple"):
        tier = "light"
        agent = "scout" if any(h.startswith("summari") for h in sig["no_target_needed"]) else "mechanic"
    elif level == "moderate":
        tier, agent = "standard", "implementer"
        if risk == "high" and mode != "economy":
            tier, agent, plan_first, then = "reasoning", "architect", True, "implementer"
            reasons.append("high risk -> plan with architect first")
        elif risk == "medium" and mode == "quality":
            tier, agent, plan_first, then = "reasoning", "architect", True, "implementer"
            reasons.append("quality mode + medium risk -> architect plan")
        elif risk == "high":
            plan_first = True
            reasons.append("economy: high risk -> implementer must present a plan for approval before editing")
    else:  # complex
        if mode == "economy" and risk != "high" and not prior_failures:
            tier, agent, plan_first = "standard", "implementer", True
            reasons.append("economy: complex but not high-risk -> sonnet plans; escalate to opus only on failure")
        else:
            tier, agent, plan_first, then = "reasoning", "architect", True, "implementer"

    deterministic = None
    if category in ("mechanical", "discovery") or sig["mechanical"]:
        for rx, hint in DETERMINISTIC_HINTS:
            if rx.search(text):
                deterministic = hint
                break

    return Classification(category, level, risk, tier, agent, plan_first, then, deterministic, reasons, sig)


TIER_RANK = {"light": 0, "standard": 1, "reasoning": 2}


def directive(c: Classification, main_tier: str | None) -> list[str]:
    """What to actually do, worded for the main session's model.

    Measured on real runs: most of a task's cost is writing what the main session reads into its cache,
    at the main model's price. So the useful move is to keep reading and routine edits out of an
    expensive main session - and to do nothing extra when the main session is already the right tier
    (delegating to the same tier only adds a fresh context to pay for)."""
    pricier = "Opus or Fable"
    if c.category == "discovery":
        # Measured: from a Sonnet session the scout's fresh context costs more than the lookup itself
        # ($0.10 vs $0.05); from Opus it saves. Accuracy comes from citing evidence either way.
        verify = "Only name functions/files you saw in the code, with file:line."
        if main_tier == "standard":
            return [f"Answer inline with targeted grep and narrow reads. {verify}"]
        if main_tier == "reasoning":
            return [f"Delegate this lookup to tierflow:scout (Haiku) unless one grep or what is already in context answers it; "
                    f"everything you read is cached at Opus prices. {verify}"]
        return [f"If you run on {pricier}: delegate this lookup to tierflow:scout (Haiku). On Sonnet: answer inline with "
                f"targeted grep. {verify}"]
    if c.category == "mechanical":
        if c.deterministic:
            return [f"Use the tool: {c.deterministic}. Otherwise tierflow:mechanic with an exact FILES: list, or inline if "
                    "the file is already in your context."]
        return ["Exact mechanical edit: tierflow:mechanic with a FILES: list, or inline if the file is already in your context. "
                "New code or tests are not mechanical - those go to tierflow:implementer."]
    if c.tier == "standard":
        if main_tier == "standard":
            return ["You are the right tier for this: do it yourself, inline. Don't delegate to a same-tier agent."]
        if main_tier == "reasoning":
            return ["This is routine engineering: hand the whole task to tierflow:implementer (Sonnet) now, before reading "
                    "code yourself. Brief: GOAL / CONTEXT (paths, no file contents) / FILES / CHECK. Trust its VALIDATION line; "
                    "don't redo its checks."]
        return [f"If you run on {pricier}: hand the whole task to tierflow:implementer (Sonnet) before reading code yourself "
                "(brief: GOAL / CONTEXT / FILES / CHECK; trust its VALIDATION line). On Sonnet: just do it inline."]
    if c.tier == "reasoning":
        lines = []
        if main_tier == "reasoning":
            lines.append("You are already on a reasoning model: make the plan yourself (short), then hand implementation to "
                         "tierflow:implementer with the plan in the brief.")
        else:
            lines.append("Get a plan from tierflow:architect (Opus) - it saves the plan to a file - then give tierflow:implementer "
                         "the plan path instead of retyping it. On Opus yourself: plan inline, delegate implementation.")
        if c.risk == "high":
            lines.append("High risk: show the plan to the user and get approval before any edit.")
        return lines
    return []


def render_hint(c: Classification, cfg: Config, *, budget_line: str | None = None,
                map_line: str | None = None, extra: list[str] | None = None, main_tier: str | None = None) -> str:
    """Compact (~60-110 token) routing hint for the main session."""
    lines = [f"[tierflow] {c.category}/{c.complexity}/{c.risk}-risk ({'; '.join(c.reasons[:2])})"]
    lines += directive(c, main_tier)
    if c.plan_first and c.tier == "standard":
        lines.append("Plan before editing; high-risk changes need the user's approval.")
    if map_line:
        lines.append(map_line)
    if budget_line:
        lines.append(budget_line)
    lines += extra or []
    return "\n".join(lines)
