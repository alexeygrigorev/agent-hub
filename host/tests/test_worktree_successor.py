"""Same-intake retries retain deletion protection through owner changes."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_hub_host import worktree_guard as guard
from agent_hub_host.fleet import Dispatcher
from test_worktree_guard import _make_repo, _add_worktree, _run


class WorktreeSuccessorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, HUB_WORKTREE_IDENTITY_DIR=self.tmp.name + '/identities')
        self.env.start()
        self.addCleanup(self.env.stop)
        self.repo = _make_repo(self.tmp.name)
        self.tree = _add_worktree(self.repo, self.tmp.name)
        self.old, self.new, self.launch = 'a' * 32, 'b' * 32, 'c' * 32
        self.assertTrue(guard.remember_repository(self.launch, self.tree))
        self.assertTrue(guard.ensure_locked('git', self.tree, self.old, 'intake-test'))
        self.fact = {'session_id': self.old, 'terminal': True, 'native_terminal': True,
                     'tag': 'task-hub-' + self.launch}

    def transfer(self, fact=None, intake='intake-test'):
        def request(method, path):
            self.assertEqual((method, path), ('GET', '/api/sessions/worktree-guards'))
            return {'sessions': [self.fact if fact is None else fact]}
        return guard.protect_successor_worktree(request, 'git', self.tree, self.new, intake)

    def test_terminal_same_intake_owner_changes_without_unlocking(self):
        # Reproduce the pre-fix gate: an existing session lock refuses retry.
        self.assertFalse(guard.ensure_locked('git', self.tree, self.new, 'intake-test'))
        real_replace = os.replace
        def replace(*args, **kwargs):
            self.assertTrue(guard.is_locked('git', self.tree))
            real_replace(*args, **kwargs)
            self.assertTrue(guard.is_locked('git', self.tree))
        with patch.object(os, 'replace', side_effect=replace), patch.object(guard, 'unlock_worktree') as unlock:
            self.assertTrue(self.transfer())
            unlock.assert_not_called()
        entry = guard.find_entry(guard.list_entries('git', self.tree), self.tree)
        self.assertEqual(entry['reason'], guard.lock_reason(self.new, 'intake-test'))
        self.assertNotEqual(_run(['git', 'worktree', 'remove', self.tree], self.repo).returncode, 0)
        self.assertFalse(guard.release_session_lock('git', self.tree, self.old, terminal=True, intake_state='done'))
        self.assertFalse(guard.release_session_lock('git', self.tree, self.new, terminal=True, intake_state='open'))
        self.assertTrue(guard.release_session_lock('git', self.tree, self.new, terminal=True, intake_state='done'))

    def test_live_unknown_foreign_or_unlinked_owner_remains_locked(self):
        for field in ('terminal', 'native_terminal'):
            for value in (False, None):
                with self.subTest(field=field, value=value):
                    self.assertFalse(self.transfer(dict(self.fact, **{field: value})))
        self.assertFalse(self.transfer(intake='another-intake'))
        self.assertFalse(self.transfer(intake=None))
        self.assertFalse(self.transfer(dict(self.fact, session_id=self.new)))
        self.assertTrue(guard.is_locked('git', self.tree))
        self.assertEqual(guard.find_entry(guard.list_entries('git', self.tree), self.tree)['reason'],
                         guard.lock_reason(self.old, 'intake-test'))

    def test_missing_repository_evidence_and_api_failure_refuse(self):
        self.assertFalse(self.transfer(dict(self.fact, tag='task-hub-' + 'd' * 32)))
        with patch.object(guard, 'repository_identity', return_value=None):
            self.assertFalse(self.transfer())
        def unavailable(*args):
            raise OSError('offline')
        self.assertFalse(guard.protect_successor_worktree(unavailable, 'git', self.tree, self.new, 'intake-test'))
        self.assertTrue(guard.is_locked('git', self.tree))

    def test_predecessor_started_in_subdirectory_of_same_checkout(self):
        nested = Path(self.tree) / 'nested'
        nested.mkdir()
        launch = 'e' * 32
        self.assertTrue(guard.remember_repository(launch, str(nested)))
        self.assertTrue(self.transfer(dict(self.fact, tag='task-hub-' + launch)))

    def test_lock_changed_after_server_lookup_refuses(self):
        def request(*args):
            lock = Path(_run(['git', 'rev-parse', '--absolute-git-dir'], self.tree).stdout.strip()) / 'locked'
            lock.write_text('operator protection\n')
            return {'sessions': [self.fact]}
        self.assertFalse(guard.protect_successor_worktree(request, 'git', self.tree, self.new, 'intake-test'))
        self.assertEqual(guard.find_entry(guard.list_entries('git', self.tree), self.tree)['reason'], 'operator protection')

    def test_old_lifecycle_unlock_cannot_race_successor_transfer(self):
        def request(*args):
            self.assertFalse(guard.release_session_lock('git', self.tree, self.old,
                             terminal=True, intake_state='done'))
            self.assertTrue(guard.is_locked('git', self.tree))
            return {'sessions': [self.fact]}
        self.assertTrue(guard.protect_successor_worktree(request, 'git', self.tree, self.new, 'intake-test'))

    def test_dispatcher_transfers_only_a_proven_terminal_predecessor(self):
        offer = {'id': 'd' * 32, 'hub_session_id': self.new, 'intake_task_id': 'intake-test'}
        requests = []
        def request(method, path):
            requests.append((method, path))
            return {'sessions': [self.fact]}
        dispatcher = Dispatcher({}, request)
        self.fact['native_terminal'] = False
        with self.assertRaisesRegex(ValueError, 'worktree_lock_failed'):
            dispatcher._lock_session_worktree(offer, self.tree)
        self.fact['native_terminal'] = True
        self.assertTrue(dispatcher._lock_session_worktree(offer, self.tree))
        # Two fresh guards lookups per attempt: the canonical stopped-lock
        # reuse check first, then the same-intake successor transfer.
        self.assertEqual([r for r in requests if r[1] == '/api/sessions/worktree-guards'],
                         [('GET', '/api/sessions/worktree-guards')] * 4)
        entry = guard.find_entry(guard.list_entries('git', self.tree), self.tree)
        self.assertEqual(entry['reason'], guard.lock_reason(self.new, 'intake-test'))
