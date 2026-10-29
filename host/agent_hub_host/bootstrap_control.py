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


def recover_stale_owner(directory, current_boot, observe):
    """Caller must hold owner.lock: an absent owner process alone is no proof."""
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
                if (isinstance(row, dict) and all(row.get(k) == receipt[k] for k in IDENTITY_KEYS)
                        and row.get('containment_empty') is True):
                    proof = 'exact_receipt_containment_empty'
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

    def __enter__(self):
        try:
            self.stack.enter_context(private_fs.exclusive_lock(self.directory / 'owner.lock'))
            self.boot = self.boot_probe()
            recover_stale_owner(self.directory, self.boot, self.observe)
            return self
        except BaseException:
            self.stack.close()
            raise

    def __exit__(self, *args):
        self.stack.close()

    def publish(self, receipt):
        _write(self.directory / 'owner.json', {'version': 2, 'generation': self.generation,
               'boot_identity': self.boot, 'receipt': {k: receipt[k] for k in IDENTITY_KEYS}})

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


def handover(directory, *, timeout=90, boot_probe=None, observe=None):
    directory = Path(directory)
    if not 0 <= timeout <= MAX_HANDOVER_SECONDS:
        raise ValueError('Invalid controller handover timeout')
    # Serializes request publication/cancellation; never unlink kernel locks.
    with private_fs.exclusive_lock(directory / 'request.lock'):
        _handover(directory, timeout, boot_probe or boot_identity, observe or observe_receipt)


def _handover(directory, timeout, boot_probe, observe):
    stack = ExitStack()
    try:
        stack.enter_context(private_fs.exclusive_lock(directory / 'owner.lock'))
    except OSError:
        stack.close()
    else:
        with stack:
            recover_stale_owner(directory, boot_probe(), observe)
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
            time.sleep(0.25)
        raise RuntimeError('Controller handover unconfirmed; existing controller and evidence preserved')
    finally:
        # A timed-out, cancelled, or crashed installer cannot leave a deferred
        # shutdown request: remove ours on unwind; expiry covers hard kills.
        try:
            if _read(directory / 'stop.json') == request:
                (directory / 'stop.json').unlink()
        except FileNotFoundError:
            pass
