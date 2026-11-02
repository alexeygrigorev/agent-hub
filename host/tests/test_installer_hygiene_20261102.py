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


if __name__ == '__main__':
    unittest.main()
