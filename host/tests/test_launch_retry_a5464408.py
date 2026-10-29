"""Launch recovery must survive death between a Hub claim and native spawn."""
import json
import copy
import tempfile
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

from agent_hub_host.fleet import Dispatcher
from agent_hub_host.bus import HttpRequestError
from agent_hub_host.dispatch_diagnostics import ClaimRefusedError
import test_preclaim_retry_488b860e as helpers


class LaunchRetryTests(unittest.TestCase):
    setUp = helpers.PreclaimRetry.setUp
    _settings = helpers.PreclaimRetry._settings
    _offer = helpers.PreclaimRetry._offer
    _runner = helpers.PreclaimRetry._runner
    _admission = helpers.PreclaimRetry._admission

    def test_empty_command_output_is_reported_as_empty_not_withheld(self):
        from agent_hub_host.launch_diagnostics import LocalCommandError
        for raw in ('', '\n', b'\n'):
            error = LocalCommandError(raw, command='systemd-run', exit_code=1,
                                      stdout=b'', stderr=b'')
            self.assertEqual(error.diagnostic['reason'], 'launcher_command_failed')
            self.assertIn('without stdout or stderr', error.diagnostic['message'])
            self.assertNotIn('withheld', error.diagnostic['message'])
        unknown = LocalCommandError('unrecognized private text', command='systemd-run',
                                    exit_code=1, stdout=None)
        self.assertNotIn('without stdout or stderr', unknown.diagnostic['message'])
        self.assertNotIn('private text', unknown.diagnostic['message'])

    def test_claim_key_is_durable_before_request_and_survives_controller_death(self):
        launch = helpers._valid_uuid4()
        key = 'a' * 32
        path = Path(self.tmp.name) / 'fleet-launches' / (launch + '.json')
        def request(*args):
            self.assertEqual(json.loads(path.read_text())['claim_key'], key)
            raise SystemExit('controller died after the remote claim committed')
        dispatcher = Dispatcher(self._settings(), request, journal_dir=self.tmp.name)
        with self.assertRaises(SystemExit):
            dispatcher._claim_launch('/api/fleet/launches/' + launch + '/claim',
                                     {'claim_key': key}, launch, self.workspace)
        restarted = Dispatcher(self._settings(), journal_dir=self.tmp.name)
        self.assertEqual(restarted._pending_claim_key(launch), key)

    def test_definitive_first_claim_refusal_clears_only_pending_state(self):
        launch = helpers._valid_uuid4()
        def request(*args):
            raise HttpRequestError(403, '{"error":"quota_unavailable"}')
        dispatcher = Dispatcher(self._settings(), request, journal_dir=self.tmp.name)
        with self.assertRaises(ClaimRefusedError):
            dispatcher._claim_launch('/api/fleet/launches/' + launch + '/claim',
                                     {'claim_key': 'b' * 32}, launch, self.workspace)
        self.assertIsNone(dispatcher._pending_claim_key(launch))

    def test_worktree_refusal_happens_before_claim_or_native_effect(self):
        hub = helpers.FakeHub()
        launch = helpers._valid_uuid4()
        hub.offer(launch)
        calls = []
        dispatcher = Dispatcher(self._settings(), hub.request, self._runner(calls, launch),
                                journal_dir=self.tmp.name)
        with patch.object(dispatcher, '_lock_session_worktree',
                          side_effect=ValueError('worktree_lock_failed: checkout could not be protected')):
            results = []
            dispatcher._dispatch_one(self._offer(launch), results)
        self.assertFalse(hub.claim_calls)
        self.assertFalse(any('submit' in args for args in calls))
        self.assertFalse(any('run' in args for args in calls))
        self.assertEqual(results[0]['reason'], 'dispatch_worktree_refused')

    def test_successful_first_claim_is_fenced_before_native_start(self):
        hub = helpers.FakeHub()
        launch = helpers._valid_uuid4()
        hub.offer(launch)
        calls = []
        normal = self._runner(calls, launch)
        def runner(argv, **kwargs):
            if 'run' in argv:
                record = json.loads((Path(self.tmp.name) / 'fleet-launches' / (launch + '.json')).read_text())
                self.assertEqual(record['state'], 'uncertain')
                self.assertEqual(record['reason'], 'native_start_pending')
                self.assertEqual(record['claim_key'], hub.keys[launch])
                self.assertFalse({'native_id', 'tag', 'engine', 'incarnation'} & record.keys())
            return normal(argv, **kwargs)
        dispatcher = Dispatcher(self._settings(), hub.request, runner, journal_dir=self.tmp.name)
        result = dispatcher.dispatch(self._offer(launch))
        self.assertEqual(result['state'], 'running')
        self.assertEqual(sum('run' in args for args in calls), 1)

    def test_worktree_owner_fact_requires_exact_terminal_session_and_intake(self):
        from agent_hub_host import worktree_guard
        owner = helpers._valid_uuid4()
        current = helpers._valid_uuid4()
        intake = helpers._valid_uuid4()
        good = {'session_id': owner, 'intake_id': intake, 'terminal': True}
        for fact, allowed in ((good, True), ({**good, 'terminal': False}, False),
                              ({**good, 'terminal': 'true'}, False),
                              ({**good, 'session_id': current}, False),
                              ({**good, 'intake_id': 'other'}, False), (None, False)):
            with self.subTest(fact=fact):
                request = Mock(return_value=fact)
                dispatcher = Dispatcher(self._settings(), request, journal_dir=self.tmp.name)
                def protect(*args, owner_terminal):
                    return owner_terminal(owner)
                with patch.object(worktree_guard, 'bound_worktree_root', return_value=None), \
                     patch.object(worktree_guard, 'protect_bound_worktree', side_effect=protect):
                    offer = {'id': current, 'hub_session_id': current, 'intake_task_id': intake}
                    if allowed:
                        self.assertTrue(dispatcher._lock_session_worktree(offer, self.workspace))
                    else:
                        with self.assertRaisesRegex(ValueError, 'worktree_lock_failed'):
                            dispatcher._lock_session_worktree(offer, self.workspace)
                request.assert_called_once_with('GET', '/api/fleet/worktree-owner/' + owner)

from agent_hub_host import worktree_guard as guard
from agent_hub_host.launch_diagnostics import exception_diagnostic
from agent_hub_host.catalog_transport import report_catalog


class LaunchWorktreeRetry(unittest.TestCase):
    def test_retry_preserves_original_protection_and_release_authority(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, tree = Path(tmp) / 'repo', Path(tmp) / 'tree'
            def git(*args):
                return subprocess.run(['git', *args], capture_output=True, text=True, check=True)
            git('init', str(repo))
            git('-C', str(repo), '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                'commit', '--allow-empty', '-m', 'initial')
            git('-C', str(repo), 'worktree', 'add', '--detach', str(tree))
            first, retry = 'a' * 32, 'b' * 32
            original = guard.lock_reason(first, 'intake-1')
            self.assertTrue(guard.ensure_locked('git', str(tree), first, 'intake-1'))
            self.assertFalse(guard.ensure_locked('git', str(tree), retry, 'intake-1'))
            self.assertFalse(guard.ensure_locked('git', str(tree), retry, 'intake-1', owner_terminal=lambda owner: False))
            self.assertTrue(guard.ensure_locked('git', str(tree), retry, 'intake-1', owner_terminal=lambda owner: owner == first))
            self.assertEqual(guard.find_entry(guard.list_entries('git', str(tree)), str(tree))['reason'], original)
            self.assertFalse(guard.ensure_locked('git', str(tree), retry, 'intake-2'))
            self.assertFalse(guard.ensure_locked('git', str(tree), retry, None))
            self.assertFalse(guard.release_session_lock('git', str(tree), retry,
                                                       terminal=True, intake_state='done'))
            self.assertTrue(guard.is_locked('git', str(tree)))
            self.assertTrue(guard.release_session_lock('git', str(tree), first,
                                                      terminal=True, intake_state='done'))
            self.assertTrue(guard.lock_worktree('git', str(tree), 'manual preservation'))
            self.assertFalse(guard.ensure_locked('git', str(tree), retry, 'intake-1'))

    def test_worktree_refusals_are_fixed_nonwithheld_diagnostics(self):
        for raw in ('worktree_lock_failed: missing offer identity',
                    'worktree_lock_failed: missing canonical session identity',
                    'worktree_lock_failed: checkout could not be protected',
                    'worktree_identity_failed: original repository could not be recorded'):
            with self.subTest(raw=raw):
                value = exception_diagnostic(ValueError(raw), 'dispatch_interactive')
                self.assertIn(value['reason'], ('launcher_worktree_lock_failed', 'launcher_worktree_identity_failed'))
                self.assertNotIn('withheld', value['message'])
                unsafe = exception_diagnostic(ValueError(raw + ' SECRET'), 'dispatch_interactive')
                self.assertNotIn('SECRET', str(unsafe))


class CatalogTransportTests(unittest.TestCase):
    def test_old_schema_retry_preserves_rows_and_does_not_mutate_cache(self):
        rows = [{'native_id': 'native-1', 'confirmed_exit': False, 'phase': 'running',
                 'progress': {'last_progress_at': '2026-10-29T05:00:00Z',
                              'sampled_at': '2026-10-29T05:01:00Z', 'goal_status': 'active'}},
                {'native_id': 'native-2', 'confirmed_exit': True, 'phase': 'exited'}]
        original = copy.deepcopy(rows)
        request = Mock(side_effect=[HttpRequestError(400, '{"error":"invalid_session"}'), {'ok': True}])
        self.assertEqual(report_catalog(request, rows), {'ok': True})
        sent = request.call_args_list[1].args[2]['sessions']
        expected = copy.deepcopy(rows)
        expected[0]['progress'].pop('sampled_at')
        expected[0]['progress'].pop('goal_status')
        self.assertEqual(sent, expected)
        self.assertEqual(rows, original)

    def test_new_server_receives_full_progress_once(self):
        rows = [{'native_id': 'native-1', 'progress': {'goal_status': 'paused'}}]
        request = Mock(return_value={'ok': True})
        self.assertEqual(report_catalog(request, rows), {'ok': True})
        request.assert_called_once_with('PUT', '/api/fleet/sessions', {'sessions': rows})

    def test_only_specific_schema_refusal_retries_and_only_once(self):
        rows = [{'progress': {'sampled_at': 'observed'}}]
        for error in (HttpRequestError(403, '{"error":"invalid_session"}'),
                      HttpRequestError(500, '{"error":"invalid_session"}'),
                      HttpRequestError(400, '{"error":"invalid_sessions"}'), TimeoutError()):
            with self.subTest(error=error):
                request = Mock(side_effect=error)
                with self.assertRaises(type(error)):
                    report_catalog(request, rows)
                self.assertEqual(request.call_count, 1)
        request = Mock(side_effect=HttpRequestError(400, '{"error":"invalid_session"}'))
        with self.assertRaises(HttpRequestError):
            report_catalog(request, [{'native_id': 'bad'}])
        self.assertEqual(request.call_count, 1)
        request.reset_mock()
        with self.assertRaises(HttpRequestError):
            report_catalog(request, rows)
        self.assertEqual(request.call_count, 2)
