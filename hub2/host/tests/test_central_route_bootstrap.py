"""Central route defaults: actual enrollment/constructor/provision, no SDK/network."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from agent_hub_host import native_launch as nl, providers, windows_host

REPO = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location('central_enrollment_fixture', REPO / 'scripts/hub2-enroll.py')
enrollment = importlib.util.module_from_spec(spec)
spec.loader.exec_module(enrollment)


class CentralRouteBootstrapTests(unittest.TestCase):
    def enroll(self, root):
        saved = []
        class PairingClient:
            base_url = 'https://example.invalid'
            def request(self, method, path, body):
                if (method, path, body) != ('POST', '/api/enrollments/complete', {'code': 'synthetic-pair'}):
                    raise AssertionError('unexpected request')
                return {'machine_id': 'synthetic-windows', 'token': 'synthetic-not-auth'}
        enrollment.enroll(PairingClient(), lambda path, config: saved.append(config),
                          root / 'missing-config.json', [str(root)], ['go', 'zai'], 'synthetic-pair')
        self.assertEqual(len(saved), 1)
        return saved[0]

    def test_actual_fresh_enrollment_bootstraps_provider_only_central_routes(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self.enroll(Path(directory))
        self.assertTrue(config['launcher']['private_provider_homes'])
        self.assertNotIn('routes', config['launcher'])
        adapter = nl.NativeLauncher(config['launcher'])
        for provider in ('codex', 'claude', 'go', 'zai'):
            self.assertEqual(adapter.config['routes'][provider], {'provider': provider})
        self.assertNotIn('antigravity', adapter.config['routes'])
        self.assertEqual(config['launcher'], {'private_provider_homes': True})

    def test_mode_transition_defaults_preserve_explicit_overrides_and_local_profiles(self):
        local = {'machine_id': 'synthetic', 'engines': ['go', 'zai']}
        central = windows_host.set_credential_mode(local, 'central')
        adapter = nl.NativeLauncher(central['launcher'])
        self.assertEqual(adapter.config['routes']['go'], {'provider': 'go'})
        self.assertEqual(adapter.config['routes']['zai'], {'provider': 'zai'})
        explicit = {'provider': 'zai', 'profile': 'operator-owned-choice', 'custom': 'untouched'}
        custom = nl.NativeLauncher({'private_provider_homes': True,
                                   'routes': {'go': explicit, 'custom-engine': {'provider': 'go'}}})
        self.assertEqual(custom.config['routes']['go'], explicit)
        self.assertEqual(custom.config['routes']['custom-engine'], {'provider': 'go'})
        self.assertEqual(nl.NativeLauncher({}).config['routes']['go'], nl.LOCAL_ROUTES['go'])
        self.assertEqual(nl.NativeLauncher({}).config['routes']['codex'], nl.LOCAL_ROUTES['codex'])

    def test_explicit_route_maps_do_not_enable_or_dispatch_unlisted_providers(self):
        for routes in ({}, {'go': {'provider': 'go'}}):
            with self.subTest(routes=routes), tempfile.TemporaryDirectory() as directory:
                original = {engine: dict(route) for engine, route in routes.items()}
                runner = mock.Mock(side_effect=AssertionError('must not spawn'))
                adapter = nl.NativeLauncher({'private_provider_homes': True, 'routes': routes}, runner=runner)
                self.assertEqual(set(adapter.config['routes']), set(original) | {'shell'})
                with mock.patch.object(nl, 'codex_launch_wrapper', side_effect=AssertionError('must not inspect wrapper')) as wrapper:
                    with self.assertRaisesRegex(RuntimeError, 'native_launch_route_missing'):
                        adapter.launch('explicit-route-policy', {'engine': 'codex', 'workspace': directory,
                            'model': 'synthetic', 'prompt': 'p', 'interactive': False, 'timeout_seconds': 60})
                wrapper.assert_not_called()
                runner.assert_not_called()
                self.assertEqual(routes, original)
                self.assertEqual(list(Path(directory).iterdir()), [])

    def test_defaults_are_per_instance_and_do_not_create_unavailable_capabilities(self):
        adapter = nl.NativeLauncher({'private_provider_homes': True})
        adapter.config['routes']['go']['provider'] = 'synthetic-other'
        self.assertEqual(nl.NativeLauncher({'private_provider_homes': True}).config['routes']['go'], {'provider': 'go'})
        adapter = nl.NativeLauncher({'private_provider_homes': True})
        with mock.patch.object(nl.shutil, 'which', return_value=None), \
                mock.patch.object(nl.provider_home, 'claude_install_available', return_value=False), \
                mock.patch.object(nl, 'codex_launch_wrapper', side_effect=ValueError('unavailable')):
            self.assertEqual(adapter.capabilities(), {'shell': {'headless'}})

    def test_installed_enrolled_provider_intersection_and_windows_codex_guard(self):
        adapter = nl.NativeLauncher({'private_provider_homes': True})
        with mock.patch.object(nl.shutil, 'which', side_effect=lambda binary: '/synthetic/' + binary if binary in ('opencode', 'zcodex') else None), \
                mock.patch.object(nl.provider_home, 'claude_install_available', return_value=False), \
                mock.patch.object(nl.sys, 'platform', 'win32'):
            available = adapter.capabilities()
        self.assertEqual(set(available), {'shell', 'go', 'zai'})
        self.assertEqual({'go', 'claude'}.intersection(available), {'go'})
        self.assertNotIn('codex', available)

    def test_actual_provision_accepts_new_routes_and_retains_generation_ack(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.enroll(root)
            routes = nl.NativeLauncher(config['launcher']).config['routes']
            for provider in ('go', 'zai', 'codex', 'claude'):
                with self.subTest(provider=provider):
                    calls = []
                    class Client:
                        def request(self, method, path, body=None):
                            calls.append((method, path, body))
                            if path.endswith('/api/account'):
                                return {'account': {'provider': provider}}
                            if method == 'GET' and '/credentials?' in path:
                                return {'generation': 7, 'files': {}}
                            if method == 'POST' and path.endswith('/credentials'):
                                return {'acknowledged': True}
                            if path == '/api/engines/sync-report':
                                return {'status': 'recorded'}
                            raise AssertionError('unexpected request')
                    offer = {'account_id': 'picked-account', 'session_id': 'session', 'operation_id': 'op', 'epoch': 1}
                    launch = {'engine': provider, 'model': 'synthetic-model', 'timeout_seconds': 60}
                    manifest = {'synthetic': 'materialized'}
                    bundle = {'credential_generation': 7, 'files': {}, 'version': '2.1.322', 'revision': 'synthetic'}
                    with mock.patch.object(providers.provider_home, 'materialize', return_value=manifest) as materialize, \
                            mock.patch.object(providers, '_claude_sync_bundle', return_value=(bundle, {})), \
                            mock.patch.object(providers.provider_home, 'install_managed_claude', return_value='/synthetic/claude'), \
                            mock.patch.object(providers, '_prepare_claude'):
                        result = providers.provision(Client(), offer, launch, root, routes)
                    self.assertIs(result, manifest)
                    self.assertEqual(launch['account_id'], 'picked-account')
                    self.assertEqual(materialize.call_args.args[1:4], ('picked-account', provider, 7))
                    self.assertTrue(any(method == 'POST' for method, path, body in calls))

    def test_actual_provision_rejects_wrong_account_provider_before_credentials(self):
        routes = nl.NativeLauncher({'private_provider_homes': True}).config['routes']
        calls = []
        class Client:
            def request(self, method, path, body=None):
                calls.append(path)
                return {'account': {'provider': 'codex'}}
        with mock.patch.object(providers.provider_home, 'materialize', side_effect=AssertionError('must not materialize')):
            with self.assertRaisesRegex(RuntimeError, 'provider_route_mismatch'):
                providers.provision(Client(), {'account_id': 'picked'}, {'engine': 'go'}, '.', routes)
        self.assertEqual(calls, ['/api/accounts/picked/api/account'])


if __name__ == '__main__':
    unittest.main()
