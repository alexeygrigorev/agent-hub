import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_hub_host import session_messages, worker_auth

NATIVE = '11111111-2222-4333-8444-555555555555'
SESSION = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'


class NativeIntakeCredentialTests(unittest.TestCase):
    def test_provision_writes_private_profile_and_cli_finds_it(self):
        with tempfile.TemporaryDirectory() as ws:
            calls = []

            def request(method, path, body):
                calls.append((method, path))
                if method == 'GET':
                    return {'sessions': [{'session_id': SESSION, 'native_id': NATIVE, 'workspace': ws, 'intake_task_id': 'x'}]}
                return {'access_token': 'hubt_a', 'refresh_token': 'hubr_b', 'base_url': 'https://hub.test',
                        'worker_launch_id': SESSION, 'worker_expires_at': 4102444800000}

            written = session_messages.provision_intake_credentials(request, 'https://hub.test', force=True)
            self.assertEqual(written, [SESSION])
            self.assertEqual(calls, [('GET', '/api/session-intake/credentials'), ('POST', '/api/sessions/' + SESSION + '/intake-credential')])
            path = Path(ws) / '.local' / 'hub-worker-auth' / (NATIVE + '.json')
            data = json.loads(path.read_text())
            self.assertEqual(data['client_kind'], 'agent')
            self.assertEqual(data['worker_launch_id'], SESSION)
            if os.name != 'nt':
                self.assertEqual(path.stat().st_mode & 0o077, 0)
            env = {'APLEXER_SESSION_ID': NATIVE, 'APLEXER_WORKSPACE': ws, 'APLEXER_TAG': 'drain-x'}
            with mock.patch.dict(os.environ, env, clear=False):
                os.environ.pop('HUB_CONFIG_FILE', None)
                os.environ.pop('HUB_WORKER_CONFIG_FILE', None)
                self.assertEqual(worker_auth.config_path(), path)
                from agent_hub_host import config
                self.assertEqual(config.load_config()['access_token'], 'hubt_a')

    def test_unlinked_or_unprovisioned_session_keeps_normal_resolution(self):
        with tempfile.TemporaryDirectory() as ws:
            env = {'APLEXER_SESSION_ID': NATIVE, 'APLEXER_WORKSPACE': ws, 'APLEXER_TAG': 'drain-x'}
            with mock.patch.dict(os.environ, env, clear=False):
                os.environ.pop('HUB_WORKER_CONFIG_FILE', None)
                self.assertIsNone(worker_auth.config_path())
            with mock.patch.dict(os.environ, {'APLEXER_SESSION_ID': 'not-a-uuid', 'APLEXER_WORKSPACE': ws, 'APLEXER_TAG': 'drain-x'}):
                os.environ.pop('HUB_WORKER_CONFIG_FILE', None)
                self.assertIsNone(worker_auth.config_path())

    def test_rejects_malformed_mint_response(self):
        with tempfile.TemporaryDirectory() as ws:
            def request(method, path, body):
                if method == 'GET':
                    return {'sessions': [{'session_id': SESSION, 'native_id': NATIVE, 'workspace': ws}]}
                return {'access_token': 'stolen', 'refresh_token': 'x'}
            self.assertEqual(session_messages.provision_intake_credentials(request, 'https://hub.test', force=True), [])
            self.assertFalse((Path(ws) / '.local').exists())


if __name__ == '__main__':
    unittest.main()
