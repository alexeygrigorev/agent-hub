"""OpenCode device-login export in the central store (intake 5814e109 AC3)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from agent_hub_host import provisioning as p


class GoBundleShape(unittest.TestCase):
    def test_export_path_is_relative_and_posix_on_every_os(self):
        self.assertEqual(p.OPENCODE_DEVICE_EXPORT, '.local/share/opencode-hub/device-auth.json')
        self.assertNotIn('\\', p.OPENCODE_DEVICE_EXPORT)
        self.assertFalse(p.OPENCODE_DEVICE_EXPORT.startswith('/'))


import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from agent_hub_host import fleet

EXPORT = json.dumps([{'id': 'c1', 'active': True, 'integrationID': 'opencode',
                      'value': {'type': 'oauth', 'methodID': 'device', 'access': 'A', 'refresh': 'R'}}])


class DeviceActivation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {'AGENT_HUB_MANAGED_DIR': self.tmp.name,
                                           'AGENT_HUB_SUBSCRIPTIONS': str(Path(self.tmp.name) / 'subs.json')})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.bundle = {'label': 'shared-go', 'provider': 'go', 'revision': 'r1', 'version': '1.19.3',
                       'files': {p.OPENCODE_DEVICE_EXPORT: EXPORT}, 'env': {}, 'args': []}
        self.calls = []

    def native(self, cfg, args, env, payload=None):
        self.calls.append((args, env['XDG_DATA_HOME'], payload))
        db = Path(cfg['source_data']) / 'opencode'
        db.mkdir(parents=True, exist_ok=True)
        (db / 'opencode.db').write_bytes(b'x')

    def sync(self):
        with patch.object(p, 'engine_argv', return_value=[str(Path(self.tmp.name) / 'opencode')]):
            return p.sync_opencode_device(self.bundle, native=self.native)

    def test_imports_once_into_hub_owned_source_store(self):
        self.assertEqual(self.sync(), 'imported')
        self.assertEqual(self.sync(), 'current')
        self.assertEqual(len(self.calls), 1)
        args, data_home, payload = self.calls[0]
        self.assertEqual(args, ['auth', 'import'])
        self.assertTrue(data_home.startswith(self.tmp.name))
        self.assertEqual(json.loads(payload), json.loads(EXPORT))

    def test_changed_export_is_reimported(self):
        self.sync()
        self.bundle['files'][p.OPENCODE_DEVICE_EXPORT] = EXPORT.replace('"R"', '"R2"')
        self.assertEqual(self.sync(), 'imported')

    def test_no_store_credential_does_nothing(self):
        self.bundle['files'] = {}
        self.assertEqual(p.sync_opencode_device(self.bundle, native=self.native), 'no_store_credential')
        self.assertEqual(self.calls, [])

    def test_default_applies_only_where_unset(self):
        self.sync()
        subs = Path(self.tmp.name) / 'subs.json'
        subs.write_text(json.dumps({'version': 1, 'subscriptions': [], 'workspaces': {'home': self.tmp.name}}))
        with patch.object(fleet, 'subscriptions_path', return_value=subs):
            hub_default = fleet.load_settings().get('opencode_auth')
            explicit_default_path = fleet.load_settings(subs).get('opencode_auth')
            explicit_default_str = fleet.load_settings(str(subs)).get('opencode_auth')
        self.assertTrue(hub_default and hub_default['source_data'].startswith(self.tmp.name))
        self.assertEqual(explicit_default_path, hub_default)
        self.assertEqual(explicit_default_str, hub_default)
        other_subs = Path(self.tmp.name) / 'other-subs.json'
        other_subs.write_text(json.dumps({'version': 1, 'subscriptions': [], 'workspaces': {'home': self.tmp.name}}))
        with patch.object(fleet, 'subscriptions_path', return_value=subs):
            self.assertNotIn('opencode_auth', fleet.load_settings(other_subs))
        explicit = {'binary': '/x/opencode', 'source_data': '/x/src', 'source_config': '/x/cfg', 'target_data': '/x/rt'}
        subs.write_text(json.dumps({'version': 1, 'subscriptions': [], 'workspaces': {'home': self.tmp.name},
                                    'opencode_auth': explicit}))
        with patch.object(fleet, 'subscriptions_path', return_value=subs):
            self.assertEqual(fleet.load_settings().get('opencode_auth'), explicit)
            self.assertEqual(fleet.load_settings(subs).get('opencode_auth'), explicit)

    def test_sync_and_managed_home_materialize_quse_auth_without_touching_runtime_store(self):
        self.assertEqual(self.sync(), 'imported')
        dest = Path(self.tmp.name) / 'shared-go-r1'
        home = dest / 'home'
        quse_auth = home / p.OPENCODE_QUSE_AUTH
        self.assertTrue(quse_auth.is_file())
        self.assertEqual(json.loads(quse_auth.read_text(encoding='utf-8')),
                         {'opencode-go': {'type': 'api', 'key': 'A'}})
        runtime_dir = Path(self.tmp.name) / 'opencode-device-shared-go' / 'runtime'
        self.assertFalse((runtime_dir / 'opencode' / 'auth.json').exists())

        # Stripped bundle receipt (files={} on disk after credentials.reconcile)
        # and an existing 0o775 .local/share/opencode directory created by the
        # engine CLI still materialize .local/share/opencode/auth.json cleanly.
        quse_auth.unlink()
        if os.name != 'nt':
            os.chmod(quse_auth.parent, 0o775)
        device_export = home / p.OPENCODE_DEVICE_EXPORT
        device_export.parent.mkdir(parents=True, exist_ok=True)
        device_export.write_text(EXPORT, encoding='utf-8')
        stripped = dict(self.bundle, files={})
        with patch.object(p, 'install_aplexer_state_hooks'), \
             patch.object(p, 'install_managed_engine_permissions'), \
             patch.object(p, 'load_git_identity', return_value={}):
            p.provision_managed_home(stripped, home=home, dest=dest)
        self.assertTrue(quse_auth.is_file())
        if os.name != 'nt':
            self.assertEqual(quse_auth.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(json.loads(quse_auth.read_text(encoding='utf-8')),
                         {'opencode-go': {'type': 'api', 'key': 'A'}})

    def test_actual_managed_go_offer_preserves_contract_and_passes_after_quse_auth(self):
        self.sync()
        subs = Path(self.tmp.name) / 'subs.json'
        subs.write_text(json.dumps({
            'version': 1,
            'workspaces': {'demo': self.tmp.name},
            'subscriptions': [{
                'service': 'go',
                'account_label': 'shared-go',
                'engine': 'opencode',
                'profile': 'hub-shared-go',
                'admission_argv': ['maintained-gate'],
                'launcher_provider': 'opencode',
                'launcher_model': 'opencode-go/muse-spark-1.3-contributor',
            }],
            'launcher': {
                'argv': ['python3', '-m', 'launcher'],
                'root': self.tmp.name,
                'config_dir': str(Path(self.tmp.name) / 'cfg'),
            },
        }))
        with patch.object(fleet, 'subscriptions_path', return_value=subs):
            cfg = fleet.load_settings(subs)
        self.assertIn('opencode_auth', cfg)

        actual_offer = {
            'id': '608c8515-2b46-47d6-898a-66ec16e2ab08',
            'account': 'shared-go',
            'engine': 'opencode',
            'profile': 'hub-shared-go',
            'mode': 'headless',
            'workspace': 'demo',
            'prompt': 'Reply with hostname',
            'selection': {
                'id': 'muse',
                'model': 'opencode-go/muse-spark-1.3-contributor',
                'provider': 'go',
                'engine': 'opencode',
                'profile': None,
                'effort': None,
            },
        }
        plan_has_creds = {'ok': False}
        submitted_payloads = []

        def runner(argv, **kw):
            if argv[0] == 'maintained-gate':
                return {
                    'launch_allowed': True,
                    'profile_supported': True,
                    'model_supported': True,
                    'account_label': 'shared-go',
                    'observed_at': fleet.utcnow(),
                }
            if 'submit' in argv:
                submitted_payloads.append(json.loads(argv[argv.index('--payload') + 1]))
                return {}
            if 'plan' in argv:
                if not plan_has_creds['ok']:
                    return {'eligible': [], 'rejections': {'go': 'Error: no-credentials'}}
                return {
                    'eligible': [{
                        'name': 'go',
                        'provider': 'opencode',
                        'model': 'opencode-go/muse-spark-1.3-contributor',
                        'account_profile': 'hub-shared-go',
                        'account_label': 'shared-go',
                        'managed_dir': self.tmp.name,
                        'health': 1.0,
                        'task_fit': 1.0,
                    }]
                }
            if 'run' in argv:
                return {
                    'backend': 'aplexer',
                    'task_id': 'hub-' + actual_offer['id'],
                    'receipt': {
                        'session_id': 'native-go-1',
                        'state': 'running',
                        'first_action_verified': True,
                        'engine': 'opencode',
                        'model': 'opencode-go/muse-spark-1.3-contributor',
                        'model_verified': True,
                        'profile': 'hub-shared-go',
                        'account_label': 'shared-go',
                    },
                }
            return {}

        dispatcher = fleet.Dispatcher(cfg, lambda *a, **kw: {}, runner, journal_dir=self.tmp.name)
        prepared_cfgs = []
        with patch('agent_hub_host.opencode_auth.prepare', side_effect=lambda c: prepared_cfgs.append(c) or {'ok': True}):
            with self.assertRaisesRegex(ValueError, 'Fresh admission refused'):
                dispatcher.dispatch(dict(actual_offer))
        self.assertEqual(prepared_cfgs, [cfg['opencode_auth']])
        self.assertEqual(len(submitted_payloads), 1)
        self.assertEqual(submitted_payloads[0]['model_requirements'], {
            'providers': ['opencode'],
            'models': ['opencode-go/muse-spark-1.3-contributor'],
        })
        self.assertEqual(submitted_payloads[0]['account_profile'], 'hub-shared-go')
        self.assertEqual(submitted_payloads[0]['account_label'], 'shared-go')
        self.assertEqual(submitted_payloads[0]['managed_dir'], self.tmp.name)

        plan_has_creds['ok'] = True
        with patch('agent_hub_host.opencode_auth.prepare', side_effect=lambda c: prepared_cfgs.append(c) or {'ok': True}):
            accepted = dispatcher.dispatch(dict(actual_offer, id='da1e2c52-880e-463c-8f03-7a5fc257774f'))
        self.assertEqual(accepted['state'], 'running')
        self.assertEqual(accepted['native_id'], 'native-go-1')


class ZcodeManagedConfig(unittest.TestCase):
    def bundle(self, goz='{}', env=None):
        return {'label': 'zai-central', 'provider': 'zai', 'revision': 'r1', 'args': [],
                'env': env or {}, 'files': {'.config/goz/config.json': goz}}

    def test_native_shaped_files_from_delivered_key(self):
        out = p.zcode_managed_configs(self.bundle(json.dumps({'zai_token': 'K', 'zai_base_url': 'https://api.z.ai/api/anthropic'})))
        self.assertEqual(sorted(out), [p.ZCODE_CLI_CONFIG, p.ZCODE_PROVIDER_CONFIG])
        cli = json.loads(out[p.ZCODE_CLI_CONFIG])
        self.assertEqual(cli['model'], 'zai/glm-5.3-flash')
        self.assertEqual(cli['provider']['zai-coding-plan']['options']['apiKey'], 'K')
        self.assertIn('GLM-5.3-Flash', cli['provider']['zai-coding-plan']['models'])
        prov = json.loads(out[p.ZCODE_PROVIDER_CONFIG])['config']
        self.assertEqual(prov['defaultModelSelection'], {'providerId': 'zai-coding-plan', 'modelId': 'GLM-5.3-Flash'})
        rule = prov['providerConfigRules']['providerRules'][0]['config']
        self.assertEqual(rule['access'], {'type': 'api-key', 'apiKey': 'K'})
        self.assertEqual(rule['api']['type'], 'anthropic-messages')
        self.assertEqual(json.loads(out[p.ZCODE_PROVIDER_CONFIG])['schemaVersion'], 1)
        self.assertIs(cli['provider']['zai-coding-plan']['options']['apiKeyRequired'], True)
        self.assertEqual(cli['provider']['zai-coding-plan']['models']['GLM-5.3-Flash']['limit']['context'], 1000000)

    def test_env_key_is_used_when_goz_has_none(self):
        out = p.zcode_managed_configs(self.bundle(env={'ZAI_API_KEY': 'E'}))
        self.assertEqual(json.loads(out[p.ZCODE_CLI_CONFIG])['provider']['zai-coding-plan']['options']['apiKey'], 'E')

    def test_no_key_or_other_provider_generates_nothing(self):
        self.assertEqual(p.zcode_managed_configs(self.bundle('{}')), {})
        self.assertEqual(p.zcode_managed_configs(self.bundle('not json')), {})
        self.assertEqual(p.zcode_managed_configs({'provider': 'codex', 'files': {}}), {})

    def test_managed_home_gets_both_files_and_keeps_engine_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / 'home'
            b = self.bundle(json.dumps({'zai_token': 'K'}))
            with patch.object(p, 'install_aplexer_state_hooks'), patch.object(p, 'install_managed_engine_permissions'), \
                    patch.object(p, 'load_git_identity', return_value={}):
                p.provision_managed_home(b, home=home)
                cli, prov = home / p.ZCODE_CLI_CONFIG, home / p.ZCODE_PROVIDER_CONFIG
                self.assertTrue(cli.is_file() and prov.is_file())
                cli.write_text(cli.read_text() + ' ')  # engine-owned edit that still carries the key
                edited = cli.read_text()
                p.provision_managed_home(b, home=home)
            self.assertEqual(cli.read_text(), edited)


class WindowsLaunchArgv(unittest.TestCase):
    def test_windows_launch_argv_has_no_usr_bin_env_and_applies_the_store(self):
        import subprocess
        from unittest import mock
        from agent_hub_host import opencode_auth
        base = Path(tempfile.mkdtemp())
        cfg = {'binary': str(base / 'oc'), 'source_data': str(base / 's'),
               'source_config': str(base / 'sc'), 'target_data': str(base / 't')}
        code = 'import os;print(os.environ["XDG_DATA_HOME"]);print("OPENCODE_SERVER_PASSWORD" in os.environ)'
        # Patching the real os.name to 'nt' on Linux breaks pathlib (cannot instantiate WindowsPath).
        # Only opencode_auth's own view of the platform needs to look like Windows.
        class WindowsOs:
            name = 'nt'
            def __getattr__(self, attr):
                return getattr(os, attr)
        with mock.patch.object(opencode_auth, 'os', WindowsOs()):
            argv = opencode_auth.launch_argv(cfg, [sys.executable, '-c', code])
        self.assertNotIn('/usr/bin/env', argv)
        out = subprocess.run(argv, capture_output=True, text=True, env={**os.environ, 'OPENCODE_SERVER_PASSWORD': 'x'})
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.split(), [cfg['target_data'], 'False'])


class ProxyEnvironment(unittest.TestCase):
    def test_user_proxy_never_reaches_the_opencode_store(self):
        from agent_hub_host import opencode_auth
        base = Path(tempfile.mkdtemp())
        cfg = {'binary': str(base / 'oc'), 'source_data': str(base / 's'),
               'source_config': str(base / 'sc'), 'target_data': str(base / 't')}
        dirty = {'HTTP_PROXY': 'http://127.0.0.1:8899', 'https_proxy': 'http://127.0.0.1:8899', 'NO_PROXY': 'x', 'PATH': os.environ.get('PATH', '')}
        for source in (False, True):
            env = opencode_auth.environment(cfg, source=source, base=dirty)
            self.assertFalse({k for k in env if k.lower().endswith('_proxy')})
            self.assertEqual(env['PATH'], dirty['PATH'])


if __name__ == "__main__":
    unittest.main()
