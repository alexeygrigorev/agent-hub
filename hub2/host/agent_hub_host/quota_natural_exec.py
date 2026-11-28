"""Optional Linux USER-side natural-quota startup composition.

Coordinates one bounded Root descriptor donation via a private USER runtime
socket and replaces the current USER Host process via same-PID fexecve with the
validated Root interpreter and release archive. Never executes Root archives,
never reads Root credentials/manifests, and never gates ordinary Host serve.
"""
import os
from pathlib import Path
import re
import secrets
import select
import socket
import stat
import subprocess
import sys
import time

from . import quota_natural_bootstrap as bootstrap
from . import quota_natural_startup as startup
from . import quota_natural_transfer as transfer
from .windows_host import host_release


HELPER_PATH = '/usr/local/lib/agent-hub/natural/hub-alert-natural-donor.py'
SUDO_BIN = '/usr/bin/sudo'
SYSTEMD_RUN_BIN = '/usr/bin/systemd-run'
DONOR_PYTHON_BIN = '/usr/bin/python3.12'
RUNTIME_SUBDIR = 'agent-hub-natural'
UNIT_PREFIX = 'agent-hub-natural-donor'


def _remaining_secs(deadline_ns):
    remaining = (deadline_ns - time.monotonic_ns()) / 1_000_000_000
    transfer._require(remaining > 0)
    return remaining


def _verify_installed_helper(helper_path=HELPER_PATH):
    """Verify the fixed Root donor helper resides in a trusted Root-only graph."""
    path = Path(helper_path)
    transfer._require(path.is_absolute() and '..' not in path.parts
                      and path.name not in ('', '.', '..'))
    nodes, _edges = startup._root_graph([path.parent])
    helper_fd = -1
    clean = True
    try:
        helper_fd = os.open(path.name, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC,
                            dir_fd=nodes[str(path.parent)])
        info = os.fstat(helper_fd)
        transfer._require(stat.S_ISREG(info.st_mode) and info.st_uid == 0
                          and info.st_nlink == 1 and not (info.st_mode & 0o022)
                          and info.st_size > 0)
    finally:
        to_close = [fd for fd in (helper_fd, *nodes.values()) if fd >= 0]
        helper_fd = -1
        nodes.clear()
        clean = transfer._close_owned(to_close)
    transfer._require(clean)


def _step_dir(holder, name):
    """Advance holder[0] with ownership transferred before closing the prior FD."""
    nxt = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=holder[0])
    old, holder[0] = holder[0], nxt
    os.close(old)


def _open_user_runtime_dir(uid, *, runtime_root='/run/user'):
    """Open or create private 0700 /run/user/<uid>/agent-hub-natural safely."""
    root_path = Path(runtime_root)
    transfer._require(root_path.is_absolute() and '..' not in root_path.parts)
    holder = [os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)]
    try:
        info = os.fstat(holder[0])
        transfer._require(info.st_uid == 0 and not (info.st_mode & 0o022))
        for part in root_path.parts[1:]:
            _step_dir(holder, part)
            info = os.fstat(holder[0])
            transfer._require(info.st_uid == 0 and not (info.st_mode & 0o022))
        _step_dir(holder, str(uid))
        info = os.fstat(holder[0])
        transfer._require(info.st_uid in (0, uid) and not (info.st_mode & 0o022))
        try:
            os.mkdir(RUNTIME_SUBDIR, 0o700, dir_fd=holder[0])
        except FileExistsError:
            pass
        _step_dir(holder, RUNTIME_SUBDIR)
        info = os.fstat(holder[0])
        transfer._require(stat.S_ISDIR(info.st_mode) and info.st_uid == uid
                          and stat.S_IMODE(info.st_mode) == 0o700)
        result, holder[0] = holder[0], -1
        return result
    finally:
        if holder[0] >= 0:
            transfer._close_owned(holder)


def _bind_unique_endpoint(nat_dir_fd, sock_name, uid):
    """Bind AF_UNIX SOCK_SEQPACKET without ever unlinking a pre-existing node."""
    try:
        os.stat(sock_name, dir_fd=nat_dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        pass
    else:
        raise transfer.TransferError('natural_donation_transfer_rejected')

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET | getattr(socket, 'SOCK_CLOEXEC', 0))
    owned_identity = None
    try:
        sock.setblocking(False)
        sock.bind(f'/proc/self/fd/{nat_dir_fd}/{sock_name}')
        st = os.stat(sock_name, dir_fd=nat_dir_fd, follow_symlinks=False)
        if stat.S_ISSOCK(st.st_mode) and st.st_uid == uid:
            owned_identity = (st.st_dev, st.st_ino)
        transfer._require(owned_identity is not None)
        sock.listen(1)
        result, sock = sock, None
        return result, owned_identity
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
            _unlink_owned_endpoint(nat_dir_fd, sock_name, owned_identity)


def _unlink_owned_endpoint(nat_dir_fd, sock_name, owned_identity):
    if owned_identity is None or nat_dir_fd < 0:
        return
    try:
        cur = os.stat(sock_name, dir_fd=nat_dir_fd, follow_symlinks=False)
        if (cur.st_dev, cur.st_ino) == owned_identity:
            os.unlink(sock_name, dir_fd=nat_dir_fd)
    except OSError:
        pass


def _systemd_run_env():
    env = {'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'}
    for key in ('XDG_RUNTIME_DIR', 'DBUS_SESSION_BUS_ADDRESS'):
        val = os.environ.get(key)
        if val:
            env[key] = val
    return env


def _sanitized_exec_env():
    env = {}
    for key, value in os.environ.items():
        if key.startswith(('PYTHON', 'LD_')) or key in ('ENV', 'BASH_ENV'):
            continue
        env[key] = value
    return env


def _launch_bounded_donor(invocation_id, main_pid, deadline_ns, *,
                          helper_path=HELPER_PATH, runner=subprocess.run):
    unit = f'{UNIT_PREFIX}-{invocation_id}-{main_pid}'
    cmd = [
        SYSTEMD_RUN_BIN,
        '--user',
        '--collect',
        f'--unit={unit}',
        '--property=RuntimeMaxSec=5s',
        '--property=TimeoutStopSec=1s',
        '--property=KillMode=control-group',
        '--no-block',
        '--',
        SUDO_BIN,
        '-n',
        DONOR_PYTHON_BIN,
        '-I',
        '-S',
        '-B',
        helper_path,
        '--donate',
    ]
    res = runner(
        cmd,
        env=_systemd_run_env(),
        shell=False,
        capture_output=True,
        timeout=_remaining_secs(deadline_ns),
        check=False,
    )
    transfer._require(res.returncode == 0)


def _accept_root_peer(listener, deadline_ns):
    while True:
        remaining = _remaining_secs(deadline_ns)
        try:
            ready = select.select([listener], [], [], remaining)
        except InterruptedError:
            continue
        transfer._require(bool(ready[0]) and time.monotonic_ns() < deadline_ns)
        conn, _addr = listener.accept()
        return conn


def maybe_exec_natural_host(
    raw_argv,
    *,
    helper_path=HELPER_PATH,
    runtime_root='/run/user',
    runner=subprocess.run,
    exec_fn=os.execve,
):
    """Attempt one bounded natural Host handoff + same-PID fexecve.

    Returns normally only if preconditions are not met or handoff fails before
    exec; raises on unexpected errors only if caller does not guard. On success
    this function replaces the current process via exec_fn.
    """
    transfer._require(sys.platform == 'linux' and os.name == 'posix')
    uid = os.getuid()
    transfer._require(uid == os.geteuid() > 0)
    main_pid = os.getpid()
    transfer._require(main_pid > 1)
    invocation_id = os.environ.get('INVOCATION_ID', '')
    transfer._require(bool(re.fullmatch(r'[0-9a-f]{32}', invocation_id)))
    transfer._require(
        isinstance(raw_argv, (list, tuple))
        and all(isinstance(arg, str) for arg in raw_argv)
        and 'serve' in raw_argv
        and '--once' not in raw_argv
        and not any(arg == '--alert-handoff-fd' or arg.startswith('--alert-handoff-fd=')
                    for arg in raw_argv)
    )
    expected_commit, _protocol = host_release()
    transfer._require(isinstance(expected_commit, str)
                      and bool(re.fullmatch(r'[0-9a-f]{40}', expected_commit)))
    _verify_installed_helper(helper_path)

    deadline_ns = time.monotonic_ns() + transfer.MAX_HANDOFF_NS
    sock_name = f'{invocation_id}-{main_pid}.sock'
    nat_dir_fd = -1
    listener = None
    owned_identity = None
    conn = None
    donation = None
    images = None
    taken_slots = None
    try:
        nat_dir_fd = _open_user_runtime_dir(uid, runtime_root=runtime_root)
        listener, owned_identity = _bind_unique_endpoint(nat_dir_fd, sock_name, uid)
        _launch_bounded_donor(invocation_id, main_pid, deadline_ns,
                              helper_path=helper_path, runner=runner)
        conn = _accept_root_peer(listener, deadline_ns)
        nonce = secrets.token_hex(32)
        donation = transfer.receive_root_donation(conn, nonce=nonce, deadline_ns=deadline_ns)
        closing_conn, conn = conn, None
        closing_conn.close()
        images = bootstrap.validate_donated_images(donation, expected_commit)
        images.recheck()
        transfer._require(images.close())
        images = None

        closing_listener, listener = listener, None
        closing_listener.close()
        identity, owned_identity = owned_identity, None
        _unlink_owned_endpoint(nat_dir_fd, sock_name, identity)
        closing_nat, nat_dir_fd = nat_dir_fd, -1
        os.close(closing_nat)

        taken_slots = donation.take()
        for fd in taken_slots:
            os.set_inheritable(fd, True)
        transfer._require(time.monotonic_ns() < deadline_ns)
        exec_argv = [
            f'/proc/self/fd/{taken_slots[0]}',
            '-I',
            f'/proc/self/fd/{taken_slots[1]}',
            'natural-host',
            *raw_argv,
            '--alert-handoff-fd',
            str(taken_slots[-1]),
        ]
        exec_fn(taken_slots[0], exec_argv, _sanitized_exec_env())
    finally:
        if images is not None:
            try:
                images.close()
            except BaseException:
                pass
        if taken_slots is not None:
            for fd in taken_slots:
                try:
                    os.set_inheritable(fd, False)
                except OSError:
                    pass
            transfer._close_owned(list(taken_slots))
        if donation is not None:
            try:
                donation.close()
            except BaseException:
                pass
        if conn is not None:
            try:
                conn.close()
            except OSError:
                pass
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass
        _unlink_owned_endpoint(nat_dir_fd, sock_name, owned_identity)
        if nat_dir_fd >= 0:
            transfer._close_owned([nat_dir_fd])
