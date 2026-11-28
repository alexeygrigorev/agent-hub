"""One explicit Windows operator resume; not a worker, enrollment daemon or API.

No arguments is HOLD. Capsule check reads code only. Generation mode invokes
only the existing import-native --resume-generation route, which keeps all its
operator/host auth, approved-host and generation guards and normal refresh
semantics. No caller/config/provider identity is manufactured here.

Pending holds files ONLY while this caller/process lives. An ephemeral CLI
reports owner-exit custody loss, never durable retention or descendant closure.
The future actual outer Hub/job command/custody requires separate review.
"""
import json
import math
import re
import subprocess
import sys
import threading
import time

CHECK_SECONDS = 20
OPERATOR_SECONDS = 420  # Existing install300 + version30 + lock30 + transport allowance.
CLEANUP_SECONDS = 2
OUTPUT_BYTES = 4096
_PENDING = None


class Refused(RuntimeError):
    """Only a closed code may reach CLI output."""


class Pending:
    def __init__(self, lease, child, pump):
        self.lease, self.child, self.pump = lease, child, pump
        self.retention = 'owning_process_lifetime_only'


class Budget:
    def __init__(self, seconds, clock=None):
        self.clock = clock or time.monotonic
        self.start = self.last = self.now()
        self.deadline = self.start + seconds

    def now(self):
        try:
            value = self.clock()
        except Exception:
            raise Refused('clock_unavailable') from None
        if type(value) not in (int, float) or not math.isfinite(value):
            raise Refused('clock_invalid')
        return value

    def remaining(self):
        value = self.now()
        if value < self.last:
            raise Refused('clock_backward')
        self.last = value
        if value >= self.deadline:
            raise Refused('operation_deadline')
        return self.deadline - value


class _Pump:
    """One bounded reader for the owned child's stdout, never stderr or a file.

    The daemon thread is scoped to this one operation, not a background watcher.
    If EOF/reader settlement is unknown, Pending retains its owner while alive.
    """
    def __init__(self, pipe):
        self.pipe, self.raw = pipe, bytearray()
        self.overflow = self.failed = False
        self.thread = threading.Thread(target=self._read, name='claude-resume-output', daemon=True)

    def start(self):
        self.thread.start()

    def _read(self):
        try:
            while True:
                chunk = self.pipe.read(512)
                if not chunk:
                    break
                if not isinstance(chunk, bytes):
                    self.failed = True
                    break
                self.raw.extend(chunk[:OUTPUT_BYTES + 1 - len(self.raw)])
                if len(self.raw) > OUTPUT_BYTES:
                    self.overflow = True
                    break
        except BaseException:
            self.failed = True
        finally:
            try:
                self.pipe.close()
            except BaseException:
                self.failed = True

    def join(self, timeout):
        self.thread.join(timeout)
        return not self.thread.is_alive()


def _acquire():
    if sys.platform != 'win32':
        raise Refused('platform_unsupported')
    from ._release import HOST_VERSION, PROTOCOL_VERSION
    from .windows_release import WindowsRelease
    return WindowsRelease.open(HOST_VERSION, PROTOCOL_VERSION)


def _parse(argv):
    if argv == ['--check-only']:
        return True, None
    if (len(argv) == 2 and argv[0] == '--generation'
            and re.fullmatch(r'[1-9][0-9]{0,15}', argv[1])):
        value = int(argv[1])
        if value <= 2**53 - 1:
            return False, value
    raise Refused('invalid_operation')


def _result(raw, generation):
    def unique(items):
        value = {}
        for key, item in items:
            if key in value:
                raise Refused('operator_output_invalid')
            value[key] = item
        return value
    try:
        value = json.loads(raw, object_pairs_hook=unique,
            parse_constant=lambda _: (_ for _ in ()).throw(Refused('operator_output_invalid')))
    except (ValueError, UnicodeError):
        raise Refused('operator_output_invalid') from None
    keys = {'status', 'provider', 'label', 'credential_generation', 'machine_id', 'acked_generation'}
    if (not isinstance(value, dict) or set(value) != keys or value.get('status') != 'resumed'
            or value.get('provider') != 'claude' or value.get('label') != 'hub-claude'
            or type(value.get('credential_generation')) is not int or value['credential_generation'] != generation
            or type(value.get('acked_generation')) is not int or value['acked_generation'] != generation
            or not isinstance(value.get('machine_id'), str) or not value['machine_id']
            or value['machine_id'] != value['machine_id'].strip() or len(value['machine_id']) > 128
            or any(char.isspace() or ord(char) < 32 for char in value['machine_id'])):
        raise Refused('operator_output_invalid')
    # Never echo machine identifiers, CLI text, error messages, paths or tokens.
    return {'status': 'resume_completed', 'generation': generation,
            'qualification': 'fixed_cli_result_only', 'descendant_custody': 'unproven'}


def _cleanup(child, pump, budget):
    """Only this Popen handle; no numeric-PID/group or descendant signaling."""
    try:
        now = budget.now()
        if now < budget.last:
            raise Refused('clock_backward')
        until = min(budget.deadline + CLEANUP_SECONDS, now + CLEANUP_SECONDS)
        previous = [now]
        def remaining():
            try:
                current = budget.now()
                if current < previous[0]:
                    return 0
                previous[0] = current
                return max(0, until - current)
            except BaseException:
                return 0
    except BaseException:
        def remaining():
            return 0
    if child is not None:
        try:
            if child.poll() is None:
                child.kill()
        except BaseException:
            pass
        try:
            child.wait(timeout=remaining())
        except BaseException:
            pass
    reader_done = child is None and pump is None
    if pump is not None:
        try:
            reader_done = pump.join(remaining())
        except BaseException:
            reader_done = False
    elif child is not None:
        # Pump construction/start may fail after acquiring the pipe. Transfer
        # ownership before closing; never retry a possibly consumed descriptor.
        try:
            pipe, child.stdout = child.stdout, None
            if pipe is not None:
                pipe.close()
            reader_done = True
        except BaseException:
            reader_done = False
    try:
        return child is not None and child.poll() is not None and reader_done
    except BaseException:
        return False


def operate(check_only, generation, *, acquire=None, spawn=None, pump_factory=None, clock=None, sleep=None):
    """Internal API: return (closed result, original Budget), or raise.

    On uncertainty the original exception carries .pending and this module also
    retains it. Caller must not retry. Process exit loses that custody; no cleanup
    or all-descendant proof is implied by a reaped leader after a failure.
    """
    global _PENDING
    if _PENDING is not None:
        raise Refused('pending_custody')
    if type(check_only) is not bool or (check_only and generation is not None) or (
            not check_only and (type(generation) is not int or not 1 <= generation <= 2**53 - 1)):
        raise Refused('invalid_operation')
    budget = Budget(CHECK_SECONDS if check_only else OPERATOR_SECONDS, clock)
    from .windows_release import WindowsRelease
    lease = (acquire or _acquire)()
    if type(lease) is not WindowsRelease:
        raise Refused('artifact_unavailable')
    child = pump = None
    attempted = borrowed = False
    failure = None
    try:
        lease.check()
        budget.remaining()
        if check_only:
            result = {'status': 'capsule_ready', 'credentials_or_provider_actions': False}
        else:
            lease.borrowers += 1
            borrowed = True
            argv = [lease.python, '-I', '-S', lease.archive, 'engines', 'import-native',
                    '--provider', 'claude', '--label', 'hub-claude', '--resume-generation', str(generation)]
            # No env rewrite: all existing operator/worker/profile auth guards
            # remain effective. No token/config selector or arbitrary argv added.
            budget.remaining()
            attempted = True
            child = (spawn or subprocess.Popen)(argv, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, close_fds=True,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            pump = (pump_factory or _Pump)(child.stdout)
            pump.start()
            while child.poll() is None:
                if pump.overflow or pump.failed:
                    raise Refused('operator_output_invalid')
                (sleep or time.sleep)(min(0.02, budget.remaining()))
            child.wait(timeout=budget.remaining())
            if not pump.join(budget.remaining()):
                raise Refused('reader_unsettled')
            budget.remaining()
            if pump.overflow or pump.failed or len(pump.raw) > OUTPUT_BYTES:
                raise Refused('operator_output_invalid')
            if type(child.returncode) is not int or child.returncode != 0:
                raise Refused('operator_failed')
            result = _result(bytes(pump.raw), generation)
            lease.check()
    except BaseException as error:
        failure = error
        settled = not attempted or _cleanup(child, pump, budget)
        if attempted and not settled:
            _PENDING = Pending(lease, child, pump)
            error.pending = _PENDING
        raise
    finally:
        if _PENDING is None or _PENDING.lease is not lease:
            if borrowed:
                lease.borrowers -= 1
            try:
                lease.close()
            except BaseException:
                if failure is None:
                    raise
    # Closes may block: never qualify success before they finish.
    budget.remaining()
    return result, budget


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print('{"status":"HOLD","reason":"explicit_operation_required"}')
        return 2
    try:
        check, generation = _parse(argv)
        result, budget = operate(check, generation)
        output = json.dumps(result, sort_keys=True)
        budget.remaining()  # Original clock immediately before success output.
        print(output)
        return 0
    except BaseException:
        print(json.dumps({'status': 'UNKNOWN', 'reason': 'operator_operation_unqualified',
            'owner_exit_custody_loss': _PENDING is not None,
            'retention': 'owning_process_lifetime_only' if _PENDING is not None else 'none_claimed',
            'descendant_custody': 'unproven', 'automatic_retry': False}, sort_keys=True))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
