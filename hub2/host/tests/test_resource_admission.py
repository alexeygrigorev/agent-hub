"""Same-journal pre-dispatch RAM admission: OS samplers, caps, serialization,
resident credit and release custody. All OS and census evidence here is
bounded synthetic injection; no provider, model, API or live participant."""
import ctypes
import hashlib
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import test_host

from agent_hub_host import resource_admission
from agent_hub_host.agent import Journal

FLOOR = resource_admission.RESERVED_FLOOR_BYTES
ALLOWANCE = resource_admission.DEFAULT_ALLOWANCE_BYTES


def admit_bridge(journal, config=None, sample=None):
    """Minimal bridge surface for direct admit_dispatch transactions."""
    bridge = SimpleNamespace(journal=journal,
                             launcher=SimpleNamespace(config=dict(config or {})))
    bridge.attempt_label = lambda session, operation, epoch: hashlib.sha256(
        json.dumps(['machine-1', session, operation, epoch], separators=(',', ':')).encode()).hexdigest()
    if sample is not None:
        bridge.resource_sample = sample
    return bridge


def launching_row(journal, operation='operation-1', session='session-1', epoch=1,
                  state='launching', marker=0, native=None, deadline=None, failure=None, tag=None):
    with journal.db as db:
        db.execute('INSERT INTO launches(operation_id,session_id,epoch,digest,workspace,tag,native_id,state,'
                   'interactive,runtime_deadline,native_dispatch_started,legacy_failure_json) '
                   'VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                   (operation, session, epoch, 'digest', '/owned', tag or 'task-hub2-' + session, native,
                    state, 0, deadline, marker, json.dumps(failure) if failure else None))


def reserve(journal, operation='operation-1', session='session-1', epoch=1,
            native=None, allowance=ALLOWANCE):
    with journal.db as db:
        db.execute('INSERT INTO resource_reservations(operation_id,session_id,epoch,digest,attempt,'
                   'native_id,allowance_bytes,updated_at_ms) VALUES(?,?,?,?,?,?,?,?)',
                   (operation, session, epoch, 'digest', 'attempt', native, allowance, int(time.time() * 1000)))


def census(rows, fresh=True):
    return SimpleNamespace(fresh=lambda: fresh, rows=tuple(rows))


def resident(native_id, tag='task-hub2-session-1', workspace='/owned', birth_ms=None, alive=True,
             phase='running', state='running'):
    return {'id': native_id, 'tag': tag, 'workspace': workspace,
            'phase': phase, 'state': state, 'worker_alive': alive,
            'created_at_ms': int(time.time() * 1000) - 400_000 if birth_ms is None else birth_ms}


class LinuxMeminfoTests(unittest.TestCase):
    def meminfo(self, available, total):
        return f'MemTotal:       {total} kB\nMemFree:        1 kB\nMemAvailable:   {available} kB\n'.encode()

    def test_strict_parse_boundaries(self):
        self.assertEqual(resource_admission.parse_linux_meminfo(self.meminfo(50, 100)), (50 * 1024, 100 * 1024))
        self.assertEqual(resource_admission.parse_linux_meminfo(self.meminfo(100, 100)), (100 * 1024, 100 * 1024))
        with self.assertRaises(RuntimeError):
            resource_admission.parse_linux_meminfo(self.meminfo(101, 100))  # available > total
        with self.assertRaises(RuntimeError):
            resource_admission.parse_linux_meminfo(b'MemTotal: 100 kB\n')  # missing MemAvailable
        with self.assertRaises(RuntimeError):
            resource_admission.parse_linux_meminfo(b'MemAvailable: 1 kB\n')  # missing MemTotal
        with self.assertRaises(RuntimeError):
            resource_admission.parse_linux_meminfo(self.meminfo(5, 100) + self.meminfo(6, 100))  # duplicates
        with self.assertRaises(RuntimeError):
            resource_admission.parse_linux_meminfo(b'MemAvailable: x kB\nMemTotal: 1 kB\n')
        with self.assertRaises(RuntimeError):
            resource_admission.parse_linux_meminfo(b'MemAvailable: 12E kB\nMemTotal: 1 kB\n')
        with self.assertRaises(RuntimeError):
            resource_admission.parse_linux_meminfo(b'MemAvailable: -1 kB\nMemTotal: 1 kB\n')
        with self.assertRaises(RuntimeError):
            resource_admission.parse_linux_meminfo(b'MemAvailable: 1 kB\nMemTotal: 0 kB\n')
        with self.assertRaises(RuntimeError):
            resource_admission.parse_linux_meminfo(b'\xff\xfe not ascii')
        with self.assertRaises(RuntimeError):
            resource_admission.parse_linux_meminfo(b'0' * 65537)
        with self.assertRaises(RuntimeError):
            resource_admission.parse_linux_meminfo('text')

    def test_injected_read_is_platform_independent(self):
        self.assertEqual(resource_admission.sample_linux(
            read=lambda: self.meminfo(7, 10)), 7 * 1024)

    @unittest.skipUnless(sys.platform == 'linux', 'real Linux /proc sample')
    def test_real_linux_meminfo_sample_is_positive(self):
        available = resource_admission.sample_linux()
        self.assertIsInstance(available, int)
        self.assertGreater(available, 0)


class WindowsMemoryStatusTests(unittest.TestCase):
    def status_call(self, load=42, total=32 * 1024**3, available=20 * 1024**3,
                    result=1, break_length=False):
        def call(pointer):
            status = pointer._obj  # byref carrier of the exact struct
            status.dwMemoryLoad = load
            status.ullTotalPhys = total
            status.ullAvailPhys = available
            if break_length:
                status.dwLength = 48
            return result
        return call

    def test_exact_struct_layout(self):
        self.assertEqual(ctypes.sizeof(resource_admission.MemoryStatusEx), 64)

    def test_success_and_range_boundaries(self):
        self.assertEqual(resource_admission.sample_windows(self.status_call()), 20 * 1024**3)
        self.assertEqual(resource_admission.sample_windows(
            self.status_call(total=10, available=10)), 10)
        self.assertEqual(resource_admission.sample_windows(
            self.status_call(total=10, available=0)), 0)
        for broken in (self.status_call(result=0),
                       self.status_call(break_length=True),
                       self.status_call(load=101),
                       self.status_call(load=-1),
                       self.status_call(total=0),
                       self.status_call(total=10, available=11)):
            with self.assertRaises(RuntimeError):
                resource_admission.sample_windows(broken)

    def test_non_windows_kernel_call_refuses(self):
        if sys.platform == 'win32':
            self.skipTest('real win32 platform')
        with self.assertRaises(RuntimeError):
            resource_admission.sample_windows()


class PlatformSampleDispatchTests(unittest.TestCase):
    def test_unknown_platform_refuses(self):
        with self.assertRaises(RuntimeError):
            resource_admission.sample_available_bytes(platform='sunos')

    def test_deadline_refuses_before_and_after_sampling(self):
        expired = time.monotonic() - 1
        with self.assertRaises(RuntimeError):
            resource_admission.sample_available_bytes(deadline=expired, platform='sunos')
        slow = lambda: (time.sleep(0.05), 5 * 1024**3)[1]
        with patch.object(resource_admission, 'sample_linux', side_effect=slow):
            with self.assertRaises(RuntimeError):
                resource_admission.sample_available_bytes(deadline=time.monotonic() + 0.01,
                                                          platform='linux')

    @unittest.skipUnless(sys.platform == 'linux', 'real Linux sample dispatch')
    def test_linux_dispatch_samples(self):
        self.assertGreater(resource_admission.sample_available_bytes(), 0)

    def test_forced_windows_branch_off_windows_refuses(self):
        if sys.platform == 'win32':
            self.skipTest('real win32 platform')
        with self.assertRaises(RuntimeError):
            resource_admission.sample_available_bytes(platform='win32')


class WorkerCapsTests(unittest.TestCase):
    def test_defaults_apply_only_when_missing(self):
        self.assertEqual(resource_admission.validate_worker_caps(None, None),
                         {'memory_mb': 1500, 'pids': 256})

    def test_valid_boundaries(self):
        self.assertEqual(resource_admission.validate_worker_caps(1500, 256),
                         {'memory_mb': 1500, 'pids': 256})
        self.assertEqual(resource_admission.validate_worker_caps(1, 1),
                         {'memory_mb': 1, 'pids': 1})

    def test_malformed_and_oversized_refuse_without_clamping(self):
        for memory, pids in ((1501, 256), (1500, 257), (0, 256), (1500, 0), (-1, 256),
                             (True, 256), ('1500', 256), (1500, True), (1500, '256'),
                             (1.5, 256), (1500, 2.0)):
            with self.assertRaises(ValueError):
                resource_admission.validate_worker_caps(memory, pids)


class ConcurrentAdmissionTests(unittest.TestCase):
    """Two real SQLite connections on ONE journal file; admission must
    serialize on BEGIN IMMEDIATE so simultaneous starts cannot overspend."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'journal.sqlite'
        self.journal = Journal(self.path)
        self.addCleanup(self.journal.db.close)
        launching_row(self.journal, 'operation-a', 'session-a')
        launching_row(self.journal, 'operation-b', 'session-b')

    def admit(self, bridge, operation, session, results, barrier):
        barrier.wait(5)
        try:
            results[operation] = resource_admission.admit_dispatch(
                bridge, operation, session, 1,
                {'memory_mb': 1500, 'pids': 256},
                census=None, sample=lambda deadline: self.available)
        except Exception as error:
            results[operation] = error

    def test_simultaneous_starts_fit_individually_but_never_both(self):
        # Enough physical headroom for exactly one new allowance above the floor.
        self.available = FLOOR + ALLOWANCE + 4096
        bridge_a = admit_bridge(self.journal, sample=lambda deadline: self.available)
        bridge_b = admit_bridge(self.journal, sample=lambda deadline: self.available)
        results, barrier = {}, threading.Barrier(2)
        threads = [threading.Thread(target=self.admit, args=(bridge, operation, session, results, barrier))
                   for bridge, operation, session in ((bridge_a, 'operation-a', 'session-a'),
                                                      (bridge_b, 'operation-b', 'session-b'))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
            self.assertFalse(thread.is_alive())
        admitted = [key for key, value in results.items() if isinstance(value, resource_admission.DispatchPermit)]
        refused = [key for key, value in results.items() if isinstance(value, RuntimeError)]
        self.assertEqual(len(admitted), 1)
        self.assertEqual(len(refused), 1)
        self.assertEqual(str(results[refused[0]]), 'host_launch_queue_full')
        operation, session = admitted[0], admitted[0].replace('operation', 'session')
        rows = {row['operation_id']: row['native_dispatch_started']
                for row in self.journal.db.execute('SELECT operation_id,native_dispatch_started FROM launches')}
        # Either thread may win the serialized transaction; exactly one
        # dispatch marker flips and only the winner holds a reservation.
        self.assertEqual(sorted(rows.values()), [0, 1])
        self.assertEqual(rows[operation], 1)
        reservations = self.journal.db.execute('SELECT operation_id,session_id,epoch,allowance_bytes '
                                               'FROM resource_reservations').fetchall()
        self.assertEqual([(row['operation_id'], row['session_id'], row['epoch'], row['allowance_bytes'])
                          for row in reservations], [(operation, session, 1, ALLOWANCE)])

    def test_reservation_is_durable_across_restart_and_private_per_journal(self):
        self.available = FLOOR + ALLOWANCE + 4096
        permit = resource_admission.admit_dispatch(
            admit_bridge(self.journal), 'operation-a', 'session-a', 1,
            {'memory_mb': 1500, 'pids': 256}, census=None, sample=lambda deadline: self.available)
        self.assertIsInstance(permit, resource_admission.DispatchPermit)
        self.journal.close_thread()
        self.journal.db.close()
        reopened = Journal(self.path)
        try:
            row = reopened.db.execute('SELECT operation_id,allowance_bytes FROM resource_reservations').fetchone()
            self.assertEqual((row['operation_id'], row['allowance_bytes']), ('operation-a', ALLOWANCE))
            self.assertEqual(reopened.db.execute(
                "SELECT native_dispatch_started FROM launches WHERE operation_id='operation-a'").fetchone()[0], 1)
            other = Journal(Path(self.temp.name) / 'other.sqlite')
            try:
                self.assertFalse(other.db.execute('SELECT 1 FROM resource_reservations').fetchone())
            finally:
                other.db.close()
        finally:
            reopened.close_thread()
            reopened.db.close()

    def test_stop_fenced_admission_refuses_before_marker_or_reservation(self):
        with self.journal.db as db:
            db.execute('INSERT INTO stop_fences VALUES(?,?)', ('session-a', 1))
        with self.assertRaisesRegex(RuntimeError, 'native_dispatch_fence_conflict'):
            resource_admission.admit_dispatch(
                admit_bridge(self.journal), 'operation-a', 'session-a', 1,
                {'memory_mb': 1500, 'pids': 256}, census=None,
                sample=lambda deadline: FLOOR + ALLOWANCE + 4096)
        self.assertEqual(self.journal.db.execute(
            "SELECT native_dispatch_started FROM launches WHERE operation_id='operation-a'").fetchone()[0], 0)
        self.assertFalse(self.journal.db.execute('SELECT 1 FROM resource_reservations').fetchone())

    def test_sampler_failure_rolls_back_cleanly(self):
        def broken(deadline):
            raise RuntimeError('host_operation_failed')
        with self.assertRaisesRegex(RuntimeError, 'host_operation_failed'):
            resource_admission.admit_dispatch(
                admit_bridge(self.journal), 'operation-a', 'session-a', 1,
                {'memory_mb': 1500, 'pids': 256}, census=None, sample=broken)
        self.assertEqual(self.journal.db.execute(
            "SELECT native_dispatch_started FROM launches WHERE operation_id='operation-a'").fetchone()[0], 0)
        self.assertFalse(self.journal.db.execute('SELECT 1 FROM resource_reservations').fetchone())

    def test_replayed_admission_never_reserves_twice(self):
        sample = lambda deadline: FLOOR + ALLOWANCE + 4096
        resource_admission.admit_dispatch(admit_bridge(self.journal), 'operation-a', 'session-a', 1,
                                          {'memory_mb': 1500, 'pids': 256}, census=None, sample=sample)
        with self.assertRaisesRegex(RuntimeError, 'native_dispatch_fence_conflict'):
            resource_admission.admit_dispatch(admit_bridge(self.journal), 'operation-a', 'session-a', 1,
                                              {'memory_mb': 1500, 'pids': 256}, census=None, sample=sample)
        self.assertEqual(self.journal.db.execute(
            'SELECT COUNT(*) FROM resource_reservations').fetchone()[0], 1)

    def test_mismatched_attempt_identity_refuses(self):
        for operation, session, epoch in (('operation-x', 'session-a', 1),
                                          ('operation-a', 'session-x', 1),
                                          ('operation-a', 'session-a', 2)):
            with self.assertRaisesRegex(RuntimeError, 'native_dispatch_fence_conflict'):
                resource_admission.admit_dispatch(admit_bridge(self.journal), operation, session, epoch,
                                                  {'memory_mb': 1500, 'pids': 256}, census=None,
                                                  sample=lambda deadline: FLOOR + ALLOWANCE + 4096)

    def test_active_bound_overrun_refuses_admission(self):
        with self.journal.db as db:
            for index in range(1001):
                db.execute('INSERT INTO launches(operation_id,session_id,epoch,digest,workspace,tag,native_id,'
                           "state,interactive,native_dispatch_started) VALUES(?,?,?,?,?,?,NULL,'running',0,1)",
                           (f'op{index}', f'session{index}', 1, 'digest', '/owned', f'task-hub2-session{index}'))
        with self.assertRaisesRegex(RuntimeError, 'active_native_bound_exceeded'):
            resource_admission.admit_dispatch(admit_bridge(self.journal), 'operation-a', 'session-a', 1,
                                              {'memory_mb': 1500, 'pids': 256}, census=None,
                                              sample=lambda deadline: FLOOR + 100 * ALLOWANCE)


class ResidentCreditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.journal = Journal(Path(self.temp.name) / 'journal.sqlite')
        self.addCleanup(self.journal.db.close)
        self.now = int(time.time() * 1000)

    def charges(self, observation, exclude='operation-new'):
        return resource_admission._uncovered_charges(self.journal.db, observation, exclude, self.now)

    def test_many_legacy_terminal_null_rows_never_poison_or_migrate(self):
        with self.journal.db as db:
            for index in range(200):
                db.execute('INSERT INTO launches(operation_id,session_id,epoch,digest,workspace,tag,native_id,'
                           'state,interactive,native_dispatch_started,legacy_failure_json) '
                           "VALUES(?,?,?,?,?,?,NULL,'exited',0,NULL,?)",
                           (f'legacy{index}', f'old{index}', 1, 'digest', '/gone', f'task-hub2-old{index}',
                            json.dumps({'operation_id': f'legacy{index}', 'epoch': 1})))
        self.assertEqual(self.charges(census([])), 0)
        self.assertFalse(self.journal.db.execute('SELECT 1 FROM resource_reservations').fetchone())

    def test_mature_proven_resident_is_not_double_charged(self):
        launching_row(self.journal, state='running', marker=1, native='1b2f9c3e-4d5a-46b7-8c9d-0e1f2a3b4c5d')
        reserve(self.journal, native='1b2f9c3e-4d5a-46b7-8c9d-0e1f2a3b4c5d')
        observation = census([resident('1b2f9c3e-4d5a-46b7-8c9d-0e1f2a3b4c5d')])
        self.assertEqual(self.charges(observation), 0)

    def test_unreserved_mature_resident_upgrade_row_is_credited_from_census(self):
        launching_row(self.journal, state='running', marker=1, native='1b2f9c3e-4d5a-46b7-8c9d-0e1f2a3b4c5d')
        observation = census([resident('1b2f9c3e-4d5a-46b7-8c9d-0e1f2a3b4c5d')])
        self.assertEqual(self.charges(observation), 0)

    def test_young_resident_keeps_full_conservative_allowance(self):
        launching_row(self.journal, state='running', marker=1, native='1b2f9c3e-4d5a-46b7-8c9d-0e1f2a3b4c5d')
        reserve(self.journal, native='1b2f9c3e-4d5a-46b7-8c9d-0e1f2a3b4c5d')
        observation = census([resident('1b2f9c3e-4d5a-46b7-8c9d-0e1f2a3b4c5d',
                                       birth_ms=self.now - 10_000)])
        self.assertEqual(self.charges(observation), ALLOWANCE)

    def test_unknown_current_inflight_keeps_full_allowance(self):
        launching_row(self.journal, marker=1)  # dispatched, unbound, no census row
        self.assertEqual(self.charges(census([])), ALLOWANCE)
        self.assertEqual(self.charges(None), ALLOWANCE)

    def test_duplicate_incarnation_credits_exactly_once(self):
        launching_row(self.journal, 'operation-a', 'session-a', state='running', marker=1,
                      native='1b2f9c3e-4d5a-46b7-8c9d-0e1f2a3b4c5d', tag='task-hub2-shared')
        launching_row(self.journal, 'operation-b', 'session-b', state='running', marker=1,
                      native='1b2f9c3e-4d5a-46b7-8c9d-0e1f2a3b4c5d', tag='task-hub2-shared')
        reserve(self.journal, 'operation-a', 'session-a', native='1b2f9c3e-4d5a-46b7-8c9d-0e1f2a3b4c5d')
        reserve(self.journal, 'operation-b', 'session-b', native='1b2f9c3e-4d5a-46b7-8c9d-0e1f2a3b4c5d')
        observation = census([resident('1b2f9c3e-4d5a-46b7-8c9d-0e1f2a3b4c5d', tag='task-hub2-shared')])
        self.assertEqual(self.charges(observation), ALLOWANCE)

    def test_birth_tag_or_workspace_mismatch_earns_no_credit(self):
        native = '1b2f9c3e-4d5a-46b7-8c9d-0e1f2a3b4c5d'
        launching_row(self.journal, state='running', marker=1, native=native)
        reserve(self.journal, native=native)
        for observation in (census([resident(native, tag='task-hub2-other')]),
                            census([resident(native, workspace='/elsewhere')]),
                            census([resident(native, birth_ms=self.now + 5_000)]),
                            census([resident(native, alive=False)]),
                            census([resident(native, alive=None)]),
                            census([resident(native, phase='exited')]),
                            census([resident(native, state='starting')]),
                            census([], fresh=False)):
            self.assertEqual(self.charges(observation), ALLOWANCE, msg=str(observation.rows))

    def test_missing_or_dead_census_refuses_credit_not_admission_evidence(self):
        launching_row(self.journal, 'operation-a', 'session-a', state='running', marker=1,
                      native='1b2f9c3e-4d5a-46b7-8c9d-0e1f2a3b4c5d')
        self.assertEqual(self.charges(None), ALLOWANCE)
        self.assertEqual(self.charges(census([resident('1b2f9c3e-4d5a-46b7-8c9d-0e1f2a3b4c5d')],
                                             fresh=False)), ALLOWANCE)


class PermitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.journal = Journal(Path(self.temp.name) / 'journal.sqlite')
        self.addCleanup(self.journal.db.close)

    def test_no_permit_verifies_trivially(self):
        self.assertIsNone(resource_admission.verify_permit(None, journal=self.journal))

    def test_expired_permit_refuses_without_renewing_the_clock(self):
        permit = resource_admission.DispatchPermit('operation-1', 'session-1', 1, time.monotonic() - 31)
        self.assertTrue(permit.expired())
        with self.assertRaisesRegex(RuntimeError, 'native_start_uncertain'):
            resource_admission.verify_permit(permit, journal=self.journal)
        time.sleep(0.01)
        with self.assertRaisesRegex(RuntimeError, 'native_start_uncertain'):
            resource_admission.verify_permit(permit, journal=self.journal)  # never renewed

    def test_fresh_permit_requires_unfenced_launching_row(self):
        launching_row(self.journal)
        permit = resource_admission.DispatchPermit('operation-1', 'session-1', 1, time.monotonic())
        self.assertIsNone(resource_admission.verify_permit(permit, journal=self.journal))
        with self.journal.db as db:
            db.execute("UPDATE launches SET state='stopped_before_dispatch'")
        with self.assertRaisesRegex(RuntimeError, 'native_start_uncertain'):
            resource_admission.verify_permit(permit, journal=self.journal)
        with self.journal.db as db:
            db.execute("UPDATE launches SET state='launching'")
            db.execute('INSERT INTO stop_fences VALUES(?,?)', ('session-1', 1))
        with self.assertRaisesRegex(RuntimeError, 'native_start_uncertain'):
            resource_admission.verify_permit(permit, journal=self.journal)

    def test_permit_without_journal_checks_age_only(self):
        permit = resource_admission.DispatchPermit('operation-1', 'session-1', 1, time.monotonic())
        self.assertIsNone(resource_admission.verify_permit(permit, journal=None))


class ReleaseCustodyTests(unittest.TestCase):
    """Release only in evidence-qualified terminal transactions; uncertain
    custody (aborting, expired, unknown) never refunds a reservation."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.journal = Journal(Path(self.temp.name) / 'journal.sqlite')
        self.addCleanup(self.journal.db.close)

    def reservations(self):
        return {row[0] for row in self.journal.db.execute('SELECT operation_id FROM resource_reservations')}

    def test_exact_key_release_only(self):
        reserve(self.journal, 'operation-a', 'session-a')
        reserve(self.journal, 'operation-b', 'session-b')
        resource_admission.release_reservation(self.journal.db, 'operation-a', 'session-a', 1)
        self.journal.db.commit()
        self.assertEqual(self.reservations(), {'operation-b'})
        resource_admission.release_reservation(self.journal.db, 'operation-a', 'session-x', 1)
        self.journal.db.commit()
        self.assertEqual(self.reservations(), {'operation-b'})

    def test_settled_no_dispatch_releases_exactly_and_keeps_other_attempts(self):
        fixture = test_host.HostTests('runTest')
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        launching_row(self.journal, 'operation-orphan', 'session-orphan', 7, marker=0)
        reserve(self.journal, 'operation-orphan', 'session-orphan', 7)
        reserve(self.journal, 'operation-held', 'session-held', 3)
        row = self.journal.db.execute("SELECT * FROM launches WHERE operation_id='operation-orphan'").fetchone()
        fixture.journal = self.journal
        fixture.bridge.journal = self.journal
        fixture.bridge.settle_undispatched_launch(row)
        settled = self.journal.db.execute(
            "SELECT state FROM launches WHERE operation_id='operation-orphan'").fetchone()[0]
        self.assertEqual(settled, 'stopped_before_dispatch')
        self.assertEqual(self.reservations(), {'operation-held'})
        payloads = self.journal.db.execute('SELECT payload FROM outbox').fetchall()
        self.assertEqual(len(payloads), 1)
        self.assertTrue(json.loads(payloads[0][0])['no_dispatch'])

    def test_stop_fenced_before_dispatch_releases_exactly(self):
        fixture = test_host.HostTests('runTest')
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        fixture.journal = self.journal
        fixture.bridge.journal = self.journal
        launching_row(self.journal, marker=0)
        reserve(self.journal)
        reserve(self.journal, 'operation-held', 'session-held', 3)
        result = fixture.bridge.command({'id': 'stop-1', 'session_id': 'session-1',
                                         'epoch': 1, 'kind': 'stop'})
        self.assertEqual(result, {'stopped': True, 'evidence': 'dispatch_fenced'})
        self.assertEqual(self.reservations(), {'operation-held'})


if __name__ == '__main__':
    unittest.main()
