"""The bootstrap wrapper must preserve the controller child's dying output.

aplexer prunes a finished session's record and history within seconds, so a
controller child that dies at startup used to leave no stderr evidence at
all (intake 4d33b30d). These tests run the real wrapper in a subprocess.
"""
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
WRAPPER = REPO / 'host' / 'agent_hub_host' / 'bootstrap_log.py'


def launch(log, *command, env_extra=None):
    env = dict(os.environ)
    env.update(env_extra or {})
    return subprocess.run([sys.executable, '-u', str(WRAPPER), '--log', str(log), *command],
                          capture_output=True, text=True, timeout=120, env=env,
                          stdin=subprocess.DEVNULL)


def dying_session_host_command(tmp_path):
    # An isolated, definitely unenrolled host config makes the real session
    # host fail during startup with a traceback on stderr.
    return ('--', '-m', 'agent_hub_host.session_host',
            '--config', str(tmp_path / 'settings.json'),
            '--journal-dir', str(tmp_path / 'journal'))


def test_dying_session_host_leaves_its_stderr_in_the_bootstrap_log(tmp_path):
    log = tmp_path / 'boot.log'
    result = launch(log, *dying_session_host_command(tmp_path),
                    env_extra={'HUB_CONFIG_FILE': str(tmp_path / 'missing-host.json'),
                               'PYTHONPATH': str(REPO / 'host'),
                               'PYTHONNOUSERSITE': '1'})
    assert result.returncode != 0
    captured = log.read_text(encoding='utf-8', errors='replace')
    assert 'session_host bootstrap' in captured
    assert 'Traceback' in captured
    assert 'session_host_exit_code=1' in captured
    # The child's own run() produced the failure, inside the pinned module.
    assert 'agent_hub_host' in captured


def test_wrapper_refuses_any_other_module(tmp_path):
    log = tmp_path / 'boot.log'
    result = launch(log, '--', '-m', 'os')
    assert result.returncode == 2
    assert not log.exists()
    result = launch(tmp_path / 'boot2.log', '-m', 'agent_hub_host.session_host')
    assert result.returncode == 2
    assert not (tmp_path / 'boot2.log').exists()


def test_bootstrap_log_rotation_keeps_the_recent_tail(tmp_path):
    log = tmp_path / 'boot.log'
    log.write_text('OLDEST-ATTEMPT-MARKER\n' + 'x' * (300 * 1024) + '\n', encoding='utf-8')
    result = launch(log, *dying_session_host_command(tmp_path),
                    env_extra={'HUB_CONFIG_FILE': str(tmp_path / 'missing-host.json'),
                               'PYTHONPATH': str(REPO / 'host'),
                               'PYTHONNOUSERSITE': '1'})
    assert result.returncode != 0
    data = log.read_bytes()
    assert len(data) < 200 * 1024
    captured = data.decode('utf-8', 'replace')
    assert 'OLDEST-ATTEMPT-MARKER' not in captured
    assert 'truncated to the last' in captured
    assert 'Traceback' in captured
