#!/usr/bin/env python3
"""Inert fixed-profile bounded Root-side natural-quota descriptor donor.

Source component only: no Root controller, SYSTEM Host replacement, installation,
startup hook, or stale-node cleanup without positive old-incarnation death proof.
All privileged work requires genuine UID/EUID 0 and a fixed Root-owned profile.
"""
import argparse
import array
import ctypes
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import select
import socket
import stat
import struct
import subprocess
import sys
import time

ROLES = (
    'interpreter', 'entry', 'selection', 'selection_directory', 'source',
    'source_directory', 'client', 'client_directory', 'fan_group',
    'gate_socket', 'gate_directory', 'gate_node', 'ack_write', 'manifest',
)
DEFAULT_PROFILE_PATH = '/etc/agent-hub/natural-donor.json'
TARGET_UNIT = 'agent-hub2-host.service'
RELEASE_ROOT = '/usr/local/lib/agent-hub/natural/releases'
APPROVED_INTERPRETERS = frozenset({'/usr/bin/python3.11', '/usr/bin/python3.12'})
SYSTEMCTL_BIN = '/usr/bin/systemctl'
HANDOFF_DEADLINE_NS = 5_000_000_000
MAX_PACKET = 4096
MAX_CONFIG_BYTES = 65536
MAX_SOURCE_BYTES = 262144
MAX_ENTRY_BYTES = 16 * 1024 * 1024
FAN_CLOEXEC = 0x00000001
FAN_NONBLOCK = 0x00000002
FAN_MARK_ADD = 0x00000001
FAN_OPEN = 0x00000020
ACK_BYTES = b'ACK\n'


class DonorError(PermissionError):
    """Fail-closed error for Root natural-quota descriptor donation."""


def _require(cond, reason='donor_rejected'):
    if not cond:
        raise DonorError(reason)


def _unique_json_pairs(items):
    result = {}
    for key, value in items:
        _require(key not in result, 'duplicate_json_key')
        result[key] = value
    return result


def _parse_json_strict(raw):
    return json.loads(
        raw,
        object_pairs_hook=_unique_json_pairs,
        parse_constant=lambda _: _require(False, 'invalid_json_constant'),
    )


def _close_once(fds):
    """Remove ownership before close; an uncertain close is never retried."""
    clean = True
    while fds:
        fd = fds.pop()
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                clean = False
    return clean


def _walk_dir(path_str, target_uid, *, require_mode=None, root_only=False):
    path = Path(path_str)
    _require(path.is_absolute() and '..' not in path.parts, 'invalid_directory')
    allowed_uids = (0,) if root_only else (0, target_uid)
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        root_info = os.fstat(fd)
        _require(root_info.st_uid == 0 and not (root_info.st_mode & 0o022), 'untrusted_root_directory')
        for part in path.parts[1:]:
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            os.close(fd)
            fd = nxt
            info = os.fstat(fd)
            _require(
                info.st_uid in allowed_uids and not (info.st_mode & (0o022 if root_only else 0o002)),
                'untrusted_directory',
            )
        info = os.fstat(fd)
        if require_mode is not None:
            _require(
                info.st_uid in allowed_uids and stat.S_IMODE(info.st_mode) == require_mode,
                'untrusted_private_directory',
            )
        return fd
    except BaseException:
        os.close(fd)
        raise


def _read_root_json(path_str, *, optional=False):
    p = Path(path_str)
    _require(p.is_absolute() and '..' not in p.parts and p.name not in ('', '.', '..'), 'invalid_root_file')
    dir_fd = _walk_dir(str(p.parent), 0, root_only=True)
    fd = -1
    try:
        try:
            fd = os.open(p.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dir_fd)
        except FileNotFoundError:
            if optional:
                return None
            raise DonorError('missing_root_file')
        st = os.fstat(fd)
        _require(
            stat.S_ISREG(st.st_mode) and st.st_uid == 0 and st.st_nlink == 1
            and not (st.st_mode & 0o022) and 0 < st.st_size <= MAX_CONFIG_BYTES,
            'untrusted_root_file',
        )
        raw = os.pread(fd, st.st_size + 1, 0)
        _require(len(raw) == st.st_size, 'root_file_size_mismatch')
        st2 = os.stat(p.name, dir_fd=dir_fd, follow_symlinks=False)
        _require(
            (st2.st_dev, st2.st_ino, st2.st_size, st2.st_mtime_ns)
            == (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns),
            'root_file_replaced',
        )
        data = _parse_json_strict(raw.decode('utf-8'))
        _require(isinstance(data, dict), 'invalid_root_json_schema')
        return data
    finally:
        if fd >= 0:
            os.close(fd)
        os.close(dir_fd)


def _write_root_json_atomic(path_str, payload):
    p = Path(path_str)
    _require(p.is_absolute() and '..' not in p.parts and p.name not in ('', '.', '..'), 'invalid_ledger_path')
    dir_fd = _walk_dir(str(p.parent), 0, root_only=True)
    tmp_name = f'.{p.name}.tmp.{os.getpid()}'
    raw = (json.dumps(payload, sort_keys=True, separators=(',', ':')) + '\n').encode('utf-8')
    fd = -1
    try:
        fd = os.open(tmp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=dir_fd)
        written = 0
        while written < len(raw):
            n = os.write(fd, raw[written:])
            _require(n > 0, 'ledger_write_failed')
            written += n
        os.fsync(fd)
        os.close(fd)
        fd = -1
        os.rename(tmp_name, p.name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        os.fsync(dir_fd)
    except BaseException:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            os.unlink(tmp_name, dir_fd=dir_fd)
        except OSError:
            pass
        raise
    finally:
        os.close(dir_fd)


def load_fixed_profile(profile_path=DEFAULT_PROFILE_PATH):
    """Load and validate the fixed Root-owned donor profile."""
    cfg = _read_root_json(profile_path, optional=False)
    required = {
        'v', 'target_user', 'unit', 'python_bin', 'release_commit',
        'entry_archive', 'entry_sha256', 'source_directory', 'selection_path',
        'broker_directory', 'gate_directory', 'startup_socket_path', 'ledger_path',
    }
    _require(required <= set(cfg.keys()), 'incomplete_donor_profile')
    _require(type(cfg['v']) is int and cfg['v'] == 1, 'unsupported_profile_version')
    _require(type(cfg['target_user']) is str and cfg['target_user'] and '@' not in cfg['target_user'], 'invalid_target_user')
    _require(cfg['unit'] == TARGET_UNIT, 'invalid_target_unit')
    _require(cfg['python_bin'] in APPROVED_INTERPRETERS, 'unapproved_interpreter')
    _require(type(cfg['release_commit']) is str and re.fullmatch(r'[0-9a-f]{40}', cfg['release_commit']), 'invalid_release_commit')
    expected_entry = f"{RELEASE_ROOT}/{cfg['release_commit']}/host.pyz"
    _require(cfg['entry_archive'] == expected_entry, 'unapproved_entry_archive')
    _require(type(cfg['entry_sha256']) is str and re.fullmatch(r'[0-9a-f]{64}', cfg['entry_sha256']), 'invalid_entry_sha256')
    for key in ('source_directory', 'selection_path', 'broker_directory', 'gate_directory', 'startup_socket_path', 'ledger_path'):
        val = cfg[key]
        _require(type(val) is str and Path(val).is_absolute() and '..' not in Path(val).parts, f'invalid_{key}')
    return cfg


def pin_interpreter(python_bin):
    _require(python_bin in APPROVED_INTERPRETERS, 'unapproved_interpreter')
    fd = os.open(python_bin, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        _require(
            stat.S_ISREG(info.st_mode) and info.st_uid == 0 and info.st_nlink == 1
            and not (info.st_mode & 0o022) and bool(info.st_mode & 0o111) and info.st_size > 4,
            'untrusted_interpreter',
        )
        _require(os.pread(fd, 4, 0) == b'\x7fELF', 'non_elf_interpreter')
        hasher = hashlib.sha256()
        total = offset = 0
        while True:
            chunk = os.pread(fd, 65536, offset)
            if not chunk:
                break
            hasher.update(chunk)
            total += len(chunk)
            offset += len(chunk)
        _require(total == info.st_size, 'interpreter_size_mismatch')
        return fd, {
            'path': python_bin, 'dev': info.st_dev, 'ino': info.st_ino,
            'size': info.st_size, 'mtime_ns': info.st_mtime_ns,
            'ctime_ns': info.st_ctime_ns, 'sha256': hasher.hexdigest(),
        }
    except BaseException:
        os.close(fd)
        raise


def pin_entry_archive(entry_path, expected_sha256):
    p = Path(entry_path)
    _require(p.is_absolute() and '..' not in p.parts and p.name not in ('', '.', '..'), 'invalid_entry_archive')
    dir_fd = _walk_dir(str(p.parent), 0, root_only=True)
    fd = -1
    try:
        fd = os.open(p.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dir_fd)
        info = os.fstat(fd)
        _require(
            stat.S_ISREG(info.st_mode) and info.st_uid == 0 and info.st_nlink == 1
            and not (info.st_mode & 0o022) and 0 < info.st_size <= MAX_ENTRY_BYTES,
            'untrusted_entry_archive',
        )
        hasher = hashlib.sha256()
        total = offset = 0
        while True:
            chunk = os.pread(fd, 65536, offset)
            if not chunk:
                break
            hasher.update(chunk)
            total += len(chunk)
            offset += len(chunk)
        _require(total == info.st_size, 'entry_archive_size_mismatch')
        digest = hasher.hexdigest()
        _require(digest == expected_sha256, 'entry_archive_sha256_mismatch')
        return fd, {
            'path': entry_path, 'dev': info.st_dev, 'ino': info.st_ino,
            'size': info.st_size, 'mtime_ns': info.st_mtime_ns,
            'ctime_ns': info.st_ctime_ns, 'sha256': digest,
        }
    except BaseException:
        if fd >= 0:
            os.close(fd)
        raise
    finally:
        os.close(dir_fd)


def pin_root_source(dir_path, name):
    _require('/' not in name and name not in ('', '.', '..'), 'invalid_source_name')
    dir_fd = _walk_dir(dir_path, 0, root_only=True)
    fd = -1
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dir_fd)
        info = os.fstat(fd)
        _require(
            stat.S_ISREG(info.st_mode) and info.st_uid == 0 and info.st_nlink == 1
            and not (info.st_mode & 0o222) and 0 < info.st_size <= MAX_SOURCE_BYTES,
            'untrusted_source',
        )
        raw = os.pread(fd, info.st_size + 1, 0)
        _require(len(raw) == info.st_size, 'source_size_mismatch')
        return fd, dir_fd, {
            'name': name, 'dev': info.st_dev, 'ino': info.st_ino,
            'mtime_ns': info.st_mtime_ns, 'ctime_ns': info.st_ctime_ns,
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
    _require(group >= 0, 'fanotify_unavailable')
    try:
        rc = libc.fanotify_mark(group, FAN_MARK_ADD, FAN_OPEN, quota_dir_fd, quota_name.encode())
        _require(rc >= 0, 'fanotify_mark_failed')
        return group
    except BaseException:
        os.close(group)
        raise


def bind_source_gate(gate_dir, target_uid):
    dir_fd = _walk_dir(gate_dir, target_uid, require_mode=0o700)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    node_fd = -1
    node_identity = None
    try:
        # Refuse if a pre-existing/foreign gate node is already present.
        try:
            os.stat('source-gate.sock', dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise DonorError('existing_gate_node_preserved')
        sock.bind(f'/proc/self/fd/{dir_fd}/source-gate.sock')
        node_fd = os.open('source-gate.sock', os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dir_fd)
        st = os.fstat(node_fd)
        _require(
            stat.S_ISSOCK(st.st_mode) and st.st_uid == os.geteuid()
            and st.st_nlink == 1 and not (st.st_mode & 0o7000),
            'untrusted_gate_socket',
        )
        node_identity = (st.st_dev, st.st_ino)
        os.chmod(f'/proc/self/fd/{node_fd}', 0o600)
        os.chown(f'/proc/self/fd/{node_fd}', target_uid, -1)
        st2 = os.fstat(node_fd)
        st_named = os.stat('source-gate.sock', dir_fd=dir_fd, follow_symlinks=False)
        _require(
            (st2.st_dev, st2.st_ino) == node_identity
            and (st_named.st_dev, st_named.st_ino) == node_identity
            and stat.S_IMODE(st2.st_mode) == 0o600 and st2.st_uid == target_uid,
            'gate_socket_race',
        )
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


def _remaining_secs(deadline_ns, monotonic_ns):
    remaining = (deadline_ns - monotonic_ns()) / 1_000_000_000
    _require(remaining > 0, 'handoff_deadline_expired')
    return remaining


def _default_proc_reader(pid):
    stat_raw = Path(f'/proc/{pid}/stat').read_text(encoding='ascii')
    status_raw = Path(f'/proc/{pid}/status').read_text(encoding='ascii')
    return stat_raw, status_raw


def _default_pidfd_opener(pid):
    if hasattr(os, 'pidfd_open'):
        return os.pidfd_open(pid, 0)
    return -1


def query_user_main_pid(target_user, target_uid, deadline_ns, *, runner=subprocess.run,
                        proc_reader=_default_proc_reader, pidfd_opener=_default_pidfd_opener,
                        monotonic_ns=time.monotonic_ns):
    """Query current USER service MainPID and bind process identity within remaining budget."""
    timeout_s = _remaining_secs(deadline_ns, monotonic_ns)
    res = runner(
        [
            SYSTEMCTL_BIN, '--user', f'--machine={target_user}@.host', 'show', TARGET_UNIT,
            '-p', 'MainPID', '-p', 'ActiveState', '-p', 'SubState',
            '-p', 'InvocationID', '-p', 'ExecMainStartTimestampMonotonic',
        ],
        env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'},
        shell=False,
        capture_output=True,
        text=True,
        timeout=timeout_s,
        check=False,
    )
    _require(res.returncode == 0, 'systemctl_show_failed')
    props = {}
    for line in res.stdout.splitlines():
        if '=' in line:
            k, v = line.split('=', 1)
            props[k] = v.strip()
    _require(props.get('ActiveState') == 'active' and props.get('SubState') == 'running', 'user_unit_not_running')
    _require(props.get('InvocationID'), 'missing_invocation_id')
    try:
        main_pid = int(props.get('MainPID', '0'))
        start_mono = int(props.get('ExecMainStartTimestampMonotonic', '0'))
    except ValueError:
        raise DonorError('invalid_unit_pid_or_timestamp')
    _require(main_pid > 1 and start_mono > 0, 'invalid_main_pid')

    pidfd = pidfd_opener(main_pid)
    try:
        stat_raw, status_raw = proc_reader(main_pid)
        after_comm = stat_raw.rsplit(')', 1)[1].split()
        starttime_ticks = int(after_comm[19])
        _require(starttime_ticks > 0, 'invalid_proc_starttime')
        uid_line = next((line for line in status_raw.splitlines() if line.startswith('Uid:')), '')
        uids = [int(x) for x in uid_line.split()[1:5]]
        _require(len(uids) == 4 and all(u == target_uid for u in uids), 'proc_uid_mismatch')
        return {
            'main_pid': main_pid,
            'invocation_id': props['InvocationID'],
            'exec_start_mono': start_mono,
            'proc_starttime_ticks': starttime_ticks,
            'pidfd': pidfd,
        }
    except BaseException:
        if pidfd >= 0:
            try:
                os.close(pidfd)
            except OSError:
                pass
        raise


def _wait_fd(fd_or_sock, deadline_ns, monotonic_ns, *, write=False):
    while True:
        remaining = _remaining_secs(deadline_ns, monotonic_ns)
        try:
            ready = select.select([] if write else [fd_or_sock], [fd_or_sock] if write else [], [], remaining)
        except InterruptedError:
            continue
        _require(bool(ready[1] if write else ready[0]), 'handoff_wait_timeout')
        _remaining_secs(deadline_ns, monotonic_ns)
        return


def _peer_cred(sock):
    pid, uid, gid = struct.unpack('3i', sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i')))
    return pid, uid, gid


def _connect_user_endpoint(socket_path, target_uid):
    p = Path(socket_path)
    _require(p.is_absolute() and '..' not in p.parts and p.name not in ('', '.', '..'), 'invalid_startup_socket')
    dir_fd = _walk_dir(str(p.parent), target_uid)
    try:
        st = os.stat(p.name, dir_fd=dir_fd, follow_symlinks=False)
        _require(stat.S_ISSOCK(st.st_mode) and st.st_uid == target_uid, 'untrusted_startup_socket')
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET | getattr(socket, 'SOCK_CLOEXEC', 0))
        try:
            sock.connect(f'/proc/self/fd/{dir_fd}/{p.name}')
            sock.setblocking(False)
            return sock
        except BaseException:
            sock.close()
            raise
    finally:
        os.close(dir_fd)


def _validate_request(raw, target_uid, deadline_ns, prior_ledger, monotonic_ns):
    data = _parse_json_strict(raw.decode('utf-8'))
    _require(
        type(data) is dict
        and set(data.keys()) == {'v', 'kind', 'nonce', 'target_uid', 'deadline_ns', 'slots', 'roles'},
        'invalid_request_schema',
    )
    _require(type(data['v']) is int and data['v'] == 1, 'invalid_request_version')
    _require(data['kind'] == 'natural_donation_request_v1', 'invalid_request_kind')
    nonce = data['nonce']
    _require(type(nonce) is str and re.fullmatch(r'[0-9a-f]{64}', nonce), 'invalid_request_nonce')
    if prior_ledger is not None and prior_ledger.get('nonce') == nonce:
        raise DonorError('replayed_nonce')
    _require(type(data['target_uid']) is int and data['target_uid'] == target_uid, 'request_uid_mismatch')
    req_deadline = data['deadline_ns']
    now = monotonic_ns()
    _require(type(req_deadline) is int and now < req_deadline <= deadline_ns, 'invalid_request_deadline')
    _require(data['roles'] == list(ROLES), 'invalid_request_roles')
    slots = data['slots']
    _require(
        isinstance(slots, list) and len(slots) == len(ROLES)
        and all(type(s) is int and s > 2 for s in slots)
        and len(set(slots)) == len(slots),
        'invalid_request_slots',
    )
    return nonce, req_deadline, dict(zip(ROLES, slots))


def donate_from_fixed_profile(
    *,
    profile_path=DEFAULT_PROFILE_PATH,
    uid_getter=os.getuid,
    euid_getter=os.geteuid,
    pw_lookup=pwd.getpwnam,
    runner=subprocess.run,
    proc_reader=_default_proc_reader,
    pidfd_opener=_default_pidfd_opener,
    monotonic_ns=time.monotonic_ns,
    endpoint_connector=_connect_user_endpoint,
    peer_cred_getter=_peer_cred,
    fanotify_opener=open_fanotify_group,
    gate_binder=bind_source_gate,
    ledger_reader=_read_root_json,
    ledger_writer=_write_root_json_atomic,
):
    """Execute one bounded Root descriptor donation against a fixed Root profile."""
    _require(uid_getter() == 0 and euid_getter() == 0, 'root_entry_required')
    deadline_ns = monotonic_ns() + HANDOFF_DEADLINE_NS
    profile = load_fixed_profile(profile_path)
    pw = pw_lookup(profile['target_user'])
    target_uid = pw.pw_uid
    _require(target_uid > 0 and pw.pw_gid > 0 and pw.pw_name == profile['target_user'], 'non_root_target_required')

    prior_ledger = ledger_reader(profile['ledger_path'], optional=True)
    if prior_ledger is not None and prior_ledger.get('state') in ('rights_may_have_transferred', 'unknown'):
        raise DonorError('prior_custody_unknown')

    incarnation = query_user_main_pid(
        profile['target_user'], target_uid, deadline_ns,
        runner=runner, proc_reader=proc_reader, pidfd_opener=pidfd_opener, monotonic_ns=monotonic_ns,
    )
    pidfd = incarnation.pop('pidfd')
    sock = None
    owned_fds = []
    gate_sock = None
    gate_dir_fd = -1
    gate_node_fd = -1
    gate_node_identity = None
    ack_r = ack_w = manifest_r = manifest_w = -1
    rights_may_have_transferred = False
    try:
        sock = endpoint_connector(profile['startup_socket_path'], target_uid)
        peer_pid, peer_uid, _ = peer_cred_getter(sock)
        _require(peer_uid == target_uid and peer_pid == incarnation['main_pid'], 'peer_identity_mismatch')

        _wait_fd(sock, deadline_ns, monotonic_ns)
        req_bytes, _anc, flags, _addr = sock.recvmsg(MAX_PACKET, 0, getattr(socket, 'MSG_DONTWAIT', 0))
        _require(not (flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC)) and bool(req_bytes), 'invalid_request_packet')
        nonce, effective_deadline_ns, slot_for = _validate_request(
            req_bytes, target_uid, deadline_ns, prior_ledger, monotonic_ns,
        )

        interp_fd, interp_pin = pin_interpreter(profile['python_bin'])
        owned_fds.append(interp_fd)
        entry_fd, entry_pin = pin_entry_archive(profile['entry_archive'], profile['entry_sha256'])
        owned_fds.append(entry_fd)
        sel_path = Path(profile['selection_path'])
        sel_fd, sel_dir_fd, sel_pin = pin_root_source(str(sel_path.parent), sel_path.name)
        owned_fds.extend((sel_fd, sel_dir_fd))
        src_fd, src_dir_fd, src_pin = pin_root_source(profile['source_directory'], 'hub-quota-publish.py')
        owned_fds.extend((src_fd, src_dir_fd))
        cli_fd, cli_dir_fd, cli_pin = pin_root_source(profile['source_directory'], 'hub_alert_client.py')
        owned_fds.extend((cli_fd, cli_dir_fd))
        fan_group = fanotify_opener(src_dir_fd, 'hub-quota-publish.py')
        owned_fds.append(fan_group)

        gate_sock, gate_dir_fd, gate_node_fd, gate_node_identity = gate_binder(profile['gate_directory'], target_uid)
        ack_r, ack_w = os.pipe2(os.O_CLOEXEC)
        manifest_r, manifest_w = os.pipe2(os.O_CLOEXEC)

        manifest = json.dumps({
            'v': 2,
            'kind': 'natural_quota_root_donation_v1',
            'target_uid': target_uid,
            'handoff_deadline_ns': effective_deadline_ns,
            'ack_fd': slot_for['ack_write'],
            'source_directory': profile['source_directory'],
            'selection_path': profile['selection_path'],
            'broker_directory': profile['broker_directory'],
            'gate_directory': profile['gate_directory'],
            'interpreter': {'fd': slot_for['interpreter'], **interp_pin},
            'entry': {'fd': slot_for['entry'], **entry_pin},
            'selection': {'fd': slot_for['selection'], 'dir_fd': slot_for['selection_directory'], **sel_pin},
            'source': {'fd': slot_for['source'], 'dir_fd': slot_for['source_directory'], **src_pin},
            'client': {'fd': slot_for['client'], 'dir_fd': slot_for['client_directory'], **cli_pin},
            'fan_group': slot_for['fan_group'],
            'gate_sock_fd': slot_for['gate_socket'],
            'gate_dir_fd': slot_for['gate_directory'],
            'gate_node_fd': slot_for['gate_node'],
            'gate_node': {'dev': gate_node_identity[0], 'ino': gate_node_identity[1]},
        }, separators=(',', ':')).encode('utf-8')
        _require(len(manifest) <= MAX_PACKET and os.write(manifest_w, manifest) == len(manifest), 'manifest_write_failed')
        consumed_w, manifest_w = manifest_w, -1
        os.close(consumed_w)

        donor_fds = [
            interp_fd, entry_fd, sel_fd, sel_dir_fd, src_fd, src_dir_fd,
            cli_fd, cli_dir_fd, fan_group, gate_sock.fileno(), gate_dir_fd,
            gate_node_fd, ack_w, manifest_r,
        ]
        reply = json.dumps({
            'v': 1,
            'kind': 'natural_donation_rights_v1',
            'nonce': nonce,
            'target_uid': target_uid,
            'deadline_ns': effective_deadline_ns,
            'roles': list(ROLES),
        }, separators=(',', ':')).encode('utf-8')
        _require(len(reply) <= MAX_PACKET, 'reply_too_large')
        _remaining_secs(effective_deadline_ns, monotonic_ns)

        # Record durable unknown-custody intent BEFORE sendmsg so any partial
        # send, missing ACK, or exception preserves the adopted/unknown gate.
        custody_record = {
            'v': 1,
            'state': 'rights_may_have_transferred',
            'nonce': nonce,
            'target_uid': target_uid,
            'main_pid': incarnation['main_pid'],
            'invocation_id': incarnation['invocation_id'],
            'gate_node': {'dev': gate_node_identity[0], 'ino': gate_node_identity[1]},
            'deadline_ns': effective_deadline_ns,
        }
        ledger_writer(profile['ledger_path'], custody_record)
        rights_may_have_transferred = True

        _wait_fd(sock, effective_deadline_ns, monotonic_ns, write=True)
        sent = sock.sendmsg(
            [reply],
            [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array('i', donor_fds))],
        )
        _require(sent == len(reply), 'partial_rights_send')

        # Drop donor-side copies of transferred FDs; keep only ack_r.
        _close_once([ack_w, manifest_r, gate_node_fd, gate_dir_fd])
        ack_w = manifest_r = gate_node_fd = gate_dir_fd = -1
        gate_sock.close()
        gate_sock = None
        _close_once(owned_fds)

        ack_buf = b''
        while len(ack_buf) < len(ACK_BYTES):
            _wait_fd(ack_r, effective_deadline_ns, monotonic_ns)
            chunk = os.read(ack_r, len(ACK_BYTES) - len(ack_buf))
            _require(bool(chunk), 'ack_eof_before_confirmation')
            ack_buf += chunk
        _require(ack_buf == ACK_BYTES, 'invalid_ack_payload')
        custody_record['state'] = 'adopted'
        ledger_writer(profile['ledger_path'], custody_record)
        return {
            'status': 'DONATED',
            'nonce': nonce,
            'main_pid': incarnation['main_pid'],
            'gate_node': custody_record['gate_node'],
        }
    finally:
        if pidfd >= 0:
            try:
                os.close(pidfd)
            except OSError:
                pass
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        if gate_sock is not None:
            try:
                gate_sock.close()
            except OSError:
                pass
        if not rights_may_have_transferred and gate_node_identity is not None and gate_dir_fd >= 0:
            try:
                cur = os.stat('source-gate.sock', dir_fd=gate_dir_fd, follow_symlinks=False)
                if (cur.st_dev, cur.st_ino) == gate_node_identity:
                    os.unlink('source-gate.sock', dir_fd=gate_dir_fd)
            except OSError:
                pass
        _close_once([ack_r, ack_w, manifest_r, manifest_w, gate_node_fd, gate_dir_fd])
        _close_once(owned_fds)


def main(argv=None):
    if not argv:
        print(json.dumps({'status': 'INERT_ROOT_DONOR', 'authority': 'NONE', 'complete': False}))
        return 0
    parser = argparse.ArgumentParser(prog='hub-alert-natural-donor')
    parser.add_argument('--donate', action='store_true', required=True)
    parser.parse_args(argv)
    result = donate_from_fixed_profile()
    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
