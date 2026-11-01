import json
import os

from helpers import TempHomeCase

from tierflow.codemap import discover
from tierflow.config import load_config
from tierflow.context import Searcher, build_context, definition_regex, identifiers_of
from tierflow.deps import analyze

import re


class DefinitionRegexTests(TempHomeCase):
    def test_languages(self):
        cases = {
            "OrderService": ["public class OrderService : IOrderService", "export class OrderService {", "class OrderService:"],
            "GetOrders": ["    public async Task<List<Order>> GetOrders(int id)", "CREATE OR ALTER PROCEDURE dbo.GetOrders"],
            "loadUser": ["export async function loadUser(id: string) {", "const loadUser = async (id) => {",
                         "export const loadUser = (id: string) =>"],
            "usp_GetOrders": ["create procedure [dbo].[usp_GetOrders]"],
            "parse_job": ["def parse_job(raw):"],
        }
        for name, lines in cases.items():
            rx = re.compile(definition_regex(name))
            for line in lines:
                self.assertTrue(rx.search(line), f"{name} not found in {line!r}")
        self.assertFalse(re.search(definition_regex("loadUser"), "  await loadUser(id);"))

    def test_identifiers(self):
        ids = identifiers_of("fix `OrderService.GetOrders` and parse_job in src/x.ts; also usp_GetOrders")
        for i in ("OrderService", "GetOrders", "parse_job", "usp_GetOrders"):
            self.assertIn(i, ids)
        self.assertNotIn("src/x.ts", ids)


class ContextTests(TempHomeCase):
    def make_repo(self):
        repo = self.git_repo()
        self.write("src/orders/OrderService.cs", "namespace Shop.Orders;\npublic class OrderService\n{\n"
                   "    public decimal GetTotal(Order o)\n    {\n        return o.Lines.Sum(l => l.Price);\n    }\n}\n", base=repo)
        self.write("src/orders/OrderController.cs", "public class OrderController\n{\n    private readonly OrderService _svc;\n"
                   "    public IActionResult Get() => Ok(_svc.GetTotal(null));\n}\n", base=repo)
        self.write("web/src/api/orders.ts", "export async function fetchOrders() {\n  return [];\n}\n", base=repo)
        self.write("web/src/pages/OrdersPage.tsx", "import { fetchOrders } from '../api/orders';\nexport function OrdersPage() {}\n", base=repo)
        self.write("db/usp_GetOrders.sql", "CREATE PROCEDURE dbo.usp_GetOrders AS SELECT * FROM Orders\n", base=repo)
        self.write("node_modules/junk/index.ts", "export class OrderService {}\n", base=repo)
        self.write("codebase-map.json", json.dumps({"modules": {"orders": {
            "keywords": ["order total", "checkout"], "entryPoints": ["OrderService.GetTotal@src/orders/OrderService.cs"],
            "tests": ["tests/OrderServiceTests.cs"]}}}), base=repo)
        self.commit(repo)
        return repo

    def test_finds_definitions_and_map(self):
        repo = self.make_repo()
        pack = build_context("fix the order total calculation in `OrderService`", repo, load_config(), use_cache=False)
        paths = [f.path for f in pack.files]
        self.assertEqual(paths[0], "src/orders/OrderService.cs")
        self.assertNotIn("node_modules/junk/index.ts", paths)
        self.assertEqual(pack.map_hits[0]["name"], "orders")
        self.assertIn("tests/OrderServiceTests.cs", pack.suggested_tests)
        self.assertTrue(any("GetTotal" in s.text for s in pack.files[0].snippets))

    def test_budget_and_cache(self):
        repo = self.make_repo()
        cfg = load_config()
        big = build_context("OrderService fetchOrders usp_GetOrders", repo, cfg, budget_tokens=5000, use_cache=False)
        small = build_context("OrderService fetchOrders usp_GetOrders", repo, cfg, budget_tokens=150)
        self.assertLessEqual(small.tokens_est, 150)
        self.assertLess(small.tokens_est, big.tokens_est)
        self.assertTrue(small.truncated)
        again = build_context("OrderService fetchOrders usp_GetOrders", repo, cfg, budget_tokens=150)
        self.assertTrue(again.cached)
        self.write("src/orders/OrderService.cs", "public class OrderService {}\n", base=repo)   # dirty -> new key
        self.assertFalse(build_context("OrderService fetchOrders usp_GetOrders", repo, cfg, budget_tokens=150).cached)

    def test_python_fallback_matches_git_backend(self):
        repo = self.make_repo()
        cfg = load_config()
        a = Searcher(repo, cfg.section("context")["exclude_dirs"])
        b = Searcher(repo, cfg.section("context")["exclude_dirs"])
        b.rg = b.git = None
        b.backend = "python"
        pat = definition_regex("fetchOrders")
        self.assertEqual({(h.path, h.line) for h in a.grep(pat)}, {(h.path, h.line) for h in b.grep(pat)})

    def test_map_discovery_is_read_only(self):
        repo = self.make_repo()
        before = (repo / "codebase-map.json").read_bytes()
        cm = discover(repo)
        self.assertTrue(cm.found)
        cm.query("checkout order total")
        self.assertEqual((repo / "codebase-map.json").read_bytes(), before)

    def test_markdown_map(self):
        repo = self.git_repo()
        self.write("docs/ai-context/CODEBASE_MAP.md", "# Map\n\n## Payments\nCard capture lives in `src/pay/capture.ts`.\n\n## Search\nx\n", base=repo)
        hits = discover(repo).query("payment capture flow")
        self.assertEqual(hits[0].name, "Payments")
        self.assertIn("src/pay/capture.ts", hits[0].files)


class DepsTests(TempHomeCase):
    def test_ts_python_csharp_sql(self):
        repo = self.git_repo()
        self.write("web/package.json", "{}", base=repo)
        self.write("web/src/api/orders.ts", "export function fetchOrders() {}\n", base=repo)
        self.write("web/src/pages/OrdersPage.tsx", "import { fetchOrders } from '../api/orders';\n", base=repo)
        self.write("app/billing.py", "def charge(): pass\n", base=repo)
        self.write("app/jobs.py", "from app.billing import charge\n", base=repo)
        self.write("Core/Core.csproj", "<Project></Project>", base=repo)
        self.write("Core/Money.cs", "public record Money(decimal Amount);\n", base=repo)
        self.write("Api/Api.csproj", '<Project><ItemGroup><ProjectReference Include="..\\Core\\Core.csproj" /></ItemGroup></Project>', base=repo)
        self.write("Api/PriceController.cs", "class PriceController { Money m; }\n", base=repo)
        self.write("Api.Tests/Api.Tests.csproj", '<Project><ItemGroup><PackageReference Include="xunit" />'
                   '<ProjectReference Include="..\\Api\\Api.csproj" /></ItemGroup></Project>', base=repo)
        self.write("db/usp_Charge.sql", "CREATE PROCEDURE dbo.usp_Charge AS SELECT 1\n", base=repo)
        self.write("Api/ChargeRepo.cs", 'class ChargeRepo { string sql = "EXEC usp_Charge"; }\n', base=repo)
        self.commit(repo)
        rep = analyze(repo, ["web/src/api/orders.ts", "app/billing.py", "Core/Money.cs", "db/usp_Charge.sql"],
                      load_config().section("context")["exclude_dirs"])
        self.assertIn("web/src/pages/OrdersPage.tsx", rep.dependents["web/src/api/orders.ts"])
        self.assertIn("app/jobs.py", rep.dependents["app/billing.py"])
        self.assertIn("Api/PriceController.cs", rep.dependents["Core/Money.cs"])
        self.assertIn("Api/ChargeRepo.cs", rep.dependents["db/usp_Charge.sql"])
        self.assertEqual(rep.projects["Core/Money.cs"], "Core/Core.csproj")
        self.assertEqual(set(rep.downstream_projects), {"Api/Api.csproj", "Api.Tests/Api.Tests.csproj"})
