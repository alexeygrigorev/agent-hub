"""Hub-pinned host self-update (intake a3227276) against real git repositories."""
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

from agent_hub_host import self_update


def git(cwd, *args):
    return subprocess.run(['git', '-C', str(cwd), *args], check=True, capture_output=True, text=True).stdout.strip()


class FakeInstall:
    """Real git, fake pip/smoke: records interpreter calls, fails on demand."""

    def __init__(self, fail=None):
        self.fail = fail
        self.calls = []

    def __call__(self, argv, **kwargs):
        if argv[0] == 'git':
            return subprocess.run(argv, **kwargs)
        kind = 'pip' if argv[1:3] == ['-m', 'pip'] else 'smoke'
        self.calls.append(kind)
        ok = self.fail != kind
        return subprocess.CompletedProcess(argv, 0 if ok else 1, 'smoke ok' if ok and kind == 'smoke' else '', '')


class SelfUpdateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.origin = base / 'origin.git'
        self.seed = base / 'seed'
        self.host = base / 'host'
        subprocess.run(['git', 'init', '--bare', '-q', '-b', 'main', str(self.origin)], check=True)
        subprocess.run(['git', 'init', '-q', '-b', 'main', str(self.seed)], check=True)
        for repo in (self.seed,):
            git(repo, 'config', 'user.email', 't@example.invalid')
            git(repo, 'config', 'user.name', 't')
        self.commit('a.txt', 'one')
        git(self.seed, 'remote', 'add', 'origin', str(self.origin))
        git(self.seed, 'push', '-q', 'origin', 'main')
        subprocess.run(['git', 'clone', '-q', str(self.origin), str(self.host)], check=True)
        git(self.host, 'config', 'user.email', 't@example.invalid')
        git(self.host, 'config', 'user.name', 't')
        self.first = git(self.host, 'rev-parse', 'HEAD')
        self.second = self.commit('a.txt', 'two', push=True)

    def tearDown(self):
        self.tmp.cleanup()

    def commit(self, name, text, push=False):
        (self.seed / name).write_text(text)
        git(self.seed, 'add', name)
        git(self.seed, 'commit', '-q', '-m', text)
        if push:
            git(self.seed, 'push', '-q', 'origin', 'main')
        return git(self.seed, 'rev-parse', 'HEAD')

    def test_fast_forwards_to_pinned_main_commit_and_installs(self):
        run = FakeInstall()
        self.assertEqual(self_update.apply(self.second, self.host, run=run, python='py'), ('updated', None, self.second))
        self.assertEqual(git(self.host, 'rev-parse', 'HEAD'), self.second)
        self.assertEqual(run.calls, ['pip', 'smoke'])

    def test_current_commit_is_a_no_op(self):
        run = FakeInstall()
        self.assertEqual(self_update.apply(self.first, self.host, run=run, python='py'), ('current', None, self.first))
        self.assertEqual(run.calls, [])

    def test_refuses_dirty_tracked_files_without_touching_them(self):
        (self.host / 'a.txt').write_text('local work')
        state = self_update.apply(self.second, self.host, run=FakeInstall(), python='py')
        self.assertEqual(state, ('refused', 'dirty_checkout', self.first))
        self.assertEqual((self.host / 'a.txt').read_text(), 'local work')

    def test_untracked_files_do_not_block_and_survive(self):
        (self.host / 'notes.txt').write_text('keep')
        self.assertEqual(self_update.apply(self.second, self.host, run=FakeInstall(), python='py')[0], 'updated')
        self.assertEqual((self.host / 'notes.txt').read_text(), 'keep')

    def test_refuses_other_branch(self):
        git(self.host, 'checkout', '-q', '-b', 'topic')
        self.assertEqual(self_update.apply(self.second, self.host, run=FakeInstall(), python='py')[:2], ('refused', 'not_on_main'))

    def test_refuses_commit_not_on_origin_main(self):
        git(self.seed, 'checkout', '-q', '-b', 'side')
        side = self.commit('b.txt', 'side')
        git(self.seed, 'push', '-q', 'origin', 'side')
        git(self.host, 'fetch', '-q', 'origin', 'side')
        self.assertEqual(self_update.apply(side, self.host, run=FakeInstall(), python='py')[:2], ('refused', 'not_on_origin_main'))
        self.assertEqual(self_update.apply('f' * 40, self.host, run=FakeInstall(), python='py')[:2], ('refused', 'unknown_commit'))
        self.assertEqual(git(self.host, 'rev-parse', 'HEAD'), self.first)

    def test_refuses_downgrade(self):
        self.assertEqual(self_update.apply(self.second, self.host, run=FakeInstall(), python='py')[0], 'updated')
        self.assertEqual(self_update.apply(self.first, self.host, run=FakeInstall(), python='py')[:2], ('refused', 'not_fast_forward'))

    def test_failed_smoke_restores_previous_commit(self):
        run = FakeInstall(fail='smoke')
        self.assertEqual(self_update.apply(self.second, self.host, run=run, python='py'), ('failed', 'smoke_failed', self.first))
        self.assertEqual(git(self.host, 'rev-parse', 'HEAD'), self.first)
        self.assertEqual(git(self.host, 'branch', '--show-current'), 'main')
        self.assertEqual(run.calls, ['pip', 'smoke', 'pip', 'smoke'])

    def test_release_dir_without_git_is_unsupported(self):
        with tempfile.TemporaryDirectory() as plain:
            self.assertEqual(self_update.apply(self.second, plain, run=FakeInstall(), python='py'), ('unsupported', 'not_a_git_checkout', None))

    def test_step_reports_and_requests_restart_only_after_update(self):
        sent = []
        pinned = {'sha': self.second}

        def request(method, path, body=None):
            sent.append((method, path, body))
            return dict(pinned) if method == 'GET' else {'status': 'recorded'}

        logs = []
        self.assertTrue(self_update.step_once(request, root=self.host, run=FakeInstall(), python='py', log=lambda *a, **k: logs.append(a)))
        self.assertEqual(sent[-1], ('POST', '/api/fleet/routing/host-release/report', {'current_sha': self.second, 'state': 'updated', 'error': None}))
        self.assertFalse(self_update.step_once(request, root=self.host, run=FakeInstall(), python='py', log=lambda *a, **k: None))
        self.assertEqual(sent[-1][2]['state'], 'current')
        pinned['sha'] = None
        self.assertFalse(self_update.step_once(request, root=self.host, run=FakeInstall(), python='py', log=lambda *a, **k: None))
        self.assertEqual(sent[-1][2], {'current_sha': self.second, 'state': 'current', 'error': None})

    def test_loop_sets_stop_after_update(self):
        stop = threading.Event()
        calls = {'n': 0}
        original = self_update.step_once

        def step(request, root=None):
            calls['n'] += 1
            return True
        self_update.step_once = step
        try:
            self_update.loop(lambda *a: None, stop, interval=1)
        finally:
            self_update.step_once = original
        self.assertTrue(stop.is_set())
        self.assertEqual(calls['n'], 1)


if __name__ == '__main__':
    unittest.main()
