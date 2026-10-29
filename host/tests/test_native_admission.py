"""Native-account admission asks Hub (intake 5814e109 I4); no host quota probe.

Hub evaluates quota/reserve windows centrally (test/quota-probe-fallback and
test/credentials cover that logic); the host validates identity and model only.
"""
import copy
import unittest
from agent_hub_host import native_admission as admission
from agent_hub_host.model_contract import configured_selection, policy_models


def hub(verdict):
    calls = []

    def request(method, path, payload=None):
        calls.append((method, path, payload))
        if isinstance(verdict, Exception):
            raise verdict
        return dict(verdict, account_label=payload['label'])
    request.calls = calls
    return request


ALLOW = {'launch_allowed': True, 'profile_supported': True}


class NativeAdmissionTest(unittest.TestCase):
    def test_hub_verdict_decides_for_every_native_account(self):
        for service, account, engine in [('codex', 'shared-codex', 'codex'), ('codex', 'codex-orin', 'codex'),
                                         ('claude', 'shared-claude', 'claude'), ('zai', 'shared-zai', 'zcodex')]:
            with self.subTest(account=account):
                payload = dict(account=account, engine=engine, profile=None, mode='interactive')
                req = hub(ALLOW)
                self.assertTrue(admission.probe(payload, service, account, engine, req)['launch_allowed'])
                self.assertEqual(req.calls, [('POST', '/api/engines/admission', {'label': account, 'native': True})])
                refused = admission.probe(payload, service, account, engine, hub({'launch_allowed': False, 'reason': 'quota_exhausted'}))
                self.assertFalse(refused['launch_allowed'])
                self.assertEqual(refused['reason'], 'quota_exhausted')

    def test_unknown_quota_admits_with_warning_and_unreachable_hub_fails_closed(self):
        payload = dict(account='shared-claude', engine='claude', profile=None, mode='headless')
        warned = admission.probe(payload, 'claude', 'shared-claude', 'claude', hub(dict(ALLOW, quota_warning='quota_probe_unavailable')))
        self.assertTrue(warned['launch_allowed'])
        self.assertEqual(warned['quota_warning'], 'quota_probe_unavailable')
        self.assertFalse(admission.probe(payload, 'claude', 'shared-claude', 'claude', hub(OSError('down')))['launch_allowed'])

    def test_model_selection_is_validated_locally(self):
        for service, engine, model_id in [('claude', 'claude', 'opus'), ('zai', 'zcodex', 'glm-flash')]:
            with self.subTest(service=service):
                model = next(m for m in policy_models() if m['id'] == model_id)
                payload = dict(account='shared-' + service, engine=engine, profile=None, mode='headless', selection=model)
                result = admission.probe(payload, service, payload['account'], engine, hub(ALLOW))
                self.assertTrue(result['launch_allowed'])
                self.assertTrue(result['model_supported'])
                self.assertEqual(result['model'], model['model'])
                self.assertEqual(configured_selection(model, engine, service), model)

    def test_mismatched_identity_fails_closed_without_asking_hub(self):
        payload = dict(account='shared-zai', engine='zcodex', profile=None, mode='headless')
        for field, value in [('account', 'other'), ('profile', 'other'), ('engine', 'codex'), ('mode', 'bogus')]:
            changed = copy.deepcopy(payload)
            changed[field] = value
            req = hub(ALLOW)
            self.assertFalse(admission.probe(changed, 'zai', 'shared-zai', 'zcodex', req)['launch_allowed'])
            self.assertEqual(req.calls, [])

    def test_grok_remains_disabled_and_zcode_alias_cannot_select_codex_model(self):
        payload = dict(account='shared-grok', engine='grok', profile=None, mode='interactive')

        def must_not_probe(*_):
            self.fail('disabled Grok execution must not probe or execute')
        self.assertFalse(admission.probe(payload, 'grok', 'shared-grok', 'grok', must_not_probe)['launch_allowed'])
        sol = next(m for m in policy_models() if m['id'] == 'sol')
        with self.assertRaises(ValueError):
            configured_selection(sol, 'zcodex', 'zai')


if __name__ == '__main__':
    unittest.main()
