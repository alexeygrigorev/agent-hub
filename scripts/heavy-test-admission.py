#!/usr/bin/env python3
"""Crash-safe, non-preemptive heavy-test admission; never unlink shared locks."""
import ast
import argparse
import fcntl
import hashlib
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
from datetime import datetime, timezone

try:
    import pwd
except ImportError:  # pragma: no cover - this script is Unix-only today
    pwd = None

# Priority classes, lowest privilege first (intake 1293b560 AC3).
# 'final' is a pre-push verification lane: it jumps queued normal tickets
# younger than NORMAL_AGE_S. 'release' is the orchestrator-main
# release/integration gate lane: it is admitted ahead of both normal and
# final tickets, so main is never blocked behind worker test lanes. Only an
# explicit --priority release / AGENT_HUB_HEAVY_TEST_PRIORITY=release takes
# the release class; aging promotes a normal ticket to the final class only,
# never to release. Readers at or after 0a353095 coerce an unknown priority
# to 'normal' with a warning instead of failing the queue (see Queue.load).
# Readers before 0a353095 (including the d8740aac era) refuse that single
# admission with a caught ValueError (exit 3, shared state untouched); the
# foreign row disappears when its ticket is admitted (release admits first
# at rank 0), so the effect is transient and self-clearing, never pool
# poisoning. No worktree edit is needed on either side for this.
PRIORITIES = ('normal', 'final', 'release')
NORMAL_AGE_S = 1500
try:  # test hook / host tuning; invalid values keep the default
    _age = os.environ.get('AGENT_HUB_HEAVY_TEST_NORMAL_AGE_S', '')
    if _age.isascii() and _age.isdecimal() and 1 <= int(_age) <= 86400:
        NORMAL_AGE_S = int(_age)
except Exception:
    pass
# Slot sizing (intake 1293b560 AC2, supersedes the 167c6178 follow-up note).
# For readers at or after 9f5ea5f2 the slot count is ONE host-level setting:
# the integer in the live <heavy-test-dir>/slots file, re-read by every
# admission. An explicit --slots flag still wins (operator/tests);
# AGENT_HUB_HEAVY_TEST_SLOTS is only a fallback for private pools and tests
# when the file is absent, never a per-process override of the live host
# setting. Readers before 9f5ea5f2 (including the d8740aac era) do not read
# the file: with the env var unset they use their compiled default (2), so
# until they rebase they contend for fewer shared slots than newer checkouts
# (measured 2026-11-03 on RMTHZ: live file 4, zero processes export the env).
# Never export the env per-process (see docs/HETZNER-OVERLOAD-0689CE9C.md
# and AGENTS.md); the live file keeps current checkouts on one count.
#
# Sizing formula (measured 2026-11-02, RMTHZ: 12 cores, 62 GiB):
#   load_budget = 1.5 * cores            (host 5-min load budget for tests)
#   cpu_slots   = floor((load_budget - base_load_5min) / cpu_per_lane)
#   mem_slots   = floor((MemAvailable_GiB - mem_reserve_GiB) / mem_per_lane_GiB)
#   slots       = clamp(1, MAX_SLOTS, min(cpu_slots, mem_slots))
# Measured per-lane cost: vitest lane ~0.71 cores / ~0.9 GiB, pytest lane
# ~0.27 cores / ~0.64 GiB (two existing lanes, 2026-11-02). mem_reserve is
# 6 GiB (at most 25% of RAM on small hosts, matching the host-dispatch
# floor). Example: budget 18, base 5-min load 10-15 -> headroom 3-8 ->
# cpu_slots 4-11 with the worst-case 0.71 lane; mem (33-6)/0.9 ~= 30, not
# binding; MAX_SLOTS clamps to 8. The default below is the formula's
# mid-range outcome; the live file carries the tuned value.
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

ADMISSION_PROTOCOL_VERSION = 1


def release_account_home():
    if pwd is not None:
        try:
            account_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
        except (KeyError, OSError, AttributeError):
            account_home = None
        if account_home is not None and account_home.is_absolute():
            return account_home
    try:
        fallback = Path.home()
    except RuntimeError:
        return None
    return fallback if fallback.is_absolute() else None


def active_release_roots():
    # An explicit root is authoritative. This keeps private releases isolated
    # and prevents a second passwd-derived root from widening the override.
    configured = os.environ.get('AGENT_HUB_HOST_RELEASES_DIR')
    test_isolated = 'HUB_TEST_GUARD_HOME' in os.environ
    if configured:
        root = Path(configured)
        if configured == '~' or configured.startswith('~/'):
            home = Path.home() if test_isolated else release_account_home()
            if home is None:
                return set()
            root = home / configured[2:]
        else:
            root = root.expanduser()
        return {root.resolve()}

    # Host tests pass this guard to subprocesses while HOME points at a
    # disposable directory. Do not let service discovery escape that sandbox.
    if test_isolated:
        return set()

    account_home = release_account_home()
    if account_home is None:
        return set()
    roots = (
        account_home / '.local/share/agent-hub/host-releases',
        account_home / '.local/share/agent-hub/host-releases-orch',
    )
    return {root.resolve() for root in roots}


def active_release_module():
    """Return the verified admission module in the active host release, if supported."""
    module_rel = Path('host/agent_hub_host/heavy_test_admission.py')
    try:
        result = subprocess.run(
            ['systemctl', '--user', 'show', 'agent-hub-session-host.service',
             '--property=ExecStart', '--value'],
            capture_output=True, text=True, timeout=5, check=False)
        if result.returncode != 0:
            return None
        match = re.search(r'(?:^|[ {])path=([^;]+)', result.stdout.strip())
        if not match:
            return None
        executable = Path(os.path.abspath(match.group(1).strip().strip('"')))
        if executable.parent.name != 'bin' or executable.parent.parent.name != '.venv':
            return None
        release = executable.parent.parent.parent.resolve()
        if (release.parent not in active_release_roots()
                or not executable.is_file()
                or not re.fullmatch(r'[0-9a-f]{40}', release.name)):
            return None
        repository = Path(__file__).resolve().parents[1]
        ancestor = subprocess.run(
            ['git', '-C', str(repository), 'merge-base', '--is-ancestor',
             'd8740aaca3944a295680177a40148bb00accea78', release.name],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=10, check=False)
        if ancestor.returncode == 1:
            return None
        if ancestor.returncode != 0:
            raise RuntimeError('cannot verify active host release revision')
        verifier = release / 'scripts' / 'verify-host-release.py'
        if not verifier.is_file() or verifier.is_symlink():
            raise RuntimeError('active host release verifier is missing')
        verified = subprocess.run(
            [str(executable), str(verifier), str(release)],
            capture_output=True, text=True, timeout=60, check=False)
        if verified.returncode != 0:
            raise RuntimeError('active host release verification failed')
        manifest_path = release / 'release-manifest.json'
        if manifest_path.is_symlink():
            raise RuntimeError('active host release manifest is a symlink')
        try:
            manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        except (OSError, ValueError) as error:
            raise RuntimeError('active host release manifest is unreadable') from error
        if not isinstance(manifest, dict) or manifest.get('release_rev') != release.name:
            raise RuntimeError('active host release revision mismatch')
        module = release / module_rel
        if not module.is_file() or module.is_symlink():
            return None
        hashes = manifest.get('files_sha256')
        if not isinstance(hashes, dict):
            raise RuntimeError('active host release manifest has no file hashes')
        expected_hash = hashes.get(module_rel.as_posix())
        if not isinstance(expected_hash, str) or not re.fullmatch(r'[0-9a-f]{64}', expected_hash):
            return None
        data = module.read_bytes()
        if hashlib.sha256(data).hexdigest() != expected_hash:
            raise RuntimeError('active host release admission module hash mismatch')
        tree = ast.parse(data, filename=str(module))
        protocol = next((node.value.value for node in tree.body
                         if isinstance(node, ast.Assign)
                         and any(isinstance(target, ast.Name)
                                 and target.id == 'ADMISSION_PROTOCOL_VERSION'
                                 for target in node.targets)
                         and isinstance(node.value, ast.Constant)
                         and type(node.value.value) is int), None)
        if protocol != ADMISSION_PROTOCOL_VERSION:
            return None
        return module
    except RuntimeError:
        raise
    except (OSError, ValueError, subprocess.SubprocessError, TypeError):
        return None


def dispatch_to_active_release():
    try:
        module = active_release_module()
    except RuntimeError as error:
        print(f'capped-test-run: REFUSED: {error}', file=sys.stderr)
        raise SystemExit(126)
    if module is not None:
        os.execv(sys.executable, [sys.executable, str(module), *sys.argv[1:]])


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

    def park_refused(self, state, name, now):
        """Release a capacity-refused waiter's queue row but keep its place.

        Intake 1293b560 AC1: a waiter refused after the full wait budget used
        to leave its row behind with no run record refresh, so a retry with
        the same AGENT_HUB_HEAVY_TEST_RUN_ID found its old sequence taken and
        re-queued at the back. Parking deletes our own active row (our lease
        unlocks on return anyway, so the row is dead) while refreshing the
        run record with the original sequence and creation time. The retry
        then reclaims the same sequence and age -- it keeps its queue
        position (and any aging) instead of joining the tail. Callers without
        a run id just drop the dead row so the shared state stays small.
        """
        row = state['active'].pop(name, None)
        if row is None:
            return
        run = self.run_id()
        if run is None:
            return
        record = {'sequence': row['sequence'], 'created': row['created'],
                  'seen': now}
        state.setdefault('runs', {})[run] = record

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
        # Release outranks everything; an aged normal joins the final class
        # with its original sequence, but never the release class. Later
        # final arrivals cannot indefinitely overtake that aged normal, and
        # later arrivals of any lower class cannot overtake a release ticket.
        priority = row['priority']
        if priority == 'release':
            rank = 0
        elif priority == 'final' or now - row['created'] >= NORMAL_AGE_S:
            rank = 1
        else:
            rank = 2
        return (rank, row['sequence'])

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
    parser.add_argument('--slots', default=None)
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
    # Precedence (intake 1293b560 AC2): explicit --slots, then the live
    # host-level <dir>/slots file, then AGENT_HUB_HEAVY_TEST_SLOTS (fallback
    # for private pools/tests when the file is absent), then DEFAULT_SLOTS.
    # The file is authoritative: a per-process env value never overrides the
    # live host setting, so old and new checkouts share one count.
    if args.slots is None:
        try:
            args.slots = (root / 'slots').read_text().strip()
            slots_source = 'file'
        except OSError:
            args.slots = None
            slots_source = None
    else:
        slots_source = 'flag'
    if args.slots is None:
        args.slots = os.environ.get('AGENT_HUB_HEAVY_TEST_SLOTS')
        slots_source = 'env' if args.slots is not None else None
    if args.slots is None:
        args.slots = str(DEFAULT_SLOTS)
        slots_source = 'default'
    if not args.slots.isascii() or not args.slots.isdecimal() or int(args.slots) < 1:
        if slots_source == 'file':
            args.slots = str(DEFAULT_SLOTS)
        else:
            parser.error('invalid slot count')
    args.slots = str(min(int(args.slots), MAX_SLOTS))
    if not args.wait.isascii() or not args.wait.isdecimal():
        parser.error('invalid wait seconds')
    if (args.run_timeout is not None and
            (not args.run_timeout.isascii() or not args.run_timeout.isdecimal()
             or not 1 <= int(args.run_timeout) <= 86400)):
        parser.error('invalid command run timeout seconds (expected 1..86400)')
    run_timeout = int(args.run_timeout) if args.run_timeout is not None else (
        FINAL_RUN_TIMEOUT_S if args.priority in ('final', 'release') else DEFAULT_RUN_TIMEOUT_S)
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
            ticket_created_at = time.monotonic()
            sequence, ticket_row = queue.new_ticket(
                state, args.priority, ticket_created_at)
            state['active'][name] = ticket_row
            # Retry affinity preserves the original ticket age. Report total
            # queue age for the run, matching the shared pool's FIFO state.
            queue_wait_started = ticket_row['created']
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
                        # Park our place before refusing (intake 1293b560
                        # AC1): delete our own dead row but keep the run
                        # record, so a retry with the same
                        # AGENT_HUB_HEAVY_TEST_RUN_ID reclaims the original
                        # sequence and age instead of re-queueing at the back.
                        fcntl.flock(mutex, fcntl.LOCK_EX)
                        try:
                            state = queue.load()
                            queue.park_refused(state, name, time.monotonic())
                            queue.save(state)
                        finally:
                            fcntl.flock(mutex, fcntl.LOCK_UN)
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
                admission_event = {
                    'event': 'heavy_test_admitted',
                    'protocol': ADMISSION_PROTOCOL_VERSION,
                    'ticket_sequence': sequence,
                    'priority': args.priority,
                    'queue_wait_s': round(max(0.0, time.monotonic() - queue_wait_started), 3),
                    'admitted_at_utc': datetime.now(timezone.utc).isoformat(
                        timespec='milliseconds').replace('+00:00', 'Z'),
                }
                # Keep the established human-readable line for older test
                # tooling, then emit the structured measurement record.
                print(f'capped-test-run: admitted {args.priority} ticket {sequence}',
                      file=sys.stderr, flush=True)
                print('capped-test-run: admission-event ' +
                      json.dumps(admission_event, sort_keys=True),
                      file=sys.stderr, flush=True)
                print(f'capped-test-run: run timeout {run_timeout}s', file=sys.stderr, flush=True)
                return run_command(command, slot, lambda: interrupted, run_timeout, identity)


if __name__ == '__main__':
    dispatch_to_active_release()
    try:
        sys.exit(main())
    except (OSError, ValueError, OverflowError, RecursionError) as error:
        # Direct Vitest/pytest hooks refuse exit 3 but historically treat exit
        # 2 as an unavailable wrapper. Queue corruption must never uncap them.
        # argparse usage errors still exit 2; command errors retain their code.
        print(f'capped-test-run: admission refused: {error}; inspect queue state/permissions under admission.lock; never remove lock files (see docs/HETZNER-OVERLOAD-0689CE9C.md)', file=sys.stderr)
        sys.exit(refuse('admission refused'))
