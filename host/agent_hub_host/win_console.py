"""Memory-only Windows ConPTY for native login, with an owned kill-on-close job.

No terminal history, screenshots, shell commands, or credential logs are made.
Win32 follows Microsoft's creating-a-pseudoconsole-session contract. Importing
this module is portable; using the native backend requires Windows 10 1809+.
"""
from __future__ import annotations

import ctypes as c
from ctypes import wintypes as w
import ntpath
import os
import subprocess
import threading


class _Coord(c.Structure):
    _fields_ = [('X', c.c_short), ('Y', c.c_short)]


class _Startup(c.Structure):
    _fields_ = [('cb', w.DWORD), ('reserved', w.LPWSTR), ('desktop', w.LPWSTR),
                ('title', w.LPWSTR), ('x', w.DWORD), ('y', w.DWORD),
                ('width', w.DWORD), ('height', w.DWORD), ('chars_x', w.DWORD),
                ('chars_y', w.DWORD), ('fill', w.DWORD), ('flags', w.DWORD),
                ('show', w.WORD), ('reserved_size', w.WORD), ('reserved_data', c.c_void_p),
                ('stdin', w.HANDLE), ('stdout', w.HANDLE), ('stderr', w.HANDLE)]


class _StartupEx(c.Structure):
    _fields_ = [('startup', _Startup), ('attributes', c.c_void_p)]


class _ProcessInfo(c.Structure):
    _fields_ = [('process', w.HANDLE), ('thread', w.HANDLE), ('pid', w.DWORD), ('tid', w.DWORD)]


class _BasicLimits(c.Structure):
    _fields_ = [('process_time', c.c_int64), ('job_time', c.c_int64), ('flags', w.DWORD),
                ('min_working_set', c.c_size_t), ('max_working_set', c.c_size_t),
                ('active_processes', w.DWORD), ('affinity', c.c_size_t),
                ('priority', w.DWORD), ('scheduling', w.DWORD)]


class _IoCounters(c.Structure):
    _fields_ = [(name, c.c_uint64) for name in ('read_ops', 'write_ops', 'other_ops', 'read_bytes', 'write_bytes', 'other_bytes')]


class _JobLimits(c.Structure):
    _fields_ = [('basic', _BasicLimits), ('io', _IoCounters),
                ('process_memory', c.c_size_t), ('job_memory', c.c_size_t),
                ('peak_process_memory', c.c_size_t), ('peak_job_memory', c.c_size_t)]


def _arguments(argv, env, cwd):
    if (not isinstance(argv, (list, tuple)) or not argv
            or any(not isinstance(a, str) or '\0' in a for a in argv)
            or not ntpath.isabs(argv[0]) or not argv[0].lower().endswith('.exe')):
        raise ValueError('Native login requires an absolute executable and separate arguments')
    if not isinstance(cwd, str) or not ntpath.isabs(cwd) or '\0' in cwd:
        raise ValueError('Native login requires an absolute working directory')
    if not isinstance(env, dict) or any(
            not isinstance(k, str) or not k or '=' in k or '\0' in k
            or not isinstance(v, str) or '\0' in v for k, v in env.items()):
        raise ValueError('Invalid native login environment')
    # CreateProcessW receives an executable separately: no cmd.exe, batch
    # expansion, shell interpolation, or inherited environment fallback.
    return subprocess.list2cmdline(argv), '\0'.join(k + '=' + v for k, v in sorted(env.items(), key=lambda item: item[0].casefold())) + '\0\0'


class _Windows:
    def __init__(self, argv, env, cwd):
        if os.name != 'nt':
            raise OSError('Windows pseudoconsole is unavailable on this platform')
        command, environment = _arguments(argv, env, cwd)
        self.k = c.WinDLL('kernel32', use_last_error=True)
        self.handles = []
        self.hpc = c.c_void_p()
        self.process = self.job = self.input_write = self.output_read = None
        self.pid = None
        self._prototypes()
        attributes = None
        attributes_initialized = False
        try:
            input_read, self.input_write = self._pipe()
            self.output_read, output_write = self._pipe()
            # A wide console avoids inserting display wraps into OAuth URLs.
            if self.k.CreatePseudoConsole(_Coord(4096, 40), input_read, output_write, 0, c.byref(self.hpc)) != 0:
                raise OSError('Windows pseudoconsole creation failed')
            size = c.c_size_t()
            self.k.InitializeProcThreadAttributeList(None, 1, 0, c.byref(size))
            if not 0 < size.value <= 1048576:
                raise OSError('Windows pseudoconsole attributes unavailable')
            storage = c.create_string_buffer(size.value)
            attributes = c.cast(storage, c.c_void_p)
            self._ok(self.k.InitializeProcThreadAttributeList(attributes, 1, 0, c.byref(size)))
            attributes_initialized = True
            # The Win32 contract takes HPCON itself here, not &HPCON.
            self._ok(self.k.UpdateProcThreadAttribute(attributes, 0, 0x00020016, self.hpc, c.sizeof(c.c_void_p), None, None))
            startup = _StartupEx()
            startup.startup.cb = c.sizeof(_StartupEx)
            startup.attributes = attributes
            self.job = self._track(self.k.CreateJobObjectW(None, None))
            limits = _JobLimits()
            limits.basic.flags = 0x2000 | 0x200 | 0x8  # kill-on-close, job memory, process count
            limits.basic.active_processes = 16
            limits.job_memory = 1500 * 1024 * 1024
            self._ok(self.k.SetInformationJobObject(self.job, 9, c.byref(limits), c.sizeof(limits)))
            info = _ProcessInfo()
            command_buffer = c.create_unicode_buffer(command)
            environment_buffer = c.create_unicode_buffer(environment)
            # Suspend before the job assignment so descendants cannot escape
            # ownership through a startup/relaunch race.
            self._ok(self.k.CreateProcessW(argv[0], command_buffer, None, None, False,
                     0x00080000 | 0x00000400 | 0x4, c.cast(environment_buffer, c.c_void_p),
                     cwd, c.byref(startup), c.byref(info)))
            self.process = self._track(info.process)
            self.thread = self._track(info.thread)
            self.pid = int(info.pid)
            self._ok(self.k.AssignProcessToJobObject(self.job, self.process))
            # Keep the ConPTY ends through CreateProcess, then release our
            # references so closing ConPTY can produce a real pipe EOF.
            self._close(input_read)
            self._close(output_write)
        except BaseException:
            if self.process:
                self.k.TerminateProcess(self.process, 1)
            try:
                self.close_console()
            finally:
                self.close_handles()
            raise
        finally:
            if attributes_initialized:
                self.k.DeleteProcThreadAttributeList(attributes)

    def _prototypes(self):
        ptr = c.c_void_p
        signatures = {
            'CreatePipe': ([c.POINTER(w.HANDLE), c.POINTER(w.HANDLE), ptr, w.DWORD], w.BOOL),
            'CreatePseudoConsole': ([_Coord, w.HANDLE, w.HANDLE, w.DWORD, c.POINTER(ptr)], c.c_long),
            'ClosePseudoConsole': ([ptr], None),
            'InitializeProcThreadAttributeList': ([ptr, w.DWORD, w.DWORD, c.POINTER(c.c_size_t)], w.BOOL),
            'UpdateProcThreadAttribute': ([ptr, c.c_size_t, c.c_size_t, ptr, c.c_size_t, ptr, ptr], w.BOOL),
            'DeleteProcThreadAttributeList': ([ptr], None),
            'CreateJobObjectW': ([ptr, w.LPCWSTR], w.HANDLE),
            'SetInformationJobObject': ([w.HANDLE, c.c_int, ptr, w.DWORD], w.BOOL),
            'AssignProcessToJobObject': ([w.HANDLE, w.HANDLE], w.BOOL),
            'TerminateJobObject': ([w.HANDLE, w.UINT], w.BOOL),
            'TerminateProcess': ([w.HANDLE, w.UINT], w.BOOL),
            'CreateProcessW': ([w.LPCWSTR, w.LPWSTR, ptr, ptr, w.BOOL, w.DWORD, ptr, w.LPCWSTR, ptr, c.POINTER(_ProcessInfo)], w.BOOL),
            'ResumeThread': ([w.HANDLE], w.DWORD),
            'CloseHandle': ([w.HANDLE], w.BOOL),
            'WaitForSingleObject': ([w.HANDLE, w.DWORD], w.DWORD),
            'GetExitCodeProcess': ([w.HANDLE, c.POINTER(w.DWORD)], w.BOOL),
            'ReadFile': ([w.HANDLE, ptr, w.DWORD, c.POINTER(w.DWORD), ptr], w.BOOL),
            'WriteFile': ([w.HANDLE, ptr, w.DWORD, c.POINTER(w.DWORD), ptr], w.BOOL),
        }
        for name, (args, result) in signatures.items():
            function = getattr(self.k, name)
            function.argtypes, function.restype = args, result

    @staticmethod
    def _ok(result):
        if not result:
            raise OSError('Windows native login operation failed')

    def _track(self, handle):
        if not handle:
            raise OSError('Windows native login handle unavailable')
        self.handles.append(handle)
        return handle

    def _pipe(self):
        read, write = w.HANDLE(), w.HANDLE()
        self._ok(self.k.CreatePipe(c.byref(read), c.byref(write), None, 0))
        return self._track(read.value), self._track(write.value)

    def _close(self, handle):
        if handle in self.handles:
            self.k.CloseHandle(handle)
            self.handles.remove(handle)

    def read(self, size):
        buffer = c.create_string_buffer(size)
        received = w.DWORD()
        if not self.k.ReadFile(self.output_read, buffer, size, c.byref(received), None):
            if c.get_last_error() in (109, 232, 995):
                return b''
            raise OSError('Windows native login read failed')
        return buffer.raw[:received.value]

    def resume(self):
        if self.k.ResumeThread(self.thread) == 0xFFFFFFFF:
            raise OSError('Windows native login could not resume')
        self._close(self.thread)

    def write(self, data):
        buffer = c.create_string_buffer(data, len(data))
        sent = w.DWORD()
        self._ok(self.k.WriteFile(self.input_write, buffer, len(data), c.byref(sent), None))
        return int(sent.value)

    def poll(self):
        result = self.k.WaitForSingleObject(self.process, 0)
        if result == 258:
            return None
        self._ok(result == 0)
        code = w.DWORD()
        self._ok(self.k.GetExitCodeProcess(self.process, c.byref(code)))
        return int(code.value)

    def wait(self, timeout):
        millis = 0xFFFFFFFF if timeout is None else max(0, min(int(timeout * 1000), 0xFFFFFFFE))
        result = self.k.WaitForSingleObject(self.process, millis)
        if result == 258:
            raise subprocess.TimeoutExpired('native-login', timeout)
        self._ok(result == 0)
        return self.poll()

    def terminate(self):
        self._ok(self.k.TerminateJobObject(self.job, 1))

    def close_input(self):
        self._close(self.input_write)

    def close_console(self):
        if self.hpc.value:
            handle, self.hpc = self.hpc, c.c_void_p()
            self.k.ClosePseudoConsole(handle)

    def close_handles(self):
        for handle in self.handles[:]:
            self._close(handle)


class _Output:
    def __init__(self):
        self.condition = threading.Condition()
        self.buffer = b''
        self.eof = self.closed = False

    def feed(self, data):
        with self.condition:
            if not self.closed:
                self.buffer = (self.buffer + data)[-65536:]
            self.condition.notify_all()

    def finish(self):
        with self.condition:
            self.eof = True
            self.condition.notify_all()

    def read1(self, size=8192):
        if size == 0:
            return b''
        size = max(1, min(size, 65536))
        with self.condition:
            self.condition.wait_for(lambda: bool(self.buffer) or self.eof or self.closed)
            data, self.buffer = self.buffer[:size], self.buffer[size:]
            return data

    read = read1

    def close(self):
        with self.condition:
            self.closed = self.eof = True
            self.buffer = b''
            self.condition.notify_all()


class _Input:
    def __init__(self, backend):
        self.backend, self.closed = backend, False

    def write(self, data):
        if self.closed:
            raise BrokenPipeError('Native login input is closed')
        if not isinstance(data, bytes) or len(data) > 65536:
            raise ValueError('Invalid native login input')
        offset = 0
        while offset < len(data):
            sent = self.backend.write(data[offset:])
            if not 0 < sent <= len(data) - offset:
                raise BrokenPipeError('Native login input is unavailable')
            offset += sent
        return offset

    def flush(self):
        pass

    def close(self):
        if not self.closed:
            self.closed = True
            self.backend.close_input()


class ConsoleProcess:
    """Popen-like native login process. Always call close() in the relay finally.

    An internal drainer stays active even if the relay's reader stops on exit:
    ClosePseudoConsole may emit a final frame and deadlock without a drainer.
    """
    def __init__(self, argv, *, env, cwd, _backend=None):
        self.backend = _backend if _backend is not None else _Windows(argv, env, str(cwd))
        self.pid, self.returncode = self.backend.pid, None
        self.stdin, self.stdout = _Input(self.backend), _Output()
        self._close_lock, self._closed = threading.Lock(), False
        self._reader = threading.Thread(target=self._drain, daemon=True, name='hub-login-console-drain')
        try:
            self._reader.start()
            self.backend.resume()
        except BaseException:
            try:
                self.backend.terminate()
            finally:
                try:
                    self.backend.close_console()
                finally:
                    if self._reader.ident is not None:
                        self._reader.join(timeout=3)
                    self.backend.close_handles()
            raise

    def _drain(self):
        try:
            while True:
                part = self.backend.read(4096)
                if not part:
                    break
                self.stdout.feed(part)
        except OSError:
            pass
        finally:
            self.stdout.finish()

    def poll(self):
        if self.returncode is None and not self._closed:
            try:
                self.returncode = self.backend.poll()
            except OSError:
                return None  # Unknown is not positive exit evidence.
        return self.returncode

    def wait(self, timeout=None):
        self.returncode = self.backend.wait(timeout)
        return self.returncode

    def terminate(self):
        self.backend.terminate()

    kill = terminate

    def close(self):
        with self._close_lock:
            if self._closed:
                return
            try:
                if self.poll() is None:
                    self.backend.terminate()
            finally:
                try:
                    self.backend.close_console()  # Reader must still drain here.
                finally:
                    self._closed = True
                    self._reader.join(timeout=3)
                    try:
                        self.stdin.close()
                    finally:
                        self.stdout.close()
                        self.backend.close_handles()
