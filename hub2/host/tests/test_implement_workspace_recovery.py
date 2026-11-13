"""Real disposable Git repositories exercise retained-intake recovery fences."""
import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from hub2_agent_hub_host import implement_workspace as module


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
        self.digest = hashlib.sha256(json.dumps(self.identity, sort_keys=True).encode()).hexdigest()
        self.branch = 'hub/intake-' + self.digest
        self.ref = 'refs/heads/' + self.branch
        self.target = self.repo / '.local' / 'hub-worktrees' / self.digest

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
        common = Path(self.git(self.repo, 'rev-parse', '--path-format=absolute', '--git-common-dir'))
        record_path = common / module._OWNERS_DIR / f'{self.digest}.json'
        self.assertTrue(record_path.is_file())
        self.assertEqual(json.loads(record_path.read_text())['source'], 'prepare')
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
from hub2_agent_hub_host.implement_workspace import prepare_workspace
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

    def test_os_lock_releases_after_holder_process_killed(self):
        tip = self.retained()
        holder_script = '''
import sys, time
from hub2_agent_hub_host.implement_workspace import _repository_lock
with _repository_lock(sys.argv[1]):
    print('locked', flush=True)
    time.sleep(60)
'''
        proc = subprocess.Popen(
            [sys.executable, '-c', holder_script, str(self.repo)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            self.assertEqual(proc.stdout.readline().strip(), 'locked')
            with self.assertRaisesRegex(ValueError, 'preparation_busy'):
                self.prepare()
        finally:
            proc.kill()
            proc.wait(timeout=10)
            proc.stdout.close()
            proc.stderr.close()
        common = Path(self.git(self.repo, 'rev-parse', '--path-format=absolute', '--git-common-dir'))
        self.assertTrue((common / module._LOCK_FILE).exists())
        recovered = self.prepare()
        self.assertEqual(self.git(recovered, 'rev-parse', 'HEAD'), tip)
        fresh = module.prepare_workspace(self.repo, 'implement', 'fresh-after-kill', 'org/project')
        self.assertTrue(fresh.exists())

    def test_foreign_same_name_branch_without_record_fails_ownership_unverified(self):
        (self.repo / 'foreign').write_text('foreign')
        self.git(self.repo, 'add', 'foreign')
        self.git(self.repo, 'commit', '-m', 'foreign branch commit')
        foreign_tip = self.git(self.repo, 'rev-parse', 'HEAD')
        self.git(self.repo, 'branch', self.branch, foreign_tip)
        self.git(self.repo, 'reset', '--hard', 'HEAD~1')
        with self.assertRaisesRegex(ValueError, 'ownership_unverified'):
            self.prepare()
        self.assertFalse(self.target.exists())
        self.assertEqual(self.git(self.repo, 'rev-parse', self.ref), foreign_tip)
        common = Path(self.git(self.repo, 'rev-parse', '--path-format=absolute', '--git-common-dir'))
        self.assertFalse((common / module._OWNERS_DIR / f'{self.digest}.json').exists())

    def test_existing_legacy_worktree_marker_bootstraps_durable_record(self):
        self.git(self.repo, 'branch', self.branch, 'main')
        self.target.parent.mkdir(parents=True, exist_ok=True)
        self.git(self.repo, 'worktree', 'add', str(self.target), self.branch)
        admin = Path(self.git(self.target, 'rev-parse', '--absolute-git-dir'))
        (admin / module._MARKER).write_text(json.dumps(self.identity, sort_keys=True))
        common = Path(self.git(self.repo, 'rev-parse', '--path-format=absolute', '--git-common-dir'))
        record_path = common / module._OWNERS_DIR / f'{self.digest}.json'
        self.assertFalse(record_path.exists())
        self.assertEqual(self.prepare(), self.target)
        self.assertTrue(record_path.is_file())
        self.assertEqual(json.loads(record_path.read_text())['source'], 'legacy_worktree_bootstrap')
        self.git(self.target, 'config', 'user.email', 'test@example.test')
        self.git(self.target, 'config', 'user.name', 'test')
        (self.target / 'legacy').write_text('legacy work')
        self.git(self.target, 'add', 'legacy')
        self.git(self.target, 'commit', '-m', 'legacy commit')
        tip = self.git(self.target, 'rev-parse', 'HEAD')
        self.git(self.repo, 'worktree', 'remove', str(self.target))
        recovered = self.prepare()
        self.assertEqual(self.git(recovered, 'rev-parse', 'HEAD'), tip)

    def test_wrong_record_permissions_and_symlinks_fail_closed(self):
        self.retained()
        common = Path(self.git(self.repo, 'rev-parse', '--path-format=absolute', '--git-common-dir'))
        owners_dir = common / module._OWNERS_DIR
        record_path = owners_dir / f'{self.digest}.json'
        original = record_path.read_text()

        # Foreign record content is rejected and never overwritten.
        foreign_payload = json.dumps({
            'version': module._RECORD_VERSION,
            'intake_id': 'other-intake',
            'project_id': 'org/project',
            'ref': self.ref,
            'source': 'prepare',
            'phase': 'owned',
        })
        record_path.write_text(foreign_payload)
        with self.assertRaisesRegex(ValueError, 'owner_conflict'):
            self.prepare()
        self.assertEqual(record_path.read_text(), foreign_payload)

        # Malformed JSON is rejected and never overwritten.
        record_path.write_text('{broken')
        with self.assertRaisesRegex(ValueError, 'owner_invalid'):
            self.prepare()
        self.assertEqual(record_path.read_text(), '{broken')

        # Group/world-writable record is rejected.
        record_path.write_text(original)
        if os.name != 'nt':
            os.chmod(record_path, 0o660)
            with self.assertRaisesRegex(ValueError, 'owner_invalid'):
                self.prepare()
            os.chmod(record_path, 0o600)

            # Symlink record file is rejected.
            record_path.unlink()
            record_path.symlink_to(self.repo / 'source')
            with self.assertRaisesRegex(ValueError, 'owner_invalid'):
                self.prepare()
            record_path.unlink()

            # Symlink owners directory is rejected.
            owners_dir.rmdir()
            owners_dir.symlink_to(self.root, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, 'owner_invalid'):
                self.prepare()
            owners_dir.unlink()

            # Symlink lockfile is rejected.
            lock = common / module._LOCK_FILE
            if lock.exists():
                lock.unlink()
            lock.symlink_to(self.repo / 'source')
            with self.assertRaisesRegex(ValueError, 'path_untrusted'):
                self.prepare()
            lock.unlink()

    def test_explicit_legacy_adoption_positive_and_cas_refusals(self):
        (self.repo / 'legacy').write_text('legacy')
        self.git(self.repo, 'add', 'legacy')
        self.git(self.repo, 'commit', '-m', 'legacy branch commit')
        tip = self.git(self.repo, 'rev-parse', 'HEAD')
        self.git(self.repo, 'branch', self.branch, tip)
        self.git(self.repo, 'reset', '--hard', 'HEAD~1')
        main_tip = self.git(self.repo, 'rev-parse', 'main')

        with self.assertRaisesRegex(ValueError, 'ownership_unverified'):
            self.prepare()

        with self.assertRaisesRegex(ValueError, 'tip_mismatch'):
            module.adopt_legacy_intake_branch(
                self.repo, **self.identity, expected_ref=self.ref,
                expected_tip=main_tip, operator='alexey', reason='legacy cleanup',
            )
        with self.assertRaisesRegex(ValueError, 'ref_mismatch'):
            module.adopt_legacy_intake_branch(
                self.repo, **self.identity, expected_ref='refs/heads/main',
                expected_tip=tip, operator='alexey', reason='legacy cleanup',
            )
        with self.assertRaisesRegex(ValueError, 'provenance_invalid'):
            module.adopt_legacy_intake_branch(
                self.repo, **self.identity, expected_ref=self.ref,
                expected_tip=tip, operator='Bearer secret', reason='legacy cleanup',
            )
        with self.assertRaisesRegex(ValueError, 'provenance_invalid'):
            module.adopt_legacy_intake_branch(
                self.repo, **self.identity, expected_ref=self.ref,
                expected_tip=tip, operator='alexey', reason='',
            )

        other = self.root / 'active-legacy'
        self.git(self.repo, 'worktree', 'add', str(other), self.branch)
        with self.assertRaisesRegex(ValueError, 'branch_in_use'):
            module.adopt_legacy_intake_branch(
                self.repo, **self.identity, expected_ref=self.ref,
                expected_tip=tip, operator='alexey', reason='legacy cleanup',
            )
        self.git(self.repo, 'worktree', 'remove', str(other))

        # Concurrent lock contention during adoption fails closed.
        with module._repository_lock(self.repo):
            with self.assertRaisesRegex(ValueError, 'preparation_busy'):
                module.adopt_legacy_intake_branch(
                    self.repo, **self.identity, expected_ref=self.ref,
                    expected_tip=tip, operator='alexey', reason='legacy cleanup',
                )

        adopted = module.adopt_legacy_intake_branch(
            self.repo, **self.identity, expected_ref=self.ref,
            expected_tip=tip, operator='alexey', reason='verified legacy cleanup',
        )
        self.assertEqual(adopted['source'], 'operator_legacy_adoption')
        self.assertEqual(adopted['tip'], tip)
        self.assertFalse(adopted['already_recorded'])
        record_path = Path(adopted['record_path'])
        if os.name != 'nt':
            self.assertEqual(record_path.stat().st_mode & 0o777, 0o600)

        again = module.adopt_legacy_intake_branch(
            self.repo, **self.identity, expected_ref=self.ref,
            expected_tip=tip, operator='alexey', reason='idempotent check',
        )
        self.assertTrue(again['already_recorded'])

        recovered = self.prepare()
        self.assertEqual(recovered, self.target)
        self.assertEqual(self.git(recovered, 'rev-parse', 'HEAD'), tip)
        self.assertEqual((recovered / 'legacy').read_text(), 'legacy')


    # --- AB0516 follow-up regressions -------------------------------------

    def common(self):
        return Path(self.git(self.repo, 'rev-parse', '--path-format=absolute', '--git-common-dir'))

    def record_path(self):
        return self.common() / module._OWNERS_DIR / f'{self.digest}.json'

    def fsynced_inodes(self, action):
        seen = set()
        real = os.fsync
        def spy(fd):
            st = os.fstat(fd)
            seen.add((st.st_dev, st.st_ino))
            return real(fd)
        with patch.object(module.os, 'fsync', side_effect=spy):
            result = action()
        return result, seen

    def chain_inodes(self):
        paths = [self.record_path(), self.common() / module._OWNERS_DIR, self.common()]
        return {(p.stat().st_dev, p.stat().st_ino) for p in paths}

    @unittest.skipIf(os.name == 'nt', 'directory fsync is POSIX-only')
    def test_new_record_fsyncs_file_owners_dir_and_common_dir(self):
        target, seen = self.fsynced_inodes(self.prepare)
        self.assertEqual(target, self.target)
        self.assertLessEqual(self.chain_inodes(), seen)
        data = json.loads(self.record_path().read_text())
        self.assertEqual((data['version'], data['phase'], data['source']),
                         (module._RECORD_VERSION, 'owned', 'prepare'))

    @unittest.skipIf(os.name == 'nt', 'directory fsync is POSIX-only')
    def test_existing_record_retries_repair_full_chain_durability(self):
        self.retained()
        # Retained-branch retry with an existing record.
        target, seen = self.fsynced_inodes(self.prepare)
        self.assertEqual(target, self.target)
        self.assertLessEqual(self.chain_inodes(), seen)
        # Continuation retries (target path and bound worktree root).
        for action in (self.prepare,
                       lambda: module.prepare_workspace(self.target, 'fix', **self.identity)):
            _, seen = self.fsynced_inodes(action)
            self.assertLessEqual(self.chain_inodes(), seen)

    def test_windows_record_flush_uses_writable_nontruncating_open(self):
        windows_flags = module._record_durability_open_flags(windows=True)
        self.assertEqual(windows_flags & (os.O_WRONLY | os.O_RDWR), os.O_RDWR)
        self.assertFalse(windows_flags & getattr(os, 'O_TRUNC', 0))
        self.assertFalse(windows_flags & (os.O_CREAT | os.O_EXCL))
        if hasattr(os, 'O_NOFOLLOW'):
            self.assertTrue(windows_flags & os.O_NOFOLLOW)

        record_path = self.record_path()
        real_open = os.open
        observed = []

        def capture_record_open(path, flags, *args, **kwargs):
            if Path(path) == record_path:
                observed.append(flags)
            return real_open(path, flags, *args, **kwargs)

        with patch.object(module, '_record_durability_open_flags',
                          return_value=windows_flags), \
                patch.object(module.os, 'open', side_effect=capture_record_open):
            target = self.prepare()
            before = record_path.read_bytes()
            module._make_record_durable(self.common(), self.digest)

        self.assertEqual(target, self.target)
        self.assertEqual(observed[-2:], [windows_flags, windows_flags])
        self.assertEqual(record_path.read_bytes(), before)

    def test_windows_record_fsync_failure_fails_closed_without_changing_bytes(self):
        self.prepare()
        record_path = self.record_path()
        before = record_path.read_bytes()
        windows_flags = module._record_durability_open_flags(windows=True)
        with patch.object(module, '_record_durability_open_flags',
                          return_value=windows_flags), \
                patch.object(module.os, 'fsync', side_effect=OSError('injected')):
            with self.assertRaisesRegex(ValueError, 'owner_durability_failed'):
                module._make_record_durable(self.common(), self.digest)
        self.assertEqual(record_path.read_bytes(), before)

    @unittest.skipIf(os.name == 'nt', 'directory fsync is POSIX-only')
    def test_directory_fsync_failure_fails_closed_before_success(self):
        tip = self.retained()
        real = os.fsync
        def fail_dirs(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError('injected')
            return real(fd)
        with patch.object(module.os, 'fsync', side_effect=fail_dirs):
            with self.assertRaisesRegex(ValueError, 'owner_durability_failed'):
                self.prepare()
        self.assertFalse(self.target.exists())
        self.assertEqual(self.git(self.repo, 'rev-parse', self.ref), tip)
        self.assertEqual(self.git(self.prepare(), 'rev-parse', 'HEAD'), tip)

    def test_foreign_ref_injected_before_add_never_auto_assigned(self):
        (self.repo / 'foreign').write_text('foreign')
        self.git(self.repo, 'add', 'foreign')
        self.git(self.repo, 'commit', '-m', 'foreign commit')
        foreign_tip = self.git(self.repo, 'rev-parse', 'HEAD')
        self.git(self.repo, 'reset', '--hard', 'HEAD~1')
        real_git = module._git
        def inject(root, *args):
            if args[:3] == ('worktree', 'add', '-b'):
                self.git(self.repo, 'update-ref', self.ref, foreign_tip, '')
            return real_git(root, *args)
        with patch.object(module, '_git', side_effect=inject):
            with self.assertRaisesRegex(ValueError, 'git_failed operation=worktree'):
                self.prepare()
        self.assertFalse(self.record_path().exists())
        self.assertFalse(self.target.exists())
        self.assertEqual(self.git(self.repo, 'rev-parse', self.ref), foreign_tip)
        with self.assertRaisesRegex(ValueError, 'ownership_unverified'):
            self.prepare()
        self.assertFalse(self.record_path().exists())
        self.assertFalse(self.target.exists())
        self.assertEqual(self.git(self.repo, 'rev-parse', self.ref), foreign_tip)
        self.assertNotIn('branch ' + self.ref, self.git(self.repo, 'worktree', 'list', '--porcelain'))

    def test_premature_pre_creation_v1_record_is_rejected(self):
        self.git(self.repo, 'branch', self.branch, 'main')
        owners = self.common() / module._OWNERS_DIR
        owners.mkdir(mode=0o700)
        self.record_path().write_text(json.dumps({
            'version': 1, 'source': 'prepare', 'ref': self.ref, **self.identity}))
        os.chmod(self.record_path(), 0o600)
        with self.assertRaisesRegex(ValueError, 'owner_invalid'):
            self.prepare()
        self.assertFalse(self.target.exists())

    def legacy_marked_worktree(self):
        self.target.parent.mkdir(parents=True, exist_ok=True)
        self.git(self.repo, 'worktree', 'add', '-b', 'legacy-renamed', str(self.target), 'main')
        admin = Path(self.git(self.target, 'rev-parse', '--absolute-git-dir'))
        (admin / module._MARKER).write_text(json.dumps(self.identity, sort_keys=True))

    def foreign_same_name_ref(self):
        (self.repo / 'foreign').write_text('foreign')
        self.git(self.repo, 'add', 'foreign')
        self.git(self.repo, 'commit', '-m', 'foreign commit')
        foreign_tip = self.git(self.repo, 'rev-parse', 'HEAD')
        self.git(self.repo, 'branch', self.branch, foreign_tip)
        self.git(self.repo, 'reset', '--hard', 'HEAD~1')
        return foreign_tip

    def assert_legacy_mismatch_preserved_without_minting(self, foreign_tip):
        (self.target / 'source').write_text('dirty legacy continuation')
        self.assertEqual(self.prepare(), self.target)
        self.assertEqual(module.prepare_workspace(self.target, 'fix', **self.identity), self.target)
        self.assertEqual((self.target / 'source').read_text(), 'dirty legacy continuation')
        self.assertFalse(self.record_path().exists())
        self.assertEqual(self.git(self.repo, 'rev-parse', self.ref), foreign_tip)
        self.git(self.repo, 'worktree', 'remove', '--force', str(self.target))
        with self.assertRaisesRegex(ValueError, 'ownership_unverified'):
            self.prepare()
        self.assertFalse(self.record_path().exists())
        self.assertEqual(self.git(self.repo, 'rev-parse', self.ref), foreign_tip)

    def test_legacy_marker_on_renamed_branch_never_mints_derived_ownership(self):
        foreign_tip = self.foreign_same_name_ref()
        self.legacy_marked_worktree()
        self.assertEqual(self.git(self.target, 'symbolic-ref', 'HEAD'), 'refs/heads/legacy-renamed')
        self.assert_legacy_mismatch_preserved_without_minting(foreign_tip)

    def test_legacy_marker_on_detached_head_never_mints_derived_ownership(self):
        foreign_tip = self.foreign_same_name_ref()
        self.legacy_marked_worktree()
        self.git(self.target, 'checkout', '--detach')
        self.assert_legacy_mismatch_preserved_without_minting(foreign_tip)

if __name__ == '__main__':
    unittest.main()
