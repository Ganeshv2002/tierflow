from helpers import TempHomeCase  # noqa: F401  (sets sys.path)

from tierflow.classifier import classify, render_hint
from tierflow.config import load_config


class ClassifierTests(TempHomeCase):
    def c(self, prompt, mode=None, failures=0):
        return classify(prompt, load_config(mode_override=mode), prior_failures=failures)

    def test_chat_gets_no_tier(self):
        for p in ("thanks!", "ok", "yes"):
            self.assertEqual(self.c(p).tier, "none")

    def test_questions_without_code_target_are_conversation(self):
        # real prompts from a session that were mislabelled "routine engineering -> implementer"
        for p in ("how is this benchmarking compared to ones in the market",
                  "now explain me how to use this orchestrator for multiple projects?",
                  "is the analyze part over??", "these are all common mistake dont make them"):
            c = self.c(p)
            self.assertEqual((c.category, c.tier, c.agent), ("chat", "none", None), p)

    def test_questions_about_code_are_still_lookups(self):
        for p in ("where is the JWT validation done?", "which function computes the total?",
                  "explain how the salary parser handles lakhs", "How does matching work in answerRules.ts?",
                  "what does `assessSalary` return for ranges?"):
            self.assertEqual(self.c(p).category, "discovery", p)

    def test_llm_token_usage_is_not_a_security_risk(self):
        self.assertEqual(self.c("reduce token usage in the prompt builder").risk, "low")
        self.assertEqual(self.c("fix the refresh token rotation").risk, "high")
        self.assertEqual(self.c("store the API key in the vault").risk, "high")

    def test_discovery_goes_light(self):
        c = self.c("where is the JWT validation done?")
        self.assertEqual((c.category, c.tier, c.agent), ("discovery", "light", "scout"))
        self.assertNotEqual(c.risk, "high", "reading auth code is not a high-risk edit")

    def test_mechanical_with_explicit_target(self):
        c = self.c("rename `getUser` to `fetchUser` in src/api/users.ts")
        self.assertEqual((c.category, c.tier, c.agent), ("mechanical", "light", "mechanic"))
        self.assertIn("rename", c.deterministic)

    def test_paths_do_not_leak_keywords(self):
        # 'api' in the path must not turn a rename into engineering work
        self.assertEqual(self.c("rename `a` to `b` in src/api/service/controller.ts").category, "mechanical")

    def test_mechanical_without_target_needs_judgement(self):
        self.assertEqual(self.c("rename the confusing variables in this module").tier, "standard")

    def test_formatting_is_deterministic(self):
        c = self.c("format the code")
        self.assertEqual(c.tier, "light")
        self.assertIn("formatter", c.deterministic)

    def test_standard_work(self):
        for p in ("fix the failing test in OrderServiceTests.cs", "add a CSV export endpoint to the reports controller"):
            self.assertEqual(self.c(p).tier, "standard", p)

    def test_complex_goes_to_architect_then_implementer(self):
        c = self.c("orders are intermittently duplicated when two requests arrive at once; find the root cause")
        self.assertEqual((c.tier, c.agent, c.then, c.plan_first), ("reasoning", "architect", "implementer", True))

    def test_economy_keeps_low_risk_complex_on_sonnet(self):
        c = self.c("orders are intermittently duplicated; find the root cause", mode="economy")
        self.assertEqual(c.tier, "standard")
        self.assertTrue(c.plan_first)

    def test_high_risk_forces_plan(self):
        c = self.c("add password reset flow")
        self.assertEqual(c.risk, "high")
        self.assertTrue(c.plan_first)

    def test_long_prompt_alone_does_not_select_opus(self):
        prompt = "add a tooltip to the save button. " + "The tooltip text should say Save. " * 60
        self.assertNotEqual(self.c(prompt).tier, "reasoning")

    def test_failures_escalate(self):
        self.assertEqual(self.c("fix the failing test in OrderServiceTests.cs", failures=2).tier, "reasoning")

    def test_wide_multi_service_change_is_complex(self):
        self.assertEqual(self.c("change the retry policy in all microservices that call the payment API").complexity, "complex")

    def test_root_cause_alone_is_not_complex(self):
        c = self.c("Jobs with 'INR 25L' are logged as salary_unknown. Find the root cause and fix it.")
        self.assertEqual((c.complexity, c.tier), ("moderate", "standard"))
        self.assertEqual(self.c("find the root cause of the timeout in the payment and order services").complexity, "complex")

    def test_cheap_model_never_writes_code(self):
        for p in ("generate boilerplate DTOs for `Order`", "write simple tests for `parse_job`", "add a stub for `fetchUser`"):
            self.assertNotEqual(self.c(p).tier, "light", p)

    def test_hint_is_compact(self):
        cfg = load_config()
        hint = render_hint(self.c("fix the failing test in OrderServiceTests.cs"), cfg)
        self.assertLess(len(hint) / 4, 150, "hint should stay around 100 tokens")
        self.assertIn("[tierflow]", hint)


class ConfigTests(TempHomeCase):
    def test_project_config_can_only_tighten(self):
        proj = self.tmp / "proj"
        self.write(".claude/tierflow.toml", """
[orchestrator]
mode = "quality"
[budgets]
max_cost_per_task_usd = 50.0
max_cost_per_session_usd = 1.0
enforcement = "off"
[routing]
opus_gate = "allow"
[pricing."claude-opus-5-5"]
input = 0.0
output = 0.0
""", base=proj)
        cfg = load_config(proj)
        self.assertEqual(cfg.mode, "balanced")
        self.assertEqual(cfg.section("budgets")["max_cost_per_task_usd"], 2.0)       # not raised
        self.assertEqual(cfg.section("budgets")["max_cost_per_session_usd"], 1.0)    # lowered
        self.assertEqual(cfg.section("budgets")["enforcement"], "ask")               # not loosened
        self.assertEqual(cfg.section("pricing")["claude-opus-5-5"]["input"], 4.0)
        self.assertTrue(any("cannot loosen" in w for w in cfg.warnings))
        self.assertTrue(any("[orchestrator]" in w for w in cfg.warnings))

    def test_user_config_can_change_anything(self):
        self.write("config.toml", '[orchestrator]\nmode = "economy"\n[budgets]\nmax_cost_per_task_usd = 9.0\n', base=self.home)
        cfg = load_config()
        self.assertEqual(cfg.mode, "economy")
        self.assertEqual(cfg.section("budgets")["max_cost_per_task_usd"], 9.0)
        self.assertEqual(cfg.opus_gate(), "ask")

    def test_bad_mode_falls_back(self):
        cfg = load_config(mode_override="turbo")
        self.assertEqual(cfg.mode, "balanced")
        self.assertTrue(cfg.warnings)
