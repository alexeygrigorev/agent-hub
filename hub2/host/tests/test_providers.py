import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from agent_hub_host import provider_home
from agent_hub_host.providers import provision

ZAI_BUNDLE_FILE = '.config/goz/config.json'
ZAI_BUNDLE = json.dumps({'config': {'schemaVersion': 1}, 'zai_token': 'synthetic-zai-token-no-spaces',
                         'zai_base_url': 'https://api.z.ai/api/anthropic'})


class ProvisionTests(unittest.TestCase):
    def test_secret_materialization_precedes_generation_ack_and_controller_stays_private(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            calls = []

            class Client:
                token = 'synthetic-parent-token'
                base_url = 'http://127.0.0.1:1'
                def request(self, method, path, body=None):
                    calls.append((method, path, body))
                    if method == 'GET' and '/credentials?' in path:
                        return {'generation': 7, 'files': {ZAI_BUNDLE_FILE: ZAI_BUNDLE}}
                    if method == 'GET':
                        return {'account': {'provider': 'zai'}}
                    self_test.assertTrue((root / 'providers').exists())
                    return {'acknowledged': True}
            self_test = self
            real_materialize = provider_home.materialize
            def no_subprocess(*args, **kwargs):
                raise AssertionError('provision must not spawn helper processes')
            with mock.patch.object(provider_home, 'materialize', wraps=real_materialize) as spy:
                if os.name != 'nt':
                    patcher = mock.patch('subprocess.run', side_effect=no_subprocess)
                    patcher.start()
                    self.addCleanup(patcher.stop)
                launch = {'engine': 'codex', 'model': 'glm-5.3-flash', 'timeout_seconds': 3600,
                          'worker_context': {'machine_id': 'host-1'}}
                offer = {'account_id': 'zai-central', 'session_id': 'session-1', 'operation_id': 'operation-1', 'epoch': 2}
                manifest, controller = provision(Client(), offer, launch, root, {'codex': {'provider': 'zai'}})
                # In-process materialization receives the bundle in memory only:
                # it must never reach an HTTP request body or a child process argv.
                for method, path, body in calls:
                    self_test.assertNotIn(ZAI_BUNDLE, json.dumps(body))
            self.assertEqual(calls[-1], ('POST', '/api/accounts/zai-central/api/account/credentials', {'generation': 7}))
            self.assertEqual(json.loads(Path(manifest).read_text())['generation'], 7)
            saved = json.loads(Path(controller).read_text())
            self.assertEqual(saved['token'], 'synthetic-parent-token')
            self.assertEqual(saved['binding'], {**offer, 'machine_id': 'host-1'})
            self.assertNotIn('synthetic-parent-token', json.dumps(manifest))
            if os.name != 'nt':
                self.assertEqual(Path(controller).stat().st_mode & 0o777, 0o600)

    def test_materialization_isolated_between_attempts_and_replay_stable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            class Client:
                token = 'synthetic-parent-token'
                base_url = 'http://127.0.0.1:1'
                def request(self, method, path, body=None):
                    if method == 'GET' and '/credentials?' in path:
                        return {'generation': 7, 'files': {ZAI_BUNDLE_FILE: ZAI_BUNDLE}}
                    if method == 'GET':
                        return {'account': {'provider': 'zai'}}
                    return {'acknowledged': True}
            launch = {'engine': 'codex', 'model': 'glm-5.3-flash', 'timeout_seconds': 3600,
                      'worker_context': {'machine_id': 'host-1'}}
            first = {'account_id': 'zai-central', 'session_id': 'session-1', 'operation_id': 'operation-1', 'epoch': 1}
            routes = {'codex': {'provider': 'zai'}}
            manifest1, _ = provision(Client(), first, dict(launch), root, routes)
            home1 = Path(manifest1).parent / 'home'
            harness = (home1 / '.zcodex' / 'config.toml').read_text()
            self.assertIn('wire_api="zcode"', harness)
            # A mutated materialized home is refused on replay verification.
            (home1 / '.zcodex' / 'config.toml').write_text(harness + '\n# SDK-owned mutation\n')
            with self.assertRaisesRegex(RuntimeError, 'provider_materialization_refused'):
                provision(Client(), first, dict(launch), root, routes)
            second = {**first, 'session_id': 'session-2', 'operation_id': 'operation-2'}
            manifest2, _ = provision(Client(), second, dict(launch), root, routes)
            replay, _ = provision(Client(), second, dict(launch), root, routes)
            self.assertNotEqual(Path(manifest1).parent, Path(manifest2).parent)
            self.assertEqual(manifest2, replay)
            self.assertEqual((Path(manifest2).parent / 'home' / '.zcodex' / 'config.toml').read_text(), harness)
            higher = {**second, 'epoch': 2}
            manifest3, _ = provision(Client(), higher, dict(launch), root, routes)
            self.assertNotEqual(Path(manifest2).parent, Path(manifest3).parent)

    def test_wrong_provider_refuses_before_bundles_or_ack(self):
        calls = []

        class Client:
            def request(self, *args):
                calls.append(args)
                return {'account': {'provider': 'zai'}}
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, 'provider_route_mismatch'):
                provision(Client(), {'account_id': 'codex-central'}, {'engine': 'codex'}, directory,
                          {'codex': {'provider': 'codex'}})
        self.assertEqual(len(calls), 1)


if __name__ == '__main__':
    unittest.main()
