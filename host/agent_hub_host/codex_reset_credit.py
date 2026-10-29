"""Earned Codex reset credit evidence through the maintained app-server contract.

This module never changes Hub admission or infers a token balance, and it
never redeems a reset (Alexey t1741u: Hub may track expiry and ask for
approval, never redeem). Every operation here only reads provider state.
"""
import argparse
import contextlib
import json
import os
from pathlib import Path
import selectors
import sqlite3
import subprocess
import time

READ = 'account/rateLimits/read'


class CreditError(RuntimeError):
    """A bounded diagnostic without provider messages or credentials."""


class AppServer:
    """One bounded stdio connection using the current managed Codex identity."""
    def __enter__(self):
        self.proc = subprocess.Popen(['codex', 'app-server'], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     bufsize=0)
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.proc.stdout, selectors.EVENT_READ)
        self.buffer = b''
        self.sequence = 0
        try:
            self.call('initialize', {'clientInfo': {'name': 'hub-earned-reset', 'version': '1'},
                                     'capabilities': {'experimentalApi': True}})
            self._send({'method': 'initialized'})
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def _send(self, value):
        self.proc.stdin.write(json.dumps(value).encode() + b'\n')
        self.proc.stdin.flush()

    def call(self, method, params=None):
        if method not in {'initialize', READ}:
            raise CreditError('unsupported_method')
        self.sequence += 1
        request_id = self.sequence
        self._send({'id': request_id, 'method': method, 'params': params or {}})
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if b'\n' not in self.buffer:
                if not self.selector.select(max(0, deadline - time.monotonic())):
                    break
                chunk = os.read(self.proc.stdout.fileno(), 65536)
                if not chunk:
                    raise CreditError('provider_disconnected')
                self.buffer += chunk
                if len(self.buffer) > 1048576:
                    raise CreditError('provider_response_too_large')
                continue
            line, self.buffer = self.buffer.split(b'\n', 1)
            try:
                reply = json.loads(line)
            except ValueError:
                raise CreditError('provider_invalid_json') from None
            if not isinstance(reply, dict):
                raise CreditError('provider_invalid_reply')
            if reply.get('id') != request_id:
                continue
            if 'error' in reply or not isinstance(reply.get('result'), dict):
                raise CreditError('provider_rpc_failed')
            return reply['result']
        raise CreditError('provider_timeout')

    def __exit__(self, *_):
        self.selector.close()
        self.proc.terminate()
        try:
            self.proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=3)
        self.proc.stdin.close()
        self.proc.stdout.close()


def _identifier(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 256 or not all(32 < ord(c) < 127 for c in value):
        raise CreditError('invalid_identifier')
    return value


def snapshot(raw, now=None):
    """Allowlist provider facts; never expose email, tokens, upsells or paid balance."""
    account = _identifier(raw.get('accountId'))
    summary = raw.get('rateLimitResetCredits')
    if not isinstance(summary, dict) or type(summary.get('availableCount')) is not int or summary['availableCount'] < 0:
        raise CreditError('credit_summary_unavailable')
    credits = summary.get('credits')
    if not isinstance(credits, list) or len(credits) > 256:
        raise CreditError('credit_identity_unavailable')
    clean = []
    for credit in credits:
        if not isinstance(credit, dict):
            raise CreditError('invalid_credit')
        expiry = credit.get('expiresAt')
        if expiry is not None and type(expiry) is not int:
            raise CreditError('invalid_expiry')
        clean.append({'id': _identifier(credit.get('id')), 'expiresAt': expiry,
                      'status': credit.get('status') if credit.get('status') in ('available', 'redeeming', 'redeemed') else 'unknown',
                      'resetType': 'codexRateLimits' if credit.get('resetType') == 'codexRateLimits' else 'unknown'})
    if len({c['id'] for c in clean}) != len(clean):
        raise CreditError('duplicate_credit_identity')
    windows = []
    buckets = raw.get('rateLimitsByLimitId')
    if not isinstance(buckets, dict) or not buckets:
        buckets = {'codex': raw.get('rateLimits')}
    for bucket, limits in buckets.items():
        if not isinstance(limits, dict):
            continue
        for name in ('primary', 'secondary'):
            window = limits.get(name)
            if not isinstance(window, dict):
                continue
            used = window.get('usedPercent')
            if type(used) is not int or not 0 <= used <= 100:
                raise CreditError('invalid_quota_window')
            for field in ('resetsAt', 'windowDurationMins'):
                if window.get(field) is not None and type(window[field]) is not int:
                    raise CreditError('invalid_quota_window')
            windows.append({'bucket': _identifier(bucket), 'window': name, 'usedPercent': used,
                            'resetsAt': window.get('resetsAt'),
                            'windowDurationMins': window.get('windowDurationMins')})
    return {'accountId': account, 'observedAt': int(time.time() if now is None else now),
            'availableCount': summary['availableCount'], 'credits': clean, 'windows': windows,
            'ordinaryUsageAllowed': raw.get('ordinaryUsageAllowed') if type(raw.get('ordinaryUsageAllowed')) is bool else None}


def _credit(view, credit_id):
    return next((c for c in view['credits'] if c['id'] == credit_id), None)


def _eligible(view, credit_id, now):
    if view['ordinaryUsageAllowed'] is None:
        raise CreditError('usage_permission_unknown')
    credit = _credit(view, credit_id)
    if credit is None or credit['status'] != 'available' or view['availableCount'] < 1:
        raise CreditError('credit_not_available')
    if credit['resetType'] != 'codexRateLimits':
        raise CreditError('unsupported_reset_type')
    expiry = credit['expiresAt']
    if expiry is None or expiry <= now:
        raise CreditError('credit_expired_or_no_deadline')
    if expiry - now > 86400:
        raise CreditError('credit_not_expiring')
    # Do not reset healthy unused quota merely because a coupon exists. The
    # provider remains the authority for whether a window can actually reset.
    if not any(w['usedPercent'] >= 85 and type(w['resetsAt']) is int and w['resetsAt'] > now
               for w in view['windows']):
        raise CreditError('quota_reset_unnecessary')


def journal_path():
    # Stable across release/worktree locations and invocations of this identity.
    home = Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))).resolve()
    return home / 'hub-earned-reset' / 'attempts.sqlite3'


def retained_receipt(path, account_id, credit_id, idempotency_key):
    """Read an existing exact attempt without creating a journal or sending RPC."""
    for value in (account_id, credit_id, idempotency_key):
        _identifier(value)
    with contextlib.closing(sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)) as db:
        row = db.execute('SELECT key,receipt FROM attempts WHERE account=? AND credit=?',
                         (account_id, credit_id)).fetchone()
    if not row or row[0] != idempotency_key:
        raise CreditError('retained_attempt_identity_mismatch')
    receipt = json.loads(row[1])
    if (receipt.get('accountId'), receipt.get('creditId'), receipt.get('idempotencyKey')) != (account_id, credit_id, idempotency_key):
        raise CreditError('retained_attempt_identity_mismatch')
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    args = parser.parse_args()
    try:
        with AppServer() as rpc:
            result = snapshot(rpc.call(READ))
        print(json.dumps(result, sort_keys=True))
        return 0
    except (CreditError, sqlite3.Error, OSError, ValueError, TypeError):
        # Never print raw provider/OS errors: they can contain private paths or
        # provider payloads.
        print(json.dumps({'error': 'earned_reset_failed_closed'}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
