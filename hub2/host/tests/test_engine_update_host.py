import subprocess
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_hub_host.agent import Bridge


class EngineUpdateHostTest(unittest.TestCase):
    def test_run_engine_update_acks_old_and_new_versions(self):
        posted = []
        client = types.SimpleNamespace(
            request=lambda method, path, body=None: posted.append((method, path, body)) or {}
        )
        bridge = Bridge(client, None, ['/tmp'], ['codex'], machine_id='hetzner-aplexer')
        versions = ['0.166.0', '0.168.0']

        def fake_run(cmd, **kwargs):
            if cmd == 'codex --version':
                return types.SimpleNamespace(returncode=0, stdout=f'codex-cli {versions.pop(0)}\n', stderr='')
            if cmd == 'npm install -g @openai/codex@latest':
                return types.SimpleNamespace(returncode=0, stdout='added 1 package\n', stderr='')
            raise AssertionError(cmd)

        with patch.object(subprocess, 'run', side_effect=fake_run):
            bridge.run_engine_update({
                'id': '11111111-1111-4111-8111-111111111111',
                'engine': 'codex',
                'binary': 'codex',
                'version_command': 'codex --version',
                'update_command': 'npm install -g @openai/codex@latest',
            })

        self.assertEqual(len(posted), 1)
        method, path, body = posted[0]
        self.assertEqual(method, 'POST')
        self.assertEqual(path, '/api/machines/hetzner-aplexer/engine-updates/11111111-1111-4111-8111-111111111111/ack')
        self.assertTrue(body['ok'])
        self.assertEqual(body['old_version'], '0.166.0')
        self.assertEqual(body['new_version'], '0.168.0')
        self.assertEqual(body['exit_code'], 0)
        self.assertIsNone(body['error'])


if __name__ == '__main__':
    unittest.main()
