"""Cooperative handover: only the supervisor can stop its own in-memory child.

The installer never kills a PID or selects a session by tag. A private nonce
addresses the live owner; the owner acknowledges only after native containment
is empty. Unknown/crashed owners retain their evidence and fail closed.
"""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import re
import subprocess
import time
import uuid
from . import private_fs

IDENTITY_KEYS = ('id', 'workspace', 'tag', 'engine', 'created_at_ms', 'command')
# Native `workspace` is observed and mutable (aplexer 0.1.23); never match on it.
MATCH_KEYS = tuple(k for k in IDENTITY_KEYS if k != 'workspace')
MAX_HANDOVER_SECONDS = 90


class NeedsAttention(RuntimeError):
    """Positive exit proof is unavailable; preserve the controller evidence."""


def boot_identity():
    # Read-only Windows boot time, not an estimate from wall clock - uptime.
    # https://learn.microsoft.com/windows/win32/cimwin32prov/win32-operatingsystem
    if os.name != 'nt':
        return None
    command = ('$ErrorActionPreference="Stop"; '
               '(Get-CimInstance -ClassName Win32_OperatingSystem).LastBootUpTime.'
               'ToUniversalTime().ToString("o",[Globalization.CultureInfo]::InvariantCulture)')
    try:
        result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', command],
                                capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL)
        value = result.stdout.strip()
        if not result.returncode and re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{7}(?:Z|\+00:00)', value):
            return 'windows-boot:' + value
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None


def observe_receipt(receipt):
    # Recovery is strictly read-only. Persisted IDs never authorize a kill.
    result = subprocess.run(['aplexer', 'status', receipt['id'], '--json'],
                            capture_output=True, text=True, timeout=15, stdin=subprocess.DEVNULL)
    if result.returncode or len(result.stdout) > 131072:
        return None
    return json.loads(result.stdout)


def receipt_absent(receipt):
    """True only when a successful full catalog listing has no row for the receipt id."""
    try:
        result = subprocess.run(['aplexer', 'list', '--all', '--json'],
                                capture_output=True, text=True, timeout=15, stdin=subprocess.DEVNULL)
        if result.returncode or len(result.stdout) > 1048576:
            return False
        rows = json.loads(result.stdout)
        return isinstance(rows, list) and all(isinstance(r, dict) for r in rows) and \
            all(r.get('id') != receipt['id'] for r in rows)
    except (OSError, ValueError, subprocess.TimeoutExpired, KeyError):
        return False


def pending_tag_absent(tag):
    """True only when a successful full catalog listing has no row with this tag."""
    try:
        result = subprocess.run(['aplexer', 'list', '--all', '--json'],
                                capture_output=True, text=True, timeout=15, stdin=subprocess.DEVNULL)
        if result.returncode or len(result.stdout) > 1048576:
            return False
        rows = json.loads(result.stdout)
        return isinstance(rows, list) and all(isinstance(r, dict) for r in rows) and \
            all(r.get('tag') != tag for r in rows)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return False


# The installer announces a task replacement so a supervisor between retry
# attempts (no lease held, nothing to hand over) and the controller.ps1 restart
# loop exit with 76 instead of keeping the task Running (intake 88ff6766).
REPLACE_MARKER = 'replace-pending'
REPLACE_MAX_AGE_S = 120


def request_replace(directory):
    _write(Path(directory) / REPLACE_MARKER, {'requested_at': time.time()})


def clear_replace(directory):
    try:
        (Path(directory) / REPLACE_MARKER).unlink()
    except FileNotFoundError:
        pass


def replace_pending(directory, now=None):
    try:
        age = (now if now is not None else time.time()) - (Path(directory) / REPLACE_MARKER).stat().st_mtime
    except OSError:
        return False
    return -5 <= age <= REPLACE_MAX_AGE_S


def _read(path):
    return json.loads(private_fs.read_bytes(path, 16384))


def _write(path, value):
    private_fs.atomic_write(path, json.dumps(value).encode(), 16384)


def _generation(owner):
    if not isinstance(owner, dict) or owner.get('version') not in (1, 2):
        raise ValueError('Invalid controller ownership record; preserved')
    value = owner.get('generation')
    if not isinstance(value, str) or not re.fullmatch('[a-f0-9]{32}', value):
        raise ValueError('Invalid controller ownership record; preserved')
    return value


def _write_attention(directory, generation=None):
    _write(directory / 'needs-attention.json', {'status': 'needs_attention',
           'reason': 'controller_exit_unproven', 'generation': generation,
           'action': 'Inspect controller.log and the exact native receipt; restore native status access, then rerun. Do not delete owner evidence.'})


def _attention(directory, generation=None):
    _write_attention(directory, generation)
    raise NeedsAttention('Previous controller exit is unproven; inspect needs-attention.json and restore native status access before retrying')


def stop_orphan_receipt(receipt, signal='TERM', grace_ms=30000):
    """Ask aplexer to stop the exact orphaned controller; the reply is only a hint, the caller re-observes."""
    result = subprocess.run(['aplexer', 'kill', receipt['id'], '--signal', signal, '--grace-ms', str(grace_ms)],
                            capture_output=True, text=True, timeout=45, stdin=subprocess.DEVNULL)
    if result.returncode or len(result.stdout) > 131072:
        return None
    return json.loads(result.stdout)


def journal_lock_held(journal):
    """True when another process holds the controller's exclusive journal lock (host.lock)."""
    path = Path(journal) / 'host.lock'
    if not path.exists():
        return False
    try:
        with path.open('a') as lock:
            if os.name == 'nt':
                import msvcrt
                if os.fstat(lock.fileno()).st_size == 0:
                    return False
                os.lseek(lock.fileno(), 0, os.SEEK_SET)
                try:
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError:
                    return True
                os.lseek(lock.fileno(), 0, os.SEEK_SET)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
                return False
            import fcntl
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return True
            fcntl.flock(lock, fcntl.LOCK_UN)
            return False
    except OSError:
        return False


def recover_stale_owner(directory, current_boot, observe, absent=None, tag_absent=None, stop=None):
    """Caller must hold owner.lock: an absent owner process alone is no proof.

    stop (installer replace only): when the lock is free but the exact receipt
    is still a LIVE orphan (its supervisor died, 2026-11-02 11:23), stop that
    exact receipt through aplexer, as the supervisor's own confirm_owned_exit
    does, then prove exit only from a fresh observation or catalog absence.
    """
    try:
        owner = _read(directory / 'owner.json')
    except FileNotFoundError:
        return
    except ValueError:
        _attention(directory)
    try:
        generation = _generation(owner)
    except ValueError:
        _attention(directory)
    proof = None
    previous_boot = owner.get('boot_identity')
    if (isinstance(previous_boot, str) and previous_boot and
            isinstance(current_boot, str) and current_boot and previous_boot != current_boot):
        proof = 'boot_changed'
    else:
        try:
            stopped = _read(directory / 'stopped.json')
        except (OSError, ValueError):
            stopped = None
        if stopped == {'generation': generation, 'containment_empty': True}:
            proof = 'owned_exit_acknowledged'
        else:
            receipt = owner.get('receipt')
            if isinstance(receipt, dict) and all(k in receipt for k in IDENTITY_KEYS):
                try:
                    row = observe(receipt)
                except Exception:
                    row = None
                if (isinstance(row, dict) and all(row.get(k) == receipt[k] for k in MATCH_KEYS)
                        and row.get('containment_empty') is True):
                    proof = 'exact_receipt_containment_empty'
                elif (stop is not None and isinstance(row, dict)
                        and all(row.get(k) == receipt[k] for k in MATCH_KEYS)
                        and row.get('containment_empty') is not True):
                    proof = _stop_live_orphan(receipt, observe, absent, stop)
                elif row is None and absent is not None and absent(receipt):
                    # Rerun recovery: caller holds owner.lock (no live owner) and a
                    # full catalog listing no longer contains the exact receipt id.
                    proof = 'receipt_absent_from_catalog'
            elif (receipt is None and isinstance(owner.get('pending_tag'), str)
                    and tag_absent is not None and tag_absent(owner['pending_tag'])):
                # A start that failed or timed out before a trusted receipt:
                # safe only when no catalog row carries the unique tag.
                proof = 'pending_tag_absent_from_catalog'
    if proof is None:
        _attention(directory, generation)
    # Archive before clearing; recovery never destroys the old receipt.
    _write(directory / 'archive' / (generation + '-' + uuid.uuid4().hex + '.json'),
           {'owner': owner, 'proof': proof, 'observed_boot_identity': current_boot})
    (directory / 'owner.json').unlink()
    try:
        (directory / 'needs-attention.json').unlink()
    except FileNotFoundError:
        pass


def _stop_live_orphan(receipt, observe, absent, stop):
    try:
        reply = stop(receipt)
    except Exception:
        reply = None
    if isinstance(reply, dict) and reply.get('id') == receipt['id'] and reply.get('record_removed') is True:
        return 'orphan_stopped_record_removed'
    try:
        row = observe(receipt)
    except Exception:
        return None
    if isinstance(row, dict):
        if all(row.get(k) == receipt[k] for k in MATCH_KEYS) and row.get('containment_empty') is True:
            return 'orphan_stopped_containment_empty'
        return None
    if row is None and absent is not None and absent(receipt):
        return 'orphan_stopped_receipt_absent'
    return None


def _force_stop_unresponsive(directory, owner, stop, boot_probe, observe, absent, tag_absent, force_wait, sleep):
    """Cooperative stop timed out on a busy host: kill the exact receipt, then prove exit as the supervisor does.

    TERM first, then KILL; the old owner must release owner.lock (bounded) and the usual receipt proof
    (record removed / containment empty / absent from catalog) must hold before the lease is cleared.
    """
    receipt = owner.get('receipt') if isinstance(owner, dict) else None
    if not isinstance(receipt, dict) or not all(k in receipt for k in IDENTITY_KEYS):
        return False
    end = time.monotonic() + force_wait
    for signal in ('TERM', 'KILL'):
        try:
            stop(receipt, signal=signal) if _accepts_signal(stop) else stop(receipt)
        except Exception:
            pass
        step_end = min(end, time.monotonic() + max(force_wait / 2, 0))
        while True:
            stack = ExitStack()
            try:
                stack.enter_context(private_fs.exclusive_lock(directory / 'owner.lock'))
            except OSError:
                stack.close()
            else:
                with stack:
                    try:
                        recover_stale_owner(directory, boot_probe(), observe, absent, tag_absent, stop)
                    except NeedsAttention:
                        return False
                return True
            if time.monotonic() >= step_end:
                break
            sleep(0.5)
    return False


def _accepts_signal(fn):
    import inspect
    try:
        return 'signal' in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False


def _fresh_request(request, generation):
    if not isinstance(request, dict) or request.get('generation') != generation:
        return False
    created, expires = request.get('created_monotonic'), request.get('expires_monotonic')
    return (type(created) in (float, int) and type(expires) in (float, int)
            and 0 < expires - created <= MAX_HANDOVER_SECONDS
            and created <= time.monotonic() < expires)


class ControllerLease:
    def __init__(self, directory, *, boot_probe=None, observe=None):
        self.directory = Path(directory)
        self.generation = uuid.uuid4().hex
        self.stack = ExitStack()
        self.boot_probe = boot_probe or boot_identity
        self.observe = observe or observe_receipt
        self.absent = receipt_absent if observe is None else None
        self.tag_absent = pending_tag_absent if observe is None else None

    def __enter__(self):
        try:
            self.stack.enter_context(private_fs.exclusive_lock(self.directory / 'owner.lock'))
            self.boot = self.boot_probe()
            recover_stale_owner(self.directory, self.boot, self.observe, self.absent, self.tag_absent)
            return self
        except BaseException:
            self.stack.close()
            raise

    def __exit__(self, *args):
        self.stack.close()

    def publish(self, receipt):
        _write(self.directory / 'owner.json', {'version': 2, 'generation': self.generation,
               'boot_identity': self.boot, 'receipt': {k: receipt[k] for k in IDENTITY_KEYS}})

    def publish_pending(self, tag):
        # The native start may have created a child we hold no receipt for;
        # keep the unique tag so recovery never starts a duplicate blindly.
        _write(self.directory / 'owner.json', {'version': 2, 'generation': self.generation,
               'boot_identity': self.boot, 'receipt': None, 'pending_tag': tag})
        _write_attention(self.directory, self.generation)

    def stop_requested(self):
        try:
            return _fresh_request(_read(self.directory / 'stop.json'), self.generation)
        except (OSError, ValueError):
            return False

    def mark_unproven(self):
        _write_attention(self.directory, self.generation)

    def confirm_exit(self):
        # Called only by the live owner after validating its exact native
        # receipt and the post-kill Job Object empty witness.
        _write(self.directory / 'stopped.json', {'generation': self.generation, 'containment_empty': True})
        (self.directory / 'owner.json').unlink()


def handover(directory, *, timeout=90, boot_probe=None, observe=None, stop=None, journal=None, force_wait=60,
             lock_held=None, sleep=None):
    # Resolved per call: a default bound at import ignores patches of time.sleep (hung the frozen-clock test).
    sleep = sleep or time.sleep
    absent = receipt_absent if observe is None else None
    tag_absent = pending_tag_absent if observe is None else None
    if stop is None and observe is None:
        stop = stop_orphan_receipt
    directory = Path(directory)
    if not 0 <= timeout <= MAX_HANDOVER_SECONDS:
        raise ValueError('Invalid controller handover timeout')
    # Serializes request publication/cancellation; never unlink kernel locks.
    with private_fs.exclusive_lock(directory / 'request.lock'):
        _handover(directory, timeout, boot_probe or boot_identity, observe or observe_receipt, absent, tag_absent, stop,
                  force_wait, sleep)
    if journal is not None:
        # The new controller dies on a held host.lock (desktop: ~10.5 min of PermissionError);
        # wait for it bounded instead of starting into it.
        held = lock_held or journal_lock_held
        end = time.monotonic() + force_wait
        while held(journal):
            if time.monotonic() >= end:
                raise NeedsAttention('Old controller still holds the journal lock host.lock; retry the install')
            sleep(0.5)


def _handover(directory, timeout, boot_probe, observe, absent=None, tag_absent=None, stop=None, force_wait=60,
              sleep=None):
    sleep = sleep or time.sleep
    stack = ExitStack()
    try:
        stack.enter_context(private_fs.exclusive_lock(directory / 'owner.lock'))
    except OSError:
        stack.close()
    else:
        with stack:
            recover_stale_owner(directory, boot_probe(), observe, absent, tag_absent, stop)
        return
    try:
        owner = _read(directory / 'owner.json')
    except FileNotFoundError:
        # A live owner may still be between acquiring its lock and publishing
        # its receipt. Never report a successful handover in that interval.
        raise NeedsAttention('Controller is starting; retry handover after its receipt is published') from None
    generation = _generation(owner)
    deadline = time.monotonic() + timeout
    request = {'generation': generation, 'request_id': uuid.uuid4().hex,
               'created_monotonic': deadline - timeout, 'expires_monotonic': deadline}
    _write(directory / 'stop.json', request)
    try:
        while time.monotonic() < deadline:
            try:
                stopped = _read(directory / 'stopped.json')
            except FileNotFoundError:
                stopped = None
            if stopped == {'generation': generation, 'containment_empty': True}:
                # Also require the owner process to release its kernel lock.
                # The task-state gate then waits for PowerShell itself to exit.
                try:
                    with private_fs.exclusive_lock(directory / 'owner.lock'):
                        if not (directory / 'owner.json').exists():
                            return
                except OSError:
                    pass
            sleep(0.25)
        if stop is not None and _force_stop_unresponsive(directory, owner, stop, boot_probe, observe, absent,
                                                         tag_absent, force_wait, sleep):
            return
        raise RuntimeError('Controller handover unconfirmed; existing controller and evidence preserved')
    finally:
        # A timed-out, cancelled, or crashed installer cannot leave a deferred
        # shutdown request: remove ours on unwind; expiry covers hard kills.
        try:
            if _read(directory / 'stop.json') == request:
                (directory / 'stop.json').unlink()
        except FileNotFoundError:
            pass
