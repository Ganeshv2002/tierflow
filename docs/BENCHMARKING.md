# Benchmarking tierflow against plain Claude Code

The goal is to show that cost goes down **without** correctness going down. The harness
(`tierflow/bench.py`) is built so the two arms can only differ in one way: whether tierflow is loaded.

| held constant | how |
|---|---|
| repository state | each run gets a fresh copy of the same snapshot, committed to a new git repo |
| prompt | identical text from the suite file |
| main-session model | `--main-model` is passed to both arms |
| hard spend cap | `claude -p --max-budget-usd` per run |
| permissions | same `--permission-mode acceptEdits` and `--allowedTools` list |
| plugin leakage | baseline passes `--settings` disabling an installed `tierflow@tierflow-local` |
| ordering effects | arm order alternates between repeats |

Measured per run: Claude Code's `total_cost_usd`, `num_turns`, `duration_ms`, wall time, per-model
tokens from the transcript (subagents, cache read/write, thinking when reported), model calls,
tierflow delegations, retries and escalations, `git diff --shortstat`, and correctness from a
**hidden** check that is copied in only after the agent finishes. "Needs human" = error, timeout, or
failed check (headless runs can't ask for help).

## Run it

```bash
# 1. see the exact commands and the worst-case spend; spends nothing
python orch.py bench run bench/tasks.toml --dry-run --main-model sonnet

# 2. the CLI needs its own login (the desktop app's login is separate)
claude auth login          # or set ANTHROPIC_API_KEY for API billing

# 3. run (7 tasks x 2 arms x N repeats; each run capped by the suite's max_budget_usd)
python orch.py bench run bench/tasks.toml --repeats 3 --main-model sonnet
python orch.py bench run bench/tasks.toml --tasks bugfix,debug --main-model opus --mode economy

# 4. re-render a report
python orch.py bench report bench/results/<timestamp>
```

The bundled suite's caps add up to $7.25 per arm per repeat, so $14.50 per repeat for both arms
(worst case; `--dry-run` prints the exact figure for your selection). Expect much less.

## The bundled suite (bench/tasks.toml)

| id | category | how correctness is checked |
|---|---|---|
| search | simple file search | answer must name `low_stock_items` and `stock.py` |
| rename | mechanical edit | hidden test: new name exists, old name gone from code and tests |
| bugfix | small bug fix | full test suite passes (bulk discount applied twice) |
| feature | feature implementation | hidden test of `to_csv` output |
| refactor | multi-file refactor | hidden test: one `fmt_money` in `formatting.py`, behaviour unchanged |
| debug | complex debugging | hidden test: cache keyed per warehouse, invalidation still works |
| architecture | architectural change | hidden test of `PricingEngine` API and preserved results |

`tests/test_bench.py` checks that every hidden check **fails** on the untouched fixture and **passes**
with a reference fix, so a "correct" result can't happen by accident.

## Add your own suite (React/TS, ASP.NET Core/C#, SQL Server)

A suite is a TOML file next to a snapshot directory. Use a real slice of your codebase and checks
that already exist in CI.

```toml
[suite]
name = "orders-service"
repo = "snapshots/orders"            # copied fresh for every run (bin/obj/node_modules skipped)
setup = [["dotnet", "restore"], ["npm", "ci", "--prefix", "web"]]
max_budget_usd = 3.0
allowed_tools = ["Read", "Edit", "Write", "Grep", "Glob", "Agent", "Task",
                 "Bash(dotnet build:*)", "Bash(dotnet test:*)", "Bash(npx tsc:*)", "Bash(npx vitest:*)", "Bash(git diff:*)"]

[[task]]
id = "fix-rounding"
category = "small bug fix"
prompt = "OrderTotalTests.Rounds_half_up fails. Fix the calculation in Orders.Domain, not the test."
verify = ["dotnet", "test", "Orders.Tests/Orders.Tests.csproj", "--filter", "FullyQualifiedName~OrderTotal"]

[[task]]
id = "csv-export"
category = "feature implementation"
prompt = "Add CSV export to the Orders page (button + GET /api/orders/export)."
hidden = "hidden/csv-export"         # tests copied in after the run
verify = ["npx", "vitest", "run", "web/src/__hidden__/export.test.ts"]
```

## Reading results honestly
- Use at least 3 repeats. Model runs vary; with fewer repeats, treat cost differences under ~20% as noise.
- Compare **cost per correct task**, not raw cost. A cheaper run that fails costs you a rerun.
- If the optimized arm is cheaper but less correct on a category, change routing for that category
  (for example `opus_gate`, or moving the category up a tier) rather than reporting the saving.
- Then look at `results.jsonl` → `tokens`: rising `cache_read` on the main model means context is
  growing in the main session (delegate more reading). Many Haiku calls that end in escalation mean
  the mechanic gets work that needs judgement (tighten the classifier).

## Results (2026-11-01, Claude Code CLI 2.1.76: `sonnet` = Sonnet 4.6, `opus` = Opus 4.6, `haiku` = Haiku 4.5)

The real-repo suite ran on a private ~23k-line TypeScript codebase (111 files, 433 vitest tests, JSON codebase
map). Its task files are not published because they contain excerpts of that code; the format is shown above.

### Sonnet main session, 1 repeat

| suite | baseline | tierflow | correct (base / tierflow) | delegations |
|---|---|---|---|---|
| bundled fixture (search, bugfix, debug) | $0.22 | $0.23 | 3/3 / 3/3 | 0 |
| private TypeScript repo (search, bugfix, debug, feature) | $0.52 | $0.50 | 3/4 / 4/4 | 0 |

Observations:
- Within noise on cost. Main-model cache reads fell on the two real-repo debugging tasks (151k -> 80k, 125k -> 92k
  tokens) but rose on search (18k -> 79k: more grep calls before answering).
- The baseline search answer named a function that does not exist; tierflow's was correct.
  One sample, so not yet evidence.
- Sonnet never delegated: for focused single tasks it solves them in ~10 turns, so the Haiku/Opus agents were
  never used. The benefit tierflow targets (long Opus sessions re-reading large contexts) is not exercised by
  short headless tasks. Next: repeat with `--main-model opus`, and with 3 repeats.

### Opus main session, 3 repeats (private TypeScript repo, 4 tasks)

| arm | cost (CLI models) | correct | delegations | wall |
|---|---|---|---|---|
| plain Claude Code | $2.33 | 12/12 | 0 | 582s |
| tierflow v1 (advisory hints) | $2.54 (+9%) | 11/12 | 0 | 636s |
| tierflow v2 (model-aware directive hints, fallbacks, plan files) | $2.47 (+6%) | 12/12 | 12/12 | 889s |

v2 made delegation happen every time and cut Opus's own work 3-4x (cache reads 48-105k -> 15-25k per run),
but the subagent re-reads the same files in a fresh context and each session pays ~6.5k tokens of fixed
system prompt, so on short tasks the total barely moves. Re-pricing the same token counts at current
model prices (Opus 5.5 / Sonnet 5.5 / Haiku 5.5): plain Opus $0.773 per 4 tasks, tierflow v2 $0.746 (-3%;
search -16% via the Haiku scout), **plain Sonnet main session $0.459 (-41%)**. The main-session model is the
dominant lever; delegation from an Opus main session only pays for lookups.

### Default setup vs recommended setup (private TypeScript repo, 4 tasks x 3 repeats)

| setup | cost (CLI models) | at current prices (est.) | correct | wall |
|---|---|---|---|---|
| plain Claude Code, Opus main (the default) | $2.33 | $0.77 / 4 tasks | 12/12 | 582s |
| **Sonnet main + tierflow v2** | **$1.57 (-32.5%)** | **$0.42 / 4 tasks (-46%)** | 12/12 | 631s |

Per task: search -8%, bugfix -25%, cross-module debug -51%, feature -26%. tierflow stayed silent on routine
work (Sonnet did it inline) and sent the lookup to the Haiku scout. The scout cost more than Sonnet doing the
lookup itself ($0.10 vs $0.05), so v2.1 answers lookups inline on Sonnet (citing file:line) and keeps the
scout for Opus sessions.
