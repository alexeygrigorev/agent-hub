import contextlib
import copy
import fcntl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('picker_alert_delivery', REPO / 'scripts/hub-picker-alerts.py')
consumer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(consumer)


def offer():
    return {'occurrence': 1, 'recipient': consumer.RECIPIENT, 'lease_token': 'a' * 64,
            'lease_until': int(time.time() * 1000) + 600_000, 'attempt': 1,
            'payload': {'to': consumer.RECIPIENT, 'subject': 'Hub degraded picks', 'occurrence': 1,
                        'receipt_seq': 3, 'project_id': 'DataTalksClub/agent-hub', 'operation_id': 'start:abc',
                        'attempt': 0, 'picker': {'pool': [{'model': 'sol', 'engine': 'codex', 'effort': 'medium'}]},
                        'credential': 'private-body-marker'}}


class StubTransport:
    base = 'https://hub.example'

    def __init__(self):
        self.offers = [offer()]
        self.calls = []
        self.fail_ack = False
        self.counts = {'recipient': consumer.RECIPIENT, 'total_picks': 3, 'degraded_picks': 3,
                       'consecutive_degraded': 3, 'occurrence_watermark': 1, 'receipt_watermark': 3,
                       'coverage': {'available': True, 'epoch': 'epoch-1', 'since': 1}}

    def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        if path.endswith('/offers'):
            return {'offers': copy.deepcopy(self.offers)}
        if path.endswith('/counters'):
            return self.counts
        if path.endswith('/ack') and self.fail_ack:
            raise OSError('synthetic credential must stay private')
        return {'ok': True}


class PickerDeliveryTests(unittest.TestCase):
    def setUp(self):
        (REPO / '.local/tmp').mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix='picker-delivery-', dir=REPO / '.local/tmp')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state = self.root / 'state/receipts.json'
        self.transport = StubTransport()
        self.log = self.root / 'helper.jsonl'
        self.helper = self.root / 'fake-helper'
        self.helper.write_text(f'''#!{sys.executable}
import json, os
with open({str(self.log)!r}, 'a') as stream:
    stream.write(json.dumps({{"to": os.environ['PRINCIPAL_ALERT_TO'], "body": os.environ['PING_ALERT_MESSAGE'],
                             "args": __import__('sys').argv[1:], "bypass": os.environ.get('ALERT_LOG')}}) + '\\n')
raise SystemExit(int(os.environ.get('TEST_HELPER_RC', '0')))
''')
        self.helper.chmod(0o700)

    def run_delivery(self):
        return consumer.deliver(self.transport, self.state, self.helper)

    def test_success_saves_private_receipt_before_ack_and_lost_ack_replays_without_send(self):
        self.transport.fail_ack = True
        with patch.dict(os.environ, {'ALERT_LOG': 'bypass-must-not-propagate'}):
            first = self.run_delivery()
        self.assertEqual(first, {'saved': 1, 'acknowledged': 0, 'errors': 1})
        self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.state.parent.stat().st_mode), 0o700)
        entry = json.loads(self.log.read_text().strip())
        self.assertEqual(entry['to'], consumer.RECIPIENT)
        self.assertEqual(entry['args'], [])
        self.assertIsNone(entry['bypass'])
        self.assertIn('DataTalksClub/agent-hub', entry['body'])
        self.assertNotIn('private-body-marker', entry['body'])
        self.assertNotIn('a' * 64, self.state.read_text())
        self.transport.fail_ack = False
        second = self.run_delivery()
        self.assertEqual(second, {'saved': 0, 'acknowledged': 1, 'errors': 0})
        self.assertEqual(len(self.log.read_text().splitlines()), 1)

    def test_helper_failure_nacks_without_receipt_or_ack_then_retries(self):
        with patch.dict(os.environ, {'TEST_HELPER_RC': '1'}):
            self.assertEqual(self.run_delivery()['errors'], 1)
        self.assertFalse(self.state.exists())
        self.assertFalse(any(path.endswith('/ack') for _, path, _ in self.transport.calls))
        self.assertTrue(self.transport.calls[-1][1].endswith('/nack'))
        self.assertEqual(self.run_delivery()['acknowledged'], 1)

    def test_malformed_recipient_and_conflicting_occurrence_never_invoke_helper(self):
        self.transport.offers[0]['recipient'] = 'another@example.com'
        self.assertEqual(self.run_delivery()['errors'], 1)
        self.assertFalse(self.log.exists())
        self.transport.offers = [offer()]
        self.run_delivery()
        self.transport.offers[0]['payload']['project_id'] = 'another/repo'
        self.assertEqual(self.run_delivery()['errors'], 1)
        self.assertEqual(len(self.log.read_text().splitlines()), 1)

    def test_malformed_response_or_expired_lease_never_sends(self):
        with patch.object(self.transport, 'request', return_value=[]):
            with self.assertRaisesRegex(ValueError, 'offers'):
                self.run_delivery()
        self.transport.offers[0]['lease_until'] = 1
        self.assertEqual(self.run_delivery()['errors'], 1)
        self.assertFalse(self.log.exists())

    def test_private_state_lock_origin_and_file_permissions_are_enforced(self):
        self.run_delivery()
        lock_path = self.state.with_name(self.state.name + '.lock')
        with lock_path.open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(BlockingIOError):
                self.run_delivery()
        self.transport.base = 'https://another.example'
        with self.assertRaisesRegex(ValueError, 'journal'):
            self.run_delivery()
        self.transport.base = 'https://hub.example'
        self.state.chmod(0o644)
        with self.assertRaisesRegex(ValueError, 'private'):
            self.run_delivery()

    def test_watermark_is_collector_specific_and_rejects_unverified_counter_values(self):
        value = consumer.watermark(self.transport)
        self.assertEqual(value['kind'], 'hub_picker_degraded')
        self.assertEqual(value['occurrence_watermark'], 1)
        self.assertEqual(value['coverage'], {'available': True, 'epoch': 'epoch-1', 'since': 1})
        self.assertEqual(self.transport.calls, [('GET', '/api/picker-alerts/counters', None)])
        for changes in ({'recipient': 'other@example.com'}, {'total_picks': True}, {'occurrence_watermark': 2}):
            with self.subTest(changes=changes), patch.dict(self.transport.counts, changes):
                with self.assertRaises(ValueError):
                    consumer.watermark(self.transport)

    def test_watermark_requires_verified_coverage_interval(self):
        for coverage in (None, {'available': False}, {'available': True, 'epoch': '', 'since': 1},
                         {'available': True, 'epoch': 'epoch-1', 'since': True}):
            with self.subTest(coverage=coverage), patch.dict(self.transport.counts, {'coverage': coverage}):
                with self.assertRaises(ValueError):
                    consumer.watermark(self.transport)

    def test_cli_failure_omits_credentials_and_keeps_authentication_out_of_arguments(self):
        config = self.root / 'cli.json'
        config.write_text(json.dumps({'access_token': 'synthetic-secret'}))
        config.chmod(0o600)
        output = io.StringIO()
        with patch.object(consumer.Transport, 'request', side_effect=OSError('synthetic-secret')), contextlib.redirect_stderr(output):
            self.assertEqual(consumer.main(['--cli-json', str(config), '--watermark']), 1)
        self.assertNotIn('synthetic-secret', output.getvalue())
        config.chmod(0o644)
        with self.assertRaises(ValueError):
            consumer.private_read(config, 1000)

    def test_transport_uses_bearer_and_does_not_follow_redirects(self):
        seen = []
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append((self.path, self.headers.get('Authorization')))
                self.send_response(302)
                self.send_header('Location', '/private-redirect')
                self.end_headers()
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            transport = consumer.Transport(f'http://127.0.0.1:{server.server_port}', 'synthetic-secret')
            with self.assertRaises(OSError):
                transport.request('GET', '/api/picker-alerts/offers')
            self.assertEqual(seen, [('/api/picker-alerts/offers', 'Bearer synthetic-secret')])
            for url in ('http://remote.example', 'https://user:password@hub.example', 'https://hub.example?token=secret'):
                with self.assertRaises(ValueError):
                    consumer.Transport(url, 'synthetic-secret')
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == '__main__':
    unittest.main()
