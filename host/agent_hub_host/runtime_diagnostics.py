"""Fixed runtime failure summaries, after independently verified native death.

Journal evidence is scoped to the native UUID and its incarnation window. It
never establishes death, releases capacity, or publishes arbitrary log text.
"""
import json
import re
import subprocess


def _sanitize_tail(text, limit=1024):
    """Keep a bounded printable tail; redact likely secret values, never fail.

    Matches the server charset for diagnostic.tail: no control characters
    (no newlines/tabs); lines joined with ' | '.
    """
    try:
        raw = str(text or '')
    except Exception:
        return ''
    raw = re.sub(r'\x1b\[[0-9;]*m', '', raw)
    raw = raw.replace('\x00', '')
    lines = raw[-8192:].splitlines()[-10:]
    cleaned = []
    for line in lines:
        line = ''.join(ch if 32 <= ord(ch) < 127 else ' ' for ch in line).strip()
        # Redact bearer/token/key assignments without keeping the value.
        line = re.sub(r'(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+', r'\1[redacted]', line)
        line = re.sub(r'(?i)(api[_-]?key\s*[:=]\s*)[^\s;]+', r'\1[redacted]', line)
        line = re.sub(r'sk-[A-Za-z0-9_-]{8,}', 'sk-[redacted]', line)
        line = re.sub(r'(?i)\bsk-ant-[A-Za-z0-9_-]{8,}\b', 'sk-ant-[redacted]', line)
        line = re.sub(r'(?i)(token\s*[:=]\s*)[^\s;]+', r'\1[redacted]', line)
        line = re.sub(r'gh[pousr]_[A-Za-z0-9_]{16,}', '[redacted]', line)
        line = re.sub(r'github_pat_[A-Za-z0-9_]{16,}', '[redacted]', line)
        line = re.sub(r'\bAKIA[0-9A-Z]{16}\b', '[redacted]', line)
        line = re.sub(r'\bxox[baprs]-[A-Za-z0-9-]{8,}', '[redacted]', line)
        if len(line) > 200:
            line = line[:200]
        if line:
            cleaned.append(line)
    out = ' | '.join(cleaned)
    if len(out) > limit:
        out = out[-limit:]
    return out


def runtime_diagnostic(record, tombstone, *, query=None):
    native_id = record.get('native_id', '')
    if not re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', native_id):
        return None
    start, end = record.get('incarnation'), tombstone.get('finished_at_ms')
    if type(start) is not int or type(end) is not int or not 0 < start <= end:
        return None
    unit = 'aplexer-workload-' + native_id + '.scope'
    oom = timeout = False
    try:
        argv = ['journalctl', '--user', '--unit', unit, '--since', '@' + str(start / 1000),
                '--until', '@' + str(end / 1000 + 1), '--no-pager', '--output=json',
                '--output-fields=MESSAGE,__REALTIME_TIMESTAMP', '--lines=32',
                '--grep=OOM killer|[Tt]imed out|[Tt]imeout|Runtime time limit']
        if query is None:
            raw = subprocess.run(argv, capture_output=True, timeout=5, check=True).stdout
        else:
            raw = query(argv)
        if isinstance(raw, bytes):
            raw = raw.decode('utf-8', 'replace')
        if len(raw) <= 65536:
            for line in raw.splitlines():
                row = json.loads(line)
                stamp = int(row['__REALTIME_TIMESTAMP']) / 1000
                if not start <= stamp <= end + 1000:
                    continue
                message = row.get('MESSAGE', '')
                # Match systemd's complete fixed unit message, never task text.
                oom |= message == unit + ': A process of this unit has been killed by the OOM killer.'
                timeout |= message in {unit + ': Runtime time limit exceeded. Stopping.',
                                       unit + ': Failed with result \'timeout\'.'}
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
        pass  # Missing journal access must not invent a specific cause.
    exit_info = record.get('runtime_exit')
    if not isinstance(exit_info, dict):
        exit_info = {}
    code = exit_info.get('code') if type(exit_info.get('code')) is int else None
    signal = exit_info.get('signal') if type(exit_info.get('signal')) is int and exit_info['signal'] > 0 else None
    wall_ms = None
    try:
        if type(start) is int and type(end) is int and 0 < start <= end:
            wall_ms = end - start
    except Exception:
        wall_ms = None
    tail = _sanitize_tail(record.get('runtime_tail') or tombstone.get('runtime_tail') or '')
    detail = 'code=' + (str(code) if code is not None else 'unknown')
    detail += ', signal=' + (str(signal) if signal is not None else 'none')
    detail += ', wall_time_ms=' + (str(wall_ms) if wall_ms is not None else 'unknown')
    if oom or exit_info.get('oom_killed') is True:
        reason, message = 'engine_oom_killed', 'The running session experienced a kernel OOM kill (' + detail + ').'
    elif timeout:
        reason, message = 'engine_runtime_timeout', 'The running engine exceeded its host runtime limit (' + detail + ').'
    elif signal is not None:
        reason, message = 'engine_signal_exit', 'The running engine terminated from signal ' + str(signal) + ' (' + detail + '); no engine result was produced.'
    elif record.get('containment_empty') is False and code in (None, 0):
        # Intake a19cac9e ddf55d00: the main workload is gone (native row
        # retired / terminal) but the cgroup was still non-empty at the last
        # host capture, i.e. backgrounded children outlived the engine while
        # no result JSON was written. Only used when no exit code/signal
        # explains the death (a real code/signal keeps its own reason).
        reason, message = 'engine_children_running', 'The engine exited while backgrounded child processes were still running (' + detail + '); no engine result was produced.'
    elif code is not None:
        reason, message = 'engine_exit_code', 'The running engine exited with code ' + str(code) + ' (' + detail + '); no engine result was produced.'
    else:
        reason, message = 'engine_exited_without_result', 'The engine started, then exited without a valid result (' + detail + '); no engine result was produced.'
    value = dict(reason=reason, message=message, error_type='RuntimeError', stage='runtime')
    if code is not None:
        value['exit_code'] = code
    if signal is not None:
        value['signal'] = signal
    if wall_ms is not None:
        value['wall_time_ms'] = wall_ms
    if tail:
        value['tail'] = tail
    return value
