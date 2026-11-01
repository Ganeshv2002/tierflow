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

    def test_dry_run_spends_nothing(self):
        self.assertEqual(bench.cmd_run(self.args(dry_run=True, claude=None)), 0)
        self.assertFalse((self.tmp / "results").exists())
