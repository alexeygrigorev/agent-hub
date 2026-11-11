import importlib.util
from pathlib import Path
import tempfile
import json
import os
import subprocess
import sys
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
            'hub2/src/quota.ts': '', 'hub2/src/fleet.ts': '',
            'hub2/host/agent_hub_host/agent.py': '',
            'hub2/host/agent_hub_host/client.py': '',
            'hub2/host/tests/test_host.py': 'from agent_hub_host.agent import Bridge',
            'hub2/host/tests/test_client.py': 'from agent_hub_host.client import Client',
            'host/tests/test_operator_client.py': 'from agent_hub_host.client import Client',
            'host/agent_hub_host/client.py': '',
            'host/agent_hub_host/jobs.py': '', 'host/agent_hub_host/fleet.py': '',
            'scripts/tests/test_quota.py': "assert 'hub2/src/quota.ts' in source",
            'scripts/tests/test_apply_model_policy.py': "assert 'scripts/apply-model-policy.py' in source",
            'host/tests/test_jobs.py': 'from agent_hub_host import jobs',
            'host/tests/test_misc.py': 'import agent_hub_host.jobs as j',
            'host/tests/test_unrelated.py': 'import os',
            'host/tests/test_python_fleet_import.py': 'from agent_hub_host import fleet',
            'host/tests/test_source_path_contract.py': "assert 'hub2/src/fleet.ts' in source",
            'host/tests/test_machine_binding.py': '',
            'host/tests/test_release_completeness.py': '',
            'host/tests/test_bootstrap_control.py': '',
            'scripts/ci/test_affected_tests.py': '',
        }.items():
            (self.root / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.root / rel).write_text(text)

    def test_source_change_selects_importers_only(self):
        plan = m.select(['hub2/src/quota.ts'], self.root)
        self.assertEqual(plan['vitest'], [])
        self.assertEqual(plan['pytest'], ['scripts/tests/test_quota.py'])
        self.assertTrue(plan['typecheck'])
        self.assertEqual(plan['broad'], [])

    def test_typescript_change_does_not_select_python_module_with_same_basename(self):
        plan = m.select(['hub2/src/fleet.ts'], self.root)
        self.assertEqual(plan['vitest'], [])
        self.assertEqual(plan['pytest'], ['host/tests/test_source_path_contract.py'])

    def test_python_change_does_not_select_typescript_module_with_same_basename(self):
        plan = m.select(['host/agent_hub_host/fleet.py'], self.root)
        self.assertEqual(plan['vitest'], [])
        self.assertEqual(plan['pytest'], sorted([
            'host/tests/test_python_fleet_import.py',
            'host/tests/test_machine_binding.py',
            'host/tests/test_release_completeness.py',
            'host/tests/test_bootstrap_control.py',
        ]))

    def test_host_change_selects_canary_regressions_in_addition_to_importers(self):
        plan = m.select(['host/agent_hub_host/jobs.py'], self.root)
        self.assertEqual(plan['pytest'], sorted([
            'host/tests/test_jobs.py', 'host/tests/test_misc.py',
            'host/tests/test_machine_binding.py',
            'host/tests/test_release_completeness.py',
            'host/tests/test_bootstrap_control.py',
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

    def test_maintained_release_helper_selects_relocated_regression(self):
        plan=m.select(['scripts/apply-model-policy.py'],self.root)
        (self.root/'scripts/apply-model-policy.py').write_text('pass')
        plan=m.select(['scripts/apply-model-policy.py'],self.root)
        self.assertEqual(plan['pytest'],['scripts/tests/test_apply_model_policy.py'])

    def test_retired_trees_never_create_legacy_vitest_selection(self):
        plan=m.select(['src/mcp-tools.ts','test/mcp.test.ts'],self.root)
        self.assertEqual(plan['vitest'],[])

    def test_relocated_test_is_selected_directly(self):
        self.assertEqual(m.select(['scripts/tests/test_apply_model_policy.py'],self.root)['pytest'],
                         ['scripts/tests/test_apply_model_policy.py'])

    def test_daemon_source_maps_only_daemon_importers(self):
        plan=m.select(['hub2/host/agent_hub_host/agent.py'],self.root)
        self.assertEqual(plan['hub2_pytest'],['hub2/host/tests/test_host.py'])
        self.assertEqual(plan['pytest'],[])

    def test_changed_daemon_test_is_selected_without_full_suite(self):
        plan=m.select(['hub2/host/tests/test_client.py'],self.root)
        self.assertEqual(plan['hub2_pytest'],['hub2/host/tests/test_client.py'])
        self.assertEqual(plan['pytest'],[])
        self.assertEqual(plan['broad'],[])

    def test_same_named_client_modules_do_not_cross_package_boundaries(self):
        daemon=m.select(['hub2/host/agent_hub_host/client.py'],self.root)
        self.assertEqual(daemon['hub2_pytest'],['hub2/host/tests/test_client.py'])
        self.assertEqual(daemon['pytest'],[])
        operator=m.select(['host/agent_hub_host/client.py'],self.root)
        self.assertEqual(operator['hub2_pytest'],[])
        self.assertIn('host/tests/test_operator_client.py',operator['pytest'])

    def test_actual_wrapper_runs_cli_and_daemon_in_separate_interpreters(self):
        scripts=self.root/'scripts';(scripts/'ci').mkdir(exist_ok=True)
        wrapper=Path(__file__).resolve().parents[1]/'test-affected-local.sh'
        (scripts/wrapper.name).write_text(wrapper.read_text())
        plan={'broad':[],'vitest':[],'pytest':['host/tests/test_operator_client.py'],
              'hub2_pytest':['hub2/host/tests/test_client.py'],'typecheck':False,'build':False,'workflows':False}
        (scripts/'ci/affected_tests.py').write_text('print('+repr(json.dumps(plan))+')')
        (scripts/'ci/scope_guard.py').write_text('pass')
        (scripts/'local-dependencies.py').write_text('pass')
        cap=scripts/'capped-test-run.sh';cap.write_text('#!/bin/sh\n[ "$1" = -- ] && shift\nexec "$@"\n');cap.chmod(0o755)
        binary=self.root/'bin';binary.mkdir();git=binary/'git';git.write_text('#!/bin/sh\nexit 0\n');git.chmod(0o755)
        record=self.root/'commands.jsonl';uv=binary/'uv'
        uv.write_text('#!'+sys.executable+'\nimport os,sys,json\nfrom pathlib import Path\n'
          +"if sys.argv[1]=='run':\n assert 'HUB2_TOKEN' not in os.environ\n with Path(os.environ['WRAPPER_RECORD']).open('a') as f: f.write(json.dumps({'args':sys.argv[1:],'pythonpath':os.environ.get('PYTHONPATH')})+'\\n')\n")
        uv.chmod(0o755)
        temporary=self.root/'tmp';temporary.mkdir()
        env={**os.environ,'PATH':str(binary)+os.pathsep+os.environ['PATH'],'TMPDIR':str(temporary),'WRAPPER_RECORD':str(record),'HUB2_TOKEN':'synthetic-parent-must-not-leak'}
        env.pop('AGENT_HUB_HEAVY_TEST_HELD',None)
        result=subprocess.run(['bash',str(scripts/wrapper.name)],cwd=self.root,env=env,capture_output=True,text=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        rows=[json.loads(line) for line in record.read_text().splitlines()]
        self.assertEqual([r['pythonpath'] for r in rows],['host:host/tests','hub2/host'])
        self.assertIn('host/tests/test_operator_client.py',rows[0]['args'])
        self.assertNotIn('hub2/host/tests/test_client.py',rows[0]['args'])
        self.assertIn('hub2/host/tests/test_client.py',rows[1]['args'])
        self.assertNotIn('host/tests/test_operator_client.py',rows[1]['args'])


if __name__ == '__main__':
    unittest.main()
