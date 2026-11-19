"""Same-journal pre-dispatch RAM admission: OS samplers, caps, serialization,
resident credit and release custody. All OS and census evidence here is
bounded synthetic injection; no provider, model, API or live participant."""
import ctypes
import hashlib
import json
import os
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
DEFAULT = ALLOWANCE



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
            native=None, allowance=ALLOWANCE, digest='digest', attempt='attempt'):
    with journal.db as db:
        db.execute('INSERT INTO resource_reservations(operation_id,session_id,epoch,digest,attempt,'
                   'native_id,allowance_bytes,updated_at_ms) VALUES(?,?,?,?,?,?,?,?)',
                   (operation, session, epoch, digest, attempt, native, allowance, int(time.time() * 1000)))


def observation(rows, collected_offset=0.0):
    """Private raw observation shape: (rows, collection monotonic)."""
    return (tuple(rows), time.monotonic() - collected_offset)


def os_proof(self, births=None, cwds=None, alive=True):
    """Deterministic positive OS incarnation proof seams for fake pids."""
    births = births or {}
    cwds = cwds or {}
    patches = [patch.object(resource_admission, '_process_birth_ms',
                            side_effect=lambda pid, read=None: births.get(pid)),
               patch.object(resource_admission, '_process_cwd',
                            side_effect=lambda pid: cwds.get(pid)),
               patch.object(resource_admission, '_process_alive',
                            side_effect=lambda pid: alive)]
    for item in patches:
        item.start()
        self.addCleanup(item.stop)


def resident(native_id, tag='task-hub2-session-1', workspace='/owned', birth_ms=None, alive=True,
             phase='running', state='running', pid=4100):
    row = {'id': native_id, 'tag': tag, 'workspace': workspace,
           'phase': phase, 'state': state, 'worker_alive': alive,
           'created_at_ms': int(time.time() * 1000) - 400_000 if birth_ms is None else birth_ms,
           'schema_version': 1, 'workload_pid': pid}
    return {key: value for key, value in row.items() if value is not None or key != 'workload_pid'}


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
        # Known-field duplicates refuse whatever their shape, including wrong
        # units or extra tokens after an already-valid field.
        with self.assertRaises(RuntimeError):
            resource_admission.parse_linux_meminfo(b'MemTotal: 100 kB\nMemAvailable: 5 kB\nMemAvailable: 6 MB\n')
        with self.assertRaises(RuntimeError):
            resource_admission.parse_linux_meminfo(b'MemTotal: 100 kB\nMemAvailable: 5 kB junk\n')
        with self.assertRaises(RuntimeError):
            resource_admission.parse_linux_meminfo(b'MemAvailable: 5 MB\nMemTotal: 100 kB\n')
        with self.assertRaises(RuntimeError):
            resource_admission.parse_linux_meminfo(b'MemAvailable:\nMemTotal: 100 kB\n')
        with self.assertRaises(RuntimeError):
            resource_admission.parse_linux_meminfo(b'MemAvailable kB\nMemTotal: 100 kB\n')

    def test_injected_read_is_platform_independent(self):
        self.assertEqual(resource_admission.sample_linux(
            read=lambda: self.meminfo(7, 10)), 7 * 1024)

    @unittest.skipUnless(sys.platform == 'linux', 'real Linux /proc sample')
    def test_real_linux_meminfo_sample_is_positive(self):
        available = resource_admission.sample_linux()
        self.assertIsInstance(available, int)
        self.assertGreater(available, 0)


class RawCensusParseTests(unittest.TestCase):
    def row(self, **changes):
        base = {'id': '1b2f9c3e-4d5a-46b7-8c9d-0e1f2a3b4c5d', 'tag': 'task-hub2-session-1',
                'workspace': '/owned', 'schema_version': 1, 'phase': 'running',
                'state': 'running', 'worker_alive': True,
                'created_at_ms': int(time.time() * 1000) - 400_000, 'workload_pid': 4200}
        base.update(changes)
        return json.dumps([base])

    def test_valid_rows_keep_the_workload_coordinate(self):
        rows = resource_admission.parse_raw_census(self.row())
        self.assertEqual(rows[0]['workload_pid'], 4200)
        self.assertEqual(rows[0]['id'], '1b2f9c3e-4d5a-46b7-8c9d-0e1f2a3b4c5d')
        self.assertEqual(resource_admission.parse_raw_census('[]'), ())

    def test_duplicate_json_keys_refuse(self):
        with self.assertRaises(RuntimeError):
            resource_admission.parse_raw_census(
                '[{"id": "1b2f9c3e-4d5a-46b7-8c9d-0e1f2a3b4c5d", "id": "1b2f9c3e-4d5a-46b7-8c9d-0e1f2a3b4c5d"}]')

    def test_malformed_observations_refuse_wholesale(self):
        native = '1b2f9c3e-4d5a-46b7-8c9d-0e1f2a3b4c5d'
        for raw in (None, 42, 'not json', '[]extra', '"x"',
                    self.row(id='not-a-uuid'),
                    '[{"id": "%s", "tag": "t", "workspace": "/a", "schema_version": 1},'
                    ' {"id": "%s", "tag": "t", "workspace": "/b", "schema_version": 1}]' % (native, native),
                    self.row(schema_version=0),
                    self.row(phase='teleporting'),
                    self.row(state='teleporting'),
                    self.row(worker_alive='yes'),
                    self.row(created_at_ms=-1),
                    self.row(created_at_ms=True),
                    self.row(created_at_ms='123'),
                    self.row(workload_pid=0),
                    self.row(workload_pid=-2),
                    self.row(workload_pid='4200'),
                    self.row(workspace='relative/path'),
                    self.row(tag=''),
                    json.dumps([{**json.loads(self.row())[0], 'workspace': '/x' * 9000}])):
            with self.assertRaises(RuntimeError, msg=str(raw)[:80]):
                resource_admission.parse_raw_census(raw)

    def test_oversized_observations_refuse(self):
        with self.assertRaises(RuntimeError):
            resource_admission.parse_raw_census('[' * (17 * 1024 * 1024))


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


class OsIncarnationProofTests(unittest.TestCase):
    def stat(self, start_ticks=500000, comm='engin'):
        tail = ['S'] + ['1'] * 16 + ['0'] * 19
        tail[19] = str(start_ticks)  # field 22 starttime after the comm paren
        return ' 4242 (%s) %s' % (comm, ' '.join(tail))

    def read_kernel(self, start_ticks=500000, btime=1000000, comm='engin', stat_bytes=None):
        stat_value = stat_bytes if stat_bytes is not None else self.stat(start_ticks)
        files = {'/proc/4242/stat': stat_value.encode() if isinstance(stat_value, str) else stat_value,
                 '/proc/stat': ('btime %d\ncpu 1 2 3\n' % btime).encode()}
        return lambda path: files[path]

    def test_kernel_birth_matches_observed_birth_within_tolerance(self):
        birth = resource_admission._linux_birth_ms(4242, read=self.read_kernel())
        self.assertEqual(birth, 1000000 * 1000 + (500000 * 1000) // 100)
        with patch.object(os, 'sysconf', return_value=250):
            birth = resource_admission._linux_birth_ms(4242, read=self.read_kernel())
        self.assertEqual(birth, 1000000 * 1000 + (500000 * 1000) // 250)

    def test_parenthesised_comm_is_parsed_with_rindex(self):
        # The comm itself contains ')'; rindex must find the true closing paren.
        raw = (' 4242 (engin) e) S ' + ' '.join(['1'] * 18) + ' 500000').encode()
        birth = resource_admission._linux_birth_ms(4242, read=self.read_kernel(stat_bytes=raw))
        self.assertEqual(birth, 1000000 * 1000 + 5_000_000)

    def test_missing_malformed_or_unbounded_birth_refuses(self):
        for read in (self.read_kernel(btime=None) if False else
                     lambda path: (self.stat().encode() if path.endswith('/stat')
                                   else b'cpu 1 2 3\n'),
                     lambda path: (self.stat().encode() if path.endswith('/stat')
                                   else b'btime -1\n'),
                     lambda path: (self.stat().encode() if path.endswith('/stat')
                                   else b'btime 1 2\n'),
                     lambda path: b' 4242 (x) S 1',  # truncated tail
                     lambda path: (' 4242 (x) S ' + ' '.join(['1'] * 16) + ' later').encode(),
                     lambda path: b''):
            self.assertIsNone(resource_admission._linux_birth_ms(4242, read=read))

    @unittest.skipUnless(sys.platform == 'linux', 'real Linux /proc probe')
    def test_real_missing_process_has_no_birth_proof(self):
        self.assertIsNone(resource_admission._linux_birth_ms(999999999))
        self.assertIsNone(resource_admission._process_birth_ms(999999999))
        self.assertFalse(resource_admission._process_alive(999999999))
        self.assertIsNone(resource_admission._process_cwd(999999999))

    def test_synthetic_windows_identity_protocol_control(self):
        calls = []
        def open_process(flag, inherit, pid):
            calls.append(('open', flag, pid))
            return 7 if pid == 4242 else 0
        def get_times(handle, creation, exit_time, kernel_time, user_time):
            creation._obj.value = (1_700_000_000_000 + resource_admission._WINDOWS_EPOCH_FILETIME_MS) * 10000
            calls.append(('times', handle))
            return 1
        def close_handle(handle):
            calls.append(('close', handle))
            return 1
        birth = resource_admission._windows_creation_ms(open_process, get_times, close_handle, 4242)
        self.assertEqual(birth, 1_700_000_000_000)
        self.assertEqual(calls, [('open', 0x1000, 4242), ('times', 7), ('close', 7)])
        calls.clear()
        self.assertIsNone(resource_admission._windows_creation_ms(lambda *args: 0, get_times, close_handle, 1))
        self.assertIsNone(resource_admission._windows_creation_ms(open_process, lambda *args: 0, close_handle, 4242))
        self.assertEqual(calls, [('open', 0x1000, 4242), ('close', 7)])

    def test_windows_birth_path_is_platform_gated(self):
        if sys.platform == 'win32':
            self.skipTest('real win32 platform')
        self.assertIsNone(resource_admission._windows_birth_ms(4242))
        self.assertIsNone(resource_admission._process_birth_ms(4242))


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

    def test_simultaneous_preparations_serialize_and_cannot_double_spend(self):
        # Each unreserved preparer is conservatively charged (plan: undispatched
        # pending preparation is reserved while it can launch concurrently), so
        # the sample covers exactly the two accounts; the serialized commits
        # land both without spending any account twice.
        self.available = FLOOR + 2 * ALLOWANCE + 4096
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
        self.assertEqual(sorted(admitted), ['operation-a', 'operation-b'])
        rows = {row['operation_id']: row['native_dispatch_started']
                for row in self.journal.db.execute('SELECT operation_id,native_dispatch_started FROM launches')}
        # Either thread may win the serialized transaction; both commit exactly
        # once and no account is spent twice.
        self.assertEqual(sorted(rows.values()), [1, 1])
        reservations = self.journal.db.execute('SELECT operation_id,session_id,epoch,allowance_bytes '
                                               'FROM resource_reservations ORDER BY operation_id').fetchall()
        self.assertEqual([(row['operation_id'], row['session_id'], row['epoch'], row['allowance_bytes'])
                          for row in reservations],
                         [('operation-a', 'session-a', 1, ALLOWANCE), ('operation-b', 'session-b', 1, ALLOWANCE)])

    def test_serial_admissions_fit_individually_but_never_together(self):
        # One new account of headroom: the first dispatch commits its own
        # reservation; the second sees that charge (plus its own) and refuses
        # with the typed capacity code - exactly one native effect is possible.
        with self.journal.db as db:
            db.execute("DELETE FROM launches WHERE operation_id='operation-b'")
        self.available = FLOOR + ALLOWANCE + 4096
        bridge_a = admit_bridge(self.journal, sample=lambda deadline: self.available)
        bridge_b = admit_bridge(self.journal, sample=lambda deadline: self.available)
        permit = resource_admission.admit_dispatch(bridge_a, 'operation-a', 'session-a', 1,
                                                   {'memory_mb': 1500, 'pids': 256},
                                                   census=None, sample=lambda deadline: self.available)
        self.assertIsInstance(permit, resource_admission.DispatchPermit)
        # The second attempt arrives as a fresh prepared row afterwards.
        launching_row(self.journal, 'operation-b', 'session-b')
        with self.assertRaisesRegex(RuntimeError, 'host_launch_queue_full'):
            resource_admission.admit_dispatch(bridge_b, 'operation-b', 'session-b', 1,
                                              {'memory_mb': 1500, 'pids': 256},
                                              census=None, sample=lambda deadline: self.available)
        rows = {row['operation_id']: row['native_dispatch_started']
                for row in self.journal.db.execute('SELECT operation_id,native_dispatch_started FROM launches')}
        self.assertEqual(rows, {'operation-a': 1, 'operation-b': 0})
        self.assertEqual(self.journal.db.execute(
            'SELECT COUNT(*) FROM resource_reservations').fetchone()[0], 1)

    def test_reservation_is_durable_across_restart_and_private_per_journal(self):
        self.available = FLOOR + 2 * ALLOWANCE + 4096  # sibling preparer is conservatively charged
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
                sample=lambda deadline: FLOOR + 2 * ALLOWANCE + 4096)
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
        sample = lambda deadline: FLOOR + 2 * ALLOWANCE + 4096
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
                                                  sample=lambda deadline: FLOOR + 2 * ALLOWANCE + 4096)

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

    def charges(self, observation, exclude='operation-new', attempt_label=None):
        return resource_admission._uncovered_charges(self.journal.db, observation, exclude, self.now,
                                                     attempt_label=attempt_label)

    def test_many_legacy_terminal_null_rows_never_poison_or_migrate(self):
        with self.journal.db as db:
            for index in range(200):
                db.execute('INSERT INTO launches(operation_id,session_id,epoch,digest,workspace,tag,native_id,'
                           "state,interactive,native_dispatch_started,legacy_failure_json) "
                           "VALUES(?,?,?,?,?,?,NULL,'exited',0,NULL,?)",
                           (f'legacy{index}', f'old{index}', 1, 'digest', '/gone', f'task-hub2-old{index}',
                            json.dumps({'operation_id': f'legacy{index}', 'epoch': 1})))
        os_proof(self, )
        self.assertEqual(self.charges(observation([])), 0)
        self.assertFalse(self.journal.db.execute('SELECT 1 FROM resource_reservations').fetchone())

    def proven(self, native, pid, tag='task-hub2-session-1', birth_ms=None):
        """Raw observation row plus matching positive OS proof seams."""
        birth_ms = self.now - 400_000 if birth_ms is None else birth_ms
        os_proof(self, births={pid: birth_ms}, cwds={pid: '/owned'})
        return observation([resident(native, tag=tag, birth_ms=birth_ms, pid=pid)])

    def test_mature_os_proven_resident_is_not_double_charged(self):
        native = '1b2f9c3e-4d5a-46b7-8c9d-0e1f2a3b4c5d'
        launching_row(self.journal, state='running', marker=1, native=native)
        reserve(self.journal, native=native)
        self.assertEqual(self.charges(self.proven(native, 4200)), 0)

    def test_unreserved_mature_resident_upgrade_row_is_credited_with_os_proof(self):
        native = '1b2f9c3e-4d5a-46b7-8c9d-0e1f2a3b4c5d'
        launching_row(self.journal, state='running', marker=1, native=native)
        self.assertEqual(self.charges(self.proven(native, 4300)), 0)

    def test_young_resident_keeps_full_conservative_allowance(self):
        native = '1b2f9c3e-4d5a-46b7-8c9d-0e1f2a3b4c5d'
        launching_row(self.journal, state='running', marker=1, native=native)
        reserve(self.journal, native=native)
        self.assertEqual(self.charges(self.proven(native, 4400, birth_ms=self.now - 10_000)), ALLOWANCE)

    def test_retained_expired_unconfirmed_reservation_keeps_its_charge(self):
        native = '1b2f9c3e-4d5a-46b7-8c9d-0e1f2a3b4c5d'
        # Terminal-looking custody: the row settled expired_unconfirmed but the
        # guard-owned reservation was never released, so the charge remains.
        launching_row(self.journal, state='expired_unconfirmed', marker=1, native=native)
        reserve(self.journal, native=native)
        self.assertEqual(self.charges(observation([])), ALLOWANCE)
        self.assertEqual(self.charges(((), None)), ALLOWANCE)

    def test_active_legacy_null_marker_row_is_charged_and_can_be_os_proven(self):
        native = '1b2f9c3e-4d5a-46b7-8c9d-0e1f2a3b4c5d'
        launching_row(self.journal, state='running', marker=None, native=native)  # pre-feature legacy row
        self.assertEqual(self.charges(observation([])), ALLOWANCE)
        self.assertEqual(self.charges(self.proven(native, 4800)), 0)

    def test_invalid_or_orphaned_reservation_tuple_is_unknown_never_zero(self):
        launching_row(self.journal, state='running', marker=1,
                      native='1b2f9c3e-4d5a-46b7-8c9d-0e1f2a3b4c5d')
        # A reduced allowance distinguishes validated charges from the
        # conservative unknown default.
        reserve(self.journal, digest='mismatched-digest', native='1b2f9c3e-4d5a-46b7-8c9d-0e1f2a3b4c5d',
                allowance=100 * 1024 * 1024)
        self.assertEqual(self.charges(observation([])), DEFAULT)  # digest mismatch -> unknown
        reserve(self.journal, 'operation-orphan', 'session-orphan', 2, allowance=100 * 1024 * 1024)
        with self.journal.db as db:
            db.execute("DELETE FROM launches WHERE operation_id='operation-orphan'")
        self.assertEqual(self.charges(observation([])), DEFAULT + DEFAULT)  # orphaned -> unknown, never zero

    def test_attempt_label_proof_validated_when_derivable(self):
        native = '1b2f9c3e-4d5a-46b7-8c9d-0e1f2a3b4c5d'
        launching_row(self.journal, state='running', marker=1, native=native)
        reserve(self.journal, native=native, allowance=100 * 1024 * 1024)
        ok = lambda session, operation, epoch: 'attempt'
        self.assertEqual(self.charges(observation([]), attempt_label=ok), 100 * 1024 * 1024)
        bad = lambda session, operation, epoch: 'different'
        self.assertEqual(self.charges(observation([]), attempt_label=bad), DEFAULT)

    def test_unknown_current_inflight_keeps_full_allowance(self):
        launching_row(self.journal, marker=1)  # dispatched, unbound, no observation row
        self.assertEqual(self.charges(observation([])), ALLOWANCE)
        self.assertEqual(self.charges(((), None)), ALLOWANCE)
        self.assertEqual(self.charges(None), ALLOWANCE)

    def test_sdk_projection_without_positive_os_proof_is_never_zero(self):
        native = '1b2f9c3e-4d5a-46b7-8c9d-0e1f2a3b4c5d'
        launching_row(self.journal, state='running', marker=1, native=native)
        reserve(self.journal, native=native)
        row = resident(native, pid=4500)
        # Matching tag/workspace/alive/birth/age from the observation alone,
        # but every positive OS step missing or contradictory: full charge.
        os_proof(self, )  # no kernel birth, no cwd, not alive
        self.assertEqual(self.charges(observation([row])), ALLOWANCE)
        os_proof(self, births={4500: self.now - 400_000})  # birth ok, no cwd
        self.assertEqual(self.charges(observation([row])), ALLOWANCE)
        os_proof(self, births={4500: self.now - 400_000}, cwds={4500: '/elsewhere'})
        self.assertEqual(self.charges(observation([row])), ALLOWANCE)
        os_proof(self, births={4500: self.now - 400_000 - 60_000}, cwds={4500: '/owned'})  # birth beyond tolerance
        self.assertEqual(self.charges(observation([row])), ALLOWANCE)
        os_proof(self, births={4500: self.now - 400_000}, cwds={4500: '/owned'}, alive=False)  # died at the boundary
        self.assertEqual(self.charges(observation([row])), ALLOWANCE)

    def test_duplicate_incarnation_credits_exactly_once(self):
        native = '1b2f9c3e-4d5a-46b7-8c9d-0e1f2a3b4c5d'
        launching_row(self.journal, 'operation-a', 'session-a', state='running', marker=1,
                      native=native, tag='task-hub2-shared')
        launching_row(self.journal, 'operation-b', 'session-b', state='running', marker=1,
                      native=native, tag='task-hub2-shared')
        reserve(self.journal, 'operation-a', 'session-a', native=native)
        reserve(self.journal, 'operation-b', 'session-b', native=native)
        self.assertEqual(self.charges(self.proven(native, 4600, tag='task-hub2-shared')), ALLOWANCE)

    def test_identity_or_stage_mismatch_earns_no_credit(self):
        native = '1b2f9c3e-4d5a-46b7-8c9d-0e1f2a3b4c5d'
        launching_row(self.journal, state='running', marker=1, native=native)
        reserve(self.journal, native=native)
        os_proof(self, births={4700: self.now - 400_000}, cwds={4700: '/owned'})
        for rows in (observation([resident(native, tag='task-hub2-other', pid=4700)]),
                     observation([resident(native, workspace='/elsewhere', pid=4700)]),
                     observation([resident(native, birth_ms=self.now + 5_000, pid=4700)]),
                     observation([resident(native, alive=False, pid=4700)]),
                     observation([resident(native, alive=None, pid=4700)]),
                     observation([resident(native, phase='exited', pid=4700)]),
                     observation([resident(native, state='starting', pid=4700)]),
                     observation([resident(native, pid=None)]),
                     observation([], collected_offset=30.0)):  # stale at the final boundary
            self.assertEqual(self.charges(rows), ALLOWANCE, msg=str(rows))


class PermitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.journal = Journal(Path(self.temp.name) / 'journal.sqlite')
        self.addCleanup(self.journal.db.close)

    def test_no_permit_verifies_trivially(self):
        self.assertIsNone(resource_admission.verify_permit(None, journal=self.journal))

    def test_expired_permit_refuses_without_renewing_the_clock(self):
        permit = resource_admission.DispatchPermit('operation-1', 'session-1', 1,
                                                   permit_deadline=time.monotonic() - 1)
        self.assertTrue(permit.expired())
        with self.assertRaisesRegex(RuntimeError, 'native_start_uncertain'):
            resource_admission.verify_permit(permit, journal=self.journal)
        time.sleep(0.01)
        with self.assertRaisesRegex(RuntimeError, 'native_start_uncertain'):
            resource_admission.verify_permit(permit, journal=self.journal)  # never renewed

    def test_fresh_permit_requires_unfenced_launching_row(self):
        launching_row(self.journal)
        permit = resource_admission.DispatchPermit('operation-1', 'session-1', 1,
                                                   permit_deadline=time.monotonic() + 30)
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
        permit = resource_admission.DispatchPermit('operation-1', 'session-1', 1,
                                                   permit_deadline=time.monotonic() + 30)
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

    def test_simultaneous_preparations_serialize_and_cannot_double_spend(self):
        # Each unreserved preparer is conservatively charged (plan: undispatched
        # pending preparation is reserved while it can launch concurrently), so
        # the sample covers exactly the two accounts; the serialized commits
        # land both without spending any account twice.
        self.available = FLOOR + 2 * ALLOWANCE + 4096
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
        self.assertEqual(sorted(admitted), ['operation-a', 'operation-b'])
        rows = {row['operation_id']: row['native_dispatch_started']
                for row in self.journal.db.execute('SELECT operation_id,native_dispatch_started FROM launches')}
        # Either thread may win the serialized transaction; both commit exactly
        # once and no account is spent twice.
        self.assertEqual(sorted(rows.values()), [1, 1])
        reservations = self.journal.db.execute('SELECT operation_id,session_id,epoch,allowance_bytes '
                                               'FROM resource_reservations ORDER BY operation_id').fetchall()
        self.assertEqual([(row['operation_id'], row['session_id'], row['epoch'], row['allowance_bytes'])
                          for row in reservations],
                         [('operation-a', 'session-a', 1, ALLOWANCE), ('operation-b', 'session-b', 1, ALLOWANCE)])

    def test_serial_admissions_fit_individually_but_never_together(self):
        # One new account of headroom: the first dispatch commits its own
        # reservation; the second sees that charge (plus its own) and refuses
        # with the typed capacity code - exactly one native effect is possible.
        with self.journal.db as db:
            db.execute("DELETE FROM launches WHERE operation_id='operation-b'")
        self.available = FLOOR + ALLOWANCE + 4096
        bridge_a = admit_bridge(self.journal, sample=lambda deadline: self.available)
        bridge_b = admit_bridge(self.journal, sample=lambda deadline: self.available)
        permit = resource_admission.admit_dispatch(bridge_a, 'operation-a', 'session-a', 1,
                                                   {'memory_mb': 1500, 'pids': 256},
                                                   census=None, sample=lambda deadline: self.available)
        self.assertIsInstance(permit, resource_admission.DispatchPermit)
        # The second attempt arrives as a fresh prepared row afterwards.
        launching_row(self.journal, 'operation-b', 'session-b')
        with self.assertRaisesRegex(RuntimeError, 'host_launch_queue_full'):
            resource_admission.admit_dispatch(bridge_b, 'operation-b', 'session-b', 1,
                                              {'memory_mb': 1500, 'pids': 256},
                                              census=None, sample=lambda deadline: self.available)
        rows = {row['operation_id']: row['native_dispatch_started']
                for row in self.journal.db.execute('SELECT operation_id,native_dispatch_started FROM launches')}
        self.assertEqual(rows, {'operation-a': 1, 'operation-b': 0})
        self.assertEqual(self.journal.db.execute(
            'SELECT COUNT(*) FROM resource_reservations').fetchone()[0], 1)

    def test_reservation_is_durable_across_restart_and_private_per_journal(self):
        self.available = FLOOR + 2 * ALLOWANCE + 4096  # sibling preparer is conservatively charged
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
                sample=lambda deadline: FLOOR + 2 * ALLOWANCE + 4096)
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
        sample = lambda deadline: FLOOR + 2 * ALLOWANCE + 4096
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
                                                  sample=lambda deadline: FLOOR + 2 * ALLOWANCE + 4096)

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

    def charges(self, observation, exclude='operation-new', attempt_label=None):
        return resource_admission._uncovered_charges(self.journal.db, observation, exclude, self.now,
                                                     attempt_label=attempt_label)

    def test_many_legacy_terminal_null_rows_never_poison_or_migrate(self):
        with self.journal.db as db:
            for index in range(200):
                db.execute('INSERT INTO launches(operation_id,session_id,epoch,digest,workspace,tag,native_id,'
                           "state,interactive,native_dispatch_started,legacy_failure_json) "
                           "VALUES(?,?,?,?,?,?,NULL,'exited',0,NULL,?)",
                           (f'legacy{index}', f'old{index}', 1, 'digest', '/gone', f'task-hub2-old{index}',
                            json.dumps({'operation_id': f'legacy{index}', 'epoch': 1})))
        os_proof(self, )
        self.assertEqual(self.charges(observation([])), 0)
        self.assertFalse(self.journal.db.execute('SELECT 1 FROM resource_reservations').fetchone())

    def proven(self, native, pid, tag='task-hub2-session-1', birth_ms=None):
        """Raw observation row plus matching positive OS proof seams."""
        birth_ms = self.now - 400_000 if birth_ms is None else birth_ms
        os_proof(self, births={pid: birth_ms}, cwds={pid: '/owned'})
        return observation([resident(native, tag=tag, birth_ms=birth_ms, pid=pid)])

    def test_mature_os_proven_resident_is_not_double_charged(self):
        native = '1b2f9c3e-4d5a-46b7-8c9d-0e1f2a3b4c5d'
        launching_row(self.journal, state='running', marker=1, native=native)
        reserve(self.journal, native=native)
        self.assertEqual(self.charges(self.proven(native, 4200)), 0)

    def test_unreserved_mature_resident_upgrade_row_is_credited_with_os_proof(self):
        native = '1b2f9c3e-4d5a-46b7-8c9d-0e1f2a3b4c5d'
        launching_row(self.journal, state='running', marker=1, native=native)
        self.assertEqual(self.charges(self.proven(native, 4300)), 0)

    def test_young_resident_keeps_full_conservative_allowance(self):
        native = '1b2f9c3e-4d5a-46b7-8c9d-0e1f2a3b4c5d'
        launching_row(self.journal, state='running', marker=1, native=native)
        reserve(self.journal, native=native)
        self.assertEqual(self.charges(self.proven(native, 4400, birth_ms=self.now - 10_000)), ALLOWANCE)

    def test_retained_expired_unconfirmed_reservation_keeps_its_charge(self):
        native = '1b2f9c3e-4d5a-46b7-8c9d-0e1f2a3b4c5d'
        # Terminal-looking custody: the row settled expired_unconfirmed but the
        # guard-owned reservation was never released, so the charge remains.
        launching_row(self.journal, state='expired_unconfirmed', marker=1, native=native)
        reserve(self.journal, native=native)
        self.assertEqual(self.charges(observation([])), ALLOWANCE)
        self.assertEqual(self.charges(((), None)), ALLOWANCE)

    def test_active_legacy_null_marker_row_is_charged_and_can_be_os_proven(self):
        native = '1b2f9c3e-4d5a-46b7-8c9d-0e1f2a3b4c5d'
        launching_row(self.journal, state='running', marker=None, native=native)  # pre-feature legacy row
        self.assertEqual(self.charges(observation([])), ALLOWANCE)
        self.assertEqual(self.charges(self.proven(native, 4800)), 0)

    def test_invalid_or_orphaned_reservation_tuple_is_unknown_never_zero(self):
        launching_row(self.journal, state='running', marker=1,
                      native='1b2f9c3e-4d5a-46b7-8c9d-0e1f2a3b4c5d')
        # A reduced allowance distinguishes validated charges from the
        # conservative unknown default.
        reserve(self.journal, digest='mismatched-digest', native='1b2f9c3e-4d5a-46b7-8c9d-0e1f2a3b4c5d',
                allowance=100 * 1024 * 1024)
        self.assertEqual(self.charges(observation([])), DEFAULT)  # digest mismatch -> unknown
        reserve(self.journal, 'operation-orphan', 'session-orphan', 2, allowance=100 * 1024 * 1024)
        with self.journal.db as db:
            db.execute("DELETE FROM launches WHERE operation_id='operation-orphan'")
        self.assertEqual(self.charges(observation([])), DEFAULT + DEFAULT)  # orphaned -> unknown, never zero

    def test_attempt_label_proof_validated_when_derivable(self):
        native = '1b2f9c3e-4d5a-46b7-8c9d-0e1f2a3b4c5d'
        launching_row(self.journal, state='running', marker=1, native=native)
        reserve(self.journal, native=native, allowance=100 * 1024 * 1024)
        ok = lambda session, operation, epoch: 'attempt'
        self.assertEqual(self.charges(observation([]), attempt_label=ok), 100 * 1024 * 1024)
        bad = lambda session, operation, epoch: 'different'
        self.assertEqual(self.charges(observation([]), attempt_label=bad), DEFAULT)

    def test_unknown_current_inflight_keeps_full_allowance(self):
        launching_row(self.journal, marker=1)  # dispatched, unbound, no observation row
        self.assertEqual(self.charges(observation([])), ALLOWANCE)
        self.assertEqual(self.charges(((), None)), ALLOWANCE)
        self.assertEqual(self.charges(None), ALLOWANCE)

    def test_sdk_projection_without_positive_os_proof_is_never_zero(self):
        native = '1b2f9c3e-4d5a-46b7-8c9d-0e1f2a3b4c5d'
        launching_row(self.journal, state='running', marker=1, native=native)
        reserve(self.journal, native=native)
        row = resident(native, pid=4500)
        # Matching tag/workspace/alive/birth/age from the observation alone,
        # but every positive OS step missing or contradictory: full charge.
        os_proof(self, )  # no kernel birth, no cwd, not alive
        self.assertEqual(self.charges(observation([row])), ALLOWANCE)
        os_proof(self, births={4500: self.now - 400_000})  # birth ok, no cwd
        self.assertEqual(self.charges(observation([row])), ALLOWANCE)
        os_proof(self, births={4500: self.now - 400_000}, cwds={4500: '/elsewhere'})
        self.assertEqual(self.charges(observation([row])), ALLOWANCE)
        os_proof(self, births={4500: self.now - 400_000 - 60_000}, cwds={4500: '/owned'})  # birth beyond tolerance
        self.assertEqual(self.charges(observation([row])), ALLOWANCE)
        os_proof(self, births={4500: self.now - 400_000}, cwds={4500: '/owned'}, alive=False)  # died at the boundary
        self.assertEqual(self.charges(observation([row])), ALLOWANCE)

    def test_duplicate_incarnation_credits_exactly_once(self):
        native = '1b2f9c3e-4d5a-46b7-8c9d-0e1f2a3b4c5d'
        launching_row(self.journal, 'operation-a', 'session-a', state='running', marker=1,
                      native=native, tag='task-hub2-shared')
        launching_row(self.journal, 'operation-b', 'session-b', state='running', marker=1,
                      native=native, tag='task-hub2-shared')
        reserve(self.journal, 'operation-a', 'session-a', native=native)
        reserve(self.journal, 'operation-b', 'session-b', native=native)
        self.assertEqual(self.charges(self.proven(native, 4600, tag='task-hub2-shared')), ALLOWANCE)

    def test_identity_or_stage_mismatch_earns_no_credit(self):
        native = '1b2f9c3e-4d5a-46b7-8c9d-0e1f2a3b4c5d'
        launching_row(self.journal, state='running', marker=1, native=native)
        reserve(self.journal, native=native)
        os_proof(self, births={4700: self.now - 400_000}, cwds={4700: '/owned'})
        for rows in (observation([resident(native, tag='task-hub2-other', pid=4700)]),
                     observation([resident(native, workspace='/elsewhere', pid=4700)]),
                     observation([resident(native, birth_ms=self.now + 5_000, pid=4700)]),
                     observation([resident(native, alive=False, pid=4700)]),
                     observation([resident(native, alive=None, pid=4700)]),
                     observation([resident(native, phase='exited', pid=4700)]),
                     observation([resident(native, state='starting', pid=4700)]),
                     observation([resident(native, pid=None)]),
                     observation([], collected_offset=30.0)):  # stale at the final boundary
            self.assertEqual(self.charges(rows), ALLOWANCE, msg=str(rows))


class PermitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.journal = Journal(Path(self.temp.name) / 'journal.sqlite')
        self.addCleanup(self.journal.db.close)

    def test_no_permit_verifies_trivially(self):
        self.assertIsNone(resource_admission.verify_permit(None, journal=self.journal))

    def test_expired_permit_refuses_without_renewing_the_clock(self):
        permit = resource_admission.DispatchPermit('operation-1', 'session-1', 1,
                                                   permit_deadline=time.monotonic() - 1)
        self.assertTrue(permit.expired())
        with self.assertRaisesRegex(RuntimeError, 'native_start_uncertain'):
            resource_admission.verify_permit(permit, journal=self.journal)
        time.sleep(0.01)
        with self.assertRaisesRegex(RuntimeError, 'native_start_uncertain'):
            resource_admission.verify_permit(permit, journal=self.journal)  # never renewed

    def test_fresh_permit_requires_unfenced_launching_row(self):
        launching_row(self.journal)
        permit = resource_admission.DispatchPermit('operation-1', 'session-1', 1,
                                                   permit_deadline=time.monotonic() + 30)
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
        permit = resource_admission.DispatchPermit('operation-1', 'session-1', 1,
                                                   permit_deadline=time.monotonic() + 30)
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
