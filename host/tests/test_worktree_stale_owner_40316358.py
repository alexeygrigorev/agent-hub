"""Stale worktree lock owners are reclaimed only on fresh, aged native evidence."""
import datetime as dt
import os
import tempfile
import time
import unittest
from unittest.mock import patch

from agent_hub_host import worktree_guard as guard
from agent_hub_host.dispatch_diagnostics import MESSAGES, classify
from agent_hub_host.fleet import Dispatcher
from agent_hub_host.launch_diagnostics import exception_diagnostic
from agent_hub_host.native_sessions import NativeSession
from host.tests.test_worktree_guard import _add_worktree, _make_repo

OWNER = 'a' * 32
SUCCESSOR = 'b' * 32
LAUNCH = 'd' * 32
NATIVE = 'c1111111-1111-4222-8333-444455556666'
INTAKE = '40316358-intake'


def _iso(milliseconds):
    return dt.datetime.fromtimestamp(milliseconds / 1000, dt.timezone.utc).isoformat().replace('+00:00', 'Z')


def _native(*, phase='running', agent_state=None, activity=None):
    return NativeSession(session_id=NATIVE, tag='task-hub-' + LAUNCH,
                         workspace='/unused', phase=phase, agent_state=agent_state,
                         last_activity_ms=activity, created_at_ms=activity)


class LockOwnerDiagnosticsTests(unittest.TestCase):
    def test_owner_error_is_strict_and_classified_for_both_dispatch_paths(self):
        raw = guard.lock_owner_error(OWNER, 'non-terminal')
        self.assertEqual(guard.parse_lock_owner_error(raw), (OWNER, 'non-terminal'))
        for bad in (raw + ' SECRET', raw + ' /private/path',
                    raw.replace('hub sessions stop ' + OWNER, 'hub sessions stop ' + SUCCESSOR)):
            with self.subTest(bad=bad):
                self.assertIsNone(guard.parse_lock_owner_error(bad))

        dispatch = classify(ValueError(raw))
        self.assertEqual(dispatch['reason'], 'dispatch_worktree_refused')
        self.assertEqual(dispatch['message'], MESSAGES['dispatch_worktree_refused'])
        self.assertEqual((dispatch['lock_owner'], dispatch['lock_owner_state']), (OWNER, 'non-terminal'))
        self.assertEqual(dispatch['recovery'], 'hub sessions stop ' + OWNER)
        self.assertEqual(classify(ValueError(raw + ' SECRET'))['reason'], 'dispatch_internal_error')

        launch = exception_diagnostic(ValueError(raw), 'dispatch_headless')
        self.assertEqual(launch['reason'], 'launcher_worktree_lock_failed')
        self.assertEqual(launch['tail'], raw)
        unsafe = exception_diagnostic(ValueError(raw + ' SECRET'), 'dispatch_headless')
        self.assertEqual(unsafe['reason'], 'launcher_command_failed')
        self.assertNotIn('SECRET', str(unsafe))


class StaleOwnerTakeoverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._identity_env = patch.dict(os.environ, HUB_WORKTREE_IDENTITY_DIR=self.tmp.name + '/identities')
        self._identity_env.start()
        self.addCleanup(self._identity_env.stop)
        self.origin = _make_repo(self.tmp.name)
        self.tree = _add_worktree(self.origin, self.tmp.name, name='locked-tree', branch='locked-branch')
        self.now_ms = int(time.time() * 1000)
        self.old_activity = self.now_ms - 48 * 60 * 60 * 1000
        self.assertTrue(guard.remember_repository(LAUNCH, self.tree))
        self.assertTrue(guard.ensure_locked('git', self.tree, OWNER, INTAKE))

    def owner_fact(self, *, terminal=False, native_terminal=False, activity=None,
                   intake=INTAKE):
        return {'session_id': OWNER, 'intake_task_id': intake,
                'session_state': 'running',
                'terminal': terminal, 'native_terminal': native_terminal,
                'native_id': NATIVE, 'tag': 'task-hub-' + LAUNCH,
                'workspace': self.tree, 'native_created_at_ms': self.old_activity,
                'native_last_activity_at': _iso(self.old_activity if activity is None else activity)}

    def hub_request(self, fact=None):
        owner_fact = fact if fact is not None else self.owner_fact()

        def request(method, path, body=None):
            if path == '/api/fleet/worktree-owner/' + OWNER:
                return {'session_id': OWNER, 'intake_id': INTAKE, 'terminal': False}
            if path.startswith('/api/sessions/worktree-guards'):
                return {'sessions': [owner_fact]}
            return {}
        return request

    def start(self, *, fact=None, activity=None, native_rows=None, intake=INTAKE,
              settings=None, successor=SUCCESSOR):
        fact = fact if fact is not None else self.owner_fact()
        request = self.hub_request(fact)
        rows = [] if native_rows is None else native_rows
        with patch('agent_hub_host.native_sessions.list_sessions', return_value=rows) as list_native:
            dispatcher = Dispatcher(settings or {'worktree_stale_owner_hours': 24}, request)
            offer = {'id': 'e' * 32, 'hub_session_id': successor, 'intake_task_id': intake}
            result = dispatcher._lock_session_worktree(offer, self.tree)
        return result, list_native

    def owner_lock_reason(self):
        entry = guard.find_entry(guard.list_entries('git', self.tree), self.tree)
        return entry['reason'] if entry else None

    def test_gone_native_with_old_last_activity_transfers_lock_for_relay_start(self):
        fact = self.owner_fact(activity=self.old_activity)
        request = self.hub_request(fact)
        with patch('agent_hub_host.native_sessions.list_sessions', return_value=[]) as list_native:
            dispatcher = Dispatcher({'worktree_stale_owner_hours': 24}, request)
            offer = {'id': 'e' * 32, 'hub_session_id': SUCCESSOR, 'intake_task_id': INTAKE}
            self.assertTrue(dispatcher._lock_session_worktree(offer, self.tree))
        self.assertEqual(self.owner_lock_reason(), guard.lock_reason(SUCCESSOR, INTAKE))
        list_native.assert_called_with('aplexer', include_exited=True, fresh=True)

    def test_native_idle_over_bound_can_be_reclaimed_but_working_native_cannot(self):
        idle = _native(agent_state='idle', activity=self.old_activity)
        result, _ = self.start(native_rows=[idle])
        self.assertTrue(result)
        self.assertEqual(self.owner_lock_reason(), guard.lock_reason(SUCCESSOR, INTAKE))

        # A fresh idle verdict cannot override recent native activity.
        self.assertTrue(guard.unlock_worktree('git', self.tree))
        self.assertTrue(guard.ensure_locked('git', self.tree, OWNER, INTAKE))
        recent_idle = _native(agent_state='idle', activity=self.now_ms - 60 * 60 * 1000)
        with self.assertRaises(ValueError) as caught:
            self.start(native_rows=[recent_idle])
        self.assertEqual(guard.parse_lock_owner_error(str(caught.exception)),
                         (OWNER, 'running'))

        # Recreate the original owner lock for the active-session refusal.
        self.assertTrue(guard.unlock_worktree('git', self.tree))
        self.assertTrue(guard.ensure_locked('git', self.tree, OWNER, INTAKE))
        working = _native(agent_state='working', activity=self.old_activity)
        with self.assertRaises(ValueError) as caught:
            self.start(native_rows=[working])
        self.assertEqual(guard.parse_lock_owner_error(str(caught.exception)),
                         (OWNER, 'running'))

        # Missing a trustworthy idle verdict is also protected.
        with self.assertRaises(ValueError) as caught:
            self.start(native_rows=[_native(agent_state=None, activity=self.old_activity)])
        self.assertEqual(guard.parse_lock_owner_error(str(caught.exception)),
                         (OWNER, 'running'))
        self.assertEqual(self.owner_lock_reason(), guard.lock_reason(OWNER, INTAKE))

    def test_recent_activity_missing_timestamp_and_truncated_absence_fail_closed(self):
        recent = self.owner_fact(activity=self.now_ms - 60 * 60 * 1000)
        with self.assertRaises(ValueError) as caught:
            self.start(fact=recent)
        self.assertEqual(guard.parse_lock_owner_error(str(caught.exception)), (OWNER, 'running'))

        missing = self.owner_fact()
        missing['native_last_activity_at'] = None
        with self.assertRaises(ValueError) as caught:
            self.start(fact=missing)
        self.assertEqual(guard.parse_lock_owner_error(str(caught.exception)), (OWNER, 'running'))

        catalog = [NativeSession(session_id=f'{index:08x}-1111-4222-8333-444455556666',
                                 tag='other-native', workspace='/unused')
                   for index in range(500)]
        with self.assertRaises(ValueError) as caught:
            self.start(native_rows=catalog)
        self.assertEqual(guard.parse_lock_owner_error(str(caught.exception)), (OWNER, 'running'))
        self.assertEqual(self.owner_lock_reason(), guard.lock_reason(OWNER, INTAKE))

    def test_same_intake_is_required_and_invalid_bound_keeps_named_refusal(self):
        with self.assertRaises(ValueError) as caught:
            self.start(intake='another-intake')
        self.assertEqual(guard.parse_lock_owner_error(str(caught.exception)), (OWNER, 'running'))

        with self.assertRaises(ValueError) as caught:
            self.start(settings={'worktree_stale_owner_hours': 0})
        self.assertEqual(guard.parse_lock_owner_error(str(caught.exception)), (OWNER, 'running'))
        self.assertEqual(self.owner_lock_reason(), guard.lock_reason(OWNER, INTAKE))

    def test_unavailable_hub_facts_still_name_git_lock_owner(self):
        def unavailable(*args, **kwargs):
            raise RuntimeError('Hub unavailable')
        with self.assertRaises(ValueError) as caught:
            dispatcher = Dispatcher({}, unavailable)
            dispatcher._lock_session_worktree(
                {'id': 'e' * 32, 'hub_session_id': SUCCESSOR, 'intake_task_id': INTAKE}, self.tree)
        self.assertEqual(guard.parse_lock_owner_error(str(caught.exception)), (OWNER, 'unknown'))


if __name__ == '__main__':
    unittest.main()
