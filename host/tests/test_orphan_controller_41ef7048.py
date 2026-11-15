"""Intake 41ef7048: an orphaned controller held host.lock for 11 days.

DESKTOP-FP4OP26: the supervisor restarted (11/3 13:35) and lost the receipt of
the session_host its predecessor started (11/3 11:49). The orphan kept
host.lock; the supervisor logged controller_retry 16,666 times and never
replaced it or alerted anyone.
"""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

SCRIPT = Path(__file__).resolve().parents[2] / 'scripts/session-host-supervisor.py'


def load_supervisor():
    spec = importlib.util.spec_from_file_location('supervisor_41ef7048', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def events(text):
    return [json.loads(line) for line in text.splitlines() if line.startswith('{')]


def controller_row(journal, **extra):
    row = dict(id='orphan-1', tag='hub-controller-abc123def456', engine='shell', created_at_ms=5,
               worker_pid=58276, workload_pid=61452, phase='running', worker_alive=True,
               command=['python.exe', '-u', 'bootstrap_log.py', '--log', 'x.log', '--', '-m',
                        'agent_hub_host.session_host', '--config', 'subs.json', '--journal-dir', str(journal)])
    row.update(extra)
    return row


class OrphanControllerTests(unittest.TestCase):
    def setUp(self):
        self.s = load_supervisor()
        self.journal = Path('/tmp/journal-41ef7048').resolve()

    def test_only_this_journals_hub_controller_qualifies(self):
        s, j = self.s, self.journal
        self.assertTrue(s.is_journal_controller(controller_row(j), j))
        self.assertFalse(s.is_journal_controller(controller_row(j, tag='task-codex'), j))
        self.assertFalse(s.is_journal_controller(controller_row(j, engine='codex'), j))
        self.assertFalse(s.is_journal_controller(controller_row(j / 'other'), j))
        self.assertFalse(s.is_journal_controller(controller_row(j, command=['codex', '--journal-dir', str(j)]), j))
        self.assertFalse(s.is_journal_controller(controller_row(j, command='not-a-list'), j))

    def test_orphan_holder_is_stopped_and_new_controller_may_start(self):
        s, j = self.s, self.journal
        worker = dict(id='codex-1', tag='task-x', engine='codex', command=['codex'])
        calls = []

        def native(*args, **_kw):
            calls.append(args)
            if args[0] == 'list':
                return [controller_row(j), worker]
            if args[0] == 'kill':
                return {'id': args[1], 'record_removed': True}
            raise AssertionError(args)
        held = iter([True, False])
        out = io.StringIO()
        with patch.object(s, 'native', side_effect=native), \
             patch.object(s, 'journal_lock_held', side_effect=lambda _j: next(held)), \
             contextlib.redirect_stdout(out):
            self.assertTrue(s.replace_orphan_controllers(j, sleep=lambda _x: None))
        self.assertEqual([c for c in calls if c[0] == 'kill'],
                         [('kill', 'orphan-1', '--signal', 'TERM', '--grace-ms', '30000')])
        replaced = next(e for e in events(out.getvalue()) if e['event'] == 'controller_orphan_replaced')
        self.assertEqual((replaced['id'], replaced['record_removed']), ('orphan-1', True))

    def test_wedged_orphan_is_force_killed_by_its_own_pids_and_forgotten(self):
        s, j = self.s, self.journal
        calls, killed = [], []

        def native(*args, **_kw):
            calls.append(args)
            if args[0] == 'list':
                return [controller_row(j)]
            raise RuntimeError('Native controller operation failed')  # wedged worker
        out = io.StringIO()
        with patch.object(s, 'native', side_effect=native), \
             patch.object(s, 'kill_pid', side_effect=lambda pid: killed.append(pid) or True), \
             patch.object(s, 'journal_lock_held', return_value=False), \
             contextlib.redirect_stdout(out):
            self.assertTrue(s.replace_orphan_controllers(j, sleep=lambda _x: None))
        self.assertEqual(killed, [58276, 61452])
        self.assertIn(('forget', 'orphan-1', '--force'), calls)

    def test_unknown_holder_still_refuses_a_second_controller(self):
        s, j = self.s, self.journal
        calls = []

        def native(*args, **_kw):
            calls.append(args)
            return []
        with patch.object(s, 'native', side_effect=native), \
             patch.object(s, 'journal_lock_held', return_value=True), \
             contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, 'lock held'):
                s.run_controller(Mock(control_dir=None), j, ['x'], tag_prefix='p-', env_args=[],
                                 journal=j, stop=Mock())
        self.assertFalse([c for c in calls if c[0] in ('start', 'kill')])

    def test_run_controller_starts_after_orphan_replacement(self):
        s, j = self.s, self.journal
        with patch.object(s, 'journal_lock_held', return_value=True), \
             patch.object(s, 'replace_orphan_controllers', return_value=True), \
             patch.object(s, 'start_child', side_effect=RuntimeError('started')) as start, \
             contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, 'started'):
                s.run_controller(Mock(control_dir=None), j, ['x'], tag_prefix='p-', env_args=[],
                                 journal=j, stop=Mock())
        start.assert_called_once()

    def test_repeated_retry_raises_a_loud_alert(self):
        s = self.s
        stop = Mock()
        n = s.RETRY_ALERT_AFTER + s.RETRY_ALERT_EVERY
        stop.is_set.side_effect = [False] * n + [True]
        stop.wait.return_value = False
        attempt = Mock(side_effect=RuntimeError('Journal lock held by another controller; not starting a second one'))
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            s.supervise_windows(attempt, stop, clock=lambda: 0.0)
        alerts = [e for e in events(out.getvalue()) if e['event'] == 'controller_retry_alert']
        self.assertEqual([a['attempt'] for a in alerts], [s.RETRY_ALERT_AFTER, s.RETRY_ALERT_AFTER + s.RETRY_ALERT_EVERY])
        self.assertEqual(alerts[0]['severity'], 'critical')
        self.assertIn('Journal lock held', alerts[0]['detail'])
        self.assertIn('controller_retry_alert', err.getvalue())


if __name__ == '__main__':
    unittest.main()
