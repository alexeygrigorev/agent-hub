"""Server-selected Hydrogen must pass the packaged managed host boundary."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from agent_hub_host.model_contract import configured_selection, policy_models
from agent_hub_host.provisioning import managed_model_allowed


ROOT = Path(__file__).resolve().parents[2]
MODEL_ID = 'antigravity-hydrogen-medium'
MODEL = 'gemini-4-hydrogen-medium'


class HydrogenContractTests(unittest.TestCase):
    def test_server_candidate_passes_host_selection_and_managed_allowlist(self):
        policy = json.loads((ROOT / 'config/model-policy.json').read_text())
        candidate = next(c for c in policy['candidates'] if c['id'] == MODEL_ID)
        selected = configured_selection(candidate, candidate['engine'], 'antigravity')
        self.assertEqual(selected['model'], MODEL)
        self.assertEqual(selected['effort'], 'medium')
        self.assertTrue(managed_model_allowed('antigravity', MODEL, policy_models()))

    def test_other_provider_and_unconfigured_model_still_fail_closed(self):
        self.assertFalse(managed_model_allowed('codex', MODEL, policy_models()))
        self.assertFalse(managed_model_allowed(
            'antigravity', 'gemini-4-hydrogen-unconfigured', policy_models()))
        with self.assertRaisesRegex(ValueError, 'model_unsupported'):
            configured_selection(dict(id=MODEL_ID, model=MODEL, effort='medium'),
                                 'codex', 'codex')

    def test_built_zipapp_carries_executable_hydrogen_contract(self):
        # Use the maintained builder and an isolated interpreter: the source
        # checkout must not mask a missing packaged model-routing.json.
        scratch = ROOT / '.local/tmp'
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='hydrogen-contract-', dir=scratch) as tmp:
            subprocess.run([sys.executable, str(ROOT / 'scripts/build-hub-cli.py'),
                            '--out-dir', tmp], cwd=ROOT, check=True,
                           capture_output=True, text=True, timeout=60)
            code = """
import sys
sys.path.insert(0, sys.argv[1])
from agent_hub_host import model_contract, provisioning
assert sys.argv[1] in model_contract.__file__
selection = dict(id='antigravity-hydrogen-medium',
                 model='gemini-4-hydrogen-medium', effort='medium')
assert model_contract.configured_selection(selection, 'antigravity', 'antigravity')['model'] == selection['model']
assert provisioning.managed_model_allowed('antigravity', selection['model'], model_contract.policy_models())
assert not provisioning.managed_model_allowed('antigravity', 'gemini-4-hydrogen-unconfigured', model_contract.policy_models())
"""
            subprocess.run([sys.executable, '-I', '-S', '-c', code,
                            str(Path(tmp) / 'hub.pyz')], cwd=tmp, check=True,
                           capture_output=True, text=True, timeout=15)
