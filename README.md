# tierflow

Token-efficient model routing for [Claude Code](https://code.claude.com). Cheap models do lookups and
mechanical edits, Sonnet does engineering, Opus only plans the hard parts, and deterministic tools
(git grep/ripgrep, your codebase map, compilers, test runners) do everything that doesn't need a model.

It is a **Claude Code plugin plus a stdlib-only Python toolkit**. No separate agent loop, no extra model
calls for routing, no vector DB, no new credentials. Your interactive workflow, permissions and login stay
as they are. Design and trade-offs: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Measured result

Three codebases (Python, TypeScript, C#/.NET 10), three tasks each (code lookup, planted bug, cross-module
debugging; bugs described only by their symptom), 2 repeats, identical snapshots, hidden checks decide
correctness ([method and every run](docs/BENCHMARKING.md)). Claude Code CLI 2.1.309, Opus 5.5 / Sonnet 5.5:

| main session | cost (16 runs) | correct | $ per correct task |
|---|---|---|---|
| plain Claude Code, Opus | $4.14 | 16/16 | $0.26 |
| plain Claude Code, Sonnet | $1.53 (-63%) | 16/16 | $0.10 |
| Sonnet + tierflow | $1.79 (+17% vs plain Sonnet) | 16/16 | $0.11 |

**The saving is the main-session model.** Sonnet matched Opus on every task, on all three stacks, for about a
third of the cost. **tierflow added nothing measurable on top of Sonnet** for these short tasks: it never
delegated or escalated, and most of its +17% came from two runs that started on a cold prompt cache
(about +5% without them, inside the noise of n = 2). An earlier round (older models, one repo) reported
-32.5% for Sonnet + tierflow against Opus; it had no plain-Sonnet control, and that saving was the model switch.

What tierflow is for, then: guard rails (budgets, the Opus gate, overlapping-edit lock), deterministic
search and affected-test validation, and per-step cost tracking. Its delegation is aimed at long,
reading-heavy Opus sessions, which this benchmark does not measure yet. Also measured and not helpful:
hints the model may ignore (+9%) and delegating everything out of an Opus session (+6%, older models).

## Where the tokens go, and what tierflow changes

Measured on the earlier Opus 4.6 runs: **67% of a task's cost was cache writes** (everything the main session reads is
written into its prompt cache at the main model's price), 18% output, 14% cache re-reads. So:

1. **The main-session model is the biggest lever.** tierflow recommends Sonnet as the main model and
   brings in Opus only through the `architect` agent when the task is genuinely hard (concurrency,
   security, cross-service, or repeated failures).
2. **Say nothing when nothing helps.** On Sonnet, routine work gets no hint at all (every hint token is
   paid for); conversation gets none either.
3. **Keep reading out of an expensive context.** From an Opus session, lookups go to a Haiku scout that
   returns `file:line` facts instead of whole files.
4. **Deterministic first.** Search via git grep/ripgrep and your existing codebase map; validation runs only
   the build/tests of affected and downstream projects and returns a compact digest.
5. **Cheap models never write code.** Haiku only reads, summarizes and does tool-like edits (renames,
   imports, formatting). New code and tests go to Sonnet or better.
6. **Evidence-based escalation.** Retry once with a different approach, then one tier up, max 5 attempts.
   Crashed, cut-off or unvalidated agent results are caught and re-checked without a model.

```
prompt -> classifier hook (0 tokens, ~0.1 s) -> short hint only when it changes the outcome
          |- deterministic: orch context / deps / validate
          |- inline (main session already the right tier)
          |- tierflow:scout / mechanic        (haiku)
          |- tierflow:implementer / reviewer  (sonnet)
          '- tierflow:architect -> plan file -> implementer   (opus plans only)
gates: budget, opus-without-reason, overlapping edits, mechanic edit scope
after: STATUS parsing -> retry / escalate / ask user; per-step tokens + cost log
```

## Install

Requirements: Claude Code, Python 3.11+ on `PATH` as `python`, git.

```bash
claude plugin marketplace add Ganeshv2002/tierflow
claude plugin install tierflow@tierflow-local --scope user
```
or inside Claude Code: `/plugin marketplace add Ganeshv2002/tierflow`, then `/plugin install tierflow@tierflow-local`.

Recommended: make Sonnet the default main model in `~/.claude/settings.json`:
```json
{ "model": "sonnet" }
```
Pick the model when a session **starts** (`claude --model opus` for a hard session). Avoid `/model` switches
mid-session: each model has its own prompt cache, so a switch re-reads the whole conversation uncached (and with
`opusplan` every plan-mode toggle is such a switch). For one hard step inside a Sonnet session, let tierflow call
the `architect` agent instead: subagents leave the main session's cache intact. Turn tierflow off with
`claude plugin disable tierflow@tierflow-local`.

State and metrics are kept in `~/.claude/tierflow/` (override with `TIERFLOW_HOME`).

## Multiple projects

Nothing to set up per project: tierflow finds each repo's codebase map (`codebase-map.json`,
`docs/ai-context/CODEBASE_MAP.md`, `ARCHITECTURE.md`, ...; read-only), search backend and stack
(.NET / TypeScript / Python) on its own. Optional per repo:

- `.claude/tierflow.toml` - extra map paths, excluded folders, extra validation commands, lower budgets
  ([example](examples/project-tierflow.toml)). A repo file can only **tighten** policy, so a cloned repo
  can't loosen your limits.
- `.claude/settings.local.json` - `{"model": "opus", "env": {"TIERFLOW_MODE": "quality"}}` for a repo that
  needs more reasoning, or `{"enabledPlugins": {"tierflow@tierflow-local": false}}` to opt out.

## Modes and budgets

Defaults: [config/default.toml](config/default.toml). Override in `~/.claude/tierflow/config.toml`
([example](examples/user-config.toml)) or with `TIERFLOW_MODE=economy|balanced|quality`.

| mode | complex, low risk | Opus delegation without a complex/high-risk reason |
|---|---|---|
| economy | Sonnet plans + implements, Opus only after failures | asks for approval |
| balanced (default) | Opus plans, Sonnet implements | allowed, flagged |
| quality | Opus plans (also for medium-risk moderate work) | allowed |

Budgets are per task and per session at list-price estimates. When a new delegation plus its tier's
reservation would exceed a limit, it needs approval; cheap Haiku delegations are never blocked.

## `orch` CLI (no model calls)

`python orch.py <command>` (agents call it via `${CLAUDE_PLUGIN_ROOT}`).

| command | what it does |
|---|---|
| `classify "<task>" [--explain]` | routing decision and the signals behind it |
| `context "<task>" [--budget N]` | codebase-map hits + ranked definitions with snippets, within a token budget |
| `map [--query q]` | show and query the existing codebase map |
| `deps <files>` | files and .NET projects that depend on the given files |
| `validate [--files ...] [--dry-run]` | build/typecheck/tests for affected + downstream projects; compact digest |
| `usage [--transcript p]` | tokens (incl. cache and thinking where reported) and cost for a session, incl. subagents |
| `report [--since days]` | cost per model, per agent, per task and per step; retries, escalations |
| `bench run/report` | baseline vs tierflow benchmark ([docs/BENCHMARKING.md](docs/BENCHMARKING.md)) |

## Subscription vs API billing

Everything works on a Pro/Max/Team/Enterprise login and on an API key. On a subscription, costs are
API-equivalent estimates (useful for comparing setups; your plan isn't billed per token) and benchmark
runs count against plan limits. tierflow does not use the Agent SDK or call the API directly.

## Limits

- Budgets are enforced when work is delegated and checked at each new prompt; a hook can't stop the main
  session mid-turn. In headless runs use `--max-budget-usd` for a hard cap.
- A hook's `ask` only prompts in interactive sessions (headless `acceptEdits` resolves it).
- The classifier is rules, not a model: `orch classify --explain` shows why it routed a task.
- Benchmarks so far are short single tasks on two codebases; long interactive sessions are not yet
  measured. Numbers above are from one CLI version's models.

## Development

```bash
python -m unittest discover -s tests
```
69 tests: classifier, config trust rules, map/context/deps across TS/C#/SQL/Python, validation (timeouts,
no-shell execution), transcript usage parsing, budgets, escalation and fallbacks, every hook handler, and the
benchmark harness, including a check that every benchmark task fails on the untouched code and passes with a
reference fix.

## License

MIT
