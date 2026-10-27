"""Windows ARM64 host support on Tabo (intake ccae37e8): AC1-AC5 on Linux CI.

Each test reproduces the Windows condition seen on Tabo (no fcntl/statvfs,
inherited WOW64 env, quse outside PATH, contended msvcrt lock) by patching
the module seams, so the Windows branches run here.
"""
import os
import subprocess
import sys
import tempfile
import types
import unittest
from collections import namedtuple
from pathlib import Path
from unittest.mock import patch

from agent_hub_host import disk_monitor, provisioning, session_host, system


def fake_msvcrt(fail_times=0):
    calls = []
    state = {'fail': fail_times}

    def locking(fd, mode, nbytes):
        calls.append((mode, nbytes, os.lseek(fd, 0, os.SEEK_CUR)))
        if mode == module.LK_NBLCK and state['fail'] > 0:
            state['fail'] -= 1
            raise OSError(36, 'Resource deadlock avoided')

    module = types.SimpleNamespace(LK_LOCK=1, LK_NBLCK=2, LK_UNLCK=0, locking=locking)
    return module, calls


def windows_os():
    """A copy of os reporting name 'nt' for one module (never patch os.name globally)."""
    fake = types.ModuleType('os')
    fake.__dict__.update(os.__dict__)
    fake.name = 'nt'
    return fake


class JournalLockWindowsTest(unittest.TestCase):
    """AC1: the msvcrt branch needs `os`; it raised NameError on Tabo."""

    def test_module_imports_os_for_windows_branch(self):
        self.assertIs(session_host.os, os)

    def test_windows_lock_writes_sentinel_locks_and_unlocks_byte_zero(self):
        msvcrt, calls = fake_msvcrt()
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(session_host, 'fcntl', None), \
                patch.dict(sys.modules, {'msvcrt': msvcrt}):
            path = Path(tmp) / 'host.lock'
            with path.open('a') as lock, session_host.acquire_journal_lock(lock):
                self.assertEqual(calls, [(msvcrt.LK_NBLCK, 1, 0)])
            self.assertEqual(path.read_text(), '0')
        self.assertEqual(calls[-1], (msvcrt.LK_UNLCK, 1, 0))

    def test_windows_lock_held_elsewhere_refuses_second_controller(self):
        msvcrt, _ = fake_msvcrt(fail_times=1)
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(session_host, 'fcntl', None), \
                patch.dict(sys.modules, {'msvcrt': msvcrt}):
            with (Path(tmp) / 'host.lock').open('a') as lock:
                with self.assertRaises(OSError):
                    with session_host.acquire_journal_lock(lock):
                        pass


class DiskMonitorWindowsTest(unittest.TestCase):
    """AC2: os.statvfs is absent on Windows; shutil.disk_usage replaces it."""

    def test_default_statvfs_falls_back_to_disk_usage(self):
        usage = namedtuple('usage', 'total used free')(1000, 600, 400)
        no_statvfs = types.ModuleType('os')
        no_statvfs.__dict__.update({k: v for k, v in os.__dict__.items() if k != 'statvfs'})
        with patch.object(disk_monitor, 'os', no_statvfs), \
                patch.object(disk_monitor.shutil, 'disk_usage', return_value=usage) as du:
            monitor = disk_monitor.DiskMonitor(config={'enabled': True, 'machine_id': 'tabo'})
            vfs = monitor.statvfs_fn('C:\\')
        du.assert_called_once_with('C:\\')
        self.assertEqual((vfs.f_blocks * vfs.f_frsize, vfs.f_bavail * vfs.f_frsize), (1000, 400))

    def test_collect_reports_windows_drive(self):
        usage = namedtuple('usage', 'total used free')(1000, 600, 400)
        monitor = disk_monitor.DiskMonitor(
            config={'enabled': True, 'machine_id': 'tabo', 'mounts': ['C:\\']},
            statvfs_fn=lambda path: disk_monitor._statvfs_from_disk_usage(path),
            stat_fn=lambda path: types.SimpleNamespace(st_dev=7, st_size=0),
            listdir_fn=lambda path: [])
        cfg = monitor.load_active_config()
        with patch.object(disk_monitor.os, 'name', 'nt'), \
                patch.object(disk_monitor.shutil, 'disk_usage', return_value=usage):
            payload, errors = monitor.collect(cfg)
        self.assertNotIn('invalid_mount_path:C:\\', errors)
        fs = payload['filesystems'][0]
        self.assertEqual((fs['mount_point'], fs['total_bytes'], fs['free_bytes'], fs['used_bytes']),
                         ('C:\\', 1000, 400, 600))

    def test_drive_paths_stay_invalid_on_posix(self):
        with patch.object(disk_monitor.os, 'name', 'posix'):
            self.assertFalse(disk_monitor._is_windows_drive_path('C:\\'))


class ArchWindowsArm64Test(unittest.TestCase):
    """AC3: Tabo = native ARM64 Python, inherited PROCESSOR_ARCHITECTURE=AMD64, W6432 unset."""

    TABO_ENV = {'PROCESSOR_ARCHITECTURE': 'AMD64'}

    def tabo(self, native):
        env = {k: v for k, v in os.environ.items() if k != 'PROCESSOR_ARCHITEW6432'}
        env.update(self.TABO_ENV)
        return [patch.dict(os.environ, env, clear=True),
                patch.object(system.platform, 'system', return_value='Windows'),
                patch.object(system.platform, 'machine', return_value='ARM64'),
                patch.object(system, '_windows_native_arch', return_value=native)]

    def run_with(self, patches, fn):
        for p in patches:
            p.start()
        try:
            return fn()
        finally:
            for p in reversed(patches):
                p.stop()

    def test_tabo_env_reports_arm64_from_native_probe(self):
        self.assertEqual(self.run_with(self.tabo('arm64'), system.get_arch), 'arm64')

    def test_tabo_env_reports_arm64_from_platform_machine_without_native_probe(self):
        self.assertEqual(self.run_with(self.tabo(None), system.get_arch), 'arm64')
        self.assertEqual(self.run_with(self.tabo(None), lambda: system.get_system_info()['arch']), 'arm64')

    def test_native_probe_failure_still_ignores_inherited_amd64_env(self):
        patches = self.tabo(None)
        patches[-1] = patch.object(system, '_windows_native_arch', side_effect=OSError('no kernel32'))
        self.assertEqual(self.run_with(patches, system.get_arch), 'arm64')

    def test_env_is_last_resort_on_windows(self):
        patches = self.tabo(None)
        patches[2] = patch.object(system.platform, 'machine', return_value='')
        self.assertEqual(self.run_with(patches, system.get_arch), 'amd64')

    def test_native_probe_reads_native_machine_not_process_machine(self):
        # Native ARM64 Python is not WOW64: process machine is UNKNOWN (0),
        # only the native machine carries IMAGE_FILE_MACHINE_ARM64.
        def is_wow64_process2(handle, process, native):
            process._obj.value, native._obj.value = 0, 0xAA64
            return 1

        kernel32 = types.SimpleNamespace(GetCurrentProcess=lambda: -1, IsWow64Process2=is_wow64_process2)
        import ctypes
        with patch.object(ctypes, 'WinDLL', return_value=kernel32, create=True):
            self.assertEqual(system._windows_native_arch(), 'arm64')

    def test_native_probe_without_windll_returns_none_off_windows(self):
        with patch.dict(sys.modules, {'winreg': None}):
            self.assertIsNone(system._windows_native_arch())


class CodexQuotaGateWindowsTest(unittest.TestCase):
    """AC4: quse is installed with the host package but not on the service PATH.

    Root cause on Tabo: subprocess.run(['quse', ...]) raised FileNotFoundError
    (the venv Scripts dir is not on the Scheduled Task PATH; quse was not
    installed at all), which the gate mapped to quota_check_failed for every
    Codex route.
    """

    QUSE_OK = ('{"codex": {"status": "ok", "details": {"limit_reached": false}, '
               '"windows": {"7d": {"percent_remaining": 80.0, "reset_at": "2999-01-01T00:00:00Z"}}}}')

    def test_resolves_quse_next_to_interpreter_when_not_on_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            scripts = Path(tmp) / 'Scripts'
            scripts.mkdir()
            quse = scripts / 'quse'
            quse.write_text('#!/bin/sh\n')
            quse.chmod(0o755)
            with patch.object(provisioning.sys, 'executable', str(scripts / 'python')):
                self.assertEqual(provisioning.quse_argv({'PATH': str(Path(tmp) / 'empty')}), [str(quse)])

    def test_falls_back_to_installed_module(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(provisioning.sys, 'executable', str(Path(tmp) / 'python')), \
                patch('importlib.util.find_spec', return_value=object()):
            self.assertEqual(provisioning.quse_argv({'PATH': tmp}),
                             [str(Path(tmp) / 'python'), '-m', 'quse.cli'])

    def test_gate_admits_with_resolved_quse_and_utf8_env(self):
        seen = {}

        def run(argv, **kw):
            seen.update(argv=argv, **kw)
            return types.SimpleNamespace(returncode=0, stdout=self.QUSE_OK, stderr='')

        with patch.object(provisioning, 'quse_argv', return_value=['C:\\venv\\Scripts\\quse.exe']), \
                patch.object(provisioning.subprocess, 'run', side_effect=run):
            self.assertEqual(provisioning.check_codex_quota_gate({'CODEX_HOME': 'x'}), (True, 'ok'))
        self.assertEqual(seen['argv'], ['C:\\venv\\Scripts\\quse.exe', 'codex', '--json'])
        self.assertEqual((seen['encoding'], seen['env']['PYTHONIOENCODING'], seen['env']['CODEX_HOME']),
                         ('utf-8', 'utf-8', 'x'))

    def test_missing_quse_is_unavailable_not_check_failed(self):
        with patch.object(provisioning, 'quse_argv', return_value=['quse']), \
                patch.object(provisioning.subprocess, 'run', side_effect=FileNotFoundError('quse')):
            self.assertEqual(provisioning.check_codex_quota_gate({}), (False, 'quota_unavailable'))

    def test_probe_timeout_is_unavailable_not_check_failed(self):
        """A probe that exceeds the 30s budget produced no evidence.

        A slow Windows cold start (antivirus scanning the venv probe) must not
        resurface quota_check_failed for every Codex route after the quse
        rollout; an unfinished check is quota_unavailable, like a missing one.
        """
        with patch.object(provisioning, 'quse_argv', return_value=['quse']), \
                patch.object(provisioning.subprocess, 'run',
                             side_effect=subprocess.TimeoutExpired(cmd=['quse', 'codex'], timeout=30)):
            self.assertEqual(provisioning.check_codex_quota_gate({}), (False, 'quota_unavailable'))

    def test_host_package_depends_on_quse(self):
        text = (Path(__file__).resolve().parents[2] / 'pyproject.toml').read_text()
        self.assertIn('"quse>=', text)


class ProvisioningLockWindowsTest(unittest.TestCase):
    """AC5: `hub engines sync` raced the session host's reconcile on Windows.

    msvcrt LK_LOCK gives up after ~10s, so the CLI sync failed while the
    service held the lock through an npm install; it now waits like flock.
    """

    def test_waits_for_holder_then_locks(self):
        msvcrt, calls = fake_msvcrt(fail_times=3)
        sleeps = []
        with tempfile.TemporaryFile() as fh, patch.dict(sys.modules, {'msvcrt': msvcrt}):
            provisioning._windows_wait_lock(fh.fileno(), timeout=60, sleep=sleeps.append)
            fh.seek(0)
            self.assertEqual(fh.read(), b'0')
        self.assertEqual(len(sleeps), 3)
        self.assertEqual([c[0] for c in calls], [msvcrt.LK_NBLCK] * 4)
        self.assertTrue(all(c[2] == 0 for c in calls))

    def test_gives_up_after_deadline(self):
        msvcrt, _ = fake_msvcrt(fail_times=10 ** 6)
        with tempfile.TemporaryFile() as fh, patch.dict(sys.modules, {'msvcrt': msvcrt}):
            with self.assertRaises(OSError):
                provisioning._windows_wait_lock(fh.fileno(), timeout=0, sleep=lambda s: None)

    def test_lock_context_uses_windows_wait(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.dict(os.environ, {'AGENT_HUB_MANAGED_DIR': tmp}), \
                patch.object(provisioning, 'os', windows_os()), \
                patch.object(provisioning.private_fs, '_open', side_effect=lambda anchor, path, flags: os.open(path, flags, 0o600)), \
                patch.object(provisioning, '_windows_wait_lock') as wait:
            with provisioning.lock():
                pass
        wait.assert_called_once()


class TrustedDirectoryAclTest(unittest.TestCase):
    """AC6: CPython 3.13 os.mkdir(mode=0o700) makes the journal DACL
    protected SYSTEM + Administrators + OWNER RIGHTS; the owner-only check
    rejected it every telemetry cycle with 'Exactly one owner-only
    full-control ACE required'. Directories accept exactly that trusted set;
    files stay strictly one owner ACE.
    """

    USER, SYSTEM, ADMINS, OWNER_RIGHTS, EVERYONE = 77, 18, 544, 4, 1

    def make_api(self, sids, flags=0):
        import ctypes as C
        from agent_hub_host import win32_fs as win
        raw = win.API.__new__(win.API)
        raw.check = lambda ok: ok
        raw.user_sid = self.USER
        raw.trusted_sids = [self.SYSTEM, self.ADMINS, self.OWNER_RIGHTS]
        acl = win.ACL()
        acl.count = len(sids)
        aces = []
        for sid in sids:
            ace = win.ACE()
            ace.size, ace.mask, ace.flags = 24, win.FULL, flags
            aces.append((ace, sid))
        self.keep = (acl, aces)

        def security(handle, kind, fields, owner, group, dacl, sacl, sd):
            C.cast(owner, C.POINTER(win.PTR))[0] = self.USER
            C.cast(dacl, C.POINTER(win.PTR))[0] = C.addressof(acl)
            C.cast(sd, C.POINTER(win.PTR))[0] = 99
            return 0

        def control(sd, value, revision):
            C.cast(value, C.POINTER(win.U16))[0] = 0x1000
            return 1

        def getace(dacl, index, result):
            C.cast(result, C.POINTER(win.PTR))[0] = C.addressof(aces[index][0])
            return 1

        sid_of = {C.addressof(ace) + win.ACE.sid.offset: sid for ace, sid in aces}

        def equal(left, right):
            left = getattr(left, 'value', left)
            right = getattr(right, 'value', right)
            return sid_of.get(left, left) == sid_of.get(right, right)

        raw.GetSecurityInfo, raw.GetSecurityDescriptorControl, raw.GetAce = security, control, getace
        raw.EqualSid, raw.LocalFree = equal, lambda ptr: None
        return raw

    def test_python313_private_mkdir_dacl_is_a_private_directory(self):
        raw = self.make_api([self.SYSTEM, self.ADMINS, self.OWNER_RIGHTS], flags=1 | 2)
        self.assertTrue(raw.private(5, directory=True))

    def test_owner_plus_admins_directory_is_private(self):
        self.assertTrue(self.make_api([self.USER, self.SYSTEM, self.ADMINS]).private(5, directory=True))

    def test_files_still_require_exactly_one_owner_ace(self):
        raw = self.make_api([self.SYSTEM, self.ADMINS, self.OWNER_RIGHTS])
        with self.assertRaisesRegex(PermissionError, 'Exactly one owner-only'):
            raw.private(5)
        self.assertTrue(self.make_api([self.USER]).private(5))

    def test_untrusted_or_ownerless_directory_acl_is_refused(self):
        for sids in ([self.USER, self.EVERYONE], [self.SYSTEM, self.ADMINS],
                     [self.USER, self.SYSTEM, self.ADMINS, self.OWNER_RIGHTS, self.SYSTEM]):
            with self.subTest(sids=sids), self.assertRaises(PermissionError):
                self.make_api(sids).private(5, directory=True)

    def test_inherited_aces_still_refused_unless_requested(self):
        with self.assertRaises(PermissionError):
            self.make_api([self.USER, self.SYSTEM], flags=16).private(5, directory=True)


class CycleReportsWindowsTest(unittest.TestCase):
    """AC6: per-cycle status and quota report errors on a host without quse/allowlist."""

    def test_missing_status_allowlist_is_disabled_not_an_error(self):
        from agent_hub_host.status_runtime import StatusCollector
        with tempfile.TemporaryDirectory() as tmp:
            collector = StatusCollector(allowlist_path=str(Path(tmp) / 'status-allowlist.json'),
                                        machine_id='tabo', min_interval_s=0)
            result = collector.step(submit=True)
        self.assertEqual((result['disabled'], result['errors']), (True, []))
        with patch.object(session_host, 'print') as out:
            session_host.status_step_once(types.SimpleNamespace(step=lambda submit: result))
        out.assert_not_called()

    def test_status_uses_configured_aplexer_binary(self):
        from agent_hub_host import native_sessions
        from agent_hub_host.status_runtime import StatusCollector
        seen = []

        def qualify(executable, *a, **kw):
            seen.append(('whoami', executable))
            raise native_sessions.SessionControlError('unbound')

        def listing(executable='aplexer', **kw):
            seen.append(('list', executable))
            return []

        exe = 'C:\\Users\\u\\AppData\\Local\\Programs\\aplexer\\aplexer.exe'
        collector = StatusCollector(allowlist={'machine_id': 'tabo'}, min_interval_s=0, aplexer_bin=exe)
        with patch.object(native_sessions, 'qualify_sender_binding', side_effect=qualify), \
                patch.object(native_sessions, 'list_sessions', side_effect=listing):
            collector.step()
        self.assertEqual(seen, [('whoami', exe), ('list', exe)])

    def test_quota_collector_holds_when_quse_absent(self):
        from agent_hub_host import quota_snapshot
        collector = quota_snapshot.QuotaCollector(base_url='https://hub.example')
        cfg = {'machine_id': 'tabo', 'access_token': 't', 'base_url': 'https://hub.example'}
        with patch.object(quota_snapshot.config, 'load_config', return_value=cfg), \
                patch.object(quota_snapshot.shutil, 'which', return_value=None), \
                patch.object(quota_snapshot.subprocess, 'Popen', side_effect=AssertionError('no spawn')):
            self.assertIsNone(collector.collect_and_post())
        self.assertEqual((collector.last_hold, collector.last_error), ('quse_unavailable', None))

    def test_session_host_resolves_quse_for_quota_collector(self):
        with patch('agent_hub_host.provisioning.quse_argv', return_value=['C:\\venv\\Scripts\\quse.exe']):
            self.assertEqual(session_host.resolve_quse_cmd(), 'C:\\venv\\Scripts\\quse.exe')
        with patch('agent_hub_host.provisioning.quse_argv', return_value=['py', '-m', 'quse.cli']):
            self.assertEqual(session_host.resolve_quse_cmd(), ['py', '-m', 'quse.cli', '--json'])

    def test_native_cli_output_decodes_as_utf8(self):
        from agent_hub_host import native_sessions
        with patch('agent_hub_host.aplexer_client.client_call', return_value=None), \
                patch.object(native_sessions.subprocess, 'run',
                             return_value=types.SimpleNamespace(returncode=0, stdout='[]', stderr='')) as run:
            try:
                native_sessions.run_native('aplexer', ['list', '--json'])
            except Exception:
                pass
        self.assertEqual((run.call_args.kwargs['encoding'], run.call_args.kwargs['errors']), ('utf-8', 'replace'))


if __name__ == '__main__':
    unittest.main()
