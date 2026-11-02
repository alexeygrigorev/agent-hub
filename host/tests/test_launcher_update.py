"""Launcher update guards against real git checkouts and mocked Windows Python."""
import subprocess
import unittest
from unittest.mock import patch
from agent_hub_host import launcher_update as update
import test_self_update as fixtures
from test_self_update import FakeInstall, git


class LauncherUpdateTest(unittest.TestCase):
    setUp = fixtures.SelfUpdateTest.setUp
    tearDown = fixtures.SelfUpdateTest.tearDown
    commit = fixtures.SelfUpdateTest.commit

    def apply(self, run=None, target=None):
        return update.apply(target or self.second, self.host,
                            ['C:\\Python\\python.exe', '-m', 'launcher'], run=run or FakeInstall())

    def test_windows_interpreter_and_environment(self):
        calls = []
        fake = FakeInstall()
        def run(argv, **kw):
            if argv[0] != 'git':
                calls.append((argv, kw))
            return fake(argv, **kw)
        self.assertEqual(self.apply(run), ('updated', None, self.second))
        argv, kw = calls[0]
        self.assertEqual(argv[:2], ['C:\\Python\\python.exe', '-c'])
        self.assertEqual(kw['env']['PYTHONPATH'], str(self.host))
        self.assertEqual(kw['cwd'], str(self.host))
        self.assertNotIn('shell', kw)

    def test_dirty_tracked_and_untracked_preserved(self):
        for name in ['a.txt', 'untracked.txt']:
            with self.subTest(name=name):
                path = self.host / name
                path.write_text('private local change')
                self.assertEqual(self.apply()[:2], ('refused', 'dirty_checkout'))
                self.assertEqual(path.read_text(), 'private local change')
                if name == 'a.txt':
                    path.write_text('one')
                else:
                    path.unlink()

    def test_other_branch(self):
        git(self.host, 'checkout', '-qb', 'topic')
        self.assertEqual(self.apply()[:2], ('refused', 'not_on_main'))

    def test_invalid_unknown_and_downgrade(self):
        self.assertEqual(self.apply(target='oops')[:2], ('refused', 'invalid_sha'))
        self.assertEqual(self.apply(target='f'*40)[:2], ('refused', 'unknown_commit'))
        self.assertEqual(self.apply()[0], 'updated')
        self.assertEqual(self.apply(target=self.first)[:2], ('refused', 'not_fast_forward'))

    def test_smoke_failure_rolls_back(self):
        self.assertEqual(self.apply(FakeInstall('smoke')), ('failed', 'smoke_failed', self.first))
        self.assertEqual(git(self.host, 'rev-parse', 'HEAD'), self.first)

    def test_smoke_timeout_rolls_back_without_exposing_output(self):
        def run(argv, **kw):
            if argv[0] != 'git':
                raise subprocess.TimeoutExpired(argv, 120, output='credential-secret')
            return subprocess.run(argv, **kw)
        self.assertEqual(self.apply(run), ('failed', 'update_command_failed', self.first))
        self.assertEqual(git(self.host, 'rev-parse', 'HEAD'), self.first)

    def test_side_branch_refused(self):
        git(self.seed, 'checkout', '-qb', 'side')
        side = self.commit('b.txt', 'side')
        git(self.seed, 'push', '-q', 'origin', 'side')
        git(self.host, 'fetch', '-q', 'origin', 'side')
        self.assertEqual(self.apply(target=side)[:2], ('refused', 'not_on_origin_main'))

    def test_immutable_release_reports_identity_without_update(self):
        release = self.host.parent / ('a'*40)
        (release / 'launcher').mkdir(parents=True)
        self.assertEqual(update.identity(release), ('a'*40, False))
        self.assertEqual(update.apply(self.second, release, ['python', '-m', 'launcher'])[:2],
                         ('unsupported', 'not_a_git_checkout'))

    def test_report_has_root_current_and_state(self):
        sent = []
        def request(method, path, body=None):
            sent.append((method, path, body))
            return {'sha': self.second} if method == 'GET' else {}
        with patch('builtins.print'):
            update.step_once(request, {'launcher': {'root': str(self.host), 'argv': ['python.exe', '-m', 'launcher']}}, run=FakeInstall())
        self.assertEqual(sent[-1][2], {'root': str(self.host), 'current_sha': self.second, 'state': 'updated', 'error': None})

    def test_unsupported_wrapper_does_not_move_checkout(self):
        self.assertEqual(update.apply(self.second, self.host, ['launcher'])[:2], ('unsupported', 'unsupported_launcher_argv'))
        self.assertEqual(git(self.host, 'rev-parse', 'HEAD'), self.first)

    def test_windows_lock_released_after_exception(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        api = SimpleNamespace(locking=Mock(), LK_NBLCK=1, LK_UNLCK=2)
        with patch.dict('sys.modules', {'msvcrt': api}):
            with self.assertRaises(RuntimeError):
                with update.update_lock(self.host / '.git' / 'mock.lock', platform='win32') as acquired:
                    self.assertTrue(acquired)
                    raise RuntimeError('test')
        self.assertEqual([c.args[1:] for c in api.locking.call_args_list], [(1, 1), (2, 1)])

    def test_concurrent_update_refused(self):
        with update.update_lock(self.host / '.git' / 'hub-launcher-update.lock') as acquired:
            self.assertTrue(acquired)
            self.assertEqual(self.apply()[:2], ('refused', 'update_in_progress'))

    def test_operator_cli_pin_and_clear(self):
        import argparse
        from agent_hub_host import routing
        parser = argparse.ArgumentParser()
        routing.add_commands(parser.add_subparsers(dest='cmd', required=True))
        with patch.object(routing.bus, '_do_request', return_value={}) as request:
            routing.execute(parser.parse_args(['launcher-release', '--machine', 'tabo', '--sha', self.second.upper()]))
            self.assertEqual(request.call_args.args, ('PUT', '/api/fleet/routing/launcher-release/tabo', {'sha': self.second}))
            routing.execute(parser.parse_args(['launcher-release', '--machine', 'tabo', '--clear']))
            self.assertEqual(request.call_args.args[2], {'sha': None})
            with self.assertRaises(ValueError):
                routing.execute(parser.parse_args(['launcher-release', '--machine', 'tabo', '--sha', 'abc']))

    def test_real_import_failure_restores_working_launcher(self):
        import sys
        (self.seed / 'launcher').mkdir()
        self.commit('launcher/__init__.py', 'VALUE = 1\n', push=True)
        working = self.commit('launcher/cli.py', 'VALUE = 1\n', push=True)
        git(self.host, 'fetch', '-q', 'origin', 'main')
        git(self.host, 'merge', '--ff-only', '-q', working)
        # Force a real import failure in the candidate, not a mocked exit code.
        broken = self.commit('launcher/cli.py', 'raise RuntimeError("private credential-shaped error")\n', push=True)
        result = update.apply(broken, self.host, [sys.executable, '-m', 'launcher'])
        self.assertEqual(result, ('failed', 'smoke_failed', working))
        self.assertEqual(git(self.host, 'rev-parse', 'HEAD'), working)
        self.assertIsNone(update.smoke(self.host, [sys.executable, '-m', 'launcher'], subprocess.run))
        self.assertEqual(list((self.host / '.git').glob('hub-launcher-smoke-*')), [])

    def test_host_loop_polls_launcher_with_live_settings(self):
        import threading
        from agent_hub_host import self_update
        stop = threading.Event()
        settings = {'launcher': {'root': str(self.host)}}
        with patch.object(self_update, 'step_once', return_value=False), patch.object(update, 'step_once') as step:
            step.side_effect = lambda request, actual: stop.set()
            self_update.loop(lambda *a: None, stop, interval=0, launcher_settings=lambda: settings)
        self.assertIs(step.call_args.args[1], settings)
