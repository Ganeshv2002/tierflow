import json
import sys
from argparse import Namespace
from pathlib import Path

from helpers import ROOT, TempHomeCase

import bench_reference
from tierflow import bench

SUITE = ROOT / "bench" / "tasks.toml"


class BenchFixtureSoundness(TempHomeCase):
    """Every check must fail on the untouched snapshot and pass with a correct change."""

    def test_hidden_checks_are_sound(self):
        suite = bench.load_suite(SUITE)
        self.assertEqual(len(suite["task"]), 7)
        for task in suite["task"]:
            with self.subTest(task=task["id"]):
                before = bench.verify(task, suite, bench.prepare_workspace(suite, self.tmp / f"{task['id']}-before"), "")
                self.assertFalse(before["correct"], f"{task['id']}: check passes without any change")
                ws = bench.prepare_workspace(suite, self.tmp / f"{task['id']}-after")
                bench_reference.apply(task["id"], ws)
                after = bench.verify(task, suite, ws, bench_reference.SEARCH_ANSWER)
                self.assertTrue(after["correct"], f"{task['id']}: reference solution fails\n{after['verify_excerpt']}")


class BenchRunner(TempHomeCase):
    def args(self, **kw):
        base = dict(suite=str(SUITE), repeats=1, tasks="search,bugfix", arms="baseline,optimized", main_model="sonnet",
                    mode=None, claude=str(ROOT / "tests" / "fake_claude.py"), out=str(self.tmp / "results"),
                    keep_workspaces=False, dry_run=False)
        base.update(kw)
        return Namespace(**base)

    def test_argv_arms(self):
        suite = bench.load_suite(SUITE)
        t = suite["task"][0]
        base = bench.claude_argv("claude", t, suite, "baseline", self.args())
        opt = bench.claude_argv("claude", t, suite, "optimized", self.args())
        self.assertNotIn("--plugin-dir", base)
        self.assertIn("--plugin-dir", opt)
        for argv in (base, opt):
            self.assertIn("--max-budget-usd", argv)
            self.assertEqual(argv[argv.index("--model") + 1], "sonnet")   # same main model in both arms

    def test_run_with_fake_cli(self):
        self.assertEqual(bench.cmd_run(self.args()), 0)
        recs = [json.loads(l) for l in (self.tmp / "results" / "results.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(recs), 4)
        self.assertTrue(all(r["correct"] for r in recs), recs)
        report = (self.tmp / "results" / "report.md").read_text(encoding="utf-8")
        self.assertIn("cost change: -50.0%", report)

    def test_refuses_out_inside_dot_claude(self):
        out = self.tmp / ".claude" / "results"
        self.assertEqual(bench.cmd_run(self.args(out=str(out))), 2)
        self.assertFalse(out.exists())

    def test_blocked_edit_is_harness_error(self):
        def entry(role, block):
            return json.dumps({"type": role, "message": {"role": role, "content": [block]}})
        tp = self.tmp / "s1.jsonl"
        lines = [
            entry("assistant", {"type": "tool_use", "id": "t1", "name": "Edit", "input": {}}),
            entry("user", {"type": "tool_result", "tool_use_id": "t1", "is_error": True,
                           "content": "Claude requested permissions to edit C:\\x\\.claude\\a.py which is a sensitive file."}),
            entry("assistant", {"type": "tool_use", "id": "t2", "name": "Bash", "input": {}}),
            entry("user", {"type": "tool_result", "tool_use_id": "t2", "is_error": True,
                           "content": "Claude requested permissions to use Bash, but you haven't granted it yet."}),
        ]
        tp.write_text("\n".join(lines), encoding="utf-8")
        sub = self.tmp / "s1" / "subagents"
        sub.mkdir(parents=True)
        (sub / "agent-a.jsonl").write_text("\n".join(lines[:2]), encoding="utf-8")
        d = bench.permission_denials(tp)
        self.assertEqual((d["edit"], d["other"]), (2, 1))
        self.assertIn("sensitive file", d["example"])

    def test_report_excludes_harness_errors(self):
        ok = {"task": "search", "arm": "baseline", "rep": 0, "cost_usd_cli": 1.0, "correct": True}
        bad = {"task": "bugfix", "arm": "baseline", "rep": 0, "cost_usd_cli": 9.0, "correct": False,
               "harness_error": "edit blocked by the CLI 1x"}
        report = bench.summarize([ok, bad])
        self.assertIn("1 run(s) excluded", report)
        self.assertIn("total $1.00, correct 1/1", report)

    def test_dry_run_spends_nothing(self):
        self.assertEqual(bench.cmd_run(self.args(dry_run=True, claude=None)), 0)
        self.assertFalse((self.tmp / "results").exists())
