"""`hub quota publish`: push `quse --json` evidence into Hub2 account quota pools.

Operator-only (PATCH/PUT on /api/accounts/<id>/api/account[/quota]). Percentages
and reset times only; nothing from quse's private details is forwarded.
"""
import datetime as dt
import json
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
    return int(dt.datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp() * 1000)


def evidence(entry, now_ms):
    """Binding window = lowest percent_remaining among measured windows; None if nothing measured."""
    windows = entry.get('windows') if isinstance(entry, dict) and entry.get('status') == 'ok' else None
    measured = [w for w in (windows or {}).values()
                if isinstance(w, dict) and isinstance(w.get('percent_remaining'), (int, float))]
    if not measured:
        return None
    low = min(measured, key=lambda w: w['percent_remaining'])
    reset = low.get('reset_at')
    return {'remaining': float(low['percent_remaining']), 'captured_at': now_ms, 'valid_until': now_ms + VALID_MS,
            'reset_at': _ms(reset) if isinstance(reset, str) else None, 'source': 'quse'}


def publish(quse=None, request=intake.request, now_ms=None, enable=False):
    quse = read_quse() if quse is None else quse
    now_ms = now_ms or int(time.time() * 1000)
    result = {'published': [], 'skipped': {}}
    for key, (account, provider) in ACCOUNTS.items():
        body = evidence(quse.get(key), now_ms)
        if body is None:
            result['skipped'][account] = (quse.get(key) or {}).get('error') or 'no measured window'
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
