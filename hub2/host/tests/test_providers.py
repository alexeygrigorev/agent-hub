import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

from agent_hub_host import provider_home
from agent_hub_host.providers import provision

ZAI_BUNDLE_FILE = '.config/goz/config.json'
ZAI_BUNDLE = json.dumps({'config': {'schemaVersion': 1}, 'zai_token': 'synthetic-zai-token-no-spaces',
                         'zai_base_url': 'https://api.z.ai/api/anthropic'})


class ProvisionTests(unittest.TestCase):
    claude_access_bundle = json.dumps({'claudeAiOauth': {
        'accessToken': 'synthetic-claude-access-token', 'refreshToken': 'synthetic-claude-refresh-token',
        'expiresAt': int(time.time() * 1000) + 12 * 60 * 60 * 1000,
    }})

    def test_claude_sdk_must_be_the_fable_capable_release(self):
        self.assertEqual(provider_home.SDK_VERSIONS['claude'], '2.1.322')
        with mock.patch.object(provider_home.shutil, 'which', return_value='/usr/bin/claude'), \
             mock.patch.object(provider_home.subprocess, 'run', return_value=mock.Mock(
                 returncode=0, stdout='2.1.322\n')):
            self.assertEqual(provider_home.sdk_version('claude'), '2.1.322')
        with mock.patch.object(provider_home.shutil, 'which', return_value='/usr/bin/claude'), \
             mock.patch.object(provider_home.subprocess, 'run', return_value=mock.Mock(
                 returncode=0, stdout='2.1.289\n')):
            with self.assertRaisesRegex(ValueError, 'provider_sdk_refresh_policy_unverified'):
                provider_home.sdk_version('claude')

    def test_managed_claude_installs_exact_native_version_without_lifecycle_scripts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'engines'
            calls = []
            system = {'linux': 'linux', 'darwin': 'darwin', 'win32': 'win32'}[provider_home.sys.platform]
            cpu = {'x86_64': 'x64', 'amd64': 'x64', 'aarch64': 'arm64', 'arm64': 'arm64'}[
                provider_home.platform.machine().lower()]
            musl = '-musl' if system == 'linux' and provider_home.platform.libc_ver()[0] == 'musl' else ''

            def fake_npm(argv, **kwargs):
                calls.append(argv)
                staging = Path(argv[argv.index('--prefix') + 1])
                main = staging / 'node_modules' / '@anthropic-ai' / 'claude-code'
                native = staging / 'node_modules' / '@anthropic-ai' / f'claude-code-{system}-{cpu}{musl}'
                main.mkdir(parents=True)
                native.mkdir(parents=True)
                (main / 'package.json').write_text(json.dumps({'version': '2.1.322'}), encoding='utf8')
                (native / 'package.json').write_text(json.dumps({'version': '2.1.322'}), encoding='utf8')
                binary = native / ('claude.exe' if system == 'win32' else 'claude')
                binary.write_text('synthetic native executable', encoding='utf8')
                if system != 'win32':
                    binary.chmod(0o700)
                return mock.Mock(returncode=0)

            with mock.patch.object(provider_home, 'resolve_npm_command', return_value=['npm-fixture']), \
                 mock.patch.object(provider_home.subprocess, 'run', side_effect=fake_npm), \
                 mock.patch.object(provider_home, '_verify_claude_binary', return_value='2.1.322'):
                binary = provider_home.install_managed_claude('2.1.322', root=root)
                replay = provider_home.install_managed_claude('2.1.322', root=root)
            self.assertEqual(binary, replay)
            self.assertTrue(Path(binary).is_file())
            self.assertEqual(len(calls), 1)
            self.assertIn('--ignore-scripts', calls[0])
            self.assertIn('--package-lock=false', calls[0])
            self.assertEqual(calls[0][-1], '@anthropic-ai/claude-code@2.1.322')
            with mock.patch.object(provider_home, 'claude_engine_root', return_value=root), \
                 mock.patch.object(provider_home, '_verify_claude_binary', return_value='2.1.322'):
                self.assertEqual(provider_home.verify_managed_claude_binary(binary), '2.1.322')

    def test_managed_claude_refuses_versions_before_fable_support(self):
        with mock.patch.object(provider_home, 'resolve_npm_command') as npm:
            with self.assertRaisesRegex(ValueError, 'provider_sdk_version_too_old'):
                provider_home.install_managed_claude('2.1.304')
        npm.assert_not_called()

    def test_fable_smoke_uses_the_versioned_binary_and_exact_model(self):
        manifest = '/private/provider/manifest.json'
        data = {'provider': 'claude', 'engine_version': '2.1.322',
                'engine_binary_path': '/private/engines/claude-2.1.322/claude'}
        with mock.patch.object(provider_home, 'verify', return_value=data), \
             mock.patch.object(provider_home, 'native_environment', return_value={'CLAUDE_CONFIG_DIR': '/private/home/.claude'}), \
             mock.patch.object(provider_home, 'clean_provider_environment', return_value={'PATH': '/usr/bin'}), \
             mock.patch.object(provider_home.subprocess, 'run', return_value=mock.Mock(
                 returncode=0, stdout='ok\n', stderr='')) as run, \
             mock.patch.object(provider_home, '_record_claude_fable_smoke') as marker:
            self.assertTrue(provider_home.claude_fable_smoke(manifest))
        argv = run.call_args.args[0]
        self.assertEqual(argv[0], data['engine_binary_path'])
        self.assertEqual(argv[argv.index('--model') + 1], 'claude-fable-5-2')
        self.assertEqual(argv[-1], 'say ok')
        self.assertEqual(run.call_args.kwargs['env']['CLAUDE_CONFIG_DIR'], '/private/home/.claude')
        marker.assert_called_once_with(data['engine_binary_path'], '2.1.322')

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
                manifest = provision(Client(), offer, launch, root, {'codex': {'provider': 'zai'}})
                # In-process materialization receives the bundle in memory only:
                # it must never reach an HTTP request body or a child process argv.
                for method, path, body in calls:
                    self_test.assertNotIn(ZAI_BUNDLE, json.dumps(body))
            self.assertEqual(calls[-1], ('POST', '/api/accounts/zai-central/api/account/credentials', {'generation': 7}))
            self.assertEqual(json.loads(Path(manifest).read_text())['generation'], 7)
            # The controller config is gone with the external launcher path: the
            # parent token must never be written into a workspace-adjacent file.
            self.assertEqual(list((root / 'controllers').glob('*')) if (root / 'controllers').exists() else [], [])
            self.assertNotIn('synthetic-parent-token', json.dumps(json.loads(Path(manifest).read_text())))

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
            manifest1 = provision(Client(), first, dict(launch), root, routes)
            home1 = Path(manifest1).parent / 'home'
            harness = (home1 / '.zcodex' / 'config.toml').read_text()
            self.assertIn('wire_api="zcode"', harness)
            # A mutated materialized home is refused on replay verification.
            (home1 / '.zcodex' / 'config.toml').write_text(harness + '\n# SDK-owned mutation\n')
            with self.assertRaisesRegex(RuntimeError, 'provider_materialization_refused'):
                provision(Client(), first, dict(launch), root, routes)
            second = {**first, 'session_id': 'session-2', 'operation_id': 'operation-2'}
            manifest2 = provision(Client(), second, dict(launch), root, routes)
            replay = provision(Client(), second, dict(launch), root, routes)
            self.assertNotEqual(Path(manifest1).parent, Path(manifest2).parent)
            self.assertEqual(manifest2, replay)
            self.assertEqual((Path(manifest2).parent / 'home' / '.zcodex' / 'config.toml').read_text(), harness)
            higher = {**second, 'epoch': 2}
            manifest3 = provision(Client(), higher, dict(launch), root, routes)
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

    def test_claude_uses_the_hub_bundle_native_version_fable_smoke_and_sync_ack(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / 'manifest.json'
            binary = str(root / 'managed' / 'claude-2.1.322' / 'claude')
            manifest.write_text(json.dumps({'engine_binary_path': binary}), encoding='utf8')
            engine = {'label': 'hub-claude', 'provider': 'claude', 'version': '2.1.322',
                      'revision': '12345678-1234-1234-1234-123456789abc',
                      'credential_generation': 8,
                      'files': {provider_home.PATHS['claude']: self.claude_access_bundle}}
            calls = []

            class Client:
                token = 'synthetic-parent-token'
                base_url = 'https://hub.invalid'
                def request(self, method, path, body=None):
                    calls.append((method, path, body))
                    if method == 'GET' and path == '/api/accounts/claude/api/account':
                        return {'account': {'provider': 'claude'}}
                    if method == 'GET' and path == '/api/engines/sync':
                        return {'engines': [engine], 'latest_version': '2.1.322'}
                    if method == 'GET' and path == '/api/engines/versions':
                        return {'reports': []}
                    if method == 'POST' and path == '/api/engines/version-report':
                        return {'status': 'recorded'}
                    if method == 'POST' and path == '/api/engines/sync-report':
                        return {'status': 'recorded'}
                    raise AssertionError((method, path))

            launch = {'engine': 'claude', 'model': 'fable', 'timeout_seconds': 600}
            offer = {'account_id': 'claude', 'session_id': 'session-1', 'operation_id': 'op-1', 'epoch': 1}
            routes = {'claude': {'provider': 'claude'}}
            with mock.patch.object(provider_home, 'install_managed_claude', return_value=binary) as install, \
                 mock.patch.object(provider_home, 'materialize', return_value=str(manifest)) as materialize, \
                 mock.patch.object(provider_home, 'verify', return_value={'engine_binary_path': binary}), \
                 mock.patch.object(provider_home, 'claude_fable_smoke_is_current', return_value=False), \
                 mock.patch.object(provider_home, 'claude_fable_smoke', return_value=True) as smoke:
                result = provision(Client(), offer, launch, root, routes)

            self.assertEqual(result, str(manifest))
            install.assert_called_once_with('2.1.322')
            materialize.assert_called_once()
            self.assertEqual(materialize.call_args.kwargs['engine_binary'], binary)
            smoke.assert_called_once_with(str(manifest))
            paths = [path for _, path, _ in calls]
            self.assertEqual(paths, ['/api/accounts/claude/api/account', '/api/engines/sync',
                                     '/api/engines/versions', '/api/engines/version-report',
                                     '/api/engines/sync-report'])
            report = calls[-2][2]['engines']['claude']
            self.assertEqual(report, {'installed': '2.1.322', 'latest': '2.1.322', 'result': 'green'})
            sync_report = calls[-1][2]
            self.assertEqual(sync_report['state'], 'synced')
            self.assertEqual(sync_report['items'][0]['credential_generation'], 8)
            self.assertNotIn('synthetic-claude', json.dumps(calls))

    def test_failed_claude_fable_smoke_reports_failure_and_blocks_launch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / 'manifest.json'
            manifest.write_text(json.dumps({'engine_binary_path': str(root / 'claude')}), encoding='utf8')
            engine = {'label': 'hub-claude', 'provider': 'claude', 'version': '2.1.322',
                      'revision': '12345678-1234-1234-1234-123456789abc',
                      'credential_generation': 8,
                      'files': {provider_home.PATHS['claude']: self.claude_access_bundle}}
            calls = []

            class Client:
                token = 'synthetic-parent-token'
                base_url = 'https://hub.invalid'
                def request(self, method, path, body=None):
                    calls.append((method, path, body))
                    if path == '/api/accounts/claude/api/account':
                        return {'account': {'provider': 'claude'}}
                    if path == '/api/engines/sync':
                        return {'engines': [engine], 'latest_version': '2.1.322'}
                    if path == '/api/engines/versions':
                        return {'reports': []}
                    if path == '/api/engines/version-report':
                        return {'status': 'recorded'}
                    raise AssertionError((method, path))

            with mock.patch.object(provider_home, 'install_managed_claude', return_value=str(root / 'claude')), \
                 mock.patch.object(provider_home, 'materialize', return_value=str(manifest)), \
                 mock.patch.object(provider_home, 'verify', return_value={'engine_binary_path': str(root / 'claude')}), \
                 mock.patch.object(provider_home, 'claude_fable_smoke_is_current', return_value=False), \
                 mock.patch.object(provider_home, 'claude_fable_smoke', side_effect=ValueError('synthetic failure')):
                with self.assertRaisesRegex(RuntimeError, 'provider_fable_smoke_failed'):
                    provision(Client(), {'account_id': 'claude', 'session_id': 's', 'operation_id': 'o', 'epoch': 1},
                              {'engine': 'claude', 'model': 'fable', 'timeout_seconds': 600}, root,
                              {'claude': {'provider': 'claude'}})
            self.assertEqual(calls[-1][1], '/api/engines/version-report')
            self.assertEqual(calls[-1][2]['engines']['claude']['result'], 'failed')
            self.assertNotIn('/api/engines/sync-report', [path for _, path, _ in calls])


if __name__ == '__main__':
    unittest.main()
