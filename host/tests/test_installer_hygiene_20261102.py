"""Windows rollout defects of 2026-11-02: live orphan controller, dead proxy, stale PATH, locked settings."""
import json
import os
from pathlib import Path, PureWindowsPath
import tempfile
import unittest
from unittest.mock import patch

from agent_hub_host import bootstrap as b, bootstrap_control as c
from agent_hub_host import installer_hygiene as h


class LiveOrphanHandover(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / 'control'
        self.receipt = dict(id='owned', workspace='/release/package', tag='owned-controller',
                            engine='shell', created_at_ms=1, command=['owned-python'])

    def crash(self, boot='boot-before'):
        with c.ControllerLease(self.directory, boot_probe=lambda: boot) as lease:
            lease.publish(self.receipt)

    def live(self, **extra):
        return dict(self.receipt, containment_empty=False, **extra)

    def test_installer_stops_exact_live_orphan_and_proves_exit(self):
        self.crash()
        stopped = []
        rows = iter([self.live(), dict(self.receipt, containment_empty=True)])
        c.handover(self.directory, timeout=0, boot_probe=lambda: 'boot-before',
                   observe=lambda r: next(rows), stop=lambda r: stopped.append(r['id']) or {})
        self.assertEqual(stopped, [self.receipt['id']])
        self.assertFalse((self.directory / 'owner.json').exists())
        proof = c._read(next((self.directory / 'archive').iterdir()))['proof']
        self.assertEqual(proof, 'orphan_stopped_containment_empty')

    def test_kill_reply_with_record_removed_is_proof(self):
        self.crash()
        c.handover(self.directory, timeout=0, boot_probe=lambda: 'boot-before', observe=lambda r: self.live(),
                   stop=lambda r: {'id': r['id'], 'record_removed': True})
        self.assertFalse((self.directory / 'owner.json').exists())

    def test_still_live_after_stop_or_other_identity_preserves_evidence(self):
        self.crash()
        for observe, stop in ((lambda r: self.live(), lambda r: {}),
                              (lambda r: self.live(id='other'), lambda r: self.fail('foreign row must not be stopped'))):
            with self.subTest(), self.assertRaises(c.NeedsAttention):
                c.handover(self.directory, timeout=0, boot_probe=lambda: 'boot-before', observe=observe, stop=stop)
            self.assertTrue((self.directory / 'owner.json').exists())

    def test_without_stop_a_live_orphan_still_fails_closed(self):
        self.crash()
        with self.assertRaises(c.NeedsAttention):
            c.handover(self.directory, timeout=0, boot_probe=lambda: 'boot-before', observe=lambda r: self.live())


class DeadProxy(unittest.TestCase):
    def test_dead_loopback_proxy_is_dropped_live_and_remote_kept(self):
        env = {'HTTPS_PROXY': 'http://127.0.0.1:8899', 'HTTP_PROXY': 'http://127.0.0.1:9000',
               'http_proxy': 'http://proxy.corp:3128', 'ALL_PROXY': 'socks5://localhost:1080', 'PATH': 'x'}
        dropped = h.drop_dead_local_proxies(env, probe=lambda host, port: port == 9000)
        self.assertEqual(sorted(dropped), ['ALL_PROXY', 'HTTPS_PROXY'])
        self.assertEqual(sorted(env), ['HTTP_PROXY', 'PATH', 'http_proxy'])

    def test_malformed_values_are_left_alone(self):
        env = {'HTTPS_PROXY': 'http://[::1', 'HTTP_PROXY': 'http://127.0.0.1'}
        self.assertEqual(h.drop_dead_local_proxies(env, probe=lambda *a: False), [])

    def test_installer_sanitizer_applies_to_process_environment(self):
        with patch.dict(os.environ, {'HTTPS_PROXY': 'http://127.0.0.1:1'}), \
                patch.object(h, '_listening', return_value=False), patch.object(h.socket, 'create_connection', side_effect=OSError):
            b.sanitize_installer_environment(Path('R'))
            self.assertNotIn('HTTPS_PROXY', os.environ)


class StaleReleasePath(unittest.TestCase):
    def test_only_current_release_survives(self):
        base = r'C:\Users\a\AppData\Local\Programs\agent-hub\releases'
        entries = [base + r'\old1-aaa\bin', base + r'\cur-bbb\venv\Scripts', base + '/old2-ccc/engines',
                   r'C:\Windows\System32', r'C:\Program Files\nodejs', base + r'\OLD3-ddd']
        self.assertEqual(h.prune_stale_release_paths(entries, 'cur-bbb'),
                         [base + r'\cur-bbb\venv\Scripts', r'C:\Windows\System32', r'C:\Program Files\nodejs'])

    def test_standalone_aplexer_entry_is_dropped_and_release_bin_stays_first(self):
        old = 'C:' + chr(92) + 'Users' + chr(92) + 'a' + chr(92) + 'AppData' + chr(92) + 'Local' + chr(92) + 'Programs' + chr(92) + 'aplexer' + chr(92) + 'bin'
        self.assertEqual(h.prune_stale_release_paths([old, '/x/other'], 'cur'), ['/x/other'])
        with patch.object(b.sys, 'platform', 'win32'), patch.object(b, 'architecture', return_value='x64'),                 patch.object(b, 'run') as run, patch.dict(os.environ, {'PATH': old + os.pathsep + 'Q'}):
            b.publish_user_path(PureWindowsPath('C:/x/agent-hub/releases/cur'))
            value = os.environ['PATH']
        self.assertNotIn(old, value)
        self.assertTrue(value.startswith(str(PureWindowsPath('C:/x/agent-hub/releases/cur') / 'bin')))
        self.assertIn('aplexer', run.call_args[0][0][-1])

    def test_engine_env_uses_release_node_and_drops_stale_entries(self):
        root = Path('/r/agent-hub/releases/cur-bbb')
        node = root / 'runtimes/node/node.exe'
        stale = '/r/agent-hub/releases/old-aaa/bin'
        with patch.dict(os.environ, {'PATH': os.pathsep.join([stale, '/usr/bin'])}):
            env = b.engine_env(root, node)
        parts = env['PATH'].split(os.pathsep)
        self.assertEqual(parts[0], str(node.parent))
        self.assertNotIn(stale, parts)
        self.assertEqual((env['NODE'], env['npm_node_execpath']), (str(node), str(node)))

    def test_user_path_script_prunes_stale_releases(self):
        with patch.object(b.sys, 'platform', 'win32'), patch.object(b, 'architecture', return_value='x64'), \
                patch.object(b, 'run') as run, patch.dict(os.environ, {'PATH': ''}):
            b.publish_user_path(PureWindowsPath('C:/x/agent-hub/releases/cur-bbb'))
        script = run.call_args[0][0][-1]
        self.assertIn("$cur='cur-bbb'", script)
        self.assertIn('agent-hub', script)


class LockedSettings(unittest.TestCase):
    def test_retries_with_backoff_then_succeeds(self):
        calls, naps = [], []
        def flaky(path, text):
            calls.append(1)
            if len(calls) < 3:
                raise PermissionError(13, 'Access is denied')
        with patch.object(b, 'write_private', flaky):
            self.assertTrue(b.write_claude_settings(Path('s'), '{}', sleep=naps.append))
        self.assertEqual((len(calls), naps), (3, [0.5, 1.0]))

    def test_persistent_lock_never_aborts_install(self):
        with patch.object(b, 'write_private', side_effect=PermissionError(13, 'denied')):
            self.assertFalse(b.write_claude_settings(Path('s'), '{}', sleep=lambda s: None))


class BusyHostHandover(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / 'control'
        self.receipt = dict(id='owned', workspace='/release/package', tag='owned-controller',
                            engine='shell', created_at_ms=1, command=['owned-python'])

    def run_handover(self, lease, stop, observe, **kwargs):
        import threading
        errors = []
        def go():
            try:
                c.handover(self.directory, timeout=0, boot_probe=lambda: 'b', observe=observe, stop=stop,
                           force_wait=5, **kwargs)
            except BaseException as error:  # noqa: BLE001
                errors.append(error)
        thread = threading.Thread(target=go)
        thread.start()
        return thread, errors

    def test_unresponsive_busy_owner_is_force_stopped_then_exit_proven(self):
        import threading
        signals, killed = [], threading.Event()
        def stop(receipt, signal='TERM'):
            signals.append(signal)
            killed.set()
        with c.ControllerLease(self.directory, boot_probe=lambda: 'b', observe=lambda r: None) as lease:
            lease.publish(self.receipt)
            thread, errors = self.run_handover(lease, stop, lambda r: dict(self.receipt, containment_empty=True))
            self.assertTrue(killed.wait(5))
        thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(signals[0], 'TERM')
        self.assertFalse((self.directory / 'owner.json').exists())
        proof = c._read(next((self.directory / 'archive').iterdir()))['proof']
        self.assertEqual(proof, 'exact_receipt_containment_empty')

    def test_owner_that_survives_force_stop_keeps_evidence(self):
        with c.ControllerLease(self.directory, boot_probe=lambda: 'b', observe=lambda r: None) as lease:
            lease.publish(self.receipt)
            c_wait = c.time.sleep
            thread, errors = self.run_handover(lease, lambda r, signal='TERM': None, lambda r: None,
                                               sleep=lambda s: c_wait(0.01))
            thread.join(30)
        self.assertEqual([type(e) for e in errors], [RuntimeError])
        self.assertTrue((self.directory / 'owner.json').exists())

    def test_journal_lock_is_awaited_bounded(self):
        held = iter([True, True, False])
        naps = []
        c.handover(self.directory, timeout=0, boot_probe=lambda: 'b', observe=lambda r: None,
                   stop=lambda r, signal='TERM': None, journal=self.directory, lock_held=lambda j: next(held),
                   sleep=naps.append)
        self.assertEqual(len(naps), 2)
        with self.assertRaises(c.NeedsAttention):
            c.handover(self.directory, timeout=0, boot_probe=lambda: 'b', observe=lambda r: None,
                       stop=lambda r, signal='TERM': None, journal=self.directory, lock_held=lambda j: True,
                       force_wait=0.01, sleep=lambda s: None)


class GitRuntimeReuse(unittest.TestCase):
    def make_git(self, root):
        for name in ('cmd/git.exe', 'usr/bin/bash.exe'):
            f = root / name
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text('x')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name) / 'agent-hub'
        self.name = 'git-' + b.GIT_VERSION + '-x64'
        self.release = self.base / 'releases' / 'new-1'
        (self.release / 'tmp').mkdir(parents=True)

    def test_previous_release_runtime_is_copied_without_download(self):
        self.make_git(self.base / 'releases' / 'old-0' / 'runtimes' / self.name)
        target = self.release / 'runtimes' / self.name
        with patch.object(b, 'run', return_value='git version ' + b.GIT_VERSION.rsplit('.', 1)[0] + '.windows.4'):
            self.assertIsNotNone(b.reuse_git_runtime(self.release, target))
        self.assertTrue((target / 'cmd/git.exe').is_file() and (target / 'usr/bin/bash.exe').is_file())

    def test_install_root_runtime_wins_and_ensure_never_downloads(self):
        self.make_git(self.base / 'runtimes' / self.name)
        with patch.object(b.sys, 'platform', 'win32'), patch.object(b, 'architecture', return_value='x64'), \
                patch.object(b, 'run', return_value='git version ' + b.GIT_VERSION.rsplit('.', 1)[0]), \
                patch.object(b, 'download', side_effect=AssertionError('must not download')), \
                patch.dict(os.environ, {'PATH': ''}):
            target = b.ensure_windows_git(self.release)
        self.assertTrue((target / 'cmd/git.exe').is_file())

    def test_wrong_version_or_missing_runtime_is_not_reused(self):
        self.make_git(self.base / 'releases' / 'old-0' / 'runtimes' / self.name)
        target = self.release / 'runtimes' / self.name
        with patch.object(b, 'run', return_value='git version 1.0.0'):
            self.assertIsNone(b.reuse_git_runtime(self.release, target))
        self.assertFalse(target.exists())


class DownloadRetry(unittest.TestCase):
    def test_dns_failure_is_retried_with_backoff(self):
        import urllib.error
        import io
        calls, naps = [], []
        class Resp(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *a): return False
        def opener(req, timeout=0):
            calls.append(1)
            if len(calls) < 3:
                raise urllib.error.URLError('getaddrinfo failed')
            return Resp(b'data')
        with tempfile.TemporaryDirectory() as t, patch.object(b.urllib.request, 'urlopen', opener), \
                patch.object(b, 'DOWNLOAD_SLEEP', naps.append):
            target = Path(t) / 'f'
            b.download('https://example.test/f', target)
            self.assertEqual(target.read_bytes(), b'data')
        self.assertEqual(naps, [1.0, 2.0])


if __name__ == '__main__':
    unittest.main()
