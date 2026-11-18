"""Inert Root pre-drop preparer for same-process quota dispatch (stdlib-only).

Acquires held source FDs, full actual interpreter binary pin, fanotify group FD,
and source-gate.sock under pre-drop authority, then drops privileges to the
target USER UID/GID and hands off owned FDs via pass_fds + read-only pipe.
Never imports user-owned packages as Root and never trusts client proof dicts.
"""
import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import pwd
import socket
import stat
import subprocess
import sys

FAN_CLOEXEC = 0x00000001
FAN_NONBLOCK = 0x00000002
FAN_MARK_ADD = 0x00000001
FAN_OPEN = 0x00000020
MAX_SOURCE_BYTES = 262144


def _walk_dir(path_str, target_uid, *, require_mode=None):
    path = Path(path_str)
    if not path.is_absolute() or '..' in path.parts:
        raise PermissionError('invalid_directory')
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for part in path.parts[1:]:
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            os.close(fd)
            fd = nxt
            info = os.fstat(fd)
            if info.st_uid not in (0, target_uid) or (info.st_mode & 0o002):
                raise PermissionError('untrusted_directory')
        info = os.fstat(fd)
        if require_mode is not None and (info.st_uid not in (0, target_uid) or stat.S_IMODE(info.st_mode) != require_mode):
            raise PermissionError('untrusted_private_directory')
        return fd
    except BaseException:
        os.close(fd)
        raise


def pin_interpreter(python_bin):
    """Pin the full actual interpreter binary without size cap or truncation."""
    path = Path(python_bin)
    if not path.is_absolute() or '..' in path.parts:
        raise PermissionError('invalid_interpreter')
    fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0
                or info.st_nlink != 1 or (info.st_mode & 0o022) or info.st_size <= 0):
            raise PermissionError('untrusted_interpreter')
        hasher = hashlib.sha256()
        total = 0
        offset = 0
        while True:
            chunk = os.pread(fd, 65536, offset)
            if not chunk:
                break
            hasher.update(chunk)
            total += len(chunk)
            offset += len(chunk)
        if total != info.st_size:
            raise PermissionError('interpreter_size_mismatch')
        return fd, {
            'dev': info.st_dev, 'ino': info.st_ino, 'size': info.st_size,
            'mtime_ns': info.st_mtime_ns, 'ctime_ns': info.st_ctime_ns,
            'sha256': hasher.hexdigest(),
        }
    except BaseException:
        os.close(fd)
        raise


def pin_source(dir_path, name, target_uid):
    """Open and pin a regular non-writable source file via dir_fd + O_NOFOLLOW."""
    if '/' in name or name in ('', '.', '..'):
        raise PermissionError('invalid_source_name')
    dir_fd = _walk_dir(dir_path, target_uid)
    fd = -1
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dir_fd)
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid not in (0, target_uid)
                or info.st_nlink != 1 or (info.st_mode & 0o222)
                or not 0 < info.st_size <= MAX_SOURCE_BYTES):
            raise PermissionError('untrusted_source')
        raw = os.pread(fd, info.st_size + 1, 0)
        if len(raw) != info.st_size:
            raise PermissionError('source_size_mismatch')
        return fd, dir_fd, {
            'name': name, 'dev': info.st_dev, 'ino': info.st_ino,
            'sha256': hashlib.sha256(raw).hexdigest(),
        }
    except BaseException:
        if fd >= 0:
            os.close(fd)
        os.close(dir_fd)
        raise


def open_fanotify_group(quota_dir_fd, quota_name):
    libc = ctypes.CDLL('libc.so.6', use_errno=True)
    libc.fanotify_init.argtypes = [ctypes.c_uint, ctypes.c_uint]
    libc.fanotify_init.restype = ctypes.c_int
    libc.fanotify_mark.argtypes = [ctypes.c_int, ctypes.c_uint, ctypes.c_uint64, ctypes.c_int, ctypes.c_char_p]
    libc.fanotify_mark.restype = ctypes.c_int
    group = libc.fanotify_init(FAN_CLOEXEC | FAN_NONBLOCK, os.O_RDONLY | os.O_CLOEXEC)
    if group < 0:
        raise PermissionError('fanotify_unavailable')
    try:
        rc = libc.fanotify_mark(group, FAN_MARK_ADD, FAN_OPEN, quota_dir_fd, quota_name.encode())
        if rc < 0:
            raise PermissionError('fanotify_mark_failed')
        return group
    except BaseException:
        os.close(group)
        raise


def bind_source_gate(gate_dir, target_uid):
    dir_fd = _walk_dir(gate_dir, target_uid, require_mode=0o700)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    try:
        sock.bind(f'/proc/self/fd/{dir_fd}/source-gate.sock')
        os.chmod(f'/proc/self/fd/{dir_fd}/source-gate.sock', 0o600)
        if os.geteuid() == 0:
            os.chown(f'/proc/self/fd/{dir_fd}/source-gate.sock', target_uid, -1)
        sock.setblocking(False)
        sock.listen(1)
        return sock, dir_fd
    except BaseException:
        sock.close()
        os.close(dir_fd)
        raise


def drop_privileges(target_uid, target_gid):
    if target_uid <= 0 or target_gid <= 0:
        raise PermissionError('non_root_target_required')
    if os.geteuid() == 0:
        os.setgroups([])
        os.setgid(target_gid)
        os.setuid(target_uid)
    if (os.getuid() != target_uid or os.geteuid() != target_uid
            or os.getgid() != target_gid or os.geteuid() != target_uid):
        raise PermissionError('privilege_drop_failed')


def prepare_and_handoff(args, *, runner=subprocess.run):
    entry = pwd.getpwuid(args.target_uid)
    target_uid, target_gid = entry.pw_uid, entry.pw_gid
    if target_uid <= 0 or target_gid <= 0:
        raise PermissionError('non_root_target_required')

    owned_fds = []
    gate_sock = None
    pipe_r = pipe_w = -1
    try:
        interp_fd, interp_pin = pin_interpreter(args.python_bin)
        owned_fds.append(interp_fd)
        q_fd, q_dir_fd, q_pin = pin_source(args.source_dir, 'hub-quota-publish.py', target_uid)
        owned_fds.extend((q_fd, q_dir_fd))
        c_fd, c_dir_fd, c_pin = pin_source(args.source_dir, 'hub_alert_client.py', target_uid)
        owned_fds.extend((c_fd, c_dir_fd))
        p_fd, p_dir_fd, p_pin = pin_source(args.source_dir, 'hub-alert-quota-dispatch.py', target_uid)
        owned_fds.extend((p_fd, p_dir_fd))

        fan_group = open_fanotify_group(q_dir_fd, 'hub-quota-publish.py')
        owned_fds.append(fan_group)
        gate_sock, gate_dir_fd = bind_source_gate(args.gate_dir, target_uid)
        owned_fds.append(gate_dir_fd)

        pipe_r, pipe_w = os.pipe()
        manifest = json.dumps({
            'v': 1,
            'interpreter': {'fd': interp_fd, **interp_pin},
            'source': {'fd': q_fd, 'dir_fd': q_dir_fd, **q_pin},
            'client': {'fd': c_fd, 'dir_fd': c_dir_fd, **c_pin},
            'prelude': {'fd': p_fd, 'dir_fd': p_dir_fd, **p_pin},
            'fan_group': fan_group,
            'gate_sock_fd': gate_sock.fileno(),
            'gate_dir_fd': gate_dir_fd,
        }, separators=(',', ':')).encode('utf-8')
        os.write(pipe_w, manifest)
        os.close(pipe_w)
        pipe_w = -1

        handoff_fds = owned_fds + [gate_sock.fileno(), pipe_r]
        for fd in handoff_fds:
            os.set_inheritable(fd, True)

        drop_privileges(target_uid, target_gid)

        cmd = [
            args.python_bin, '-I', '-S', '-m', 'agent_hub_host.cli',
            '--config', args.config, 'quota-dispatch', '--enable', '--handoff-fd', str(pipe_r),
        ]
        env = {'PATH': '/usr/bin:/bin', 'HOME': entry.pw_dir}
        res = runner(cmd, pass_fds=tuple(handoff_fds), env=env, check=False)
        return res.returncode
    finally:
        if pipe_w >= 0:
            os.close(pipe_w)
        if pipe_r >= 0:
            os.close(pipe_r)
        if gate_sock is not None:
            gate_sock.close()
        for fd in owned_fds:
            try:
                os.close(fd)
            except OSError:
                pass


def main(argv=None):
    if not argv:
        print(json.dumps({'status': 'INERT_ROOT_PREPARER', 'authority': 'NONE', 'complete': False}))
        return 0
    parser = argparse.ArgumentParser(prog='hub-alert-quota-prepare')
    parser.add_argument('--prepare-handoff', action='store_true', required=True)
    parser.add_argument('--target-uid', type=int, required=True)
    parser.add_argument('--config', required=True)
    parser.add_argument('--source-dir', required=True)
    parser.add_argument('--gate-dir', required=True)
    parser.add_argument('--python-bin', default='/usr/bin/python3')
    args = parser.parse_args(argv)
    return prepare_and_handoff(args)


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
