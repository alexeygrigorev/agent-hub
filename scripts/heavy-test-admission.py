#!/usr/bin/env python3
"""Crash-safe, non-preemptive heavy-test admission; never unlink shared locks."""
import argparse
import fcntl
import json
import math
import os
from pathlib import Path
import random
import signal
import subprocess
import sys
import time

# A normal ticket only joins the final class after 25 minutes (inside the 30 minute
# wait budget), so --priority final really jumps a long queue of fresh and
# moderately aged normal tickets (was 300s, which made every queued ticket
# final-equivalent and the priority meaningless; position 33/35 observed).
PRIORITIES = ('normal', 'final')
NORMAL_AGE_S = 1500
try:  # test hook / host tuning; invalid values keep the default
    _age = os.environ.get('AGENT_HUB_HEAVY_TEST_NORMAL_AGE_S', '')
    if _age.isascii() and _age.isdecimal() and 1 <= int(_age) <= 86400:
        NORMAL_AGE_S = int(_age)
except Exception:
    pass
# Slot sizing (intake 167c6178 follow-up, 2026-11-02): RMTHZ (12 cores, 64G)
# was capped at 2 slots with 40 sessions queued while ~65% CPU / 30G free.
# The default is 6; a host may override it live, without restarting sessions,
# by writing an integer to <heavy-test-dir>/slots (re-read on every start).
# MAX_SLOTS is a hard ceiling that prevents the 2026-10-28 overload (46 vitest,
# 21 workerd, 25 playwright at once); it also clamps AGENT_HUB_HEAVY_TEST_SLOTS.
DEFAULT_SLOTS = 6
MAX_SLOTS = 8
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
# Run affinity (intake cb5c362e, 2026-11-02: 33 queued tickets, pushes blocked).
# A full gate takes one ticket per heavy phase and a capacity retry takes a new
# ticket per attempt; each used to rejoin the back of the queue, so one run paid
# the whole queue wait four or five times. Tickets that carry the same
# AGENT_HUB_HEAVY_TEST_RUN_ID reuse the run's first sequence number (and age) for
# RUN_AFFINITY_S, so a started run finishes ahead of newer arrivals.
RUN_ID_ENV = 'AGENT_HUB_HEAVY_TEST_RUN_ID'
RUN_AFFINITY_S = 7200
# Holders of an unbounded wrapper (written before the run timeout existed) never
# release. A waiter reclaims a slot whose holder wrapper is older than this
# (longest run + longest wait + slack) by stopping the holder's test group.
OVERDUE_HOLDER_S = FINAL_RUN_TIMEOUT_S + 1800 + 600
try:  # test hook; invalid values keep the default
    _overdue = os.environ.get('AGENT_HUB_HEAVY_TEST_OVERDUE_S', '')
    if _overdue.isascii() and _overdue.isdecimal() and 1 <= int(_overdue) <= 86400:
        OVERDUE_HOLDER_S = int(_overdue)
except Exception:
    pass
REAP_INTERVAL_S = 60


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
                    or type(row.get('created')) not in (float, int)
                    or not math.isfinite(row['created']) or row['created'] < 0):
                raise ValueError('invalid heavy-test queue entry')
            if row.get('priority') not in PRIORITIES:
                # Self-heal (2026-11-02 incident: one 'intake' row refused every
                # admission): an unknown priority is just a normal ticket.
                print(f"capped-test-run: warning: queue ticket {lease} had invalid priority {row.get('priority')!r}; treating as normal", file=sys.stderr, flush=True)
                row['priority'] = 'normal'
            sequences.add(row['sequence'])
        runs = state.get('runs')
        if not isinstance(runs, dict):
            runs = {}
        # Affinity is an optimisation: a malformed run record is dropped, never fatal.
        state['runs'] = {
            run: record for run, record in runs.items()
            if isinstance(record, dict) and type(record.get('sequence')) is int
            and 0 < record['sequence'] < state['next']
            and type(record.get('created')) in (float, int)
            and math.isfinite(record['created']) and type(record.get('seen')) in (float, int)
            and math.isfinite(record['seen'])}
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
    def run_id():
        run = os.environ.get(RUN_ID_ENV, '')
        if run and len(run) <= 64 and run.isascii() and all(c.isalnum() or c in '_.-' for c in run):
            return run
        return None

    def new_ticket(self, state, priority, now):
        """Allocate a ticket row; a known run reuses its original sequence."""
        runs = state.setdefault('runs', {})
        for run in [r for r, v in runs.items() if now - v['seen'] > RUN_AFFINITY_S]:
            del runs[run]
        taken = {row['sequence'] for row in state['active'].values()}
        run = self.run_id()
        record = runs.get(run) if run else None
        if record is not None and record['sequence'] not in taken:
            sequence, created = record['sequence'], record['created']
        else:
            sequence, created = state['next'], now
            state['next'] += 1
        if run:
            runs[run] = {'sequence': sequence, 'created': created, 'seen': now}
        return sequence, {'sequence': sequence, 'priority': priority, 'created': created}

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


def proc_ppid_pgid(pid):
    try:
        stat = Path(f'/proc/{pid}/stat').read_text()
        fields = stat[stat.rfind(')') + 1:].split()
        return int(fields[1]), int(fields[2])
    except (OSError, ValueError, IndexError):
        return None


def lock_holders(path):
    """PIDs of this user's processes with the lock file open (fuser-like /proc scan)."""
    try:
        target = path.stat()
    except OSError:
        return []
    found = []
    for entry in os.listdir('/proc'):
        if not entry.isdecimal():
            continue
        try:
            for fd in os.listdir(f'/proc/{entry}/fd'):
                try:
                    info = os.stat(f'/proc/{entry}/fd/{fd}')
                except OSError:
                    continue
                if (info.st_ino, info.st_dev) == (target.st_ino, target.st_dev):
                    found.append(int(entry))
                    break
        except OSError:
            continue
    return found


def process_age_s(pid):
    start = process_start_time(pid)
    if start is None:
        return None
    try:
        uptime = float(Path('/proc/uptime').read_text().split()[0])
        return uptime - int(start) / os.sysconf('SC_CLK_TCK')
    except (OSError, ValueError, IndexError):
        return None


def stop_group(pgid):
    signal_group(pgid, signal.SIGTERM)
    deadline = time.monotonic() + TERMINATE_GRACE_S
    while time.monotonic() < deadline:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.1)
    signal_group(pgid, signal.SIGKILL)


def reap_overdue_holders(root, slots):
    """Stop the test group of any slot holder older than OVERDUE_HOLDER_S.

    Only an admission wrapper that declared no bound of its own (no
    --run-timeout / run-timeout env) is touched, and only its command's
    process group; the wrapper then exits normally and releases the slot.
    """
    for index in range(slots):
        for pid in lock_holders(root / f'slot-{index}.lock'):
            try:
                argv = Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')
                bounded = (b'--run-timeout' in argv or b'AGENT_HUB_HEAVY_TEST_RUN_TIMEOUT_S='
                           in Path(f'/proc/{pid}/environ').read_bytes())
            except OSError:
                continue
            if not any(a.endswith(b'heavy-test-admission.py') for a in argv[:3]) or bounded:
                continue
            age = process_age_s(pid)
            if age is None or age < OVERDUE_HOLDER_S:
                continue
            for child in os.listdir('/proc'):
                if child.isdecimal() and proc_ppid_pgid(int(child)) == (pid, int(child)):
                    print(f'capped-test-run: reclaiming slot-{index}: holder wrapper PID {pid} is {int(age)}s old '
                          f'(> {OVERDUE_HOLDER_S}s, no run bound); stopping its command group {child}',
                          file=sys.stderr, flush=True)
                    stop_group(int(child))


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
    parser.add_argument('--slots', default=os.environ.get('AGENT_HUB_HEAVY_TEST_SLOTS'))
    parser.add_argument('--wait', default=os.environ.get('AGENT_HUB_HEAVY_TEST_WAIT_S', '1800'))
    parser.add_argument('--run-timeout', default=os.environ.get(
        'AGENT_HUB_HEAVY_TEST_RUN_TIMEOUT_S'))
    env_priority = os.environ.get('AGENT_HUB_HEAVY_TEST_PRIORITY', 'normal')
    if env_priority not in PRIORITIES:
        # argparse does not validate a default against choices; a bad env value
        # used to be written into the shared queue and poison every contender.
        print(f'capped-test-run: warning: ignoring invalid AGENT_HUB_HEAVY_TEST_PRIORITY={env_priority!r}; using normal', file=sys.stderr, flush=True)
        env_priority = 'normal'
    parser.add_argument('--priority', default=env_priority, choices=list(PRIORITIES))
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    root = Path(os.environ.get('AGENT_HUB_HEAVY_TEST_DIR', f'/run/user/{os.getuid()}/agent-hub/heavy-test'))
    root.mkdir(parents=True, exist_ok=True)
    if args.slots is None:
        try:
            args.slots = (root / 'slots').read_text().strip()
        except OSError:
            args.slots = str(DEFAULT_SLOTS)
        if not args.slots.isascii() or not args.slots.isdecimal() or int(args.slots) < 1:
            args.slots = str(DEFAULT_SLOTS)
    if not args.slots.isascii() or not args.slots.isdecimal() or int(args.slots) < 1:
        parser.error('invalid slot count')
    args.slots = str(min(int(args.slots), MAX_SLOTS))
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
            sequence, state['active'][name] = queue.new_ticket(
                state, args.priority, time.monotonic())
            queue.save(state)
        finally:
            fcntl.flock(mutex, fcntl.LOCK_UN)
        with ticket:
            announced = False
            slot = None
            next_reap = time.monotonic() + REAP_INTERVAL_S
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
                        if waiting > 2 * int(args.slots):
                            print('capped-test-run: queue is deep; prefer targeted verification (scripts/test-affected-local.sh, see docs/PREPUSH-TARGETED-TESTS.md) over the full suite', file=sys.stderr, flush=True)
                        announced = True
                    if time.monotonic() >= next_reap:
                        next_reap = time.monotonic() + REAP_INTERVAL_S
                        reap_overdue_holders(root, int(args.slots))
                    time.sleep(min(random.uniform(1, 2), remaining))
            with slot:
                running = True
                print(f'capped-test-run: admitted {args.priority} ticket {sequence}', file=sys.stderr, flush=True)
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
