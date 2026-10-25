"""Fixed runtime failure summaries, after independently verified native death.

Journal evidence is scoped to the native UUID and its incarnation window. It
never establishes death, releases capacity, or publishes arbitrary log text.
"""
import json
import re
import subprocess


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
    if oom or exit_info.get('oom_killed') is True:
        reason, message = 'engine_oom_killed', 'The running session experienced a kernel OOM kill.'
    elif timeout:
        reason, message = 'engine_runtime_timeout', 'The running engine exceeded its host runtime limit.'
    elif type(exit_info.get('signal')) is int and exit_info['signal'] > 0:
        reason, message = 'engine_signal_exit', 'The running engine terminated from signal ' + str(exit_info['signal']) + '.'
    elif type(exit_info.get('code')) is int:
        reason, message = 'engine_exit_code', 'The running engine exited with code ' + str(exit_info['code']) + ' without a valid result.'
    else:
        reason, message = 'engine_exited_without_result', 'The engine started, then exited without a valid result; the exact runtime cause is unavailable.'
    value = dict(reason=reason, message=message, error_type='RuntimeError', stage='runtime')
    if type(exit_info.get('code')) is int:
        value['exit_code'] = exit_info['code']
    return value
