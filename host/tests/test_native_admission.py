import copy
import datetime as dt
import unittest
from unittest.mock import patch
from agent_hub_host import native_admission as admission
from agent_hub_host.model_contract import configured_selection, policy_models


class NativeAdmissionTest(unittest.TestCase):
    def test_codex_configured_zero_reserve_admits_nine_and_blocks_zero(self):
        for account in ('shared-codex', 'codex-orin'):
            payload = dict(account=account, engine='codex', mode='interactive')
            for remaining, allowed in ((9, True), (0, False)):
                with self.subTest(account=account, remaining=remaining):
                    quota = self.quota('codex')
                    quota['codex']['windows']['7d']['percent_remaining'] = remaining
                    result = admission.probe(payload, 'codex', account, 'codex', lambda _: quota)
                    self.assertEqual(result['launch_allowed'], allowed)

    def test_codex_reserve_configuration_is_binding_and_invalid_fails_closed(self):
        payload = dict(account='shared-codex', engine='codex', mode='interactive')
        quota = self.quota('codex')
        quota['codex']['windows']['7d']['percent_remaining'] = 9
        with patch.object(admission, 'reserve_percent', return_value=10):
            self.assertFalse(admission.probe(payload, 'codex', 'shared-codex', 'codex', lambda _: quota)['launch_allowed'])
        with patch.object(admission, 'reserve_percent', side_effect=ValueError('invalid')):
            self.assertFalse(admission.probe(payload, 'codex', 'shared-codex', 'codex', lambda _: quota)['launch_allowed'])

    def quota(self, service):
        return {service: {'status': 'ok', 'details': {'limit_reached': False}, 'windows': {
            '5h': {'percent_remaining': 80, 'reset_at': (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=2)).isoformat()},
            '7d': {'percent_remaining': 90, 'reset_at': (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=2)).isoformat()},
        }}}

    def test_claude_and_native_zcode_match_policy_and_fresh_admission(self):
        for service, engine, model_id in [('claude', 'claude', 'opus'), ('zai', 'zcodex', 'glm-flash')]:
            with self.subTest(service=service):
                model = next(m for m in policy_models() if m['id'] == model_id)
                payload = dict(account='shared-' + service, engine=engine, profile=None, mode='headless', selection=model)
                result = admission.probe(payload, service, payload['account'], engine, lambda _: self.quota(service))
                self.assertTrue(result['launch_allowed'])
                self.assertTrue(result['model_supported'])
                self.assertEqual(result['model'], model['model'])
                self.assertEqual(configured_selection(model, engine, service), model)

    def test_unknown_exhausted_stale_malformed_and_mismatched_identity_fail_closed(self):
        payload = dict(account='shared-zai', engine='zcodex', profile=None, mode='headless')
        for mutate in [
            lambda q: q['zai'].update(status='error'),
            lambda q: q['zai']['details'].update(limit_reached=True),
            lambda q: q['zai']['details'].update(stale=True),
            lambda q: q['zai']['windows']['5h'].update(percent_remaining=float('nan')),
            lambda q: q['zai']['windows']['5h'].update(percent_remaining=0),
            lambda q: q['zai']['windows']['5h'].update(reset_at='2000-01-01T00:00:00Z'),
            lambda q: q['zai']['windows']['5h'].update(reset_at=None),
        ]:
            quota = self.quota('zai')
            mutate(quota)
            self.assertFalse(admission.probe(payload, 'zai', 'shared-zai', 'zcodex', lambda _: quota)['launch_allowed'])
        for field, value in [('account', 'other'), ('profile', 'other'), ('engine', 'codex')]:
            changed = copy.deepcopy(payload)
            changed[field] = value
            self.assertFalse(admission.probe(changed, 'zai', 'shared-zai', 'zcodex', lambda _: self.quota('zai'))['launch_allowed'])

    def test_grok_remains_disabled_and_zcode_alias_cannot_select_codex_model(self):
        payload = dict(account='shared-grok', engine='grok', profile=None, mode='interactive')
        def must_not_probe(_):
            self.fail('disabled Grok execution must not probe or execute')
        self.assertFalse(admission.probe(payload, 'grok', 'shared-grok', 'grok', must_not_probe)['launch_allowed'])
        sol = next(m for m in policy_models() if m['id'] == 'sol')
        with self.assertRaises(ValueError):
            configured_selection(sol, 'zcodex', 'zai')

    def test_unused_claude_short_window_requires_a_valid_timed_weekly_window(self):
        for service, account, engine in [('claude', 'shared-claude', 'claude'), ('zai', 'shared-zai', 'zcodex')]:
            with self.subTest(service=service):
                payload = dict(account=account, engine=engine, mode='interactive')
                quota = self.quota(service)
                quota[service]['windows']['5h'].update(percent_remaining=100, reset_at=None)
                self.assertTrue(admission.probe(payload, service, account, engine, lambda _: quota)['launch_allowed'])
                quota[service]['windows']['5h']['percent_remaining'] = 99
                self.assertFalse(admission.probe(payload, service, account, engine, lambda _: quota)['launch_allowed'])
                quota[service]['windows']['5h']['percent_remaining'] = 100
                quota[service]['windows']['7d']['reset_at'] = None
                self.assertFalse(admission.probe(payload, service, account, engine, lambda _: quota)['launch_allowed'])
