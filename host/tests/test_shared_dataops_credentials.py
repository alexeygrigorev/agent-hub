"""Managed sessions reach the Hub-owned DataOps credential directory (intake 3bae55cf)."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_hub_host import provisioning as p


class SharedDataopsCredentialTests(unittest.TestCase):
    def env(self, root, create):
        shared = Path(root) / 'agent-hub' / 'shared' / 'dataops'
        if create:
            shared.mkdir(parents=True)
            (shared / 'credentials.json').write_text('{}', encoding='utf-8')
        with mock.patch.object(p.config, 'get_config_path', return_value=Path(root) / 'agent-hub' / 'host.json'):
            return p.engine_environment({'provider': 'codex'}, Path(root) / 'home', Path(root)), shared

    def test_points_sessions_at_the_shared_directory_when_provisioned(self):
        with tempfile.TemporaryDirectory() as root, mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('DATAOPS_CONFIG_DIR', None)
            env, shared = self.env(root, True)
            self.assertEqual(env['DATAOPS_CONFIG_DIR'], str(shared))

    def test_absent_credential_leaves_environment_unchanged(self):
        with tempfile.TemporaryDirectory() as root, mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('DATAOPS_CONFIG_DIR', None)
            env, _ = self.env(root, False)
            self.assertNotIn('DATAOPS_CONFIG_DIR', env)


if __name__ == '__main__':
    unittest.main()
