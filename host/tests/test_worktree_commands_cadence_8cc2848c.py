"""Intake 8cc2848c AC2: hub worktree refresh (status/remove) commands must be
claimed and executed on their own cadence. The commands poll previously shared
``next_worktree`` and ``worktree_thread`` with the worktree-observer step,
whose check reset ``next_worktree`` to +60s before the commands check ran, so
the commands step never fired, refresh requests stayed queued forever and the
Hub worktree close gate kept reporting 'status unknown'."""
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent_hub_host import session_host

try:
    from host.tests.test_fleet import settings
except ImportError:  # PYTHONPATH=host/tests
    from test_fleet import settings

REPO = Path(__file__).resolve().parents[2]


def test_commands_cadence_is_bounded_under_two_minutes():
    # AC3: worktree refresh must be <=2 min old; the 30s poll plus one bounded
    # execution keeps that. The commands step keeps its own cadence variable
    # and is scheduled exactly once per loop pass.
    assert session_host.WORKTREE_POLL_S <= 60, session_host.WORKTREE_POLL_S
    src = (REPO / 'host' / 'agent_hub_host' / 'session_host.py').read_text()
    assert 'next_worktree_commands' in src
    assert src.count('next_worktree_commands = time.monotonic() + WORKTREE_POLL_S') == 1


def test_run_schedules_worktree_commands_independent_of_observer():
    with tempfile.TemporaryDirectory() as directory:
        observer_calls, commands_calls = [], []
        observer_done = threading.Event()
        commands_done = threading.Event()

        def fake_observer(dispatcher, machine_id, journal_dir):
            observer_calls.append(machine_id)
            observer_done.set()

        def fake_commands(dispatcher):
            commands_calls.append(1)
            commands_done.set()

        patches = [
            patch.object(session_host.config, 'load_config',
                         return_value={'machine_id': 'test-machine', 'access_token': 'synthetic'}),
            patch.object(session_host.fleet, 'load_settings', return_value={}),
            patch.object(session_host, 'qualify_sender_binding',
                         return_value=SimpleNamespace(session_id='test-native',
                                                      workspace=str(Path.cwd()))),
            patch.object(session_host.fleet, 'Dispatcher'),
            patch.object(session_host.session_control, 'execute'),
            patch.object(session_host.session_messages, 'provision_credentials'),
            patch.object(session_host, 'worktree_step_once', side_effect=fake_observer),
            patch.object(session_host, 'worktree_commands_step_once', side_effect=fake_commands),
            patch.object(session_host, 'maybe_start_status_thread', return_value=None),
            patch.object(session_host, 'maybe_start_metrics_thread', return_value=None),
            patch.object(session_host, 'maybe_start_disk_thread', return_value=None),
            patch.object(session_host, 'maybe_start_host_health_thread', return_value=None),
            patch.object(session_host, 'maybe_start_role_consumer_thread', return_value=None),
            patch.object(session_host, 'maybe_start_token_thread', return_value=None),
        ]
        for item in patches:
            item.start()
        try:
            session_host.run('synthetic-settings', directory, cycles=1)
        finally:
            for item in reversed(patches):
                item.stop()
        assert observer_done.wait(10), 'worktree observer step did not run'
        # Regression: the commands step is reachable on its own cadence even
        # though the observer check already reset its own timer this pass.
        assert commands_done.wait(10), 'worktree commands step never scheduled'
        assert observer_calls == ['test-machine']
        assert len(commands_calls) == 1
