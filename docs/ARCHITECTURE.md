# tierflow — architecture and decisions

_Researched 2026-10-31 against the official Claude Code docs (code.claude.com/docs) and the
Claude API reference. Local environment at design time: Claude Code CLI 2.1.76 (standalone),
desktop app on a newer build, Python 3.11, ripgrep 14, dotnet, node._

## 1. What Claude Code already does (and we must not rebuild)

| Need | Native feature | Used how |
|---|---|---|
| Per-task model choice | Subagents with `model: haiku \| sonnet \| opus \| inherit` frontmatter; per-call `model` param on the Agent tool | Five tiered agents ship in the plugin |
| Dynamic model IDs | Aliases resolve to the current model; `ANTHROPIC_DEFAULT_{HAIKU,SONNET,OPUS}_MODEL` override them | Agents use aliases only. No model ID is hard-coded except in the **pricing** table (needed to price transcripts) |
| Context isolation | Each subagent has a fresh context; only its final message returns | Read-heavy work goes to a cheap scout so the main context stays small |
| Prompt caching | Automatic in Claude Code (system prompt, tools, history) | We keep injected text short and stable so it does not break cache prefixes |
| Thinking control | `effort` (low…max) per agent; thinking can't be disabled on 5.5 models | Haiku agents run `effort: low`, architect `high` |
| Planning | Plan mode, `opusplan` alias (Opus plans, Sonnet executes) | Not recommended for long sessions: each plan-mode toggle is a model switch, and a switch re-reads the whole conversation without cache hits. tierflow gets Opus planning from the `architect` subagent instead, which leaves the main cache intact |
| Enforcement points | Hooks: `UserPromptSubmit` (inject context), `PreToolUse` (allow/ask/deny), `PostToolUse`, `SubagentStop`, `Stop` | Routing hint, budget gate, edit-scope gate, escalation advice, usage logging |
| Packaging | Plugins (`agents/`, `skills/`, `hooks/hooks.json`, `${CLAUDE_PLUGIN_ROOT}`) | Whole orchestrator is one plugin; load with `--plugin-dir` or a local marketplace |
| Hard cost cap | `claude -p --max-budget-usd` (print mode only) | Used by the benchmark runner |
| Cost reporting | `/usage`, transcripts with per-message `usage` incl. `output_tokens_details.thinking_tokens` and cache 5m/1h split | We parse transcripts; we don't invent numbers that aren't there |

**Decision: hybrid = Claude Code plugin + a small stdlib-only Python toolkit.** No external
orchestrator loop, no Agent SDK process, no vector DB.

Why not a separate orchestrator (Agent SDK / API loop)?
- It would need API-key billing (the Agent SDK is not meant to run on a claude.ai subscription
  login), would duplicate permissions/auth/caching that Claude Code already has, and would pull you
  out of the interactive workflow.
- Every extra model call it made to "decide" routing would be pure overhead. Here, routing is a
  deterministic classifier (zero tokens) plus the main session's own judgement.

## 2. Components

```
user prompt
   │
   ▼  UserPromptSubmit hook (python, ~50 ms, 0 tokens)
classifier ──► one ~80-token hint: class / complexity / risk / tier / agent / deterministic tool / budget
   │           (skipped for chit-chat; skipped when there's nothing useful to say)
   ▼
main session (your model) reads the hint, uses the `route` skill when it needs the playbook
   │
   ├─ trivial: does it inline (delegation has a fixed cost — see §4)
   ├─ discovery: `orch context` (ripgrep + codebase map, 0 tokens) → scout (haiku) only if reading is heavy
   ├─ simple edit with an exact spec → mechanic (haiku)
   ├─ moderate → implementer (sonnet)
   └─ complex / high risk → architect (opus, read-only) → plan → implementer / mechanic
   │
   ▼  PreToolUse(Agent): budget gate + opus gate + overlapping-edit lock
   ▼  PreToolUse(Edit|Write) inside tierflow editing agents: scope gate (files outside the claim → ask)
   ▼  PostToolUse(Agent): parse the agent's STATUS line → ledger → escalation advice back to main
   ▼  `orch validate` (local build/tests for affected + downstream projects, 0 tokens)
   ▼  Stop / SubagentStop: incremental transcript parse → metrics.jsonl
```

| Module | Responsibility |
|---|---|
| `tierflow/config.py` | Defaults + user config + project config. **Project config can only tighten** (lower budgets, add context paths/validation commands); it cannot change mode, routing, pricing or loosen gates. |
| `tierflow/classifier.py` | Deterministic task classification (category, complexity, risk, tier, agent, plan needed, deterministic alternative). Prompt length never selects Opus. |
| `tierflow/codemap.py` | Finds and reads existing maps (`codebase-map.json` routing maps, `docs/ai-context/CODEBASE_MAP.md`, `ARCHITECTURE.md`, …). Read-only; never writes a map. |
| `tierflow/context.py` | Symbol/file-level retrieval with ripgrep (git grep / Python fallback), language-aware definition patterns (TS/JS, C#, SQL, Python), ranking, dedup, token budget. Returns path:line references, not whole files. |
| `tierflow/deps.py` | Reverse-dependency expansion: who imports/references the changed files (TS imports, C# `ProjectReference` + namespace use, Python imports). Feeds validation. |
| `tierflow/validation.py` | Detects affected projects and runs build/typecheck/tests with timeouts, no shell, process-tree kill. Returns a compact failure digest. |
| `tierflow/usage.py` | Transcript parser: dedupes by message id, main + subagent transcripts, cache and thinking tokens when present, incremental by byte offset. |
| `tierflow/pricing.py` | Prices usage from the configurable table. Unknown model → cost `null`, never a guess. |
| `tierflow/ledger.py` | Per-session state (prompt cost snapshots, delegations, attempts, edit claims, validations) with a file lock. |
| `tierflow/budget.py` | Session/task budget decisions with tier reservations. |
| `tierflow/escalation.py` | Evidence-based retry/escalate/stop policy and the compact failure report format. |
| `tierflow/hooks.py` | Hook entry points. Errors are reported (exit 1 → visible notice), never silently swallowed; the budget gate fails open with a message. |
| `tierflow/tune.py` | `orch tune`: per-prompt rows from the metrics (cost until the next prompt, context size at the start, hint followed, agents, outcome), compared within context-size groups; suggestions are applied only on request, to a whitelisted `tuned.toml`. |
| `tierflow/bench.py` | Baseline vs optimized benchmark harness over identical repo snapshots. |

## 3. Routing policy

Tiers map to aliases in config (`light=haiku`, `standard=sonnet`, `reasoning=opus`).

| Signal (deterministic) | Effect |
|---|---|
| Search/summarize/explain/list/where-is | `discovery`, light tier, prefer `orch context`/rg first |
| rename/format/boilerplate/add-import with explicit target | `mechanical`, light tier; suggest deterministic tool (IDE/LSP rename, prettier, `dotnet format`) |
| fix/implement/add/refactor/test/integrate | `standard` |
| architecture/design/root cause/intermittent/race/deadlock/memory leak/trade-off/"which approach" | `complex` → architect plans, implementer executes |
| auth/security/crypto/payments/PII/migrations/DROP/prod data/secrets | raises **risk**; high risk forces a plan + approval wording |
| scope words ("all services", "across", "every") and many named files/modules | raises complexity one step |
| ledger shows repeated failure on this task | escalates one tier |

Modes shift thresholds rather than rewriting rules:
- **economy**: complex+low-risk stays on Sonnet (no Opus plan); Opus delegation is gated with `ask`.
- **balanced**: Opus only for complex or high-risk; unplanned Opus delegations are logged and flagged.
- **quality**: moderate+medium-risk also gets an Opus plan; no Opus gate.

## 4. Cost model behind "delegate or not"

List prices per MTok (input / output / cache read): Opus 5.5 $4 / $20 / $0.20, Sonnet 5.5
$2 / $10 / $0.20, Haiku 5.5 $0.10 / $0.50 / ~$0.01. Cache writes are 1.25× (5 min) or 2× (1 h)
input.

- A subagent starts with its own system prompt + tool definitions + CLAUDE.md (roughly 5–15k
  tokens). On Haiku that fixed cost is ≈ $0.001; on Sonnet ≈ $0.02; on Opus ≈ $0.05.
- Anything read in the main session is re-read from cache on every later turn. 25k tokens of file
  content read into an Opus main session costs ≈ $0.10–0.20 to write to cache plus ≈ $0.005 per
  later turn; the same reading in a Haiku scout costs ≈ $0.003 and only the summary returns.
- Therefore: delegate **reading-heavy** work to the scout; do **small edits inline**; never spawn a
  subagent for a one-line change. The single biggest lever is the main-session model: in the author's own usage, sessions ran
  100% on Opus. Running the main session on Sonnet with the Opus architect on call is where most
  savings come from — and the benchmark is how you verify that quality holds. Choose the main model
  at session start: a mid-session `/model` switch (or an `opusplan` plan-mode toggle) re-reads the
  whole conversation uncached on the new model.

## 5. Context engineering

- `orch context "<task>"` → codebase-map hits (module, entry points `symbol@path`, tests) + ranked
  definition sites with ±N-line snippets, capped by `context.max_context_tokens` (chars/4 estimate,
  documented as an estimate). Results are cached per session keyed by (query, git HEAD, dirty files).
- Snippets carry `path:line` so agents can verify; summaries from agents are treated as possibly
  incomplete — the playbook requires them to cite files/symbols.
- Delegation prompts pass references + a short brief, not file contents. Subagent contexts are
  isolated by Claude Code.
- Map content is never injected automatically; only the map's path and matched module names are,
  so repository text cannot steer the router.

## 6. Safety boundaries

- Hooks never grant permissions: they only return `ask`/`deny`, or no decision. Claude Code's own
  permission system and auth stay in charge.
- Policy comes from the plugin's default config and `~/.claude/tierflow/config.toml`. Repo files,
  logs and model output are data: project config may only tighten; agent STATUS lines feed the
  escalation ledger but cannot change budgets or modes.
- Subprocesses run with argument lists (no shell), timeouts, and process-tree kill.
- Editing agents declare `FILES:`; a second editing agent claiming an overlapping file is denied
  until the first finishes (or use `isolation: worktree`).
- SQL changes with DROP/TRUNCATE/DELETE-without-WHERE are flagged high risk and need approval.

## 7. Known limitations (stated, not hidden)

- **Budget enforcement is at delegation boundaries and prompt start.** Claude Code has no hook to
  stop the main session mid-turn on cost; tierflow gates new delegations (`ask`/`deny`) and warns at
  the next prompt. For hard caps in headless runs use `--max-budget-usd` (the bench does).
- Costs are **estimates at list price** from transcript usage. On a Pro/Max subscription they are
  API-equivalent numbers for comparison, not a bill.
- Thinking tokens are reported only when the transcript includes `output_tokens_details`
  (newer builds); otherwise they are included in output tokens and shown as `n/a`.
- Subagent transcripts are read from `<session>/subagents/*.jsonl` (current layout); if a build
  stores them elsewhere, subagent usage is reported as missing rather than guessed.
- **`ask` only protects interactive sessions.** Observed live (CLI 2.1.76, `-p --permission-mode
  acceptEdits`): an `ask` from a hook was resolved by the permission mode and the edit went ahead. Use
  `enforcement = "deny"` if a gate must hold in headless runs.
- **Cost estimates differ from Claude Code's `total_cost_usd` on 1-hour cache writes.** Token counts
  match exactly (verified live), but Claude Code priced 1h-TTL cache writes at the 5-minute rate
  (1.25×) where the published price list says 2×. tierflow uses the published rate; the benchmark
  records both figures. Compare arms with one figure consistently.
- Transcripts from some builds write a growing usage snapshot per response (output 8 -> 253 for one
  message); the parser keeps the final values. Windows paths over 260 chars are opened with `\\?\`.
- The classifier is heuristic. It is cheap and transparent (`orch classify --explain`), and the
  benchmark is how its thresholds should be tuned.
