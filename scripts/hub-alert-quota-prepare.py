"""Inert Root pre-drop preparer for same-process quota dispatch (stdlib-only).

Requires genuine Root entry, binds source-gate.sock using O_PATH+O_NOFOLLOW
FD-pinned node operations (no mutable basename chmod/chown), pins the full
approved ELF interpreter binary, irrevocably clears UIDs/GIDs/groups/caps/
ambient/keepcaps and enforces PR_SET_NO_NEW_PRIVS before handing off owned FDs
via pass_fds + read-only pipe to the target USER dispatcher.
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
import time

FAN_CLOEXEC = 0x00000001
FAN_NONBLOCK = 0x00000002
FAN_MARK_ADD = 0x00000001
FAN_OPEN = 0x00000020
MAX_SOURCE_BYTES = 262144
MAX_ENTRY_BYTES = 16 * 1024 * 1024
DEFAULT_ENTRY_ARCHIVE = '/usr/local/lib/agent-hub/agent-hub-host.pyz'
HANDOFF_DEADLINE_NS = 5_000_000_000
APPROVED_INTERPRETERS = frozenset({
    '/usr/bin/python3',
    '/usr/bin/python3.12',
    '/usr/bin/python3.11',
})
PR_SET_KEEPCAPS = 8
PR_SET_NO_NEW_PRIVS = 38
PR_GET_NO_NEW_PRIVS = 39
PR_CAP_AMBIENT = 47
PR_CAP_AMBIENT_CLEAR_ALL = 4


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
    """Pin an approved full ELF interpreter binary without size cap or truncation."""
    if python_bin not in APPROVED_INTERPRETERS:
        raise PermissionError('unapproved_interpreter')
    fd = os.open(python_bin, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0
                or info.st_nlink != 1 or (info.st_mode & 0o022)
                or not (info.st_mode & 0o111) or info.st_size <= 4):
            raise PermissionError('untrusted_interpreter')
        if os.pread(fd, 4, 0) != b'\x7fELF':
            raise PermissionError('non_elf_interpreter')
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
            'path': python_bin,
            'dev': info.st_dev, 'ino': info.st_ino, 'size': info.st_size,
            'mtime_ns': info.st_mtime_ns, 'ctime_ns': info.st_ctime_ns,
            'sha256': hasher.hexdigest(),
        }
    except BaseException:
        os.close(fd)
        raise


def pin_entry_archive(entry_path):
    """Open and pin a Root-owned non-writable maintained Host entry archive."""
    if not entry_path or not Path(entry_path).is_absolute() or '..' in Path(entry_path).parts:
        raise PermissionError('invalid_entry_archive')
    fd = os.open(entry_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0
                or info.st_nlink != 1 or (info.st_mode & 0o022)
                or not 0 < info.st_size <= MAX_ENTRY_BYTES):
            raise PermissionError('untrusted_entry_archive')
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
            raise PermissionError('entry_archive_size_mismatch')
        return fd, {
            'path': entry_path,
            'dev': info.st_dev, 'ino': info.st_ino, 'size': info.st_size,
            'mtime_ns': info.st_mtime_ns, 'ctime_ns': info.st_ctime_ns,
            'sha256': hasher.hexdigest(),
        }
    except BaseException:
        os.close(fd)
        raise


def validate_user_runtime(target_uid):
    """Validate and derive target USER manager XDG_RUNTIME_DIR and D-Bus address."""
    runtime_dir = f'/run/user/{target_uid}'
    rt_fd = _walk_dir(runtime_dir, target_uid, require_mode=0o700)
    try:
        rt_stat = os.fstat(rt_fd)
        if rt_stat.st_uid != target_uid:
            raise PermissionError('untrusted_user_runtime')
        bus_stat = os.stat('bus', dir_fd=rt_fd, follow_symlinks=False)
        if not stat.S_ISSOCK(bus_stat.st_mode) or bus_stat.st_uid != target_uid:
            raise PermissionError('untrusted_user_bus')
        return {
            'XDG_RUNTIME_DIR': runtime_dir,
            'DBUS_SESSION_BUS_ADDRESS': f'unix:path={runtime_dir}/bus',
        }
    finally:
        os.close(rt_fd)


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
    """Bind source-gate.sock and apply FD-pinned metadata via O_PATH+O_NOFOLLOW."""
    dir_fd = _walk_dir(gate_dir, target_uid, require_mode=0o700)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    node_fd = -1
    node_identity = None
    try:
        sock.bind(f'/proc/self/fd/{dir_fd}/source-gate.sock')
        node_fd = os.open('source-gate.sock', os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dir_fd)
        st = os.fstat(node_fd)
        if not stat.S_ISSOCK(st.st_mode) or st.st_uid != os.geteuid() or st.st_nlink != 1 or (st.st_mode & 0o7000):
            raise PermissionError('untrusted_gate_socket')
        node_identity = (st.st_dev, st.st_ino)
        os.chmod(f'/proc/self/fd/{node_fd}', 0o600)
        os.chown(f'/proc/self/fd/{node_fd}', target_uid, -1)
        st2 = os.fstat(node_fd)
        st_named = os.stat('source-gate.sock', dir_fd=dir_fd, follow_symlinks=False)
        if ((st2.st_dev, st2.st_ino) != node_identity
                or (st_named.st_dev, st_named.st_ino) != node_identity
                or stat.S_IMODE(st2.st_mode) != 0o600 or st2.st_uid != target_uid):
            raise PermissionError('gate_socket_race')
        sock.setblocking(False)
        sock.listen(1)
        return sock, dir_fd, node_fd, node_identity
    except BaseException:
        sock.close()
        if node_identity is not None:
            try:
                cur = os.stat('source-gate.sock', dir_fd=dir_fd, follow_symlinks=False)
                if (cur.st_dev, cur.st_ino) == node_identity:
                    os.unlink('source-gate.sock', dir_fd=dir_fd)
            except OSError:
                pass
        if node_fd >= 0:
            os.close(node_fd)
        os.close(dir_fd)
        raise


def _verify_proc_dropped():
    status = Path('/proc/self/status').read_text(encoding='ascii')
    fields = {}
    for line in status.splitlines():
        if ':' in line:
            k, v = line.split(':', 1)
            fields[k.strip()] = v.strip()
    if fields.get('NoNewPrivs') != '1':
        raise PermissionError('no_new_privs_failed')
    for cap_key in ('CapInh', 'CapPrm', 'CapEff', 'CapAmb'):
        val = fields.get(cap_key)
        if val is None or int(val, 16) != 0:
            raise PermissionError('capabilities_retained')


def drop_privileges(target_uid, target_gid):
    """Require genuine Root entry, then irrevocably clear IDs, groups, caps, and set no_new_privs."""
    if os.getuid() != 0 or os.geteuid() != 0:
        raise PermissionError('root_entry_required')
    if target_uid <= 0 or target_gid <= 0:
        raise PermissionError('non_root_target_required')
    libc = ctypes.CDLL('libc.so.6', use_errno=True)
    libc.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
    libc.prctl.restype = ctypes.c_int
    if libc.prctl(PR_SET_KEEPCAPS, 0, 0, 0, 0) != 0:
        raise PermissionError('keepcaps_clear_failed')
    if libc.prctl(PR_CAP_AMBIENT, PR_CAP_AMBIENT_CLEAR_ALL, 0, 0, 0) != 0:
        raise PermissionError('ambient_cap_clear_failed')
    os.setgroups([])
    if os.getgroups() != []:
        raise PermissionError('groups_clear_failed')
    os.setresgid(target_gid, target_gid, target_gid)
    if os.getresgid() != (target_gid, target_gid, target_gid):
        raise PermissionError('gid_drop_failed')
    os.setresuid(target_uid, target_uid, target_uid)
    if os.getresuid() != (target_uid, target_uid, target_uid):
        raise PermissionError('uid_drop_failed')
    if libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        raise PermissionError('no_new_privs_set_failed')
    if libc.prctl(PR_GET_NO_NEW_PRIVS, 0, 0, 0, 0) != 1:
        raise PermissionError('no_new_privs_verify_failed')
    _verify_proc_dropped()


def prepare_and_handoff(args, *, runner=subprocess.run):
    if os.getuid() != 0 or os.geteuid() != 0:
        raise PermissionError('root_entry_required')
    entry = pwd.getpwuid(args.target_uid)
    target_uid, target_gid = entry.pw_uid, entry.pw_gid
    if target_uid <= 0 or target_gid <= 0:
        raise PermissionError('non_root_target_required')

    owned_fds = []
    gate_sock = None
    gate_dir_fd = -1
    gate_node_fd = -1
    gate_node_identity = None
    pipe_r = pipe_w = -1
    ack_r = ack_w = -1
    handed_off = False
    try:
        interp_fd, interp_pin = pin_interpreter(args.python_bin)
        owned_fds.append(interp_fd)
        entry_fd, entry_pin = pin_entry_archive(args.entry_archive)
        owned_fds.append(entry_fd)
        q_fd, q_dir_fd, q_pin = pin_source(args.source_dir, 'hub-quota-publish.py', target_uid)
        owned_fds.extend((q_fd, q_dir_fd))
        c_fd, c_dir_fd, c_pin = pin_source(args.source_dir, 'hub_alert_client.py', target_uid)
        owned_fds.extend((c_fd, c_dir_fd))
        p_fd, p_dir_fd, p_pin = pin_source(args.source_dir, 'hub-alert-quota-dispatch.py', target_uid)
        owned_fds.extend((p_fd, p_dir_fd))

        fan_group = open_fanotify_group(q_dir_fd, 'hub-quota-publish.py')
        owned_fds.append(fan_group)
        gate_sock, gate_dir_fd, gate_node_fd, gate_node_identity = bind_source_gate(args.gate_dir, target_uid)
        owned_fds.append(gate_dir_fd)
        user_env = validate_user_runtime(target_uid)

        pipe_r, pipe_w = os.pipe()
        ack_r, ack_w = os.pipe()
        manifest = json.dumps({
            'v': 1,
            'target_uid': target_uid,
            'handoff_deadline_ns': time.monotonic_ns() + HANDOFF_DEADLINE_NS,
            'ack_fd': ack_w,
            'interpreter': {'fd': interp_fd, **interp_pin},
            'entry': {'fd': entry_fd, **entry_pin},
            'source': {'fd': q_fd, 'dir_fd': q_dir_fd, **q_pin},
            'client': {'fd': c_fd, 'dir_fd': c_dir_fd, **c_pin},
            'prelude': {'fd': p_fd, 'dir_fd': p_dir_fd, **p_pin},
            'fan_group': fan_group,
            'gate_sock_fd': gate_sock.fileno(),
            'gate_dir_fd': gate_dir_fd,
            'gate_node_fd': gate_node_fd,
            'gate_node': {'dev': gate_node_identity[0], 'ino': gate_node_identity[1]},
        }, separators=(',', ':')).encode('utf-8')
        os.write(pipe_w, manifest)
        os.close(pipe_w)
        pipe_w = -1

        handoff_fds = owned_fds + [gate_sock.fileno(), gate_node_fd, pipe_r, ack_w]
        for fd in handoff_fds:
            os.set_inheritable(fd, True)

        drop_privileges(target_uid, target_gid)

        cmd = [
            f'/proc/self/fd/{interp_fd}', '-I', f'/proc/self/fd/{entry_fd}',
            '--config', args.config, 'quota-dispatch', '--enable', '--handoff-fd', str(pipe_r),
        ]
        env = {'PATH': '/usr/bin:/bin', 'HOME': entry.pw_dir, **user_env}
        res = runner(cmd, pass_fds=tuple(handoff_fds), env=env, check=False)
        os.close(ack_w)
        ack_w = -1
        os.set_blocking(ack_r, False)
        try:
            ack_data = os.read(ack_r, 8)
        except OSError:
            ack_data = b''
        handed_off = ack_data == b'ACK\n'
        return res.returncode
    finally:
        for fd in (pipe_w, pipe_r, ack_w, ack_r):
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
        if gate_sock is not None:
            gate_sock.close()
        if not handed_off and gate_node_identity is not None and gate_dir_fd >= 0:
            try:
                cur = os.stat('source-gate.sock', dir_fd=gate_dir_fd, follow_symlinks=False)
                if (cur.st_dev, cur.st_ino) == gate_node_identity:
                    os.unlink('source-gate.sock', dir_fd=gate_dir_fd)
            except OSError:
                pass
        if gate_node_fd >= 0:
            try:
                os.close(gate_node_fd)
            except OSError:
                pass
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
    parser.add_argument('--entry-archive', default=DEFAULT_ENTRY_ARCHIVE)
    args = parser.parse_args(argv)
    return prepare_and_handoff(args)


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
