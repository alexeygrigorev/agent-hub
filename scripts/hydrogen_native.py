"""Hydrogen native display adapter: acquirer callbacks over the existing native/Hub CLIs.

Integrated from Root's private native-adapter proposal (manifest sha256
515360f93c1be1910e8dfb38eebe8c3402dae954f9b5aa8b04993d559439a141, adapter
sha256 6e0dd63fd76092bb87fc9df324cc781aeb90f5d792f73906c94388e65de4838b) with
the independent review blocker N1 closed: EVERY Intent passed to a callback is
strictly validated before equality or any effect, and the durable fence name and
receipt are derived only from the frozen, validated ``self.intent``.

Only these existing native commands are used, all with the full canonical
native UUID and through ``runner.run_bounded`` (stream-capped, deadline-bounded
collector that reaps only its own CLI child; there is no fallback to the
unbounded ``Runner.run``):

* ``a status <uuid> --json``    positive running/reachable worker observation
* ``a capture <uuid> --screen --svg``  one live styled screen buffer
* ``a send <uuid> --hex 0f --json``    exactly one raw byte, no Enter

Hub identity comes from the existing ``hub sessions show <sid> --json`` Machine
row (``operation_id`` is the ATTEMPT id there). No ping CLI, endpoint field,
request digest or provider version is invented: native birth comes from native
status, provider/client executable pins from the Root-attested transport record.
"""
import dataclasses
import hashlib
import json
import math
import os
import stat

import hydrogen_acquire as acquire_api

SVG_CAP = 65536
JSON_CAP = 65536
ERROR_CAP = 4096


class Refused(Exception):
    """Only fixed nonsecret reason codes cross this boundary."""


def require(value, code):
    if not value:
        raise Refused(code)


def strict_json(raw):
    require(type(raw) is bytes and 0 < len(raw) <= JSON_CAP, 'json_size')
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, 'duplicate_json')
            result[key] = value
        return result
    def bad(_):
        raise Refused('nonfinite_json')
    def finite_float(value):
        number = float(value)
        require(math.isfinite(number), 'nonfinite_json')
        return number
    try:
        result = json.loads(raw.decode('utf-8'), object_pairs_hook=pairs,
                            parse_constant=bad, parse_float=finite_float)
    except (ValueError, UnicodeError, RecursionError):
        raise Refused('json_invalid') from None
    require(type(result) is dict, 'json_shape')
    return result


def exact(row, key, expected):
    require(key in row and type(row[key]) is type(expected) and row[key] == expected,
            'identity_mismatch')


def open_private_directory(path):
    """Existing trusted directory only; no mkdir, chmod, cleanup or recovery."""
    import grp
    import pwd
    require(type(path) is str and path.startswith('/') and '..' not in path.split('/'), 'fence_path')
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        parts = [p for p in path.split('/') if p]
        require(bool(parts), 'fence_path')
        for part in parts:
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nxt
            m = os.fstat(fd)
            require(m.st_uid in (0, os.geteuid()) and not m.st_mode & 0o002, 'untrusted_ancestor')
            if m.st_mode & 0o020:
                me = pwd.getpwuid(os.geteuid())
                members = set(grp.getgrgid(m.st_gid).gr_mem)
                members.update(p.pw_name for p in pwd.getpwall() if p.pw_gid == m.st_gid)
                require(m.st_gid == me.pw_gid and members == {me.pw_name}, 'untrusted_group')
        m = os.fstat(fd)
        require(m.st_uid == os.geteuid() and stat.S_IMODE(m.st_mode) == 0o700, 'fence_not_private')
        return fd
    except BaseException:
        os.close(fd)
        raise


def fence_name(intent):
    """Stable per ORIGINAL owned attempt: [Hub SID, attempt operation, epoch] of a validated intent."""
    acquire_api._valid_intent(intent)
    stable = json.dumps([intent.identity.hub_session, intent.identity.operation, intent.identity.epoch],
                        separators=(',', ':')).encode()
    return hashlib.sha256(stable).hexdigest() + '.attempt.json'


class Adapter:
    """Callbacks for ``hydrogen_acquire.acquire``.

    runner.run_bounded(tool, args, *, deadline, timeout, max_stdout, max_stderr)
    must stream-cap and reap its own CLI child. read_hub returns the actual
    Machine ``sessions show`` row. artifact_reader returns the sha256 of the
    independently validated canonical artifact. verify_public_pins verifies the
    Root-attested client/provider executables. known_human is the controller's
    tracked intervention flag, not an atomic proof that nobody could attach.
    """

    def __init__(self, *, intent, runner, read_hub, artifact_reader,
                 verify_public_pins, known_human, clock, fence_directory):
        acquire_api._valid_intent(intent)
        require(callable(getattr(runner, 'run_bounded', None)), 'bounded_runner_required')
        self.intent, self.runner = intent, runner
        self.hub, self.artifact, self.pins = read_hub, artifact_reader, verify_public_pins
        self.known_human, self.clock, self.fence_directory = known_human, clock, fence_directory
        self._permit = False
        self._record_deadline = None
        self.sent = 0

    def _same_intent(self, intent):
        # N1: validate the CALLER's object before equality (7.0 == 7 and True == 1 in Python).
        try:
            acquire_api._valid_intent(intent)
        except acquire_api.Refused:
            raise Refused('intent_invalid') from None
        require(type(intent) is acquire_api.Intent and intent == self.intent, 'intent_changed')

    def _remaining(self, deadline, timeout):
        now = self.clock()
        require(type(deadline) in (int, float) and type(timeout) in (int, float)
                and type(now) in (int, float) and math.isfinite(deadline)
                and math.isfinite(timeout) and math.isfinite(now)
                and timeout > 0 and deadline > now, 'deadline')
        return min(timeout, deadline - now)

    def _call(self, callback, *args, deadline, timeout):
        remaining = self._remaining(deadline, timeout)
        result = callback(*args, deadline=deadline, timeout=remaining)
        self._remaining(deadline, remaining)
        return result

    def _native(self, args, cap, *, deadline, timeout):
        remaining = self._remaining(deadline, timeout)
        result = self.runner.run_bounded('a', args, deadline=deadline, timeout=remaining,
                                         max_stdout=cap, max_stderr=ERROR_CAP)
        self._remaining(deadline, remaining)
        require(type(result) is tuple and len(result) == 3, 'runner_shape')
        rc, out, err = result
        require(type(rc) is int and type(out) is bytes and type(err) is bytes, 'runner_shape')
        require(len(out) <= cap and len(err) <= ERROR_CAP, 'runner_cap_violation')
        # Never print/store stderr or native status raw output.
        require(rc == 0, 'native_cli_failed')
        return out

    def read_identity(self, *, deadline, timeout):
        i = self.intent.identity
        require(self._call(self.pins, i, deadline=deadline, timeout=timeout) is True, 'public_pins_changed')
        require(self._call(self.known_human, deadline=deadline, timeout=timeout) is False,
                'known_human_intervention')
        hub = self._call(self.hub, i.hub_session, deadline=deadline, timeout=timeout)
        require(type(hub) is dict, 'hub_shape')
        for key, value in {'id': i.hub_session, 'intake_id': i.intake,
                           'project_id': 'alexeygrigorev/agent-hub',
                           'machine_id': i.machine, 'epoch': i.epoch,
                           'operation_id': i.operation, 'native_id': i.native,
                           'engine': i.engine, 'model': i.model,
                           'workspace': i.workspace, 'state': 'running'}.items():
            exact(hub, key, value)
        status = strict_json(self._native(['status', i.native, '--json'], JSON_CAP,
                                          deadline=deadline, timeout=timeout))
        for key, value in {'id': i.native, 'tag': i.tag, 'workspace': i.workspace,
                           'created_at_ms': i.birth_ms, 'engine': i.engine,
                           'phase': 'running', 'state': 'running',
                           'worker_alive': True, 'worker_reachable': True}.items():
            exact(status, key, value)
        require('rpc_error' not in status, 'native_status_fallback')
        return acquire_api.Observation(i, True, True, False)

    def capture(self, *, deadline, timeout):
        self.read_identity(deadline=deadline, timeout=timeout)
        raw = self._native(['capture', self.intent.identity.native, '--screen', '--svg'], SVG_CAP,
                           deadline=deadline, timeout=timeout)
        require(raw.startswith((b'<svg', b'<ns0:svg')), 'capture_not_svg')
        # No --plain, history, --json envelope, ANSI strip, fallback or tail join.
        self.read_identity(deadline=deadline, timeout=timeout)
        return acquire_api.Snapshot(self.intent.identity, raw, True)

    def validate_artifact(self, intent, *, deadline, timeout):
        self._same_intent(intent)
        value = self._call(self.artifact, self.intent, deadline=deadline, timeout=timeout)
        require(type(value) is str and len(value) == 64
                and all(c in '0123456789abcdef' for c in value), 'artifact_invalid')
        return value

    def record_attempt(self, intent, before, key, *, deadline, timeout):
        self._permit = False
        self._same_intent(intent)
        require(type(key) is bytes and key == b'\x0f', 'intent_changed')
        require(type(before) is acquire_api.Snapshot, 'capture_binding')
        acquire_api._valid_identity(before.identity)
        require(before.identity == self.intent.identity and before.complete is True
                and type(before.svg) is bytes and 0 < len(before.svg) <= SVG_CAP, 'capture_binding')
        self._remaining(deadline, timeout)
        frozen = self.intent
        # Stable original attempt key from the FROZEN validated intent; no new key on recovery.
        # No raw SVG, prompt, status, environment, secret or stderr in this receipt.
        payload = json.dumps({'schema': 1, 'identity': dataclasses.asdict(frozen.identity),
                              'command': frozen.command,
                              'original_deadline': frozen.deadline, 'acquisition_deadline': deadline,
                              'key_hex': '0f', 'before_svg_sha256': hashlib.sha256(before.svg).hexdigest(),
                              'state': 'ATTEMPT_RESERVED_OUTCOME_UNKNOWN'},
                             allow_nan=False, sort_keys=True, separators=(',', ':')).encode() + b'\n'
        name = fence_name(frozen)
        directory = open_private_directory(self.fence_directory)
        try:
            self._remaining(deadline, timeout)
            try:
                fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=directory)
            except FileExistsError:
                return False  # even an incomplete prior record blocks mutation
            try:
                os.fchmod(fd, 0o600)
                offset = 0
                while offset < len(payload):
                    count = os.write(fd, payload[offset:])
                    require(count > 0, 'fence_write')
                    offset += count
                os.fsync(fd)
            finally:
                os.close(fd)
            os.fsync(directory)
            self._remaining(deadline, timeout)
            self._record_deadline = deadline
            self._permit = True
            return True
        finally:
            os.close(directory)

    def send(self, identity, key, *, deadline, timeout):
        # Consume before ANY external call, even if the call fails/ACK is lost.
        permit = self._permit
        self._permit = False
        require(permit and type(identity) is acquire_api.Identity and type(key) is bytes
                and key == b'\x0f', 'send_not_reserved')
        try:
            acquire_api._valid_identity(identity)
        except acquire_api.Refused:
            raise Refused('send_not_reserved') from None
        require(identity == self.intent.identity, 'send_not_reserved')
        require(type(deadline) in (int, float) and deadline <= self._record_deadline, 'deadline_extension')
        self.read_identity(deadline=deadline, timeout=timeout)
        native = self.intent.identity.native
        self.sent += 1
        receipt = strict_json(self._native(['send', native, '--hex', '0f', '--json'],
                                           JSON_CAP, deadline=deadline, timeout=timeout))
        require(set(receipt) == {'id', 'bytes', 'status', 'enter_written', 'consumed'}, 'send_receipt_shape')
        for k, v in {'id': native, 'bytes': 1, 'status': 'pty_written', 'enter_written': False}.items():
            exact(receipt, k, v)
        require(receipt['consumed'] is None, 'send_receipt_shape')
        return True  # byte-write receipt only; NEVER display/proof completion
