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
            'host/tests/test_jobs.py': 'from agent_hub_host import jobs',
            'host/tests/test_misc.py': 'import agent_hub_host.jobs as j',
            'host/tests/test_unrelated.py': 'import os',
        }.items():
            (self.root / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.root / rel).write_text(text)

    def test_source_change_selects_importers_only(self):
        plan = m.select(['src/quota.ts', 'host/agent_hub_host/jobs.py'], self.root)
        self.assertEqual(plan['vitest'], ['test/quota.test.ts'])
        self.assertEqual(plan['pytest'], ['host/tests/test_jobs.py', 'host/tests/test_misc.py'])
        self.assertTrue(plan['typecheck'])
        self.assertEqual(plan['broad'], [])

    def test_docs_only_selects_nothing(self):
        plan = m.select(['docs/x.md'], self.root)
        self.assertEqual((plan['vitest'], plan['pytest'], plan['typecheck']), ([], [], False))

    def test_shared_infrastructure_is_broad(self):
        self.assertEqual(m.select(['package-lock.json', 'host/tests/conftest.py'], self.root)['broad'],
                         ['package-lock.json', 'host/tests/conftest.py'])


if __name__ == '__main__':
    unittest.main()
