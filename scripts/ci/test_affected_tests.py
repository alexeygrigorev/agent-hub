import importlib.util
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('affected_tests', Path(__file__).with_name('affected_tests.py'))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class SelectTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root = Path(self.dir.name)
        for rel, text in {
            'src/quota.ts': '', 'host/agent_hub_host/jobs.py': '',
            'test/quota.test.ts': "import '../src/quota'", 'test/other.test.ts': 'nothing',
            'test/mcp.test.ts': 'MCP integration test',
            'src/mcp-tools.ts': '', 'src/mcp.ts': '', 'src/mcp-usage.ts': '',
            'src/mcp-oauth.ts': '', 'src/conversation-hub.ts': '', 'src/worker.ts': '',
            'src/ui-pages.ts': '',
            'host/tests/test_jobs.py': 'from agent_hub_host import jobs',
            'host/tests/test_misc.py': 'import agent_hub_host.jobs as j',
            'host/tests/test_unrelated.py': 'import os',
            'host/tests/test_machine_binding.py': '',
            'host/tests/test_release_completeness.py': '',
            'host/tests/test_bootstrap_control.py': '',
            'test/test_mcp_parity.py': '',
            'scripts/ci/test_affected_tests.py': '',
        }.items():
            (self.root / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.root / rel).write_text(text)

    def test_source_change_selects_importers_only(self):
        plan = m.select(['src/quota.ts'], self.root)
        self.assertEqual(plan['vitest'], ['test/quota.test.ts'])
        self.assertEqual(plan['pytest'], [])
        self.assertTrue(plan['typecheck'])
        self.assertEqual(plan['broad'], [])

    def test_host_change_selects_canary_regressions_in_addition_to_importers(self):
        plan = m.select(['host/agent_hub_host/jobs.py'], self.root)
        self.assertEqual(plan['pytest'], sorted([
            'host/tests/test_jobs.py', 'host/tests/test_misc.py',
            'host/tests/test_machine_binding.py',
            'host/tests/test_release_completeness.py',
            'host/tests/test_bootstrap_control.py',
            'test/test_mcp_parity.py',
        ]))

    def test_release_manifest_change_selects_completeness_test(self):
        plan = m.select(['scripts/host-release-manifest.json'], self.root)
        self.assertEqual(plan['pytest'], ['host/tests/test_release_completeness.py'])

    def test_selector_change_selects_its_focused_test(self):
        plan = m.select(['scripts/ci/affected_tests.py'], self.root)
        self.assertEqual(plan['pytest'], ['scripts/ci/test_affected_tests.py'])

    def test_docs_only_selects_nothing(self):
        plan = m.select(['docs/x.md'], self.root)
        self.assertEqual((plan['vitest'], plan['pytest'], plan['typecheck']), ([], [], False))

    def test_shared_infrastructure_is_broad(self):
        self.assertEqual(m.select(['package-lock.json', 'host/tests/conftest.py'], self.root)['broad'],
                         ['package-lock.json', 'host/tests/conftest.py'])

    def test_mcp_surface_changes_always_select_mcp_integration_test(self):
        for path in sorted(m.MCP_FILES):
            with self.subTest(path=path):
                self.assertEqual(m.select([path], self.root)['vitest'], ['test/mcp.test.ts'])


if __name__ == '__main__':
    unittest.main()
