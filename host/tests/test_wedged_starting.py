"""Lost Hub claims and completed receipts recover without native side effects."""
import copy
import datetime as dt
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from agent_hub_host.fleet import Dispatcher
from agent_hub_host.account_capacity import reconcile_unstarted
from agent_hub_host.wedged_launches import reconcile_claimed_launch


class WedgedStartingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ws, self.cfg = self.root / 'repo', self.root / 'launcher'
        self.ws.mkdir(); self.cfg.mkdir(); (self.ws / '.local').mkdir()
        self.offer = '11111111-1111-4111-8111-111111111111'
        self.task = 'hub-' + self.offer
        self.native = '22222222-2222-4222-8222-222222222222'
        self.created = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=10)).isoformat()
        self.row = {'id': self.offer, 'workspace': 'repo', 'mode': 'headless', 'engine': 'opencode', 'native_id': None}
        self.status = {'state': 'queued',
                       'payload': {'owner': 'hub-fleet', 'cwd': str(self.ws)}}
        self.settings = {'journal_dir': str(self.root / 'journal'), 'workspaces': {'repo': str(self.ws)}, 'subscriptions': [],
                         'launcher': {'argv': ['launcher'], 'root': str(self.cfg), 'config_dir': str(self.cfg)}}
        self.d = Dispatcher(self.settings, request=Mock(), runner=Mock(return_value=self.status))
        self.d.request.side_effect = lambda method, path, data: {'id': self.offer, 'state': data['state']}
        with sqlite3.connect(self.cfg / 'state.db') as c:
            c.executescript('CREATE TABLE tasks (id TEXT PRIMARY KEY,state TEXT,payload TEXT,idempotency_key TEXT,reason TEXT,reviewer TEXT,updated_at TEXT,created_at TEXT); CREATE TABLE task_native_bindings (task_id TEXT);')
            c.execute('INSERT INTO tasks VALUES(?,?,?,?,NULL,NULL,NULL,?)', (self.task, 'queued', json.dumps(self.status['payload']), self.task, self.created))

    def state(self):
        with sqlite3.connect(self.cfg / 'state.db') as c:
            return c.execute('SELECT state FROM tasks').fetchone()[0]

    def completed(self):
        tomb = {'finished_at_ms': 1793016447393,
                'exit': {'code': 0, 'signal': None, 'exited_at_ms': 1793016447393}}
        retirement = patch('agent_hub_host.retired_native.verify_native_retirement', return_value=(True, tomb, 'verified'))
        retirement.start(); self.addCleanup(retirement.stop)
        terminal = patch('agent_hub_host.fleet.verify_native_retirement', return_value=(True, tomb, 'verified'))
        terminal.start(); self.addCleanup(terminal.stop)
        self.status.update(state='completed-awaiting-review', native_binding={
            'task_id': self.task, 'native_id': self.native, 'tag': 'task-' + self.task,
            'workspace': str(self.ws), 'engine': 'opencode', 'incarnation': 1793016446093})
        self.receipt = {'task_id': self.task, 'native_id': self.native, 'workspace': str(self.ws),
                        'tag': 'task-' + self.task, 'resources_reserved': False,
                        'native_status': 'retired-finished', 'state': 'completed-awaiting-review'}
        self.d.runner.side_effect = lambda argv, **kw: self.receipt if 'reconcile' in argv else self.status
        with sqlite3.connect(self.cfg / 'state.db') as c:
            c.execute('UPDATE tasks SET state=?', ('completed-awaiting-review',))
            c.execute('INSERT INTO task_native_bindings VALUES(?)', (self.task,))
        results = self.ws / '.local' / 'hub-results'; results.mkdir()
        (results / (self.offer + '.json')).write_text(json.dumps({'status': 'done', 'report': 'Finished.', 'artifacts': []}))

    def test_missing_journal_and_diagnostic_fences_queued_task_and_reports_failure(self):
        out = reconcile_unstarted(self.d, rows=[self.row])
        self.assertEqual(out, [{'id': self.offer, 'state': 'failed', 'reconciled': True}])
        self.assertEqual(self.state(), 'failed')
        self.assertEqual(self.d.request.call_args.args[2]['reason'], 'launcher_not_started')
        # Replay after a lost HTTP acknowledgement revalidates the same fence.
        self.assertEqual(reconcile_claimed_launch(self.d, self.row)['state'], 'failed')
        self.assertTrue(all('run' not in c.args[0] and 'stop' not in c.args[0] for c in self.d.runner.call_args_list))

    def test_recent_future_and_unknown_timestamp_do_not_fence(self):
        for value in [dt.datetime.now(dt.timezone.utc).isoformat(), '2999-01-01T00:00:00Z', 'invalid']:
            with sqlite3.connect(self.cfg / 'state.db') as c:
                c.execute('UPDATE tasks SET created_at=?', (value,))
            self.assertIsNone(reconcile_claimed_launch(self.d, self.row))
            self.assertEqual(self.state(), 'queued')
        self.d.request.assert_not_called()

    def test_ledger_identity_payload_and_path_must_match_real_status_contract(self):
        for column, value in [('idempotency_key', 'foreign'), ('payload', '{}'), ('payload', 'invalid')]:
            with self.subTest(column=column, value=value):
                with sqlite3.connect(self.cfg / 'state.db') as c:
                    c.execute('UPDATE tasks SET idempotency_key=?,payload=?', (self.task, json.dumps(self.status['payload'])))
                    c.execute(f'UPDATE tasks SET {column}=?', (value,))
                self.assertIsNone(reconcile_claimed_launch(self.d, self.row))
                self.assertEqual(self.state(), 'queued')
        self.d.request.assert_not_called()

    def test_winning_launcher_and_contended_fence_retain_capacity(self):
        import fcntl
        with (self.cfg / 'launch.lock').open('a') as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertIsNone(reconcile_claimed_launch(self.d, self.row))
        with sqlite3.connect(self.cfg / 'state.db') as c:
            c.execute("UPDATE tasks SET state='starting'")
        self.assertIsNone(reconcile_claimed_launch(self.d, self.row))
        self.assertEqual(self.state(), 'starting')
        self.d.request.assert_not_called()

    def test_completed_result_without_host_journal_reports_finish(self):
        self.completed()
        self.assertEqual(reconcile_claimed_launch(self.d, self.row)['state'], 'exited')
        result = self.d.request.call_args.args[2]
        self.assertEqual((result['native_id'], result['report_status'], result['report']), (self.native, 'done', 'Finished.'))
        self.assertTrue(result['confirmed_exit'])
        self.assertEqual(result['runtime']['exit_code'], 0)
        self.assertIsNone(result['runtime']['exit_signal'])
        self.assertEqual(result['runtime']['wall_time_ms'], 1300)
        self.assertEqual(result['runtime']['outcome'], 'reported')
        self.assertTrue((self.ws / '.local/hub-results' / (self.offer + '.json')).exists())

    def test_lost_receipt_with_retained_terminal_record_uses_full_runtime_path(self):
        self.completed()
        self.receipt['native_status']='dead'
        history=self.ws/'native/sessions'/self.native/'history.bin'
        history.parent.mkdir(parents=True);history.write_text('Kept final output')
        row=dict(id=self.native,tag='task-'+self.task,workspace=str(self.ws),engine='opencode',
                 created_at_ms=1793016446093,phase='exited',worker_alive=False,
                 containment_empty=True,processes=0,worker_pid=99999999,workload_pid=99999998,
                 history_path=str(history),exit=dict(code=0,signal=None,exited_at_ms=1793016447393))
        self.d.runner.side_effect=lambda argv,**kwargs:[row] if 'list' in argv else self.receipt if 'reconcile' in argv else self.status
        with patch('agent_hub_host.retired_native.verify_native_retirement',return_value=(False,None,'retained')),             patch('agent_hub_host.fleet.verify_native_retirement',return_value=(False,None,'retained')):
            out=reconcile_claimed_launch(self.d,self.row)
        self.assertEqual(out['state'],'exited')
        payload=self.d.request.call_args.args[2]
        self.assertEqual(payload['report_status'],'done')
        self.assertEqual(payload['runtime']['exit_code'],0)
        self.assertEqual(payload['runtime']['wall_time_ms'],1300)
        self.assertEqual(payload['runtime']['log_tail'],'Kept final output')
        self.assertTrue(history.is_file())

    def test_nested_cwd_lost_receipt_uses_root_result_and_durable_full_replay(self):
        self.completed()
        nested = self.ws / 'nested'; nested.mkdir()
        self.row['cwd'] = 'nested'
        self.status['payload']['cwd'] = str(nested)
        with sqlite3.connect(self.cfg / 'state.db') as c:
            c.execute('UPDATE tasks SET payload=?', (json.dumps(self.status['payload']),))
        # A tempting nested result belongs to no such launch. Only the
        # workspace-root result is the launcher's authorized report path.
        wrong = nested / '.local/hub-results'; wrong.mkdir(parents=True)
        (wrong / (self.offer + '.json')).write_text(json.dumps({'status': 'failed', 'report': 'Wrong nested result'}))
        posts = []
        def request(method, path, payload):
            posts.append(copy.deepcopy(payload))
            return {'id': self.offer, 'state': 'failed' if len(posts)==1 else payload['state']}
        self.d.request = request
        self.assertIsNone(reconcile_claimed_launch(self.d, self.row))
        self.assertFalse(self.d._active_launches[self.offer]['settled'])
        self.d = Dispatcher(self.settings, request=request, runner=self.d.runner)
        result = reconcile_claimed_launch(self.d, self.row)
        self.assertEqual(result['state'], 'exited')
        self.assertEqual(posts[0], posts[1])
        self.assertEqual(posts[1]['report'], 'Finished.')
        self.assertEqual(posts[1]['runtime']['wall_time_ms'], 1300)
        self.assertTrue(self.d._active_launches[self.offer]['settled'])
        self.assertEqual(self.d._active_launches[self.offer]['target_cwd'], str(nested))
        self.assertEqual(self.d._active_launches[self.offer]['result_path'], str(self.ws / '.local/hub-results' / (self.offer + '.json')))
        self.assertEqual(self.d.reconcile_launches(), [])

    def test_positive_bound_exit_reports_before_the_no_start_age_timeout(self):
        self.completed()
        with sqlite3.connect(self.cfg / 'state.db') as c:
            c.execute('UPDATE tasks SET created_at=?', (dt.datetime.now(dt.timezone.utc).isoformat(),))
        result = reconcile_claimed_launch(self.d, self.row)
        self.assertEqual(result['state'], 'exited')
        self.assertEqual(self.d.request.call_args.args[2]['runtime']['wall_time_ms'], 1300)

    def test_launcher_dead_receipt_cannot_replace_independent_native_retirement(self):
        self.completed()
        with patch('agent_hub_host.retired_native.verify_native_retirement', return_value=(False, None, 'unknown')):
            self.assertIsNone(reconcile_claimed_launch(self.d, self.row))
        self.d.request.assert_not_called()
        self.assertNotIn(self.offer, self.d._active_launches)

    def test_live_unknown_wrong_native_and_reserved_receipts_cannot_finish(self):
        self.completed()
        for key, val in [('native_status', 'alive'), ('native_status', 'unknown'), ('native_id', self.offer),
                         ('resources_reserved', True), ('workspace', '/foreign'), ('tag', 'foreign')]:
            original = copy.deepcopy(self.receipt)
            self.receipt[key] = val
            self.assertIsNone(reconcile_claimed_launch(self.d, self.row), key)
            self.receipt = original
        self.d.request.assert_not_called()

    def test_foreign_or_malformed_launcher_binding_is_refused(self):
        self.completed()
        for key, val in [('workspace', '/foreign'), ('engine', 'codex'), ('tag', 'foreign'), ('native_id', 'invalid')]:
            original = copy.deepcopy(self.status['native_binding'])
            self.status['native_binding'][key] = val
            self.assertIsNone(reconcile_claimed_launch(self.d, self.row), key)
            self.status['native_binding'] = original
        self.d.request.assert_not_called()

    def test_missing_or_invalid_result_is_failed_and_report_cannot_prove_exit(self):
        self.completed()
        (self.ws / '.local/hub-results' / (self.offer + '.json')).write_text('{}')
        self.assertEqual(reconcile_claimed_launch(self.d, self.row)['state'], 'failed')
        self.assertEqual(self.d.request.call_args.args[2]['report_status'], 'failed')
        self.assertIn('failed-with-partial-work', self.d.request.call_args.args[2]['report'])
        self.assertEqual(self.d.request.call_args.args[2]['runtime']['exit_code'], 0)
        self.assertEqual(self.d.request.call_args.args[2]['runtime']['outcome'], 'failed-without-report')

    def test_dry_run_and_lost_acknowledgement_are_safe(self):
        self.assertEqual(reconcile_claimed_launch(self.d, self.row, dry_run=True)['state'], 'probe_required')
        self.assertEqual(self.state(), 'queued')
        self.d.request.assert_not_called()
        self.d.request.side_effect = RuntimeError('lost ack')
        self.assertIsNone(reconcile_claimed_launch(self.d, self.row))
        self.assertEqual(self.state(), 'failed')
        self.d.request.side_effect = lambda method, path, data: {'id': self.offer, 'state': data['state']}
        self.assertEqual(reconcile_claimed_launch(self.d, self.row)['state'], 'failed')

    def test_configured_worktree_cwd_uses_exact_launcher_path(self):
        other = self.root / 'worktree'; other.mkdir()
        self.settings['workspaces']['worktree'] = str(other)
        self.row['cwd'] = str(other)
        self.assertIsNone(reconcile_claimed_launch(self.d, self.row))
        self.assertEqual(self.state(), 'queued')  # payload names the original repo
        self.status['payload']['cwd'] = str(other)
        with sqlite3.connect(self.cfg / 'state.db') as c:
            c.execute('UPDATE tasks SET payload=?', (json.dumps(self.status['payload']),))
        self.assertEqual(reconcile_claimed_launch(self.d, self.row)['state'], 'failed')

    def test_malformed_status_payload_and_binding_fail_closed(self):
        for status in [[], {'payload': []}, {**self.status, 'payload': ['foreign']},
                       {**self.status, 'native_binding': ['invalid']}]:
            with self.subTest(status=status):
                self.d.runner.return_value = status
                with patch('agent_hub_host.launcher_recovery.settle_unstarted', return_value=False):
                    self.assertIsNone(reconcile_claimed_launch(self.d, self.row))
        self.d.request.assert_not_called()
