"""The bootstrap wrapper must preserve the controller child's dying output.

aplexer prunes a finished session's record and history within seconds, so a
controller child that dies at startup used to leave no stderr evidence at
all (intake 4d33b30d). Most tests run the wrapper in a subprocess; the invalid
inherited-handle case calls it in-process to simulate failed OS descriptors.
"""
import os
import subprocess
import sys
import types
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


def test_wrapper_binds_owned_streams_when_inherited_windows_handles_are_invalid(tmp_path, monkeypatch):
    import importlib.util

    spec = importlib.util.spec_from_file_location('bootstrap_log_invalid_stdio', WRAPPER)
    wrapper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(wrapper)
    log = tmp_path / 'boot.log'
    calls = []

    class InvalidStream:
        def reconfigure(self, **_kwargs):
            calls.append('reconfigure')
            raise OSError(9, 'The handle is invalid')

    invalid = InvalidStream()
    fake_sys = types.SimpleNamespace(stdout=invalid, stderr=invalid,
                                     __stdout__=invalid, __stderr__=invalid, argv=[])
    monkeypatch.setattr(wrapper, 'sys', fake_sys)

    class InvalidStandardHandles:
        def __getattr__(self, name):
            return getattr(os, name)

        @staticmethod
        def dup2(_source, _target):
            raise OSError(9, 'The handle is invalid')

    monkeypatch.setattr(wrapper, 'os', InvalidStandardHandles())

    class NoopThread:
        def __init__(self, *_args, **_kwargs):
            pass

        def start(self):
            pass

        def join(self, timeout=None):
            pass

    monkeypatch.setattr(wrapper.threading, 'Thread', NoopThread)

    def run_session_host(*_args, **_kwargs):
        wrapper.sys.stdout.write('stdout survived\n')
        wrapper.sys.stderr.write('stderr survived\n')

    monkeypatch.setattr(wrapper.runpy, 'run_module', run_session_host)
    wrapper.main(['--log', str(log), '--', '-m', 'agent_hub_host.session_host'])
    wrapper.sys.stdout.close()
    wrapper.sys.stderr.close()

    assert calls == []
    assert fake_sys.stdout is fake_sys.__stdout__
    assert fake_sys.stderr is fake_sys.__stderr__
    captured = log.read_text(encoding='utf-8')
    assert 'stdout survived' in captured
    assert 'stderr survived' in captured
    assert 'session_host_exit_code=0' in captured


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
