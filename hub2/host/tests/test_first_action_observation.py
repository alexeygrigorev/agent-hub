"""Late first-action observation: bounded, exactly bound, never a start gate."""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_hub_host import first_action_observation as fao
from agent_hub_host.agent import Bridge, Journal

SESSION = '11111111-2222-3333-4444-555555555555'
TASK = 'hub2-' + SESSION
NATIVE = 'd6cf9872-8597-48a5-be64-990cdef4db31'
CREATED = int(time.time() * 1000) - 60000
STAMP = CREATED + 20000


class Fixture:
    """A private workspace with launch evidence written the way the launcher writes it."""

    def __init__(self, test, **record):
        self.directory = tempfile.mkdtemp(prefix='fa-', dir=os.environ.get('TMPDIR') or None)
        test.addCleanup(self.cleanup)
        self.workspace = str(Path(self.directory).resolve())
        self.start = {'id': NATIVE, 'tag': 'task-' + TASK, 'workspace': self.workspace,
                      'command': ['codex', 'exec'], 'phase': 'starting', 'schema_version': 1, 'created_at_ms': CREATED}
        self.dispatched = time.time() - 70
        self.record = {'task_id': TASK, 'session_id': NATIVE, 'tag': 'task-' + TASK, 'workspace': self.workspace,
                       'created_at_ms': CREATED, 'engine': 'codex', 'native_started_at': self.dispatched + 1,
                       'first_action_validated_at': None, 'first_action_activity_ms': None,
                       'native_dispatch_at': self.dispatched, 'native_start': self.start,
                       'operation_id': 'operation-1', 'epoch': 1}
        self.record.update(record)
        fao.write_record(self.record_path, self.record)
        self.candidate = {'session_id': SESSION, 'operation_id': 'operation-1', 'epoch': 1, 'native_id': NATIVE,
                          'tag': 'task-' + TASK, 'workspace': self.workspace, 'created_at_ms': CREATED}

    def cleanup(self):
        import shutil
        shutil.rmtree(self.directory, ignore_errors=True)

    @property
    def record_path(self):
        return fao.record_path(self.workspace, TASK)

    @property
    def artifact(self):
        return fao.artifact_path(self.workspace, TASK)

    def whoami(self, **overrides):
        data = {'id': NATIVE, 'tag': self.start['tag'], 'workspace': self.workspace, 'command': ['codex'],
                'phase': 'working', 'schema_version': 2, 'worker_pid': os.getpid() + 1,
                'updated_at_ms': STAMP}
        data.update(overrides)
        return data

    def write_artifact(self, data=None, age=30, mode=0o644, raw=None):
        path = self.artifact
        path.write_text(raw if raw is not None else json.dumps(self.whoami() if data is None else data))
        os.chmod(path, mode)
        when = time.time() - age
        os.utime(path, (when, when))
        return when

    def stored(self):
        return json.loads(self.record_path.read_text())


class ObservationTests(unittest.TestCase):
    def setUp(self):
        self.umask = os.umask(0o022)
        self.addCleanup(os.umask, self.umask)
        self.fixture = Fixture(self)
        self.late = fao.LateFirstAction()

    def observe(self, **kwargs):
        self.late.observe(self.fixture.candidate, **kwargs)
        return self.late.activity(self.fixture.candidate)

    def test_late_valid_artifact_publishes_once_with_artifact_time_not_observer_time(self):
        written = self.fixture.write_artifact(age=30)
        self.assertIs(self.late.activity(self.fixture.candidate), fao.UNOBSERVED)
        self.assertEqual(self.observe(), STAMP)
        stored = self.fixture.stored()
        self.assertAlmostEqual(stored['first_action_validated_at'], written, delta=1)
        self.assertLess(stored['first_action_validated_at'], time.time() - 20)  # artifact time, not "now"
        self.assertEqual(stored['first_action_activity_ms'], STAMP)
        self.assertEqual(stored['operation_id'], 'operation-1')
        before = os.stat(self.fixture.record_path)
        with mock.patch.object(fao, 'write_record') as write, mock.patch.object(fao, 'read_artifact') as read:
            self.late.request([self.fixture.candidate], {self.late.key(self.fixture.candidate)})
            self.late.observe(self.fixture.candidate)
        write.assert_not_called()
        read.assert_not_called()
        self.assertEqual(os.stat(self.fixture.record_path).st_ino, before.st_ino)

    def test_missing_artifact_stays_unknown_and_record_unchanged(self):
        original = self.fixture.record_path.read_bytes()
        self.assertIs(self.observe(), fao.UNOBSERVED)
        self.assertEqual(self.fixture.record_path.read_bytes(), original)

    def test_invalid_artifacts_never_become_proof(self):
        fixture = self.fixture
        for name, prepare in {
            'malformed': lambda: fixture.write_artifact(raw='{not json'),
            'empty': lambda: fixture.write_artifact(raw=''),
            'foreign native': lambda: fixture.write_artifact(fixture.whoami(id='aaaaaaaa-0000-0000-0000-000000000000')),
            'foreign tag': lambda: fixture.write_artifact(fixture.whoami(tag='task-other')),
            'foreign workspace': lambda: fixture.write_artifact(fixture.whoami(workspace='/somewhere/else')),
            'replaced incarnation (stamp not after birth)': lambda: fixture.write_artifact(fixture.whoami(updated_at_ms=CREATED)),
            'frontend only phase': lambda: fixture.write_artifact(fixture.whoami(phase='exited')),
            'no worker': lambda: fixture.write_artifact({k: v for k, v in fixture.whoami().items() if k != 'worker_pid'}),
            'start clone': lambda: fixture.write_artifact({**fixture.start, 'updated_at_ms': STAMP, 'worker_pid': 5}),
            'older than dispatch': lambda: fixture.write_artifact(age=3600),
            'future file time': lambda: fixture.write_artifact(age=-3600),
            'future stamp': lambda: fixture.write_artifact(fixture.whoami(updated_at_ms=int(time.time() * 1000) + 3600000)),
            'group writable': lambda: fixture.write_artifact(mode=0o664),
            'oversized': lambda: fixture.write_artifact(raw=' ' * (fao.ARTIFACT_LIMIT + 1)),
        }.items():
            with self.subTest(name):
                if fixture.artifact.exists() or fixture.artifact.is_symlink():
                    fixture.artifact.unlink()
                prepare()
                self.assertIs(self.observe(), fao.UNOBSERVED)
                self.assertIsNone(fixture.stored()['first_action_validated_at'])

    def test_symlinked_artifact_or_directory_is_not_followed(self):
        fixture = self.fixture
        real = Path(fixture.directory) / 'real-whoami.json'
        real.write_text(json.dumps(fixture.whoami()))
        fixture.artifact.symlink_to(real)
        self.assertIs(self.observe(), fao.UNOBSERVED)
        fixture.artifact.unlink()
        Path(fixture.workspace, '.local').rename(Path(fixture.workspace, 'moved'))
        os.symlink(Path(fixture.workspace, 'moved'), Path(fixture.workspace, '.local'))
        self.assertIs(self.observe(), fao.UNOBSERVED)

    def test_proof_is_bound_to_exact_incarnation_and_fence(self):
        self.fixture.write_artifact()
        base = self.fixture.candidate
        for name, change in {'created': {'created_at_ms': CREATED + 1}, 'native': {'native_id': 'aaaaaaaa-0000-0000-0000-000000000000'},
                             'epoch': {'epoch': 2}, 'operation': {'operation_id': 'operation-2'},
                             'session': {'session_id': 'other-session'}, 'workspace': {'workspace': '/elsewhere'},
                             'tag': {'tag': 'task-other'}}.items():
            with self.subTest(name):
                late = fao.LateFirstAction()
                late.observe({**base, **change})
                self.assertIs(late.activity({**base, **change}), fao.UNOBSERVED)
                self.assertIsNone(self.fixture.stored()['first_action_validated_at'])

    def test_memory_latch_does_not_match_a_replaced_fence_or_birth(self):
        self.fixture.write_artifact()
        self.assertEqual(self.observe(), STAMP)
        self.assertIs(self.late.activity({**self.fixture.candidate, 'epoch': 2}), fao.UNOBSERVED)
        self.assertIs(self.late.activity({**self.fixture.candidate, 'created_at_ms': CREATED + 5}), fao.UNOBSERVED)

    def test_record_replaced_between_read_and_write_is_not_overwritten(self):
        self.fixture.write_artifact()
        real = fao.observe_proof

        def replace_record(*args, **kwargs):
            proof = real(*args, **kwargs)
            replaced = dict(self.fixture.record, epoch=2, operation_id='operation-2')
            fao.write_record(self.fixture.record_path, replaced)  # a retry's new evidence
            return proof

        with mock.patch.object(fao, 'observe_proof', replace_record):
            self.assertIs(self.observe(), fao.UNOBSERVED)
        stored = self.fixture.stored()
        self.assertEqual(stored['epoch'], 2)
        self.assertIsNone(stored['first_action_validated_at'])

    def test_stop_before_publication_writes_nothing(self):
        self.fixture.write_artifact()
        stop = threading.Event()
        real = fao.observe_proof

        def stopping(*args, **kwargs):
            proof = real(*args, **kwargs)
            stop.set()
            return proof

        with mock.patch.object(fao, 'observe_proof', stopping):
            self.assertIs(self.observe(stop=stop), fao.UNOBSERVED)
        self.assertIsNone(self.fixture.stored()['first_action_validated_at'])
        self.assertFalse(self.late.request([self.fixture.candidate], set(), stop))

    def test_missing_symlinked_or_foreign_record_stays_unknown(self):
        self.fixture.write_artifact()
        original = self.fixture.record_path.read_bytes()
        self.fixture.record_path.unlink()
        self.assertIs(self.observe(), fao.UNOBSERVED)
        real = Path(self.fixture.directory) / 'elsewhere.json'
        real.write_bytes(original)
        os.chmod(real, 0o600)
        self.fixture.record_path.symlink_to(real)
        self.assertIs(self.observe(), fao.UNOBSERVED)
        self.fixture.record_path.unlink()
        fao.write_record(self.fixture.record_path, {**json.loads(original), 'session_id': 'aaaaaaaa-0000-0000-0000-000000000000'})
        self.assertIs(self.observe(), fao.UNOBSERVED)

    def test_already_latched_record_restores_memory_without_reproving_or_rewriting(self):
        self.fixture.write_artifact()
        self.observe()
        restarted = fao.LateFirstAction()
        with mock.patch.object(fao, 'read_artifact', side_effect=AssertionError('re-proved')), \
                mock.patch.object(fao, 'write_record', side_effect=AssertionError('rewritten')):
            restarted.observe(self.fixture.candidate)
        self.assertEqual(restarted.activity(self.fixture.candidate), STAMP)

    def test_legacy_latched_record_without_activity_is_not_refreshed(self):
        stored = self.fixture.stored()
        stored['first_action_validated_at'] = time.time() - 50
        fao.write_record(self.fixture.record_path, stored)
        self.fixture.write_artifact()
        self.assertIsNone(self.observe())  # latched, but no truthful activity to add
        self.assertEqual(self.fixture.stored()['first_action_validated_at'], stored['first_action_validated_at'])

    def test_record_without_start_evidence_stays_unknown(self):
        stored = self.fixture.stored()
        stored.pop('native_start')
        fao.write_record(self.fixture.record_path, stored)
        self.fixture.write_artifact()
        self.assertIs(self.observe(), fao.UNOBSERVED)

    def test_record_replacement_is_atomic_and_private(self):
        mode = os.stat(self.fixture.record_path).st_mode & 0o777
        self.assertEqual(mode, 0o600)
        leftovers = [name for name in os.listdir(Path(self.fixture.workspace, '.local')) if name.endswith('.tmp')]
        self.assertEqual(leftovers, [])
        with mock.patch.object(fao.os, 'replace', side_effect=OSError('boom')):
            with self.assertRaises(OSError):
                fao.write_record(self.fixture.record_path, {'x': 1})
        self.assertEqual(self.fixture.stored()['session_id'], NATIVE)
        self.assertEqual([name for name in os.listdir(Path(self.fixture.workspace, '.local')) if name.endswith('.tmp')], [])


class PassTests(unittest.TestCase):
    def setUp(self):
        self.fixture = Fixture(self)
        self.late = fao.LateFirstAction()

    def test_request_is_single_inflight_non_blocking_and_bounded(self):
        started, release = threading.Event(), threading.Event()
        calls = []

        def blocked(candidate, stop=None):
            calls.append(candidate)
            started.set()
            release.wait(10)

        candidate = self.fixture.candidate
        with mock.patch.object(self.late, 'observe', blocked):
            begin = time.monotonic()
            self.assertTrue(self.late.request([candidate], {self.late.key(candidate)}))
            self.assertTrue(started.wait(5))
            self.assertFalse(self.late.request([candidate], {self.late.key(candidate)}))
            self.assertLess(time.monotonic() - begin, 2)
            release.set()
            self.late._thread.join(5)
        self.assertEqual(len(calls), 1)

    def test_pass_is_capped_and_a_failing_candidate_does_not_stop_the_rest(self):
        seen = []

        def observe(candidate, stop=None):
            seen.append(candidate['session_id'])
            if candidate['session_id'] == 's0':
                raise OSError('unreadable')

        many = [{**self.fixture.candidate, 'session_id': f's{i}'} for i in range(40)]
        with mock.patch.object(self.late, 'observe', observe):
            self.late.request(many, {self.late.key(c) for c in many})
            self.late._thread.join(5)
        self.assertEqual(len(seen), fao.MAX_PASS)
        self.assertIn('s0', seen)  # the failing one was attempted, the rest still ran

    def run_passes(self, candidates, passes, seen):
        for _ in range(passes):
            self.late.request(candidates, {self.late.key(c) for c in candidates})
            self.late._thread.join(5)

    def test_unknown_candidates_beyond_the_cap_are_not_starved(self):
        seen = []
        self.late.observe = lambda candidate, stop=None: seen.append(candidate['session_id'])
        many = [{**self.fixture.candidate, 'session_id': f's{i:02d}'} for i in range(40)]
        self.run_passes(many, 3, seen)
        self.assertEqual(set(seen), {c['session_id'] for c in many})  # every unknown seen within 3 passes
        self.assertEqual(len(seen), 3 * fao.MAX_PASS)

    def test_rotation_survives_membership_changes_and_pruning(self):
        seen = []
        self.late.observe = lambda candidate, stop=None: seen.append(candidate['session_id'])
        many = [{**self.fixture.candidate, 'session_id': f's{i:02d}'} for i in range(20)]
        self.run_passes(many, 1, seen)
        served = set(seen)
        shrunk = [c for c in many if c['session_id'] not in {'s05', 's16'}] + [
            {**self.fixture.candidate, 'session_id': 's99'}]  # served members leave, a new one joins
        seen.clear()
        self.run_passes(shrunk, 2, seen)
        self.assertEqual(set(seen) | served, {c['session_id'] for c in shrunk} | served)
        self.assertIn('s99', seen)
        self.assertTrue({c['session_id'] for c in shrunk} <= set(seen) | served)

    def test_unknown_launches_do_not_starve_a_later_real_proof(self):
        late_candidate = {**self.fixture.candidate}
        self.fixture.write_artifact()
        filler = [{**self.fixture.candidate, 'session_id': f'a{i:02d}'} for i in range(fao.MAX_PASS + 4)]
        everyone = filler + [late_candidate]  # real session sorts after 16+ missing-evidence entries
        self.run_passes(everyone, 3, [])
        self.assertEqual(self.late.activity(late_candidate), STAMP)

    def test_retired_launches_are_pruned_from_the_latch(self):
        self.fixture.write_artifact = None
        candidate = self.fixture.candidate
        self.late._latch(candidate, STAMP)
        self.assertEqual(self.late.activity(candidate), STAMP)
        self.late.request([], set())
        self.assertIs(self.late.activity(candidate), fao.UNOBSERVED)


class BridgeHeartbeatTests(unittest.TestCase):
    """The heartbeat projection only snapshots; it never waits on observation."""

    def setUp(self):
        self.umask = os.umask(0o022)
        self.addCleanup(os.umask, self.umask)
        self.fixture = Fixture(self)
        self.directory = tempfile.mkdtemp(prefix='fa-journal-', dir=os.environ.get('TMPDIR') or None)
        self.addCleanup(__import__('shutil').rmtree, self.directory, True)
        self.bridge = Bridge.__new__(Bridge)
        self.bridge.journal = Journal(Path(self.directory) / 'journal.sqlite')
        self.addCleanup(self.bridge.journal.close_thread)
        self.bridge._inflight = set()
        self.bridge._workspace_is_available = lambda workspace: True
        self.status = {'id': NATIVE, 'tag': 'task-' + TASK, 'workspace': self.fixture.workspace,
                       'phase': 'running', 'created_at_ms': CREATED, 'last_activity_ms': CREATED + 1000}
        self.bridge.resolve_launch_native = lambda row, **kw: (NATIVE, dict(self.status))
        self.insert('operation-1', 1)

    def insert(self, operation, epoch, state='running'):
        with self.bridge.journal.db as db:
            db.execute('DELETE FROM launches')
            db.execute("INSERT INTO launches(operation_id,session_id,epoch,digest,workspace,tag,native_id,state,native_dispatch_started)"
                       " VALUES(?,?,?,?,?,?,?,?,1)", (operation, SESSION, epoch, 'd' * 64, self.fixture.workspace,
                                                      'task-' + TASK, NATIVE, state))

    def drain(self):
        thread = getattr(self.bridge._late_first_action(), '_thread', None)
        if thread is not None:
            thread.join(5)

    def test_late_proof_updates_projected_activity_with_artifact_stamp_and_publishes_once(self):
        self.assertEqual(self.bridge.live_sessions()[0]['last_activity_at'], CREATED + 1000)
        self.drain()
        self.assertEqual(self.bridge.live_sessions()[0]['last_activity_at'], CREATED + 1000)  # no artifact yet
        self.fixture.write_artifact(age=40)
        self.assertEqual(self.bridge.live_sessions()[0]['last_activity_at'], CREATED + 1000)  # observation is later
        self.drain()
        row = self.bridge.live_sessions()[0]
        self.assertEqual(row['last_activity_at'], STAMP)
        self.assertEqual(row['native_id'], NATIVE)
        self.assertLess(row['last_activity_at'], int(time.time() * 1000) - 1000)  # never "now"
        with mock.patch.object(fao, 'read_artifact', side_effect=AssertionError('latched proof re-read')):
            self.assertEqual(self.bridge.live_sessions()[0]['last_activity_at'], STAMP)
            self.drain()

    def test_proof_never_moves_activity_backwards(self):
        self.status['last_activity_ms'] = STAMP + 5000
        self.fixture.write_artifact()
        self.bridge.live_sessions()
        self.drain()
        self.assertEqual(self.bridge.live_sessions()[0]['last_activity_at'], STAMP + 5000)

    def test_blocked_observation_cannot_block_the_heartbeat_projection(self):
        release, started = threading.Event(), threading.Event()

        def blocked(*args, **kwargs):
            started.set()
            release.wait(10)

        self.fixture.write_artifact()
        with mock.patch.object(fao, '_record', blocked):
            begin = time.monotonic()
            first = self.bridge.live_sessions()
            self.assertTrue(started.wait(5))
            second = self.bridge.live_sessions()
            elapsed = time.monotonic() - begin
            self.assertEqual(first[0]['phase'], 'running')
            self.assertEqual(second[0]['last_activity_at'], CREATED + 1000)
            self.assertLess(elapsed, 2)
            release.set()
            self.drain()

    def test_replaced_fence_or_exited_native_never_reuses_the_proof(self):
        self.fixture.write_artifact()
        self.bridge.live_sessions()
        self.drain()
        self.assertEqual(self.bridge.live_sessions()[0]['last_activity_at'], STAMP)
        self.insert('operation-2', 2)  # retry/replacement under a new fence
        self.assertEqual(self.bridge.live_sessions()[0]['last_activity_at'], CREATED + 1000)
        self.drain()
        self.assertEqual(self.bridge.live_sessions()[0]['last_activity_at'], CREATED + 1000)  # record still says epoch 1
        self.insert('operation-1', 1)
        self.status['phase'] = 'exited'
        self.assertEqual(self.bridge.live_sessions()[0]['last_activity_at'], CREATED + 1000)

    def test_unqualified_native_identity_is_not_observed(self):
        self.fixture.write_artifact()
        for change in ({'id': 'aaaaaaaa-0000-0000-0000-000000000000'}, {'tag': 'task-other'},
                       {'workspace': '/elsewhere'}, {'created_at_ms': None}):
            self.status.update(change)
            with mock.patch.object(fao.LateFirstAction, 'observe') as observe:
                self.bridge.live_sessions()
                self.drain()
            observe.assert_not_called()
            self.status.update({'id': NATIVE, 'tag': 'task-' + TASK, 'workspace': self.fixture.workspace,
                                'created_at_ms': CREATED})
        self.insert('operation-1', 1, state='aborting')
        with mock.patch.object(fao.LateFirstAction, 'observe') as observe:
            self.bridge.live_sessions()
            self.drain()
        observe.assert_not_called()


if __name__ == '__main__':
    unittest.main()
