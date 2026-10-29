"""Offline safety tests; fixtures do not establish provider consumption."""
import copy
import json
import unittest

from agent_hub_host import codex_reset_credit as native
from agent_hub_host.codex_reset_credit import READ, AppServer, CreditError, snapshot

NOW = 1800000000


def view(count=2, used=90, credit=True):
    return {'accountId': 'account-a', 'ordinaryUsageAllowed': False,
            'rateLimitResetCredits': {'availableCount': count, 'credits': [
                {'id': 'credit-a', 'expiresAt': NOW + 3600, 'status': 'available',
                 'resetType': 'codexRateLimits'}] if credit else []},
            'rateLimits': {'primary': {'usedPercent': used, 'resetsAt': NOW + 86400,
                                       'windowDurationMins': 10080}}}


class Rpc:
    """Read-only provider stand-in; asserts no mutation call is ever made."""

    def __init__(self, before=None):
        self.before = before or view()
        self.calls = []

    def call(self, method, params=None):
        self.calls.append((method, params))
        if method == READ:
            return copy.deepcopy(self.before)
        raise AssertionError('read-only adapter must never call ' + method)


class ResetCreditTests(unittest.TestCase):
    def test_adapter_only_allows_initialize_and_read(self):
        # The method allowlist refuses everything but initialize/READ, before
        # any transport is touched (no provider process needed for the guard).
        for method in ('account/rateLimitResetCredit/consume', 'redeem', 'other'):
            with self.assertRaisesRegex(CreditError, 'unsupported_method'):
                AppServer.call(object.__new__(AppServer), method, {})

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

    def test_unknown_permission_refused(self):
        raw = view()
        del raw['ordinaryUsageAllowed']
        with self.assertRaisesRegex(CreditError, 'permission_unknown'):
            native._eligible(snapshot(raw), 'credit-a', NOW)

    def test_expired_unknown_and_nonexpiring_credits_refused(self):
        for expiry in (NOW, NOW - 1, None, NOW + 86401):
            with self.subTest(expiry=expiry):
                raw = view()
                raw['rateLimitResetCredits']['credits'][0]['expiresAt'] = expiry
                with self.assertRaises(CreditError):
                    native._eligible(snapshot(raw), 'credit-a', NOW)

    def test_healthy_quota_not_reset(self):
        for used in (0, 1, 84):
            with self.subTest(used=used):
                raw = view(used=used)
                with self.assertRaisesRegex(CreditError, 'unnecessary'):
                    native._eligible(snapshot(raw), 'credit-a', NOW)

    def test_missing_absent_or_wrong_type_credit_refused(self):
        raw = view(credit=False)
        with self.assertRaisesRegex(CreditError, 'not_available'):
            native._eligible(snapshot(raw), 'credit-a', NOW)
        for field, value in [('resetType', 'unknown'), ('status', 'redeeming'), ('status', 'redeemed')]:
            with self.subTest(field=field, value=value):
                raw = view()
                raw['rateLimitResetCredits']['credits'][0][field] = value
                with self.assertRaises(CreditError):
                    native._eligible(snapshot(raw), 'credit-a', NOW)

    def test_read_path_returns_clean_snapshot_and_only_reads(self):
        rpc = Rpc()
        snap = snapshot(rpc.call(READ))
        self.assertEqual(snap['accountId'], 'account-a')
        self.assertEqual(snap['availableCount'], 2)
        self.assertEqual(snap['credits'][0]['id'], 'credit-a')
        self.assertEqual(rpc.calls, [(READ, None)])


class NoRedemptionGuardTests(unittest.TestCase):
    """Alexey t1741u: track expiry and ask for approval, never redeem."""

    FORBIDDEN = (
        'rate-limit-reset-credits/consume', 'rate-limit-reset-credits/redeem',
        'account/rateLimitResetCredit/consume', 'customer-package-reset/use')

    def test_modules_carry_no_redemption_path(self):
        import re
        from pathlib import Path
        root = Path(native.__file__).resolve().parent
        for name in ('codex_reset_credit.py', 'reset_credit_workflow.py'):
            source = (root / name).read_text()
            for pattern in self.FORBIDDEN:
                self.assertIsNone(re.search(pattern, source, re.I), (name, pattern))
        self.assertFalse(hasattr(native, 'redeem'))
        self.assertFalse(hasattr(native, 'CONSUME'))


if __name__ == '__main__':
    unittest.main()
