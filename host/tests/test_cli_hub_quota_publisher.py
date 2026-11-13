"""Offline tests: real helper CLI, fake installed Hub and principal alert sink."""
import importlib.util
import fcntl
import http.server
import multiprocessing
import threading
import json
import os
from pathlib import Path
import stat
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'scripts/hub-quota-publish.py'
spec = importlib.util.spec_from_file_location('quota_publisher_helper', SCRIPT)
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


class PublisherTests(unittest.TestCase):
    def setUp(self):
        parent = ROOT / '.local/tmp'
        parent.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='quota-publisher-test-', dir=parent)
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.cfg = self.home / 'fixture.json'
        self.log = self.home / 'alerts.jsonl'
        self.argvlog = self.home / 'hub-argv.jsonl'
        self.state = self.home / 'state.json'
        self.hub = self.home / 'hub'
        self.sink = self.home / 'principal-alert.sh'
        self.config()
        self.hub.write_text('''#!/usr/bin/env python3
import json,sys,time
from pathlib import Path
cfg=json.loads(Path(%r).read_text())
with Path(%r).open('a') as f:f.write(json.dumps(sys.argv[1:])+'\\n')
assert sys.argv[1:]==['quota','publish']
time.sleep(cfg.get('sleep',0))
if cfg.get('failure'):
 print('SECRET provider/token/quse details',file=sys.stderr);sys.exit(1)
print(cfg.get('raw',json.dumps(cfg['receipt'])))
''' % (str(self.cfg), str(self.argvlog)))
        self.sink.write_text('''#!/usr/bin/env python3
import json,sys,os
from pathlib import Path
cfg=json.loads(Path(%r).read_text())
entry={'recipient':os.environ.get('PRINCIPAL_ALERT_TO'),'message':os.environ.get('PING_ALERT_MESSAGE'),
       'argv':sys.argv[1:],'alternate_to':os.environ.get('ALERT_TO'),'hub_token_forwarded':'HUB_TOKEN' in os.environ}
with Path(%r).open('a') as f:f.write(json.dumps(entry)+'\\n')
sys.exit(1 if cfg.get('sink_failure') else 0)
''' % (str(self.cfg), str(self.log)))
        self.hub.chmod(0o700); self.sink.chmod(0o700)

    def config(self, **kw):
        value = {'receipt': {'published': ['gemini', 'zai-central'],
                            'skipped': {a: 'no measured window' for a in helper.ACCOUNTS - {'gemini', 'zai-central'}}}}
        value.update(kw)
        self.cfg.write_text(json.dumps(value))

    def invoke(self, check=False, **env):
        args = [sys.executable, str(SCRIPT), '--state', str(self.state), '--hub', str(self.hub),
                '--alert-command', str(self.sink), '--accounts', 'gemini,zai-central']
        if check: args.append('--check')
        done = subprocess.run(args, capture_output=True, text=True, timeout=8,
                              env={**os.environ, 'HOME': str(self.home), **env})
        self.assertNotIn('SECRET', done.stdout + done.stderr)
        self.assertEqual(done.stderr, '', done.stderr)
        return done.returncode, json.loads(done.stdout)

    def read(self):
        return json.loads(self.state.read_text())

    def seed(self, **accounts):
        self.state.write_text(json.dumps({'version': 1, 'accounts': accounts, 'alerted': []}))
        self.state.chmod(0o600)

    def alerts(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def test_actual_publish_argv_and_gemini_zai_private_receipts(self):
        before = int(time.time() * 1000)
        rc, result = self.invoke()
        self.assertEqual(rc, 0, result)
        self.assertEqual(json.loads(self.argvlog.read_text()), ['quota', 'publish'])
        self.assertEqual(set(self.read()['accounts']), {'gemini', 'zai-central'})
        self.assertTrue(all(before <= at <= int(time.time() * 1000) for at in self.read()['accounts'].values()))
        self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o600)
        self.assertEqual(self.invoke(check=True)[0], 0)
        self.assertEqual(self.alerts(), [])

    def test_partial_success_preserves_skipped_times(self):
        old = int(time.time() * 1000) - 900000
        self.seed(gemini=old, **{'zai-central': old})
        self.config(receipt={'published': ['gemini'], 'skipped': {'zai-central': 'SECRET private provider error'}})
        self.assertEqual(self.invoke()[0], 0)
        self.assertEqual(self.read()['accounts']['zai-central'], old)
        self.assertGreater(self.read()['accounts']['gemini'], old)
        rc, result = self.invoke(check=True)
        self.assertEqual(rc, 2)
        self.assertEqual(result['accounts'], ['zai-central'])
        self.assertNotIn('SECRET', json.dumps(self.alerts()))

    def test_invalid_or_failed_result_cannot_refresh_state(self):
        old = int(time.time() * 1000) - 900000
        cases = [{'failure': True}, {'raw': 'not json'}, {'raw': '{"published":["unknown"],"published":["gemini"],"skipped":{}}'}, {'receipt': {'published': ['unknown'], 'skipped': {}}},
                 {'receipt': {'published': ['gemini', 'gemini'], 'skipped': {}}},
                 {'receipt': {'published': ['gemini'], 'skipped': {'gemini': 'conflict'}}},
                 {'receipt': {'published': [], 'skipped': {}}},
                 {'receipt': {'published': ['gemini'], 'skipped': {}, 'unexpected': 'SECRET'}}]
        for case in cases:
            with self.subTest(case=case):
                self.seed(gemini=old, **{'zai-central': old}); self.config(**case)
                self.assertEqual(self.invoke()[0], 1)
                self.assertEqual(self.read()['accounts'], {'gemini': old, 'zai-central': old})

    def test_monitor_detects_stopped_publisher_independently_and_dedupes(self):
        self.hub.unlink()  # monitor cannot depend on hub or publisher being runnable
        for _ in range(2):
            rc, result = self.invoke(check=True)
            self.assertEqual(rc, 2, result)
        self.assertEqual(len(self.alerts()), 1)
        self.assertEqual(self.read()['alerted'], ['gemini', 'zai-central'])
        self.assertFalse(self.argvlog.exists())

    def test_recovery_resets_incident_then_new_staleness_alerts(self):
        self.assertEqual(self.invoke(check=True)[0], 2)
        self.assertEqual(self.invoke()[0], 0)
        self.assertEqual(self.invoke(check=True)[0], 0)
        self.assertEqual(self.read()['alerted'], [])
        stale = int(time.time() * 1000) - 900000
        data = self.read(); data['accounts'] = dict(gemini=stale, **{'zai-central': stale})
        self.state.write_text(json.dumps(data))
        self.assertEqual(self.invoke(check=True)[0], 2)
        self.assertEqual(len(self.alerts()), 2)

    def test_sink_failure_never_marks_sent_and_recipient_is_fixed(self):
        self.config(sink_failure=True)
        self.assertEqual(self.invoke(check=True, PRINCIPAL_ALERT_TO='wrong@example.test', ALERT_TO='wrong@example.test',
                                     PING_ALERT_MESSAGE='SECRET', HUB_TOKEN='SECRET')[0], 3)
        self.assertEqual(self.read()['alerted'], [])
        self.config()
        self.assertEqual(self.invoke(check=True)[0], 2)
        self.assertEqual(len(self.alerts()), 2)
        for alert in self.alerts():
            self.assertEqual(alert['recipient'], 'dev@dtcdev.click')
            self.assertEqual(alert['alternate_to'], 'dev@dtcdev.click')
            self.assertEqual(alert['argv'], [alert['message']])
            self.assertIn('quota_stale', alert['message'])
            self.assertFalse(alert['hub_token_forwarded'])

    def test_corrupt_or_future_state_never_proves_fresh(self):
        for raw in ('{broken', json.dumps({'version': 1, 'accounts': {'gemini': int(time.time()*1000)+60000}, 'alerted': []}),
                    json.dumps({'version': 1, 'accounts': {}, 'alerted': [{}]})):
            with self.subTest(raw=raw):
                self.state.write_text(raw); self.state.chmod(0o600)
                self.assertEqual(self.invoke()[0], 1)
                self.assertEqual(self.state.read_text(), raw)
                rc, result = self.invoke(check=True)
                self.assertEqual(rc, 2, result)
                self.assertEqual(result['accounts'], ['gemini', 'zai-central'])
                self.assertEqual(self.read()['accounts'], {})

    def test_concurrent_partial_publishers_preserve_both_receipts(self):
        # Each subprocess has an immutable fake receipt, with the slower older
        # publication completing after the newer one.
        hubs = []
        for account, delay in [('gemini', 0.2), ('zai-central', 0)]:
            binary = self.home / ('hub-' + account)
            binary.write_text('#!/usr/bin/env python3\nimport json,time,sys\nassert sys.argv[1:]==["quota","publish"]\ntime.sleep(%r)\nprint(%r)\n' %
                              (delay, json.dumps({'published': [account], 'skipped': {}})))
            binary.chmod(0o700); hubs.append(binary)
        def run(binary):
            return subprocess.run([sys.executable, str(SCRIPT), '--state', str(self.state), '--hub', str(binary)],
                                  capture_output=True, text=True, timeout=5).returncode
        with ThreadPoolExecutor(2) as pool:
            self.assertEqual(list(pool.map(run, hubs)), [0, 0])
        self.assertEqual(set(self.read()['accounts']), {'gemini', 'zai-central'})
        self.assertEqual(self.invoke(check=True)[0], 0)

    def test_older_overlapping_receipt_never_overwrites_newer_same_account(self):
        marker = self.home / 'slow-entered'
        slow = self.home / 'slow-hub'
        fast = self.home / 'fast-hub'
        response = json.dumps({'published': ['gemini'], 'skipped': {}})
        slow.write_text('#!/usr/bin/env python3\nfrom pathlib import Path\nimport time\nPath(%r).write_text("entered")\ntime.sleep(0.3)\nprint(%r)\n' % (str(marker), response))
        fast.write_text('#!/usr/bin/env python3\nprint(%r)\n' % response)
        slow.chmod(0o700); fast.chmod(0o700)
        args = [sys.executable, str(SCRIPT), '--state', str(self.state), '--hub']
        child = subprocess.Popen(args + [str(slow)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 2
            while not marker.exists() and time.monotonic() < deadline: time.sleep(0.01)
            self.assertTrue(marker.exists())
            done = subprocess.run(args + [str(fast)], capture_output=True, text=True, timeout=3)
            self.assertEqual(done.returncode, 0, done.stderr)
            newer = self.read()['accounts']['gemini']
            stdout, stderr = child.communicate(timeout=3)
            self.assertEqual(child.returncode, 0, (stdout, stderr))
            self.assertEqual(self.read()['accounts']['gemini'], newer)
        finally:
            if child.poll() is None: child.kill(); child.communicate()

    def test_state_symlink_and_nonprivate_state_cannot_claim_freshness(self):
        outside = self.home / 'unrelated-data'
        outside.write_text('SECRET unrelated data')
        self.state.symlink_to(outside)
        self.assertEqual(self.invoke()[0], 1)
        self.assertEqual(self.invoke(check=True)[0], 2)
        self.assertEqual(outside.read_text(), 'SECRET unrelated data')
        self.assertFalse(self.state.is_symlink())
        self.assertEqual(self.read()['accounts'], {})
        self.seed(gemini=int(time.time()*1000), **{'zai-central': int(time.time()*1000)})
        self.state.chmod(0o644)
        self.assertEqual(self.invoke()[0], 1)
        self.assertEqual(self.invoke(check=True)[0], 2)
        self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o600)
        self.assertEqual(self.read()['accounts'], {})

    def test_successful_publication_recovers_incident_without_an_intervening_check(self):
        self.assertEqual(self.invoke(check=True)[0], 2)
        self.assertEqual(self.invoke()[0], 0)
        self.assertEqual(self.read()['alerted'], [])
        data = self.read(); data['accounts'] = {a: int(time.time()*1000)-900000 for a in data['accounts']}
        self.state.write_text(json.dumps(data))
        self.assertEqual(self.invoke(check=True)[0], 2)
        self.assertEqual(len(self.alerts()), 2)

    def test_monitor_does_not_wait_for_inflight_hub_process(self):
        self.config(sleep=2)
        process = subprocess.Popen([sys.executable, str(SCRIPT), '--state', str(self.state), '--hub', str(self.hub)],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 2
            while not self.argvlog.exists() and time.monotonic() < deadline: time.sleep(0.01)
            self.assertTrue(self.argvlog.exists())
            start = time.monotonic(); self.assertEqual(self.invoke(check=True)[0], 2)
            self.assertLess(time.monotonic() - start, 1)
            self.assertIsNone(process.poll())
            stdout, stderr = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 0, (stdout, stderr))
            self.assertEqual(self.invoke(check=True)[0], 0)
        finally:
            if process.poll() is None: process.kill(); process.communicate()

    def test_safe_slug_and_timeout(self):
        self.config(sleep=2)
        result = subprocess.run([sys.executable, str(SCRIPT), '--state', str(self.state), '--hub', str(self.hub),
                                 '--publish-timeout', '1'], capture_output=True, text=True, timeout=4)
        self.assertEqual(result.returncode, 1)
        self.assertNotIn('SECRET', result.stdout + result.stderr)
        self.assertFalse(self.state.exists())


class HealthTests(unittest.TestCase):
    setUp = PublisherTests.setUp
    config = PublisherTests.config
    alerts = PublisherTests.alerts
    seed = PublisherTests.seed

    def start_server(self):
        self.responses = {
            '/health': (200, b'{"ok":true}'),
            '/api/fleet/routing/self': (200, b'{"machine_id":"fixture-machine","connected":true}')}
        self.requests = []
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                owner.requests.append((self.path, self.headers.get('Authorization')))
                code, body = owner.responses[self.path]
                self.send_response(code)
                self.send_header('Content-Type', 'application/json')
                if code == 302:
                    self.send_header('Location', getattr(owner, 'redirect_to', owner.origin + '/api/fleet/routing/self'))
                self.end_headers()
                try:
                    if body == b'SLOW':
                        for _ in range(30):
                            self.wfile.write(b' '); self.wfile.flush(); time.sleep(0.1)
                    else:
                        self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.origin = 'http://127.0.0.1:' + str(server.server_port)
        self.host_config = self.home / 'host.json'
        self.health_state = self.home / 'health.json'
        self.host_value = {'server': self.origin, 'machine_id': 'fixture-machine', 'token': 'SECRET-fixture-machine-token'}
        self.save_host()
        self.seed(gemini=int(time.time()*1000), **{'zai-central': int(time.time()*1000)})

    def save_host(self):
        self.host_config.write_text(json.dumps(self.host_value))
        self.host_config.chmod(0o600)

    def invoke_health(self, *extra):
        done = subprocess.run([sys.executable, str(SCRIPT), '--check', '--health-check',
            '--health-config', str(self.host_config), '--health-state', str(self.health_state),
            '--health-offline-loopback', '--health-timeout', '1', '--alert-timeout', '1',
            '--state', str(self.state), '--accounts', 'gemini,zai-central', '--alert-command', str(self.sink), *extra],
            env={**os.environ, 'HOME': str(self.home), 'HTTPS_PROXY': 'http://127.0.0.1:1',
                 'HTTP_PROXY': 'http://127.0.0.1:1', 'ALL_PROXY': 'http://127.0.0.1:1', 'NO_PROXY': ''},
            capture_output=True, text=True, timeout=6)
        self.assertEqual(done.stderr, '', done.stderr)
        self.assertNotIn('SECRET', done.stdout + done.stderr + json.dumps(self.alerts()))
        self.assertNotIn(self.origin, done.stdout + json.dumps(self.alerts()))
        self.assertNotIn('SECRET', self.health_state.read_text() if self.health_state.exists() and not self.health_state.is_symlink() else '')
        return done.returncode, json.loads(done.stdout)

    def test_public_and_exact_own_host_and_no_proxy_or_public_token(self):
        self.start_server()
        rc, result = self.invoke_health()
        self.assertEqual(rc, 0, result)
        self.assertTrue(result['health']['healthy'])
        self.assertEqual(self.requests, [('/health', None),
            ('/api/fleet/routing/self', 'Bearer SECRET-fixture-machine-token')])
        self.assertEqual(stat.S_IMODE(self.health_state.stat().st_mode), 0o600)
        self.assertFalse(self.argvlog.exists())

    def test_unhealthy_public_json_and_http_never_reaches_authenticated_route(self):
        self.start_server()
        cases = [(200, b'{"status":"bootstrap","dispatch_enabled":false}', 'not_ok'), (200, b'{"ok":false}', 'not_ok'),
            (200, b'{"ok":true,"status":"bootstrap"}', 'not_ok'),
            (200, b'{"ok":1}', 'not_ok'), (200, b'{"ok":true,"ok":false}', 'response_invalid'),
            (200, b'not JSON SECRET', 'response_invalid'), (200, b'[]', 'response_invalid'),
            (200, b'x'*65537, 'response_invalid'), (503, b'{"ok":true}', 'http_rejected'),
            (302, b'', 'http_rejected')]
        for code, body, reason in cases:
            with self.subTest(code=code, reason=reason, size=len(body)):
                self.responses['/health'] = (code, body)
                self.requests.clear()
                rc, result = self.invoke_health()
                self.assertEqual(rc, 2, result)
                self.assertEqual(result['health']['reason'], reason)
                self.assertEqual(self.requests, [('/health', None)])

    def test_host_disconnected_identity_auth_and_nonliteral_boolean(self):
        self.start_server()
        for code, body, reason in [
            (200, b'{"machine_id":"fixture-machine","connected":false}', 'disconnected'),
            (200, b'{"machine_id":"fixture-machine","connected":1}', 'disconnected'),
            (200, b'{"machine_id":"other-machine","connected":true}', 'identity_mismatch'),
            (200, b'{"machines":[{"machine_id":"fixture-machine","connected":true}]}', 'identity_mismatch'),
            (401, b'SECRET rejected', 'http_rejected'), (403, b'SECRET rejected', 'http_rejected')]:
            with self.subTest(reason=reason, code=code):
                self.responses['/api/fleet/routing/self'] = code, body
                rc, result = self.invoke_health()
                self.assertEqual(rc, 2, result)
                self.assertEqual(result['health']['stage'], 'host')
                self.assertEqual(result['health']['reason'], reason)

    def test_authenticated_redirects_never_forward_token(self):
        self.start_server()
        self.responses['/api/fleet/routing/self'] = 302, b''
        for target in (self.origin + '/health', 'http://127.0.0.1:1/secret'):
            self.redirect_to = target
            self.requests.clear()
            result = self.invoke_health()[1]['health']
            self.assertEqual(result['reason'], 'http_rejected')
            self.assertEqual(result['stage'], 'host')
            self.assertEqual(len(self.requests), 2)

    def test_renderer_timeout_remains_retryable_within_budget(self):
        self.start_server()
        self.responses['/health'] = 200, b'{"ok":false}'
        self.invoke_health()
        self.sink.write_text('#!/usr/bin/env python3\nimport time\nprint("SECRET sink output",flush=True)\ntime.sleep(20)\n')
        start = time.monotonic()
        rc, result = self.invoke_health()
        self.assertLess(time.monotonic()-start, 2.5)
        self.assertEqual(rc, 3, result)
        self.assertTrue(result['health']['sink_failed'])
        self.assertFalse(json.loads(self.health_state.read_text())['alerted'])

    def test_held_or_symlink_health_lock_is_bounded_unknown(self):
        self.start_server()
        lock_path = Path(str(self.health_state) + '.lock')
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            start = time.monotonic()
            rc, result = self.invoke_health()
            self.assertLess(time.monotonic()-start, 2)
            self.assertEqual(rc, 2, result)
            self.assertEqual(result['health']['status'], 'health_unknown')
            self.assertFalse(self.health_state.exists())
            self.assertEqual(self.requests, [])
        finally:
            os.close(fd)
        lock_path.unlink()
        target = self.home / 'lock-target'; target.write_text('SECRET')
        lock_path.symlink_to(target)
        self.assertEqual(self.invoke_health()[1]['health']['status'], 'health_unknown')
        self.assertEqual(target.read_text(), 'SECRET')

    def test_slow_drip_total_wall_deadline(self):
        self.start_server()
        self.responses['/health'] = 200, b'SLOW'
        start = time.monotonic()
        rc, result = self.invoke_health()
        self.assertLess(time.monotonic() - start, 2.5)
        self.assertEqual(rc, 2, result)
        self.assertEqual(result['health']['reason'], 'deadline')
        self.assertEqual(self.requests, [('/health', None)])

    def test_slow_dns_owned_child_is_reaped(self):
        self.host_config = self.home / 'host.json'
        self.host_config.write_text(json.dumps({'server': 'https://offline.invalid',
            'machine_id': 'fixture-machine', 'token': 'SECRET-fixture-token'}))
        self.host_config.chmod(0o600)
        args = mock.Mock(health_config=self.host_config, health_offline_loopback=False, health_timeout=1)
        before = {child.pid for child in multiprocessing.active_children()}
        def hang(*args, **kwargs):
            time.sleep(20)
        with mock.patch.object(socket, 'getaddrinfo', side_effect=hang):
            start = time.monotonic()
            self.assertEqual(helper.sample_health(args)['reason'], 'deadline')
            self.assertLess(time.monotonic()-start, 2)
        self.assertEqual({child.pid for child in multiprocessing.active_children()}, before)

    def test_private_config_fences_and_expiration_no_network(self):
        self.start_server()
        for kind in ('missing', 'permissions', 'symlink', 'hardlink', 'oversize', 'fifo', 'wrong_uid', 'expired', 'no_machine', 'no_token'):
            with self.subTest(kind=kind):
                self.host_config.unlink(missing_ok=True)
                self.save_host()
                if kind == 'missing': self.host_config.unlink()
                elif kind == 'permissions': self.host_config.chmod(0o644)
                elif kind == 'symlink':
                    target = self.home / 'config-target'; target.write_text(self.host_config.read_text()); target.chmod(0o600)
                    self.host_config.unlink(); self.host_config.symlink_to(target)
                elif kind == 'hardlink': os.link(self.host_config, self.home / 'config-hardlink')
                elif kind == 'oversize': self.host_config.write_text('x'*65537)
                elif kind == 'fifo': self.host_config.unlink(); os.mkfifo(self.host_config, 0o600)
                elif kind == 'wrong_uid':
                    # fstat owner mismatch tested without requiring root/chown.
                    info = self.host_config.stat()
                    fake = mock.Mock(st_mode=info.st_mode, st_uid=os.getuid()+1, st_nlink=1, st_size=info.st_size)
                    with mock.patch.object(helper.os, 'fstat', return_value=fake):
                        with self.assertRaises(helper.Failure): helper.private_json(self.host_config)
                    continue
                else:
                    value = dict(self.host_value)
                    if kind == 'expired': value['expires_at'] = time.time()-10
                    else: value.pop('machine_id' if kind == 'no_machine' else 'token')
                    self.host_config.write_text(json.dumps(value))
                self.requests.clear()
                rc, result = self.invoke_health()
                self.assertEqual(rc, 2, result)
                self.assertEqual(result['health']['stage'], 'config')
                self.assertFalse(self.requests)

    def test_symlink_config_parent_and_origin_fences(self):
        self.start_server()
        link = self.home / 'linked'; link.symlink_to(self.home, target_is_directory=True)
        with self.assertRaises(OSError): helper.private_json(link / 'host.json')
        for raw in (self.origin, 'https://user:SECRET@example.test', 'https://example.test/path',
                    'https://example.test?x', 'https://example.test#x', 'https://example.test/%2f',
                    'https://example.test\\evil', 'https://example.test:99999', 'http://example.test',
                    'https://example.test\n', 'https://example.test:0', 'https://example.test:', 'https://example..test'):
            with self.subTest(raw=raw):
                with self.assertRaises(helper.Failure): helper.health_origin(raw, False)
        self.assertEqual(helper.health_origin(self.origin, True), self.origin)

    def test_two_samples_dedupe_recovery_new_incident_and_sink_retry(self):
        self.start_server()
        self.responses['/health'] = 200, b'{"ok":false}'
        self.assertEqual(self.invoke_health()[1]['health']['failures'], 1)
        self.assertEqual(self.alerts(), [])
        self.config(sink_failure=True)
        rc, result = self.invoke_health()
        self.assertEqual(rc, 3, result)
        self.assertFalse(json.loads(self.health_state.read_text())['alerted'])
        self.config()
        self.assertTrue(self.invoke_health()[1]['health']['alert_sent'])
        self.assertFalse(self.invoke_health()[1]['health']['alert_sent'])
        self.assertEqual(len(self.alerts()), 2)  # failed rendering + successful retry
        self.responses['/health'] = 200, b'{"ok":true}'
        self.assertEqual(self.invoke_health()[0], 0)
        self.assertEqual(json.loads(self.health_state.read_text())['failures'], 0)
        self.responses['/health'] = 200, b'{"ok":false}'
        self.invoke_health(); self.invoke_health()
        self.assertEqual(len(self.alerts()), 3)
        for entry in self.alerts():
            self.assertEqual(entry['recipient'], 'dev@dtcdev.click')
            self.assertEqual(entry['alternate_to'], 'dev@dtcdev.click')
            self.assertEqual(entry['argv'], [entry['message']])
            self.assertIn('health_critical', entry['message'])

    def test_quota_corruption_and_held_lock_cannot_block_health_threshold(self):
        self.start_server()
        self.responses['/health'] = 200, b'{"ok":false}'
        self.state.write_text('{corrupt SECRET')
        self.invoke_health()
        quota_before_lock = self.state.read_bytes()
        lock = os.open(str(self.state)+'.lock', os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)
            start = time.monotonic()
            result = self.invoke_health()[1]
            self.assertLess(time.monotonic()-start, 2.5)
            self.assertTrue(result['health']['alert_sent'])
            self.assertEqual(self.state.read_bytes(), quota_before_lock)
        finally:
            os.close(lock)
        self.assertEqual(len([a for a in self.alerts() if 'health_critical' in a['message']]), 1)

    def test_health_state_fences_unknown_and_regressed_sample_never_recover(self):
        self.start_server()
        target = self.home / 'unrelated'; target.write_text('SECRET')
        self.health_state.symlink_to(target)
        result = self.invoke_health()[1]['health']
        self.assertEqual(result['status'], 'health_unknown')
        self.assertFalse(result['healthy'])
        self.assertEqual(target.read_text(), 'SECRET')
        self.health_state.unlink()
        self.invoke_health()
        data = json.loads(self.health_state.read_text()); data['last_sample'] += 60000
        self.health_state.write_text(json.dumps(data))
        self.assertEqual(self.invoke_health()[1]['health']['status'], 'health_unknown')
        self.assertEqual(json.loads(self.health_state.read_text()), data)
        self.health_state.chmod(0o644)
        self.assertEqual(self.invoke_health()[1]['health']['status'], 'health_unknown')
        self.health_state.chmod(0o600)
        for raw in ('{broken', '['*2000 + ']'*2000):
            self.health_state.write_text(raw)
            self.assertEqual(self.invoke_health()[1]['health']['status'], 'health_unknown')

    def test_old_check_remains_network_free(self):
        self.start_server()
        self.assertEqual(PublisherTests.invoke(self, check=True)[0], 0)
        self.assertEqual(self.requests, [])
        self.assertFalse(self.health_state.exists())

    def test_health_state_ancestor_mkdir_and_fsync_fault_injection_retry(self):
        self.start_server()
        self.responses['/health'] = 200, b'{"ok":false}'
        nested_parent = self.home / 'nested'
        state_dir = nested_parent / 'state-dir'
        self.health_state = state_dir / 'health.json'
        args = helper.argparse.Namespace(
            health_config=self.host_config, health_state=self.health_state,
            health_timeout=1, health_offline_loopback=True,
            alert_command=self.sink, alert_timeout=1)

        real_mkdir = os.mkdir
        real_fsync = os.fsync
        fsynced_inos = []
        fail_mkdir = [True]
        fail_ino = [None]
        fail_after_alerts = [None]

        def wrapped_mkdir(path, mode=0o777, *, dir_fd=None):
            if fail_mkdir[0]:
                fail_mkdir[0] = False
                raise OSError('injected mkdir failure')
            return real_mkdir(path, mode, dir_fd=dir_fd)

        def wrapped_fsync(fd):
            ino = os.fstat(fd).st_ino
            fsynced_inos.append(ino)
            if fail_ino[0] is not None and ino == fail_ino[0]:
                if fail_after_alerts[0] is None or len(self.alerts()) >= fail_after_alerts[0]:
                    fail_ino[0] = None
                    raise OSError('injected parent fsync failure')
            return real_fsync(fd)

        with mock.patch.object(helper.os, 'mkdir', side_effect=wrapped_mkdir), \
             mock.patch.object(helper.os, 'fsync', side_effect=wrapped_fsync):
            # 1. First mkdir fails: unknown, no renderer, nothing created.
            res = helper.check_health(args)
            self.assertEqual(res['status'], 'health_unknown')
            self.assertFalse(res['alert_sent'])
            self.assertEqual(self.alerts(), [])
            self.assertFalse(nested_parent.exists())

            # 2. Retry mkdir succeeds, but parent barrier (self.home) fsync fails.
            home_ino = self.home.stat().st_ino
            fail_ino[0] = home_ino
            res = helper.check_health(args)
            self.assertEqual(res['status'], 'health_unknown')
            self.assertFalse(res['alert_sent'])
            self.assertEqual(self.alerts(), [])
            self.assertTrue(state_dir.is_dir())  # existing-dir retry must repair!

            # 3. Retry with existing state_dir repairs ancestor barriers and records failure 1.
            fsynced_inos.clear()
            time.sleep(0.002)
            res = helper.check_health(args)
            self.assertEqual(res['status'], 'health_failed')
            self.assertEqual(res['failures'], 1)
            self.assertFalse(res['alert_sent'])
            self.assertEqual(self.alerts(), [])
            nested_ino = nested_parent.stat().st_ino
            dir_ino = state_dir.stat().st_ino
            self.assertIn(dir_ino, fsynced_inos)
            self.assertIn(nested_ino, fsynced_inos)
            self.assertIn(home_ino, fsynced_inos)

            # 4. Second failure hits parent fsync failure before renderer: must NOT invoke renderer.
            fail_ino[0] = nested_ino
            time.sleep(0.002)
            res = helper.check_health(args)
            self.assertEqual(res['status'], 'health_unknown')
            self.assertFalse(res['alert_sent'])
            self.assertEqual(self.alerts(), [])

            # 5. Retry repairs barrier, retains counter=2, invokes renderer once, persists alerted=True.
            fsynced_inos.clear()
            time.sleep(0.002)
            res = helper.check_health(args)
            self.assertEqual(res['status'], 'health_failed')
            self.assertEqual(res['failures'], 2)
            self.assertTrue(res['alert_sent'])
            self.assertEqual(len(self.alerts()), 1)
            self.assertIn(nested_ino, fsynced_inos)
            self.assertIn(home_ino, fsynced_inos)
            self.assertTrue(json.loads(self.health_state.read_text())['alerted'])

            # 6. Subsequent failure dedupes with no duplicate alert.
            time.sleep(0.002)
            res = helper.check_health(args)
            self.assertEqual(res['status'], 'health_failed')
            self.assertEqual(res['failures'], 2)
            self.assertFalse(res['alert_sent'])
            self.assertEqual(len(self.alerts()), 1)

            # 7. Recovery, then post-renderer barrier failure must not claim alert_sent=True.
            self.responses['/health'] = 200, b'{"ok":true}'
            time.sleep(0.002)
            self.assertEqual(helper.check_health(args)['failures'], 0)
            self.responses['/health'] = 200, b'{"ok":false}'
            time.sleep(0.002)
            self.assertEqual(helper.check_health(args)['failures'], 1)
            fail_ino[0] = nested_ino
            fail_after_alerts[0] = 2
            time.sleep(0.002)
            res = helper.check_health(args)
            self.assertEqual(res['status'], 'health_unknown')
            self.assertFalse(res['alert_sent'])

    def test_health_state_symlink_ancestor_parent_fences(self):
        self.start_server()
        real_dir = self.home / 'real-dir'
        real_dir.mkdir(mode=0o700)
        link_parent = self.home / 'link-parent'
        link_parent.symlink_to(real_dir, target_is_directory=True)
        self.health_state = link_parent / 'sub' / 'health.json'
        res = self.invoke_health()[1]['health']
        self.assertEqual(res['status'], 'health_unknown')
        self.assertFalse(res['healthy'])
        self.assertEqual(self.alerts(), [])
        self.assertFalse((real_dir / 'sub').exists())


class ContractTests(unittest.TestCase):
    def test_config_bounds_and_known_default_accounts(self):
        self.assertTrue({'gemini', 'zai-central'}.issubset(helper.ACCOUNTS))
        for raw in ('', 'gemini,gemini', 'gemini,unknown', '../bad', 'Gemini'):
            with self.assertRaises(Exception): helper.account_set(raw)
        for value in ('relative', '/a\nsecret'):
            with self.assertRaises(Exception): helper.absolute_path(value)
        for stale in ('0', '901'):
            with mock.patch('sys.stderr'):
                with self.assertRaises(SystemExit): helper.main(['--stale-seconds', stale])

    def test_timers_independent_five_minute_cadence_and_versioned_path(self):
        for name in ('publish', 'monitor'):
            timer = (ROOT / ('deploy/agent-hub-quota-' + name + '.timer')).read_text()
            service = (ROOT / ('deploy/agent-hub-quota-' + name + '.service')).read_text()
            self.assertIn('OnCalendar=*-*-* *:0/5:00', timer)
            self.assertIn('AccuracySec=1s', timer)
            self.assertIn('RandomizedDelaySec=0', timer)
            self.assertIn('UMask=0077', service)
            self.assertIn('%h/.local/lib/agent-hub-quota-publisher/current/hub-quota-publish.py', service)
            self.assertNotIn('Requires=', service)
            self.assertNotIn('--enable', service)
            self.assertEqual('--check' in service, name == 'monitor')
        self.assertLessEqual(helper.DEFAULT_STALE_SECONDS + 300 + 1 + 30, 1200)
        monitor = (ROOT / 'deploy/agent-hub-quota-monitor.service').read_text()
        self.assertIn('--health-check', monitor)
        self.assertIn('--health-config %h/.config/agent-hub/v2.json', monitor)
        self.assertIn('--health-timeout 8 --alert-timeout 8', monitor)
        self.assertIn('TimeoutStartSec=45s', monitor)
        # Probe + two bounded state locks + at most one health and one quota
        # rendering leave 19s for interpreter and local durable state I/O.
        self.assertLess(8 + 2*1 + 2*8, 45)


if __name__ == '__main__':
    unittest.main()
