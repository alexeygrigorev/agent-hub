import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class MachineRouteDisableTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        if os.name != 'nt':
            self.root.chmod(0o700)
        self.config = self.root / 'subscriptions.json'
        self.config.write_text(json.dumps({
            'version': 1,
            'workspaces': {'demo': str(self.root)},
            'subscriptions': [
                {'service': 'claude', 'account_label': 'shared-claude'},
                {'service': 'codex', 'account_label': 'shared-codex'},
                {'service': 'codex', 'account_label': 'desktop-codex'},
            ],
        }), encoding='utf-8')

    def run_with_writable_config(self, fn):
        from agent_hub_host import private_fs, provisioning
        with patch.object(provisioning, 'managed_subscriptions', return_value=[]), \
             patch.object(private_fs, 'atomic_write',
                          side_effect=lambda path, raw, limit, **kw: Path(path).write_bytes(raw)):
            return fn()

    def test_engine_and_account_disables_filter_only_this_machine_routes(self):
        from agent_hub_host import fleet

        def exercise():
            result = fleet.set_machine_route_disabled(disabled=True, engine='claude', path=self.config)
            self.assertEqual((result['scope'], result['kind'], result['status']), ('machine', 'engine', 'disabled'))
            loaded = fleet.load_settings(self.config)
            self.assertEqual([s['account_label'] for s in loaded['subscriptions']], ['shared-codex', 'desktop-codex'])
            self.assertEqual(loaded['disabled_engines'], ['claude'])

            fleet.set_machine_route_disabled(disabled=True, account='shared-codex', path=self.config)
            loaded = fleet.load_settings(self.config)
            self.assertEqual([s['account_label'] for s in loaded['subscriptions']], ['desktop-codex'])
            self.assertEqual(loaded['disabled_accounts'], ['shared-codex'])

            enabled = fleet.set_machine_route_disabled(disabled=False, engine='claude', path=self.config)
            self.assertTrue(enabled['changed'])
            loaded = fleet.load_settings(self.config)
            self.assertEqual([s['account_label'] for s in loaded['subscriptions']], ['shared-claude', 'desktop-codex'])

        self.run_with_writable_config(exercise)

    def test_disable_is_idempotent_and_invalid_settings_are_not_overwritten(self):
        from agent_hub_host import fleet

        def exercise():
            first = fleet.set_machine_route_disabled(disabled=True, account='desktop-codex', path=self.config)
            second = fleet.set_machine_route_disabled(disabled=True, account='desktop-codex', path=self.config)
            self.assertTrue(first['changed'])
            self.assertFalse(second['changed'])
            before = self.config.read_bytes()
            with self.assertRaisesRegex(ValueError, 'Invalid engine or account label'):
                fleet.set_machine_route_disabled(disabled=True, engine='../codex', path=self.config)
            self.assertEqual(self.config.read_bytes(), before)

        self.run_with_writable_config(exercise)

    def test_cli_accepts_machine_scoped_engine_disable(self):
        import io
        import sys
        from contextlib import redirect_stdout
        from agent_hub_host import cli

        out = io.StringIO()
        with patch.object(sys, 'argv', ['hub', 'subscriptions', 'disable', '--engine', 'claude',
                                        '--config', str(self.config)]), \
             patch('agent_hub_host.fleet.set_machine_route_disabled', return_value={
                 'kind': 'engine', 'target': 'claude', 'status': 'disabled', 'changed': True}), \
             redirect_stdout(out):
            cli.main()
        self.assertIn('engine claude disabled on this machine', out.getvalue())


if __name__ == '__main__':
    unittest.main()
