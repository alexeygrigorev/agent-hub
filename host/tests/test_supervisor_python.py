"""The native host child must inherit the supervisor's uv interpreter."""
import importlib.util
from pathlib import Path
from unittest.mock import Mock, patch


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
    assert receipt['command'][:4] == ['/release/.venv/bin/python', '-u', '-m', 'agent_hub_host.session_host']
    assert calls[0][calls[0].index('--memory') + 1] == '1500M'
    assert calls[0][calls[0].index('--pids') + 1] == '256'
    assert calls[0][calls[0].index('--cpu-quota-us') + 1] == '100000'
    assert calls[0][calls[0].index('--cpu-period-us') + 1] == '100000'
    assert '--env' in calls[0]
    assert 'AGENT_HUB_MANAGED_DIR=/custom/managed' in calls[0]
    assert 'PATH=/user/runtimes/bin' in calls[0]
    assert 'CLAUDE_CODE_GIT_BASH_PATH=/user/git/bash' in calls[0]
    assert 'UV_PYTHON_INSTALL_DIR=/user/python' in calls[0]
    assert calls[-1][:2] == ('kill', receipt['id'])
