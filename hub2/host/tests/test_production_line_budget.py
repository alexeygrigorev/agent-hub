"""Retained compatibility code counts toward the product line budget."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/production-line-budget.py"
spec = importlib.util.spec_from_file_location("production_line_budget", SCRIPT)
budget = importlib.util.module_from_spec(spec)
spec.loader.exec_module(budget)


class ProductionLineBudgetTests(unittest.TestCase):
    def run_fixture(self, extra_unknown=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            hub = root / "hub2"
            required = [hub / "host/agent_hub_host/agent.py",
                        hub / "host/agent_hub_host/transport.py",
                        hub / "host/bootstrap.py", hub / "host/pyproject.toml"]
            for path in required:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("# fixture runtime\nvalue = 1\n")
            source = hub / "compat/mcp-contract/mcp-tools.ts"
            source.parent.mkdir(parents=True)
            source.write_text("// retained contract\nexport const routes = [];\n")
            if extra_unknown:
                unknown = hub / "unclassified/unknown.ts"
                unknown.parent.mkdir()
                unknown.write_text("export const unknown = true;\n")
            out, err = io.StringIO(), io.StringIO()
            with patch.multiple(budget, ROOT=root, HUB2=hub,
                                REQUIRED_HOST_FILES=tuple(required)), \
                    patch("sys.argv", [str(SCRIPT), "--json", "--report-only-size"]), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                result = budget.main()
            return result, out.getvalue(), err.getvalue()

    def test_retained_compatibility_code_is_counted_as_product(self):
        result, out, _ = self.run_fixture()
        self.assertEqual(result, 0)
        report = json.loads(out)
        name = "hub2/compat/mcp-contract/mcp-tools.ts"
        self.assertEqual(report["product"]["files"][name],
                         {"sloc": 1, "physical_lines": 2})
        self.assertNotIn(name, report["verification_tooling"]["files"])
        self.assertNotIn(name, report["tests_and_harnesses"]["files"])
        self.assertEqual(report["accepted_release_baseline"], 8962)
        self.assertEqual(report["limit"], 6000)

    def test_unknown_source_still_fails_even_when_size_is_report_only(self):
        result, _, err = self.run_fixture(extra_unknown=True)
        self.assertEqual(result, 2)
        self.assertIn("Unclassified Hub 2 source/config files: "
                      "hub2/unclassified/unknown.ts", err)


if __name__ == "__main__":
    unittest.main()
