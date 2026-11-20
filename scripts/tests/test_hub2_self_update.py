"""Optional, non-gating natural staging hook of the Hub2 updater (no real sudo/Root/network)."""
import ast
import contextlib
import importlib.util
import inspect
import io
import os
from pathlib import Path
import stat
import subprocess
import types
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('hub2_self_update_hook_under_test', REPO / 'scripts/hub2-self-update.py')
updater = importlib.util.module_from_spec(spec)
spec.loader.exec_module(updater)

ENTRY = updater.NATURAL_STAGE_ENTRY


def fake_lstat(overrides=None, *, default_uid=0):
    """Simulated Root-owned tree: directories 0755, entry regular 0755."""
    overrides = overrides or {}

    def lstat(path):
        path = str(path)
        if path in overrides:
            value = overrides[path]
            if isinstance(value, BaseException):
                raise value
            return value
        if path == ENTRY:
            return types.SimpleNamespace(st_mode=stat.S_IFREG | 0o755, st_uid=default_uid, st_nlink=1, st_size=4096)
        return types.SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_uid=default_uid, st_nlink=2, st_size=4096)
    return lstat


class VerifiedEntryTests(unittest.TestCase):
    def check(self, overrides=None, **kwargs):
        with patch.object(updater.os, 'lstat', fake_lstat(overrides, **kwargs)):
            return updater.verified_natural_stage_entry()

    def test_fixed_entry_path_and_argv(self):
        self.assertEqual(ENTRY, '/usr/local/lib/agent-hub/natural/hub-natural-stage.py')
        self.assertEqual(updater.NATURAL_STAGE_ARGV,
                         ('/usr/bin/sudo', '-n', '/usr/bin/python3', '-I', '-S', '-B', ENTRY, '--stage-current'))

    def test_root_owned_graph_passes(self):
        self.assertTrue(self.check())

    def test_absent_symlinked_foreign_or_writable_nodes_fail(self):
        directory = '/usr/local/lib/agent-hub'
        cases = {
            'absent': {ENTRY: FileNotFoundError()},
            'entry_symlink': {ENTRY: types.SimpleNamespace(st_mode=stat.S_IFLNK | 0o777, st_uid=0, st_nlink=1, st_size=9)},
            'entry_group_writable': {ENTRY: types.SimpleNamespace(st_mode=stat.S_IFREG | 0o775, st_uid=0, st_nlink=1, st_size=9)},
            'entry_hardlinked': {ENTRY: types.SimpleNamespace(st_mode=stat.S_IFREG | 0o755, st_uid=0, st_nlink=2, st_size=9)},
            'entry_user_owned': {ENTRY: types.SimpleNamespace(st_mode=stat.S_IFREG | 0o755, st_uid=1000, st_nlink=1, st_size=9)},
            'parent_symlink': {directory: types.SimpleNamespace(st_mode=stat.S_IFLNK | 0o777, st_uid=0, st_nlink=1, st_size=9)},
            'parent_user_owned': {directory: types.SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_uid=1000, st_nlink=2, st_size=9)},
            'parent_world_writable': {directory: types.SimpleNamespace(st_mode=stat.S_IFDIR | 0o757, st_uid=0, st_nlink=2, st_size=9)},
        }
        for label, overrides in cases.items():
            with self.subTest(label):
                self.assertFalse(self.check(overrides))


class HookTests(unittest.TestCase):
    def run_hook(self, *, verified=True, runner=None):
        calls = []

        def default_runner(argv, **kwargs):
            calls.append((argv, kwargs))
            return types.SimpleNamespace(returncode=0)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            result = updater.run_optional_natural_stage(verifier=lambda: verified, runner=runner or default_runner)
        return result, calls, out.getvalue()

    def test_absent_or_untrusted_entry_is_never_invoked(self):
        result, calls, out = self.run_hook(verified=False)
        self.assertFalse(result)
        self.assertEqual(calls, [])
        self.assertIn('non-gating', out)

    def test_invokes_only_fixed_argv_bounded_without_caller_authority(self):
        result, calls, _ = self.run_hook()
        self.assertTrue(result)
        argv, kwargs = calls[0]
        self.assertEqual(tuple(argv), updater.NATURAL_STAGE_ARGV)
        self.assertEqual(kwargs['timeout'], updater.NATURAL_STAGE_TIMEOUT)
        self.assertLessEqual(kwargs['timeout'], 600)
        self.assertIs(kwargs['check'], False)
        self.assertEqual(kwargs['env'], {'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'})
        self.assertEqual(kwargs['stdin'], subprocess.DEVNULL)
        self.assertNotIn('shell', kwargs)

    def test_failures_never_raise_or_gate(self):
        failures = [
            subprocess.TimeoutExpired('sudo', 300),
            OSError('sudo missing'),
            PermissionError('a password is required'),
            RuntimeError('boom'),
        ]
        for failure in failures:
            def runner(*_a, failure=failure, **_k):
                raise failure
            with self.subTest(type(failure).__name__):
                result, _calls, out = self.run_hook(runner=runner)
                self.assertFalse(result)
                self.assertIn('non-gating', out)

    def test_nonzero_status_is_reported_not_raised(self):
        for code in (1, 2, 127):
            result, _calls, out = self.run_hook(runner=lambda *a, code=code, **k: types.SimpleNamespace(returncode=code))
            self.assertFalse(result)
            self.assertIn(f'status={code}', out)

    def test_hook_does_not_claim_natural_readiness(self):
        _, _, out = self.run_hook()
        for forbidden in ('MainPID', 'ready', 'qualif', 'caps', 'active'):
            self.assertNotIn(forbidden.lower(), out.lower())


class InstallIntegrationContractTests(unittest.TestCase):
    """Structural guards: the hook is only a trailing observation after success."""

    SOURCE = (REPO / 'scripts/hub2-self-update.py').read_text()
    TREE = ast.parse(SOURCE)

    def function(self, name):
        return next(node for node in ast.walk(self.TREE) if isinstance(node, ast.FunctionDef) and node.name == name)

    def test_hook_calls_follow_success_prints_and_require_restart(self):
        install = self.function('install')
        found = []
        for node in ast.walk(install):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'run_optional_natural_stage':
                found.append(node)
        self.assertEqual(len(found), 3)
        source = ast.get_source_segment(self.SOURCE, install)
        for message in ('already current', 'Reactivated retained', 'Activated Hub2 release'):
            index = source.index(message)
            tail = source[index:index + 220]
            self.assertIn('if restart:', tail)
            self.assertIn('run_optional_natural_stage()', tail)

    def test_readiness_and_rollback_do_not_reference_natural_staging(self):
        for name in ('check_service_ready', 'restart_host', 'failure', 'restore_units', 'reactivate_retained_release'):
            body = inspect.getsource(getattr(updater, name)).lower()
            with self.subTest(name):
                self.assertNotIn('natural', body)
        ready = inspect.getsource(updater.check_service_ready)
        # Existing readiness semantics are untouched: wait, then authenticated exact identity.
        for needle in ('range(6)', 'time.sleep(5)', '/api/fleet/routing/self', "status.get('connected') is not True",
                       "status.get('host_version') != installed['host_version']",
                       "status['protocol_version'] != installed['protocol_version']"):
            self.assertIn(needle, ready)

    def test_hook_runs_after_activation_commit_and_cannot_roll_back(self):
        install = self.function('install')
        # The final call sits outside the try/except BaseException rollback block.
        for node in ast.walk(install):
            if isinstance(node, ast.Try) and any(isinstance(h.type, ast.Name) and h.type.id == 'BaseException'
                                                 for h in node.handlers if h.type is not None):
                segment = ast.get_source_segment(self.SOURCE, node)
                self.assertNotIn('run_optional_natural_stage', segment)

    def test_unavailable_stage_leaves_install_result_unchanged(self):
        hook = updater.run_optional_natural_stage
        with patch.object(updater.subprocess, 'run', side_effect=AssertionError('must not run')), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(hook(verifier=lambda: False))


if __name__ == '__main__':
    unittest.main()
