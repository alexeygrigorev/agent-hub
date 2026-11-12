"""Background hub-claude delivery: passive observation, actual consumer, exact
ACK, single in-flight, irreversible connection-epoch ownership, never blocking
the real Bridge heartbeat or STOP, and no secret in any diagnostic."""
import io
import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent_hub_host import credential_delivery as delivery
from agent_hub_host.agent import Bridge, Journal
from agent_hub_host.client import ApiError

REVISION = '11111111-2222-3333-4444-555555555555'
SECRET = 'sk-ant-SECRET-token-value'
PATH = '.claude/.credentials.json'
ALLOWED = {('GET', delivery.PASSIVE_PATH), ('POST', delivery.ACK_PATH)}

# A stand-in for the verified release's bundled CLI helper. It is run as a
# real child process through the same boot line; the maintained consumer is a
# temp home file with a generation state written file-first, state-last.
HELPER = r'''
import json, os, sys, time
from pathlib import Path
def main(argv):
    root = Path(__file__).resolve().parents[2]
    mode = (root / 'mode').read_text().strip()
    (root / 'started').write_text(str(os.getpid()))
    if mode == 'sleep':
        time.sleep(30)
    request = json.loads(sys.stdin.read(32768))
    if argv != ['materialize']:
        print(json.dumps({'status': 'refused', 'reason': 'invalid_arguments'})); return 3
    if mode == 'absent':
        print(json.dumps({'status': 'absent'})); return 0
    if mode == 'refused':
        print(json.dumps({'status': 'refused', 'reason': 'receipt_revision_mismatch'})); return 3
    if mode == 'leaky':
        print(json.dumps({'status': 'refused', 'reason': request['files']['.claude/.credentials.json']})); return 3
    home = root / 'consumer' / 'home' / '.claude'
    home.mkdir(parents=True, exist_ok=True)
    state = root / 'consumer' / 'credential-state.json'
    data = request['files']['.claude/.credentials.json']
    current = (home / '.credentials.json').read_text() if (home / '.credentials.json').exists() else None
    (home / '.credentials.json').write_text(data)
    state.write_text(json.dumps({'generation': request['generation']}))
    print(json.dumps({'status': 'in_step' if current == data else 'materialized',
                      'generation': request['generation'], 'revision': request['revision']}))
    return 0
'''


def credential(generation):
    return json.dumps({'claudeAiOauth': {'accessToken': SECRET + str(generation), 'expiresAt': 4102444800000}})


class FakeClient:
    """Host machine client: passive read, exact generation check, strict ACK."""

    def __init__(self, generation=9):
        self.generation, self.revision = generation, REVISION
        self.calls, self.timeouts = [], []
        self.passive_error = self.check_error = None
        self.ack_reply = {'status': 'recorded'}
        self.gate = None
        self.heartbeats = 0

    def heartbeat(self, *args, **kwargs):
        self.heartbeats += 1

    def request(self, method, path, body=None, key=None, timeout=None):
        self.calls.append((method, path, body))
        self.timeouts.append(timeout)
        if path == delivery.PASSIVE_PATH:
            if self.gate is not None:
                self.gate.wait(10)
            if self.passive_error:
                raise self.passive_error
            return {'label': 'hub-claude', 'generation': self.generation, 'revision': self.revision,
                    'status': 'current', 'files': {PATH: credential(self.generation)}}
        if path.startswith('/api/engines/hub-claude/generation?'):
            if self.check_error:
                raise self.check_error
            return {'label': 'hub-claude', 'generation': self.generation, 'revision': self.revision, 'status': 'current'}
        if path == delivery.ACK_PATH:
            return self.ack_reply
        return {}

    def paths(self):
        return [(method, path.split('?')[0]) for method, path, _ in self.calls]

    def acks(self):
        return [body for method, path, body in self.calls if path == delivery.ACK_PATH]


class Release:
    """A temp 'verified release': bin/python and a bundled helper module."""

    def __init__(self, root):
        self.root = Path(root) / 'release'
        (self.root / 'bin').mkdir(parents=True)
        (self.root / 'bin/python').symlink_to(sys.executable)
        package = self.root / 'hub.pyz' / 'agent_hub_host'
        package.mkdir(parents=True)
        (package / '__init__.py').write_text('')
        (package / 'managed_claude_deliver.py').write_text(HELPER)
        self.mode('deliver')

    def mode(self, value):
        (self.root / 'mode').write_text(value)

    @property
    def archive(self):
        return str(self.root / 'hub.pyz')

    @property
    def target(self):
        return self.root / 'consumer' / 'home' / PATH

    def state(self):
        return json.loads((self.root / 'consumer' / 'credential-state.json').read_text())


class DeliveryCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.journal = Journal(Path(self.temp.name) / 'journal.sqlite')
        self.addCleanup(self.journal.db.close)
        self.release = Release(self.temp.name)
        self.client = FakeClient()
        launcher = SimpleNamespace(managed_claude_readiness=lambda: {'archive': self.release.archive})
        self.bridge = SimpleNamespace(client=self.client, journal=self.journal, engines={'claude', 'shell'},
                                      launcher=launcher)
        delivery.start(self.bridge)
        self.addCleanup(self.finish)

    def finish(self):
        delivery.stop(self.bridge)
        self.join()

    def join(self):
        thread = delivery.delivery_thread(self.bridge)
        if thread is not None:
            thread.join(15)
            self.assertFalse(thread.is_alive())

    def run_once(self, now=None):
        started = delivery.request(self.bridge, now=now)
        self.join()
        return started

    def assert_no_forbidden_paths(self):
        for method, path in self.client.paths():
            self.assertTrue((method, path) in ALLOWED or path == '/api/engines/hub-claude/generation',
                            (method, path))


class ConsumerDeliveryTests(DeliveryCase):
    def test_new_generation_reaches_the_consumer_then_exact_ack(self):
        self.assertTrue(self.run_once())
        self.assertEqual(self.release.target.read_text(), credential(9))
        self.assertEqual(self.release.state(), {'generation': 9})
        self.assertEqual(self.client.paths(), [('GET', delivery.PASSIVE_PATH),
                                               ('GET', '/api/engines/hub-claude/generation'),
                                               ('POST', delivery.ACK_PATH)])
        self.assertEqual(self.client.calls[1][1],
                         '/api/engines/hub-claude/generation?generation=9&revision=' + REVISION)
        self.assertEqual(self.client.acks(), [{'state': 'synced', 'items': [{
            'label': 'hub-claude', 'revision': REVISION, 'state': 'installed', 'credential_generation': 9}]}])
        self.assertEqual(delivery.delivered(self.bridge), (9, REVISION))
        self.assertIsNone(delivery.diagnostic(self.bridge))

    def test_renewed_generation_is_redelivered_and_unchanged_one_is_not(self):
        self.run_once(now=0)
        self.run_once(now=10**9)
        self.assertEqual(len(self.client.acks()), 1)
        self.client.generation = 10
        self.run_once(now=2 * 10**9)
        self.assertEqual(self.release.target.read_text(), credential(10))
        self.assertEqual(self.release.state(), {'generation': 10})
        self.assertEqual([a['items'][0]['credential_generation'] for a in self.client.acks()], [9, 10])
        self.assertEqual(delivery.delivered(self.bridge), (10, REVISION))

    def test_absent_consumer_never_acks_an_unused_copy(self):
        self.release.mode('absent')
        self.run_once()
        self.assertEqual(self.client.acks(), [])
        self.assertFalse(self.release.target.exists())
        self.assertEqual(delivery.diagnostic(self.bridge), 'managed_claude_consumer_absent')
        self.assertIsNone(delivery.delivered(self.bridge))

    def test_unavailable_release_is_a_truthful_diagnostic_without_any_request(self):
        self.bridge.launcher.managed_claude_readiness = lambda: None
        self.run_once()
        self.assertEqual(self.client.calls, [])
        self.assertEqual(delivery.diagnostic(self.bridge), 'managed_claude_unavailable')

    def test_consumer_refusal_is_sanitized_and_not_acknowledged(self):
        self.release.mode('refused')
        self.run_once()
        self.assertEqual(self.client.acks(), [])
        self.assertEqual(delivery.diagnostic(self.bridge), 'consumer_receipt_revision_mismatch')

    def test_generation_changed_after_materialization_is_not_acknowledged(self):
        self.client.check_error = ApiError('generation_conflict', 409)
        self.run_once()
        self.assertEqual(self.client.acks(), [])
        self.assertEqual(delivery.diagnostic(self.bridge), 'api_409')
        self.assertIsNone(delivery.delivered(self.bridge))

    def test_rejected_ack_is_retried_not_recorded(self):
        self.client.ack_reply = {'status': 'conflict'}
        self.run_once(now=0)
        self.assertEqual(delivery.diagnostic(self.bridge), 'generation_ack_unverified')
        self.assertIsNone(delivery.delivered(self.bridge))
        self.client.ack_reply = {'status': 'recorded'}
        self.run_once(now=10**9)
        self.assertEqual(delivery.delivered(self.bridge), (9, REVISION))

    def test_passive_refusals_never_reach_the_consumer(self):
        for error, code in ((ApiError('refresh_in_progress', 409), 'api_409'),
                            (ApiError('refresh_outcome_uncertain', 503), 'api_503')):
            with self.subTest(code=code):
                self.client.passive_error = error
                self.run_once(now=10**12 + len(self.client.calls) * 10**9)
                self.assertFalse((self.release.root / 'started').exists())
                self.assertEqual(self.client.acks(), [])
                self.assertEqual(delivery.diagnostic(self.bridge), code)

    def test_invalid_passive_material_is_refused_before_the_consumer(self):
        original = self.client.request
        def request(method, path, body=None, key=None, timeout=None):
            reply = original(method, path, body, key, timeout)
            if path == delivery.PASSIVE_PATH:
                reply = dict(reply, files={PATH: reply['files'][PATH], '.claude/settings.json': '{}'})
            return reply
        self.client.request = request
        self.run_once()
        self.assertFalse((self.release.root / 'started').exists())
        self.assertEqual(delivery.diagnostic(self.bridge), 'passive_generation_invalid')

    def test_only_passive_check_and_ack_routes_with_bounded_timeouts(self):
        self.run_once()
        self.client.generation = 10
        self.run_once(now=10**9)
        self.assert_no_forbidden_paths()
        for method, path, _ in self.client.calls:
            self.assertNotIn('sync', path.replace('sync-report', ''))
            self.assertNotIn('refresh', path)
            self.assertNotIn('login', path)
            self.assertNotIn('/credential', path.replace('passive-credential', ''))
        self.assertTrue(self.client.timeouts)
        self.assertEqual(set(self.client.timeouts), {delivery.REQUEST_TIMEOUT_SECONDS})

    def test_secrets_never_reach_logs_diagnostics_or_argv(self):
        launched = []
        def popen(argv, **options):
            launched.append((argv, options.get('env')))
            return subprocess.Popen(argv, **options)
        self.bridge.delivery_popen = popen
        self.release.mode('leaky')
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        logging.getLogger(delivery.__name__).addHandler(handler)
        self.addCleanup(logging.getLogger(delivery.__name__).removeHandler, handler)
        self.run_once()
        self.assertEqual(delivery.diagnostic(self.bridge), 'consumer_refused')
        self.assertNotIn(SECRET, stream.getvalue())
        self.assertNotIn(SECRET, json.dumps(launched))
        argv, env = launched[0]
        self.assertEqual(argv[1:3], ['-I', '-S'])
        self.assertEqual(argv[-2:], [self.release.archive, 'materialize'])
        self.assertFalse({'HUB_TOKEN', 'HUB2_TOKEN', 'HUB_JOB_TOKEN'} & set(env))
        self.assertFalse([key for key in env if key.startswith(('CLAUDE_', 'ANTHROPIC_'))])


class SchedulingTests(DeliveryCase):
    def test_single_inflight_and_never_waits(self):
        self.client.gate = threading.Event()
        started = time.monotonic()
        self.assertTrue(delivery.request(self.bridge))
        self.assertFalse(delivery.request(self.bridge))
        self.assertFalse(delivery.request(self.bridge))
        self.assertLess(time.monotonic() - started, 1)
        self.client.gate.set()
        self.join()
        self.assertEqual(self.client.paths().count(('GET', delivery.PASSIVE_PATH)), 1)
        self.assertEqual(len(self.client.acks()), 1)

    def test_network_timeout_backs_off_exponentially_then_recovers(self):
        self.client.passive_error = TimeoutError('timed out')
        self.run_once(now=0)
        self.assertEqual(delivery.diagnostic(self.bridge), 'network_unavailable')
        first = self.bridge._credential_delivery['next_at'] - time.monotonic()
        self.assertGreater(first, delivery.BACKOFF_BASE_SECONDS - 2)
        self.assertFalse(delivery.request(self.bridge))  # not due yet
        self.run_once(now=10**9)
        second = self.bridge._credential_delivery['next_at'] - time.monotonic()
        self.assertGreater(second, 2 * delivery.BACKOFF_BASE_SECONDS - 2)
        self.client.passive_error = None
        self.run_once(now=2 * 10**9)
        self.assertIsNone(delivery.diagnostic(self.bridge))
        self.assertEqual(self.bridge._credential_delivery['failures'], 0)

    def test_unenrolled_host_waits_the_long_interval(self):
        self.client.passive_error = ApiError('forbidden', 403)
        self.run_once()
        remaining = self.bridge._credential_delivery['next_at'] - time.monotonic()
        self.assertGreater(remaining, delivery.NOT_ENROLLED_SECONDS - 5)
        self.assertEqual(delivery.diagnostic(self.bridge), 'api_403')

    def test_worker_closes_its_own_journal_connection(self):
        closed = []
        original = self.journal.close_thread
        self.journal.close_thread = lambda: (closed.append(threading.current_thread().name), original())
        self.run_once()
        self.assertEqual(closed, ['hub2-credential-delivery'])

    def test_requires_claude_engine_and_running_epoch(self):
        self.bridge.engines = {'shell'}
        self.assertFalse(delivery.request(self.bridge))
        self.bridge.engines = {'claude'}
        delivery.stop(self.bridge)
        self.assertFalse(delivery.request(self.bridge))
        self.assertEqual(self.client.calls, [])


class ConnectionOwnershipTests(DeliveryCase):
    def test_late_result_of_old_connection_cannot_write_or_ack_after_reconnect(self):
        self.client.gate = threading.Event()
        self.assertTrue(delivery.request(self.bridge))
        old = self.bridge._credential_delivery['epoch']
        delivery.stop(self.bridge)            # connection lost
        delivery.start(self.bridge)           # reconnect: a new epoch
        self.assertTrue(old.stopped.is_set())
        # The old worker is still in flight: no second worker meanwhile.
        self.assertFalse(delivery.request(self.bridge))
        self.client.gate.set()
        self.join()
        self.assertFalse((self.release.root / 'started').exists())
        self.assertEqual(self.client.acks(), [])
        self.assertIsNone(delivery.delivered(self.bridge))
        self.assertIsNone(delivery.diagnostic(self.bridge))  # nothing published by the old epoch
        self.assertTrue(old.stopped.is_set())                # never revived
        self.client.gate = None
        self.assertTrue(self.run_once())
        self.assertEqual(delivery.delivered(self.bridge), (9, REVISION))
        self.assertEqual(len(self.client.acks()), 1)

    def test_stop_kills_an_inflight_consumer_and_nothing_is_acknowledged(self):
        self.release.mode('sleep')
        self.assertTrue(delivery.request(self.bridge))
        deadline = time.monotonic() + 10
        while not (self.release.root / 'started').exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue((self.release.root / 'started').exists())
        started = time.monotonic()
        delivery.stop(self.bridge)
        self.assertLess(time.monotonic() - started, 1)
        self.join()
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(self.client.acks(), [])
        self.assertIsNone(self.bridge._credential_delivery['child'])
        self.assertFalse(delivery.request(self.bridge))

    def test_restart_creates_a_fresh_epoch_and_never_clears_the_old_one(self):
        first = self.bridge._credential_delivery['epoch']
        delivery.start(self.bridge)
        second = self.bridge._credential_delivery['epoch']
        self.assertIsNot(first, second)
        self.assertTrue(first.stopped.is_set())
        self.assertFalse(second.stopped.is_set())


class FakeNative:
    def __init__(self):
        self.calls = []

    def __call__(self, args, **options):
        self.calls.append(args)
        if args[1:2] == ['list']:
            return subprocess.CompletedProcess(args, 0, '[]', '')
        return subprocess.CompletedProcess(args, 1, '', 'not found')


class RealBridgeTests(unittest.TestCase):
    """The actual Bridge heartbeat and STOP paths stay prompt while delivery is stuck."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = self.root = Path(self.temp.name)
        self.journal = Journal(root / 'private/journal.sqlite')
        self.addCleanup(self.journal.db.close)
        self.release = Release(root)
        self.client = FakeClient()
        self.gate = self.client.gate = threading.Event()
        self.addCleanup(self.gate.set)
        self.bridge = Bridge(self.client, self.journal, [root], ['claude'], executable=sys.executable,
                             runner=FakeNative(),
                             launcher_config={'private_provider_homes': True, 'argv': [sys.executable],
                                              'config_dir': str(root / 'launcher'),
                                              'routes': {'claude': {'provider': 'claude'}}},
                             machine_id='machine-1')
        self.bridge.launcher.capabilities = lambda: {'claude': {'headless'}}
        self.bridge.launcher.managed_claude_readiness = lambda: {'archive': self.release.archive}
        delivery.start(self.bridge)
        def finish():
            delivery.stop(self.bridge)
            self.gate.set()
            thread = delivery.delivery_thread(self.bridge)
            if thread is not None:
                thread.join(15)
        self.addCleanup(finish)

    def test_heartbeat_and_stop_do_not_wait_for_a_stuck_delivery(self):
        started = time.monotonic()
        self.bridge.heartbeat(['worker'])
        thread = delivery.delivery_thread(self.bridge)
        self.assertIsNotNone(thread)
        self.assertTrue(thread.is_alive())
        self.bridge.heartbeat(['worker'])
        self.bridge.heartbeat(['worker'])
        self.assertIs(delivery.delivery_thread(self.bridge), thread)  # single in-flight
        with self.journal.db:
            self.journal.db.execute(
                "INSERT INTO launches(operation_id,session_id,epoch,digest,workspace,tag,native_id,state,interactive) "
                "VALUES(?,?,?,?,?,?,NULL,?,0)",
                ('op-stop', 'session-stop', 7, 'digest', str(self.root), 'task-hub2-session-stop', 'launching'))
        result = self.bridge.command({'id': 'stop-launching', 'session_id': 'session-stop', 'epoch': 7, 'kind': 'stop'})
        self.assertEqual(result.get('reason'), 'stop_unconfirmed')
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(self.client.heartbeats, 3)
        self.assertTrue(thread.is_alive())
        self.assertEqual(self.client.acks(), [])
        # Shutdown is also non-blocking and the worker then exits without acting.
        stop_started = time.monotonic()
        delivery.stop(self.bridge)
        self.assertLess(time.monotonic() - stop_started, 1)
        self.client.gate.set()
        thread.join(15)
        self.assertFalse(thread.is_alive())
        self.assertEqual(self.client.acks(), [])
        self.assertFalse((self.release.root / 'started').exists())

    def test_heartbeat_delivers_through_the_actual_bridge(self):
        self.client.gate.set()
        self.client.gate = None
        self.bridge.heartbeat(['worker'])
        thread = delivery.delivery_thread(self.bridge)
        self.assertIsNotNone(thread)
        thread.join(15)
        self.assertEqual(self.release.target.read_text(), credential(9))
        self.assertEqual(len(self.client.acks()), 1)


if __name__ == '__main__':
    unittest.main()
