"""Test double for `claude -p ... --output-format json`. Applies the reference solution for the task
named in the prompt and prints a result object shaped like Claude Code's JSON output."""
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bench_reference  # noqa: E402

args = sys.argv[1:]
if "--version" in args:
    print("0.0.0-fake (Claude Code)")
    sys.exit(0)
prompt = args[args.index("-p") + 1]
optimized = "--plugin-dir" in args
keys = {"reordering": "search", "calculate_total": "rename", "test_bulk_discount": "bugfix", "to_csv": "feature",
        "formatting.py": "refactor", "two different warehouses": "debug", "PricingEngine": "architecture"}
task = next(v for k, v in keys.items() if k in prompt)
if task != "search":
    bench_reference.apply(task, Path.cwd())
print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                  "total_cost_usd": 0.05 if optimized else 0.10, "num_turns": 3, "duration_ms": 1200,
                  "session_id": f"fake-{uuid.uuid4()}", "result": bench_reference.SEARCH_ANSWER if task == "search" else "done"}))
