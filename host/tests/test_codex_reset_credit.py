"""Offline safety tests; fixtures do not establish provider consumption."""
import copy
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

from agent_hub_host.codex_reset_credit import CONSUME, READ, CreditError, redeem, snapshot

NOW = 1800000000


def view(count=2, used=90, credit=True):
    return {'accountId': 'account-a', 'ordinaryUsageAllowed': False,
            'rateLimitResetCredits': {'availableCount': count, 'credits': [
                {'id': 'credit-a', 'expiresAt': NOW + 3600, 'status': 'available',
                 'resetType': 'codexRateLimits'}] if credit else []},
            'rateLimits': {'primary': {'usedPercent': used, 'resetsAt': NOW + 86400,
                                       'windowDurationMins': 10080}}}


class Rpc:
    def __init__(self, before=None, outcome='reset', after=None):
        self.before = before or view()
        self.after = after or view(count=1, used=0, credit=False)
        self.outcome = outcome
        self.calls = []
        self.reads = 0
        self.on_consume = None

    def call(self, method, params=None):
        self.calls.append((method, params))
        if method == READ:
            self.reads += 1
            result = self.before if self.reads == 1 else self.after
            if isinstance(result, Exception):
                raise result
            return copy.deepcopy(result)
        if self.on_consume:
            self.on_consume(params)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return {'outcome': self.outcome}


class ResetCreditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'attempts.sqlite3'

    def run_attempt(self, rpc, credit='credit-a'):
        return redeem(rpc, self.path, 'account-a', credit, now=NOW)

    def receipt(self):
        with sqlite3.connect(self.path) as db:
            return json.loads(db.execute('SELECT receipt FROM attempts').fetchone()[0])

    def test_success_requires_real_read_after_and_decrement(self):
        rpc = Rpc()
        result = self.run_attempt(rpc)
        self.assertEqual([m for m, _ in rpc.calls], [READ, CONSUME, READ])
        self.assertTrue(result['creditDecrementVerified'])
        self.assertEqual(result['before']['availableCount'], 2)
        self.assertEqual(result['after']['windows'][0]['usedPercent'], 0)
        self.assertEqual(result, self.receipt())
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)

    def test_journal_is_committed_before_mutation(self):
        rpc = Rpc()
        def inspect(params):
            saved = self.receipt()
            self.assertEqual(saved['outcome'], 'pending')
            self.assertEqual(saved['idempotencyKey'], params['idempotencyKey'])
        rpc.on_consume = inspect
        self.run_attempt(rpc)

    def test_terminal_success_never_redeems_twice(self):
        saved = self.run_attempt(Rpc())
        retry = Rpc(before=view(count=1, credit=False))
        self.assertEqual(self.run_attempt(retry), saved)
        self.assertEqual([m for m, _ in retry.calls], [READ])

    def test_uncertain_retry_uses_same_key_when_credit_has_disappeared(self):
        rpc = Rpc(outcome=TimeoutError('private error'))
        with self.assertRaisesRegex(CreditError, 'provider_outcome_unknown'):
            self.run_attempt(rpc)
        key = self.receipt()['idempotencyKey']
        retry = Rpc(before=view(count=1, credit=False), outcome='alreadyRedeemed')
        result = self.run_attempt(retry)
        self.assertEqual(result['outcome'], 'alreadyRedeemed')
        self.assertEqual(retry.calls[1][1]['idempotencyKey'], key)
        self.assertTrue(result['creditDecrementVerified'])

    def test_uncertain_attempt_blocks_other_credit_and_bounds_retries(self):
        for _ in range(2):
            with self.assertRaises(CreditError):
                self.run_attempt(Rpc(outcome=TimeoutError()))
        blocked = Rpc()
        with self.assertRaisesRegex(CreditError, 'retry_limit'):
            self.run_attempt(blocked)
        self.assertFalse(any(m == CONSUME for m, _ in blocked.calls))
        with self.assertRaisesRegex(CreditError, 'account_attempt_unresolved'):
            self.run_attempt(Rpc(), credit='credit-b')

    def test_success_with_failed_refresh_only_retries_read(self):
        with self.assertRaisesRegex(CreditError, 'refresh_required'):
            self.run_attempt(Rpc(after=TimeoutError()))
        self.assertEqual(self.receipt()['outcome'], 'reset')
        retry = Rpc(before=view(count=1, credit=False))
        self.assertTrue(self.run_attempt(retry)['creditDecrementVerified'])
        self.assertEqual([m for m, _ in retry.calls], [READ, READ])

    def test_no_op_outcomes_preserved_without_repeat_mutation(self):
        for outcome in ('nothingToReset', 'noCredit'):
            with self.subTest(outcome=outcome):
                path = Path(self.temp.name) / outcome
                first = redeem(Rpc(outcome=outcome), path, 'account-a', 'credit-a', now=NOW)
                retry = Rpc()
                self.assertEqual(redeem(retry, path, 'account-a', 'credit-a', now=NOW), first)
                self.assertEqual(first['outcome'], outcome)
                self.assertEqual([m for m, _ in retry.calls], [READ])

    def test_expired_unknown_and_nonexpiring_credits_refused(self):
        for expiry in (NOW, NOW - 1, None, NOW + 86401):
            with self.subTest(expiry=expiry):
                raw = view()
                raw['rateLimitResetCredits']['credits'][0]['expiresAt'] = expiry
                rpc = Rpc(before=raw)
                with self.assertRaises(CreditError):
                    self.run_attempt(rpc)
                self.assertFalse(any(m == CONSUME for m, _ in rpc.calls))

    def test_healthy_quota_not_reset(self):
        for used in (0, 1, 84):
            with self.subTest(used=used):
                rpc = Rpc(before=view(used=used))
                with self.assertRaisesRegex(CreditError, 'unnecessary'):
                    self.run_attempt(rpc)
                self.assertFalse(any(m == CONSUME for m, _ in rpc.calls))

    def test_account_mismatch_never_mutates(self):
        rpc = Rpc()
        with self.assertRaisesRegex(CreditError, 'account_mismatch'):
            redeem(rpc, self.path, 'other-account', 'credit-a', now=NOW)
        self.assertFalse(any(m == CONSUME for m, _ in rpc.calls))

    def test_unknown_provider_outcome_remains_uncertain(self):
        with self.assertRaisesRegex(CreditError, 'provider_outcome_unknown'):
            self.run_attempt(Rpc(outcome='surprise'))
        self.assertEqual(self.receipt()['outcome'], 'unknown')

    def test_quota_drop_alone_does_not_verify_coupon_consumption(self):
        result = self.run_attempt(Rpc(after=view(count=2, used=0)))
        self.assertFalse(result['creditDecrementVerified'])

    def test_count_drop_with_credit_still_available_does_not_verify(self):
        result = self.run_attempt(Rpc(after=view(count=1, used=0)))
        self.assertFalse(result['creditDecrementVerified'])

    def test_inconclusive_success_blocks_other_credit_then_only_refreshes(self):
        self.run_attempt(Rpc(after=view(count=2, used=0)))
        with self.assertRaisesRegex(CreditError, 'account_attempt_unresolved'):
            self.run_attempt(Rpc(), credit='credit-b')
        retry = Rpc(before=view(count=1, credit=False))
        self.assertTrue(self.run_attempt(retry)['creditDecrementVerified'])
        self.assertEqual([m for m, _ in retry.calls], [READ, READ])

    def test_unknown_permission_never_mutates(self):
        raw = view()
        del raw['ordinaryUsageAllowed']
        rpc = Rpc(before=raw)
        with self.assertRaisesRegex(CreditError, 'permission_unknown'):
            self.run_attempt(rpc)
        self.assertFalse(any(m == CONSUME for m, _ in rpc.calls))

    def test_unknown_credit_type_or_redeeming_status_never_mutates(self):
        for field, value in [('resetType', 'unknown'), ('status', 'redeeming'), ('status', 'redeemed')]:
            with self.subTest(field=field, value=value):
                raw = view()
                raw['rateLimitResetCredits']['credits'][0][field] = value
                rpc = Rpc(before=raw)
                with self.assertRaises(CreditError):
                    self.run_attempt(rpc)
                self.assertFalse(any(m == CONSUME for m, _ in rpc.calls))

    def test_concurrent_adapter_is_refused_before_provider_call(self):
        with open(str(self.path) + '.lock', 'w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            rpc = Rpc()
            with self.assertRaisesRegex(CreditError, 'busy'):
                self.run_attempt(rpc)
            self.assertEqual(rpc.calls, [])

    def test_missing_credit_details_fail_closed(self):
        for credits in (None, 'bad'):
            raw = view()
            raw['rateLimitResetCredits']['credits'] = credits
            with self.assertRaisesRegex(CreditError, 'identity_unavailable'):
                snapshot(raw)

    def test_snapshot_never_fabricates_token_balance_or_copies_secrets(self):
        raw = view()
        raw['token'] = 'secret'
        raw['rateLimitUpsell'] = {'private': 'secret'}
        clean = json.dumps(snapshot(raw))
        self.assertNotIn('secret', clean)
        self.assertNotIn('remaining_tokens', clean)


if __name__ == '__main__':
    unittest.main()
