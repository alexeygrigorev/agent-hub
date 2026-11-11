"""Offline service bootstrap exchange tests; no production credentials or APIs."""
import contextlib
import copy
from http.server import BaseHTTPRequestHandler, HTTPServer
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('service_bootstrap', ROOT / 'scripts/prepare-model-policy-service-credential.py')
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)
SHA = 'a' * 40
NOW = 1800000000000
OPERATOR = 'synthetic-operator-' + 'x' * 40
OLD = 'synthetic-old-operator-' + 'z' * 40
SERVICE = 'b' * 64
ID = '9f8be3ac-8ad2-4639-8b25-941540f19860'


class ServiceBootstrapTests(unittest.TestCase):
    def setUp(self):
        temporary = ROOT / '.local/tmp'
        temporary.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=temporary, prefix='service-bootstrap-')
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.directory.chmod(0o700)
        self.fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, self.fd)
        owner = patch.object(helper, 'OWNER', os.getuid())
        owner.start(); self.addCleanup(owner.stop)
        self.bootstrap = {'schema': 'hub2-model-policy-bootstrap-v1',
                          'intent': 'exchange_model_policy_service_credential', 'request_id': ID,
                          'target_sha': SHA, 'access_token': OPERATOR, 'refreshed_at_ms': NOW - 1000,
                          'expires_at_ms': NOW - 1000 + 3600000, 'ttl_seconds': 3600}
        self.metadata = {'id': 'deployment:' + ID, 'request_id': ID, 'kind': 'service',
                         'purpose': 'model-policy', 'label': helper.LABEL, 'scopes': helper.SCOPES,
                         'expires_at': None, 'created_at': NOW, 'revoked': False}
        self.response = {'credential': self.metadata, 'access_token': SERVICE, 'token_type': 'Bearer'}
        self.calls = []
        self.write(helper.BOOTSTRAP, self.bootstrap)
        self.write(helper.OUTPUT, {'access_token': OLD})

    def write(self, name, value):
        path = self.directory / name
        path.write_bytes(helper.canonical(value)); path.chmod(0o600)

    def request(self, method, path, bearer=None, body=None):
        self.calls.append((method, path, bearer, copy.deepcopy(body)))
        if path == '/health': return 200, {'ok': True, 'service': 'hub2'}
        if path == '/api/deployment-credentials':
            # Issuance must never precede a durable, bound intent record.
            intent = helper.parse(helper.read_private(self.fd, helper.INTENT))
            self.assertEqual(intent['request_id'], ID)
            self.assertEqual(intent['target_sha'], SHA)
            self.assertEqual(intent['base_url'], helper.BASE_URL)
            self.assertEqual(intent['phase'], 'issuance_started')
            self.assertEqual(body, {'request_id': ID, 'label': helper.LABEL})
            self.assertEqual(bearer, OPERATOR)
            return 201, self.response
        self.assertEqual(path, '/api/model-policy')
        self.assertEqual(bearer, SERVICE)
        return 200, {'version': 1, 'candidates': []}

    def run_prepare(self, **kwargs):
        return helper.prepare(self.fd, SHA, request=self.request, now=NOW, **kwargs)

    def test_default_preflight_is_read_only_and_never_uses_old_operator(self):
        before = {path.name: path.read_bytes() for path in self.directory.iterdir()}
        self.assertEqual(self.run_prepare()['status'], 'bootstrap_fresh_metadata')
        self.assertEqual(self.calls, [])
        self.assertEqual({path.name: path.read_bytes() for path in self.directory.iterdir()}, before)

    def test_exchange_issues_once_scoped_service_and_preserves_bootstrap(self):
        bootstrap = (self.directory / helper.BOOTSTRAP).read_bytes()
        result = self.run_prepare(exchange=True, post_health=True)
        self.assertEqual(result, {'status': 'service_installed', 'id': 'deployment:' + ID})
        installed = helper.service(helper.read_private(self.fd, helper.OUTPUT))
        self.assertEqual(installed['credential'], self.metadata)
        self.assertEqual(installed['access_token'], SERVICE)
        self.assertEqual((self.directory / helper.BOOTSTRAP).read_bytes(), bootstrap)
        for path in self.directory.iterdir(): self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual([call[1] for call in self.calls], ['/health', '/api/deployment-credentials', '/api/model-policy'])
        self.calls.clear()
        (self.directory / helper.BOOTSTRAP).unlink()
        self.assertEqual(self.run_prepare(exchange=True, post_health=True)['status'], 'service_ready')
        self.assertEqual([call[1] for call in self.calls], ['/api/model-policy'])

    def test_existing_service_is_reused_for_later_transaction_without_bootstrap(self):
        marker = {'schema': helper.SERVICE_SCHEMA, 'target_sha': 'c' * 40,
                  'credential': self.metadata, 'access_token': SERVICE}
        self.write(helper.OUTPUT, marker); (self.directory / helper.BOOTSTRAP).unlink()
        self.assertEqual(self.run_prepare()['status'], 'service_shape_valid')
        self.assertEqual(self.calls, [])
        self.assertEqual(self.run_prepare(exchange=True, post_health=True)['status'], 'service_ready')
        self.assertEqual([call[1] for call in self.calls], ['/api/model-policy'])

    def test_expired_bootstrap_and_mismatched_transaction_refuse_without_requests(self):
        for changed in ({'expires_at_ms': NOW - 1}, {'target_sha': 'c' * 40}, {'ttl_seconds': 3601},
                        {'refreshed_at_ms': NOW + 5001}, {'request_id': str(uuid.uuid5(uuid.NAMESPACE_DNS, 'synthetic'))},
                        {'refresh_token': 'must-never-be-imported'}):
            with self.subTest(changed=changed):
                value = dict(self.bootstrap, **changed); self.write(helper.BOOTSTRAP, value)
                with self.assertRaises(helper.Problem): self.run_prepare(exchange=True, post_health=True)
                self.assertEqual(self.calls, [])
                self.assertFalse((self.directory / helper.INTENT).exists())

    def test_exchange_requires_explicit_post_health_attestation(self):
        with self.assertRaisesRegex(helper.Problem, 'post_health_attestation_required'):
            self.run_prepare(exchange=True)
        self.assertEqual(self.calls, [])

    def test_health_failure_preserves_old_file_and_prevents_issuance(self):
        before = (self.directory / helper.OUTPUT).read_bytes()
        def unhealthy(*args): return 503, {'error': 'unavailable'}
        with self.assertRaisesRegex(helper.Problem, 'health_unverified'):
            helper.prepare(self.fd, SHA, exchange=True, post_health=True, request=unhealthy, now=NOW)
        self.assertEqual((self.directory / helper.OUTPUT).read_bytes(), before)
        self.assertFalse((self.directory / helper.INTENT).exists())

    def test_lost_201_retains_known_id_and_never_reissues(self):
        before = (self.directory / helper.OUTPUT).read_bytes()
        def lost(method, path, bearer=None, body=None):
            if method == 'POST': raise subprocess.TimeoutExpired(['synthetic-public-url'], 10)
            return self.request(method, path, bearer, body)
        with self.assertRaises(helper.Problem) as error:
            helper.prepare(self.fd, SHA, exchange=True, post_health=True, request=lost, now=NOW)
        self.assertEqual(error.exception.request_id, ID)
        self.assertEqual(error.exception.code, 'issuance_recovery_required')
        self.assertEqual((self.directory / helper.OUTPUT).read_bytes(), before)
        self.calls.clear()
        with self.assertRaisesRegex(helper.Problem, 'issuance_recovery_required'): self.run_prepare(exchange=True, post_health=True)
        self.assertEqual(self.calls, [])

    def test_401_issuance_retains_intent_and_never_installs_operator_token(self):
        before = (self.directory / helper.OUTPUT).read_bytes()
        def refused(method, path, bearer=None, body=None):
            if method == 'POST': return 401, {'error': 'unauthorized', 'detail': 'private-provider-detail'}
            return self.request(method, path, bearer, body)
        with self.assertRaises(helper.Problem) as error:
            helper.prepare(self.fd, SHA, exchange=True, post_health=True, request=refused, now=NOW)
        self.assertEqual(error.exception.request_id, ID)
        self.assertEqual((self.directory / helper.OUTPUT).read_bytes(), before)
        self.assertTrue((self.directory / helper.INTENT).exists())

    def test_malformed_201_and_candidate_mismatch_never_replace_old_output(self):
        before = (self.directory / helper.OUTPUT).read_bytes()
        mutations = [lambda v: v.update(refresh_token='forbidden'),
                     lambda v: v['credential'].update(request_id=str(uuid.uuid4())),
                     lambda v: v['credential'].update(scopes=helper.SCOPES + ['operator:admin']),
                     lambda v: v['credential'].update(kind='operator'),
                     lambda v: v.update(token_type='other'),
                     lambda v: v.update(access_token=OPERATOR)]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                self.response = copy.deepcopy({'credential': self.metadata, 'access_token': SERVICE, 'token_type': 'Bearer'})
                mutate(self.response)
                with self.assertRaisesRegex(helper.Problem, 'issuance_recovery_required'):
                    self.run_prepare(exchange=True, post_health=True)
                self.assertEqual((self.directory / helper.OUTPUT).read_bytes(), before)
                self.assertFalse(list(self.directory.glob('*.received.json')))
                (self.directory / helper.INTENT).unlink()  # synthetic case isolation only

    def test_interrupted_output_publication_retains_received_candidate_and_old_output(self):
        before = (self.directory / helper.OUTPUT).read_bytes()
        original = helper.publish
        def interrupted(fd, name, raw, expected=None):
            if name == helper.OUTPUT: raise OSError('synthetic interruption')
            return original(fd, name, raw, expected)
        with patch.object(helper, 'publish', side_effect=interrupted):
            with self.assertRaisesRegex(helper.Problem, 'issuance_recovery_required'):
                self.run_prepare(exchange=True, post_health=True)
        self.assertEqual((self.directory / helper.OUTPUT).read_bytes(), before)
        self.assertEqual(len(list(self.directory.glob('*.received.json'))), 1)
        self.assertTrue((self.directory / helper.INTENT).exists())
        self.calls.clear()
        with self.assertRaisesRegex(helper.Problem, 'issuance_recovery_required'): self.run_prepare(exchange=True, post_health=True)
        self.assertEqual(self.calls, [])

    def test_operator_output_change_after_issuance_is_never_overwritten(self):
        original = self.request
        def concurrent(method, path, bearer=None, body=None):
            if path == '/api/model-policy': self.write(helper.OUTPUT, {'access_token': 'future-operator-' + 'q' * 40})
            return original(method, path, bearer, body)
        with self.assertRaisesRegex(helper.Problem, 'issuance_recovery_required'):
            helper.prepare(self.fd, SHA, exchange=True, post_health=True, request=concurrent, now=NOW)
        self.assertNotEqual(helper.parse(helper.read_private(self.fd, helper.OUTPUT))['access_token'], SERVICE)

    def test_existing_service_unverifiable_never_falls_back_to_bootstrap(self):
        self.write(helper.OUTPUT, {'schema': helper.SERVICE_SCHEMA, 'target_sha': SHA,
                                  'credential': self.metadata, 'access_token': SERVICE})
        def refused(method, path, bearer=None, body=None):
            self.calls.append(path); return 401, {'error': 'unauthorized'}
        with self.assertRaisesRegex(helper.Problem, 'policy_read_unverified'):
            helper.prepare(self.fd, SHA, exchange=True, post_health=True, request=refused, now=NOW)
        self.assertEqual(self.calls, ['/api/model-policy'])
        self.assertFalse((self.directory / helper.INTENT).exists())

    def test_policy_verification_requires_actual_versioned_shape(self):
        for status, body in ((200, {'ok': True}), (404, {'error': 'model_policy_unconfigured'}),
                             (200, {'version': True, 'candidates': []})):
            with self.subTest(status=status, body=body), self.assertRaises(helper.Problem):
                helper.verify_policy(lambda *args: (status, body), {'access_token': SERVICE})
        helper.verify_policy(lambda *args: (404, {'error': 'model_policy_unconfigured', 'version': 0}), {'access_token': SERVICE})

    def test_private_file_symlink_hardlink_owner_and_mode_refused(self):
        path = self.directory / helper.BOOTSTRAP
        path.chmod(0o644)
        with self.assertRaises(helper.Problem): helper.read_private(self.fd, helper.BOOTSTRAP)
        path.chmod(0o600)
        os.link(path, self.directory / 'alias')
        with self.assertRaises(helper.Problem): helper.read_private(self.fd, helper.BOOTSTRAP)
        (self.directory / 'alias').unlink()
        with patch.object(helper, 'OWNER', os.getuid() + 1):
            with self.assertRaises(helper.Problem): helper.read_private(self.fd, helper.BOOTSTRAP)
        path.unlink(); path.symlink_to(self.directory / helper.OUTPUT)
        with self.assertRaises(OSError): helper.read_private(self.fd, helper.BOOTSTRAP)

    def test_private_publication_failure_before_link_preserves_destination(self):
        before = (self.directory / helper.OUTPUT).read_bytes()
        with patch.object(os, 'fsync', side_effect=OSError('synthetic fsync failure')):
            with self.assertRaises(OSError): helper.publish(self.fd, 'new-file.json', b'{}')
        self.assertFalse((self.directory / 'new-file.json').exists())
        self.assertFalse(list(self.directory.glob('.policy-service-*')))
        self.assertEqual((self.directory / helper.OUTPUT).read_bytes(), before)

    def test_http_transport_is_bounded_isolated_and_secret_free_in_argv(self):
        reply = helper.canonical({'status': 201, 'body': self.response})
        with patch('subprocess.run', return_value=subprocess.CompletedProcess([], 0, reply)) as run:
            helper.http('POST', '/api/deployment-credentials', OPERATOR, {'request_id': ID, 'label': helper.LABEL})
        argv = run.call_args.args[0]; options = run.call_args.kwargs
        self.assertEqual(argv[:4], [sys.executable, '-I', '-S', '-c'])
        self.assertNotIn(OPERATOR, str(argv)); self.assertNotIn(SERVICE, str(argv))
        self.assertEqual(options['env'], {}); self.assertEqual(options['timeout'], 10)
        self.assertEqual(options['stderr'], subprocess.DEVNULL)
        self.assertEqual(helper.parse(options['input'])['token'], OPERATOR)
        for base in ('https://hub.example', 'http://127.0.0.1@other.example', 'http://localhost:8787'):
            with self.assertRaises(helper.Problem): helper.http('GET', '/health', base=base)

    @contextlib.contextmanager
    def server(self, response_status, response_body, location=None):
        observed = []
        raw = json.dumps(response_body, ensure_ascii=False, separators=(',', ':')).encode('utf-8')

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self):
                observed.append(self.path)
                self.send_response(response_status)
                self.send_header('Content-Length', str(len(raw)))
                if location is not None: self.send_header('Location', location)
                self.end_headers()
                try: self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError): pass

        server = HTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        try:
            thread.start()
            yield 'http://127.0.0.1:%d' % server.server_port, observed
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)
            self.assertFalse(thread.is_alive())

    def test_real_loopback_transport_ignores_proxy_and_never_follows_redirect(self):
        with self.server(200, {'ok': True}) as (trap, visits), \
             self.server(302, {'error': 'redirect'}, location=trap + '/stolen') as (base, actual), \
             patch.dict(os.environ, {'HTTP_PROXY': trap, 'http_proxy': trap, 'NO_PROXY': '', 'no_proxy': ''}):
            status, body = helper.http('GET', '/health', base=base)
        self.assertEqual(status, 302)
        self.assertEqual(body, {'error': 'redirect'})
        self.assertEqual(actual, ['/health'])
        self.assertEqual(visits, [])

    def test_real_loopback_transport_bounds_response_body(self):
        with self.server(200, {'padding': 'x' * helper.HTTP_LIMIT}) as (base, observed):
            with self.assertRaisesRegex(helper.Problem, 'http_outcome_unknown'):
                helper.http('GET', '/health', base=base)
        self.assertEqual(observed, ['/health'])

    def test_large_utf8_policy_is_accepted_and_existing_service_never_issues(self):
        policy = {'version': 4, 'candidates': [
            {'id': f'sol-{i}', 'engine': 'codex', 'model': 'gpt-6.1-sol', 'effort': 'medium',
             'account_id': 'codex', 'weight': 1, 'enabled': True,
             'capabilities': ['🌐' * 19 + f'{j:02}' for j in range(32)]}
            for i in range(32)]}
        raw = json.dumps(policy, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        self.assertGreater(len(raw), 65536)
        self.assertLess(len(raw), 98304)
        self.assertGreater(len(helper.canonical(policy)), 128000)
        self.write(helper.OUTPUT, {'schema': helper.SERVICE_SCHEMA, 'target_sha': SHA,
                                  'credential': self.metadata, 'access_token': SERVICE})
        (self.directory / helper.BOOTSTRAP).unlink()
        real_run = subprocess.run
        lengths = []
        def capture_transport(*args, **kwargs):
            result = real_run(*args, **kwargs)
            lengths.append(len(result.stdout))
            return result
        with self.server(200, policy) as (base, observed), patch('subprocess.run', side_effect=capture_transport):
            status, body = helper.http('GET', '/api/model-policy', SERVICE, base=base)
            self.assertEqual(status, 200); self.assertEqual(body, policy)
            result = helper.run(SHA, directory=self.directory, owner_uid=os.getuid(),
                                base_url=base, action='exchange', post_health=True)
        self.assertEqual(result['status'], 'service_ready')
        self.assertEqual(observed, ['/api/model-policy', '/api/model-policy'])
        self.assertTrue(all(size <= len(raw) + 128 for size in lengths), 'probe stdout expanded Unicode into ASCII escapes')
        self.assertFalse((self.directory / helper.INTENT).exists())
        self.assertFalse((self.directory / helper.BOOTSTRAP).exists())

    def test_http_body_limit_does_not_expand_private_file_limit(self):
        self.assertEqual(helper.LIMIT, 65536)
        path = self.directory / helper.BOOTSTRAP
        path.write_bytes(b' ' * 65537)
        with self.assertRaisesRegex(helper.Problem, 'private_file_untrusted'):
            helper.read_private(self.fd, helper.BOOTSTRAP)

    def test_fixed_production_directory_walk_rejects_untrusted_ancestor(self):
        synthetic = self.directory / 'synthetic-root'
        expected = synthetic / 'etc/agent-hub/deploy-credentials'
        expected.mkdir(parents=True)
        for parent in (synthetic, synthetic / 'etc', expected.parent): parent.chmod(0o755)
        expected.chmod(0o700)
        real_open = os.open
        opened = []
        def confined(path, flags, *args, **kwargs):
            opened.append(path)
            self.assertTrue(flags & os.O_NOFOLLOW)
            return real_open(synthetic if path == '/' else path, flags, *args, **kwargs)
        with patch.object(os, 'open', side_effect=confined), patch.object(os, 'geteuid', return_value=os.getuid()):
            fd = helper.open_directory()
            os.close(fd)
            self.assertEqual(opened, ['/', 'etc', 'agent-hub', 'deploy-credentials'])
            (synthetic / 'etc').chmod(0o775)
            with self.assertRaisesRegex(helper.Problem, 'directory_untrusted'): helper.open_directory()

    def test_new_output_is_created_without_overwriting_future_operator_file(self):
        (self.directory / helper.OUTPUT).unlink()
        def future(method, path, bearer=None, body=None):
            if path == '/api/model-policy': self.write(helper.OUTPUT, {'access_token': 'future-operator-' + 'q' * 40})
            return self.request(method, path, bearer, body)
        with self.assertRaisesRegex(helper.Problem, 'issuance_recovery_required'):
            helper.prepare(self.fd, SHA, exchange=True, post_health=True, request=future, now=NOW)
        self.assertEqual(helper.parse(helper.read_private(self.fd, helper.OUTPUT))['access_token'], 'future-operator-' + 'q' * 40)

    def test_intent_contains_no_credentials_and_blocks_a_different_transaction(self):
        with patch.object(helper, 'verify_policy', side_effect=OSError('synthetic interruption')):
            with self.assertRaises(helper.Problem): self.run_prepare(exchange=True, post_health=True)
        raw = helper.read_private(self.fd, helper.INTENT)
        self.assertNotIn(OPERATOR.encode(), raw); self.assertNotIn(SERVICE.encode(), raw); self.assertNotIn(OLD.encode(), raw)
        self.calls.clear()
        with self.assertRaises(helper.Problem) as error:
            helper.prepare(self.fd, 'c' * 40, exchange=True, post_health=True, request=self.request, now=NOW)
        self.assertEqual(error.exception.request_id, ID)
        self.assertEqual(self.calls, [])

    def test_exported_core_accepts_owned_test_root_without_production_cli_override(self):
        result = helper.run(SHA, directory=self.directory, owner_uid=os.getuid(),
                            request=self.request, now=NOW)
        self.assertEqual(result['status'], 'bootstrap_fresh_metadata')
        self.assertEqual(result['remaining_ms'], 3599000)
        self.assertEqual(self.calls, [])
        result = helper.run(SHA, directory=self.directory, owner_uid=os.getuid(),
                            action='exchange', post_health=True, request=self.request, now=NOW)
        self.assertEqual(result['status'], 'service_installed')

    def test_main_emits_only_safe_recovery_id(self):
        with patch.object(helper, 'open_directory', return_value=os.dup(self.fd)), \
             patch.object(helper, 'prepare', side_effect=helper.Problem('issuance_recovery_required', ID)), \
             patch('sys.stdout', new_callable=io.StringIO) as output:
            self.assertEqual(helper.main(['--target-sha', SHA]), 1)
        self.assertEqual(json.loads(output.getvalue()), {'error': 'issuance_recovery_required', 'id': 'deployment:' + ID})
        self.assertNotIn(OPERATOR, output.getvalue()); self.assertNotIn(SERVICE, output.getvalue())

    def test_post_health_rechecks_remaining_bootstrap_lifetime(self):
        self.bootstrap.update(refreshed_at_ms=NOW-3560000, expires_at_ms=NOW+40000)
        self.write(helper.BOOTSTRAP, self.bootstrap)
        with patch.object(helper.time, 'time', side_effect=[NOW/1000, (NOW+15000)/1000]):
            with self.assertRaisesRegex(helper.Problem, 'bootstrap_expired_or_invalid'):
                helper.prepare(self.fd, SHA, exchange=True, post_health=True, request=self.request)
        self.assertFalse((self.directory / helper.INTENT).exists())
        self.assertEqual([call[1] for call in self.calls], ['/health'])


if __name__ == '__main__':
    unittest.main()
