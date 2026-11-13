#!/usr/bin/env python3
"""Phase 7 final acceptance: ten consecutive model-less interactive native-pane starts.

Fails closed: any missing evidence is a failure, never a skip. Only owned
sessions and proven automatic descendants of this run's keyed child intakes are
ever stopped. The live run needs ``--live`` and HUB_ACCEPTANCE_READY=1
(orchestrator readiness); without them the script exits 2 before touching Hub.
All I/O goes through an injectable ``Runner`` so offline tests never reach the
network.
"""
import argparse
import base64
import datetime
import hashlib
import json
import math
import os
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

COUNT = 10
RUNNING_BUDGET_S = 150
STOP_BUDGET_S = 60
CAPTURE_BUDGET_S = 90
CREATE_BUDGET_S = 60
MIN_MODELS = 3
MAX_LUNA = 3
MAX_DESCENDANTS = 8
MAX_HISTORY_PAGES = 16
HISTORY_PAGE_LIMIT = 64
MAX_HISTORY_EVENTS = 1024
MAX_HISTORY_BYTES = 1024 * 1024
HOST = 'hetzner-aplexer'
PROJECT = 'alexeygrigorev/agent-hub'
EVIDENCE_GREEN = ('success', 'green', 'passed', 'pass')

EVIDENCE_LIMIT = 128000
RECEIPT_LIMIT = 2 * 1024 * 1024
ARTIFACT_LIMIT = 8192
CAPTURE_LIMIT = 262144
MANIFEST_LIMIT = 512 * 1024

NATIVE_EVIDENCE_KIND = 'phase7_native_operator_validated_evidence_v1'
WORK_SCHEMA = 'phase7_native_work_v2'
WORK_DIGEST_DOMAIN = b'HUB_PHASE7_NATIVE_WORK_V2\x00'
EXPECTED_CALCULATION = 5050

NATIVE_CAPTURE_FORMATS = {
    'claude': 'claude_native_pane_v1',
    'antigravity': 'hydrogen_native_pane_v1',
}
NATIVE_PROOF_CLASSES = {
    'claude': 'native_bash_output_plus_artifact_v2',
}
# Hydrogen (antigravity) native framing is NOT qualified. No proof-class label, record, flag, env var or
# same-UID artifact can enable it: a class label is not attestation. Enabling requires a Root-qualified actual
# renderer preflight AND a later separately reviewed source recognizer change (docs/HUB-ACCEPTANCE-10.md).
UNQUALIFIED_NATIVE_FRAMING = {
    'antigravity': {
        'status': 'UNAVAILABLE',
        'reason': 'UNKNOWN_NATIVE_FRAMING',
        'root_checker_status': 'NEEDS_ROOT_RENDERER_ATTESTATION',
        'native_action_verified': False,
        'typed_stdout_verified': False,
        'diagnostic': ('Hydrogen native-pane framing has no qualified renderer contract; an exact command row, '
                       'footer or artifact cannot attest a completed native action'),
    },
}
NATIVE_CAPTURE_COMMAND = 'a capture <native_id> --screen --plain'
NATIVE_CAPTURE_KEYS = {
    'engine', 'machine_id', 'mode', 'format', 'proof_class',
    'capture_command', 'artifact_schema', 'host_release_sha',
    'provider_version', 'profile_set_sha256', 'reference', 'sha256', 'path',
}

WORK_KEYS = (
    'schema', 'run_id', 'sample', 'parent_intake_id', 'child_intake_id',
    'session_id', 'native_id', 'project', 'machine_id', 'workspace',
    'head', 'tracked_status_sha256', 'tracked_status_entries', 'calculation',
)
RESULT_KEYS = WORK_KEYS + ('proof_digest',)

# Legacy stream-parser constants retained for legacy parser unit tests only.
CAPTURE_FORMATS = {
    'codex': 'codex_jsonl_v1', 'zai': 'codex_jsonl_v1', 'go': 'opencode_json_v1',
    'claude': 'claude_stream_json_v1', 'antigravity': 'argon_stream_json_v1',
}
CAPTURE_KEYS = {
    'engine', 'machine_id', 'format', 'host_release_sha', 'provider_version',
    'profile_set_sha256', 'reference', 'sha256', 'path',
}
CLAUDE_EMPTY_BASH_PLACEHOLDER = '(Bash completed with no output)'


class Runner:
    """Executes `hub`/`a`/`git`/alert-sample commands; tests use fake binaries on PATH."""

    def __init__(self, hub='hub', a='a', git='git', alerts_cmd=None):
        self.bins = {
            'hub': [hub],
            'a': [a],
            'git': [git],
            'alerts': shlex.split(alerts_cmd) if alerts_cmd else None,
        }

    def run(self, tool, args, timeout=300):
        base = self.bins.get(tool)
        if not base:
            return 127, '', 'tool %s not configured' % tool
        try:
            proc = subprocess.run(
                [*base, *(args if tool != 'alerts' else [])],
                capture_output=True, text=True, timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return 124, '', 'timeout after %ss' % timeout
        except OSError as exc:
            return 127, '', str(exc)
        return proc.returncode, proc.stdout, proc.stderr

    def run_bytes(self, tool, args, timeout=60):
        base = self.bins.get(tool)
        if not base:
            return 127, b'', b'tool not configured'
        try:
            proc = subprocess.run(
                [*base, *(args if tool != 'alerts' else [])],
                capture_output=True, timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return 124, b'', ('timeout after %ss' % timeout).encode()
        except OSError as exc:
            return 127, b'', str(exc).encode()
        return proc.returncode, proc.stdout, proc.stderr

    def json(self, tool, args, timeout=300):
        rc, out, err = self.run(tool, args, timeout)
        if rc != 0:
            raise RuntimeError('%s %s failed rc=%s: %s' % (tool, ' '.join(args[:2]), rc, (err or out)[-300:]))
        try:
            return json.loads(out)
        except ValueError as exc:
            raise RuntimeError('%s %s returned non-JSON: %s' % (tool, ' '.join(args[:2]), exc))


def left(clock, deadline, cap=300):
    """Every subprocess shares the stage's actual wall-clock budget."""
    remaining = deadline - clock()
    if remaining <= 0:
        raise TimeoutError('stage deadline elapsed')
    return min(cap, remaining)


def is_astra(*parts):
    return any('astra' in str(p).lower() for p in parts)


def is_luna(*parts):
    return any('luna' in str(p).lower() for p in parts)


def unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('duplicate JSON field')
        value[key] = item
    return value


def canonical_json_bytes(obj):
    """Canonical UTF-8 JSON per V2 spec; rejects lone surrogates and non-finite floats."""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def bounded_file(path, limit):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
            raise ValueError('receipt must be a bounded regular single-link file')
        with os.fdopen(fd, 'rb') as stream:
            fd = None
            data = stream.read(limit + 1)
        if len(data) > limit or len(data) != info.st_size:
            raise ValueError('receipt changed or exceeds bound')
        return data
    finally:
        if fd is not None:
            os.close(fd)


def fsync_dir(path):
    """Make a create/replace in `path` crash-durable by fsyncing the containing directory."""
    fd = os.open(str(path), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def verify_private_bytes(path, data, mode=0o600):
    """Custody check: the persisted file is a private regular non-symlink with exactly `data`."""
    fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW)
    try:
        st = os.fstat(fd)
        if (not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or stat.S_IMODE(st.st_mode) != mode
                or st.st_uid != os.getuid() or st.st_size != len(data)):
            raise OSError('custody file %s is not an exact private regular file' % path)
        with os.fdopen(os.dup(fd), 'rb') as stream:
            if stream.read(len(data) + 1) != data:
                raise OSError('custody file %s bytes changed after write' % path)
    finally:
        os.close(fd)


def make_private_output_dir(output_dir):
    """Create a NEW private 0700 output directory (never reuse/chmod an existing path or follow symlinks)."""
    raw = Path(output_dir)
    if '..' in raw.parts:
        raise ValueError('output dir must not contain .. traversal')
    path = Path(os.path.abspath(str(raw)))
    parent = path.parent
    if parent.resolve(strict=True) != parent:
        raise ValueError('output dir parent must be a canonical path without symlinks')
    pst = os.lstat(parent)
    if not stat.S_ISDIR(pst.st_mode):
        raise ValueError('output dir parent is not a directory')
    old_umask = os.umask(0o077)
    try:
        os.mkdir(str(path), 0o700)  # FileExistsError for any existing path, including symlinks
    finally:
        os.umask(old_umask)
    st = os.lstat(path)
    if (not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) != 0o700):
        raise ValueError('output dir is not a new private 0700 directory owned by this user')
    fsync_dir(parent)
    return path


def atomic_write_private(path, data, mode=0o600):
    path = Path(path)
    tmp = None
    try:
        fd, tmp_name = tempfile.mkstemp(prefix=path.name + '.', dir=str(path.parent))
        tmp = Path(tmp_name)
        os.fchmod(fd, mode)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        tmp = None
        fsync_dir(path.parent)
        verify_private_bytes(path, data, mode)
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


def exclusive_write_private(path, data, mode=0o600):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        with os.fdopen(fd, 'wb') as stream:
            fd = None
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        if fd is not None:
            os.close(fd)
    fsync_dir(Path(path).parent)
    verify_private_bytes(path, data, mode)


def evidence_text(value, bound):
    return (isinstance(value, str) and bool(value.strip()) and len(value.encode('utf-8')) <= bound
            and not any(ord(char) < 32 or ord(char) == 127 for char in value))


def valid_native_id(value):
    return isinstance(value, str) and re.fullmatch(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', value) is not None


def valid_session_id(value):
    return isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}', value) is not None


def valid_hex40(value):
    return isinstance(value, str) and re.fullmatch(r'[0-9a-f]{40}', value) is not None


def valid_hex64(value):
    return isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value) is not None


def _validate_receipt_files(receipts, base_dir, problems):
    for name, receipt in receipts.items():
        if 'path' not in receipt:
            continue
        if not evidence_text(receipt['path'], 4096):
            problems.append('%s local receipt path invalid' % name)
            continue
        source = Path(receipt['path'])
        if not source.is_absolute():
            source = base_dir / source
        try:
            if hashlib.sha256(bounded_file(source, RECEIPT_LIMIT)).hexdigest() != receipt['sha256']:
                problems.append('%s local receipt hash mismatch' % name)
        except (OSError, ValueError):
            problems.append('%s local receipt unreadable or invalid' % name)


def _validate_evidence_common(ev, commit, expected_kind):
    problems = []
    if not isinstance(ev, dict):
        return ['evidence is not an object'], None, None
    if not valid_hex40(commit):
        problems.append('deployed commit unknown (--commit)')
    for key in ('canary_commit', 'ci_e2e_commit'):
        if not ev.get(key):
            problems.append('%s missing' % key)
        elif commit and ev[key] != commit:
            problems.append('%s %s != %s' % (key, ev[key], commit))
    for key in ('canary', 'ci_e2e'):
        if str(ev.get(key, '')).lower() not in EVIDENCE_GREEN:
            problems.append('%s not green (%r)' % (key, ev.get(key)))
    validation = ev.get('operator_validation')
    if (ev.get('kind') != expected_kind or not isinstance(validation, dict)
            or set(validation) != {'actor', 'target_commit', 'receipts', 'capture_transports'}
            or not evidence_text(validation.get('actor'), 120) or validation.get('target_commit') != commit
            or not isinstance(validation.get('receipts'), dict)
            or set(validation['receipts']) != {'deployment', 'canary', 'ci_e2e'}):
        problems.append('operator-validated same-commit receipt references required')
        return problems, None, None
    return problems, validation.get('target_commit'), validation


def native_transport_ok(engine, record, host_sha):
    """Root attests interactive native-pane transport, proof class, and capture command."""
    if engine in UNQUALIFIED_NATIVE_FRAMING or engine not in NATIVE_PROOF_CLASSES:
        return False
    return (engine in NATIVE_CAPTURE_FORMATS and isinstance(record, dict)
            and not set(record) - NATIVE_CAPTURE_KEYS
            and NATIVE_CAPTURE_KEYS - {'path'} <= set(record)
            and record.get('engine') == engine
            and record.get('machine_id') == HOST
            and record.get('mode') == 'interactive'
            and record.get('format') == NATIVE_CAPTURE_FORMATS[engine]
            and record.get('proof_class') == NATIVE_PROOF_CLASSES[engine]
            and record.get('capture_command') == NATIVE_CAPTURE_COMMAND
            and record.get('artifact_schema') == WORK_SCHEMA
            and valid_hex40(host_sha)
            and record.get('host_release_sha') == host_sha
            and evidence_text(record.get('provider_version'), 120)
            and evidence_text(record.get('reference'), 2048)
            and valid_hex64(record.get('profile_set_sha256'))
            and valid_hex64(record.get('sha256')))


def validate_native_evidence(ev, path, commit):
    """Validate Root-supplied interactive native-pane evidence bundle (refuses headless)."""
    problems, host_sha, validation = _validate_evidence_common(ev, commit, NATIVE_EVIDENCE_KIND)
    if validation is None:
        return problems
    transports = validation.get('capture_transports')
    if isinstance(transports, dict):
        for engine in sorted(set(transports) & set(UNQUALIFIED_NATIVE_FRAMING)):
            info = UNQUALIFIED_NATIVE_FRAMING[engine]
            problems.append('capture_%s native proof %s: %s (%s)' % (engine, info['status'], info['reason'], info['diagnostic']))
    if (not valid_hex40(host_sha) or not isinstance(transports, dict)
            or not 1 <= len(transports) <= len(NATIVE_CAPTURE_FORMATS)
            or any(not native_transport_ok(engine, record, host_sha) for engine, record in transports.items())):
        problems.append('verified interactive native-pane capture transport records required')
        return problems
    receipts = dict(validation['receipts'])
    for name, receipt in receipts.items():
        if (not isinstance(receipt, dict) or set(receipt) - {'commit', 'reference', 'sha256', 'path'}
                or not {'reference', 'sha256'} <= set(receipt)
                or receipt.get('commit') != commit or not evidence_text(receipt.get('reference'), 2048)
                or not valid_hex64(receipt.get('sha256'))):
            problems.append('%s receipt identity invalid or commit mismatch' % name)
    for engine, record in transports.items():
        if not native_transport_ok(engine, record, host_sha):
            problems.append('capture_%s receipt identity invalid or commit mismatch' % engine)
    all_receipts = dict(receipts)
    all_receipts.update({'capture_' + engine: record for engine, record in transports.items()})
    _validate_receipt_files(all_receipts, Path(path).parent, problems)
    return problems


def checked_native_evidence(path, commit):
    """Validate and hash the SAME bounded byte buffer for interactive native-pane evidence."""
    if not path:
        return ['evidence file missing (--evidence)'], None, {}
    try:
        raw = bounded_file(path, EVIDENCE_LIMIT)
        ev = json.loads(raw, object_pairs_hook=unique_object)
    except (OSError, ValueError, UnicodeError, TypeError):
        return ['evidence unreadable or invalid'], None, {}
    problems = validate_native_evidence(ev, path, commit)
    return problems, hashlib.sha256(raw).hexdigest(), (ev['operator_validation']['capture_transports'] if not problems else {})


# Legacy headless evidence validators (retained for ParserTests compatibility only; never used in formal run).
def check_evidence(path, commit):
    return checked_evidence(path, commit)[0]


def checked_evidence(path, commit):
    if not path:
        return ['evidence file missing (--evidence)'], None, {}
    try:
        raw = bounded_file(path, EVIDENCE_LIMIT)
        ev = json.loads(raw, object_pairs_hook=unique_object)
    except (OSError, ValueError, UnicodeError, TypeError):
        return ['evidence unreadable or invalid'], None, {}
    problems = validate_evidence(ev, path, commit)
    return problems, hashlib.sha256(raw).hexdigest(), (ev['operator_validation']['capture_transports'] if not problems else {})


def validate_evidence(ev, path, commit):
    problems, host_sha, validation = _validate_evidence_common(ev, commit, 'phase7_operator_validated_evidence_v1')
    if validation is None:
        return problems
    transports = validation.get('capture_transports')
    if (not valid_hex40(host_sha) or not isinstance(transports, dict)
            or not 1 <= len(transports) <= len(CAPTURE_FORMATS)
            or any(not capture_transport_ok(engine, record, host_sha) for engine, record in transports.items())):
        problems.append('verified engine/host/stream capture transport records required')
        return problems
    receipts = dict(validation['receipts'])
    receipts.update({'capture_' + engine: record for engine, record in transports.items()})
    for name, receipt in receipts.items():
        if (not isinstance(receipt, dict) or set(receipt) - ({'commit', 'reference', 'sha256', 'path'} if not name.startswith('capture_') else CAPTURE_KEYS)
                or not {'reference', 'sha256'} <= set(receipt)
                or not name.startswith('capture_') and receipt.get('commit') != commit or not evidence_text(receipt.get('reference'), 2048)
                or not valid_hex64(receipt.get('sha256'))):
            problems.append('%s receipt identity invalid or commit mismatch' % name)
    _validate_receipt_files(receipts, Path(path).parent, problems)
    return problems


def capture_transport_ok(engine, record, host_sha):
    return (engine in CAPTURE_FORMATS and isinstance(record, dict)
            and not set(record) - CAPTURE_KEYS and CAPTURE_KEYS - {'path'} <= set(record)
            and record.get('engine') == engine and record.get('machine_id') == HOST
            and record.get('format') == CAPTURE_FORMATS[engine]
            and valid_hex40(host_sha)
            and record.get('host_release_sha') == host_sha
            and evidence_text(record.get('provider_version'), 120)
            and evidence_text(record.get('reference'), 2048)
            and valid_hex64(record.get('profile_set_sha256'))
            and valid_hex64(record.get('sha256')))


def paired_stream_output_ok(captured, expected, engine):
    """Legacy stream-parser coverage; never authorized for formal native-pane acceptance."""
    if len(captured.encode()) > 65536:
        return False
    events = []
    for index, line in enumerate(captured.splitlines()):
        try:
            event = json.loads(line, object_pairs_hook=unique_object)
        except (ValueError, TypeError):
            if index == 0:
                continue
            return False
        if not isinstance(event, dict):
            return False
        events.append(event)
    if len(events) > 128:
        return False
    uses, results = {}, set()
    proven = False

    def marker_in(text):
        return isinstance(text, str) and any(line.strip() == expected for line in text.splitlines())

    def provider_id(value):
        return isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,100}', value) is not None

    for event in events:
        if engine == 'claude':
            message = event.get('message')
            if event.get('type') not in ('assistant', 'user') or not isinstance(message, dict):
                continue
            blocks = message.get('content')
            if not isinstance(blocks, list):
                continue
            session = event.get('session_id')
            for block in blocks:
                if not isinstance(block, dict):
                    continue
                if event['type'] == 'assistant' and block.get('type') == 'tool_use':
                    identity = block.get('id')
                    if not provider_id(identity) or not valid_native_id(session):
                        return False
                    if identity in uses:
                        return False
                    params = block.get('input')
                    if (message.get('role') != 'assistant' or block.get('name') != 'Bash'
                            or not isinstance(params, dict) or not isinstance(params.get('command'), str)):
                        continue
                    background = params.get('run_in_background')
                    if background is not None and type(background) is not bool:
                        return False
                    uses[identity] = (session, background is True)
                elif event['type'] == 'user' and block.get('type') == 'tool_result':
                    identity = block.get('tool_use_id')
                    if not provider_id(identity) or identity in results:
                        return False
                    results.add(identity)
                    if identity not in uses:
                        continue
                    use_session, is_background = uses[identity]
                    detail = event.get('tool_use_result')
                    if (session != use_session or message.get('role') != 'user'
                            or block.get('is_error') is not False or not isinstance(detail, dict)
                            or detail.get('interrupted') is not False or detail.get('stderr') != ''
                            or not isinstance(block.get('content'), str)):
                        return False
                    if is_background:
                        bg_task_id = detail.get('backgroundTaskId')
                        if (not provider_id(bg_task_id) or detail.get('stdout') != ''
                                or not block['content'].startswith(f'Command running in background with ID: {bg_task_id}.')):
                            return False
                        continue
                    if detail.get('backgroundTaskId') is not None:
                        return False
                    if block['content'] == CLAUDE_EMPTY_BASH_PLACEHOLDER and detail.get('stdout') == '':
                        if detail.get('isImage') is not False or detail.get('noOutputExpected') is not False:
                            return False
                        continue
                    if detail.get('stdout') != block['content']:
                        return False
                    proven = proven or marker_in(block['content'])
        else:
            step = event.get('step_update')
            if event.get('event') != 'step_update' or not isinstance(step, dict) or step.get('step_type') != 'tool':
                continue
            conversation, index = step.get('conversation_id'), step.get('step_index')
            tool = step.get('tool_name')
            info = step.get('tool_info')
            if (not valid_native_id(conversation) or type(index) is not int or not 0 <= index <= 2**31 - 1
                    or tool not in ('run_command', 'read_file', 'manage_task', 'schedule', 'command_status', 'wait', 'wait_5_seconds')
                    or not isinstance(info, dict) or info.get('name') != tool):
                return False
            if any(part.get(key) not in (None, False, '') for part in (event, step, info)
                   for key in ('error', 'is_error', 'interrupted')):
                return False
            params = info.get('parameters')
            if not isinstance(params, dict):
                return False
            if tool == 'run_command':
                if (set(params) != {'CommandLine'}
                        or not isinstance(params['CommandLine'], str) or not params['CommandLine']):
                    return False
            elif tool == 'read_file':
                if (not set(params) <= {'Path', 'StartLine', 'EndLine'}
                        or not isinstance(params.get('Path'), str) or not params['Path']
                        or '\0' in params['Path']
                        or not all(type(params[k]) is int and 0 <= params[k] <= 2**31 - 1
                                   for k in ('StartLine', 'EndLine') if k in params)
                        or ('StartLine' in params and 'EndLine' in params
                            and params['EndLine'] < params['StartLine'])):
                    return False
            elif tool == 'manage_task':
                if (not params or not set(params) <= {'Action', 'TaskId', 'Input'}
                        or params.get('Action') not in ('list', 'status', 'kill', 'send_input')
                        or not all(isinstance(v, str) and bool(v) for v in params.values())):
                    return False
            elif tool == 'schedule':
                if (not params
                        or not set(params) <= {'Prompt', 'DurationSeconds', 'CronExpression',
                                               'TimerCondition', 'ExactTiming', 'IsDaemon', 'MaxIterations'}
                        or not isinstance(params.get('Prompt'), str) or not params['Prompt']
                        or not all((k in ('Prompt', 'CronExpression', 'TimerCondition') and isinstance(v, str) and bool(v))
                                   or (k in ('DurationSeconds', 'MaxIterations') and type(v) is int and v > 0)
                                   or (k in ('ExactTiming', 'IsDaemon') and type(v) is bool)
                                   for k, v in params.items())):
                    return False
            elif (bool(params) if tool == 'wait_5_seconds'
                  else (not params
                        or not all(provider_id(k)
                                   and ((isinstance(v, str) and bool(v))
                                        or (type(v) is int and v >= 0)
                                        or type(v) is bool)
                                   for k, v in params.items()))):
                return False
            identity = (conversation, index)
            if step.get('state') == 'ACTIVE':
                if identity in uses or identity in results or 'output' in info:
                    return False
                uses[identity] = (tool, params)
            elif step.get('state') == 'DONE':
                if identity in results:
                    return False
                results.add(identity)
                if identity in uses and uses[identity] != (tool, params):
                    return False
                if not isinstance(info.get('output'), str):
                    return False
                if tool != 'run_command' or identity not in uses:
                    continue
                match = re.fullmatch(r'Created At: ([^\n]+)\nCompleted At: ([^\n]+)\n'
                                     r'Command exited with code 0\.\n\nCommand output:\n([\s\S]*)', info['output'])
                if not match:
                    continue
                try:
                    created, completed = (datetime.datetime.fromisoformat(match[group]) for group in (1, 2))
                    if created.tzinfo is None or completed.tzinfo is None or completed < created:
                        return False
                except ValueError:
                    return False
                proven = proven or marker_in(match[3])
            else:
                return False
    return proven


def probe_output_ok(captured, prompt, expected, engine=None, transport=None):
    """Legacy stream-parser coverage; never authorized for formal native-pane acceptance."""
    if (not capture_transport_ok(engine, transport, (transport.get('host_release_sha') if isinstance(transport, dict) else None))
            or expected in prompt or not isinstance(captured, str)):
        return False
    if engine in ('claude', 'antigravity'):
        return paired_stream_output_ok(captured, expected, engine)

    def marker_in(text):
        return isinstance(text, str) and any(line.strip() == expected for line in text.splitlines())

    for line in captured.splitlines():
        try:
            event = json.loads(line, object_pairs_hook=unique_object)
        except (ValueError, TypeError):
            continue
        if not isinstance(event, dict):
            continue
        item = event.get('item')
        if (engine in ('codex', 'zai') and event.get('type') == 'item.completed' and isinstance(item, dict)
                and item.get('type') == 'command_execution'
                and item.get('status') in (None, 'completed')
                and (item.get('exit_code') is None or type(item.get('exit_code')) is int and item['exit_code'] == 0)
                and marker_in(item.get('aggregated_output'))):
            return True
        part = event.get('part')
        if (engine == 'go' and event.get('type') == 'tool_use' and isinstance(part, dict) and part.get('type') == 'tool'
                and isinstance(part.get('tool'), str) and part['tool'].strip()
                and isinstance(part.get('state'), dict) and part['state'].get('status') == 'completed'
                and marker_in(part['state'].get('output'))):
            return True
    return False


# --- Alerts ---
ALERT_SAMPLE_MAX_AGE_MS = 60000
ALERT_SAMPLE_FUTURE_SKEW_MS = 5000


def sample_alerts(r, wall_clock=time.time):
    data = r.json('alerts', [], 60)
    if (not isinstance(data, dict) or data.get('kind') != 'dev_alert_occurrences_v1'
            or data.get('recipient') != 'dev@dtcdev.click'):
        raise RuntimeError('durable dev@ occurrence watermark required (active IDs are insufficient)')
    generation, count, observed = data.get('generation'), data.get('count'), data.get('observed_at_ms')
    if (not isinstance(generation, str) or not generation.strip() or len(generation.encode()) > 200
            or type(count) is not int or count < 0 or type(observed) is not int):
        raise RuntimeError('alert watermark metadata invalid')
    age = wall_clock() * 1000 - observed
    if age > ALERT_SAMPLE_MAX_AGE_MS or age < -ALERT_SAMPLE_FUTURE_SKEW_MS:
        raise RuntimeError('alert watermark stale or from the future')
    return {key: data[key] for key in ('kind', 'recipient', 'generation', 'count', 'observed_at_ms')}


def alert_changes(before, after):
    if after['generation'] != before['generation']:
        return ['alert watermark generation changed']
    if after['count'] < before['count'] or after['observed_at_ms'] < before['observed_at_ms']:
        return ['alert watermark reset or observation time regressed']
    if after['count'] > before['count']:
        return ['dev alert occurrences during run: %d' % (after['count'] - before['count'])]
    return []


# --- V2 Challenge, Git, Work Artifact, and Native Pane Recognition ---
def parse_porcelain_z_count(status_raw):
    """Count complete records in `git status --porcelain=v1 -z --untracked-files=no`."""
    if not isinstance(status_raw, (bytes, bytearray)):
        raise ValueError('status_raw must be bytes')
    raw = bytes(status_raw)
    if not raw:
        return 0
    if not raw.endswith(b'\x00'):
        raise ValueError('malformed porcelain -z output (missing trailing NUL)')
    tokens = raw[:-1].split(b'\x00')
    idx, count = 0, 0
    while idx < len(tokens):
        entry = tokens[idx]
        if len(entry) < 4 or entry[2:3] != b' ' or not entry[3:]:
            raise ValueError('malformed porcelain -z entry')
        xy = entry[:2]
        idx += 1
        if ord('R') in xy or ord('C') in xy:
            if idx >= len(tokens) or not tokens[idx]:
                raise ValueError('malformed porcelain -z rename/copy record')
            idx += 1
        count += 1
    return count


def compute_proof_digest(challenge32, work_obj):
    if not isinstance(challenge32, (bytes, bytearray)) or len(challenge32) != 32:
        raise ValueError('challenge must be exactly 32 bytes')
    return hashlib.sha256(
        WORK_DIGEST_DOMAIN + bytes(challenge32) + b'\x00' + canonical_json_bytes(work_obj)
    ).hexdigest()


def work_rel_path(run_id, sample):
    return Path('.local') / 'acceptance-native' / run_id / str(sample) / 'result.json'


def probe_rel_path(run_id, sample):
    """Short unique per-sample probe script path; short enough to be fully visible in a Hydrogen Ran header."""
    return Path('.local') / 'a10' / ('%s%02d.py' % (run_id.replace('-', '')[:8], sample))


def probe_command(run_id, sample):
    return 'python3 %s' % probe_rel_path(run_id, sample)


def validate_no_symlink_ancestors(base_dir, rel_path):
    """Ensure base_dir and every component up to rel_path exist without symlinks."""
    base = Path(base_dir)
    if not base.is_absolute():
        raise ValueError('workspace must be absolute')
    st = os.lstat(base)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise ValueError('workspace is symlink or not a directory')
    cur = base
    for part in Path(rel_path).parts:
        cur = cur / part
        st = os.lstat(cur)
        if stat.S_ISLNK(st.st_mode):
            raise ValueError('symlink component in artifact path: %s' % cur)


def _fresh_git_timeout(clock, deadline, what, cap=30):
    """Freshly remaining proof budget immediately BEFORE one Git call (never a reused earlier value)."""
    remaining = deadline - clock()
    if remaining <= 0:
        raise TimeoutError('proof deadline exhausted before %s' % what)
    return max(0.01, min(cap, remaining))


def observe_git_state(r, workspace, clock, deadline):
    """Two Git observations inside ONE total proof deadline: each call gets the budget remaining at its own start
    and the deadline is rechecked after each call returns."""
    rc, head_out, head_err = r.run('git', ['-C', workspace, 'rev-parse', '--verify', 'HEAD'],
                                   _fresh_git_timeout(clock, deadline, 'git rev-parse'))
    if clock() > deadline:
        raise TimeoutError('proof deadline exhausted during git rev-parse')
    if rc != 0:
        raise RuntimeError('git rev-parse HEAD failed: %s' % (head_err or head_out).strip()[:200])
    head = head_out.strip()
    if not valid_hex40(head):
        raise RuntimeError('git rev-parse HEAD invalid: %r' % head)
    rc, st_out, st_err = r.run_bytes('git', ['-C', workspace, 'status', '--porcelain=v1', '-z', '--untracked-files=no'],
                                     _fresh_git_timeout(clock, deadline, 'git status'))
    if clock() > deadline:
        raise TimeoutError('proof deadline exhausted during git status')
    if rc != 0:
        raise RuntimeError('git status failed: %s' % st_err.decode('utf-8', 'replace').strip()[:200])
    entries = parse_porcelain_z_count(st_out)
    status_sha = hashlib.sha256(st_out).hexdigest()
    return head, status_sha, entries


def verify_work_artifact(r, actual_workspace, run_id, sample, parent_intake_id, child_intake_id,
                         session_id, native_id, challenge32, clock, deadline, require_probe=False):
    """Independently verify result.json bytes, schema, identities, Git state, and challenge digest.

    Every Git call uses the REMAINING proof deadline (never a fresh default timeout)."""
    if clock() >= deadline:
        raise TimeoutError('proof deadline exhausted before artifact verification')
    probe_sha = None
    if require_probe:
        prel = probe_rel_path(run_id, sample)
        validate_no_symlink_ancestors(actual_workspace, prel)
        probe_bytes = bounded_file(Path(actual_workspace) / prel, ARTIFACT_LIMIT)
        if not probe_bytes.strip():
            raise ValueError('per-sample probe script is empty')
        probe_sha = hashlib.sha256(probe_bytes).hexdigest()
    rel = work_rel_path(run_id, sample)
    validate_no_symlink_ancestors(actual_workspace, rel)
    result_path = Path(actual_workspace) / rel
    raw_bytes = bounded_file(result_path, ARTIFACT_LIMIT)
    mtime_ns = os.lstat(result_path).st_mtime_ns
    parsed = json.loads(raw_bytes.decode('utf-8'), object_pairs_hook=unique_object)
    if not isinstance(parsed, dict) or set(parsed) != set(RESULT_KEYS):
        raise ValueError('result.json keys do not match exact V2 schema')
    for int_key in ('sample', 'tracked_status_entries', 'calculation'):
        if type(parsed.get(int_key)) is not int:
            raise ValueError('%s must be exact int' % int_key)
    if clock() >= deadline:
        raise TimeoutError('proof deadline exhausted before Git observation')
    head, status_sha, entries = observe_git_state(r, actual_workspace, clock, deadline)
    if clock() > deadline:
        raise TimeoutError('proof deadline exhausted during Git observation')
    expected_work = {
        'schema': WORK_SCHEMA,
        'run_id': run_id,
        'sample': sample,
        'parent_intake_id': parent_intake_id,
        'child_intake_id': child_intake_id,
        'session_id': session_id,
        'native_id': native_id,
        'project': PROJECT,
        'machine_id': HOST,
        'workspace': actual_workspace,
        'head': head,
        'tracked_status_sha256': status_sha,
        'tracked_status_entries': entries,
        'calculation': EXPECTED_CALCULATION,
    }
    expected_digest = compute_proof_digest(challenge32, expected_work)
    expected_result = dict(expected_work, proof_digest=expected_digest)
    expected_bytes = canonical_json_bytes(expected_result) + b'\n'
    if raw_bytes != expected_bytes:
        raise ValueError('result.json does not match exact canonical V2 serialization or expected fields')
    return {
        'artifact_sha256': hashlib.sha256(raw_bytes).hexdigest(),
        'proof_digest': expected_digest,
        'head': head,
        'tracked_status_sha256': status_sha,
        'tracked_status_entries': entries,
        'calculation': EXPECTED_CALCULATION,
        'mtime_ns': mtime_ns,
        'result_path': str(result_path),
        'probe_sha256': probe_sha,
    }


def _strip_ansi(text):
    return re.sub(r'\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)', '', text)


def _has_trust_menu(text):
    return ('Do you trust the contents of this project?' in text
            or 'Yes, I trust this folder' in text)


def _collapse_ws(text):
    return ' '.join(text.split())


def _claude_native_pane_ok(captured, expected_line, brief_text):
    """Require completed `● Bash(...)` heading + `⎿` output block carrying expected_line."""
    lines = [_strip_ansi(line) for line in captured.splitlines()]
    in_bash = False
    header_closed = False
    in_output = False
    bash_pending = False
    output_chunks = []

    def block_has_expected(chunks):
        joined = _collapse_ws(' '.join(chunks))
        joined_nowrap = _collapse_ws(''.join(line.strip() for line in chunks))
        target = _collapse_ws(expected_line)
        target_nospace = target.replace(' ', '')
        return target in joined or target_nospace in joined_nowrap.replace(' ', '')

    for line in lines:
        stripped = line.strip()
        if re.match(r'^●\s*Bash\(', stripped):
            if in_bash and header_closed and in_output and not bash_pending and block_has_expected(output_chunks):
                return True
            in_bash = True
            header_closed = ')' in stripped
            in_output = False
            bash_pending = False
            output_chunks = []
            continue
        if in_bash:
            if re.match(r'^(●|✽|❯|─{10,}|⏵)', stripped):
                if header_closed and in_output and not bash_pending and block_has_expected(output_chunks):
                    return True
                in_bash = False
                header_closed = False
                in_output = False
                bash_pending = False
                output_chunks = []
                continue
            if not header_closed:
                if ')' in stripped:
                    header_closed = True
                continue
            if 'Waiting…' in stripped or 'Running…' in stripped or 'running PreToolUse' in stripped:
                bash_pending = True
                continue
            if stripped.startswith('⎿'):
                in_output = True
                content = stripped[1:].strip()
                if content and content != '(No content)':
                    output_chunks.append(content)
            elif in_output and stripped:
                output_chunks.append(stripped)
    if in_bash and header_closed and in_output and not bash_pending and block_has_expected(output_chunks):
        return True
    return False


def hydrogen_framing_diagnostic(captured, command_id):
    """PROSPECTIVE support hook only -- never native authority.

    Reports how many exact, fully visible `● Ran (<command_id>) (ctrl+o to expand)` rows the plain pane shows and
    which known ambiguity classes are present (same-command Running/error rows, duplicates). It cannot tell a
    completed native action from assistant/footer text, so its status is always UNKNOWN_NATIVE_FRAMING and
    native_action_verified is always False until a Root-qualified renderer contract and a separately reviewed
    recognizer replace this function."""
    info = dict(UNQUALIFIED_NATIVE_FRAMING['antigravity'])
    exact = bool(isinstance(command_id, str) and re.fullmatch(r'python3 \.local/a10/[0-9a-f]{8}\d{2}\.py', command_id))
    rows = 0
    contradictions = []
    if exact and isinstance(captured, str):
        want = '● Ran (%s) (ctrl+o to expand)' % command_id
        for line in captured.splitlines():
            flat = _collapse_ws(_strip_ansi(line))
            if flat == want:
                rows += 1
            elif command_id in flat and re.search(r'Running|Error|failed|exit', flat):
                contradictions.append('same-command non-completed/failed row')
        if rows > 1:
            contradictions.append('duplicate exact row')
    info.update(exact_command=exact, candidate_rows=rows, contradictions=sorted(set(contradictions)))
    return info


def _hydrogen_native_pane_ok(captured, command_id):
    """Hydrogen native proof is UNAVAILABLE (UNKNOWN_NATIVE_FRAMING): always False today."""
    hydrogen_framing_diagnostic(captured, command_id)
    return False


def native_pane_output_ok(captured, engine, transport, expected_line, brief_text, command_id=None):
    """Validate direct `a capture <native_id> --screen --plain` against attested interactive profile."""
    if (not isinstance(captured, str) or len(captured.encode('utf-8')) > CAPTURE_LIMIT
            or not isinstance(brief_text, str) or expected_line in brief_text):
        return False
    if not native_transport_ok(engine, transport, (transport.get('host_release_sha') if isinstance(transport, dict) else None)):
        return False
    clean = _strip_ansi(captured)
    if _has_trust_menu(clean):
        return False
    # Never accept raw headless JSON stream frames as native-pane proof.
    first_nonempty = next((ln.strip() for ln in clean.splitlines() if ln.strip()), '')
    if first_nonempty.startswith('{') and '"type":' in first_nonempty:
        return False
    if engine == 'claude':
        return _claude_native_pane_ok(captured, expected_line, brief_text)
    if engine == 'antigravity':
        return _hydrogen_native_pane_ok(captured, command_id)
    return False


def make_probe():
    """Legacy headless probe helper retained for legacy ParserTests only; never used in formal run."""
    marker = 'ACCEPTANCE-OK:' + uuid.uuid4().hex
    encoded = base64.b64encode(marker.encode()).decode()
    prompt = ('Use two sequential shell tool calls. First run a separate foreground shell command '
              '`python3 -c \'import base64; print(base64.b64decode("%s").decode())\'` as one output line and let it '
              'finish. Immediately after the marker print finishes, run a separate foreground `sleep 60` command '
              'using the existing shell tool with a supported timeout sufficient for the 60-second hold so the '
              'orchestrator can observe and stop this session before any final prose or completion bookkeeping. '
              'Do not chain the marker print with the hold in one command, do not use a background flag, do not '
              'include the marker or nonce in the hold command, do not echo proof or emit waiting prose, and do not '
              'search for or call any monitor, task-output, or status tools or poll in a loop. Apart from mandatory '
              'Hub first-action/completion evidence files if required by the session wrapper, do not create or edit '
              'other files, start another agent, or exit early.' % encoded)
    return prompt, marker


# --- Brief / AC construction ---
def make_child_brief_and_ac(run_id, sample, parent_intake_id, challenge_path):
    rel_result = str(work_rel_path(run_id, sample))
    brief = (
        f'Phase 7 interactive native-pane probe (run {run_id}, sample {sample}/{COUNT}, parent {parent_intake_id}).\n'
        'First action: run `aplexer whoami --json` to observe your canonical native id, tag, and workspace '
        '(it prints a full session record: read only its `id` and `workspace` fields; never copy the record, '
        'command, env or profile into files or output).\n'
        'Then use a real shell/Python tool in your actual child workspace to:\n'
        '1. Run `git rev-parse --verify HEAD` and `git status --porcelain=v1 -z --untracked-files=no`.\n'
        '2. Compute `calculation = sum(range(1, 101))`.\n'
        f'3. Read the 32-byte challenge file at `{challenge_path}`.\n'
        '4. Build the canonical JSON work object with exact keys: schema="phase7_native_work_v2", '
        f'run_id="{run_id}", sample={sample}, parent_intake_id="{parent_intake_id}", '
        'child_intake_id=<this intake id>, session_id=<this Hub session id>, native_id=<whoami id>, '
        f'project="{PROJECT}", machine_id="{HOST}", workspace=<actual workspace>, head=<HEAD>, '
        'tracked_status_sha256=<sha256 of status -z bytes>, tracked_status_entries=<record count>, calculation=5050.\n'
        '5. Compute proof_digest = sha256(b"HUB_PHASE7_NATIVE_WORK_V2\\x00" + challenge32 + b"\\x00" + C(work_object)).hexdigest().\n'
        f'6. Write canonical JSON (work_object + proof_digest, sort_keys=True, separators=(",", ":"), ensure_ascii=False, + "\\n") '
        f'only to `{rel_result}` inside your actual child workspace.\n'
        f'7. Print `AC10_WORK_V2 {run_id} {sample} <proof_digest> <head> 5050` from the real tool stdout AFTER writing the file.\n'
        f'Perform steps 1-7 in ONE Python script saved at `{probe_rel_path(run_id, sample)}` in your child workspace and '
        f'run it once as the single foreground command `{probe_command(run_id, sample)}` (no cd, pipes, chaining or extra args).\n'
        'Do not edit tracked files, commit, push, exit, start timers, or call `hub sessions finish`; remain at the interactive prompt until Hub STOP.'
    )
    ac = (
        f'1. Genuine `aplexer whoami --json` and independent V2 work artifact written at `{rel_result}` '
        f'matching the 32-byte challenge at `{challenge_path}` and current Git state.\n'
        '2. Real native-pane tool completion output printed and worker remains interactive until Hub STOP.'
    )
    return brief, ac


# --- Hub / Native response helpers ---
def session_of(view, expected_id=None):
    """`hub sessions show --json` is {"session": {...}}; a flat or foreign shape is an error."""
    sess = view.get('session') if isinstance(view, dict) else None
    if not isinstance(sess, dict) or not sess.get('id'):
        raise RuntimeError('show response is not {"session": {...}}')
    if expected_id is not None and sess['id'] != expected_id:
        raise RuntimeError('show session id mismatch with requested session')
    return sess


def intake_of(view, expected_id=None):
    """`hub intake show --json` is {"intake": {...}, "task": {...}}."""
    item = view.get('intake') if isinstance(view, dict) else None
    if not isinstance(item, dict) or not item.get('id'):
        raise RuntimeError('intake show response is not {"intake": {...}}')
    if expected_id is not None and item['id'] != expected_id:
        raise RuntimeError('intake show id mismatch with requested intake')
    return item


def list_rows(res):
    rows = res.get('sessions') if isinstance(res, dict) else res
    if rows is None and isinstance(res, dict):
        rows = res.get('items')
    if not isinstance(rows, list):
        raise RuntimeError('list response has no sessions array')
    return [x for x in rows if isinstance(x, dict)]


def row_host(row):
    return row.get('host') or row.get('machine_id') or row.get('machine')


def row_workspace(row):
    return row.get('workspace')


def live_native_rows(r, timeout):
    """Return dict of {native_id: row} for currently running native sessions."""
    rows = r.json('a', ['list', '--json', '--running'], timeout)
    if not isinstance(rows, list):
        raise RuntimeError('a list --json is not an array')
    live = {}
    for row in rows:
        if not isinstance(row, dict) or not row.get('id'):
            continue
        if row.get('phase') in ('exited', 'dead', 'stopped') or row.get('state') in ('exited', 'dead', 'stopped'):
            continue
        live[row['id']] = row
    return live


def live_natives(r, timeout):
    """Native sessions live right now: `a list --json --running`, filtered to state running."""
    return set(live_native_rows(r, timeout))


# --- Picker validation ---
def pool_problems(pool):
    if not isinstance(pool, list) or not pool or len(pool) > 32:
        return ['picker pool missing or empty']
    shares, weights, ids = [], [], set()
    for row in pool:
        sh = row.get('share') if isinstance(row, dict) else None
        weight = row.get('weight') if isinstance(row, dict) else None
        if (isinstance(sh, bool) or not isinstance(sh, (int, float)) or not math.isfinite(sh) or not 0 < sh <= 1 + 1e-9
                or isinstance(weight, bool) or not isinstance(weight, (int, float)) or not math.isfinite(weight) or weight <= 0):
            return ['picker pool has an invalid or zero share']
        if not all(isinstance(row.get(k), str) and row[k].strip() and len(row[k].encode()) <= 120 for k in ('policy_id', 'model', 'engine')):
            return ['picker pool row missing policy/model/engine']
        if 'effort' not in row or row['policy_id'] in ids or row.get('effort') is not None and (not isinstance(row['effort'], str) or not row['effort'] or len(row['effort'].encode()) > 16):
            return ['picker pool duplicate policy or invalid effort']
        ids.add(row['policy_id'])
        shares.append(sh)
        weights.append(weight)
    if abs(sum(shares) - 1) > 1e-6:
        return ['picker shares sum to %.3f, not 1' % sum(shares)]
    if not math.isfinite(sum(weights)) or any(abs(share - weight / sum(weights)) > 1e-6 for share, weight in zip(shares, weights)):
        return ['picker shares do not match normalized weights']
    return []


CHOICE_FIELDS = ('policy_id', 'policy_version', 'machine_id', 'attempt', 'engine', 'model', 'effort')


def choice_of(picker):
    return {key: picker.get(key) for key in CHOICE_FIELDS}


def valid_choice(row):
    if not isinstance(row, dict) or any(key not in row for key in CHOICE_FIELDS):
        return False
    for key, bound in (('policy_id', 120), ('model', 120), ('engine', 120), ('machine_id', 80)):
        value = row.get(key)
        if not isinstance(value, str) or not value.strip() or len(value.encode()) > bound:
            return False
    version, effort = row.get('policy_version'), row.get('effort')
    return (type(row.get('attempt')) is int and row['attempt'] in (0, 1)
            and (type(version) is int and 0 <= version <= 9007199254740991 or isinstance(version, str) and bool(version.strip()) and len(version.encode()) <= 80)
            and (effort is None or isinstance(effort, str) and bool(effort) and len(effort.encode()) <= 16))


def picker_problems(picker, session):
    """Validate the actual CURRENT draw; another attempt's pool cannot justify it."""
    if not isinstance(picker, dict):
        return ['session picker evidence missing']
    try:
        if len(json.dumps(picker, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode()) > 24000:
            return ['session picker evidence exceeds bound']
    except (ValueError, TypeError):
        return ['session picker evidence is not finite JSON']
    problems = pool_problems(picker.get('pool'))
    if not valid_choice(picker):
        problems.append('picker choice invalid')
    if type(picker.get('attempt')) is not int or picker.get('attempt') not in (0, 1):
        problems.append('picker attempt invalid')
    version = picker.get('policy_version')
    if (isinstance(version, bool) or not isinstance(version, (int, str))
            or isinstance(version, int) and not 0 <= version <= 9007199254740991 or isinstance(version, str) and not version.strip()):
        problems.append('picker policy version missing or invalid')
    for key in ('engine', 'model', 'effort', 'policy_id', 'policy_version'):
        if key not in session or picker.get(key) != session.get(key):
            problems.append('picker %s mismatch with current session' % key)
    if picker.get('machine_id') != row_host(session):
        problems.append('picker machine mismatch with current session')
    # Endpoint-aware: a Project-shaped view (start response) carries base + attempt alias; a Machine show carries
    # the attempt id in operation_id. Both are strictly normalized; malformed/contradictory is a mismatch.
    try:
        if session.get('attempt_operation_id') is not None and session.get('attempt_operation_id') != session.get('operation_id'):
            attempt = _project_shape_identity(session, 'session', 'id')['attempt']
        else:
            attempt = machine_session_identity(session)['attempt']
    except IdentityError:
        attempt = None
    if attempt is None or picker.get('attempt') != attempt:
        problems.append('picker attempt mismatch with current session')
    pool = picker.get('pool')
    if isinstance(pool, list) and not any(isinstance(row, dict) and all(row.get(key) == picker.get(key)
            for key in ('policy_id', 'engine', 'model', 'effort')) for row in pool):
        problems.append('current chosen model/policy not in the picker pool shares')
    history = picker.get('history')
    if not isinstance(history, list) or not history or len(history) != (picker.get('attempt') + 1 if type(picker.get('attempt')) is int else 0):
        problems.append('picker history missing or cross-attempt')
    elif any(not valid_choice(row) or row.get('attempt') != i for i, row in enumerate(history)) or choice_of(history[-1]) != choice_of(picker):
        problems.append('picker history does not end with current choice')
    return problems


# --- Keyed Create / Start Recovery ---
def reconcile_by_key(r, argv, key, deadline, clock):
    """Replay the identical request/key, changing only `--no-wait` for start."""
    if '--key' not in argv or argv[argv.index('--key') + 1] != key:
        return None
    replay = list(argv)
    if '--no-wait' not in replay:
        replay.append('--no-wait')
    try:
        created = r.json('hub', replay, left(clock, deadline, 60))
    except Exception:
        return None
    sid = created.get('id') if isinstance(created, dict) else None
    return sid if valid_session_id(sid) else None


def recover_owned(r, rec, clock):
    sid = reconcile_by_key(r, rec['_start_argv'], rec['start_key'], clock() + 60, clock)
    if sid:
        rec['id'] = sid
        rec['cleanup_uncertain'] = False
    else:
        rec['cleanup_uncertain'] = True
    return sid


def normalize_acceptance_criteria(text):
    """Match Hub2 normalizeCriteria(): split on newlines, strip leading bullet/number, trim, drop empty."""
    return [
        line for line in (
            re.sub(r'^\s*(?:[-*]|\d+[.)])\s+', '', raw).strip()
            for raw in text.splitlines()
        ) if line
    ]


def create_child_intake(r, rec, run_id, sample, parent_intake_id, brief_path, ac_path, brief_text, ac_text,
                        seen_intakes, clock):
    """Create a unique keyed child intake; replay exact key/body on ambiguity."""
    create_key = rec['create_key']
    title = 'Phase7 owned run %s/%d' % (run_id, sample)
    # Actual hub2 Project create persists only title/text/acceptance_criteria/code_task and stores
    # provenance={type:user,code_task:<bool>}; it ignores body.source and has no structural parent link.
    # So no --source-* flags are sent and no source-reference persistence is claimed.
    argv = [
        'intake', 'create',
        '--project', PROJECT,
        '--title', title,
        '--text-file', str(brief_path),
        '--acceptance-criteria-file', str(ac_path),
        '--key', create_key,
    ]
    deadline = clock() + CREATE_BUDGET_S
    created = None
    last_exc = None
    for attempt in range(2):
        try:
            created = r.json('hub', argv, left(clock, deadline, 30))
            break
        except Exception as exc:
            last_exc = exc
            if attempt == 0 and clock() < deadline:
                continue
    if created is None:
        rec['create_uncertain'] = True
        raise RuntimeError('intake create failed (uncertain key %s): %s' % (create_key, last_exc))
    item = intake_of(created)
    cid = item['id']
    if not valid_native_id(cid):
        raise RuntimeError('child intake id is not canonical UUID: %r' % cid)
    if cid == parent_intake_id or cid in seen_intakes:
        raise RuntimeError('child intake id reused or equals parent: %s' % cid)
    seen_intakes.add(cid)
    shown = intake_of(r.json('hub', ['intake', 'show', cid], left(clock, deadline, 30)), cid)
    if shown.get('project') != PROJECT:
        raise RuntimeError('child intake project mismatch: %r' % shown.get('project'))
    if shown.get('text') != brief_text:
        raise RuntimeError('child intake text mismatch')
    if shown.get('acceptance_criteria') != normalize_acceptance_criteria(ac_text):
        raise RuntimeError('child intake acceptance_criteria mismatch')
    problem = child_intake_shape_problem(shown, cid, title, brief_text, ac_text)
    if problem:
        raise RuntimeError(problem)
    rec['child_intake_id'] = cid
    text_sha = hashlib.sha256(brief_text.encode('utf-8')).hexdigest()
    ac_sha = hashlib.sha256(canonical_json_bytes(normalize_acceptance_criteria(ac_text))).hexdigest()
    # Ownership is an explicit, keyed, maintained parent note binding exact create receipt fields; it is
    # NOT a structural parent link (parent_intake_id stays null on the actual server view).
    note_key = 'acc10-own-%s-%d' % (run_id, sample)
    note_text = ('Owned Phase7 child intake %s (run=%s sample=%d project=%s create_key=%s start_key=%s '
                 'text_sha256=%s ac_sha256=%s)' % (cid, run_id, sample, PROJECT, create_key, rec['start_key'], text_sha, ac_sha))
    r.json('hub', ['intake', 'append', parent_intake_id, '--key', note_key, '--note', note_text],
           left(clock, deadline, 30))
    rec['create_receipt'] = {
        'create_key': create_key, 'child_intake_id': cid, 'project': PROJECT, 'title': title,
        'text_sha256': text_sha, 'acceptance_criteria_sha256': ac_sha,
        'provenance': dict(shown['provenance']), 'parent_note_key': note_key,
        'parent_intake_id': parent_intake_id, 'structural_parent_link': False,
    }
    return cid


def child_intake_shape_problem(shown, cid, title, brief_text, ac_text):
    """Validate the actual hub2 viewIntake shape (project-do.ts create/viewIntake) for a fresh child."""
    if not isinstance(shown, dict) or shown.get('id') != cid:
        return 'child intake id mismatch on show'
    if shown.get('project') != PROJECT:
        return 'child intake project mismatch: %r' % shown.get('project')
    if shown.get('title') != title.strip():
        return 'child intake title mismatch'
    if shown.get('text') != brief_text:
        return 'child intake text mismatch'
    if shown.get('acceptance_criteria') != normalize_acceptance_criteria(ac_text):
        return 'child intake acceptance_criteria mismatch'
    if shown.get('original_ask') != {'title': title.strip(), 'text': brief_text}:
        return 'child intake original_ask mismatch'
    if shown.get('provenance') != {'type': 'user', 'code_task': True}:
        return 'child intake provenance mismatch: %r' % (shown.get('provenance'),)
    if shown.get('parent_intake_id') is not None:
        return 'child intake unexpectedly has structural parent_intake_id %r' % shown.get('parent_intake_id')
    if shown.get('status') != 'open' or shown.get('session_id') is not None:
        return 'fresh child intake is not open/unassigned'
    return None


# --- V2 Complete Paged History & Owned Lineage Cleanup ---
def scan_intake_lineage(r, child_id, deadline, clock):
    """Bracket complete paged intake history with fresh show snapshots."""
    before_show = intake_of(r.json('hub', ['intake', 'show', child_id], left(clock, deadline, 20)), child_id)
    archive = before_show.get('history_archive')
    if isinstance(archive, dict) and archive.get('count', 0) > 0:
        return None, 'history_archive has %s unread archived events' % archive.get('count')
    events = []
    after = 0
    total_bytes = 0
    last_seq = 0
    for _page in range(MAX_HISTORY_PAGES):
        res = r.json('hub', ['intake', 'history', child_id, '--after', str(after), '--limit', str(HISTORY_PAGE_LIMIT)],
                     left(clock, deadline, 20))
        if not isinstance(res, dict) or not isinstance(res.get('events'), list):
            return None, 'intake history response malformed'
        raw_len = len(canonical_json_bytes(res))
        total_bytes += raw_len
        if total_bytes > MAX_HISTORY_BYTES:
            return None, 'intake history exceeded 1 MiB aggregate bound'
        page = res['events']
        for ev in page:
            if not isinstance(ev, dict) or type(ev.get('seq')) is not int or ev['seq'] != last_seq + 1:
                return None, 'intake history sequence gap or non-monotonic seq'
            last_seq = ev['seq']
            events.append(ev)
            if len(events) > MAX_HISTORY_EVENTS:
                return None, 'intake history exceeded 1024 event bound'
        nxt = res.get('next')
        if nxt is None:
            break
        if type(nxt) is not int or nxt <= after or (page and nxt != page[-1]['seq']):
            return None, 'intake history invalid pagination cursor'
        after = nxt
    else:
        return None, 'intake history exceeded %d page bound' % MAX_HISTORY_PAGES

    after_show = intake_of(r.json('hub', ['intake', 'show', child_id], left(clock, deadline, 20)), child_id)
    after_archive = after_show.get('history_archive')
    if isinstance(after_archive, dict) and after_archive.get('count', 0) > 0:
        return None, 'history_archive has %s unread archived events' % after_archive.get('count')
    if any(before_show.get(k) != after_show.get(k)
           for k in ('updated_at', 'session_id', 'status', 'assigned_machine_id', 'dispatch', 'final_evidence')):
        return None, 'intake snapshot changed during history scan'
    return {'show': after_show, 'events': events}, None


def extract_proven_descendants(events, owned_sids):
    """Only exact source-proven automatic continuation/verification events prove descent."""
    known = set(owned_sids)
    found = []
    seen = set()
    changed = True
    while changed:
        changed = False
        for ev in events:
            kind = ev.get('kind')
            body = ev.get('body') if isinstance(ev.get('body'), dict) else {}
            if kind == 'task_continuation_started':
                prev_sid = body.get('previous_session_id')
                new_sid = body.get('session_id')
                if prev_sid in known and valid_session_id(new_sid) and new_sid not in known:
                    known.add(new_sid)
                    if new_sid not in seen:
                        seen.add(new_sid)
                        found.append(('continuation', new_sid))
                        changed = True
            elif kind == 'task_verification_started':
                orig_sid = body.get('original_session_id')
                prev_sid = body.get('previous_session_id')
                new_sid = body.get('session_id')
                if ((orig_sid in known or prev_sid in known)
                        and valid_session_id(new_sid) and new_sid not in known):
                    known.add(new_sid)
                    if new_sid not in seen:
                        seen.add(new_sid)
                        found.append(('verification', new_sid))
                        changed = True
    return found


def _pending_completion_reason(intake_row):
    final = intake_row.get('final_evidence')
    if not isinstance(final, dict):
        return None
    comp = final.get('completion')
    if not isinstance(comp, dict):
        return None
    for branch in ('continuation', 'verification'):
        sub = comp.get(branch)
        if isinstance(sub, dict) and sub.get('state') in ('pending', 'inflight', 'running'):
            return '%s %s' % (branch, sub.get('state'))
    return None


def _stop_single_session(r, sid, known_native, stop_key, deadline, clock, sleep):
    """Issue standard Hub STOP with stable key even if native_id is NULL; confirm stop + native absence."""
    wait_s = max(1, min(int(left(clock, deadline, 30)), 30))
    r.json('hub', ['sessions', 'stop', sid, '--key', stop_key, '--wait', '--timeout', str(wait_s)],
           left(clock, deadline, 35))
    sess = session_of(r.json('hub', ['sessions', 'show', sid, '--json'], left(clock, deadline, 20)), sid)
    if sess.get('state') != 'stopped':
        return sess, 'state after stop %r != stopped' % sess.get('state')
    if sess.get('stop_confirmed') is not True:
        return sess, 'stop_confirmed != true after stop'
    native = known_native if valid_native_id(known_native) else sess.get('native_id')
    if valid_native_id(native):
        while True:
            if native not in live_natives(r, left(clock, deadline, 20)):
                return sess, None
            if clock() + 1 >= deadline:
                return sess, 'native %s still listed after %ds' % (native, STOP_BUDGET_S)
            sleep(min(1, max(0.01, deadline - clock())))
    return sess, None


DISPATCH_TERMINAL_STATES = ('stopped', 'failed', 'complete', 'cancelled')
ARCHIVABLE_INTAKE_STATUSES = ('open', 'stopped', 'failed', 'cancelled')


def _identity_problem(expected, actual, label):
    """Strict binding: every expected field must be present (non-null) on BOTH sides and equal.

    Missing/NULL metadata is UNKNOWN, never equality."""
    for key, want in expected.items():
        got = actual.get(key) if isinstance(actual, dict) else None
        if want is None or got is None:
            return '%s %s UNKNOWN (expected=%r actual=%r)' % (label, key, want, got)
        if got != want:
            return '%s %s drift %r != %r' % (label, key, got, want)
    return None


# --- Endpoint-specific operation identity normalization ---
# Source contract (hub2/src/machine-do.ts sessionView + claim at :722; hub2/src/project-do.ts sagaView and the
# previous-attempt relation at :1016): the Machine session row stores the ATTEMPT operation id in `operation_id`
# (its base dispatch id is not projected) and emits no `attempt_operation_id`/`attempt`. The Project dispatch saga,
# final_evidence and the nested exit evidence carry `operation_id` = BASE and `attempt_operation_id` =
# `<base>:<attempt>` (dispatch also carries integer `attempt`). Every view is normalized into the SAME
# (base, attempt_operation_id, attempt) triple; raw `operation_id` labels are never compared across endpoints.
_BASE_OP_RE = r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'
_ATTEMPT_OP_RE = re.compile(r'(%s):(0|[1-9][0-9]{0,2})' % _BASE_OP_RE)
IDENTITY_KEYS = ('session_id', 'operation_id', 'attempt_operation_id', 'attempt', 'epoch', 'machine_id', 'native_id')
BINDING_KEYS = ('session_id', 'operation_id', 'attempt_operation_id', 'attempt', 'epoch', 'machine_id')


class IdentityError(ValueError):
    """Malformed or self-contradictory endpoint identity (never treated as equality)."""


def split_attempt_operation_id(value):
    """Strict `<lowercase UUID base>:<attempt>`; anything else (opaque/other scheme) is refused, never stripped."""
    m = _ATTEMPT_OP_RE.fullmatch(value) if isinstance(value, str) else None
    if not m:
        raise IdentityError('attempt operation id %r is not strict <uuid>:<attempt>' % (value,))
    return m.group(1), int(m.group(2))


def _valid_epoch(value):
    return type(value) is int and value >= 0


def _project_shape_identity(view, label, session_key):
    """Project dispatch / final_evidence / nested exit evidence: base + attempt alias (+ optional int attempt)."""
    if not isinstance(view, dict):
        raise IdentityError('%s missing or malformed' % label)
    base = view.get('operation_id')
    if not (isinstance(base, str) and re.fullmatch(_BASE_OP_RE, base)):
        raise IdentityError('%s operation_id %r is not a base UUID' % (label, base))
    att_base, attempt = split_attempt_operation_id(view.get('attempt_operation_id'))
    if att_base != base:
        raise IdentityError('%s attempt_operation_id base %r != operation_id %r' % (label, att_base, base))
    if 'attempt' in view and view.get('attempt') is not None and (type(view['attempt']) is not int or view['attempt'] != attempt):
        raise IdentityError('%s attempt %r contradicts attempt_operation_id index %d' % (label, view['attempt'], attempt))
    return {
        'session_id': view.get(session_key), 'operation_id': base, 'attempt_operation_id': view['attempt_operation_id'],
        'attempt': attempt, 'epoch': view.get('epoch') if _valid_epoch(view.get('epoch')) else None,
        'machine_id': view.get('machine_id'), 'native_id': view.get('native_id') if valid_native_id(view.get('native_id')) else None,
    }


def machine_session_identity(row):
    """Normalize a Machine `sessions show`/`stop_wait` row. `operation_id` IS the attempt id."""
    if not isinstance(row, dict):
        raise IdentityError('session view missing or malformed')
    op = row.get('operation_id')
    base, attempt = split_attempt_operation_id(op)
    # Optional redundant aliases are not required (Machine does not emit them); if present they must agree.
    if row.get('attempt_operation_id') is not None and row.get('attempt_operation_id') != op:
        raise IdentityError('session attempt_operation_id %r contradicts Machine operation_id %r'
                            % (row.get('attempt_operation_id'), op))
    if row.get('attempt') is not None and (type(row.get('attempt')) is not int or row.get('attempt') != attempt):
        raise IdentityError('session attempt %r contradicts Machine operation_id %r' % (row.get('attempt'), op))
    if row.get('dispatch_id') is not None and row.get('dispatch_id') != base:
        raise IdentityError('session dispatch_id %r contradicts Machine operation_id %r' % (row.get('dispatch_id'), op))
    machine = row.get('machine_id')
    for alias in ('host', 'machine'):
        if row.get(alias) is not None and machine is not None and row.get(alias) != machine:
            raise IdentityError('session %s %r contradicts machine_id %r' % (alias, row.get(alias), machine))
    ident = {
        'session_id': row.get('id'), 'operation_id': base, 'attempt_operation_id': op, 'attempt': attempt,
        'epoch': row.get('epoch') if _valid_epoch(row.get('epoch')) else None,
        'machine_id': machine if machine is not None else row_host(row),
        'native_id': row.get('native_id') if valid_native_id(row.get('native_id')) else None,
    }
    # Terminal Machine views nest Project-shaped exit evidence; it must repeat the SAME base/attempt tuple.
    nested = row.get('evidence')
    if isinstance(nested, dict) and ('operation_id' in nested or 'attempt_operation_id' in nested):
        inner = _project_shape_identity(nested, 'session exit evidence', 'session_id')
        bad = _identity_problem({k: ident[k] for k in IDENTITY_KEYS if ident[k] is not None}, inner, 'session exit evidence')
        if bad:
            raise IdentityError(bad)
    return ident


def dispatch_identity(dispatch):
    return _project_shape_identity(dispatch, 'current dispatch', 'id')


def final_evidence_identity(final):
    return _project_shape_identity(final, 'final_evidence', 'session_id')


def _owned_identity(row, native):
    """Immutable identity captured from an original start show or a proven-descendant pre-STOP show.

    Malformed/contradictory Machine identity yields all-None operation fields (UNKNOWN, never equality)."""
    try:
        ident = machine_session_identity(row)
    except IdentityError:
        ident = {k: None for k in IDENTITY_KEYS}
        ident['session_id'] = row.get('id') if isinstance(row, dict) else None
        ident['machine_id'] = row_host(row) if isinstance(row, dict) else None
    ident['native_id'] = native if valid_native_id(native) else None
    return ident


def _bind_view(expected, view, normalize, label, keys=BINDING_KEYS):
    """Normalize an endpoint view, then require strict equality on `keys` (missing on either side = UNKNOWN)."""
    try:
        actual = normalize(view)
    except IdentityError as exc:
        return '%s identity invalid: %s' % (label, exc)
    return _identity_problem({k: expected.get(k) for k in keys}, actual, label)


def _stop_view_problem(expected, view, label):
    """A post-STOP Machine view may only CONFIRM the immutable expected tuple (it never overwrites it).

    A native id is compared when the immutable tuple has one; a late native on a NULL-native original is not
    adopted here (its absence is still checked) and keeps the dispatch binding UNKNOWN."""
    keys = BINDING_KEYS + (('native_id',) if expected.get('native_id') is not None else ())
    return _bind_view(expected, view, machine_session_identity, label, keys)


def final_stop_proof_problem(final, expected, intake_id, label='final_evidence'):
    """Normal operator STOP: Project final_evidence must prove stop_confirmed + exit + empty containment for the
    exact owned base/attempt tuple, including the nested finish_after_stop repetition."""
    if not isinstance(final, dict):
        return '%s missing (UNKNOWN, not clean)' % label
    bad = _bind_view(expected, final, final_evidence_identity, label, IDENTITY_KEYS)
    if bad:
        return bad
    if final.get('stopped') is not True or final.get('reason') != 'stop_confirmed':
        return '%s is not a confirmed stop (stopped=%r reason=%r)' % (label, final.get('stopped'), final.get('reason'))
    comp = final.get('completion')
    if not isinstance(comp, dict) or comp.get('reason') != 'session_stop_confirmed' or comp.get('session_id') != expected.get('session_id'):
        return '%s completion is not session_stop_confirmed for the owned session' % label
    fas = final.get('finish_after_stop')
    bad = _bind_view(expected, fas, final_evidence_identity, label + '.finish_after_stop', IDENTITY_KEYS)
    if bad:
        return bad
    inner = fas.get('evidence') if isinstance(fas.get('evidence'), dict) else {}
    if (fas.get('state') != 'stopped' or fas.get('native_exit_confirmed') is not True
            or inner.get('containment_empty') is not True or fas.get('intake_id') != intake_id):
        return '%s.finish_after_stop lacks stopped/native exit/empty containment proof' % label
    return None


def stop_and_drain_lineage(r, rec, run_id, clock, sleep):
    """V2 owned-lineage cleanup within ONE overall 60s budget, then exact-CAS terminal archival.

    Order: unconditional STOP of every known owned SID (even native_id NULL) -> complete paged lineage
    scan -> mandatory fresh current dispatch/assignment custody bound strictly to the immutable original
    or proven-descendant identity -> exact-CAS archive -> cleanup CONFIRMED only after archive proof."""
    sid = rec.get('id')
    child_id = rec.get('child_intake_id')
    err = rec['errors']
    if not sid:
        return
    t0 = clock()
    deadline = t0 + STOP_BUDGET_S
    rec.setdefault('cleanup_lineage', [])
    rec['cleanup_confirmed'] = False
    rec['cleanup_status'] = 'PENDING'
    owned_sids = {sid}
    # Immutable original identity recorded from the start show; NEVER overwritten from stop/show views.
    original = rec.get('owned_identity')
    if not isinstance(original, dict):
        # No validated start show: every operation field is UNKNOWN (STOP still runs first, archive never).
        original = {k: None for k in IDENTITY_KEYS}
        original.update(session_id=sid, machine_id=rec.get('host'),
                        native_id=rec.get('native_id') if valid_native_id(rec.get('native_id')) else None)
    owned_meta = {sid: dict(original)}
    stopped_sids = {}
    descendant_count = 0

    def unknown(msg):
        rec['cleanup_status'] = 'UNKNOWN'
        rec['cleanup_confirmed'] = False
        err.append(msg)
        rec['stop_s'] = round(clock() - t0, 1)

    try:
        sess, stop_err = _stop_single_session(r, sid, rec.get('native_id'), rec['stop_key'], deadline, clock, sleep)
        stopped_sids[sid] = sess
        if stop_err:
            err.append(stop_err)
    except Exception as exc:
        return unknown('stop failed: %s' % exc)
    # The stop view may only CONFIRM the original identity; a changed/missing binding is drift/UNKNOWN.
    drift = _stop_view_problem(original, sess, 'stopped session %s' % sid)
    if drift:
        return unknown(drift)

    if not child_id:
        rec['stop_s'] = round(clock() - t0, 1)
        if not err:
            rec['cleanup_status'] = 'CONFIRMED_NO_INTAKE'
            rec['cleanup_confirmed'] = True
        else:
            rec['cleanup_status'] = 'UNKNOWN'
        return

    # Drain any source-proven automatic continuation/verification descendants within the same 60s budget.
    while True:
        try:
            snap, scan_err = scan_intake_lineage(r, child_id, deadline, clock)
        except Exception as exc:
            snap, scan_err = None, str(exc)
        if scan_err:
            return unknown('cleanup lineage scan UNKNOWN: %s' % scan_err)

        intake_row = snap['show']
        current_sid = intake_row.get('session_id')
        new_desc = extract_proven_descendants(snap['events'], owned_sids)
        pending_desc = [(kind, dsid) for kind, dsid in new_desc if dsid not in stopped_sids]

        if current_sid not in owned_sids and current_sid not in {d for _, d in new_desc}:
            return unknown('foreign, missing or unprovable current session %r assigned to child intake' % current_sid)
        if intake_row.get('assigned_machine_id') != HOST:
            return unknown('current assigned_machine_id %r is not %s (missing is UNKNOWN)'
                           % (intake_row.get('assigned_machine_id'), HOST))

        if not pending_desc:
            dispatch = intake_row.get('dispatch')
            if not isinstance(dispatch, dict):
                return unknown('missing or malformed current dispatch on child intake (UNKNOWN, not clean)')
            d_sid = dispatch.get('id')
            if d_sid not in owned_sids or d_sid != current_sid or d_sid not in stopped_sids:
                return unknown('foreign or mismatched current dispatch session %r (current=%r)' % (d_sid, current_sid))
            if dispatch.get('state') not in DISPATCH_TERMINAL_STATES:
                return unknown('non-terminal dispatch state %r after drain' % dispatch.get('state'))
            bind = _bind_view(owned_meta[d_sid], dispatch, dispatch_identity, 'current dispatch %s' % d_sid, IDENTITY_KEYS)
            if bind:
                return unknown(bind)
            if dispatch.get('intake_id') is not None and dispatch.get('intake_id') != child_id:
                return unknown('current dispatch intake_id %r != %s' % (dispatch.get('intake_id'), child_id))
            pending_comp = _pending_completion_reason(intake_row)
            if pending_comp:
                return unknown('completion action still %s after drain' % pending_comp)
            break

        for kind, dsid in pending_desc:
            descendant_count += 1
            if descendant_count > MAX_DESCENDANTS:
                return unknown('cleanup lineage exceeded %d descendant bound' % MAX_DESCENDANTS)
            try:
                dsess = session_of(r.json('hub', ['sessions', 'show', dsid, '--json'], left(clock, deadline, 20)), dsid)
            except Exception as exc:
                return unknown('descendant show failed: %s' % exc)
            if dsess.get('intake_id') != child_id or dsess.get('project_id') != PROJECT or row_host(dsess) != HOST:
                return unknown('descendant %s failed intake/project/host binding' % dsid)
            owned_sids.add(dsid)
            # Proven-descendant identity is captured once from its pre-STOP show and is immutable thereafter.
            owned_meta[dsid] = _owned_identity(dsess, dsess.get('native_id'))
            desc_stop_key = '%s-desc-%d' % (rec['stop_key'], descendant_count)
            try:
                dsess_after, d_err = _stop_single_session(r, dsid, dsess.get('native_id'), desc_stop_key, deadline, clock, sleep)
            except Exception as exc:
                return unknown('descendant %s stop failed: %s' % (dsid, exc))
            stopped_sids[dsid] = dsess_after
            rec['cleanup_lineage'].append({
                'kind': kind,
                'session_id': dsid,
                'native_id': dsess.get('native_id'),
                'stopped': d_err is None,
            })
            if d_err:
                return unknown('descendant %s: %s' % (dsid, d_err))
            d_drift = _stop_view_problem(owned_meta[dsid], dsess_after, 'stopped descendant %s' % dsid)
            if d_drift:
                return unknown(d_drift)

    rec['stop_s'] = round(clock() - t0, 1)
    if rec['stop_s'] > STOP_BUDGET_S:
        return unknown('stop took %.1fs > %ds' % (rec['stop_s'], STOP_BUDGET_S))
    if err:
        rec['cleanup_status'] = 'UNKNOWN'
        return
    if any(s.get('state') != 'stopped' or s.get('stop_confirmed') is not True for s in stopped_sids.values()):
        rec['cleanup_status'] = 'UNKNOWN'
        return

    # Stop + custody proven; archive is still PENDING. CONFIRMED only after archive proof.
    rec['cleanup_status'] = 'STOPPED_ARCHIVE_PENDING'
    version = intake_row.get('updated_at')
    if type(version) is not int:
        return unknown('cannot archive child intake: missing integer updated_at version')

    final_bad = final_stop_proof_problem(intake_row.get('final_evidence'), owned_meta[current_sid], child_id)
    if final_bad:
        return unknown('cannot archive: %s' % final_bad)

    def archive_proven(row):
        disp = row.get('dispatch') if isinstance(row, dict) else None
        return (isinstance(row, dict) and row.get('id') == child_id and row.get('status') == 'archived'
                and row.get('session_id') == current_sid and isinstance(disp, dict)
                and disp.get('id') == current_sid and disp.get('state') in DISPATCH_TERMINAL_STATES
                and _bind_view(owned_meta[current_sid], disp, dispatch_identity, 'archived dispatch', IDENTITY_KEYS) is None
                and final_stop_proof_problem(row.get('final_evidence'), owned_meta[current_sid], child_id) is None)

    if intake_row.get('status') == 'archived':
        if archive_proven(intake_row):
            rec['archived'] = True
            rec['cleanup_confirmed'] = True
            rec['cleanup_status'] = 'CONFIRMED'
        else:
            unknown('already-archived child intake lacks terminal owned custody proof')
        return
    if intake_row.get('status') not in ARCHIVABLE_INTAKE_STATUSES:
        return unknown('refusing archive for unexpected child intake status %r' % intake_row.get('status'))
    archive_key = 'acc10-archive-%s-%d' % (run_id, rec['n'])
    note = 'Phase7 acceptance run %s sample %d terminal cleanup (owned_sids=%d)' % (run_id, rec['n'], len(owned_sids))
    arch_err = None
    try:
        updated = r.json('hub', [
            'intake', 'update', child_id,
            '--status', 'archived',
            '--version', str(version),
            '--key', archive_key,
            '--note', note,
        ], left(clock, deadline, 20))
        if not archive_proven(intake_of(updated, child_id)):
            arch_err = 'archive response did not prove terminal owned archival'
    except Exception as exc:
        arch_err = 'archive CAS failed: %s' % exc
    # Final recheck: an uncertain archive is re-read; never CONFIRMED on failed/unknown archive.
    try:
        final = intake_of(r.json('hub', ['intake', 'show', child_id], left(clock, deadline, 10)), child_id)
    except Exception as exc:
        final = None
        arch_err = arch_err or 'archive final recheck failed: %s' % exc
    if final is not None and archive_proven(final):
        rec['archived'] = True
        rec['cleanup_confirmed'] = True
        rec['cleanup_status'] = 'CONFIRMED'
        return
    rec['cleanup_status'] = 'ARCHIVE_PENDING'
    err.append('child intake %s archive unproven (%s); custody remains pending' % (child_id, arch_err or 'final recheck not archived'))


# --- Core Sample Execution ---
def build_start_argv(workspace, child_intake_id, start_key):
    """Build exact V1+V2 interactive model-less start argv (no prompt/cwd/engine/model/effort)."""
    return [
        'sessions', 'start',
        '--repo', PROJECT,
        '--workspace', workspace,
        '--machine', HOST,
        '--interactive',
        '--task-kind', 'ask',
        '--intake-task-id', child_intake_id,
        '--key', start_key,
        '--timeout', str(RUNNING_BUDGET_S),
    ]


def _persist_manifest(manifest_path, manifest):
    atomic_write_private(manifest_path, canonical_json_bytes(manifest) + b'\n')


def one_start(r, rec, run_id, parent_intake_id, workspace, output_dir, manifest, manifest_path,
              clock, sleep, seen_intakes, seen_sessions, seen_natives, capture_transports):
    err = rec['errors']
    sample = rec['n']
    rec['create_key'] = 'acc10-create-%s-%d' % (run_id, sample)
    rec['start_key'] = 'acc10-start-%s-%d' % (run_id, sample)
    rec['stop_key'] = 'acc10-stop-%s-%d' % (run_id, sample)

    challenge_path = Path(output_dir) / ('challenge-%d.bin' % sample)
    brief_path = Path(output_dir) / ('brief-%d.txt' % sample)
    ac_path = Path(output_dir) / ('ac-%d.txt' % sample)
    brief_text, ac_text = make_child_brief_and_ac(run_id, sample, parent_intake_id, challenge_path)
    exclusive_write_private(brief_path, brief_text.encode('utf-8'))
    exclusive_write_private(ac_path, ac_text.encode('utf-8'))

    sample_manifest = {
        'sample': sample,
        'create_key': rec['create_key'],
        'create_body_digest': hashlib.sha256(canonical_json_bytes({
            'project': PROJECT, 'title': 'Phase7 owned run %s/%d' % (run_id, sample),
            'text': brief_text, 'acceptance_criteria': ac_text,
        })).hexdigest(),
        'start_key': rec['start_key'],
        'stop_key': rec['stop_key'],
        'challenge_path': str(challenge_path),
    }
    manifest['samples'].append(sample_manifest)
    _persist_manifest(manifest_path, manifest)

    try:
        child_id = create_child_intake(
            r, rec, run_id, sample, parent_intake_id, brief_path, ac_path, brief_text, ac_text,
            seen_intakes, clock,
        )
    except Exception as exc:
        err.append('intake create failed: %s' % exc)
        return
    sample_manifest['child_intake_id'] = child_id

    # Seed 32-byte random challenge AFTER freezing brief/AC and BEFORE start.
    challenge32 = os.urandom(32)
    exclusive_write_private(challenge_path, challenge32, mode=0o400)
    sample_manifest['challenge_sha256'] = hashlib.sha256(challenge32).hexdigest()
    _persist_manifest(manifest_path, manifest)

    argv = build_start_argv(workspace, child_id, rec['start_key'])
    t0 = clock()
    deadline = t0 + RUNNING_BUDGET_S
    rec['_start_argv'] = list(argv)
    try:
        created = r.json('hub', argv, left(clock, deadline))
    except Exception as exc:
        err.append('start failed: %s' % exc)
        sid = recover_owned(r, rec, clock)
        if sid:
            sample_manifest['session_id'] = sid
            _persist_manifest(manifest_path, manifest)
            err.append('start failed after creating session %s (stopped in cleanup)' % sid)
        return

    rec['elapsed_s'] = round(clock() - t0, 3)
    sid = created.get('id') if isinstance(created, dict) else None
    rec['id'] = sid
    if not valid_session_id(sid):
        rec.pop('id', None)
        recover_owned(r, rec, clock)
        err.append('start returned no valid id')
        return
    sample_manifest['session_id'] = sid
    _persist_manifest(manifest_path, manifest)

    if sid in seen_sessions:
        err.append('session id reused within this acceptance run')
        return
    seen_sessions.add(sid)

    if created.get('state') != 'running':
        err.append('default waited start did not return running')
    err.extend('start ' + p for p in picker_problems(created.get('picker'), created))

    row = session_of(r.json('hub', ['sessions', 'show', sid, '--json'], left(clock, deadline, 60)), sid)
    actual_workspace = row_workspace(row)
    rec.update(
        model=row.get('model'),
        engine=row.get('engine'),
        host=row_host(row),
        native_id=row.get('native_id'),
        requested_workspace=row.get('requested_workspace'),
        workspace=actual_workspace,
        epoch=row.get('epoch'),
    )
    # Immutable expected tuple from the Machine start show (operation_id is the ATTEMPT id there).
    rec['owned_identity'] = _owned_identity(row, row.get('native_id'))
    rec.update({k: rec['owned_identity'][k] for k in ('operation_id', 'attempt_operation_id', 'attempt')})
    try:
        machine_session_identity(row)
    except IdentityError as exc:
        err.append('start show identity invalid: %s' % exc)
    if rec['owned_identity'].get('session_id') != sid:
        err.append('start show session id %r != %s' % (rec['owned_identity'].get('session_id'), sid))
    # The Project start response (dispatch saga view) must name the SAME base/attempt/epoch/machine.
    # Allowed difference: only the source-backed previous-attempt relation (project-do.ts:1016) when the waited
    # start fell back after the POST response; base and session must still be identical.
    created_bind = _bind_view(rec['owned_identity'], created, lambda v: _project_shape_identity(v, 'start response', 'id'),
                              'start response', BINDING_KEYS)
    if created_bind:
        try:
            created_ident = _project_shape_identity(created, 'start response', 'id')
        except IdentityError:
            created_ident = None
        own = rec['owned_identity']
        fallback_relation = (
            created_ident is not None and own.get('operation_id') is not None
            and created_ident['session_id'] == own['session_id'] == sid
            and created_ident['operation_id'] == own['operation_id']
            and type(own.get('attempt')) is int and created_ident['attempt'] == own['attempt'] - 1
            and created_ident['attempt_operation_id'] == '%s:%d' % (own['operation_id'], own['attempt'] - 1))
        if not fallback_relation:
            err.append(created_bind)
    if row.get('state') != 'running':
        err.append('state %r not running' % row.get('state'))
    if row_host(row) != HOST:
        err.append('host %r != %s' % (row_host(row), HOST))
    if row.get('mode') != 'interactive' or row.get('interactive') is not True:
        err.append('session is not interactive (mode=%r interactive=%r)' % (row.get('mode'), row.get('interactive')))
    if row.get('intake_id') and row.get('intake_id') != child_id:
        err.append('show intake_id %r != %s' % (row.get('intake_id'), child_id))
    if row.get('project_id') and row.get('project_id') != PROJECT:
        err.append('show project_id %r != %s' % (row.get('project_id'), PROJECT))
    if row.get('requested_workspace') is not None and row.get('requested_workspace') != workspace:
        err.append('requested_workspace %r != %s' % (row.get('requested_workspace'), workspace))
    if not isinstance(actual_workspace, str) or not actual_workspace.startswith('/') or '\x00' in actual_workspace:
        err.append('actual child workspace invalid: %r' % (actual_workspace,))
        return

    err.extend(picker_problems(row.get('picker'), row))
    current, initial = row.get('picker'), created.get('picker')
    if isinstance(current, dict) and isinstance(initial, dict):
        if current.get('attempt') == initial.get('attempt'):
            if current != initial:
                err.append('same attempt picker evidence changed after start')
        elif (current.get('attempt') != 1 or initial.get('attempt') != 0
              or not current.get('history') or current['history'][0] != choice_of(initial)):
            err.append('picker transition is not the recorded fallback')
    if isinstance(current, dict):
        rec['policy_id'] = current.get('policy_id')
        pool = current.get('pool') or []
        rec['pool_size'] = len(pool)
        rec['shares'] = {x.get('policy_id'): x.get('share') for x in pool if isinstance(x, dict)}
        rec['shares_source'] = 'session.picker'
        if not picker_problems(current, row):
            rec['picker'] = dict(
                choice_of(current),
                pool=[{k: option[k] for k in ('policy_id', 'engine', 'model', 'effort', 'weight', 'share')}
                      for option in pool],
                history=[choice_of(choice) for choice in current['history']],
            )
        if is_luna(rec.get('model'), rec.get('policy_id')) and len({x.get('model') for x in pool if isinstance(x, dict)}) <= 1:
            err.append('luna picked from a degraded one-model pool')
    choices = current.get('history', []) if isinstance(current, dict) else []
    if (is_astra(rec.get('model'), rec.get('engine'), rec.get('policy_id'))
            or any(isinstance(choice, dict) and is_astra(choice.get('model'), choice.get('engine'), choice.get('policy_id'))
                   for choice in choices)):
        err.append('astra picked')

    native_id = rec.get('native_id')
    if not valid_native_id(native_id):
        err.append('no canonical native UUID on running session')
        return
    if native_id in seen_natives:
        err.append('native UUID reused within this acceptance run')
        return
    seen_natives.add(native_id)

    live_map = live_native_rows(r, left(clock, deadline, 60))
    if native_id not in live_map:
        err.append('native not live in a list --running')
        return
    native_ws = live_map[native_id].get('workspace')
    if native_ws is not None and native_ws != actual_workspace:
        err.append('native workspace %r != %s' % (native_ws, actual_workspace))
        return

    engine = rec.get('engine')
    if engine in UNQUALIFIED_NATIVE_FRAMING:
        # Honest unavailable gate: artifact-only evidence is still verified and recorded, but it can never
        # bootstrap native authority. The sample is not resampled and cannot count toward the formal ten.
        info = UNQUALIFIED_NATIVE_FRAMING[engine]
        rec['native_proof'] = {k: info[k] for k in ('status', 'reason', 'root_checker_status',
                                                    'native_action_verified', 'typed_stdout_verified')}
        rec['capture_verified'] = False
        adeadline = clock() + CAPTURE_BUDGET_S
        while clock() < adeadline:
            try:
                work_info = verify_work_artifact(
                    r, actual_workspace, run_id, sample, parent_intake_id, child_id, sid, native_id, challenge32,
                    clock, adeadline, require_probe=True,
                )
                rec['artifact_only'] = {k: work_info[k] for k in (
                    'artifact_sha256', 'proof_digest', 'head', 'tracked_status_sha256', 'probe_sha256')}
                break
            except (OSError, ValueError, RuntimeError, TimeoutError):
                pass
            if clock() >= adeadline:
                break
            sleep(min(1, max(0.01, adeadline - clock())))
        err.append('native proof %s for engine %s: %s (artifact-only=%s; %s)' % (
            info['status'], engine, info['reason'], 'VALIDATED' if rec.get('artifact_only') else 'absent',
            info['diagnostic']))
        return
    transport = (capture_transports or {}).get(engine)
    if not native_transport_ok(engine, transport, (transport.get('host_release_sha') if isinstance(transport, dict) else None)):
        err.append('capture proof unavailable: no verified interactive native-pane transport for engine %s' % engine)
        return
    rec['capture_transport'] = {
        key: transport[key] for key in (
            'engine', 'mode', 'format', 'proof_class', 'host_release_sha', 'provider_version', 'profile_set_sha256'
        )
    }

    cdeadline = clock() + CAPTURE_BUDGET_S
    while clock() < cdeadline:
        try:
            # Verify native pane is still live before capturing.
            live_now = live_native_rows(r, left(clock, cdeadline, 20))
            if native_id not in live_now:
                err.append('native exited before work proof completed')
                return
            rc_cap, cap_out, _cap_err = r.run('a', ['capture', native_id, '--screen', '--plain'], left(clock, cdeadline, 20))
            cap_acquired_ns = time.time_ns()
            if rc_cap == 0 and cap_out:
                work_info = verify_work_artifact(
                    r, actual_workspace, run_id, sample, parent_intake_id, child_id, sid, native_id, challenge32,
                    clock, cdeadline, require_probe=(engine == 'antigravity'),
                )
                expected_line = 'AC10_WORK_V2 %s %d %s %s %d' % (
                    run_id, sample, work_info['proof_digest'], work_info['head'], EXPECTED_CALCULATION
                )
                if native_pane_output_ok(cap_out, engine, transport, expected_line, brief_text,
                                         command_id=probe_command(run_id, sample)):
                    if clock() > cdeadline:
                        err.append('work proof completed after %ds proof deadline' % CAPTURE_BUDGET_S)
                        return
                    if cap_acquired_ns < work_info['mtime_ns']:
                        err.append('capture acquired before result file mtime')
                        return
                    cap_bytes = cap_out.encode('utf-8')
                    cap_path = Path(output_dir) / ('capture-%d.txt' % sample)
                    atomic_write_private(cap_path, cap_bytes)
                    rec['capture_verified'] = True
                    rec['capture_sha256'] = hashlib.sha256(cap_bytes).hexdigest()
                    rec['capture_path'] = str(cap_path)
                    rec['artifact_sha256'] = work_info['artifact_sha256']
                    rec['proof_digest'] = work_info['proof_digest']
                    rec['git_head'] = work_info['head']
                    rec['tracked_status_sha256'] = work_info['tracked_status_sha256']
                    rec['tracked_status_entries'] = work_info['tracked_status_entries']
                    rec['calculation'] = work_info['calculation']
                    if work_info.get('probe_sha256'):
                        rec['probe_command'] = probe_command(run_id, sample)
                        rec['probe_sha256'] = work_info['probe_sha256']
                    sample_manifest.update(
                        capture_sha256=rec['capture_sha256'],
                        artifact_sha256=rec['artifact_sha256'],
                        proof_digest=rec['proof_digest'],
                    )
                    _persist_manifest(manifest_path, manifest)
                    return
        except (OSError, ValueError, RuntimeError, TimeoutError):
            pass
        if clock() >= cdeadline:
            break
        sleep(min(1, max(0.01, cdeadline - clock())))
    err.append('Native pane capture and independent V2 artifact did not prove completed work')


def verify_listing(r, rec, clock):
    """`hub sessions list` row carries host, actual child workspace, interactive mode, and native_id."""
    err = rec['errors']
    actual_workspace = rec.get('workspace')
    try:
        rows = list_rows(r.json('hub', ['sessions', 'list', '--json', '--limit', '500'], 120))
    except Exception as exc:
        err.append('list failed: %s' % exc)
        return
    mine = [s for s in rows if s.get('id') == rec['id']]
    if not mine:
        err.append('session absent from hub sessions list')
        return
    row = mine[0]
    if row_host(row) != HOST:
        err.append('host %r != %s' % (row_host(row), HOST))
    ws = row_workspace(row)
    if ws != actual_workspace:
        err.append('workspace path missing or mismatched in list row (%r != %r)' % (ws, actual_workspace))
    if row.get('state') != 'running':
        err.append('session is not running in live Hub list')
    if row.get('mode') != 'interactive' and row.get('interactive') is not True:
        err.append('session is not interactive in live Hub list')
    if row.get('native_id') and row.get('native_id') != rec.get('native_id'):
        err.append('list native_id %r != %s' % (row.get('native_id'), rec.get('native_id')))
    rec['listed_workspace'] = ws


def verify_running_before_stop(r, rec, clock):
    """Mandatory fresh exact-native + Hub running barrier immediately before live STOP."""
    sid, native_id, err = rec.get('id'), rec.get('native_id'), rec['errors']
    try:
        live = live_natives(r, 20)
        sess = session_of(r.json('hub', ['sessions', 'show', sid, '--json'], 20), sid)
    except Exception as exc:
        err.append('pre-stop running barrier failed: %s' % exc)
        return False
    if native_id not in live or sess.get('state') != 'running':
        err.append('natural exit after proof before live STOP (native_live=%s, state=%r)' % (
            native_id in live, sess.get('state')))
        return False
    expected = rec.get('owned_identity') if isinstance(rec.get('owned_identity'), dict) else {}
    drift = _bind_view(expected, sess, machine_session_identity, 'pre-stop session', IDENTITY_KEYS)
    if drift:
        err.append(drift)
        return False
    return True


def summarize(recs, evidence_problems):
    models = sorted({str(x['model']) for x in recs if x.get('model')})
    luna = sum(1 for x in recs if is_luna(x.get('model'), x.get('policy_id')))
    astra = sum(1 for x in recs if is_astra(x.get('model'), x.get('engine'), x.get('policy_id')))
    problems = list(evidence_problems)
    if len(recs) != COUNT:
        problems.append('only %d of %d starts attempted' % (len(recs), COUNT))
    sessions = [rec.get('id') for rec in recs]
    natives = [rec.get('native_id') for rec in recs]
    intakes = [rec.get('child_intake_id') for rec in recs]
    if not all(valid_session_id(value) for value in sessions) or len(set(sessions)) != COUNT:
        problems.append('ten distinct valid session identities required')
    if not all(valid_native_id(value) for value in natives) or len(set(natives)) != COUNT:
        problems.append('ten distinct canonical native UUIDs required')
    if not all(valid_native_id(value) for value in intakes) or len(set(intakes)) != COUNT:
        problems.append('ten distinct child intake UUIDs required')
    if len(models) < MIN_MODELS:
        problems.append('%d distinct models < %d' % (len(models), MIN_MODELS))
    if luna > MAX_LUNA:
        problems.append('luna %d > %d' % (luna, MAX_LUNA))
    if astra:
        problems.append('astra %d != 0' % astra)
    for x in recs:
        for e in x['errors']:
            problems.append('start %d: %s' % (x['n'], e))
    return {'ok': not problems, 'models': models, 'luna': luna, 'astra': astra, 'problems': problems}


def run(r, evidence_path, commit, clock=time.monotonic, sleep=time.sleep, out=None,
        intake=None, workspace=None, output_dir=None, plan_only=False):
    if out is None:
        out = sys.stdout
    window = {'start': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}
    run_id = str(uuid.uuid4())
    evidence_problems, evidence_digest, capture_transports = checked_native_evidence(evidence_path, commit)
    problems = list(evidence_problems)
    if not isinstance(workspace, str) or not workspace.startswith('/') or '\x00' in workspace:
        problems.append('explicit absolute --workspace required')
    if not valid_native_id(intake):
        problems.append('valid parent --intake-task-id UUID required')
    if not output_dir:
        problems.append('explicit private --output-dir required')
    if not plan_only:
        try:
            before = sample_alerts(r)
        except Exception as exc:
            before = None
            problems.append('alert baseline unavailable: %s' % exc)
    else:
        before = None

    if problems:
        print('ACCEPTANCE FAIL (preconditions): %s' % '; '.join(problems), file=out)
        return 1

    try:
        out_dir = make_private_output_dir(output_dir)
    except (OSError, ValueError) as exc:
        print('ACCEPTANCE FAIL (preconditions): output dir must be a NEW private directory: %s' % exc, file=out)
        return 1
    manifest_path = out_dir / ('manifest-%s.json' % run_id)
    manifest = {
        'schema': 'phase7_native_run_manifest_v2',
        'run_id': run_id,
        'parent_intake_id': intake,
        'requested_workspace': workspace,
        'commit': commit,
        'evidence_sha256': evidence_digest,
        'samples': [],
    }
    exclusive_write_private(manifest_path, canonical_json_bytes(manifest) + b'\n')

    if plan_only:
        plan = [
            {
                'sample': i,
                'create_key': 'acc10-create-%s-%d' % (run_id, i),
                'start_argv': build_start_argv(workspace, '<child-intake-%d>' % i, 'acc10-start-%s-%d' % (run_id, i)),
            }
            for i in range(1, COUNT + 1)
        ]
        print(json.dumps({'run_id': run_id, 'manifest': str(manifest_path), 'plan': plan}, indent=2), file=out)
        return 0

    recs = []
    seen_intakes, seen_sessions, seen_natives = set(), set(), set()
    try:
        for i in range(1, COUNT + 1):
            rec = {'n': i, 'errors': []}
            recs.append(rec)
            try:
                one_start(
                    r, rec, run_id, intake, workspace, out_dir, manifest, manifest_path,
                    clock, sleep, seen_intakes, seen_sessions, seen_natives, capture_transports,
                )
            except Exception as exc:
                rec['errors'].append('start crashed: %s' % exc)
            print('start %d/%d id=%s child=%s model=%s engine=%s host=%s elapsed=%ss shares(%s)=%s' % (
                i, COUNT, rec.get('id'), rec.get('child_intake_id'), rec.get('model'), rec.get('engine'),
                rec.get('host'), rec.get('elapsed_s'), rec.get('shares_source'),
                json.dumps(rec.get('shares'), sort_keys=True),
            ), file=out, flush=True)
            if rec.get('id') and not rec['errors']:
                try:
                    verify_listing(r, rec, clock)
                except Exception as exc:
                    rec['errors'].append('listing check crashed: %s' % exc)
            if rec.get('id') and not rec['errors']:
                verify_running_before_stop(r, rec, clock)
            if rec.get('id'):
                stop_and_drain_lineage(r, rec, run_id, clock, sleep)
            if rec['errors']:
                break
    finally:
        for rec in recs:
            # CONFIRMED (archive proven) or ARCHIVE_PENDING (stopped, custody proven, archive unproven after its
            # own final recheck) are not re-driven; pending custody stays explicit, never CONFIRMED.
            if rec.get('cleanup_confirmed') or rec.get('cleanup_status') == 'ARCHIVE_PENDING':
                continue
            try:
                if not rec.get('id') and rec.get('cleanup_uncertain'):
                    recover_owned(r, rec, clock)
                stop_and_drain_lineage(r, rec, run_id, clock, sleep)
            except Exception as exc:
                rec['errors'].append('stop crashed: %s' % exc)
            if rec.get('cleanup_uncertain'):
                rec['errors'].append('cleanup uncertain: could not determine whether start was accepted; original key ' + rec['start_key'])

    window['end'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    evidence_problems, post_digest, _ = checked_native_evidence(evidence_path, commit)
    post_problems = list(evidence_problems)
    if post_digest != evidence_digest:
        post_problems.append('operator-validated evidence bundle changed during run')
    after = None
    post_cutoff_ms = int(time.time() * 1000)
    try:
        after = sample_alerts(r)
        if after['observed_at_ms'] < post_cutoff_ms:
            post_problems.append('post-run alert watermark predates end-of-run cutoff')
        post_problems.extend(alert_changes(before, after))
    except Exception as exc:
        post_problems.append('post-run alert sample unavailable: %s' % exc)
    for rec in recs:
        rec.pop('row', None)
        rec.pop('_start_argv', None)
    summary = summarize(recs, post_problems)
    summary.update(
        run_id=run_id,
        manifest_path=str(manifest_path),
        commit=commit,
        evidence_sha256=evidence_digest,
        window=window,
        starts=recs,
        alert_watermarks={'before': before, 'after': after, 'required_post_observed_at_ms': post_cutoff_ms},
        parent_intake_id=intake,
    )
    print(json.dumps(summary, indent=2, sort_keys=True, default=str), file=out)
    print('ACCEPTANCE %s' % ('PASS' if summary['ok'] else 'FAIL'), file=out)
    return 0 if summary['ok'] else 1


def main(argv=None, runner=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--live', action='store_true', help='really start ten sessions (also needs HUB_ACCEPTANCE_READY=1)')
    ap.add_argument('--plan', action='store_true', help='read-only plan mode: render manifest and argv without invoking Hub')
    ap.add_argument('--evidence', help='Root-validated same-commit native-pane/deployment/canary/CI evidence JSON')
    ap.add_argument('--workspace', help='approved absolute base workspace on hetzner-aplexer')
    ap.add_argument('--cwd', help='deprecated alias for --workspace')
    ap.add_argument('--output-dir', help='new private directory for run manifest, challenges, and captures')
    ap.add_argument('--commit', help='deployed main commit the evidence must match')
    ap.add_argument('--alerts-cmd', help='verified durable sampler JSON (dev_alert_occurrences_v1)')
    ap.add_argument('--intake-task-id', help='parent intake task UUID for provenance and ownership notes')
    args = ap.parse_args(argv)
    ws = args.workspace or args.cwd
    if args.plan:
        return run(
            runner or Runner(alerts_cmd=args.alerts_cmd), args.evidence, args.commit,
            intake=args.intake_task_id, workspace=ws, output_dir=args.output_dir, plan_only=True,
        )
    if not args.live or os.environ.get('HUB_ACCEPTANCE_READY') != '1':
        print('refusing: live run needs --live and HUB_ACCEPTANCE_READY=1 (orchestrator readiness)', file=sys.stderr)
        return 2
    return run(
        runner or Runner(alerts_cmd=args.alerts_cmd), args.evidence, args.commit,
        intake=args.intake_task_id, workspace=ws, output_dir=args.output_dir,
    )


if __name__ == '__main__':
    sys.exit(main())
