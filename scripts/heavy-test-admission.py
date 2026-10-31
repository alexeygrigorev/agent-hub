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

NORMAL_AGE_S = 300
# Refusal channel (intake 9432731e): exit 3 alone is ambiguous because the
# admitted command may itself exit 3 (pytest INTERNALERROR). When this names a
# path, a refusal before the command starts writes it; the command never sees
# the variable, so its own exit 3 leaves no marker.
REFUSAL_FILE_ENV = 'AGENT_HUB_HEAVY_TEST_REFUSAL_FILE'
COMMAND_STARTED = False


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
    def head(state, now):
        # An aged normal joins the final class with its original sequence.
        # Later final arrivals cannot indefinitely overtake that normal.
        def order(item):
            row = item[1]
            preferred = row['priority'] == 'final' or now - row['created'] >= NORMAL_AGE_S
            return (not preferred, row['sequence'])
        return min(state['active'].items(), key=order)[0]


def run_command(command, slot, interrupted):
    global COMMAND_STARTED
    # Reserve/inherit the historical FD9. Parent retains its own descriptor
    # even when the command closes FD9. Descendants retaining it also keep
    # the slot after an uncatchable wrapper-only crash.
    os.dup2(slot.fileno(), 9)
    env = dict(os.environ, AGENT_HUB_HEAVY_TEST_HELD='1')
    env.pop(REFUSAL_FILE_ENV, None)
    COMMAND_STARTED = True
    try:
        try:
            child = subprocess.Popen(command, pass_fds=(9,), env=env)
        except OSError as error:
            print(f'capped-test-run: {error}', file=sys.stderr)
            return 127 if isinstance(error, FileNotFoundError) else 126
        result = child.wait()
        return interrupted() or (128 - result if result < 0 else result)
    finally:
        os.close(9)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--slots', default=os.environ.get('AGENT_HUB_HEAVY_TEST_SLOTS', '2'))
    parser.add_argument('--wait', default=os.environ.get('AGENT_HUB_HEAVY_TEST_WAIT_S', '1800'))
    parser.add_argument('--priority', default=os.environ.get('AGENT_HUB_HEAVY_TEST_PRIORITY', 'normal'), choices=['normal', 'final'])
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if not args.slots.isascii() or not args.slots.isdecimal() or int(args.slots) < 1:
        parser.error('invalid slot count')
    if not args.wait.isascii() or not args.wait.isdecimal():
        parser.error('invalid wait seconds')
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    if not command:
        parser.error('no command given')
    root = Path(os.environ.get('AGENT_HUB_HEAVY_TEST_DIR', f'/run/user/{os.getuid()}/agent-hub/heavy-test'))
    root.mkdir(parents=True, exist_ok=True)
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
                        print(f'capped-test-run: all {args.slots} heavy-test slots busy; waiting up to {args.wait}s ({args.priority} ticket {sequence})', file=sys.stderr, flush=True)
                        announced = True
                    time.sleep(min(random.uniform(1, 2), remaining))
            with slot:
                running = True
                print(f'capped-test-run: admitted {args.priority} ticket {sequence}', file=sys.stderr, flush=True)
                return run_command(command, slot, lambda: interrupted)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError, OverflowError, RecursionError) as error:
        # Direct Vitest/pytest hooks refuse exit 3 but historically treat exit
        # 2 as an unavailable wrapper. Queue corruption must never uncap them.
        # argparse usage errors still exit 2; command errors retain their code.
        print(f'capped-test-run: admission refused: {error}; inspect queue state/permissions under admission.lock; never remove lock files (see docs/HETZNER-OVERLOAD-0689CE9C.md)', file=sys.stderr)
        sys.exit(refuse('admission refused'))
