"""`hub quota publish`: push `quse --json` evidence into Hub2 account quota pools.

Operator-only (PATCH/PUT on /api/accounts/<id>/api/account[/quota]). Percentages
and reset times only; nothing from quse's private details is forwarded.
"""
import datetime as dt
import json
import math
import subprocess
import time

from . import intake

# quse key -> (hub account label, provider). Labels match worker.ts QUOTA_ACCOUNT_ENGINES.
ACCOUNTS = {
    'claude': ('claude', 'claude'), 'codex': ('codex', 'codex'), 'codex-orin': ('codex-orin', 'codex'),
    'gemini': ('gemini', 'gemini'), 'grok': ('grok', 'grok'), 'copilot': ('copilot', 'copilot'),
    'go': ('shared-go', 'go'), 'zai': ('zai-central', 'zai'),
}
VALID_MS = 15 * 60 * 1000
# Concurrent sessions per account. Capacity 1 turned every second start into account_saturated,
# which Hub reports as quota_unavailable even with headroom.
DEFAULT_CAPACITY = 8
QUSE_TIMEOUT = 90


def read_quse():
    try:
        done = subprocess.run(['quse', '--json'], capture_output=True, text=True, timeout=QUSE_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError(f'quse failed: {type(exc).__name__}') from None
    if done.returncode:
        raise ValueError(f'quse exited {done.returncode}')
    try:
        data = json.loads(done.stdout)
    except json.JSONDecodeError:
        raise ValueError('quse returned invalid JSON') from None
    if not isinstance(data, dict):
        raise ValueError('quse returned an unexpected shape')
    return data


def _ms(value):
    stamp = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError('quota_reset_timezone_required')
    return int(stamp.timestamp() * 1000)


def evidence(entry, now_ms):
    """Lowest measured remaining, or zero for an explicit current provider limit."""
    if not isinstance(entry, dict) or entry.get('status') != 'ok' or entry.get('error'):
        return None
    exhausted = isinstance(entry.get('details'), dict) and entry['details'].get('limit_reached') is True
    windows = entry.get('windows')
    if windows is None and exhausted:
        windows = {}
    if not isinstance(windows, dict) or len(windows) > 32:
        return None
    measured = []
    for window in windows.values():
        if not isinstance(window, dict):
            return None
        left = window.get('percent_remaining')
        if left is None:
            continue
        if type(left) not in (int, float) or not 0 <= left <= 100 or not math.isfinite(left):
            return None
        measured.append(window)
    if not measured and not exhausted:
        return None
    low = min(measured, key=lambda w: w['percent_remaining']) if measured else None
    reset = low.get('reset_at') if low else None
    try:
        reset_ms = _ms(reset) if isinstance(reset, str) else None
    except (ValueError, OverflowError, OSError):
        return None
    if (reset is not None and not isinstance(reset, str)) or (reset_ms is not None and reset_ms <= now_ms):
        return None
    return {'remaining': 0.0 if exhausted else float(low['percent_remaining']), 'captured_at': now_ms,
            'valid_until': min(now_ms + VALID_MS, reset_ms) if reset_ms is not None else now_ms + VALID_MS,
            'reset_at': reset_ms, 'source': 'quse'}


def publish(quse=None, request=intake.request, now_ms=None, enable=False):
    quse = read_quse() if quse is None else quse
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    if not isinstance(quse, dict) or type(now_ms) is not int or now_ms < 0:
        raise ValueError('invalid_quota_snapshot')
    result = {'published': [], 'skipped': {}}
    for key, (account, provider) in ACCOUNTS.items():
        body = evidence(quse.get(key), now_ms)
        if body is None:
            entry = quse.get(key)
            state = entry.get('status') if isinstance(entry, dict) else None
            result['skipped'][account] = 'quota_' + (state if state in {'stale', 'error', 'unavailable'} else 'unmeasured')
            continue
        base = f'/api/accounts/{account}/api/account'
        current = request('GET', base)
        existing = current.get('account')
        grow = bool(existing) and (existing.get('capacity') or 0) < DEFAULT_CAPACITY
        if not existing or grow or (enable and not existing.get('enabled')):
            # Routable by default: pools Hub reports must be usable by engine pickers (model-roulette).
            request('PATCH', base, {'provider': provider, 'enabled': bool(existing.get('enabled')) if existing and not enable else True,
                                    'capacity': max((existing or {}).get('capacity') or 0, DEFAULT_CAPACITY),
                                    'min_remaining': (existing or {}).get('min_remaining') or 0})
        request('PUT', base + '/quota', body)
        result['published'].append(account)
    if not result['published']:
        raise ValueError('quse reported no measured quota window for any account: ' + json.dumps(result['skipped']))
    return result
