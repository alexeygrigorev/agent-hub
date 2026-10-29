import tempfile
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from agent_hub_host import session_reaper as reaper
from agent_hub_host.native_sessions import SessionUncertain


class SessionReaperTests(unittest.TestCase):
    def row(self, workspace):
        return dict(id='ed111111-1111-4111-8111-111111111111', tag='task-hub-test',
                    workspace=workspace, created_at_ms=1000, engine='codex')

    def fact(self, row, **kw):
        return dict(reaper.identity(row), managed=True, mode='headless', protected=False, state='finished', **kw)

    def test_terminal_and_missing_exact_managed_incarnations_are_stopped_once(self):
        for status in ('finished', 'missing', 'cancelled', 'failed'):
            with self.subTest(state=status), tempfile.TemporaryDirectory() as journal:
                row = self.row(journal)
                fact = self.fact(row)
                fact['state'] = status
                request = Mock(return_value={'session': fact})
                with patch.object(reaper, 'stop_session', return_value=SimpleNamespace(state='stopped')) as stop:
                    self.assertEqual(reaper.reconcile(request, [row], journal)['reaped'], 1)
                    self.assertEqual(reaper.reconcile(request, [row], journal)['reaped'], 0)
                self.assertEqual(stop.call_count, 1)
                self.assertEqual(stop.call_args.args[1].created_at_ms, 1000)

    def test_protected_interactive_active_unknown_and_mismatched_never_stopped(self):
        changes = [dict(protected=True), dict(mode='interactive'), dict(state='running'),
                   dict(managed=False), dict(native_created_at_ms=2000), dict(tag='replacement'),
                   dict(protected=None), dict(workspace='/another')]
        for change in changes:
            with self.subTest(change=change), tempfile.TemporaryDirectory() as journal:
                row = self.row(journal)
                fact = self.fact(row)
                fact.update(change)
                with patch.object(reaper, 'stop_session') as stop:
                    reaper.reconcile(Mock(return_value={'session': fact}), [row], journal)
                    stop.assert_not_called()

    def test_server_error_or_failed_journal_never_authorizes_effect(self):
        with tempfile.TemporaryDirectory() as journal:
            row = self.row(journal)
            with patch.object(reaper, 'stop_session') as stop:
                reaper.reconcile(Mock(side_effect=RuntimeError('private response')), [row], journal)
                with patch.object(reaper.private_fs, 'atomic_write', side_effect=OSError()):
                    reaper.reconcile(Mock(return_value={'session': self.fact(row)}), [row], journal)
                stop.assert_not_called()

    def test_bounded_scan_rotates_past_non_candidates(self):
        with tempfile.TemporaryDirectory() as journal:
            rows = [dict(self.row(journal), id='ed111111-1111-4111-8111-' + str(i).zfill(12)) for i in range(10)]
            request = Mock(return_value={'session': None})
            with patch.object(reaper, 'stop_session') as stop:
                for _ in range(3):
                    reaper.reconcile(request, rows, journal, max_checks=4)
                self.assertEqual(request.call_count, 12)
                ids = [call.args[2]['native']['native_id'] for call in request.call_args_list]
                self.assertEqual(len(set(ids)), 10)
                stop.assert_not_called()

    def test_rotating_scan_reaches_natives_beyond_the_first_500(self):
        with tempfile.TemporaryDirectory() as journal:
            rows = [dict(self.row(journal), id='ed111111-1111-4111-8111-' + str(i).zfill(12)) for i in range(501)]
            reaper.private_fs.atomic_write(reaper.Path(journal) / 'session-reaper.json', json.dumps({'_cursor':500}).encode(), 512*1024)
            request = Mock(return_value={'session':None})
            reaper.reconcile(request, rows, journal, max_checks=1)
            self.assertEqual(request.call_args.args[2]['native']['native_id'], rows[500]['id'])

    def test_uncertain_stop_reconciles_same_oracle_without_rekill(self):
        with tempfile.TemporaryDirectory() as journal:
            row = self.row(journal)
            request = Mock(return_value={'session': self.fact(row)})
            error = SessionUncertain('unknown')
            error.oracle = {'qualified': 'test'}
            error.kill = {'signal': 'TERM'}
            with patch.object(reaper, 'stop_session', side_effect=error) as stop:
                self.assertEqual(reaper.reconcile(request, [row], journal)['pending'], 1)
                with patch.object(reaper, 'reconcile_stop_oracle', return_value=(False, 'unknown')):
                    self.assertEqual(reaper.reconcile(request, [row], journal)['pending'], 1)
                with patch.object(reaper, 'reconcile_stop_oracle', return_value=(True, 'empty')) as reconcile:
                    self.assertEqual(reaper.reconcile(request, [row], journal)['reaped'], 1)
                    self.assertEqual(reconcile.call_args.args[1], error.oracle)
                self.assertEqual(stop.call_count, 1)
