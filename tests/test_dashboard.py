import json
import os
import threading
import time
import urllib.error
import urllib.request

from helpers import TempHomeCase, assistant_line

from tierflow import dashboard
from tierflow.config import load_config
from tierflow.usage import scan_file

NOW = 1_800_000_000.0           # fixed clock for snapshots


def iso(e: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(e))


def asst(mid, model, ts, out=100, inp=10, cr=0, cw=0, tool=None):
    d = {"type": "assistant", "isSidechain": False, "timestamp": iso(ts), "cwd": "C:\\work\\shop",
         "message": {"id": mid, "model": model, "content": [{"type": "tool_use", "name": tool[0], "input": tool[1]}] if tool else [],
                     "usage": {"input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": cr,
                               "cache_creation_input_tokens": cw}}}
    return json.dumps(d) + "\n"


def user(text, ts):
    return json.dumps({"type": "user", "timestamp": iso(ts), "cwd": "C:\\work\\shop", "message": {"role": "user", "content": text}}) + "\n"


class DashboardTests(TempHomeCase):
    def setUp(self):
        super().setUp()
        self.root = self.tmp / "projects"
        self.cfg = load_config()

    def session(self, sid="aaaa1111-0000", folder="C--work-shop", main_lines=(), mtime=NOW - 5):
        d = self.root / folder
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{sid}.jsonl"
        p.write_text("".join(main_lines), encoding="utf-8")
        os.utime(p, (mtime, mtime))
        return d, p

    def agent(self, folder_dir, sid, aid, lines, meta=None, mtime=NOW - 5):
        sub = folder_dir / sid / "subagents"
        sub.mkdir(parents=True, exist_ok=True)
        p = sub / f"agent-{aid}.jsonl"
        p.write_text("".join(lines), encoding="utf-8")
        os.utime(p, (mtime, mtime))
        if meta is not None:
            (sub / f"agent-{aid}.meta.json").write_text(json.dumps(meta), encoding="utf-8")
        return p

    def snap(self, window=86400, bench=False):
        return dashboard.Dashboard(self.cfg, self.root).snapshot(window, bench, now=NOW)

    # ---------------------------------------------------------------------------------------------
    def test_incremental_scan_matches_usage_scan_file(self):
        lines = [asst("m1", "claude-opus-5-5", NOW - 60, out=10, cr=1000, cw=200),
                 asst("m1", "claude-opus-5-5", NOW - 59, out=253, cr=1000, cw=200),       # growing snapshot of the same response
                 asst("m2", "claude-sonnet-5-5", NOW - 50, out=40, cr=500),
                 asst("m2", "claude-sonnet-5-5", NOW - 50, out=40, cr=500)]                # repeated line
        _, p = self.session(main_lines=lines)
        ref = scan_file(p, self.cfg, incremental=False)
        s = dashboard.FileScan(p, self.cfg)
        s.refresh()
        got = dashboard._sum([s])
        for model, m in ref.models.items():
            v = got[model]
            self.assertEqual((v[0], v[2], v[3], v[4]), (m.requests, m.output, m.cache_read, m.cache_write_5m + m.cache_write_1h), model)
            self.assertAlmostEqual(v[5], m.cost_usd, places=9)
        # appending more lines is picked up without re-reading: same result as a full re-scan
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(asst("m3", "claude-opus-5-5", NOW - 40, out=7, cr=10))
        s.refresh()
        ref = scan_file(p, self.cfg, incremental=False)
        self.assertEqual(dashboard._sum([s])["claude-opus-5-5"][0], ref.models["claude-opus-5-5"].requests)

    def test_agent_states_models_and_tasks(self):
        d, _ = self.session(main_lines=[user("build the checkout page", NOW - 100),
                                         asst("m1", "claude-sonnet-5-5", NOW - 90, cr=300000, tool=("Task", {"description": "x"}))])
        brief = user("Find where the order total is computed", NOW - 80)
        a_run = self.agent(d, "aaaa1111-0000", "run1", [brief, asst("r1", "claude-haiku-5-5", NOW - 20, tool=("Grep", {"pattern": "total"}))],
                           meta={"agentType": "tierflow:scout", "description": "locate order total"}, mtime=NOW - 10)
        self.agent(d, "aaaa1111-0000", "done1", [brief, asst("d1", "claude-opus-5-5", NOW - 200)],
                   meta={"agentType": "tierflow:architect", "description": "plan it", "finished": True}, mtime=NOW - 190)
        self.agent(d, "aaaa1111-0000", "stuck1", [brief, asst("s1", "claude-sonnet-5-5", NOW - 900), asst("s2", "claude-haiku-5-5", NOW - 899)],
                   meta={"agentType": "tierflow:implementer"}, mtime=NOW - 800)
        self.agent(d, "aaaa1111-0000", "hand1", [brief, asst("h1", "claude-haiku-5-5", NOW - 300, tool=("SubagentHandback", {"message": "ok"}))],
                   meta=None, mtime=NOW - 290)
        snap = self.snap()
        by = {a["id"]: a for a in snap["agents"]}
        self.assertEqual({k: v["state"] for k, v in by.items()},
                         {"run1": "running", "done1": "finished", "stuck1": "stalled", "hand1": "finished"})
        self.assertEqual(by["run1"]["model"], "claude-haiku-5-5")
        self.assertEqual(by["run1"]["elapsed"], 80)                                       # since its first message (the brief)
        self.assertEqual(by["run1"]["description"], "locate order total")
        self.assertIn("order total", by["run1"]["task"])
        self.assertEqual(by["run1"]["doing"], "Grep total")
        self.assertEqual(by["done1"]["doing"], "")                                        # nothing "now" for a finished agent
        self.assertEqual(len(by["stuck1"]["models"]), 2)                                  # an agent can use several models
        self.assertEqual(by["stuck1"]["model"], "claude-haiku-5-5")                       # tie on requests: the latest one
        self.assertEqual(snap["kpi"]["agents_running"], 1)
        self.assertEqual((snap["kpi"]["agents_stalled"], snap["kpi"]["agents_finished"]), (1, 2))
        self.assertEqual([a["state"] for a in snap["agents"]][:2], ["running", "stalled"])   # running first
        s = snap["sessions"][0]
        self.assertEqual((s["state"], s["agents_running"], s["agents_total"], s["task"]), ("working", 1, 4, "build the checkout page"))
        self.assertEqual(s["context"], 300000 + 10 + 100)
        self.assertEqual(s["project"], "shop")

    def test_window_totals_series_and_unpriced(self):
        old, new = NOW - 3 * 86400, NOW - 600
        self.session(main_lines=[asst("m1", "claude-opus-5-5", old, out=1000), asst("m2", "claude-sonnet-5-5", new, out=500),
                                 asst("m3", "claude-mystery-9", new + 1, out=50)])
        s24 = self.snap(86400)
        self.assertEqual(s24["total"]["output"], 550)                         # the 3-day-old response is outside 24h
        s7 = self.snap(7 * 86400)
        self.assertEqual(s7["total"]["output"], 1550)
        self.assertEqual(s7["step_s"], 86400)
        self.assertEqual(s24["step_s"], 3600)
        self.assertEqual(self.snap(3600)["step_s"], 300)
        self.assertEqual(s24["unpriced"], ["claude-mystery-9"])
        self.assertEqual(sum(b.get("sonnet", {}).get("requests", 0) for b in s24["series"]), 1)
        self.assertEqual(sum(b.get("other", {}).get("requests", 0) for b in s24["series"]), 1)
        self.assertEqual(sum(b.get("opus", {}).get("requests", 0) for b in s24["series"]), 0)
        self.assertEqual(dashboard.family("claude-fable-5-1"), "other")
        self.assertGreaterEqual(len(s24["series"]), 24)                       # empty buckets are kept: a continuous axis

    def test_benchmark_workspaces_hidden_unless_asked(self):
        d, p = self.session(folder="C--x-bench-results-v3-repo", main_lines=[
            json.dumps({"type": "assistant", "timestamp": iso(NOW - 30), "cwd": "C:\\Users\\g\\.claude\\tierflow\\bench\\results\\v3\\repo",
                        "message": {"id": "b1", "model": "claude-opus-5-5", "usage": {"input_tokens": 1, "output_tokens": 5}}}) + "\n"])
        self.assertEqual(self.snap()["sessions"], [])
        self.assertEqual(len(self.snap(bench=True)["sessions"]), 1)

    def test_prompt_noise_is_not_a_task(self):
        self.session(main_lines=[user("real question about caching", NOW - 100), user("[Request interrupted by user]", NOW - 90),
                                 user("<system-reminder>ignore</system-reminder>", NOW - 80),
                                 asst("m1", "claude-opus-5-5", NOW - 70)])
        self.assertEqual(self.snap()["sessions"][0]["task"], "real question about caching")

    def test_server_is_local_read_only_and_checks_host(self):
        self.session(main_lines=[asst("m1", "claude-opus-5-5", time.time() - 5, out=9)])
        httpd = dashboard.make_server(self.cfg, 0, projects=self.root)
        port = httpd.server_address[1]
        self.assertEqual(httpd.server_address[0], "127.0.0.1")
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            def get(path, host=None):
                req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers={"Host": host} if host else {})
                return urllib.request.urlopen(req, timeout=10)
            r = get("/")
            self.assertIn(b"tierflow live", r.read())
            self.assertIn("default-src 'self'", r.headers["Content-Security-Policy"])
            data = json.loads(get("/api/snapshot?window=1h").read())
            self.assertEqual(data["window_s"], 3600)
            self.assertEqual(data["total"]["output"], 9)
            with self.assertRaises(urllib.error.HTTPError) as cm:
                get("/api/snapshot", host="evil.example.com")                    # DNS-rebinding guard
            self.assertEqual(cm.exception.code, 403)
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{port}/api/snapshot", data=b"x", method="POST"), timeout=10)
            self.assertIn(cm.exception.code, (501, 405))
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_page_never_uses_innerhtml(self):
        html = (dashboard.Path(dashboard.__file__).with_name("dashboard.html")).read_text(encoding="utf-8")
        self.assertNotIn("innerHTML", html)          # task text comes from prompts: only textContent / text nodes
        self.assertNotIn("http://", html.replace("http://www.w3.org/2000/svg", ""))
        self.assertNotIn("https://", html)           # no external scripts, fonts or images
