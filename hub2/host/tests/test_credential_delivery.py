"""Background hub-claude generation delivery: exact ACK, single-inflight, bounded, secret-free."""
import io
import json
import logging
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent_hub_host import credential_delivery as delivery
from agent_hub_host.agent import Journal
from agent_hub_host.client import ApiError

REVISION = '11111111-2222-3333-4444-555555555555'
SECRET = 'sk-ant-SECRET-token-value'


def bundle(generation, revision=REVISION):
    return {'engines': [{
        'label': 'hub-claude', 'provider': 'claude', 'version': '2.1.400', 'revision': revision,
        'credential_generation': generation, 'env': {}, 'args': [],
        'files': {'.claude/.credentials.json': json.dumps({'claudeAiOauth': {'accessToken': SECRET}})},
    }], 'latest_version': '2.1.400'}


class FakeClient:
    def __init__(self, generation=9):
        self.generation, self.calls, self.timeouts = generation, [], []
        self.sync_error = None
        self.ack_reply = {'status': 'recorded'}
        self.gate = None

    def request(self, method, path, body=None, key=None, timeout=None):
        self.calls.append((method, path, body))
        self.timeouts.append(timeout)
        if self.gate is not None:
            self.gate.wait(5)
        if path == '/api/engines/sync':
            if self.sync_error:
                raise self.sync_error
            return bundle(self.generation)
        if path == '/api/engines/sync-report':
            return self.ack_reply
        raise AssertionError('unexpected request ' + path)

    def acks(self):
        return [body for method, path, body in self.calls if path == '/api/engines/sync-report']


class CredentialDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.journal = Journal(Path(self.temp.name) / 'journal.sqlite')
        self.addCleanup(self.journal.db.close)
        self.client = FakeClient()
        self.bridge = SimpleNamespace(client=self.client, journal=self.journal, engines={'claude', 'shell'})
        self.materialized = []
        self.installs = []

        def install(version):
            self.installs.append(version)
            return '/managed/claude'

        def materialize(root, account, provider, generation, files, model, timeout_seconds=0, engine_binary=None):
            self.materialized.append((account, provider, generation, model, engine_binary))
            home = Path(root) / (account + '-' + str(generation) + '-' + 'a' * 12)
            home.mkdir(parents=True, exist_ok=True)
            return str(home / 'manifest.json')

        def verify(manifest):
            generation = int(Path(manifest).parent.name.split('-')[1])
            return {'generation': generation}

        for name, fake in (('install_managed_claude', install), ('materialize', materialize), ('verify', verify)):
            patcher = patch.object(delivery.provider_home, name, fake)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.join)

    def join(self):
        delivery.stop(self.bridge)
        thread = delivery.delivery_thread(self.bridge)
        if thread is not None:
            thread.join(5)

    def run_once(self, now=None):
        started = delivery.request(self.bridge, now)
        thread = delivery.delivery_thread(self.bridge)
        if thread is not None:
            thread.join(5)
            self.assertFalse(thread.is_alive())
        return started

    def test_new_generation_is_materialized_and_exactly_acknowledged(self):
        self.assertTrue(self.run_once())
        self.assertEqual(self.materialized, [('claude', 'claude', 9, 'opus', '/managed/claude')])
        self.assertEqual(self.client.acks(), [{'state': 'synced', 'items': [{
            'label': 'hub-claude', 'revision': REVISION, 'state': 'installed', 'credential_generation': 9}]}])
        self.assertEqual(delivery.delivered(self.bridge), (9, REVISION))
        self.assertIsNone(delivery.diagnostic(self.bridge))

    def test_unchanged_generation_is_not_rematerialized_but_a_new_one_is(self):
        self.run_once()
        self.assertTrue(self.run_once(time.monotonic() + 10_000))
        self.assertEqual(len(self.materialized), 1)
        self.assertEqual(len(self.client.acks()), 1)
        self.client.generation = 10
        self.assertTrue(self.run_once(time.monotonic() + 20_000))
        self.assertEqual([item[2] for item in self.materialized], [9, 10])
        self.assertEqual([ack['items'][0]['credential_generation'] for ack in self.client.acks()], [9, 10])

    def test_scope_is_only_the_enrolled_claude_label(self):
        self.run_once()
        paths = {(method, path) for method, path, _ in self.client.calls}
        self.assertEqual(paths, {('GET', '/api/engines/sync'), ('POST', '/api/engines/sync-report')})
        self.assertTrue(all(call[1].startswith('/api/engines/') for call in self.client.calls))
        self.assertTrue(all(ack['items'][0]['label'] == 'hub-claude' for ack in self.client.acks()))

    def test_non_claude_hosts_and_missing_journal_never_start_a_worker(self):
        self.bridge.engines = {'codex', 'shell'}
        self.assertFalse(delivery.request(self.bridge))
        self.assertEqual(self.client.calls, [])

    def test_single_inflight_never_blocks_the_caller(self):
        self.client.gate = threading.Event()
        begun = time.monotonic()
        self.assertTrue(delivery.request(self.bridge))
        for _ in range(5):
            self.assertFalse(delivery.request(self.bridge, time.monotonic() + 10_000))
        self.assertLess(time.monotonic() - begun, 1)
        self.assertEqual(sum(1 for t in threading.enumerate() if t.name == 'hub2-credential-delivery'), 1)
        self.client.gate.set()
        delivery.delivery_thread(self.bridge).join(5)
        self.assertEqual(len(self.client.acks()), 1)

    def test_failures_back_off_exponentially_then_recover(self):
        self.client.sync_error = ApiError('Hub HTTP 503', 503, 'managed_credential_unavailable')
        base = time.monotonic()
        self.run_once(base)
        self.assertEqual(delivery.diagnostic(self.bridge), 'api_503')
        self.assertFalse(delivery.request(self.bridge, base + 1))          # inside backoff
        first = self.bridge._credential_delivery['next_at']
        self.assertTrue(self.run_once(first + 1))
        second = self.bridge._credential_delivery['next_at']
        self.assertGreater(second - first, delivery.BACKOFF_BASE_SECONDS)   # doubled
        self.client.sync_error = None
        self.assertTrue(self.run_once(second + 1))
        self.assertIsNone(delivery.diagnostic(self.bridge))
        self.assertEqual(delivery.delivered(self.bridge), (9, REVISION))

    def test_not_enrolled_waits_long_and_missing_ack_is_not_delivered(self):
        self.client.sync_error = ApiError('Hub HTTP 403', 403, 'forbidden')
        self.run_once()
        self.assertEqual(delivery.diagnostic(self.bridge), 'api_403')
        self.assertGreaterEqual(self.bridge._credential_delivery['next_at'] - time.monotonic(), delivery.NOT_ENROLLED_SECONDS - 5)
        self.setUp_new_bridge_state()
        self.client.ack_reply = {'status': 'other'}
        self.run_once()
        self.assertEqual(delivery.diagnostic(self.bridge), 'provider_generation_ack_unverified')
        self.assertIsNone(delivery.delivered(self.bridge))

    def setUp_new_bridge_state(self):
        self.bridge._credential_delivery = None
        self.client.sync_error = None
        delattr(self.bridge, '_credential_delivery')

    def test_network_timeout_is_bounded_and_sanitized(self):
        self.client.sync_error = TimeoutError('timed out connecting with ' + SECRET)
        self.run_once()
        self.assertEqual(delivery.diagnostic(self.bridge), 'network_unavailable')
        self.assertTrue(all(t == delivery.REQUEST_TIMEOUT_SECONDS for t in self.client.timeouts))
        self.assertEqual(self.materialized, [])

    def test_materialization_refusal_withholds_ack(self):
        with patch.object(delivery.provider_home, 'materialize', side_effect=ValueError(SECRET)):
            self.run_once()
        self.assertEqual(delivery.diagnostic(self.bridge), 'provider_materialization_refused')
        self.assertEqual(self.client.acks(), [])

    def test_no_secret_in_logs_diagnostics_or_errors(self):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        logger = logging.getLogger('agent_hub_host.credential_delivery')
        logger.addHandler(handler)
        self.addCleanup(logger.removeHandler, handler)
        self.client.sync_error = ApiError('Hub HTTP 500 ' + SECRET, 500, 'x')
        self.run_once()
        with patch.object(delivery.provider_home, 'materialize', side_effect=RuntimeError(SECRET)):
            self.client.sync_error = None
            self.run_once(time.monotonic() + 10_000)
        self.assertNotIn(SECRET, stream.getvalue())
        self.assertNotIn(SECRET, repr(self.bridge._credential_delivery))

    def test_stop_before_ack_prevents_acknowledgement_and_rearm_resumes(self):
        original = delivery.provider_home.verify

        def stopping(manifest):
            delivery.stop(self.bridge)
            return original(manifest)

        with patch.object(delivery.provider_home, 'verify', stopping):
            self.run_once()
        self.assertEqual(self.client.acks(), [])
        self.assertFalse(delivery.request(self.bridge, time.monotonic() + 10_000))  # stopped
        delivery.start(self.bridge)
        self.assertTrue(self.run_once(time.monotonic() + 10_000))
        self.assertEqual(len(self.client.acks()), 1)

    def test_old_generation_homes_are_pruned_only_inside_own_root(self):
        root = Path(self.journal.directory) / 'credential-delivery'
        for generation in (1, 2, 3):
            (root / ('claude-' + str(generation) + '-' + 'b' * 12)).mkdir(parents=True)
        other = Path(self.temp.name) / 'unrelated'
        other.mkdir()
        self.client.generation = 4
        self.run_once()
        names = sorted(path.name for path in root.iterdir())
        self.assertNotIn('claude-1-' + 'b' * 12, names)
        self.assertIn('claude-4-' + 'a' * 12, names)
        self.assertTrue(other.exists())

    def test_heartbeat_hook_returns_without_waiting_for_a_stalled_worker(self):
        from agent_hub_host.agent import Bridge
        self.client.gate = threading.Event()
        bridge = Bridge.__new__(Bridge)
        bridge.client, bridge.journal, bridge.engines = self.client, self.journal, {'claude'}
        begun = time.monotonic()
        delivery.request(bridge)
        delivery.request(bridge, time.monotonic() + 10_000)
        self.assertLess(time.monotonic() - begun, 1)
        self.client.gate.set()
        delivery.stop(bridge)
        delivery.delivery_thread(bridge).join(5)


if __name__ == '__main__':
    unittest.main()
