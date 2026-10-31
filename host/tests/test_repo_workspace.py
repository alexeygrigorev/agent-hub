"""Repo-identity workspaces: the host clones its own work tree (intake 8028bb06)."""
import subprocess
import tempfile
import unittest
from pathlib import Path, PureWindowsPath
from unittest import mock

from agent_hub_host import fleet, repo_workspace

LABEL = 'repo-8028bb06-alexeygrigorev__agent-hub'


class DecodeTests(unittest.TestCase):
    def test_decode(self):
        self.assertEqual(repo_workspace.decode(LABEL), ('8028bb06', 'alexeygrigorev', 'agent-hub'))
        self.assertEqual(repo_workspace.decode('repo-8028bb06-org__a_d_b')[2], 'a.b')
        for bad in ('agent-hub', '/home/alexey/git/agent-hub', 'repo-zzzzzzzz-a__b', 'repo-8028bb06-a__'):
            self.assertIsNone(repo_workspace.decode(bad))

    def test_windows_work_root_layout(self):
        # %USERPROFILE%\work\<id>\<repo>
        with mock.patch.object(repo_workspace, 'Path', PureWindowsPath):
            path = repo_workspace.repo_path({}, LABEL, home='C:\\Users\\alexey')
        self.assertEqual(str(path), 'C:\\Users\\alexey\\work\\8028bb06\\agent-hub')


class EnsureTests(unittest.TestCase):
    def runner(self, calls, fail_gh=False):
        def run(argv, cwd, timeout):
            calls.append((argv, cwd))
            if argv[1:3] == ['repo', 'clone'] and fail_gh:
                return subprocess.CompletedProcess(argv, 1, '', 'gh: not logged in')
            if 'clone' in argv:
                Path(argv[-1]).mkdir(parents=True)
                (Path(argv[-1]) / '.git').mkdir()
            return subprocess.CompletedProcess(argv, 0, '', '')
        return run

    def test_clones_into_own_work_root_from_origin_main(self):
        with tempfile.TemporaryDirectory() as home:
            calls = []
            path = repo_workspace.ensure_repo_workspace({}, LABEL, home=home, runner=self.runner(calls), gh_bin='gh')
            self.assertEqual(path, (Path(home) / 'work' / '8028bb06' / 'agent-hub').resolve())
            self.assertEqual(calls[0][0][:4], ['gh', 'repo', 'clone', 'alexeygrigorev/agent-hub'])
            self.assertIn(['git', 'fetch', '--quiet', 'origin', 'main'], [c[0] for c in calls])
            self.assertIn(['git', 'checkout', '--quiet', '-B', 'wt-8028bb06', 'origin/main'], [c[0] for c in calls])
            # second dispatch reuses the checkout, no new commands
            before = len(calls)
            repo_workspace.ensure_repo_workspace({}, LABEL, home=home, runner=self.runner(calls), gh_bin='gh')
            self.assertEqual(len(calls), before)

    def test_falls_back_to_git_clone_and_honours_work_root_setting(self):
        with tempfile.TemporaryDirectory() as root:
            calls = []
            path = repo_workspace.ensure_repo_workspace({'work_root': root}, LABEL, runner=self.runner(calls, fail_gh=True), gh_bin='gh')
            self.assertEqual(path, (Path(root) / '8028bb06' / 'agent-hub').resolve())
            self.assertEqual(calls[1][0][:3], ['git', 'clone', '--quiet'])
            self.assertTrue(calls[1][0][3].endswith('github.com/alexeygrigorev/agent-hub.git'))

    def test_clone_failure_is_reported(self):
        with tempfile.TemporaryDirectory() as home:
            run = lambda argv, cwd, timeout: subprocess.CompletedProcess(argv, 128, '', 'fatal: repository not found')
            with self.assertRaisesRegex(ValueError, 'repo clone failed'):
                repo_workspace.ensure_repo_workspace({}, LABEL, home=home, runner=run, gh_bin='')


class DispatchIntegrationTests(unittest.TestCase):
    def test_route_and_resolve_accept_repo_workspace_without_a_path(self):
        with tempfile.TemporaryDirectory() as root:
            settings = {'workspaces': {}, 'work_root': root, 'subscriptions': [
                {'service': 'gemini', 'account_label': 'shared-gemini', 'engine': 'antigravity', 'admission_argv': ['true']}]}
            sub = fleet.Dispatcher(settings)._route({'account': 'shared-gemini', 'engine': 'antigravity',
                                                     'profile': None, 'workspace': LABEL})
            self.assertEqual(sub['account_label'], 'shared-gemini')
            with self.assertRaises(ValueError):
                fleet.resolve_workspace_path(settings, LABEL)  # not cloned yet
            (Path(root) / '8028bb06' / 'agent-hub').mkdir(parents=True)
            self.assertEqual(fleet.resolve_workspace_path(settings, LABEL), (Path(root) / '8028bb06' / 'agent-hub').resolve())
            self.assertTrue(fleet.absolute_workspace_ok('C:\\Users\\alexey\\work\\8028bb06\\agent-hub'))

    def test_host_advertises_repo_workspaces(self):
        import inspect
        self.assertIn("caps['repo_workspaces'] = True", inspect.getsource(fleet.Dispatcher))


if __name__ == '__main__':
    unittest.main()
