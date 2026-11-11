#!/usr/bin/env python3
"""Phase 7 final acceptance: ten consecutive model-less Hub starts.

Fails closed: any missing evidence is a failure, never a skip. Only sessions
this run started are ever stopped. The live run needs ``--live`` and
HUB_ACCEPTANCE_READY=1 (orchestrator readiness); without them the script
exits 2 before touching Hub. All I/O goes through an injectable ``Runner`` so
the offline tests never reach the network.
"""
import argparse
import base64
import json
import hashlib
import stat
import os
import math
import re
import shlex
import subprocess
import sys
import time
import uuid
from pathlib import Path

COUNT = 10
RUNNING_BUDGET_S = 150
STOP_BUDGET_S = 60
CAPTURE_BUDGET_S = 90
MIN_MODELS = 3
MAX_LUNA = 3
HOST = 'hetzner-aplexer'
EVIDENCE_GREEN = ('success', 'green', 'passed', 'pass')



class Runner:
    """Executes `hub`/`a`/alert-sample commands; tests use fake binaries on PATH."""

    def __init__(self, hub='hub', a='a', alerts_cmd=None):
        self.bins = {'hub': [hub], 'a': [a], 'alerts': shlex.split(alerts_cmd) if alerts_cmd else None}

    def run(self, tool, args, timeout=300):
        base = self.bins.get(tool)
        if not base:
            return 127, '', 'tool %s not configured' % tool
        try:
            proc = subprocess.run([*base, *(args if tool != 'alerts' else [])], capture_output=True, text=True,
                                  timeout=timeout)
        except subprocess.TimeoutExpired:
            return 124, '', 'timeout after %ss' % timeout
        except OSError as exc:
            return 127, '', str(exc)
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


EVIDENCE_LIMIT = 128000
RECEIPT_LIMIT = 2 * 1024 * 1024


def unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('duplicate JSON field')
        value[key] = item
    return value


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


def evidence_text(value, bound):
    return (isinstance(value, str) and bool(value.strip()) and len(value.encode()) <= bound
            and not any(ord(char) < 32 or ord(char) == 127 for char in value))


def check_evidence(path, commit):
    """Compatibility view for callers interested only in validation errors."""
    return checked_evidence(path, commit)[0]


def checked_evidence(path, commit):
    """Validate and hash the SAME bounded byte buffer, never reopen for its pin.

    Remote authenticity remains Root's responsibility. A concurrent replacement
    cannot become the baseline unless those exact replacement bytes were read
    and validated together on this invocation.
    """
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
    """Check the parsed Root-supplied receipt pins, not remote authenticity."""
    problems = []
    if not isinstance(ev, dict):
        return ['evidence is not an object']
    if not isinstance(commit, str) or not re.fullmatch(r'[0-9a-f]{40}', commit):
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
    if (ev.get('kind') != 'phase7_operator_validated_evidence_v1' or not isinstance(validation, dict)
            or set(validation) != {'actor', 'target_commit', 'receipts', 'capture_transports'}
            or not evidence_text(validation.get('actor'), 120) or validation.get('target_commit') != commit
            or not isinstance(validation.get('receipts'), dict)
            or set(validation['receipts']) != {'deployment', 'canary', 'ci_e2e'}):
        problems.append('operator-validated same-commit receipt references required')
        return problems
    host_sha = validation.get('target_commit')
    transports = validation.get('capture_transports')
    if (not isinstance(host_sha, str) or not re.fullmatch(r'[0-9a-f]{40}', host_sha)
            or not isinstance(transports, dict) or not 1 <= len(transports) <= 3
            or any(not capture_transport_ok(engine, record, host_sha) for engine, record in transports.items())):
        problems.append('verified engine/host/stream capture transport records required')
        return problems
    receipts = dict(validation['receipts'])
    receipts.update({'capture_' + engine: record for engine, record in transports.items()})
    for name, receipt in receipts.items():
        if (not isinstance(receipt, dict) or set(receipt) - ({'commit', 'reference', 'sha256', 'path'} if not name.startswith('capture_') else CAPTURE_KEYS)
                or not {'reference', 'sha256'} <= set(receipt)
                or not name.startswith('capture_') and receipt.get('commit') != commit or not evidence_text(receipt.get('reference'), 2048)
                or not isinstance(receipt.get('sha256'), str) or not re.fullmatch(r'[0-9a-f]{64}', receipt['sha256'])):
            problems.append('%s receipt identity invalid or commit mismatch' % name)
            continue
        if 'path' in receipt:
            if not evidence_text(receipt['path'], 4096):
                problems.append('%s local receipt path invalid' % name)
                continue
            source = Path(receipt['path'])
            if not source.is_absolute():
                source = Path(path).parent / source
            try:
                if hashlib.sha256(bounded_file(source, RECEIPT_LIMIT)).hexdigest() != receipt['sha256']:
                    problems.append('%s local receipt hash mismatch' % name)
            except (OSError, ValueError):
                problems.append('%s local receipt unreadable or invalid' % name)
    return problems


ALERT_SAMPLE_MAX_AGE_MS = 60000
ALERT_SAMPLE_FUTURE_SKEW_MS = 5000


def sample_alerts(r, wall_clock=time.time):
    """Require a durable cumulative occurrence source, including resolved and deduplicated alerts.

    This is deliberately not the active-alert listing API. The operator must
    provide a verified durable source implementing this exact versioned contract.
    """
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


def make_probe():
    marker = 'ACCEPTANCE-OK:' + uuid.uuid4().hex
    encoded = base64.b64encode(marker.encode()).decode()
    prompt = ('Use two sequential shell tool calls. First run `python3 -c '
              "'import base64; print(base64.b64decode(\"%s\").decode())'` and let it finish. "
              'Then use a separate shell tool call to run `sleep 60` so the orchestrator can stop this session. '
              'Do not combine printing and sleeping. Do not edit files, start another agent, or exit early.' % encoded)
    return prompt, marker


CAPTURE_FORMATS = {'codex': 'codex_jsonl_v1', 'zai': 'codex_jsonl_v1', 'go': 'opencode_json_v1'}
CAPTURE_KEYS = {'engine', 'machine_id', 'format', 'host_release_sha', 'provider_version',
                'profile_set_sha256', 'reference', 'sha256', 'path'}


def capture_transport_ok(engine, record, host_sha):
    """Root attests the effective argv/protocol, never inferred from output text."""
    return (engine in CAPTURE_FORMATS and isinstance(record, dict)
            and not set(record) - CAPTURE_KEYS and CAPTURE_KEYS - {'path'} <= set(record)
            and record.get('engine') == engine and record.get('machine_id') == HOST
            and record.get('format') == CAPTURE_FORMATS[engine]
            and isinstance(host_sha, str) and re.fullmatch(r'[0-9a-f]{40}', host_sha) is not None
            and record.get('host_release_sha') == host_sha
            and evidence_text(record.get('provider_version'), 120)
            and evidence_text(record.get('reference'), 2048)
            and all(isinstance(record.get(key), str) and re.fullmatch(r'[0-9a-f]{64}', record[key])
                    for key in ('profile_set_sha256', 'sha256')))


def probe_output_ok(captured, prompt, expected, engine=None, transport=None):
    """Parse only the Root-verified engine's stream; plaintext JSON is not proof.

    Claude/Antigravity remain unsupported even with forged stream-shaped text.
    The caller supplies a record extracted from the same validated evidence bytes.
    """
    if (not capture_transport_ok(engine, transport, (transport.get('host_release_sha') if isinstance(transport, dict) else None))
            or expected in prompt or not isinstance(captured, str)):
        return False

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


def session_of(view, expected_id=None):
    """`hub sessions show --json` is {"session": {...}}; a flat or foreign shape is an error, never guessed."""
    sess = view.get('session') if isinstance(view, dict) else None
    if not isinstance(sess, dict) or not sess.get('id'):
        raise RuntimeError('show response is not {"session": {...}}')
    if expected_id is not None and sess['id'] != expected_id:
        raise RuntimeError('show session id mismatch with requested session')
    return sess


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


def live_natives(r, timeout):
    """Native sessions live right now: `a list --json --running`, filtered to state running (exited rows ignored)."""
    rows = r.json('a', ['list', '--json', '--running'], timeout)
    if not isinstance(rows, list):
        raise RuntimeError('a list --json is not an array')
    return {x['id'] for x in rows if isinstance(x, dict) and x.get('id')
            and x.get('phase') not in ('exited', 'dead', 'stopped')
            and x.get('state') not in ('exited', 'dead', 'stopped')}


def pool_problems(pool):
    """Valid, nonempty pool with finite positive shares summing to ~1."""
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
    operation = session.get('operation_id') or session.get('attempt_operation_id')
    suffix = re.search(r':([01])$', operation) if isinstance(operation, str) else None
    attempt = int(suffix.group(1)) if suffix else session.get('attempt')
    if picker.get('attempt') != attempt:
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


def valid_native_id(value):
    return isinstance(value, str) and re.fullmatch(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', value) is not None


def valid_session_id(value):
    return isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}', value) is not None


def reconcile_by_key(r, argv, key, deadline, clock):
    """Replay the identical request/key, changing only the CLI's local wait behavior.

    Even a 4xx can be from the wait after acceptance, so errors are conservatively
    ambiguous. A same-key replay never authorizes a different launch request.
    """
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
    sid = reconcile_by_key(r, rec['_start_argv'], rec['key'], clock() + 60, clock)
    if sid:
        rec['id'] = sid
        rec['cleanup_uncertain'] = False
    else:
        rec['cleanup_uncertain'] = True
    return sid


def one_start(r, rec, clock, sleep, intake=None, workspace=None, seen_sessions=None, seen_natives=None, capture_transports=None):
    err = rec['errors']
    prompt, expected = make_probe()
    key = 'acc10-' + uuid.uuid4().hex
    argv = ['sessions', 'start', '--repo', 'alexeygrigorev/agent-hub', '--task-kind', 'ask',
            '--machine', HOST, '--cwd', workspace, '--prompt', prompt, '--key', key,
            '--timeout', str(RUNNING_BUDGET_S)]
    if intake:
        argv += ['--intake-task-id', intake]
    t0 = clock()
    deadline = t0 + RUNNING_BUDGET_S
    rec['key'] = key
    rec['_start_argv'] = list(argv)
    try:
        created = r.json('hub', argv, left(clock, deadline))
    except Exception as exc:
        err.append('start failed: %s' % exc)
        sid = recover_owned(r, rec, clock)
        if sid:
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
    if seen_sessions is not None:
        if sid in seen_sessions:
            err.append('session id reused within this acceptance run')
            return
        seen_sessions.add(sid)
    if created.get('state') != 'running':
        err.append('default waited start did not return running')
    err.extend('start ' + p for p in picker_problems(created.get('picker'), created))
    row = session_of(r.json('hub', ['sessions', 'show', sid, '--json'], left(clock, deadline, 60)), sid)
    rec.update(model=row.get('model'), engine=row.get('engine'), host=row_host(row), native_id=row.get('native_id'))
    if row.get('state') != 'running':
        err.append('state %r not running' % row.get('state'))
    if row_host(row) != HOST:
        err.append('host %r != %s' % (row_host(row), HOST))
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
            rec['picker'] = dict(choice_of(current),
                                 pool=[{k: option[k] for k in ('policy_id', 'engine', 'model', 'effort', 'weight', 'share')}
                                       for option in pool],
                                 history=[choice_of(choice) for choice in current['history']])
        if is_luna(rec.get('model'), rec.get('policy_id')) and len({x.get('model') for x in pool if isinstance(x, dict)}) <= 1:
            err.append('luna picked from a degraded one-model pool')
    choices = current.get('history', []) if isinstance(current, dict) else []
    if (is_astra(rec.get('model'), rec.get('engine'), rec.get('policy_id'))
            or any(isinstance(choice, dict) and is_astra(choice.get('model'), choice.get('engine'), choice.get('policy_id'))
                   for choice in choices)):
        err.append('astra picked')
    if not valid_native_id(rec.get('native_id')):
        err.append('no canonical native UUID on running session')
        return
    if seen_natives is not None:
        if rec['native_id'] in seen_natives:
            err.append('native UUID reused within this acceptance run')
            return
        seen_natives.add(rec['native_id'])
    if rec['native_id'] not in live_natives(r, left(clock, deadline, 60)):
        err.append('native not live in a list --running')
    transport = (capture_transports or {}).get(rec.get('engine'))
    if not capture_transport_ok(rec.get('engine'), transport, (transport.get('host_release_sha') if isinstance(transport, dict) else None)):
        err.append('capture proof unavailable: no verified stream transport for engine %s' % rec.get('engine'))
        return
    rec['capture_transport'] = {key: transport[key] for key in
                                ('engine', 'format', 'host_release_sha', 'provider_version', 'profile_set_sha256')}
    cdeadline = clock() + CAPTURE_BUDGET_S
    while clock() < cdeadline:
        try:
            captured = r.json('hub', ['sessions', 'capture', sid, '--wait', '--timeout', str(max(1, int(left(clock, cdeadline)))),
                                    '--max-bytes', '65536', '--key', 'acc-capture-' + uuid.uuid4().hex],
                              left(clock, cdeadline))
        except (RuntimeError, TimeoutError):
            if clock() >= cdeadline:
                break
            sleep(min(1, max(0, cdeadline - clock())))
            continue
        wait = captured.get('command_wait', {}) if isinstance(captured, dict) else {}
        ack = wait.get('ack', {})
        if wait.get('state') == 'acked' and isinstance(ack, dict) and probe_output_ok(ack.get('capture'), prompt, expected, rec.get('engine'), transport):
            rec['capture_verified'] = True
            return
        sleep(min(1, max(0, cdeadline - clock())))
    err.append('Hub capture shows no completed tool output beyond the prompt')


def verify_listing(r, rec, clock, workspace):
    """hub sessions list row carries host + workspace (from the actual row)."""
    err = rec['errors']
    try:
        rows = list_rows(r.json('hub', ['sessions', 'list', '--json', '--limit', '500'], 120))
    except Exception as exc:
        err.append('list failed: %s' % exc)
        return
    mine = [s for s in rows if s.get('id') == rec['id']]
    if not mine:
        err.append('session absent from hub sessions list')
        return
    host = row_host(mine[0])
    if host != HOST:
        err.append('host %r != %s' % (host, HOST))
    ws = row_workspace(mine[0])
    if ws != workspace:
        err.append('workspace path missing or mismatched in list row (%r)' % (ws,))
    if mine[0].get('state') != 'running':
        err.append('session is not running in live Hub list')
    rec['listed_workspace'] = ws


def stop_one(r, rec, clock, sleep):
    """Stop within STOP_BUDGET_S total, subprocess timeouts included; native proven gone from the live list."""
    sid, native, err = rec.get('id'), rec.get('native_id'), rec['errors']
    if not sid:
        return
    t0 = clock()
    deadline = t0 + STOP_BUDGET_S
    try:
        r.json('hub', ['sessions', 'stop', sid, '--wait', '--timeout', str(max(1, STOP_BUDGET_S - 5))],
               left(clock, deadline))
        sess = session_of(r.json('hub', ['sessions', 'show', sid, '--json'], left(clock, deadline, 60)), sid)
    except Exception as exc:
        err.append('stop failed: %s' % exc)
        rec['stop_s'] = round(clock() - t0, 1)
        return
    if sess.get('state') != 'stopped':
        err.append('state after stop %r != stopped' % sess.get('state'))
    if not valid_native_id(native):
        native = sess.get('native_id')
    if not valid_native_id(native):
        err.append('cleanup native UUID unconfirmed; authenticated stop requested but absence cannot be proved')
        return
    while native:
        try:
            gone = native not in live_natives(r, left(clock, deadline, 60))
        except Exception as exc:
            err.append('a list failed: %s' % exc)
            break
        if gone:
            rec['cleanup_confirmed'] = sess.get('state') == 'stopped'
            break
        if clock() + 3 >= deadline:
            err.append('native %s still listed after %ds' % (native, STOP_BUDGET_S))
            break
        sleep(3)
    rec['stop_s'] = round(clock() - t0, 1)
    if rec['stop_s'] > STOP_BUDGET_S:
        err.append('stop took %.1fs > %ds' % (rec['stop_s'], STOP_BUDGET_S))


def summarize(recs, evidence_problems):
    models = sorted({str(x['model']) for x in recs if x.get('model')})
    luna = sum(1 for x in recs if is_luna(x.get('model'), x.get('policy_id')))
    astra = sum(1 for x in recs if is_astra(x.get('model'), x.get('engine'), x.get('policy_id')))
    problems = list(evidence_problems)
    if len(recs) != COUNT:
        problems.append('only %d of %d starts attempted' % (len(recs), COUNT))
    sessions = [rec.get('id') for rec in recs]
    natives = [rec.get('native_id') for rec in recs]
    if not all(valid_session_id(value) for value in sessions) or len(set(sessions)) != COUNT:
        problems.append('ten distinct valid session identities required')
    if not all(valid_native_id(value) for value in natives) or len(set(natives)) != COUNT:
        problems.append('ten distinct canonical native UUIDs required')
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


def run(r, evidence_path, commit, clock=time.monotonic, sleep=time.sleep, out=sys.stdout,
        intake=None, synthetic_ac=False, workspace=None):
    window = {'start': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}
    evidence_problems, evidence_digest, capture_transports = checked_evidence(evidence_path, commit)
    problems = list(evidence_problems)
    if not isinstance(workspace, str) or not workspace.startswith('/') or '\x00' in workspace:
        problems.append('explicit absolute --cwd workspace required')
    if not intake and not synthetic_ac:
        problems.append('no --intake-task-id and no --synthetic-ac-authorized (acceptance criteria source missing)')
    try:
        before = sample_alerts(r)
    except Exception as exc:
        before = None
        problems.append('alert baseline unavailable: %s' % exc)
    recs = []
    seen_sessions, seen_natives = set(), set()
    if problems:  # fail closed before touching Hub
        print('ACCEPTANCE FAIL (preconditions): %s' % '; '.join(problems), file=out)
        return 1
    try:
        for i in range(1, COUNT + 1):
            rec = {'n': i, 'errors': []}
            recs.append(rec)
            try:
                one_start(r, rec, clock, sleep, intake, workspace, seen_sessions, seen_natives, capture_transports)
            except Exception as exc:  # keep the created id for cleanup
                rec['errors'].append('start crashed: %s' % exc)
            print('start %d/%d id=%s model=%s engine=%s host=%s elapsed=%ss shares(%s)=%s' % (
                i, COUNT, rec.get('id'), rec.get('model'), rec.get('engine'), rec.get('host'), rec.get('elapsed_s'),
                rec.get('shares_source'), json.dumps(rec.get('shares'), sort_keys=True)), file=out, flush=True)
            if rec.get('id') and not rec['errors']:
                try:
                    verify_listing(r, rec, clock, workspace)
                except Exception as exc:
                    rec['errors'].append('listing check crashed: %s' % exc)
            if rec.get('id'):
                stop_one(r, rec, clock, sleep)
            if rec['errors']:
                break  # consecutive means a failed start ends the run
    finally:
        for rec in recs:  # only sessions this run created, always
            if rec.get('cleanup_confirmed'):
                continue
            try:
                if not rec.get('id') and rec.get('cleanup_uncertain'):
                    recover_owned(r, rec, clock)
                stop_one(r, rec, clock, sleep)
            except Exception as exc:
                rec['errors'].append('stop crashed: %s' % exc)
            if rec.get('cleanup_uncertain'):
                rec['errors'].append('cleanup uncertain: could not determine whether start was accepted; original key ' + rec['key'])
    window['end'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    evidence_problems, post_digest, _ = checked_evidence(evidence_path, commit)
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
    summary.update(commit=commit, evidence_sha256=evidence_digest, window=window, starts=recs, alert_watermarks={'before': before, 'after': after, 'required_post_observed_at_ms': post_cutoff_ms},
                   acceptance_criteria=('intake ' + intake) if intake else 'server-synthetic (explicitly authorized)')
    print(json.dumps(summary, indent=2, sort_keys=True, default=str), file=out)
    print('ACCEPTANCE %s' % ('PASS' if summary['ok'] else 'FAIL'), file=out)
    return 0 if summary['ok'] else 1


def main(argv=None, runner=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--live', action='store_true', help='really start ten sessions (also needs HUB_ACCEPTANCE_READY=1)')
    ap.add_argument('--evidence', help='Root-validated same-commit deployment/canary/CI receipt references and SHA256 pins; see docs/HUB-ACCEPTANCE-10.md')
    ap.add_argument('--cwd', help='explicit workspace on hetzner-aplexer')
    ap.add_argument('--commit', help='deployed main commit the evidence must match')
    ap.add_argument('--alerts-cmd', help='verified durable sampler JSON: kind=dev_alert_occurrences_v1, recipient=dev@dtcdev.click, '
                                         'stable generation, cumulative count of ALL occurrences (including resolved/deduplicated), '
                                         'fresh observed_at_ms (post snapshot must cover the end-of-run cutoff). Active alert IDs are insufficient; source must be verified before live use')
    ap.add_argument('--intake-task-id', help='intake task with acceptance criteria passed to every start')
    ap.add_argument('--synthetic-ac-authorized', action='store_true',
                    help='explicit authorization to rely on the server-fabricated synthetic criterion')
    args = ap.parse_args(argv)
    if not args.live or os.environ.get('HUB_ACCEPTANCE_READY') != '1':
        print('refusing: live run needs --live and HUB_ACCEPTANCE_READY=1 (orchestrator readiness)', file=sys.stderr)
        return 2
    return run(runner or Runner(alerts_cmd=args.alerts_cmd), args.evidence, args.commit,
               intake=args.intake_task_id, synthetic_ac=args.synthetic_ac_authorized, workspace=args.cwd)


if __name__ == '__main__':
    sys.exit(main())
