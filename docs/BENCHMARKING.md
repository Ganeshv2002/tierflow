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
| plugin leakage | both arms pass `--settings` disabling an installed `tierflow@tierflow-local`; the optimized arm loads this source tree with `--plugin-dir` |
| CLI version | pass `--claude <path>`; the version is printed at the start and stored in every record (`cli_version`) |
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
python orch.py bench report ~/tierflow-bench/<timestamp>
```

Results go to `~/tierflow-bench/<timestamp>` (or `--out`). The runner **refuses** an `--out` inside a
`.claude` folder: Claude Code blocks edits there, so every edit task would silently "fail". On Windows also
avoid `%TEMP%` (antivirus deleted build outputs there). A run whose transcript shows a blocked Edit/Write is
recorded as `harness_error` and left out of the report.

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
- Check `cli_version` and the model ids in `tokens`: the aliases (`sonnet`, `opus`) resolve to whatever
  the CLI you ran knows. One v3 re-run silently used an old CLI (Opus/Sonnet 4.6) found first on `PATH`.
- tierflow changes the start of the prompt (its agent list), so the first tierflow run in a suite cannot
  reuse a cache that plain runs warmed and pays a cold write (~40k tokens). With few repeats that is
  visible in the totals; report it rather than hide it.
- Use at least 3 repeats. Model runs vary; with fewer repeats, treat cost differences under ~20% as noise.
- Compare **cost per correct task**, not raw cost. A cheaper run that fails costs you a rerun.
- If the optimized arm is cheaper but less correct on a category, change routing for that category
  (for example `opus_gate`, or moving the category up a tier) rather than reporting the saving.
- Then look at `results.jsonl` → `tokens`: rising `cache_read` on the main model means context is
  growing in the main session (delegate more reading). Many Haiku calls that end in escalation mean
  the mechanic gets work that needs judgement (tighten the classifier).

## Results v3 (2026-11-02, CLI 2.1.309: `opus` = Opus 5.5, `sonnet` = Sonnet 5.5)

Three arms, three codebases, two repeats, hidden checks:

- **Python**: NaukriApply (public, ~5k lines, pytest), suite in `bench/suites/naukri/`.
- **TypeScript**: private app (~23k lines, 433 vitest tests). Suite not published.
- **C#/.NET 10**: private service (~11.7k lines, xUnit v3 on Microsoft.Testing.Platform). Suite not published.

Tasks: `search` (code lookup, answer checked), `bugfix` (planted bug described by its symptom), `debug`
(cross-module, described by its symptom). Every check fails on the untouched snapshot and passes with a
reference fix. Arms: plain Opus main, plain Sonnet main (the control rounds 1-3 lacked), Sonnet main +
tierflow. Totals below are like-for-like (16 runs per arm: the TypeScript `search` task has no Sonnet runs,
so it is left out of every arm).

| arm | cost (16 runs) | correct | $ per correct task | wall | delegations / escalations |
|---|---|---|---|---|---|
| plain Opus | $4.14 | 16/16 | $0.259 | 531 s | - |
| plain Sonnet | $1.53 (-63%) | 16/16 | $0.095 | 470 s | - |
| Sonnet + tierflow | $1.79 (+17% vs Sonnet) | 16/16 | $0.112 | 452 s | 0 / 0 |

Mean cost per run by task:

| task | Opus | Sonnet | Sonnet + tierflow |
|---|---|---|---|
| search (Python, C#) | $0.231 | $0.076 | $0.103 |
| bugfix (all three) | $0.229 | $0.096 | $0.116 |
| debug (all three) | $0.308 | $0.108 | $0.113 |

Sonnet vs Opus per codebase: Python -69%, C# -62%, TypeScript -57% (bugfix + debug).

What this shows, with n = 2 (differences under ~20% may be noise):
- **The main-session model is the lever.** Sonnet 5.5 solved every task Opus 5.5 solved, for about a third
  of the cost, on all three stacks.
- **tierflow added nothing measurable on top of Sonnet** on these short tasks: 0 delegations, 0 escalations,
  same correctness. Its +17% is mostly two runs that started on a cold cache (the first tierflow run of the
  Python suite, for `search` and `bugfix`: ~40k tokens written, none read; $0.17/$0.19 vs $0.08/$0.09).
  Without them the gap is about +5%. Steady-state overhead: ~850 more tokens written on the first request
  (tierflow's agent list), ~530 fewer read from the shared prefix.
- Rounds 1-3's possible correctness gain (plain Sonnet once named a function that does not exist) did not
  reproduce: plain Sonnet was 16/16.
- Not measured: long interactive sessions, which is where delegation out of an expensive context could pay.

Invalid runs, excluded and replaced: the first v3 attempt put workspaces under `~/.claude/...`; CLI 2.1.309
blocked every edit there (20+ transcripts show "requested permissions to edit ... which is a sensitive
file"), so all bugfix/debug runs ended with an empty diff. A first re-run then used CLI 2.1.76 from `PATH`.
Its results (Opus 4.6 $0.197/run, Sonnet 4.6 $0.142, Sonnet 4.6 + tierflow $0.139; 12/12 correct each) agree
with the conclusion but are not mixed into the table. No usage-limit cut-offs occurred.

## Earlier results (2026-11-01, Claude Code CLI 2.1.76: `sonnet` = Sonnet 4.6, `opus` = Opus 4.6, `haiku` = Haiku 4.5)

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

> Correction (v3): this round had no plain-Sonnet arm. A one-repeat plain-Sonnet run cost about the same
> per 4 tasks as Sonnet + tierflow, and v3's control confirms it: the saving below is the main-model switch,
> not tierflow.


| setup | cost (CLI models) | at current prices (est.) | correct | wall |
|---|---|---|---|---|
| plain Claude Code, Opus main (the default) | $2.33 | $0.77 / 4 tasks | 12/12 | 582s |
| **Sonnet main + tierflow v2** | **$1.57 (-32.5%)** | **$0.42 / 4 tasks (-46%)** | 12/12 | 631s |

Per task: search -8%, bugfix -25%, cross-module debug -51%, feature -26%. tierflow stayed silent on routine
work (Sonnet did it inline) and sent the lookup to the Haiku scout. The scout cost more than Sonnet doing the
lookup itself ($0.10 vs $0.05), so v2.1 answers lookups inline on Sonnet (citing file:line) and keeps the
scout for Opus sessions.
