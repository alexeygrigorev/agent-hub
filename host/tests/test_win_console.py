import os
from pathlib import Path
import queue
import subprocess
import sys
import threading

import pytest

from agent_hub_host import win_console as wc


class Backend:
    """An owned process/pipe cohort, including a final close-time frame."""
    pid = 123

    def __init__(self):
        self.pending = queue.Queue()
        self.events = []
        self.result = None
        self.final_drained = threading.Event()
        self.written = b''

    def resume(self):
        self.events.append('resume')
        self.pending.put(b'authorization code:')

    def read(self, size):
        data = self.pending.get(timeout=5)
        if data == b'final-frame':
            self.final_drained.set()
        return data

    def write(self, data):
        part = data[:3]
        self.written += part
        return len(part)

    def poll(self):
        return self.result

    def wait(self, timeout):
        if self.result is None:
            raise subprocess.TimeoutExpired('fixture', timeout)
        return self.result

    def terminate(self):
        self.events.append('terminate-owned-job')
        self.result = 1

    def close_console(self):
        self.events.append('close-console')
        self.pending.put(b'final-frame')
        # Reproduce the Win32 close requirement: output must keep draining
        # even after the login relay stops reading or the child exits.
        assert self.final_drained.wait(timeout=2), 'ConPTY final output was not drained'
        self.pending.put(b'')

    def close_input(self):
        self.events.append('close-input')

    def close_handles(self):
        self.events.append('close-handles')


def process(backend):
    return wc.ConsoleProcess(['fixture.exe'], env={}, cwd='C:\\fixture', _backend=backend)


def test_short_prompt_arrives_without_waiting_for_a_full_read_buffer():
    backend = Backend()
    p = process(backend)
    try:
        assert p.stdout.read1(1024) == b'authorization code:'
        assert p.stdin.write(b'synthetic-code\r\n') == len(b'synthetic-code\r\n')
        p.stdin.flush()
        assert backend.written == b'synthetic-code\r\n'
    finally:
        p.close()
    assert backend.events.index('terminate-owned-job') < backend.events.index('close-console')
    assert not p._reader.is_alive()
    assert p.stdout.buffer == b''


def test_natural_exit_cleanup_still_drains_final_frame_and_is_idempotent():
    backend = Backend()
    p = process(backend)
    assert p.stdout.read1(1024)
    backend.result = 0
    assert p.poll() == 0
    assert p.wait(timeout=1) == 0
    p.stdout.close()  # The external relay is already done reading.
    p.close()
    p.close()
    assert 'terminate-owned-job' not in backend.events
    assert backend.events.count('close-handles') == 1
    assert backend.final_drained.is_set()


def test_buffer_is_bounded_and_closed_memory_is_cleared():
    output = wc._Output()
    output.feed(b'x' * 200000)
    assert len(output.buffer) == 65536
    assert output.read1(4) == b'xxxx'
    output.close()
    assert output.buffer == b''
    assert output.read1(1024) == b''


def test_wait_timeout_does_not_fabricate_process_exit():
    backend = Backend()
    p = process(backend)
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            p.wait(timeout=0.1)
        assert p.poll() is None
    finally:
        p.close()


def test_input_bounds_and_closed_input_do_not_write():
    backend = Backend()
    channel = wc._Input(backend)
    with pytest.raises(ValueError):
        channel.write(b'x' * 65537)
    channel.close()
    with pytest.raises(BrokenPipeError):
        channel.write(b'code')
    assert backend.written == b''


def test_windows_argv_and_unicode_environment_are_separate_data():
    argv = [r'C:\Program Files\Node & tools\node.exe', r'C:\Users\A B\cli.js', 'a"b & %PATH%']
    line, env = wc._arguments(argv, {'USERPROFILE': r'C:\Users\A B', 'TEST': 'unicode-ä'}, r'C:\Users\A B')
    assert line == subprocess.list2cmdline(argv)
    assert env.endswith('\0\0')
    assert 'TEST=unicode-ä\0' in env
    assert 'USERPROFILE=C:\\Users\\A B\0' in env


@pytest.mark.parametrize('binary', ['cmd.exe', r'C:\tools\login.cmd', r'C:\tools\login.bat', 'node.exe\0bad'])
def test_shell_batch_and_relative_programs_are_refused(binary):
    with pytest.raises(ValueError):
        wc._arguments([binary], {}, r'C:\profile')


def test_environment_injection_is_refused():
    with pytest.raises(ValueError):
        wc._arguments([r'C:\node.exe'], {'BAD=KEY': 'value'}, r'C:\profile')
    with pytest.raises(ValueError):
        wc._arguments([r'C:\node.exe'], {'KEY': 'value\0OTHER=value'}, r'C:\profile')


@pytest.mark.skipif(os.name == 'nt', reason='non-Windows import contract')
def test_native_backend_fails_closed_on_other_platforms():
    with pytest.raises(OSError, match='unavailable'):
        wc.ConsoleProcess([r'C:\node.exe'], env={}, cwd=r'C:\profile')


@pytest.mark.skipif(os.name != 'nt', reason='physical Windows ConPTY acceptance')
def test_windows_native_child_sees_a_real_console_and_exits(tmp_path):
    script = 'import sys;print("NATIVE_TTY="+str(sys.stdin.isatty()),flush=True);print("NATIVE_RESULT="+input(),flush=True)'
    p = wc.ConsoleProcess([str(Path(sys.executable).resolve()), '-c', script], env=dict(os.environ), cwd=str(tmp_path))
    try:
        p.stdin.write(b'synthetic-input\r\n')
        p.stdin.flush()
        assert p.wait(timeout=10) == 0
        with p.stdout.condition:
            assert p.stdout.condition.wait_for(lambda: b'NATIVE_RESULT=synthetic-input' in p.stdout.buffer, timeout=3)
            assert b'NATIVE_TTY=True' in p.stdout.buffer
    finally:
        p.close()
