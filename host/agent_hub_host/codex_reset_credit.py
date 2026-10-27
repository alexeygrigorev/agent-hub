"""Earned Codex reset credits through the maintained app-server contract.

This module never changes Hub admission or infers a token balance. Redemption
is explicit; importing the module and the default CLI operation only read.
"""
import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import selectors
import sqlite3
import subprocess
import time
import uuid

READ = 'account/rateLimits/read'
CONSUME = 'account/rateLimitResetCredit/consume'
OUTCOMES = {'reset', 'alreadyRedeemed', 'nothingToReset', 'noCredit'}


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
        if method not in {'initialize', READ, CONSUME}:
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


def redeem(rpc, path, account_id, credit_id, *, now=None, idempotency_key=None):
    """One durable attempt per account+credit; at most two sends of the same key.

    A pending/unknown attempt blocks other credits for this account. Provider
    success is persisted before the mandatory post-read so a read failure can
    never cause a new redemption. Local receipts are not Hub acceptance events.
    """
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # SQLite commits must precede network sends. A separate process lock keeps
    # serialization intact across those commits, including post-read retries.
    fd = os.open(str(path) + '.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise CreditError('account_attempt_busy') from None
        return _redeem_locked(rpc, path, account_id, credit_id, now=now, idempotency_key=idempotency_key)
    finally:
        os.close(fd)


def _redeem_locked(rpc, path, account_id, credit_id, *, now=None, idempotency_key=None):
    account_id, credit_id = _identifier(account_id), _identifier(credit_id)
    if idempotency_key is not None:
        _identifier(idempotency_key)
    fixed_now = now
    def stamp():
        return int(time.time() if fixed_now is None else fixed_now)
    with contextlib.closing(sqlite3.connect(path, timeout=1)) as db:
        os.chmod(path, 0o600)
        db.execute('PRAGMA synchronous=FULL')
        db.execute('CREATE TABLE IF NOT EXISTS attempts (account TEXT, credit TEXT, key TEXT UNIQUE, sends INTEGER, receipt TEXT, PRIMARY KEY(account, credit))')
        db.commit()
        # Hold the write lock through both reads and mutation to serialize all
        # adapter processes sharing this managed account's canonical journal.
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT key,sends,receipt FROM attempts WHERE account=? AND credit=?', (account_id, credit_id)).fetchone()
        before = snapshot(rpc.call(READ), stamp())
        now = stamp()
        if before['accountId'] != account_id:
            raise CreditError('account_mismatch')
        if row:
            key, sends, encoded = row
            if idempotency_key is not None and key != idempotency_key:
                raise CreditError('idempotency_key_conflict')
            receipt = json.loads(encoded)
            if receipt['outcome'] in OUTCOMES:
                if receipt['outcome'] not in {'reset', 'alreadyRedeemed'} or receipt.get('creditDecrementVerified'):
                    return receipt
            elif sends >= 2:
                raise CreditError('unknown_outcome_retry_limit')
        else:
            for (encoded,) in db.execute('SELECT receipt FROM attempts WHERE account=?', (account_id,)):
                old = json.loads(encoded)
                if old['outcome'] in {'pending', 'unknown'} or (old['outcome'] in {'reset', 'alreadyRedeemed'} and not old.get('creditDecrementVerified')):
                    raise CreditError('account_attempt_unresolved')
            _eligible(before, credit_id, now)
            key, sends = idempotency_key or str(uuid.uuid4()), 0
            receipt = {'eventId': 'codex-earned-reset:' + hashlib.sha256((account_id + ':' + credit_id).encode()).hexdigest(),
                       'accountId': account_id, 'creditId': credit_id, 'idempotencyKey': key,
                       'before': before, 'startedAt': now, 'outcome': 'pending', 'after': None}

        def save():
            db.execute('INSERT OR REPLACE INTO attempts VALUES (?,?,?,?,?)',
                       (account_id, credit_id, key, sends, json.dumps(receipt, sort_keys=True)))
            db.commit()

        if receipt['outcome'] not in {'reset', 'alreadyRedeemed'}:
            # Retrying an uncertain logical attempt must not run availability
            # checks again: it may have succeeded and removed the credit.
            sends += 1
            receipt['outcome'] = 'pending'
            save()  # Durable before sending; never issue a different key.
            db.execute('BEGIN IMMEDIATE')
            try:
                result = rpc.call(CONSUME, {'idempotencyKey': key, 'creditId': credit_id})
                outcome = result.get('outcome')
                if outcome not in OUTCOMES:
                    raise CreditError('provider_unknown_outcome')
                receipt['outcome'] = outcome
            except Exception:
                receipt['outcome'] = 'unknown'
                receipt['updatedAt'] = stamp()
                save()
                raise CreditError('provider_outcome_unknown') from None
            receipt['updatedAt'] = stamp()
            save()
            db.execute('BEGIN IMMEDIATE')
        if receipt['outcome'] in {'reset', 'alreadyRedeemed'}:
            try:
                after = snapshot(rpc.call(READ), stamp())
                if after['accountId'] != account_id:
                    raise CreditError('account_mismatch')
            except Exception:
                save()
                raise CreditError('reset_received_refresh_required') from None
            receipt['after'] = after
            selected = _credit(after, credit_id)
            receipt['creditDecrementVerified'] = (after['availableCount'] < receipt['before']['availableCount']
                                                   and (selected is None or selected['status'] == 'redeemed'))
        save()
        return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--consume', action='store_true')
    parser.add_argument('--account-id')
    parser.add_argument('--credit-id')
    args = parser.parse_args()
    try:
        with AppServer() as rpc:
            if args.consume:
                if not args.account_id or not args.credit_id:
                    parser.error('--consume requires --account-id and --credit-id')
                result = redeem(rpc, journal_path(), args.account_id, args.credit_id)
            else:
                result = snapshot(rpc.call(READ))
        print(json.dumps(result, sort_keys=True))
        if args.consume and not result.get('creditDecrementVerified'):
            return 2
        return 0
    except (CreditError, sqlite3.Error, OSError, ValueError, TypeError):
        # Never print raw provider/OS errors: they can contain private paths or
        # provider payloads. A durable receipt remains available for inspection.
        print(json.dumps({'error': 'earned_reset_failed_closed'}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
