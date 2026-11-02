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
        self.assertTrue(hub_default and hub_default['source_data'].startswith(self.tmp.name))
        explicit = {'binary': '/x/opencode', 'source_data': '/x/src', 'source_config': '/x/cfg', 'target_data': '/x/rt'}
        subs.write_text(json.dumps({'version': 1, 'subscriptions': [], 'workspaces': {'home': self.tmp.name},
                                    'opencode_auth': explicit}))
        with patch.object(fleet, 'subscriptions_path', return_value=subs):
            self.assertEqual(fleet.load_settings().get('opencode_auth'), explicit)


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
        with mock.patch.object(opencode_auth.os, 'name', 'nt'):
            argv = opencode_auth.launch_argv(cfg, [sys.executable, '-c', code])
        self.assertNotIn('/usr/bin/env', argv)
        out = subprocess.run(argv, capture_output=True, text=True, env={**os.environ, 'OPENCODE_SERVER_PASSWORD': 'x'})
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.split(), [cfg['target_data'], 'False'])


if __name__ == "__main__":
    unittest.main()
