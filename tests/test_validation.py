import sys
import time

from helpers import TempHomeCase

from tierflow import proc
from tierflow.config import load_config
from tierflow.validation import execute, plan, sql_risks


class ValidationTests(TempHomeCase):
    def py_repo(self, failing: bool):
        repo = self.git_repo()
        self.write("calc/__init__.py", "", base=repo)
        self.write("calc/core.py", "def add(a, b):\n    return a + b\n", base=repo)
        self.write("tests/__init__.py", "", base=repo)
        expected = 5 if failing else 4
        self.write("tests/test_core.py", f"import unittest\nfrom calc.core import add\n\nclass T(unittest.TestCase):\n"
                   f"    def test_add(self):\n        self.assertEqual(add(2, 2), {expected})\n", base=repo)
        self.commit(repo)
        self.write("calc/core.py", "def add(a, b):\n    return b + a\n", base=repo)   # the change under validation
        return repo

    def test_plan_targets_related_tests(self):
        repo = self.py_repo(failing=False)
        rep = plan(repo, load_config())
        self.assertEqual(rep.changed, ["calc/core.py"])
        self.assertEqual(len(rep.steps), 1)
        self.assertIn("tests/test_core.py", rep.steps[0].argv)

    def test_execute_pass_and_fail(self):
        ok = execute(plan(self.py_repo(failing=False), load_config()), load_config())
        self.assertTrue(ok.ok, ok.digest())
        self.tearDown(); self.setUp()
        bad = execute(plan(self.py_repo(failing=True), load_config()), load_config())
        self.assertFalse(bad.ok)
        digest = bad.digest()
        self.assertIn("FAIL", digest)
        self.assertIn("AssertionError", digest)
        self.assertLess(len(digest), 3000, "digest must stay compact")

    def test_dotnet_and_node_plans(self):
        repo = self.git_repo()
        self.write("Core/Core.csproj", "<Project></Project>", base=repo)
        self.write("Core/Money.cs", "public record Money(decimal A);\n", base=repo)
        self.write("Core.Tests/Core.Tests.csproj", '<Project><ItemGroup><PackageReference Include="Microsoft.NET.Test.Sdk" />'
                   '<ProjectReference Include="../Core/Core.csproj" /></ItemGroup></Project>', base=repo)
        self.write("web/package.json", '{"devDependencies": {"typescript": "5", "vitest": "2"}}', base=repo)
        self.write("web/tsconfig.json", "{}", base=repo)
        self.write("web/src/a.ts", "export const a = 1;\n", base=repo)
        self.commit(repo)
        rep = plan(repo, load_config(), ["Core/Money.cs", "web/src/a.ts"])
        cmds = [" ".join(s.argv) for s in rep.steps]
        self.assertTrue(any(c.startswith("dotnet build Core/Core.csproj") for c in cmds), cmds)
        self.assertTrue(any(c.startswith("dotnet test Core.Tests/Core.Tests.csproj") for c in cmds), cmds)
        self.assertTrue(any("tsc --noEmit" in c for c in cmds), cmds)
        self.assertTrue(any("vitest related --run src/a.ts" in c for c in cmds), cmds)

    def test_sql_risks(self):
        self.assertIn("DROP statement", sql_risks("DROP TABLE dbo.Orders"))
        self.assertIn("DELETE without WHERE", sql_risks("DELETE FROM Orders;"))
        self.assertEqual(sql_risks("DELETE FROM Orders WHERE Id = 1; UPDATE x SET a = 1 WHERE id = 2"), [])
        self.assertIn("UPDATE without WHERE", sql_risks("update Orders set Paid = 1\nGO"))
        self.assertEqual(sql_risks("-- DROP TABLE x\nSELECT 1"), [])


class ProcTests(TempHomeCase):
    def test_timeout_kills(self):
        t = time.monotonic()
        r = proc.run([sys.executable, "-c", "import time; time.sleep(30)"], timeout=1)
        self.assertTrue(r.timed_out)
        self.assertLess(time.monotonic() - t, 20)

    def test_no_shell_injection(self):
        r = proc.run([sys.executable, "-c", "import sys; print(sys.argv[1])", "a; echo injected"])
        self.assertEqual(r.stdout.strip(), "a; echo injected")

    def test_missing_executable(self):
        r = proc.run(["definitely-not-a-real-exe-tierflow"])
        self.assertIsNotNone(r.error)
        self.assertFalse(r.ok)
