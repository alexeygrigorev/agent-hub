"""Bounded read-only probes of explicitly configured long-lived services.

No discovery, restart, journal text, container environment or inferred failure.
Probe errors and ordinary stops remain unknown. Existing service DTO only.
"""
from datetime import datetime, timezone
import json
import math
import os
import re
import selectors
import subprocess
import time

MAX_OUTPUT = 16384
MAX_SERVICES = 8
PROBE_BUDGET_S = 8.0
LABEL = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z')
UNIT = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.@:-]{0,119}\.service\Z')
CONTAINER = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z')


def bounded_probe(argv, timeout_s):
    """Only the child query is killed on timeout/excess output, never a service."""
    with subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as process:
        deadline = time.monotonic() + timeout_s
        chunks = bytearray()
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            try:
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError('probe_timeout')
                    if not selector.select(remaining):
                        raise TimeoutError('probe_timeout')
                    chunk = os.read(process.stdout.fileno(), min(4096, MAX_OUTPUT + 1 - len(chunks)))
                    if not chunk:
                        break
                    chunks.extend(chunk)
                    if len(chunks) > MAX_OUTPUT:
                        raise ValueError('probe_output_excess')
                if process.wait(timeout=max(.01, deadline - time.monotonic())):
                    raise ValueError('probe_failed')
                return bytes(chunks)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()


def _stamp(value):
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed.timestamp() if parsed.tzinfo is not None else None
    except (ValueError, OverflowError):
        return None


def _recent(at, now, cooldown_s):
    return at is not None and 0 <= now - at <= cooldown_s


def _dto(service_id, worker='unknown', reason='probe_unknown', observed_at=None):
    return {'service_id': service_id, 'worker': worker, 'worker_state': worker,
            'status_file_age_s': 0 if observed_at is not None else None,
            'observed_at': observed_at, 'observation_time': observed_at, 'reason': reason}


def _systemd(definition, raw, now, mono, cooldown):
    fields = dict(line.split('=', 1) for line in raw.decode('utf-8').splitlines() if '=' in line)
    if fields.get('Id') != definition['unit'] or fields.get('LoadState') != 'loaded':
        return 'unknown', 'service_identity_unknown', None
    if fields.get('ActiveState') == 'active' and fields.get('SubState') == 'running':
        return 'alive', 'ok', now
    failed = fields.get('ActiveState') == 'failed' or (fields.get('ActiveState') == 'activating' and fields.get('SubState') == 'auto-restart')
    if not failed:
        return 'unknown', 'service_not_running', None
    # A single exact-unit snapshot must bind the exit to an actual invocation.
    start = int(fields.get('ExecMainStartTimestampMonotonic', '0')) / 1e6
    end = int(fields.get('ExecMainExitTimestampMonotonic', '0')) / 1e6
    if not 0 < start <= end <= mono or mono - end > cooldown:
        return 'unknown', 'failure_time_unknown', None
    result = fields.get('Result')
    code, status = fields.get('ExecMainCode'), int(fields.get('ExecMainStatus', '0'))
    positive = result in ('timeout', 'oom-kill', 'core-dump') or (
        result == 'exit-code' and code == '1' and status > 0) or (
        result == 'signal' and code in ('2', '3') and status not in (0, 2, 15))
    if not positive:
        return 'unknown', 'failure_cause_unknown', None
    return 'broken', 'service_timeout' if result == 'timeout' else 'service_crash', now - (mono - end)


def _docker(definition, raw, now, cooldown):
    # Format confines output to exact container identity and State; never Config.Env.
    row = json.loads(raw)
    if not isinstance(row, dict) or row.get('Name') != '/' + definition['container'] and row.get('Id') != definition['container']:
        return 'unknown', 'service_identity_unknown', None
    if not isinstance(row.get('Id'), str) or not re.fullmatch('[0-9a-f]{64}', row['Id']):
        return 'unknown', 'service_identity_unknown', None
    state = row.get('State')
    if not isinstance(state, dict):
        return 'unknown', 'probe_unknown', None
    if state.get('Running') is True and state.get('Status') == 'running':
        return 'alive', 'ok', now
    start, end = _stamp(state.get('StartedAt')), _stamp(state.get('FinishedAt'))
    failed = (state.get('Running') is False and state.get('Status') in ('exited', 'dead')) or (state.get('Status') == 'restarting' and state.get('Restarting') is True)
    if not failed:
        return 'unknown', 'service_not_running', None
    if start is None or end is None or not 0 < start <= end or not _recent(end, now, cooldown):
        return 'unknown', 'failure_time_unknown', None
    code = state.get('ExitCode')
    if state.get('OOMKilled') is True or (type(code) is int and code not in (0, 130, 143) and 0 < code <= 255):
        return 'broken', 'service_crash', end
    return 'unknown', 'service_stopped', None


def collect_runtime_services(definitions, *, now=None, cooldown_s=300, runner=None, monotonic=time.monotonic):
    """Config entries: {service_id, backend:systemd|docker, unit|container, user?}.

    Missing config performs no query. Invalid/oversized config performs no query.
    Hard bounds: eight exact services, eight seconds total, 16KiB per query.
    """
    if definitions is None:
        return []
    if not isinstance(definitions, list) or len(definitions) > MAX_SERVICES:
        return []
    if type(cooldown_s) is not int or not 30 <= cooldown_s <= 3600:
        return []
    now = time.time() if now is None else now
    if isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(now):
        return []
    runner = runner or bounded_probe
    rows, seen = [], set()
    deadline = monotonic() + PROBE_BUDGET_S
    for definition in definitions:
        if not isinstance(definition, dict):
            continue
        service_id = definition.get('service_id')
        if not isinstance(service_id, str) or not LABEL.fullmatch(service_id) or service_id in seen:
            continue
        seen.add(service_id)
        row = _dto(service_id)
        backend = definition.get('backend')
        unit, container = definition.get('unit'), definition.get('container')
        if backend == 'systemd' and isinstance(unit, str) and UNIT.fullmatch(unit) and type(definition.get('user', False)) is bool and set(definition) <= {'service_id','backend','unit','user'}:
            props = 'Id,LoadState,ActiveState,SubState,Result,ExecMainCode,ExecMainStatus,ExecMainStartTimestampMonotonic,ExecMainExitTimestampMonotonic'
            argv = ['systemctl'] + (['--user'] if definition.get('user') else []) + ['show', '--no-pager', '--property=' + props, '--', unit]
        elif backend == 'docker' and isinstance(container, str) and CONTAINER.fullmatch(container) and set(definition) <= {'service_id','backend','container'}:
            argv = ['docker', 'inspect', '--type', 'container', '--format', '{"Id":{{json .Id}},"Name":{{json .Name}},"State":{{json .State}}}', '--', container]
        else:
            rows.append(_dto(service_id, reason='invalid_service_config'))
            continue
        try:
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise TimeoutError('probe_budget')
            raw = runner(argv, min(3., remaining))
            if not isinstance(raw, bytes) or len(raw) > MAX_OUTPUT:
                raise ValueError('probe_output')
            worker, reason, observed = _systemd(definition, raw, now, monotonic(), cooldown_s) if backend == 'systemd' else _docker(definition, raw, now, cooldown_s)
            stamp = datetime.fromtimestamp(observed, timezone.utc).isoformat().replace('+00:00', 'Z') if observed is not None else None
            row = _dto(service_id, worker, reason, stamp)
        except (OSError, ValueError, TypeError, OverflowError, TimeoutError, subprocess.SubprocessError):
            pass
        rows.append(row)
    return rows
