"""Late first-action observation: proof is later activity, never a start gate."""
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from agent_hub_host import native_launch as nl
from agent_hub_host.agent import Bridge, Journal
from agent_hub_host.first_action_observation import FirstActionObserver
from agent_hub_host.native_launch import NativeLauncher

NATIVE = 'd6cf9872-8597-48a5-be64-990cdef4db31'


def wait_for(predicate, seconds=5):
    end = time.time() + seconds
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class ObserverTests(unittest.TestCase):
    def observer(self, **options):
        observer = FirstActionObserver(interval=0.01, **options)
        self.addCleanup(observer.close)
        return observer

    def test_late_proof_publishes_once_and_latches_activity(self):
        observer, published, proofs = self.observer(), [], iter([None, None, 1234, 5678])
        observer.watch('t', 'n', time.time() + 60, lambda: next(proofs), lambda: True,
                       lambda stamp: published.append(stamp) or True)
        self.assertTrue(wait_for(lambda: published))
        time.sleep(0.1)
        self.assertEqual(published, [1234])
        self.assertEqual(observer.activity('n'), 1234)
        self.assertIsNone(observer.activity('other'))

    def test_no_proof_never_publishes_and_expiry_drops_watch(self):
        observer, published = self.observer(), []
        observer.watch('t', 'n', time.time() + 0.1, lambda: None, lambda: True, published.append)
        self.assertTrue(wait_for(lambda: not observer._watches))
        self.assertEqual(published, [])
        self.assertIsNone(observer.activity('n'))

    def test_stop_or_replaced_fence_drops_without_publication(self):
        observer, published, live = self.observer(), [], threading.Event()
        observer.watch('t', 'n', time.time() + 60, lambda: 99, live.is_set, lambda stamp: published.append(stamp) or True)
        self.assertTrue(wait_for(lambda: not observer._watches))
        self.assertEqual(published, [])
        self.assertIsNone(observer.activity('n'))

    def test_stop_during_proof_read_wins(self):
        observer, published, live, reading, release = self.observer(), [], threading.Event(), threading.Event(), threading.Event()
        live.set()
        def prove():
            reading.set()
            release.wait(5)
            return 77
        observer.watch('t', 'n', time.time() + 60, prove, live.is_set, published.append)
        self.assertTrue(reading.wait(5))
        live.clear()
        release.set()
        self.assertTrue(wait_for(lambda: not observer._watches))
        time.sleep(0.05)
        self.assertEqual(published, [])
        self.assertIsNone(observer.activity('n'))

    def test_retry_replaces_older_attempt(self):
        observer, published, gate = self.observer(), [], threading.Event()
        observer.watch('t', 'old', time.time() + 60, lambda: 11 if gate.is_set() else None, lambda: True,
                       lambda stamp: published.append(('old', stamp)) or True)
        observer.watch('t', 'new', time.time() + 60, lambda: 22 if gate.is_set() else None, lambda: True,
                       lambda stamp: published.append(('new', stamp)) or True)
        gate.set()
        self.assertTrue(wait_for(lambda: published))
        time.sleep(0.05)
        self.assertEqual(published, [('new', 22)])
        self.assertIsNone(observer.activity('old'))

    def test_slow_observation_never_blocks_caller_heartbeat_or_stop_path(self):
        observer, release = self.observer(), threading.Event()
        started = time.monotonic()
        self.assertTrue(observer.watch('t', 'n', time.time() + 60, lambda: release.wait(5) and None, lambda: True, lambda stamp: True))
        self.assertIsNone(observer.activity('n'))
        observer.cancel('t')
        self.assertLess(time.monotonic() - started, 1)
        release.set()

    def test_table_is_bounded(self):
        observer = self.observer(limit=2)
        for name in 'ab':
            self.assertTrue(observer.watch(name, name, time.time() + 60, lambda: None, lambda: True, lambda stamp: True))
        self.assertFalse(observer.watch('c', 'c', time.time() + 60, lambda: None, lambda: True, lambda stamp: True))
        self.assertTrue(observer.watch('a', 'a', time.time() + 60, lambda: None, lambda: True, lambda stamp: True))

    def test_failed_publication_retries_a_bounded_number_of_times(self):
        observer, calls = self.observer(), []
        observer.watch('t', 'n', time.time() + 60, lambda: 5, lambda: True, lambda stamp: calls.append(stamp) or False)
        self.assertTrue(wait_for(lambda: len(calls) == 3))
        self.assertTrue(wait_for(lambda: not observer._watches))
        time.sleep(0.05)
        self.assertEqual(calls, [5, 5, 5])


class ProofTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.tmp = Path(self.directory.name)
        self.start = {'id': NATIVE, 'tag': 'task-t1', 'workspace': str(self.tmp),
                      'command': ['x'], 'phase': 'starting', 'schema_version': 1,
                      'created_at_ms': int(time.time() * 1000) - 1000}
        self.path = self.tmp / 'fa.json'

    def whoami(self, **overrides):
        data = {'id': NATIVE, 'tag': 'task-t1', 'workspace': str(self.tmp), 'command': ['x'], 'phase': 'working',
                'schema_version': 2, 'worker_pid': os.getpid() + 1, 'updated_at_ms': self.start['created_at_ms'] + 500}
        data.update(overrides)
        return data

    def proof(self, data=None):
        if data is not None:
            self.path.write_text(json.dumps(data))
        return nl.first_action_proof(str(self.path), self.start, time.time() - 5)

    def test_genuine_artifact_returns_its_own_stamp_not_now(self):
        self.assertEqual(self.proof(self.whoami()), self.start['created_at_ms'] + 500)

    def test_unknown_inputs_stay_unknown(self):
        self.assertIsNone(self.proof())  # absent
        cases = {
            'foreign id': self.whoami(id='11111111-1111-4111-8111-111111111111'),
            'foreign tag': self.whoami(tag='task-other'),
            'foreign workspace': self.whoami(workspace='/elsewhere'),
            'frontend only phase': self.whoami(phase='starting'),
            'future stamp': self.whoami(updated_at_ms=int(time.time() * 1000) + 600000),
            'predates start': self.whoami(updated_at_ms=self.start['created_at_ms']),
            'string stamp': self.whoami(updated_at_ms='x'),
            'no worker': {k: v for k, v in self.whoami().items() if k != 'worker_pid'},
            'array': [1],
        }
        for name, data in cases.items():
            with self.subTest(name):
                self.assertIsNone(self.proof(data))
        self.path.write_text('{malformed')
        self.assertIsNone(self.proof())

    def test_stale_future_symlinked_and_non_regular_artifacts_rejected(self):
        self.path.write_text(json.dumps(self.whoami()))
        os.utime(self.path, (time.time() - 3600, time.time() - 3600))
        self.assertIsNone(nl.first_action_proof(str(self.path), self.start, time.time() - 5))
        os.utime(self.path, (time.time() + 3600, time.time() + 3600))
        self.assertIsNone(nl.first_action_proof(str(self.path), self.start, time.time() - 5))
        real = self.tmp / 'real.json'
        real.write_text(json.dumps(self.whoami()))
        link = self.tmp / 'link.json'
        link.symlink_to(real)
        self.assertIsNone(nl.first_action_proof(str(link), self.start, time.time() - 5))
        directory = self.tmp / 'dir.json'
        directory.mkdir()
        self.assertIsNone(nl.first_action_proof(str(directory), self.start, time.time() - 5))
        linked_parent = self.tmp / 'parent'
        linked_parent.symlink_to(self.tmp)
        self.assertIsNone(nl.first_action_proof(str(linked_parent / 'real.json'), self.start, time.time() - 5))

    def test_start_record_clone_rejected(self):
        self.assertIsNone(self.proof(dict(self.start, phase='working', worker_pid=os.getpid() + 1, extra='x')))


@unittest.skipUnless(os.name == 'posix', 'POSIX launch command contract')
class LaunchTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.tmp = self.directory.name
        self.created = int(time.time() * 1000) - 5000

        def runner(argv, **options):
            return subprocess.CompletedProcess(argv, 0, json.dumps({
                'id': NATIVE, 'tag': 'task-hub2-s', 'workspace': self.tmp, 'created_at_ms': self.created,
                'command': argv[argv.index('--') + 1:]}), '')
        self.launcher = NativeLauncher({}, runner=runner)
        self.launcher._first_action = FirstActionObserver(interval=0.01)
        self.addCleanup(self.launcher._first_action.close)
        self.current = True
        self.specification = {'session_id': 's', 'workspace': self.tmp, 'engine': 'claude', 'model': 'test-model',
                              'effort': None, 'prompt': 'Work', 'timeout_seconds': 600,
                              'operation_id': 'op1', 'epoch': 3,
                              'first_action_current': lambda native: self.current and native == NATIVE}
        self.fa = Path(self.tmp) / '.local' / 'first-action-hub2-s.json'
        self.evidence = Path(self.tmp) / '.local' / 'launch-hub2-s.json'

    def artifact(self, **overrides):
        data = {'id': NATIVE, 'tag': 'task-hub2-s', 'workspace': self.tmp, 'command': ['x'], 'phase': 'working',
                'schema_version': 2, 'worker_pid': os.getpid() + 1, 'updated_at_ms': self.created + 800}
        data.update(overrides)
        self.fa.write_text(json.dumps(data))

    def launch(self):
        self.launcher.codex_exec_readiness = Mock(return_value={'wrapper': 'proof'})
        return self.launcher.launch('hub2-s', dict(self.specification))

    def record(self):
        return json.loads(self.evidence.read_text())

    def test_receipt_is_immediate_then_late_valid_proof_updates_evidence_once(self):
        receipt = self.launch()
        self.assertEqual(receipt['session_id'], NATIVE)
        self.assertFalse(receipt['first_action_verified'])
        before = self.record()
        self.assertIsNone(before['first_action_validated_at'])
        self.assertEqual((before['operation_id'], before['epoch']), ('op1', 3))
        self.assertIsNone(self.launcher.first_action_activity(NATIVE))
        self.artifact()
        self.assertTrue(wait_for(lambda: self.record()['first_action_validated_at'] is not None))
        after = self.record()
        self.assertEqual(after['first_action_validated_at'], (self.created + 800) / 1000)
        self.assertEqual(after['native_started_at'], before['native_started_at'])
        self.assertEqual(self.launcher.first_action_activity(NATIVE), self.created + 800)
        self.assertEqual(oct(self.evidence.stat().st_mode & 0o777), '0o600')
        # Latched: further artifact changes neither rewrite nor refresh.
        stamp = self.evidence.stat().st_mtime_ns
        self.artifact(updated_at_ms=self.created + 900)
        time.sleep(0.1)
        self.assertEqual(self.evidence.stat().st_mtime_ns, stamp)
        self.assertEqual(self.launcher.first_action_activity(NATIVE), self.created + 800)

    def test_frontend_and_foreign_artifacts_never_become_proof(self):
        self.launch()
        for overrides in ({'phase': 'starting'}, {'id': 'other'}, {'tag': 'task-x'}):
            self.artifact(**overrides)
            time.sleep(0.05)
        self.assertIsNone(self.record()['first_action_validated_at'])
        self.assertIsNone(self.launcher.first_action_activity(NATIVE))

    def test_stopped_or_replaced_attempt_does_not_publish(self):
        self.current = False
        self.launch()
        self.artifact()
        self.assertTrue(wait_for(lambda: not self.launcher._first_action._watches))
        self.assertIsNone(self.record()['first_action_validated_at'])
        self.assertIsNone(self.launcher.first_action_activity(NATIVE))

    def test_evidence_for_a_different_attempt_is_not_updated(self):
        self.launch()
        record = self.record() | {'epoch': 9}
        self.evidence.write_text(json.dumps(record))
        self.assertTrue(self.launcher.update_first_action_evidence(
            'hub2-s', self.specification, {'id': NATIVE, 'created_at_ms': self.created}, self.created + 800))
        self.assertIsNone(self.record()['first_action_validated_at'])
        record = self.record() | {'epoch': 3, 'session_id': 'foreign'}
        self.evidence.write_text(json.dumps(record))
        self.assertTrue(self.launcher.update_first_action_evidence(
            'hub2-s', self.specification, {'id': NATIVE, 'created_at_ms': self.created}, self.created + 800))
        self.assertIsNone(self.record()['first_action_validated_at'])

    def test_symlinked_evidence_is_refused(self):
        self.launch()
        target = Path(self.tmp) / 'elsewhere.json'
        target.write_text(self.evidence.read_text())
        self.evidence.unlink()
        self.evidence.symlink_to(target)
        self.assertTrue(self.launcher.update_first_action_evidence(
            'hub2-s', self.specification, {'id': NATIVE, 'created_at_ms': self.created}, self.created + 800))
        self.assertIsNone(json.loads(target.read_text())['first_action_validated_at'])

    def test_blocked_observation_cannot_block_launch_or_stop(self):
        release = threading.Event()
        self.launcher._first_action.close()
        self.launcher._first_action = FirstActionObserver(interval=0.01)
        calls = []
        def slow_proof(*args):
            calls.append(args)
            if len(calls) > 1:
                release.wait(5)  # Only the later observation is slow.
        slow = Mock(side_effect=slow_proof)
        original = nl.first_action_proof
        nl.first_action_proof = slow
        self.addCleanup(setattr, nl, 'first_action_proof', original)
        self.addCleanup(release.set)
        started = time.monotonic()
        self.assertFalse(self.launch()['first_action_verified'])
        self.assertLess(time.monotonic() - started, 2)
        self.assertTrue(wait_for(lambda: slow.called))
        started = time.monotonic()
        self.assertIsNone(self.launcher.first_action_activity(NATIVE))
        self.launcher._first_action.cancel('hub2-s')
        self.assertLess(time.monotonic() - started, 0.5)


class BridgeProjectionTests(unittest.TestCase):
    def bridge(self, journal, activity):
        bridge = Bridge.__new__(Bridge)
        bridge.journal = journal
        bridge._inflight = set()
        bridge._workspace_is_available = lambda workspace: True
        bridge.launcher = SimpleNamespace(first_action_activity=lambda native: activity.get(native))
        return bridge

    def row(self, journal, state='running', operation='op1', epoch=3, native=NATIVE):
        with journal.db:
            journal.db.execute('DELETE FROM launches')
            journal.db.execute('INSERT INTO launches(operation_id,session_id,epoch,digest,workspace,tag,native_id,state,interactive) '
                               'VALUES(?,?,?,?,?,?,?,?,0)', (operation, 's', epoch, 'd', '/w', 'task-hub2-s', native, state))

    def test_current_requires_exact_live_attempt_and_no_stop_fence(self):
        with tempfile.TemporaryDirectory() as temp:
            journal = Journal(Path(temp) / 'journal.sqlite')
            self.addCleanup(journal.close_thread)
            bridge = self.bridge(journal, {})
            self.row(journal)
            self.assertTrue(bridge.first_action_current('op1', 's', 3, NATIVE))
            self.assertFalse(bridge.first_action_current('op0', 's', 3, NATIVE))
            self.assertFalse(bridge.first_action_current('op1', 's', 4, NATIVE))
            self.assertFalse(bridge.first_action_current('op1', 's', 3, 'other'))
            self.row(journal, state='exited')
            self.assertFalse(bridge.first_action_current('op1', 's', 3, NATIVE))
            self.row(journal)
            with journal.db:
                journal.db.execute("INSERT INTO stop_fences VALUES('s',3)")
            self.assertFalse(bridge.first_action_current('op1', 's', 3, NATIVE))

    def test_heartbeat_projection_uses_proven_activity_only_when_native_is_live(self):
        with tempfile.TemporaryDirectory() as temp:
            journal = Journal(Path(temp) / 'journal.sqlite')
            self.addCleanup(journal.close_thread)
            self.row(journal)
            bridge = self.bridge(journal, {NATIVE: 5000})
            def live(status):
                bridge.resolve_launch_native = lambda row: (NATIVE, status)
                return bridge.live_sessions()[0]['last_activity_at']
            self.assertEqual(live({'phase': 'running', 'created_at_ms': 1000}), 5000)
            # Native-reported newer activity is never lowered.
            self.assertEqual(live({'phase': 'running', 'last_activity_ms': 9000}), 9000)
            # Unknown/missing native identity stays uncertain; no fabricated activity.
            self.assertIsNone(live(None))
            self.assertEqual(live({'phase': 'dead', 'created_at_ms': 1000}), 1000)


if __name__ == '__main__':
    unittest.main()
