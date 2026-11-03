"""Intake fae60b8a: clone failures carry git stderr; quota shows a concrete reason.

(1) repo_clone_failed must carry the real git stderr, never a bare slug or
    an empty exit code -- on both the POSIX fleet path and the Windows path
    (DESKTOP-FP4OP26 clones through repo_clone_windows).
(2) The dispatch diagnostic for repo_clone_failed keeps a scrubbed tail so
    the fleet-launch journal holds the stderr; auth/not-found refusals stay
    slug-only (ad8aa931 privacy contract).
(3) Quota sanitizing forwards fixed probe reason codes instead of a bare
    "unavailable".
"""
import contextlib
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agent_hub_host import fleet
from agent_hub_host import quota_snapshot
from agent_hub_host import repo_clone_windows as clone_fs
from agent_hub_host.dispatch_diagnostics import classify


@unittest.skipUnless(fleet.repo_provisioning_supported(), 'POSIX repository effects require flock/statvfs')
class PosixCloneStderrTests(unittest.TestCase):
    def setUp(self):
        fleet._GH_AUTH_CACHE = None

    def test_failed_clone_carries_git_stderr(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            target = root / 'demo'
            staging = Path(str(target) + '.tmp.' + str(os.getpid()))

            def failed(argv, **kwargs):
                staging.mkdir()
                return subprocess.CompletedProcess(
                    argv, 128, None,
                    b"fatal: unable to connect to github.com: proxy 127.0.0.1 refused\n")

            with patch.object(fleet, 'gh_auth_ok', return_value=True), patch.object(
                    fleet, '_run_repo_clone_command', side_effect=failed), patch.object(
                    fleet.os, 'statvfs', return_value=SimpleNamespace(
                        f_blocks=1000, f_bfree=900, f_bavail=900, f_frsize=1024 ** 3)):
                with self.assertRaisesRegex(ValueError, 'fatal: unable to connect') as raised:
                    fleet.guarded_repo_clone({'workspace_roots': [str(root)]}, 'demo',
                                             'org/demo', str(target), disk_floor_gb=15)
            self.assertFalse(staging.exists())
        diagnostic = classify(raised.exception)
        self.assertEqual(diagnostic['reason'], 'repo_clone_failed')
        self.assertIn('unable to connect', diagnostic.get('tail', ''))

    def test_bare_clone_failure_still_names_exit_code(self):
        self.assertEqual(fleet._short_clone_detail([b'', None], 1), 'exit 1')
        self.assertEqual(fleet._short_clone_detail([], None), 'clone command failed')

    def test_clone_detail_scrubs_secrets_and_paths(self):
        detail = fleet._short_clone_detail(
            [b"fatal: could not read Password for 'https://token@github.com/org/demo.git': cancelled\n"
             b"second line never used\n"], 128)
        self.assertIn('fatal: could not read Password', detail)
        self.assertNotIn('token@github.com', detail)
        self.assertNotIn('second line', detail)
        self.assertLessEqual(len(detail), fleet.CLONE_DETAIL_LIMIT)


class WindowsCloneStderrTests(unittest.TestCase):
    def fixture(self, callback):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            settings = {'workspace_roots': [str(root)], 'repos': {'checkout_root': str(root)}}
            usage = SimpleNamespace(total=1000 * 1024 ** 3, used=100 * 1024 ** 3, free=900 * 1024 ** 3)
            with patch.object(clone_fs, 'checkout_lock', side_effect=lambda p: contextlib.nullcontext()), \
                    patch.object(clone_fs.shutil, 'disk_usage', return_value=usage):
                callback(root, settings)

    def call(self, root, settings, **kwargs):
        return clone_fs.clone(settings, 'demo', 'org/demo', str(root / 'demo'),
                              disk_floor_gb=15, auth_ok=lambda: True,
                              verify_origin=lambda *args: None, **kwargs)

    def test_bare_exit_names_exit_code(self):
        def check(root, settings):
            with patch.object(clone_fs, '_run', return_value=1):
                with self.assertRaisesRegex(ValueError, '^repo_clone_failed: exit 1$'):
                    self.call(root, settings)
        self.fixture(check)

    def test_git_stderr_reaches_message_with_paths_scrubbed(self):
        def check(root, settings):
            def run(argv, **kwargs):
                kwargs['stderr'].write(
                    b"Failed to connect to github.com port 443 after 0 ms: Couldn't connect to server\n")
                return 1
            with patch.object(clone_fs, '_run', side_effect=run):
                with self.assertRaises(ValueError) as raised:
                    self.call(root, settings)
            message = str(raised.exception)
            self.assertTrue(message.startswith('repo_clone_failed: '))
            self.assertIn('Failed to connect to github.com port 443', message)

            def run_secret(argv, **kwargs):
                kwargs['stderr'].write(
                    b"fatal: could not read Password for 'https://x-access-token@github.com/org/demo.git'\n")
                return 1
            with patch.object(clone_fs, '_run', side_effect=run_secret):
                with self.assertRaises(ValueError) as raised_secret:
                    self.call(root, settings)
            self.assertIn('fatal: could not read Password', str(raised_secret.exception))
            self.assertNotIn('x-access-token@github.com', str(raised_secret.exception))
        self.fixture(check)


class ClassifyTailTests(unittest.TestCase):
    def test_repo_clone_failed_keeps_scrubbed_tail(self):
        diagnostic = classify(ValueError(
            'repo_clone_failed: DataTalksClub/website: Failed to connect to github.com port 443 '
            'after 0 ms: proxy 127.0.0.1 refused'))
        self.assertEqual(diagnostic['reason'], 'repo_clone_failed')
        self.assertIn('Failed to connect to github.com port 443', diagnostic.get('tail', ''))
        self.assertLessEqual(len(diagnostic['tail']), 400)

    def test_auth_and_not_found_refusals_stay_slug_only(self):
        for message in ('repo_clone_auth_failed: owner/name: fatal: could not read Username',
                        'repo_not_found: owner/name: remote: Repository not found.'):
            diagnostic = classify(ValueError(message))
            self.assertNotIn('tail', diagnostic, message)


class ProbeErrorTests(unittest.TestCase):
    def test_fixed_probe_codes_are_forwarded(self):
        self.assertEqual(quota_snapshot.sanitize_probe_error('native_claude_auth_failed'),
                         'native_claude_auth_failed')
        self.assertEqual(quota_snapshot.sanitize_probe_error('native_claude_rate_limited'),
                         'native_claude_rate_limited')
        self.assertEqual(quota_snapshot.sanitize_probe_error('unavailable'), 'unavailable')

    def test_free_form_probe_text_stays_unavailable(self):
        for value in ('Secret internal backend trace: syntax error at char 12',
                      'rate limit error code 429', '', None, 42, 'has space', 'semi;colon'):
            self.assertEqual(quota_snapshot.sanitize_probe_error(value), 'unavailable', repr(value))

    def test_snapshot_route_carries_probe_code(self):
        raw = {'claude': {'status': 'unknown', 'error': 'native_claude_auth_failed', 'windows': {}}}
        snap = quota_snapshot.create_quota_snapshot(raw, host_id='mach-1')
        route = snap['routes']['claude']
        self.assertEqual(route['status'], 'error')
        self.assertEqual(route['error'], 'native_claude_auth_failed')


if __name__ == '__main__':
    unittest.main()
