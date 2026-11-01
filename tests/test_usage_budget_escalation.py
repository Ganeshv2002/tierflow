from helpers import TempHomeCase, assistant_line

from tierflow.budget import check
from tierflow.config import load_config
from tierflow.escalation import advise, parse_outcome, tier_of_model
from tierflow.pricing import cost_of
from tierflow.usage import last_model, scan_file, session_usage


class UsageTests(TempHomeCase):
    def test_dedupe_split_thinking_and_cost(self):
        cfg = load_config()
        t = self.tmp / "s.jsonl"
        line = assistant_line("m1", "claude-opus-5-5", inp=1000, out=2000, cache_read=10000, cw=0, cw1h=4000, thinking=500)
        t.write_text(line + line + line, encoding="utf-8")       # 3 content blocks of one response
        tot = scan_file(t, cfg, incremental=False)
        m = tot.models["claude-opus-5-5"]
        self.assertEqual((m.requests, m.input, m.output, m.cache_read, m.cache_write_1h, m.thinking), (1, 1000, 2000, 10000, 4000, 500))
        expected = (1000 * 4 + 2000 * 20 + 10000 * 0.2 + 4000 * 8) / 1e6
        self.assertAlmostEqual(m.cost_usd, expected, places=9)

    def test_growing_snapshots_use_final_values(self):
        # CLI 2.1.76 writes the same message id with output_tokens 8, then 253 (seen in a live run)
        cfg = load_config()
        t = self.tmp / "s.jsonl"
        t.write_text(assistant_line("m1", "claude-sonnet-4-6", out=8) + assistant_line("m1", "claude-sonnet-4-6", out=253)
                     + assistant_line("m2", "claude-sonnet-4-6", out=8), encoding="utf-8")
        self.assertEqual(scan_file(t, cfg).models["claude-sonnet-4-6"].output, 261)
        with open(t, "a", encoding="utf-8") as fh:          # the pending message grows in a later scan
            fh.write(assistant_line("m2", "claude-sonnet-4-6", out=151))
        m = scan_file(t, cfg).models["claude-sonnet-4-6"]
        self.assertEqual((m.requests, m.output), (2, 404))

    def test_missing_fields_are_not_invented(self):
        cfg = load_config()
        t = self.tmp / "s.jsonl"
        t.write_text(assistant_line("m1", "claude-sonnet-5-5", cw=100) + assistant_line("m2", "some-future-model"), encoding="utf-8")
        tot = scan_file(t, cfg, incremental=False)
        self.assertIsNone(tot.models["claude-sonnet-5-5"].thinking)
        self.assertEqual(tot.models["claude-sonnet-5-5"].cache_write_5m, 100)    # no TTL split -> 5m
        self.assertIsNone(tot.models["some-future-model"].cost_usd)
        self.assertEqual(tot.unknown_models, ["some-future-model"])

    def test_incremental_and_partial_lines(self):
        cfg = load_config()
        t = self.tmp / "s.jsonl"
        t.write_text(assistant_line("m1", "claude-haiku-5-5"), encoding="utf-8")
        self.assertEqual(scan_file(t, cfg).requests, 1)
        with open(t, "a", encoding="utf-8") as fh:
            fh.write(assistant_line("m2", "claude-haiku-5-5") + assistant_line("m3", "claude-haiku-5-5")[:-20])  # last one half-written
        self.assertEqual(scan_file(t, cfg).requests, 2)
        with open(t, "a", encoding="utf-8") as fh:
            fh.write(assistant_line("m3", "claude-haiku-5-5")[-20:])
        self.assertEqual(scan_file(t, cfg).requests, 3)

    def test_subagents_included(self):
        cfg = load_config()
        t = self.tmp / "abc.jsonl"
        t.write_text(assistant_line("m1", "claude-sonnet-5-5"), encoding="utf-8")
        self.write("abc/subagents/agent-1.jsonl", assistant_line("s1", "claude-haiku-5-5", out=50))
        su = session_usage(t, cfg)
        self.assertEqual(set(su.total.models), {"claude-sonnet-5-5", "claude-haiku-5-5"})
        self.assertEqual(last_model(t), "claude-sonnet-5-5")

    def test_haiku_long_context_rate(self):
        cfg = load_config()
        short = cost_of("claude-haiku-5-5", {"input": 50_000, "output": 1000}, cfg)
        long = cost_of("claude-haiku-5-5", {"input": 150_000, "output": 1000}, cfg)
        self.assertAlmostEqual(short, (50_000 * 0.10 + 1000 * 0.5) / 1e6)
        self.assertAlmostEqual(long, (150_000 * 0.50 + 1000 * 2.5) / 1e6)


class BudgetTests(TempHomeCase):
    def test_reservation_and_enforcement(self):
        cfg = load_config()
        self.assertEqual(check(cfg, 0.5, 0.5, "reasoning").action, "allow")
        d = check(cfg, 1.0, 1.0, "reasoning")                  # 1.0 + 1.2 reserve > 2.0
        self.assertEqual(d.action, "ask")
        self.assertIn("task", d.reason)
        self.assertEqual(check(cfg, 1.0, 1.0, "light").action, "allow")
        self.assertEqual(check(cfg, 99, 99, "light").action, "warn")          # cheap tier never blocked
        self.assertEqual(check(cfg, 99, 99, "standard").action, "ask")
        cfg.data["budgets"]["enforcement"] = "off"
        self.assertEqual(check(cfg, 99, 99, "reasoning").action, "allow")


class EscalationTests(TempHomeCase):
    def test_parse(self):
        o = parse_outcome("CHANGES:\n- a\nSTATUS: failed; CONFIDENCE: low; FILES: a.py, b.py; VALIDATION: fail; REASON: test red")
        self.assertEqual((o.status, o.confidence, o.files, o.validation, o.reason), ("failed", "low", ["a.py", "b.py"], "fail", "test red"))
        self.assertTrue(o.failed)
        self.assertEqual(parse_outcome("no status here").status, "unknown")

    def test_ladder(self):
        cfg = load_config()
        task = {}
        fail = parse_outcome("STATUS: failed; VALIDATION: fail")
        self.assertEqual(advise(task, "light", fail, cfg).action, "retry")
        a = advise(task, "light", fail, cfg)
        self.assertEqual(a.action, "escalate")
        self.assertIn("implementer", a.message)
        self.assertIn("FAILURE REPORT", a.message)
        self.assertEqual(advise(task, "standard", fail, cfg).action, "retry")
        a = advise(task, "standard", fail, cfg)
        self.assertEqual(a.action, "escalate")
        self.assertIn("architect", a.message)
        self.assertEqual(advise(task, "reasoning", fail, cfg).action, "stop")   # 5 attempts = max_total_attempts
        self.assertEqual(task["escalations"], 2)

    def test_needs_info_and_scope(self):
        cfg = load_config()
        self.assertEqual(advise({}, "reasoning", parse_outcome("STATUS: needs_info; REASON: which tenant?"), cfg).action, "ask_user")
        self.assertEqual(advise({}, "standard", parse_outcome("STATUS: scope_exceeded"), cfg).action, "reassess")
        self.assertEqual(advise({}, "light", parse_outcome("STATUS: done; CONFIDENCE: low; VALIDATION: pass"), cfg).action, "none")

    def test_reasoning_failure_stops(self):
        cfg = load_config()
        task = {}
        fail = parse_outcome("STATUS: failed")
        advise(task, "reasoning", fail, cfg)
        self.assertEqual(advise(task, "reasoning", fail, cfg).action, "stop")

    def test_tier_of_model(self):
        cfg = load_config()
        self.assertEqual(tier_of_model("opus", cfg), "reasoning")
        self.assertEqual(tier_of_model("claude-haiku-5-5", cfg), "light")
        self.assertIsNone(tier_of_model(None, cfg))
