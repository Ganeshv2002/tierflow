import json

from helpers import TempHomeCase, assistant_line

from tierflow import tune
from tierflow.config import load_config, tuned_config_path
from tierflow.ledger import read_metrics


def session(sid, prompts, start_ctx=10_000, ctx_step=0, cost_each=0.05, category="standard", agents=(),
            hinted_agent=None, follow_ups=()):
    """Synthetic metrics: one prompt + one turn per entry."""
    evs, total, req = [], 0.0, 0
    for i in range(prompts):
        ts = f"2026-11-02T10:{i // 60:02d}:{i % 60:02d}+00:00"
        evs.append({"ts": ts, "event": "prompt", "session": sid, "task": f"t{i}", "new_task": True,
                    "category": category, "session_cost": total, "context_tokens": start_ctx + i * ctx_step,
                    "follow_up": i in follow_ups, "hint": bool(hinted_agent), "hinted_agent": hinted_agent})
        for a in agents:
            evs.append({"ts": ts.replace("+", ".5+"), "event": "delegation", "session": sid, "task": f"t{i}",
                        "agent": a, "decision": "allow"})
        total += cost_each
        req += 3
        evs.append({"ts": ts.replace("+", ".9+"), "event": "turn", "session": sid, "task": f"t{i}",
                    "cost_total": total, "requests": req})
    return evs


class TuneTests(TempHomeCase):
    def cfg(self):
        return load_config()

    def test_rows_measure_cost_per_prompt_including_the_first(self):
        rows = tune.prompt_rows(session("s1", 3, cost_each=0.1))
        self.assertEqual(len(rows), 3)
        self.assertEqual([round(r["cost"], 3) for r in rows], [0.1, 0.1, 0.1])
        self.assertEqual(rows[0]["requests"], 3)
        self.assertEqual([r["outcome"] for r in rows], ["ok", "ok", "unknown"])

    def test_follow_up_marks_previous_prompt_failed(self):
        evs = session("s1", 3, follow_ups=(1,))
        for e in evs:                                   # make prompt 1 a follow-up of task t0
            if e.get("task") == "t1":
                e["task"] = "t0"
        rows = tune.prompt_rows(evs)
        self.assertEqual(rows[0]["outcome"], "failed")

    def test_large_context_suggests_earlier_warning(self):
        evs = session("s1", 4, start_ctx=10_000, cost_each=0.05) + \
              session("s2", 12, start_ctx=250_000, ctx_step=10_000, cost_each=0.60)
        rep = tune.analyse(evs, self.cfg())
        ids = [s["id"] for s in rep["suggestions"]]
        self.assertIn("context-threshold", ids)
        self.assertGreater(rep["buckets"]["200k+"]["share"], 0.9)

    def test_ignored_hints_suggest_switching_them_off(self):
        evs = session("s1", 12, category="discovery", hinted_agent="tierflow:scout")
        rep = tune.analyse(evs, self.cfg())
        self.assertIn("hint-off:discovery", [s["id"] for s in rep["suggestions"]])
        # followed hints: no suggestion
        evs = session("s2", 12, category="discovery", hinted_agent="tierflow:scout", agents=("tierflow:scout",))
        self.assertNotIn("hint-off:discovery", [s["id"] for s in tune.analyse(evs, self.cfg())["suggestions"]])

    def test_costlier_delegation_suggests_inline(self):
        evs = session("s1", 10, category="discovery", agents=("tierflow:scout",), cost_each=0.10) + \
              session("s2", 10, category="discovery", cost_each=0.05)
        sug = {s["id"]: s for s in tune.analyse(evs, self.cfg())["suggestions"]}
        self.assertIn("hint-off:discovery", sug)
        self.assertIn("$0.100", sug["hint-off:discovery"]["evidence"])

    def test_too_few_samples_suggest_nothing(self):
        evs = session("s1", 5, start_ctx=300_000, cost_each=1.0, category="discovery", hinted_agent="scout")
        rep = tune.analyse(evs, self.cfg())
        self.assertEqual(rep["suggestions"], [])

    def test_apply_writes_only_whitelisted_keys_and_revert_removes(self):
        evs = session("s1", 4) + session("s2", 12, start_ctx=250_000, cost_each=0.6)
        rep = tune.analyse(evs, self.cfg())
        tune.apply(rep, ["context-threshold"])
        self.assertEqual(load_config().section("session")["warn_context_tokens"], 150_000)
        with self.assertRaises(ValueError):
            tune.apply(rep, ["no-such-id"])
        # a hand-edited tuned.toml cannot change anything outside the whitelist
        path = tuned_config_path()
        path.write_text(path.read_text(encoding="utf-8") + '\n[orchestrator]\nmode = "quality"\n', encoding="utf-8")
        cfg = load_config()
        self.assertEqual(cfg.mode, "balanced")
        self.assertTrue(any("tuned config: ignored orchestrator.mode" in w for w in cfg.warnings))
        self.assertTrue(tune.revert())
        self.assertEqual(load_config().section("session")["warn_context_tokens"], 200_000)

    def test_user_config_wins_over_tuned(self):
        tuned_config_path().parent.mkdir(parents=True, exist_ok=True)
        tuned_config_path().write_text("[session]\nwarn_context_tokens = 150000\n", encoding="utf-8")
        (tuned_config_path().parent / "config.toml").write_text("[session]\nwarn_context_tokens = 300000\n",
                                                               encoding="utf-8")
        self.assertEqual(load_config().section("session")["warn_context_tokens"], 300_000)


class ContextWarningHookTests(TempHomeCase):
    def setUp(self):
        super().setUp()
        self.repo = self.git_repo()
        self.transcript = self.tmp / "sess.jsonl"

    def ctx(self, tokens, model="claude-opus-5-5"):
        line = json.loads(assistant_line("m1", model, out=100))
        line["message"]["usage"].update({"input_tokens": 10, "cache_read_input_tokens": tokens - 110,
                                         "cache_creation_input_tokens": 0, "output_tokens": 100})
        self.transcript.write_text(json.dumps(line) + "\n", encoding="utf-8")

    def prompt(self, text="thanks, looks good"):
        return self.run_hook("prompt", {"session_id": "S1", "transcript_path": str(self.transcript),
                                        "cwd": str(self.repo), "prompt": text, "hook_event_name": "UserPromptSubmit"})

    def test_warns_once_per_growth_step_and_costs_no_model_tokens(self):
        self.ctx(150_000)
        self.assertEqual(self.prompt()[1], None)
        self.ctx(210_000)
        out = self.prompt()[1]
        self.assertIn("210k tokens", out["systemMessage"])
        self.assertIn("/compact", out["systemMessage"])
        self.assertNotIn("hookSpecificOutput", out)              # nothing added to the model's context
        self.ctx(260_000)
        self.assertEqual(self.prompt()[1], None)                 # grew < rewarn step: stay quiet
        self.ctx(320_000)
        self.assertIn("320k", self.prompt()[1]["systemMessage"])
        self.ctx(40_000)                                         # after /compact
        self.assertEqual(self.prompt()[1], None)
        self.ctx(205_000)
        self.assertIn("205k", self.prompt()[1]["systemMessage"])
        ev = [e for e in read_metrics() if e["event"] == "prompt"]
        self.assertEqual(ev[-1]["context_tokens"], 205_000)
        self.assertTrue(ev[-1]["context_warning"])

    def test_hint_categories_off(self):
        self.ctx(20_000)
        code, out, _ = self.prompt("where is the order total computed?")
        self.assertIn("additionalContext", out["hookSpecificOutput"])
        (tuned_config_path().parent).mkdir(parents=True, exist_ok=True)
        tuned_config_path().write_text('[routing]\nhint_categories_off = ["discovery"]\n', encoding="utf-8")
        code, out, _ = self.prompt("where is the order total computed?")
        self.assertEqual(out, None)
        ev = [e for e in read_metrics() if e["event"] == "prompt"]
        self.assertEqual((ev[-1]["hint"], ev[-1]["hinted_agent"]), (False, None))
