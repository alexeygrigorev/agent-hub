"""The native host child must inherit the supervisor's uv interpreter."""
import importlib.util
import json
from pathlib import Path
from unittest.mock import Mock, patch
import pytest


def test_supervisor_propagates_venv_python_to_owned_native_child(tmp_path):
    script = Path(__file__).resolve().parents[2] / 'scripts' / 'session-host-supervisor.py'
    spec = importlib.util.spec_from_file_location('supervisor_python_contract', script)
    supervisor = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(supervisor)
    config = tmp_path / 'config.json'
    config.write_text('{}')
    calls, receipt = [], {}

    def native(*args):
        calls.append(args)
        if args[0] == 'start':
            receipt.update(id='00000000-0000-0000-0000-000000000001',
                           tag=args[args.index('--tag') + 1], workspace=str(tmp_path),
                           engine='shell', created_at_ms=1,
                           command=list(args[args.index('--') + 1:]))
        return receipt.copy()

    stopped = Mock()
    stopped.wait.return_value = True
    argv = [str(script), '--workspace', str(tmp_path), '--config', str(config),
            '--credential-config', str(config), '--journal-dir', str(tmp_path / 'journal')]
    with patch.object(supervisor.sys, 'argv', argv), \
         patch.object(supervisor.sys, 'executable', '/release/.venv/bin/python'), \
         patch.object(supervisor.os, 'environ', {'AGENT_HUB_MANAGED_DIR': '/custom/managed',
             'PATH': '/user/runtimes/bin', 'CLAUDE_CODE_GIT_BASH_PATH': '/user/git/bash',
             'UV_PYTHON_INSTALL_DIR': '/user/python'}), \
         patch.object(supervisor.threading, 'Event', return_value=stopped), \
         patch.object(supervisor.signal, 'signal'), \
         patch.object(supervisor, 'native', side_effect=native):
        supervisor.main()
    wrapper = tmp_path / 'host' / 'agent_hub_host' / 'bootstrap_log.py'
    boot_log = tmp_path / 'journal' / 'session-host-bootstrap.log'
    assert receipt['command'][:3] == ['/release/.venv/bin/python', '-u', str(wrapper)]
    assert receipt['command'][3:6] == ['--log', str(boot_log), '--']
    assert receipt['command'][6:9] == ['-m', 'agent_hub_host.session_host', '--config']
    assert calls[0][calls[0].index('--memory') + 1] == '1500M'
    assert calls[0][calls[0].index('--pids') + 1] == '256'
    assert calls[0][calls[0].index('--cpu-quota-us') + 1] == '100000'
    assert calls[0][calls[0].index('--cpu-period-us') + 1] == '100000'
    assert '--env' in calls[0]
    assert 'AGENT_HUB_MANAGED_DIR=/custom/managed' in calls[0]
    assert 'PATH=/user/runtimes/bin' in calls[0]
    assert 'CLAUDE_CODE_GIT_BASH_PATH=/user/git/bash' in calls[0]
    assert 'UV_PYTHON_INSTALL_DIR=/user/python' in calls[0]
    assert 'PYTHONPATH=' + str(tmp_path / 'host') in calls[0]
    assert calls[-1][:2] == ('kill', receipt['id'])


def _load_supervisor():
    script = Path(__file__).resolve().parents[2] / 'scripts' / 'session-host-supervisor.py'
    spec = importlib.util.spec_from_file_location('supervisor_wedge_contract', script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_wedged_controller_is_killed_by_pid_forgotten_and_never_respawned(tmp_path):
    supervisor = _load_supervisor()
    import agent_hub_host.bootstrap_control as bootstrap_control
    receipt = {'id': 'abc', 'tag': 't', 'engine': 'shell', 'created_at_ms': 1,
               'command': ['x'], 'workspace': str(tmp_path)}
    calls, killed = [], []

    def native(*args):
        calls.append(args)
        if args[0] == 'start':
            receipt.update(tag=args[args.index('--tag') + 1], workspace=str(tmp_path),
                           command=list(args[args.index('--') + 1:]))
            return dict(receipt)
        if args[0] == 'status':
            if len([c for c in calls if c[0] == 'status']) == 1:
                return dict(receipt, phase='running', worker_alive=True, worker_pid=11, workload_pid=22)
            raise supervisor.subprocess.TimeoutExpired('aplexer', 35)
        return {}

    class Stop:
        def wait(self, _):
            return False

        def is_set(self):
            return False

    with patch.object(bootstrap_control, 'receipt_absent', return_value=False), \
         patch.object(supervisor, 'native', side_effect=native), \
         patch.object(supervisor, 'kill_pid', side_effect=lambda pid: killed.append(pid) or True), \
         patch.object(supervisor, 'journal_lock_held', return_value=False), \
         patch.object(supervisor, 'publish_controller_crash'):
        try:
            supervisor.run_controller(Mock(control_dir=None), tmp_path, ['x'], tag_prefix='p-', env_args=[],
                                      journal=tmp_path, stop=Stop())
            raise AssertionError('must raise')
        except RuntimeError as error:
            assert 'wedged' in str(error)
    assert killed == [11, 22]
    assert ('forget', 'abc', '--force') in calls
    assert [c[0] for c in calls].count('start') == 1


def test_no_second_controller_while_journal_lock_is_held(tmp_path):
    supervisor = _load_supervisor()
    with patch.object(supervisor, 'native') as native, \
         patch.object(supervisor, 'journal_lock_held', return_value=True):
        try:
            supervisor.run_controller(Mock(control_dir=None), tmp_path, ['x'], tag_prefix='p-', env_args=[],
                                      journal=tmp_path, stop=Mock())
            raise AssertionError('must refuse')
        except RuntimeError as error:
            assert 'lock held' in str(error)
    native.assert_not_called()


def test_native_failure_logs_aplexer_stderr_without_changing_type():
    supervisor = _load_supervisor()
    done = Mock(returncode=1, stdout='', stderr='a: no matching session\n')
    with patch.object(supervisor.subprocess, 'run', return_value=done):
        try:
            supervisor.native('status', 'x')
            raise AssertionError('must raise')
        except RuntimeError as error:
            reason = supervisor.failure_reason(error)
    assert reason['type'] == 'RuntimeError' and reason['detail'] == 'Native controller operation failed'
    assert reason['exit_code'] == 1 and reason['stderr'] == 'a: no matching session'


def _controller_stop_once():
    class Stop:
        def wait(self, _):
            return False

        def is_set(self):
            return False
    return Stop()


def test_child_exit_event_carries_phase_code_history_and_host_health_crash():
    supervisor = _load_supervisor()
    receipt = {'id': 'receipt-1'}
    health = {'service_id': 'hub-controller', 'worker': 'broken', 'reason': 'service_crash'}
    event = supervisor.child_exit_event(
        receipt,
        {'phase': 'exited', 'worker_alive': False, 'exit': {'code': 7}},
        Path('/journal/session-host-bootstrap.log'), health, True, 'native history tail')
    assert event['phase'] == 'exited'
    assert event['exit_code'] == 7
    assert event['history_tail'] == 'native history tail'
    assert event['host_health_crash_record'] == health
    assert event['crash_published'] is True


def test_pruned_row_reads_only_the_latest_bootstrap_exit_code(tmp_path):
    supervisor = _load_supervisor()
    path = tmp_path / 'session-host-bootstrap.log'
    path.write_text('=== session_host bootstrap previous ===\nsession_host_exit_code=0\n'
                    '=== session_host bootstrap current ===\nImportError: bad path\n',
                    encoding='utf-8')
    assert supervisor.boot_log_exit_code(path) is None
    with path.open('a', encoding='utf-8') as stream:
        stream.write('session_host_exit_code=1\n')
    assert supervisor.boot_log_exit_code(path) == 1


def test_pruned_child_logs_boot_cause_before_cleanup_and_confirms_windows_lease(
        tmp_path, capsys, monkeypatch):
    supervisor = _load_supervisor()
    import agent_hub_host.bootstrap_control as bootstrap_control

    control = tmp_path / 'control'
    journal = tmp_path / 'journal'
    journal.mkdir()
    boot_log = journal / 'session-host-bootstrap.log'
    boot_log.write_text('=== session_host bootstrap current ===\n'
                        'ImportError: No module named agent_hub_host\n'
                        'session_host_exit_code=1\n', encoding='utf-8')
    receipt = {}

    def native(*args):
        if args[0] == 'start':
            receipt.update(id='00000000-0000-0000-0000-000000000005',
                           tag=args[args.index('--tag') + 1],
                           workspace=args[args.index('--workspace') + 1], engine='shell',
                           created_at_ms=5, command=list(args[args.index('--') + 1:]))
            return dict(receipt)
        if args[0] == 'status':
            error = RuntimeError('Native controller operation failed')
            error.stderr = 'a: no matching session'
            error.exit_code = 1
            raise error
        raise AssertionError('unexpected native operation: %r' % (args,))

    monkeypatch.setattr(supervisor, 'is_windows', lambda: True)
    monkeypatch.setattr(supervisor, 'native', native)
    monkeypatch.setattr(supervisor, 'journal_lock_held', lambda _journal: False)
    monkeypatch.setattr(supervisor, 'child_output_tail', lambda _receipt: 'captured history tail')
    posted = []
    monkeypatch.setattr(supervisor, 'publish_controller_crash',
                        lambda *_args, **kwargs: posted.append(kwargs.get('service')) or True)
    monkeypatch.setattr(bootstrap_control, 'receipt_absent', lambda _receipt: True)
    with pytest.raises(RuntimeError, match='native record was removed'):
        supervisor.run_controller(Mock(control_dir=control), tmp_path, ['python', 'child.py'],
                                  tag_prefix='test-', env_args=[], journal=journal,
                                  stop=_controller_stop_once(), boot_log=boot_log)

    events = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line]
    exit_event = next(event for event in events if event['event'] == 'controller_child_exit')
    assert exit_event['phase'] == 'record_removed'
    assert exit_event['exit_code'] == 1
    assert 'No module named agent_hub_host' in exit_event['stderr_tail']
    assert exit_event['history_tail'] == 'captured history tail'
    assert 'no matching session' in exit_event['status_error']
    assert exit_event['host_health_crash_record']['service_id'] == 'hub-controller'
    assert exit_event['crash_published'] is None
    health_event = next(event for event in events if event['event'] == 'controller_child_health_publish')
    assert health_event['published'] is True
    assert health_event['record'] == exit_event['host_health_crash_record']
    assert posted == [exit_event['host_health_crash_record']]
    assert not any(event['event'] == 'controller_cleanup_unconfirmed' for event in events)
    assert json.loads((control / 'stopped.json').read_text())['containment_empty'] is True
    assert not (control / 'owner.json').exists()
    assert not (control / 'needs-attention.json').exists()


def test_dead_child_logs_health_record_before_cleanup(tmp_path, capsys, monkeypatch):
    supervisor = _load_supervisor()
    import agent_hub_host.bootstrap_control as bootstrap_control

    control = tmp_path / 'control'
    journal = tmp_path / 'journal'
    journal.mkdir()
    boot_log = journal / 'session-host-bootstrap.log'
    boot_log.write_text('PermissionError: host settings path is locked\n', encoding='utf-8')
    receipt = {}

    def native(*args):
        if args[0] == 'start':
            receipt.update(id='00000000-0000-0000-0000-000000000006',
                           tag=args[args.index('--tag') + 1],
                           workspace=args[args.index('--workspace') + 1], engine='shell',
                           created_at_ms=6, command=list(args[args.index('--') + 1:]))
            return dict(receipt)
        if args[0] == 'status':
            return dict(receipt, phase='exited', worker_alive=False, containment_empty=True,
                        exit={'code': 7})
        raise AssertionError('unexpected native operation: %r' % (args,))

    monkeypatch.setattr(supervisor, 'is_windows', lambda: True)
    monkeypatch.setattr(supervisor, 'native', native)
    monkeypatch.setattr(supervisor, 'journal_lock_held', lambda _journal: False)
    posted = []
    monkeypatch.setattr(supervisor, 'publish_controller_crash',
                        lambda *_args, **kwargs: posted.append(kwargs.get('service')) or True)
    monkeypatch.setattr(supervisor, 'child_output_tail', lambda _receipt: 'a: worker exited')
    monkeypatch.setattr(bootstrap_control, 'receipt_absent', lambda _receipt: False)
    with pytest.raises(RuntimeError, match='Controller exited; supervisor will restart'):
        supervisor.run_controller(Mock(control_dir=control), tmp_path, ['python', 'child.py'],
                                  tag_prefix='test-', env_args=[], journal=journal,
                                  stop=_controller_stop_once(), boot_log=boot_log)

    events = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line]
    exit_event = next(event for event in events if event['event'] == 'controller_child_exit')
    assert exit_event['phase'] == 'exited'
    assert exit_event['exit_code'] == 7
    assert exit_event['host_health_crash_record']['service_id'] == 'hub-controller'
    health_event = next(event for event in events if event['event'] == 'controller_child_health_publish')
    assert health_event['published'] is True
    assert health_event['record'] == exit_event['host_health_crash_record']
    assert posted == [exit_event['host_health_crash_record']]
    assert exit_event['history_tail'] == 'a: worker exited'
    assert not any(event['event'] == 'controller_cleanup_unconfirmed' for event in events)
