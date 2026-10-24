import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_hub_host.session_progress import SessionProgress, event_ms, file_signature, process, leader_in_containment


class SessionProgressTests(unittest.TestCase):
    def setUp(self):
        self.clock = 10_000
        self.row = {'id': 'native', 'tag': 'task-hub-offer', 'workspace': '/repo', 'cwd': '/repo',
                    'engine': 'opencode', 'created_at_ms': 1000, 'workload_pid': 123,
                    'containment_empty': False, 'command': ['opencode', 'run', 'goal'],
                    'reported_state_event_ms': 2000, 'last_activity_ms': 3000}
        self.record = {'offer': {'offer_id': 'offer', 'task_id': 'hub-offer', 'mode': 'headless',
                                'native_id': 'native', 'tag': 'task-hub-offer', 'workspace': '/repo',
                                'engine': 'opencode', 'incarnation': 1000}}

    def sampler(self, **kwargs):
        return SessionProgress(now_ms=lambda: self.clock, files=lambda _: 'same', **kwargs)

    def test_output_and_state_use_newest_actual_event(self):
        self.assertEqual(event_ms(self.row), 3000)
        self.row['reported_state_event_ms'] = 4000
        self.assertEqual(event_ms(self.row), 4000)

    @patch('agent_hub_host.session_progress.tree_cpu', return_value={})
    @patch('agent_hub_host.session_progress.process', return_value={'pid': 123, 'start': 1, 'cpu': 0})
    def test_polling_is_not_progress_and_unknown_does_not_prove_death(self, proc, cpu):
        sampler = self.sampler()
        first = sampler.sample([self.row])['native']
        self.clock += 20000
        self.assertEqual(sampler.sample([self.row])['native']['last_progress_at'], first['last_progress_at'])
        proc.return_value = None
        self.assertIsNone(sampler.sample([self.row])['native']['agent_alive'])
        self.assertFalse(sampler.sample([self.row])['native']['orphaned'])

    @patch('agent_hub_host.session_progress.tree_cpu')
    @patch('agent_hub_host.session_progress.process', return_value={'pid': 123, 'start': 1, 'cpu': 0})
    def test_real_cpu_and_files_advance_activity_but_pid_reuse_does_not(self, proc, cpu):
        cpu.return_value = {(123, 1): 0}
        sampler = self.sampler()
        first = sampler.sample([self.row])['native']['last_progress_at']
        cpu.return_value = {(123, 1): 100}
        self.clock += 5000
        second = sampler.sample([self.row])['native']['last_progress_at']
        self.assertNotEqual(first, second)
        cpu.return_value = {(123, 2): 5000}
        self.clock += 5000
        self.assertEqual(second, sampler.sample([self.row])['native']['last_progress_at'])
        sampler.files = lambda _: 'changed'
        self.assertNotEqual(second, sampler.sample([self.row])['native']['last_progress_at'])

    @patch('agent_hub_host.session_progress.tree_cpu', return_value={})
    @patch('agent_hub_host.session_progress.process')
    def test_orphan_reap_requires_owned_incarnation_and_previously_live_agent(self, proc, cpu):
        calls = []
        sampler = self.sampler(stop=lambda row: calls.append(row['id']) or True)
        proc.return_value = {'pid': 123, 'start': 1, 'cpu': 0}
        sampler.sample([self.row], self.record)
        proc.return_value = False
        result = sampler.sample([self.row], self.record)['native']
        self.assertEqual(result['cleanup'], 'reaped')
        self.assertTrue(result['orphaned'])
        self.assertIs(result['agent_alive'], False)
        sampler.sample([self.row], self.record)
        self.assertEqual(calls, ['native'])
        # An unrelated/already orphaned session is reported but never reaped.
        self.row['id'] = 'unrelated'
        sampler.sample([self.row], self.record)
        self.assertEqual(calls, ['native'])
        self.row['id'] = 'native'
        self.row['created_at_ms'] = 2000
        sampler.sample([self.row], self.record)
        self.assertEqual(calls, ['native'])

    @patch('agent_hub_host.session_progress.tree_cpu', return_value={})
    @patch('agent_hub_host.session_progress.process')
    def test_failed_cleanup_stays_uncertain_and_is_not_blindly_retried(self, proc, cpu):
        calls = []
        sampler = self.sampler(stop=lambda row: calls.append(1) or False)
        proc.return_value = {'pid': 123, 'start': 1, 'cpu': 0}
        sampler.sample([self.row], self.record)
        proc.return_value = False
        self.assertEqual(sampler.sample([self.row], self.record)['native']['cleanup'], 'uncertain')
        sampler.sample([self.row], self.record)
        self.assertEqual(calls, [1])

    def test_bounded_metadata_and_zombie_detection(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            p = root / '123'
            p.mkdir()
            # stat fields after comm begin with state; starttime index19.
            fields = ['Z'] + ['0'] * 30
            (p / 'stat').write_text('123 (process name) ' + ' '.join(fields))
            self.assertIs(process(123, root), False)
            self.assertIs(process(456, root), False)
            (root / 'file').write_text('one')
            signature = file_signature(d)
            (root / 'file').write_text('two with changed size')
            self.assertNotEqual(file_signature(d), signature)

    def test_reused_pid_in_foreign_containment_is_not_alive_or_progress(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            p = root / '123'
            p.mkdir()
            leader = {'pid': 123, 'start': 4, 'cpu': 100000}
            row = dict(self.row, workload_cgroup='/user.slice/native.scope')
            (p / 'cgroup').write_text('0::/user.slice/unrelated.scope\n')
            self.assertIsNone(leader_in_containment(row, leader, root))
            (p / 'cgroup').write_text('0::/user.slice/native.scope\n')
            self.assertEqual(leader_in_containment(row, leader, root), leader)
            (p / 'cgroup').unlink()
            self.assertIsNone(leader_in_containment(row, leader, root))

    @patch('agent_hub_host.session_progress.tree_cpu', return_value={})
    @patch('agent_hub_host.session_progress.process')
    def test_monitor_restart_keeps_cleanup_dedup_and_failure_evidence(self, proc, cpu):
        with tempfile.TemporaryDirectory() as d:
            state = Path(d) / 'private' / 'progress.json'
            calls = []
            sampler = self.sampler(stop=lambda row: calls.append(1) or True, state_path=state)
            proc.return_value = {'pid': 123, 'start': 1, 'cpu': 0}
            sampler.sample([self.row], self.record)
            proc.return_value = False
            sampler = self.sampler(stop=lambda row: calls.append(1) or True, state_path=state)
            self.assertEqual(sampler.sample([self.row], self.record)['native']['cleanup'], 'reaped')
            sampler = self.sampler(stop=lambda row: calls.append(1) or True, state_path=state)
            self.row.update(phase='exited', containment_empty=True)
            result = sampler.sample([self.row], self.record)['native']
            self.assertEqual(result['cleanup'], 'reaped')
            self.assertTrue(result['orphaned'])
            self.assertEqual(calls, [1])
            self.assertEqual(state.stat().st_mode & 0o777, 0o600)


if __name__ == '__main__':
    unittest.main()
