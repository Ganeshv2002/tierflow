# tierflow

Token-efficient model routing for [Claude Code](https://code.claude.com). Cheap models do lookups and
mechanical edits, Sonnet does engineering, Opus only plans the hard parts, and deterministic tools
(git grep/ripgrep, your codebase map, compilers, test runners) do everything that doesn't need a model.

It is a **Claude Code plugin plus a stdlib-only Python toolkit**. No separate agent loop, no extra model
calls for routing, no vector DB, no new credentials. Your interactive workflow, permissions and login stay
as they are. Design and trade-offs: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Measured result

Real private TypeScript repo (~23k lines, 433 tests), 4 tasks (lookup, bug fix, cross-module debugging,
feature) x 3 repeats, identical repo snapshots, hidden tests decide correctness
([method](docs/BENCHMARKING.md)):

| setup | cost (12 runs) | correct | wall time |
|---|---|---|---|
| plain Claude Code, Opus main session | $2.33 | 12/12 | 582 s |
| **Sonnet main session + tierflow** | **$1.57 (-32.5%)** | **12/12** | 631 s |

Per task: lookup -8%, bug fix -25%, cross-module debug -51%, feature -26%. Costs are Claude Code's own
`total_cost_usd` (list-price estimate) with the CLI's models at the time (Opus 4.6 / Sonnet 4.6 / Haiku 4.5).
Re-pricing the same token counts at current model prices gives about -46%.

What did **not** work, also measured: routing hints that the model was free to ignore (0 delegations, +9%),
and delegating everything out of an Opus session (12/12 delegations, still +6%: each subagent re-reads the
files in a fresh context). See [docs/BENCHMARKING.md](docs/BENCHMARKING.md) for every run.

## Where the tokens go, and what tierflow changes

Measured on the Opus runs: **67% of a task's cost was cache writes** (everything the main session reads is
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
Switch per session with `/model opus` when you want it. Turn tierflow off with
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
