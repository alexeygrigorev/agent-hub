"""Tests for no casual direct engine picker, waiting-idle digests, and headless auto-cleanup."""
import argparse
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, MagicMock

from agent_hub_host import session_control
from agent_hub_host import status
from agent_hub_host.fleet import Dispatcher


def parser():
    root = argparse.ArgumentParser()
    sub = root.add_subparsers(dest='cmd', required=True)
    session_control.add_commands(sub)
    return root


class TestNoCasualEngine(unittest.TestCase):
    def parse(self, *args):
        return parser().parse_args(['sessions', *args])


    def test_cli_sessions_override_requires_engine_and_allows_optional_reason(self):
        args = self.parse('override', '--workspace', 'demo', '--engine', 'flash')
        self.assertEqual(args.workspace, 'demo')
        self.assertEqual(args.engine, 'flash')
        self.assertIsNone(args.reason)
        explained = self.parse('override', '--workspace', 'demo', '--engine', 'flash', '--reason', 'Specific model verification')
        self.assertEqual(explained.reason, 'Specific model verification')
        with self.assertRaises(SystemExit):
            self.parse('override', '--workspace', 'demo', '--reason', 'Missing engine')

    def test_cli_sessions_start_forwards_overrides_without_explanation_gate(self):
        for extra in ((), ('--tier', 'quick'), ('--tier', 'deep'), ('--engine', 'flash'),
                      ('--machine', 'hetzner-aplexer'), ('--model', 'gpt-6.1-sol', '--effort', 'medium')):
            with self.subTest(extra=extra), patch.object(session_control, 'request', return_value={'id': 's1'}) as transport:
                session_control.execute(self.parse('start', '--no-wait', '--workspace', 'demo', '--prompt', 'start', '--key', 'k1', *extra))
            payload = transport.call_args.args[2]
            self.assertNotIn('engine_override_reason', payload)
            for index in range(0, len(extra), 2):
                self.assertEqual(payload[extra[index][2:]], extra[index + 1])
        with patch.object(session_control, 'request', return_value={'id': 's1'}) as transport:
            session_control.execute(self.parse('start', '--no-wait', '--workspace', 'demo', '--prompt', 'override start', '--engine', 'flash', '--reason', 'Benchmark test', '--key', 'k3'))
        self.assertEqual(transport.call_args.args[2]['engine'], 'flash')
        self.assertEqual(transport.call_args.args[2]['engine_override_reason'], 'Benchmark test')

    def test_cli_sessions_list_filter_waiting_and_idle(self):
        sessions = [
            {'id': 's_running', 'state': 'running'},
            {'id': 's_idle', 'state': 'idle'},
            {'id': 's_wait_conf', 'state': 'running', 'agent_state': 'waiting-confirmation'},
            {'id': 's_wait_input', 'state': 'running', 'agent_state': 'waiting'},
            {'id': 's_stopped', 'state': 'stopped'},
        ]

        # Filter 'waiting' surfaces both confirmation and input waits
        waiting_filtered = [s for s in sessions if session_control.match_session_state(s, 'waiting')]
        self.assertEqual([s['id'] for s in waiting_filtered], ['s_wait_conf', 's_wait_input'])

        # Filter 'waiting-confirmation' surfaces only confirmation wait
        wait_conf_filtered = [s for s in sessions if session_control.match_session_state(s, 'waiting-confirmation')]
        self.assertEqual([s['id'] for s in wait_conf_filtered], ['s_wait_conf'])

        # Filter 'idle' surfaces both idle and waiting sessions (AC 8 & 9: waiting count as idle too)
        idle_filtered = [s for s in sessions if session_control.match_session_state(s, 'idle')]
        idle_ids = [s['id'] for s in idle_filtered]
        self.assertIn('s_idle', idle_ids)
        self.assertIn('s_wait_conf', idle_ids)
        self.assertIn('s_wait_input', idle_ids)
        self.assertNotIn('s_running', idle_ids)
        self.assertNotIn('s_stopped', idle_ids)

        # Filter 'running' excludes idle and waiting sessions
        running_filtered = [s for s in sessions if session_control.match_session_state(s, 'running')]
        self.assertEqual([s['id'] for s in running_filtered], ['s_running'])

    def test_status_digest_renders_agent_counts(self):
        status_data = {
            'cpu': {'percent': 12.5},
            'memory': {'used_bytes': 1024, 'total_bytes': 2048, 'percent': 50.0},
            'disk': [],
            'agents': {
                'running': 3,
                'idle': 2,
                'waiting': 1,
                'waiting_confirmation': 1,
            },
        }
        human_text = status.render_human(status_data)
        self.assertIn('agents: running=3 idle=2 waiting=1 (waiting-confirmation=1)', human_text)

    def _guarded_cleanup_service(self, watchdog_sessions=()):
        """Dispatcher whose guard facts are stubbed: no locks, no live sessions.

        Worktree-guard (intake a0f01448): _maybe_cleanup_worktree only acts on
        fresh positive evidence — an unlocked tree, no live native session
        referencing it, and a watchdog snapshot row with a terminal state and a
        closed linked intake. The stubs here provide exactly that evidence so
        the delegation itself stays testable without real git/aplexer/HTTP.
        """
        service = Dispatcher(settings={
            'cleanup_roots': ['/tmp/worktrees'],
            'machine_id': 'm1',
            'journal_dir': '/tmp/journal',
        })
        service.request = lambda method, path, body=None: {'sessions': list(watchdog_sessions)}
        return service

    def test_headless_auto_cleanup_worktree_safety(self):
        # 1. Without cleanup_roots, _maybe_cleanup_worktree is a no-op
        service = Dispatcher(settings={'cleanup_roots': []})
        with patch('agent_hub_host.session_cleanup.remove_owned_worktree') as mock_cleanup:
            service._maybe_cleanup_worktree(Path('/some/path'), 'native-1')
            mock_cleanup.assert_not_called()

        # 2. With cleanup_roots and full guard evidence (unlocked, no live
        # session, watchdog row terminal with closed intake), delegates to
        # remove_owned_worktree with stop_confirmed=True.
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td) / 'wt-task-1'
            service = self._guarded_cleanup_service(watchdog_sessions=[{
                'native_id': 'task-native-1', 'workspace': str(ws.resolve()),
                'linked_intake_open': False, 'state': 'stopped'}])
            with patch('agent_hub_host.session_cleanup.remove_owned_worktree') as mock_cleanup, \
                 patch('pathlib.Path.mkdir'), \
                 patch('agent_hub_host.worktree_guard.is_locked', return_value=False), \
                 patch('agent_hub_host.native_sessions.list_sessions', return_value=[]):
                service._maybe_cleanup_worktree(ws, 'task-native-1')
                mock_cleanup.assert_called_once()
                call_kwargs = mock_cleanup.call_args.kwargs
                self.assertEqual(call_kwargs['native_id'], 'task-native-1')
                self.assertTrue(call_kwargs['stop_confirmed'])
                self.assertEqual(call_kwargs['cleanup_roots'], ['/tmp/worktrees'])

    def test_headless_auto_cleanup_skips_locked_worktree(self):
        # AC2: a Hub-locked tree is never auto-cleaned, even with terminal evidence.
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td) / 'wt-task-1'
            service = self._guarded_cleanup_service(watchdog_sessions=[{
                'native_id': 'task-native-1', 'workspace': str(ws.resolve()),
                'linked_intake_open': False, 'state': 'stopped'}])
            with patch('agent_hub_host.session_cleanup.remove_owned_worktree') as mock_cleanup, \
                 patch('agent_hub_host.worktree_guard.is_locked', return_value=True):
                service._maybe_cleanup_worktree(ws, 'task-native-1')
                mock_cleanup.assert_not_called()

    def test_headless_auto_cleanup_skips_live_session_worktree(self):
        # AC2: a tree still referenced by a live native session is never auto-cleaned.
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td) / 'wt-task-1'
            live = MagicMock()
            live.phase = 'running'
            live.workspace = str(ws.resolve())
            live.cwd = None
            service = self._guarded_cleanup_service(watchdog_sessions=[{
                'native_id': 'task-native-1', 'workspace': str(ws.resolve()),
                'linked_intake_open': False, 'state': 'stopped'}])
            with patch('agent_hub_host.session_cleanup.remove_owned_worktree') as mock_cleanup, \
                 patch('agent_hub_host.worktree_guard.is_locked', return_value=False), \
                 patch('agent_hub_host.native_sessions.list_sessions', return_value=[live]):
                service._maybe_cleanup_worktree(ws, 'task-native-1')
                mock_cleanup.assert_not_called()

    def test_headless_auto_cleanup_requires_released_intake(self):
        # AC2: without a watchdog row proving terminal state AND closed intake,
        # the owned cleanup does not run (missing evidence keeps the tree).
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td) / 'wt-task-1'
            for row in ({'native_id': 'task-native-1', 'workspace': str(ws.resolve()),
                         'linked_intake_open': True, 'state': 'stopped'},
                        {'native_id': 'task-native-1', 'workspace': str(ws.resolve()),
                         'linked_intake_open': False, 'state': 'running'},
                        {'native_id': 'other-native', 'workspace': str(ws.resolve()),
                         'linked_intake_open': False, 'state': 'stopped'}):
                service = self._guarded_cleanup_service(watchdog_sessions=[row])
                with patch('agent_hub_host.session_cleanup.remove_owned_worktree') as mock_cleanup, \
                     patch('agent_hub_host.worktree_guard.is_locked', return_value=False), \
                     patch('agent_hub_host.native_sessions.list_sessions', return_value=[]):
                    service._maybe_cleanup_worktree(ws, 'task-native-1')
                    mock_cleanup.assert_not_called()


if __name__ == '__main__':
    unittest.main()
