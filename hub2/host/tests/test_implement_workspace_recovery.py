"""Real disposable Git repositories exercise retained-intake recovery fences."""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from agent_hub_host import implement_workspace as module


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        # The runner supplies a workspace-owned TMPDIR; never create live repos.
        self.temp = tempfile.TemporaryDirectory(dir=os.environ['TMPDIR'])
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / 'enrolled'
        self.git(self.root, 'init', '-b', 'main', str(self.repo))
        self.git(self.repo, 'config', 'user.email', 'test@example.test')
        self.git(self.repo, 'config', 'user.name', 'test')
        (self.repo / 'source').write_text('main')
        self.git(self.repo, 'add', 'source')
        self.git(self.repo, 'commit', '-m', 'main')
        self.git(self.root, 'clone', '--bare', str(self.repo), str(self.root / 'origin.git'))
        self.git(self.repo, 'remote', 'add', 'origin', str(self.root / 'origin.git'))
        self.identity = {'intake_id': 'retained-intake', 'project_id': 'org/project'}
        digest = hashlib.sha256(json.dumps(self.identity, sort_keys=True).encode()).hexdigest()
        self.branch = 'hub/intake-' + digest
        self.ref = 'refs/heads/' + self.branch
        self.target = self.repo / '.local' / 'hub-worktrees' / digest

    def git(self, root, *args):
        return subprocess.run(['git', '-C', str(root), *args], capture_output=True,
                              text=True, check=True).stdout.strip()

    def prepare(self):
        return module.prepare_workspace(self.repo, 'implement', **self.identity)

    def retained(self):
        target = self.prepare()
        self.git(target, 'config', 'user.email', 'test@example.test')
        self.git(target, 'config', 'user.name', 'test')
        (target / 'owned').write_text('intake work beyond main')
        self.git(target, 'add', 'owned')
        self.git(target, 'commit', '-m', 'owned intake commit')
        tip = self.git(target, 'rev-parse', 'HEAD')
        self.git(self.repo, 'worktree', 'remove', str(target))
        return tip

    def test_cleaned_successor_retains_non_main_commit_branch_and_marker_offline(self):
        tip = self.retained()
        self.assertNotEqual(tip, self.git(self.repo, 'rev-parse', 'main'))
        self.git(self.repo, 'remote', 'set-url', 'origin', str(self.root / 'absent'))
        recovered = self.prepare()
        self.assertEqual(recovered, self.target)
        self.assertEqual(self.git(recovered, 'rev-parse', 'HEAD'), tip)
        self.assertEqual(self.git(recovered, 'symbolic-ref', 'HEAD'), self.ref)
        self.assertEqual((recovered / 'owned').read_text(), 'intake work beyond main')
        admin = Path(self.git(recovered, 'rev-parse', '--absolute-git-dir'))
        self.assertEqual(json.loads((admin / module._MARKER).read_text()), self.identity)
        self.assertEqual(self.git(self.repo, 'branch', '--show-current'), 'main')
        (recovered / 'owned').write_text('dirty continuation')
        self.assertEqual(self.prepare(), recovered)
        self.assertEqual(module.prepare_workspace(recovered, 'fix', **self.identity), recovered)
        self.assertEqual((recovered / 'owned').read_text(), 'dirty continuation')
        with self.assertRaisesRegex(ValueError, 'owner_conflict'):
            module.prepare_workspace(recovered, 'implement', 'foreign', 'org/project')

    def test_other_registered_worktree_is_preserved_even_if_missing_or_locked(self):
        tip = self.retained()
        other = self.root / 'other'
        self.git(self.repo, 'worktree', 'add', str(other), self.branch)
        self.git(self.repo, 'worktree', 'lock', str(other))
        with self.assertRaisesRegex(ValueError, 'branch_in_use'):
            self.prepare()
        self.assertEqual(self.git(other, 'rev-parse', 'HEAD'), tip)
        self.git(self.repo, 'worktree', 'unlock', str(other))
        moved = self.root / 'moved'
        other.rename(moved)
        with self.assertRaisesRegex(ValueError, 'branch_in_use'):
            self.prepare()
        self.assertEqual(self.git(self.repo, 'rev-parse', self.ref), tip)
        self.assertFalse(self.target.exists())

    def test_foreign_marker_symlink_and_partial_target_fail_closed(self):
        tip = self.retained()
        self.target.mkdir()
        (self.target / 'foreign').write_text('preserve')
        with self.assertRaisesRegex(ValueError, 'owner_conflict'):
            self.prepare()
        self.assertEqual((self.target / 'foreign').read_text(), 'preserve')
        (self.target / 'foreign').unlink()
        self.target.rmdir()
        if os.name != 'nt':
            self.target.symlink_to(self.repo, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, 'path_untrusted'):
                self.prepare()
            self.target.unlink()
        self.git(self.repo, 'worktree', 'add', str(self.target), self.branch)
        admin = Path(self.git(self.target, 'rev-parse', '--absolute-git-dir'))
        marker = admin / module._MARKER
        marker.write_text(json.dumps({**self.identity, 'project_id': 'foreign'}))
        with self.assertRaisesRegex(ValueError, 'owner_conflict'):
            self.prepare()
        marker.write_text('{malformed')
        with self.assertRaisesRegex(ValueError, 'owner_invalid'):
            self.prepare()
        if os.name != 'nt':
            marker.unlink()
            marker.symlink_to(self.repo / 'source')
            with self.assertRaisesRegex(ValueError, 'owner_invalid'):
                self.prepare()
        self.assertEqual(self.git(self.target, 'rev-parse', 'HEAD'), tip)

    def test_sanitized_git_failure_timeout_and_oserror(self):
        secret = 'https://user:secret@example.test Authorization: Bearer token'
        result = subprocess.CompletedProcess(['git', secret], 128, '', secret)
        with patch.object(module.subprocess, 'run', return_value=result):
            with self.assertRaises(ValueError) as raised:
                module._git(self.repo, 'worktree', 'add', secret)
        self.assertEqual(str(raised.exception), 'implement_worktree_git_failed operation=worktree exit_code=128')
        for error, reason in [(subprocess.TimeoutExpired(['git', secret], 120, stderr=secret), 'timeout'),
                              (OSError(secret), 'unavailable')]:
            with patch.object(module.subprocess, 'run', side_effect=error):
                with self.assertRaises(ValueError) as raised:
                    module._git(self.repo, 'fetch', secret)
            self.assertEqual(str(raised.exception), 'implement_worktree_git_failed operation=fetch reason=' + reason)
            self.assertTrue(raised.exception.__suppress_context__)

    def test_cross_process_preparation_lock_fails_closed(self):
        self.retained()
        # A separate Python process must honor the common-directory fence.
        script = '''
import sys
from agent_hub_host.implement_workspace import prepare_workspace
try:
    prepare_workspace(sys.argv[1], 'implement', 'retained-intake', 'org/project')
except ValueError as error:
    print(str(error))
'''
        with module._repository_lock(self.repo):
            result = subprocess.run([sys.executable, '-c', script, str(self.repo)],
                                    capture_output=True, text=True, check=True, timeout=15)
            self.assertEqual(result.stdout.strip(), 'implement_worktree_preparation_busy')
        self.assertEqual(self.prepare(), self.target)

    def test_concurrent_successors_reuse_one_worktree(self):
        tip = self.retained()
        with ThreadPoolExecutor(max_workers=2) as pool:
            paths = list(pool.map(lambda _: self.prepare(), range(2)))
        self.assertEqual(paths, [self.target, self.target])
        self.assertEqual(self.git(self.target, 'rev-parse', 'HEAD'), tip)

    def test_changed_tip_before_add_refused_without_rewriting(self):
        self.retained()
        real_tip = module._branch_tip
        count = 0
        def change(root, ref):
            nonlocal count
            count += 1
            if count == 2:
                self.git(self.repo, 'update-ref', ref, self.git(self.repo, 'rev-parse', 'main'))
            return real_tip(root, ref)
        with patch.object(module, '_branch_tip', side_effect=change):
            with self.assertRaisesRegex(ValueError, 'branch_changed'):
                self.prepare()
        self.assertFalse(self.target.exists())
        self.assertEqual(self.git(self.repo, 'rev-parse', self.ref), self.git(self.repo, 'rev-parse', 'main'))

    def test_changed_tip_during_add_leaves_unmarked_result(self):
        self.retained()
        real_git = module._git
        def change(root, *args):
            result = real_git(root, *args)
            if args[:2] == ('worktree', 'add'):
                self.git(self.repo, 'update-ref', self.ref, self.git(self.repo, 'rev-parse', 'main'))
            return result
        with patch.object(module, '_git', side_effect=change):
            with self.assertRaisesRegex(ValueError, 'branch_changed'):
                self.prepare()
        admin = Path(self.git(self.target, 'rev-parse', '--absolute-git-dir'))
        self.assertFalse((admin / module._MARKER).exists())
        with self.assertRaisesRegex(ValueError, 'owner_conflict'):
            self.prepare()

    def test_symbolic_branch_is_ambiguous(self):
        self.retained()
        self.git(self.repo, 'symbolic-ref', self.ref, 'refs/heads/main')
        with self.assertRaisesRegex(ValueError, 'branch_ambiguous'):
            self.prepare()
        self.assertFalse(self.target.exists())


if __name__ == '__main__':
    unittest.main()
