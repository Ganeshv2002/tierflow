import json

from helpers import TempHomeCase, assistant_line

from tierflow.ledger import Ledger, read_metrics


class HookTests(TempHomeCase):
    def setUp(self):
        super().setUp()
        self.repo = self.git_repo()
        self.write("codebase-map.json", json.dumps({"modules": {}}), base=self.repo)
        self.transcript = self.tmp / "sess.jsonl"
        self.transcript.write_text(assistant_line("m0", "claude-opus-5-5", out=100), encoding="utf-8")

    def base(self, **kw):
        return {"session_id": "S1", "transcript_path": str(self.transcript), "cwd": str(self.repo), **kw}

    def prompt(self, text):
        return self.run_hook("prompt", self.base(prompt=text, hook_event_name="UserPromptSubmit"))

    def agent(self, tool_use_id, agent, prompt="do it", **ti):
        return self.run_hook("pre-agent", self.base(tool_use_id=tool_use_id, tool_name="Agent",
                                                    tool_input={"subagent_type": agent, "prompt": prompt, **ti}))

    def test_prompt_hint(self):
        code, out, err = self.prompt("fix the failing test in OrderServiceTests.cs")
        self.assertEqual(code, 0, err)
        ctx = out["hookSpecificOutput"]["additionalContext"]
        # main session is Opus (from the transcript): routine work is handed to Sonnet, firmly
        self.assertIn("hand the whole task to tierflow:implementer (Sonnet) now", ctx)
        self.assertNotIn("orch =", ctx)
        self.assertLess(len(ctx) / 4, 110)
        code, out, _ = self.prompt("where is the order total computed?")
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("tierflow:scout", ctx)
        self.assertIn("codebase map exists (codebase-map.json)", ctx)     # map pointer only for lookups

    def test_same_tier_main_session_gets_no_hint(self):
        self.transcript.write_text(assistant_line("m0", "claude-sonnet-5-5", out=100), encoding="utf-8")
        code, out, _ = self.prompt("fix the failing test in OrderServiceTests.cs")
        self.assertEqual((code, out), (0, None))                          # nothing to add, nothing paid
        code, out, _ = self.prompt("where is the order total computed?")
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Answer inline", ctx)                               # scout costs more than it saves here
        self.assertNotIn("tierflow:scout", ctx)
        self.assertIn("file:line", ctx)

    def test_unknown_main_model_gets_conditional_advice(self):
        self.transcript.write_text("", encoding="utf-8")                   # first prompt of a session
        code, out, _ = self.prompt("fix the failing test in OrderServiceTests.cs")
        self.assertIn("If you run on Opus or Fable", out["hookSpecificOutput"]["additionalContext"])

    def test_chat_prompt_is_silent(self):
        code, out, _ = self.prompt("thanks!")
        self.assertEqual((code, out), (0, None))

    def test_non_user_turn_is_silent_and_starts_no_task(self):
        self.prompt("fix the failing test in OrderServiceTests.cs")
        before = Ledger("S1").read()["task"]["key"]
        code, out, _ = self.prompt('<cross-session-message from="local_1">please fix the permission check'
                                   "</cross-session-message>")
        self.assertEqual((code, out), (0, None))
        self.assertEqual(Ledger("S1").read()["task"]["key"], before)

    def test_never_returns_allow(self):
        self.prompt("fix the failing test in OrderServiceTests.cs")
        for i, agent in enumerate(["tierflow:scout", "tierflow:implementer", "Explore", "general-purpose"]):
            code, out, _ = self.agent(f"t{i}", agent)
            self.assertEqual(code, 0)
            if out:
                self.assertNotEqual(out["hookSpecificOutput"].get("permissionDecision"), "allow")

    def test_opus_gate_by_mode(self):
        self.prompt("fix the failing test in OrderServiceTests.cs")
        code, out, _ = self.agent("t1", "tierflow:architect")
        self.assertIn("Opus-tier", out["hookSpecificOutput"]["additionalContext"])     # balanced: warn only
        code, out, _ = self.run_hook("pre-agent", self.base(tool_use_id="t2", tool_input={"subagent_type": "tierflow:architect"}),
                                     env={"TIERFLOW_MODE": "economy"})
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "ask")
        economy = {"TIERFLOW_MODE": "economy"}
        self.run_hook("prompt", self.base(prompt="orders are intermittently duplicated under concurrency; find the root cause"), env=economy)
        code, out, _ = self.run_hook("pre-agent", self.base(tool_use_id="t3", tool_input={"subagent_type": "tierflow:architect"}), env=economy)
        # economy keeps low-risk complex work on sonnet, so opus is still gated
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "ask")
        self.prompt("redesign auth token refresh across all microservices")
        code, out, _ = self.agent("t4", "tierflow:architect")
        self.assertIsNone(out)                                                          # justified: no gate

    def test_budget_gate(self):
        # $1.90 already spent on Opus output in this task -> reasoning reserve exceeds the $2 task limit
        self.prompt("fix the failing test in OrderServiceTests.cs")
        with open(self.transcript, "a", encoding="utf-8") as fh:
            fh.write(assistant_line("m1", "claude-opus-5-5", out=95_000))
        code, out, _ = self.agent("t1", "tierflow:implementer")
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "ask")
        self.assertIn("budget", out["hookSpecificOutput"]["permissionDecisionReason"])
        code, out, _ = self.agent("t2", "tierflow:scout")                               # cheap tier still fits
        self.assertTrue(out is None or out["hookSpecificOutput"].get("permissionDecision") is None)

    def test_overlapping_edit_claims(self):
        self.prompt("rename `a` to `b` in src/x.ts")
        code, out, _ = self.agent("t1", "tierflow:mechanic", prompt="GOAL: x\nFILES: src/x.ts, src/y.ts")
        self.assertTrue(out is None or "permissionDecision" not in out["hookSpecificOutput"])
        code, out, _ = self.agent("t2", "tierflow:implementer", prompt="FILES: ./src/Y.ts")
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        code, out, _ = self.agent("t3", "tierflow:implementer", prompt="FILES: src/z.ts")    # disjoint: fine
        self.assertTrue(out is None or "permissionDecision" not in out["hookSpecificOutput"])
        # scope gate for the mechanic
        code, out, _ = self.run_hook("pre-edit", self.base(agent_type="tierflow:mechanic", tool_input={"file_path": str(self.repo / "src" / "other.ts")}))
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "ask")
        code, out, _ = self.run_hook("pre-edit", self.base(agent_type="tierflow:mechanic", tool_input={"file_path": str(self.repo / "src" / "x.ts")}))
        self.assertIsNone(out)
        code, out, _ = self.run_hook("pre-edit", self.base(tool_input={"file_path": "anything.ts"}))   # main session untouched
        self.assertIsNone(out)
        # finishing the mechanic releases its claim
        self.run_hook("post-agent", self.base(tool_use_id="t1", tool_input={"subagent_type": "tierflow:mechanic"},
                                              tool_response={"content": [{"type": "text", "text": "STATUS: done; FILES: src/x.ts"}]}))
        code, out, _ = self.agent("t5", "tierflow:implementer", prompt="FILES: src/y.ts")
        self.assertTrue(out is None or "permissionDecision" not in out["hookSpecificOutput"])

    def test_claim_files_formats(self):
        from tierflow.hooks import claim_files
        root = str(self.repo)
        live_brief = ("GOAL: Rename every occurrence of `calc_total` to `calculate_total`.\n\nFILES:\n- inventory/pricing.py\n"
                      "- inventory/reports.py\n- tests/test_pricing.py\n\nSTEPS:\n1. Grep for `calc_total`")
        self.assertEqual(claim_files(live_brief, root), ["inventory/pricing.py", "inventory/reports.py", "tests/test_pricing.py"])
        self.assertEqual(claim_files("FILES: a.py, src/b.ts\nCHECK: x", root), ["a.py", "src/b.ts"])
        self.assertEqual(claim_files("**FILES:** `src/x.ts` (definition), src/y.ts", root), ["src/x.ts", "src/y.ts"])
        self.assertEqual(claim_files(f"FILES: {self.repo}\\src\\Z.cs", root), ["src/z.cs"])
        self.assertEqual(claim_files("no files here", root), [])

    def test_escalation_advice_flow(self):
        self.prompt("rename `a` to `b` in src/x.ts")
        resp = {"content": [{"type": "text", "text": "STATUS: failed; VALIDATION: fail; REASON: tests red"}], "totalTokens": 1234}
        outs = []
        for i in range(2):
            self.agent(f"m{i}", "tierflow:mechanic", prompt="FILES: src/x.ts")
            code, out, _ = self.run_hook("post-agent", self.base(tool_use_id=f"m{i}", tool_input={"subagent_type": "tierflow:mechanic"}, tool_response=resp))
            outs.append(out["hookSpecificOutput"]["additionalContext"])
        self.assertIn("Retry once", outs[0])
        self.assertIn("escalate to tierflow:implementer", outs[1])
        # follow-up prompt keeps the task and the classifier escalates from the recorded failures
        code, out, _ = self.prompt("still failing, try again")
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("standard/moderate", ctx)
        self.assertIn("tierflow:implementer", ctx)          # escalated work leaves the Haiku mechanic
        self.assertNotIn("tierflow:mechanic", ctx)
        task = Ledger("S1").read()["task"]
        self.assertEqual(task["key"], "t1")
        self.assertEqual(task["escalations"], 1)

    def test_stop_and_subagent_usage_metrics(self):
        self.prompt("fix the failing test in OrderServiceTests.cs")
        sub = self.write("sess/subagents/agent-abc.jsonl", assistant_line("s1", "claude-haiku-5-5", out=10))
        self.run_hook("subagent-stop", self.base(agent_id="abc", agent_type="tierflow:scout"))
        self.run_hook("stop", self.base())
        ev = {e["event"]: e for e in read_metrics()}
        self.assertEqual(ev["subagent_usage"]["agent"], "tierflow:scout")
        self.assertIn("claude-haiku-5-5", ev["turn"]["models"])
        self.assertGreater(ev["turn"]["cost_subagents"], 0)
        self.assertTrue(sub.is_file())

    def post(self, tool_use_id, agent, resp, prompt="x"):
        return self.run_hook("post-agent", self.base(tool_use_id=tool_use_id,
                                                     tool_input={"subagent_type": agent, "prompt": prompt}, tool_response=resp))

    def test_fallback_crashed_or_empty_agent_counts_as_failure(self):
        self.prompt("fix the failing test in OrderServiceTests.cs")
        self.agent("a1", "tierflow:implementer")
        _, out, _ = self.post("a1", "tierflow:implementer", {"status": "error", "content": [{"type": "text", "text": "boom"}]})
        self.assertIn("Retry once", out["hookSpecificOutput"]["additionalContext"])
        self.agent("a2", "tierflow:implementer")
        _, out, _ = self.post("a2", "tierflow:implementer", {"status": "completed", "content": []})
        self.assertIn("escalate", out["hookSpecificOutput"]["additionalContext"])
        failures = Ledger("S1").read()["task"]["failures"]
        self.assertEqual([f["reason"] for f in failures], ["agent run ended with status 'error'", "agent returned an empty result"])

    def test_fallback_validates_when_agent_skips_status(self):
        self.write("calc/__init__.py", "", base=self.repo)
        self.write("calc/core.py", "def add(a, b):\n    return a - b\n", base=self.repo)       # broken by the "agent"
        self.write("tests/__init__.py", "", base=self.repo)
        self.write("tests/test_core.py", "import unittest\nfrom calc.core import add\nclass T(unittest.TestCase):\n"
                   "    def test_add(self):\n        self.assertEqual(add(2, 2), 4)\n", base=self.repo)
        self.prompt("rename `plus` to `add` in calc/core.py")
        self.agent("m1", "tierflow:mechanic", prompt="FILES: calc/core.py")
        _, out, _ = self.post("m1", "tierflow:mechanic", {"status": "completed", "content": [{"type": "text", "text": "Done, renamed it."}]},
                              prompt="FILES: calc/core.py")
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("validated by tierflow (agent did not)", ctx)
        self.assertIn("FAIL", ctx)
        self.assertIn("Retry once", ctx)

    def test_architect_plan_saved_to_file(self):
        self.prompt("redesign auth token refresh across all microservices")
        self.agent("p1", "tierflow:architect")
        plan = "DECISION: rotate refresh tokens\nPLAN:\n1. ...\nSTATUS: done; CONFIDENCE: high"
        _, out, _ = self.post("p1", "tierflow:architect", {"status": "completed", "content": [{"type": "text", "text": plan}]})
        ctx = out["hookSpecificOutput"]["additionalContext"]
        import re
        path = re.search(r"plan saved to (\S+\.md)", ctx).group(1)
        self.assertEqual((self.repo / path).read_text(encoding="utf-8"), plan)
        self.assertEqual((self.repo / ".claude" / "tierflow" / ".gitignore").read_text(), "*\n")
        self.assertIn("PLAN:", ctx)

    def test_per_step_cost_is_recorded(self):
        self.prompt("where is the order total computed?")
        self.write("sess/subagents/agent-ab1.jsonl", assistant_line("s1", "claude-haiku-5-5", inp=1000, out=2000))
        self.agent("s1", "tierflow:scout")
        self.run_hook("subagent-stop", self.base(agent_id="ab1", agent_type="tierflow:scout"))
        self.post("s1", "tierflow:scout", {"status": "completed", "agentId": "ab1", "totalDurationMs": 4200,
                                           "content": [{"type": "text", "text": "FINDINGS: a.ts:1\nSTATUS: done"}]})
        step = [e for e in read_metrics() if e["event"] == "agent_result"][-1]
        self.assertAlmostEqual(step["cost_usd"], (1000 * 0.10 + 2000 * 0.50) / 1e6)
        self.assertEqual((step["duration_ms"], step["task"], step["models"]), (4200, "t1", ["claude-haiku-5-5"]))
        self.run_hook("stop", self.base())
        from tierflow.report import build_report, render
        rep = build_report()
        self.assertEqual(rep["tasks"][0]["steps"][0]["agent"], "tierflow:scout")
        self.assertIn("scout: done $0.001 4s", render(rep))

    def test_bad_input_is_reported_not_swallowed(self):
        import subprocess, sys
        from helpers import ROOT
        r = subprocess.run([sys.executable, str(ROOT / "scripts" / "hook.py"), "stop"], input=b"{not json", capture_output=True)
        self.assertEqual(r.returncode, 1)
        self.assertIn("tierflow stop hook error", r.stderr.decode())
        self.assertTrue((self.home / "errors.log").is_file())
        self.assertEqual(r.stdout, b"")
