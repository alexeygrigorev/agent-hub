import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hub2_agent_hub_host.cli import main
from hub2_agent_hub_host.client import save_config


class AdvertisementCliTests(unittest.TestCase):
    def test_standalone_heartbeat_loads_resource_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.json'
            save_config(path, {'machine_id': 'test-host', 'roots': [directory], 'engines': ['codex'],
                               'capabilities': ['tool:agent-browser'], 'workspaces': {'home': directory}})
            with patch('hub2_agent_hub_host.cli.Bridge') as bridge, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(['--config', str(path), 'heartbeat']), 0)
                self.assertEqual(bridge.call_args.kwargs['capabilities'], ['tool:agent-browser'])
                self.assertEqual(bridge.call_args.kwargs['workspaces'], {'home': directory})
                bridge.return_value.heartbeat.assert_called_once_with([], include_native_sessions=False)

    def test_configuration_is_local_preserves_enrollment_and_replaces_observations(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / 'v2.json'
            config = {'machine_id': 'test-host', 'roots': [str(root)], 'engines': ['codex'],
                      'token': 'synthetic-test-token', 'refresh_token': 'synthetic-refresh', 'expires_at': 0,
                      'launcher': {'private_provider_homes': True}}
            save_config(config_path, config)
            def invoke(*args):
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    return main(['--config', str(config_path), 'configure-host', *args])
            with patch('hub2_agent_hub_host.client.Client.request', side_effect=AssertionError('network forbidden')):
                self.assertEqual(invoke('--capability', 'browser:orin-chrome', '--capability', 'tool:agent-browser',
                                        '--workspace', 'home=' + str(root)), 0)
                updated = json.loads(config_path.read_text())
                self.assertEqual({k: updated[k] for k in config}, config)
                self.assertEqual(updated['workspaces'], {'home': str(root)})
                before = config_path.read_bytes()
                self.assertEqual(invoke('--workspace', 'home=' + str(root.parent)), 1)
                self.assertEqual(config_path.read_bytes(), before)
                self.assertEqual(invoke(), 0)
                cleared = json.loads(config_path.read_text())
                self.assertEqual(cleared['capabilities'], [])
                self.assertEqual(cleared['workspaces'], {})
