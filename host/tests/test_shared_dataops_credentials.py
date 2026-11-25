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
        with mock.patch.object(p.config, 'get_config_path', return_value=Path(root) / 'agent-hub' / 'host.json'), \
                mock.patch.object(p, 'operator_config_dir', lambda name: Path(root) / 'operator' / name), \
                mock.patch.dict(os.environ, {'HUB_SHARED_CLI_ROOT': str(Path(root) / 'agent-hub/shared')}):
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

    def test_operator_login_wins_over_the_private_copy(self):
        with tempfile.TemporaryDirectory() as root, mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('DATAOPS_CONFIG_DIR', None)
            operator = Path(root) / 'operator' / 'dataops'
            operator.mkdir(parents=True)
            (operator / 'credentials.json').write_text('{}', encoding='utf-8')
            env, _ = self.env(root, True)
            self.assertEqual(env['DATAOPS_CONFIG_DIR'], str(operator))

    def test_dapier_uses_shared_store_instead_of_operator_login(self):
        with tempfile.TemporaryDirectory() as root, mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('DATAOPS_CONFIG_DIR', None)
            dapier = Path(root) / 'operator' / 'dapier'
            dapier.mkdir(parents=True)
            (dapier / 'session.json').write_text('{}', encoding='utf-8')
            shared = Path(root) / 'agent-hub/shared/dapier'
            shared.mkdir(parents=True)
            (shared / 'session.json').write_text('{}', encoding='utf-8')
            self.env(root, False)
            link = Path(root) / 'home' / '.config' / 'dapier'
            self.assertTrue(link.is_symlink())
            self.assertEqual(link.resolve(), Path(root) / 'agent-hub/shared/dapier')
            self.assertNotEqual(link.resolve(), dapier.resolve())

    def test_old_host_symlink_is_retargeted_even_before_shared_pairing(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root) / 'home'
            link = home / '.config/dapier'
            link.parent.mkdir(parents=True)
            link.symlink_to(Path(root) / 'expired-host/dapier', target_is_directory=True)
            self.env(root, False)
            self.assertEqual(link.resolve(), Path(root) / 'agent-hub/shared/dapier')


if __name__ == '__main__':
    unittest.main()
