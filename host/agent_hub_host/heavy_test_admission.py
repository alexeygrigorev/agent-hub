#!/usr/bin/env python3
"""Versioned host-release implementation of heavy-test admission.

The small worktree wrapper selects this file from the active verified host
release when available. Keep the CLI and shared queue protocol compatible
with the script fallback shipped in worktrees.
"""
import argparse
from datetime import datetime, timezone
import fcntl
import json
import math
import os
from pathlib import Path
import random
import re
import signal
import subprocess
import sys
import time

NORMAL_AGE_S = 300
ADMISSION_PROTOCOL_VERSION = 1
# Refusal channel (intake 9432731e): exit 3 alone is ambiguous because the
# admitted command may itself exit 3 (pytest INTERNALERROR). When this names a
# path, a refusal before the command starts writes it; the command never sees
# the variable, so its own exit 3 leaves no marker.
REFUSAL_FILE_ENV = 'AGENT_HUB_HEAVY_TEST_REFUSAL_FILE'
HOLDER_TOKEN_ENV = 'AGENT_HUB_HEAVY_TEST_HOLDER_TOKEN'
COMMAND_STARTED = False
DEFAULT_RUN_TIMEOUT_S = 1800
FINAL_RUN_TIMEOUT_S = 7200
TERMINATE_GRACE_S = 10
NESTED_WAIT_QUIET_S = 10.0
_REAPER_SNAPSHOT = None


def refuse(reason):
    path = os.environ.get(REFUSAL_FILE_ENV)
    if path and not COMMAND_STARTED:
        try:
            Path(path).write_text(f'capped-test-run refused: {reason}\n')
        except OSError as error:
            print(f'capped-test-run: cannot write refusal marker: {error}', file=sys.stderr)
    return 3


def lock_available(handle):
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False


class Queue:
    """Called only under admission.lock; work/storage depend on live waiters.

    Lease lock filenames are reused after proving them unlocked. The counter
    and active index are atomically replaced together; no historical directory
    walk, PID liveness check, or unlink of any ticket/slot lock is needed.
    """
    def __init__(self, root):
        self.root = root
        self.leases = root / 'leases'
        self.leases.mkdir(exist_ok=True)
        self.path = root / 'fifo-state.json'

    def load(self):
        try:
            state = json.loads(self.path.read_text())
        except FileNotFoundError:
            return {'version': 1, 'next': 1, 'active': {}}
        if (not isinstance(state, dict) or state.get('version') != 1
                or type(state.get('next')) is not int or state['next'] < 1
                or not isinstance(state.get('active'), dict)):
            raise ValueError('invalid heavy-test queue state')
        sequences = set()
        for lease, row in state['active'].items():
            if (not lease.isascii() or not lease.isdecimal()
                    or not isinstance(row, dict)
                    or type(row.get('sequence')) is not int
                    or not 0 < row['sequence'] < state['next']
                    or row['sequence'] in sequences
                    or row.get('priority') not in ('normal', 'final')
                    or type(row.get('created')) not in (float, int)
                    or not math.isfinite(row['created']) or row['created'] < 0):
                raise ValueError('invalid heavy-test queue entry')
            sequences.add(row['sequence'])
        return state

    def save(self, state):
        # A fixed staging filename is safe because the global mutex covers
        # every reader/writer. A crash leaves either the old or new full index.
        with self.path.with_suffix('.next').open('w') as out:
            json.dump(state, out)
            out.flush()
            os.fsync(out.fileno())
        os.replace(self.path.with_suffix('.next'), self.path)

    def open_lease(self, name):
        return (self.leases / f'{name}.lock').open('a+')

    def prune_dead(self, state):
        for name in list(state['active']):
            with self.open_lease(name) as lease:
                if lock_available(lease):
                    del state['active'][name]

    @staticmethod
    def order(row, now):
        # An aged normal joins the final class with its original sequence.
        # Later final arrivals cannot indefinitely overtake that normal.
        preferred = row['priority'] == 'final' or now - row['created'] >= NORMAL_AGE_S
        return (not preferred, row['sequence'])

    @classmethod
    def head(cls, state, now):
        return min(state['active'].items(), key=lambda item: cls.order(item[1], now))[0]

    @classmethod
    def position(cls, state, name, now):
        ordered = sorted(state['active'].items(), key=lambda item: cls.order(item[1], now))
        return next(index for index, item in enumerate(ordered, 1) if item[0] == name), len(ordered)


def signal_group(pgid, number):
    try:
        os.killpg(pgid, number)
    except ProcessLookupError:
        pass


def process_start_time(pid):
    """Return Linux process start ticks, parsing comm fields with parentheses safely."""
    try:
        stat = Path(f'/proc/{pid}/stat').read_text()
    except (OSError, ValueError):
        return None
    close = stat.rfind(')')
    if close < 0:
        return None
    fields = stat[close + 1:].split()
    # The tail starts at field 3 (state); starttime is field 22.
    try:
        return fields[19]
    except IndexError:
        return None


def pool_identity(root):
    """Return a stable identity for the admission pool directory."""
    info = root.stat()
    return info.st_dev, info.st_ino


def proc_snapshot(proc_root='/proc'):
    """Read only bounded process metadata needed to recognize queued nesting.

    No command arguments or environment values leave this function. Only fixed
    executable tokens, process ids, groups, start ticks, CPU ticks and lock fd
    targets are retained.
    """
    root = Path(proc_root)
    processes = {}
    try:
        entries = list(root.iterdir())[:100000]
    except OSError:
        return processes
    for entry in entries:
        if not entry.name.isdecimal():
            continue
        pid = int(entry.name)
        try:
            raw = (entry / 'stat').read_text()
            end = raw.rfind(')')
            fields = raw[end + 1:].split()
            if end < 0 or len(fields) < 20:
                continue
            ppid, pgid = int(fields[1]), int(fields[2])
            cpu = int(fields[11]) + int(fields[12])
            start = fields[19]
            argv = (entry / 'cmdline').read_bytes().decode(errors='replace').split('\0')
            argv = [part for part in argv if part]
            tokens = {Path(part).name for part in argv}
            intake_id = None
            if tokens & {'heavy-test-admission.py', 'agent_hub_host.heavy_test_admission'}:
                try:
                    environ = (entry / 'environ').read_bytes()[:131072].split(b'\0')
                    for item in environ:
                        key, separator, value = item.partition(b'=')
                        if separator and key in (b'AGENT_HUB_INTAKE_ID', b'HUB_INTAKE_ID'):
                            candidate = value.decode('ascii', errors='ignore')
                            if re.fullmatch(r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}', candidate):
                                intake_id = candidate
                            break
                except OSError:
                    pass
            fds = set()
            for fd in (entry / 'fd').iterdir():
                try:
                    fds.add(os.readlink(fd))
                except OSError:
                    continue
            processes[pid] = {
                'pid': pid, 'ppid': ppid, 'pgid': pgid, 'cpu': cpu,
                'start': start, 'tokens': tokens, 'fds': fds,
                'intake_id': intake_id,
            }
        except (OSError, ValueError, IndexError):
            continue
    return processes


def _descendants(processes, root_pid):
    children = {}
    for row in processes.values():
        children.setdefault(row['ppid'], []).append(row['pid'])
    found, stack = set(), list(children.get(root_pid, ()))
    while stack:
        pid = stack.pop()
        if pid in found:
            continue
        found.add(pid)
        stack.extend(children.get(pid, ()))
    return found


def _lock_is_held(path):
    try:
        with Path(path).open('a+') as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(handle, fcntl.LOCK_UN)
            return False
    except OSError:
        return False


def nested_hold_candidates(pool, current, previous, now, *, quiet_s=NESTED_WAIT_QUIET_S):
    """Return only stable queued nested waiters; never infer from age alone."""
    if not previous or now - previous.get('at', now) < quiet_s:
        return []
    try:
        pool_identity(pool)
        state = Queue(pool).load()
        slots = [p for p in pool.glob('slot-*.lock') if p.is_file() and not p.is_symlink()]
    except (OSError, ValueError):
        return []
    processes = current.get('processes', {})
    old = previous.get('processes', {})
    lock_owners = {}
    for path in slots:
        if not _lock_is_held(path):
            continue
        try:
            target = str(path.resolve())
        except OSError:
            continue
        for pid, row in processes.items():
            if target in row['fds']:
                lock_owners[pid] = True
    if not lock_owners:
        return []

    lease_pids = {}
    for name, ticket in state['active'].items():
        lease_path = pool / 'leases' / f'{name}.lock'
        if not _lock_is_held(lease_path):
            continue
        path = str(lease_path.resolve())
        for pid, row in processes.items():
            before = old.get(pid)
            if (path in row['fds'] and row['tokens'] & {
                    'heavy-test-admission.py', 'agent_hub_host.heavy_test_admission'}
                    and before and before.get('start') == row['start']
                    and before.get('cpu') == row['cpu']):
                lease_pids[pid] = (name, ticket)

    active_tests = {'pytest', 'py.test', 'vitest', 'vitest.mjs', 'unittest', 'nosetests', 'node'}
    output, seen = [], set()
    for holder_pid in lock_owners:
        descendants = _descendants(processes, holder_pid)
        if any(processes[pid]['tokens'] & active_tests for pid in descendants if pid in processes):
            continue
        for waiter_pid in descendants:
            waiter = processes.get(waiter_pid)
            if waiter_pid not in lease_pids or waiter is None or waiter['pgid'] <= 1:
                continue
            before = old.get(waiter_pid)
            if (before is None or before.get('start') != waiter['start']
                    or before.get('cpu') != waiter['cpu']):
                continue
            _, ticket = lease_pids[waiter_pid]
            key = (holder_pid, waiter['pgid'], waiter_pid, waiter['start'], ticket['sequence'])
            if key in seen:
                continue
            seen.add(key)
            output.append({
                'event': 'nested_heavy_test_hold', 'holder_pid': holder_pid,
                'waiter_pid': waiter_pid, 'process_group': waiter['pgid'],
                'waiter_start': waiter['start'], 'ticket_sequence': ticket['sequence'],
                'priority': ticket['priority'],
                'intake_id': waiter.get('intake_id') or processes[holder_pid].get('intake_id'),
            })
    return output


def reap_nested_holds(pool=None, *, proc_root='/proc', now=None, previous=None,
                      signaller=os.killpg, post_note=None):
    """Terminate only quiet nested wait command groups, then report the event."""
    pool = Path(pool or os.environ.get(
        'AGENT_HUB_HEAVY_TEST_DIR', f'/run/user/{os.getuid()}/agent-hub/heavy-test'))
    now = time.monotonic() if now is None else now
    current = {'at': now, 'processes': proc_snapshot(proc_root)}
    events = nested_hold_candidates(pool, current, previous, now)
    delivered = []
    for event in events:
        latest = {'at': now, 'processes': proc_snapshot(proc_root)}
        still_waiting = nested_hold_candidates(pool, latest, previous, now)
        if not any(
                candidate['holder_pid'] == event['holder_pid']
                and candidate['waiter_pid'] == event['waiter_pid']
                and candidate['waiter_start'] == event['waiter_start']
                and candidate['process_group'] == event['process_group']
                for candidate in still_waiting):
            continue
        try:
            signaller(event['process_group'], signal.SIGTERM)
        except ProcessLookupError:
            continue
        delivered.append(event)
        if post_note is not None:
            try:
                event['intake_note_posted'] = post_note(event) is True
            except Exception:
                # The local structured log remains authoritative if Hub is
                # unavailable; never turn a queue reaper into a host failure.
                pass
    return current, delivered


def nested_holder_ancestor(identity):
    """Find an ancestor holding a slot in this command's admission pool."""
    token = os.environ.get(HOLDER_TOKEN_ENV, '')
    try:
        holder_pid_text, holder_start, holder_device, holder_inode = token.split(':', 3)
        holder_pid = int(holder_pid_text)
        if (holder_pid < 2 or not holder_start.isdecimal()
                or not holder_device.isdecimal() or not holder_inode.isdecimal()
                or (int(holder_device), int(holder_inode)) != identity):
            return None
    except (ValueError, TypeError):
        return None
    if process_start_time(holder_pid) != holder_start:
        return None

    parent = os.getppid()
    visited = set()
    while parent > 1 and parent not in visited:
        if parent == holder_pid:
            return holder_pid
        visited.add(parent)
        try:
            stat = Path(f'/proc/{parent}/stat').read_text()
            close = stat.rfind(')')
            if close < 0:
                return None
            fields = stat[close + 1:].split()
            parent = int(fields[1])  # tail fields: state, ppid, ...
        except (OSError, ValueError, IndexError):
            return None
    return None


def run_command(command, slot, interrupted, run_timeout, identity):
    global COMMAND_STARTED
    # Reserve/inherit the historical FD9. Parent retains its own descriptor
    # even when the command closes FD9. Descendants retaining it also keep
    # the slot after an uncatchable wrapper-only crash.
    os.dup2(slot.fileno(), 9)
    holder_pid = os.getpid()
    holder_start = process_start_time(holder_pid)
    env = dict(os.environ, AGENT_HUB_HEAVY_TEST_HELD='1')
    if holder_start is not None:
        device, inode = identity
        env[HOLDER_TOKEN_ENV] = f'{holder_pid}:{holder_start}:{device}:{inode}'
    env.pop(REFUSAL_FILE_ENV, None)
    COMMAND_STARTED = True
    try:
        try:
            # A dedicated process group lets a timeout clean up the test runner
            # and its ordinary descendants before releasing the shared slot.
            child = subprocess.Popen(command, pass_fds=(9,), env=env,
                                     start_new_session=True)
        except OSError as error:
            print(f'capped-test-run: {error}', file=sys.stderr)
            return 127 if isinstance(error, FileNotFoundError) else 126
        # The session leader's PID is the PGID because start_new_session is
        # true. Keep it unreaped until cleanup signals are finished so that
        # this numeric PGID cannot be reused for an unrelated process group.
        child_pgid = child.pid
        deadline = time.monotonic() + run_timeout
        stopping_at = None
        timed_out = False
        while True:
            signal_status = interrupted()
            now = time.monotonic()
            if stopping_at is None and signal_status:
                stopping_at = now
                signal_group(child_pgid, signal.SIGTERM)
            elif stopping_at is None and now >= deadline:
                timed_out = True
                stopping_at = now
                print(f'capped-test-run: command exceeded {run_timeout}s; terminating process group',
                      file=sys.stderr, flush=True)
                signal_group(child_pgid, signal.SIGTERM)
            elif stopping_at is not None and now - stopping_at >= TERMINATE_GRACE_S:
                signal_group(child_pgid, signal.SIGKILL)
                result = child.wait()
                break
            if stopping_at is not None:
                # Observe leader exit without reaping it. If it exits on TERM,
                # kill any descendants in its group before releasing the PID.
                try:
                    exited = os.waitid(os.P_PID, child.pid,
                                       os.WEXITED | os.WNOHANG | os.WNOWAIT)
                except ChildProcessError:
                    exited = None
                if exited is not None and exited.si_pid:
                    signal_group(child_pgid, signal.SIGKILL)
                    result = child.wait()
                    break
                time.sleep(0.1)
                continue
            try:
                result = child.wait(timeout=0.1)
                break
            except subprocess.TimeoutExpired:
                continue
        if timed_out:
            return 124
        return interrupted() or (128 - result if result < 0 else result)
    finally:
        os.close(9)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--slots', default=os.environ.get('AGENT_HUB_HEAVY_TEST_SLOTS', '2'))
    parser.add_argument('--wait', default=os.environ.get('AGENT_HUB_HEAVY_TEST_WAIT_S', '1800'))
    parser.add_argument('--run-timeout', default=os.environ.get(
        'AGENT_HUB_HEAVY_TEST_RUN_TIMEOUT_S'))
    parser.add_argument('--priority', default=os.environ.get('AGENT_HUB_HEAVY_TEST_PRIORITY', 'normal'), choices=['normal', 'final'])
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if not args.slots.isascii() or not args.slots.isdecimal() or int(args.slots) < 1:
        parser.error('invalid slot count')
    if not args.wait.isascii() or not args.wait.isdecimal():
        parser.error('invalid wait seconds')
    if (args.run_timeout is not None and
            (not args.run_timeout.isascii() or not args.run_timeout.isdecimal()
             or not 1 <= int(args.run_timeout) <= 86400)):
        parser.error('invalid command run timeout seconds (expected 1..86400)')
    run_timeout = int(args.run_timeout) if args.run_timeout is not None else (
        FINAL_RUN_TIMEOUT_S if args.priority == 'final' else DEFAULT_RUN_TIMEOUT_S)
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    if not command:
        parser.error('no command given')
    root = Path(os.environ.get('AGENT_HUB_HEAVY_TEST_DIR', f'/run/user/{os.getuid()}/agent-hub/heavy-test'))
    root.mkdir(parents=True, exist_ok=True)
    identity = pool_identity(root)
    ancestor_holder = nested_holder_ancestor(identity)
    if ancestor_holder is not None:
        print('capped-test-run: nested heavy-test admission detected; '
              f'ancestor holder PID {ancestor_holder} already owns a slot',
              file=sys.stderr, flush=True)
        return 2
    queue = Queue(root)
    deadline = time.monotonic() + int(args.wait)
    interrupted = 0
    running = False

    def on_signal(number, _frame):
        nonlocal interrupted
        interrupted = 128 + number
        if not running:
            raise SystemExit(interrupted)

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)
    # Reserve FD9 before opening our own queue/slot handles.
    reserve = os.open(os.devnull, os.O_RDONLY)
    os.dup2(reserve, 9)
    if reserve != 9:
        os.close(reserve)
    with (root / 'admission.lock').open('a+') as mutex:
        fcntl.flock(mutex, fcntl.LOCK_EX)
        try:
            state = queue.load()
            queue.prune_dead(state)
            index = 0
            while str(index) in state['active']:
                index += 1
            name = str(index)
            ticket = queue.open_lease(name)
            if not lock_available(ticket):
                ticket.close()
                raise ValueError('unindexed heavy-test lease is locked')
            sequence = state['next']
            state['next'] += 1
            state['active'][name] = {'sequence': sequence, 'priority': args.priority,
                                     'created': time.monotonic()}
            ticket_created = state['active'][name]['created']
            queue.save(state)
        finally:
            fcntl.flock(mutex, fcntl.LOCK_UN)
        with ticket:
            announced = False
            slot = None
            while slot is None:
                fcntl.flock(mutex, fcntl.LOCK_EX)
                try:
                    state = queue.load()
                    changed = False
                    head = queue.head(state, time.monotonic())
                    while head != name:
                        with queue.open_lease(head) as other:
                            if not lock_available(other):
                                break
                        del state['active'][head]
                        changed = True
                        head = queue.head(state, time.monotonic())
                    if head == name:
                        for index in range(int(args.slots)):
                            candidate = (root / f'slot-{index}.lock').open('a+')
                            if lock_available(candidate):
                                slot = candidate
                                break
                            candidate.close()
                    if slot is not None:
                        del state['active'][name]
                        changed = True
                        # Release the reusable lease before publishing its
                        # removal. The global mutex prevents a reuse race.
                        fcntl.flock(ticket, fcntl.LOCK_UN)
                    if changed:
                        queue.save(state)
                finally:
                    fcntl.flock(mutex, fcntl.LOCK_UN)
                if slot is None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        print(f'capped-test-run: no heavy-test slot free after {args.wait}s ({args.slots} slots)', file=sys.stderr)
                        return refuse(f'no heavy-test slot free after {args.wait}s')
                    if not announced:
                        position, waiting = queue.position(state, name, time.monotonic())
                        print(f'capped-test-run: all {args.slots} heavy-test slots busy; waiting up to {args.wait}s ({args.priority} ticket {sequence}), queue position {position}/{waiting}', file=sys.stderr, flush=True)
                        announced = True
                    time.sleep(min(random.uniform(1, 2), remaining))
            with slot:
                running = True
                event = {
                    'event': 'heavy_test_admitted',
                    'protocol': ADMISSION_PROTOCOL_VERSION,
                    'ticket_sequence': sequence,
                    'priority': args.priority,
                    'queue_wait_s': round(max(0.0, time.monotonic() - ticket_created), 3),
                    'admitted_at_utc': datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z'),
                }
                print('capped-test-run: admitted ' + json.dumps(event, sort_keys=True),
                      file=sys.stderr, flush=True)
                print(f'capped-test-run: run timeout {run_timeout}s', file=sys.stderr, flush=True)
                return run_command(command, slot, lambda: interrupted, run_timeout, identity)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError, OverflowError, RecursionError) as error:
        # Direct Vitest/pytest hooks refuse exit 3 but historically treat exit
        # 2 as an unavailable wrapper. Queue corruption must never uncap them.
        # argparse usage errors still exit 2; command errors retain their code.
        print(f'capped-test-run: admission refused: {error}; inspect queue state/permissions under admission.lock; never remove lock files (see docs/HETZNER-OVERLOAD-0689CE9C.md)', file=sys.stderr)
        sys.exit(refuse('admission refused'))
