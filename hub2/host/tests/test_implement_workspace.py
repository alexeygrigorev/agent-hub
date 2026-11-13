import os
import subprocess
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from agent_hub_host.implement_workspace import prepare_workspace


class ImplementWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.origin = self.root / 'origin.git'
        self.seed = self.root / 'seed'
        self.shared = self.root / 'shared'
        self.git(self.root, 'init', '--bare', str(self.origin))
        self.git(self.root, 'init', '-b', 'main', str(self.seed))
        self.git(self.seed, 'config', 'user.email', 'test@example.test')
        self.git(self.seed, 'config', 'user.name', 'test')
        (self.seed / 'tracked').write_text('main')
        self.git(self.seed, 'add', 'tracked')
        self.git(self.seed, 'commit', '-m', 'main')
        self.git(self.seed, 'remote', 'add', 'origin', str(self.origin))
        self.git(self.seed, 'push', 'origin', 'main')
        self.git(self.root, 'clone', '-b', 'main', str(self.origin), str(self.shared))
        self.git(self.shared, 'switch', '-c', 'old-merged-feature')

    def git(self, root, *args):
        return subprocess.run(['git', '-C', str(root), *args], check=True,
                              capture_output=True, text=True).stdout.strip()

    def test_new_intakes_use_fresh_main_and_continuation_keeps_dirty_work(self):
        # Advance origin after the shared checkout was cloned.
        (self.seed / 'tracked').write_text('fresh-main')
        self.git(self.seed, 'commit', '-am', 'advance origin')
        self.git(self.seed, 'push', 'origin', 'main')
        expected = self.git(self.seed, 'rev-parse', 'HEAD')
        first = prepare_workspace(self.shared, 'implement', 'intake-1', 'org/repo')
        self.assertEqual(self.git(first, 'rev-parse', 'HEAD'), expected)
        self.assertEqual(self.git(self.shared, 'branch', '--show-current'), 'old-merged-feature')
        (first / 'tracked').write_text('work in progress')
        with patch('agent_hub_host.implement_workspace.subprocess.run', wraps=subprocess.run) as run:
            self.assertEqual(prepare_workspace(self.shared, None, 'intake-1', 'org/repo'), first)
            self.assertEqual(prepare_workspace(first, 'implement', 'intake-1', 'org/repo'), first)
            self.assertFalse(any('fetch' in call.args[0] for call in run.call_args_list))
        self.assertEqual((first / 'tracked').read_text(), 'work in progress')
        second = prepare_workspace(self.shared, 'implement', 'intake-2', 'org/repo')
        self.assertNotEqual(first, second)
        self.assertEqual(self.git(second, 'rev-parse', 'HEAD'), expected)
        with self.assertRaisesRegex(ValueError, 'owner_conflict'):
            prepare_workspace(first, 'implement', 'foreign-intake', 'org/repo')

    def test_label_launch_ack_and_bound_continuation_use_intake_worktree(self):
        from test_host import HostTests
        fixture = HostTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.addCleanup(fixture.tearDown)
        checkout = fixture.root / 'relay'
        self.git(fixture.root, 'clone', '-b', 'main', str(self.origin), str(checkout))
        offer = fixture.offer()
        offer['launch'].update(workspace='relay', purpose='implement')
        acknowledgements = []
        fixture.bridge.offer(offer, lambda *args: acknowledgements.append(args))
        resolved = Path(fixture.native.process['workspace'])
        self.assertNotEqual(resolved, checkout)
        self.assertEqual(acknowledgements[0][2]['resolved_workspace'], str(resolved))
        self.assertEqual(self.git(resolved, 'rev-parse', 'HEAD'), self.git(self.seed, 'rev-parse', 'HEAD'))
        continuation = fixture.offer(operation_id='continuation', session_id='continuation')
        continuation['launch'].update(workspace=str(resolved), purpose='implement')
        self.assertEqual(fixture.bridge.validate_launch(continuation)['workspace'], str(resolved))
        # Old offers remain reconcilable after the isolation rollout.
        offer['launch']['purpose'] = 'implement'
        fixture.bridge.offer(offer)

    def test_concurrent_same_intake_reuses_and_different_intakes_isolate(self):
        with ThreadPoolExecutor(max_workers=3) as pool:
            paths = list(pool.map(lambda intake: prepare_workspace(self.shared, 'implement', intake, 'org/repo'), ['one', 'two', 'one']))
        self.assertEqual(paths[0], paths[2])
        self.assertNotEqual(paths[0], paths[1])

    def test_read_purposes_need_no_git_and_failed_fetch_does_not_launch_shared(self):
        directory = self.root / 'non-git'
        directory.mkdir()
        for purpose in ('review', 'think'):
            self.assertEqual(prepare_workspace(directory, purpose, None, None), directory)
        self.git(self.shared, 'remote', 'set-url', 'origin', str(self.root / 'missing'))
        with self.assertRaisesRegex(ValueError, 'git_failed'):
            prepare_workspace(self.shared, 'implement', 'one', 'org/repo')
        self.assertEqual(self.git(self.shared, 'branch', '--show-current'), 'old-merged-feature')

    def test_foreign_path_and_symlink_refused(self):
        directory = self.shared / '.local' / 'hub-worktrees'
        directory.parent.mkdir()
        if os.name != 'nt':
            directory.symlink_to(self.root, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, 'path_untrusted'):
                prepare_workspace(self.shared, 'implement', 'one', 'org/repo')

    def test_default_branch_master_when_origin_has_no_main(self):
        origin = self.root / 'origin-master.git'
        seed = self.root / 'seed-master'
        checkout = self.root / 'checkout-master'
        self.git(self.root, 'init', '--bare', str(origin))
        self.git(self.root, 'init', '-b', 'master', str(seed))
        self.git(seed, 'config', 'user.email', 'test@example.test')
        self.git(seed, 'config', 'user.name', 'test')
        (seed / 'tracked').write_text('master')
        self.git(seed, 'add', 'tracked')
        self.git(seed, 'commit', '-m', 'master')
        self.git(seed, 'remote', 'add', 'origin', str(origin))
        self.git(seed, 'push', 'origin', 'master')
        self.git(origin, 'symbolic-ref', 'HEAD', 'refs/heads/master')
        self.git(self.root, 'clone', '-b', 'master', str(origin), str(checkout))
        expected = self.git(seed, 'rev-parse', 'HEAD')
        wt = prepare_workspace(checkout, 'implement', 'intake-master', 'org/repo-master')
        self.assertEqual(self.git(wt, 'rev-parse', 'HEAD'), expected)
        self.assertEqual((wt / 'tracked').read_text(), 'master')


if __name__ == '__main__':
    unittest.main()
