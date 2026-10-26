"""Windows Tabo branches (AC2-AC4): arch, cgroup flags, containment, needs CLI."""
import os
import unittest
from unittest.mock import patch

from agent_hub_host import fleet, system
from agent_hub_host import native_sessions
from agent_hub_host import session_control
import argparse


def make_parser():
    root = argparse.ArgumentParser()
    sub = root.add_subparsers(dest='cmd', required=True)
    session_control.add_commands(sub)
    return root


class SystemArchTest(unittest.TestCase):
    def test_normalizes_common_arches(self):
        self.assertEqual(system._normalize_arch('ARM64'), 'arm64')
        self.assertEqual(system._normalize_arch('aarch64'), 'arm64')
        self.assertEqual(system._normalize_arch('AMD64'), 'amd64')
        self.assertEqual(system._normalize_arch('x86_64'), 'amd64')
        self.assertIsNone(system._normalize_arch('mips'))

    def test_prefers_architew6432_under_x64_emulation(self):
        with patch.object(system.platform, 'system', return_value='Windows'), patch.dict(os.environ, {'PROCESSOR_ARCHITEW6432': 'ARM64', 'PROCESSOR_ARCHITECTURE': 'AMD64'}):
            self.assertEqual(system.get_arch(), 'arm64')

    def test_linux_ignores_windows_environment(self):
        with patch.object(system.platform, 'system', return_value='Linux'), patch.object(system.platform, 'machine', return_value='x86_64'), patch.dict(os.environ, {'PROCESSOR_ARCHITEW6432': 'ARM64'}):
            self.assertEqual(system.get_arch(), 'amd64')

    def test_system_info_carries_arch(self):
        with patch.object(system.platform, 'system', return_value='Windows'), patch.dict(os.environ, {'PROCESSOR_ARCHITEW6432': 'ARM64', 'PROCESSOR_ARCHITECTURE': 'AMD64'}):
            info = system.get_system_info()
        self.assertEqual(info['arch'], 'arm64')
        self.assertEqual(info['os'], 'windows')


class WindowsFlagsTest(unittest.TestCase):
    def test_worker_argv_empty_on_windows(self):
        with patch.object(fleet, 'is_windows_host', return_value=True):
            self.assertEqual(fleet.worker_resource_argv(), [])
        with patch.object(fleet, 'is_windows_host', return_value=False):
            argv = fleet.worker_resource_argv()
        self.assertIn('--memory', argv)
        self.assertIn('--pids', argv)

    def test_supervisor_argv_empty_on_windows(self):
        import importlib.util
        import pathlib
        path = pathlib.Path(__file__).resolve().parents[2] / 'scripts' / 'session-host-supervisor.py'
        spec = importlib.util.spec_from_file_location('supervisor_under_test', str(path))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        with patch.object(mod, 'is_windows', return_value=True):
            self.assertEqual(mod.controller_resource_argv(), [])
        with patch.object(mod, 'is_windows', return_value=False):
            self.assertIn('--memory', mod.controller_resource_argv())


class ContainmentTest(unittest.TestCase):
    def test_windows_job_object_empty_is_exit_proof(self):
        row = native_sessions.NativeSession(
            session_id='s', tag='t', workspace='w', engine='e', phase='running',
            worker_pid=None, workload_pid=None, containment_empty=True,
            workload_cgroup='JobObject-Tabo', created_at_ms=1)
        ok, reason = native_sessions._confirm_workload_exit(row)
        self.assertTrue(ok)
        self.assertIn('containment empty', reason)

    def test_windows_without_proof_is_unproven(self):
        row = native_sessions.NativeSession(
            session_id='s', tag='t', workspace='w', engine='e', phase='running',
            worker_pid=None, workload_pid=None, containment_empty=False,
            workload_cgroup='JobObject-Tabo', created_at_ms=1)
        ok, _ = native_sessions._confirm_workload_exit(row)
        self.assertFalse(ok)

    def test_windows_never_probes_linux_or_signals_pids(self):
        row = native_sessions.NativeSession(session_id='s', tag='t', workspace='w', engine='e', phase='running', worker_pid=123, workload_pid=124, containment_empty=True, workload_cgroup='/sys/fs/cgroup/fake', created_at_ms=1)
        with patch.object(native_sessions.os, 'name', 'nt'), patch.object(native_sessions, '_workload_containment_gone') as cgroup, patch.object(native_sessions, '_pid_dead') as pid:
            self.assertTrue(native_sessions._confirm_workload_exit(row)[0])
            from dataclasses import replace
            row = replace(row, containment_empty=False)
            self.assertFalse(native_sessions._confirm_workload_exit(row)[0])
        cgroup.assert_not_called()
        pid.assert_not_called()

    def test_linux_cgroup_path_still_supported(self):
        self.assertIsNone(native_sessions._workload_containment_gone('JobObject-Tabo'))
        # Non-existent Linux path reports reaped (True) without probing Windows.
        self.assertTrue(native_sessions._workload_containment_gone('/sys/fs/cgroup/definitely-missing-tabo-test'))


class NeedsCliTest(unittest.TestCase):
    def parse(self, *args):
        return make_parser().parse_args(['sessions', *args])

    def test_start_accepts_needs(self):
        args = self.parse('start', '--workspace', 'demo', '--needs', 'os:windows', '--needs', 'arch:arm64')
        self.assertEqual(args.needs, ['os:windows', 'arch:arm64'])

    def test_start_forwards_needs(self):
        args = self.parse('start', '--workspace', 'demo', '--needs', 'os:windows')
        with patch.object(session_control, 'request', return_value={'id': 's'}) as transport:
            session_control.execute(args)
        payload = transport.call_args.args[2]
        self.assertEqual(payload['needs'], ['os:windows'])

    def test_start_rejects_bad_and_duplicate_needs(self):
        args = self.parse('start', '--workspace', 'demo', '--needs', 'BAD')
        with patch.object(session_control, 'request') as transport:
            with self.assertRaises(ValueError):
                session_control.execute(args)
        transport.assert_not_called()
        dup = self.parse('start', '--workspace', 'demo', '--needs', 'os:windows', '--needs', 'os:windows')
        with patch.object(session_control, 'request') as transport:
            with self.assertRaises(ValueError):
                session_control.execute(dup)
        transport.assert_not_called()


if __name__ == '__main__':
    unittest.main()
