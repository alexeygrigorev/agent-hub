import datetime
import json
from pathlib import Path
import tempfile
import unittest

from agent_hub_host import quota_server, quota as quota_cli


class ServerQuotaTests(unittest.TestCase):
    def test_collects_account_observations_without_machine_coverage(self):
        settings = {'accounts': [{'provider': 'codex', 'account': 'agent-main'},
                                 {'provider': 'codex-orin', 'account': 'orin'}]}
        future = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=1)).isoformat()
        route = {'status': 'ok', 'error': None, 'details': {'limit_reached': False, 'secret': 'must disappear'},
                 'windows': {'7d': {'percent_remaining': 71, 'reset_at': future}}}
        payload = quota_server.collect_accounts(settings, runner=lambda *a, **k: {'codex': route})
        self.assertNotIn('host_id', payload)
        self.assertEqual(payload['accounts'][0]['quota']['windows']['7d']['percent_remaining'], 71)
        self.assertNotIn('secret', str(payload))
        self.assertEqual(payload['accounts'][1]['quota']['status'], 'error')
        self.assertEqual(payload['accounts'][1]['quota']['windows'], {})

    def test_duplicate_account_and_unknown_provider_refused_before_execution(self):
        for accounts in [[{'provider': 'codex', 'account': 'main'}] * 2,
                         [{'provider': 'unknown', 'account': 'main'}]]:
            with self.assertRaises(ValueError):
                quota_server.collect_accounts({'accounts': accounts}, runner=lambda *a, **k: self.fail('must not execute'))

    def test_cli_coverage_counts_accounts_not_machines(self):
        text = quota_cli.render_text({'pools': [], 'account_coverage': {'total': 2, 'fresh': ['main'], 'unavailable': ['go']},
                                     'coverage': {'machines_total': 999, 'missing': ['offline']}})
        self.assertIn('1 fresh, 0 stale, 1 unavailable of 2', text)
        self.assertIn('coverage: 0 fresh, 0 stale, 1 missing of 999 machines', text)

    def test_https_origin_validation_precedes_token_read(self):
        for origin in ['https://evil.invalid/path', 'http://localhost', 'https://user:pass@evil.invalid']:
            with self.assertRaises(ValueError):
                quota_server.admin_opener(origin, '/nonexistent')

    def test_managed_account_discovery_needs_no_per_account_collector_config(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / 'state.json'
            state.write_text(json.dumps([{'label': 'second-agent', 'provider': 'codex', 'revision': 'opaque'}]))
            def runner(argv, **kwargs):
                if isinstance(argv, str):
                    return {}
                self.assertEqual(argv, ['hub', 'engines', 'quota'])
                return {'accounts': [{'account': 'second-agent', 'provider': 'codex', 'quota': {
                    'status': 'ok', 'details': {'limit_reached': False, 'api_key': 'private'},
                    'windows': {'7d': {'percent_remaining': 88, 'reset_at': None}}}}]}
            settings = {'accounts': [{'provider': 'go', 'account': 'go'}], 'managed_state_file': str(state)}
            payload = quota_server.collect_accounts(settings, runner=runner)
            self.assertEqual(payload['accounts'][1]['account'], 'second-agent')
            self.assertEqual(payload['accounts'][1]['quota']['windows']['7d']['percent_remaining'], 88)
            self.assertNotIn('private', str(payload))
            def broken(argv, **kwargs):
                if isinstance(argv, list):
                    raise TimeoutError()
                return {}
            payload = quota_server.collect_accounts(settings, runner=broken)
            self.assertEqual(payload['accounts'][1]['quota']['status'], 'error')

    def test_managed_claude_account_discovery_and_sanitization(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / 'state.json'
            state.write_text(json.dumps([{'label': 'my-claude', 'provider': 'claude', 'revision': 'rev-opaque'}]))
            def runner(argv, **kwargs):
                if isinstance(argv, str):
                    return {}
                self.assertEqual(argv, ['hub', 'engines', 'quota'])
                return {'accounts': [{'account': 'my-claude', 'provider': 'claude', 'quota': {
                    'status': 'ok', 'details': {'limit_reached': False, 'token': 'must-not-leak'},
                    'windows': {'5h': {'percent_remaining': 75.0, 'reset_at': '2030-01-01T05:00:00Z'},
                                '7d': {'percent_remaining': 85.0, 'reset_at': '2030-01-07T00:00:00Z'}}}}]}
            settings = {'accounts': [{'provider': 'codex', 'account': 'main'}], 'managed_state_file': str(state)}
            payload = quota_server.collect_accounts(settings, runner=runner)
            claude_row = payload['accounts'][1]
            self.assertEqual(claude_row['account'], 'my-claude')
            self.assertEqual(claude_row['provider'], 'claude')
            self.assertEqual(claude_row['quota']['status'], 'ok')
            self.assertEqual(claude_row['quota']['windows']['5h']['percent_remaining'], 75.0)
            self.assertEqual(claude_row['quota']['windows']['7d']['percent_remaining'], 85.0)
            self.assertNotIn('must-not-leak', json.dumps(payload))

    def test_managed_pair_replaces_personal_probe_on_same_key(self):
        """Wrong-account-key regression (intake f6709905): a managed 401 for
        (codex, shared-codex) must replace the fresh personal server-local
        probe for the same fleet account, so `hub quota` no longer routes
        /goal starts at a dead account. Managed (codex, codex-orin) likewise
        replaces personal (codex-orin, codex-orin) in place: the server
        dedupes snapshots on account alone (duplicate_account 400), so the
        payload must carry unique accounts."""
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / 'state.json'
            state.write_text(json.dumps([
                {'label': 'shared-codex', 'provider': 'codex', 'revision': 'rev-a'},
                {'label': 'codex-orin', 'provider': 'codex', 'revision': 'rev-b'},
            ]))
            future = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=7)).isoformat()
            personal_fresh = {'status': 'ok', 'error': None, 'details': {'limit_reached': False},
                              'windows': {'7d': {'percent_remaining': 68.0, 'reset_at': future}}}
            personal_orin = {'status': 'ok', 'error': None, 'details': {'limit_reached': False},
                             'windows': {'7d': {'percent_remaining': 54.0, 'reset_at': future}}}
            managed_rows = [
                {'account': 'shared-codex', 'provider': 'codex', 'quota': {
                    'status': 'error', 'details': {'limit_reached': False},
                    'windows': {'7d': {'percent_remaining': None, 'reset_at': None}}}},
                {'account': 'codex-orin', 'provider': 'codex', 'quota': {
                    'status': 'ok', 'details': {'limit_reached': False},
                    'windows': {'7d': {'percent_remaining': 54.0, 'reset_at': future}}}},
            ]
            def runner(argv, **kwargs):
                if isinstance(argv, str):
                    return {'codex': personal_fresh, 'codex-orin': personal_orin}
                return {'accounts': managed_rows}
            settings = {'accounts': [{'provider': 'codex', 'account': 'shared-codex'},
                                      {'provider': 'codex-orin', 'account': 'codex-orin'}],
                        'managed_state_file': str(state)}
            payload = quota_server.collect_accounts(settings, runner=runner)
            accounts = [r['account'] for r in payload['accounts']]
            # Server rejects duplicate accounts: payload must stay unique.
            self.assertEqual(len(accounts), len(set(accounts)))
            by_account = {r['account']: r for r in payload['accounts']}
            # Same fleet account: managed 401 replaces the misleading fresh probe.
            self.assertEqual(by_account['shared-codex']['quota']['status'], 'error')
            self.assertEqual(by_account['shared-codex']['provider'], 'codex')
            # codex-orin is one fleet account: managed (codex) replaces the
            # legacy personal (codex-orin) route key in place.
            self.assertEqual(by_account['codex-orin']['quota']['status'], 'ok')
            self.assertEqual(by_account['codex-orin']['provider'], 'codex')
            self.assertEqual(by_account['codex-orin']['quota']['windows']['7d']['percent_remaining'], 54.0)

    def test_managed_duplicate_labels_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / 'state.json'
            state.write_text(json.dumps([
                {'label': 'dup', 'provider': 'codex', 'revision': 'rev-a'},
                {'label': 'dup', 'provider': 'claude', 'revision': 'rev-b'},
            ]))
            settings = {'accounts': [{'provider': 'go', 'account': 'go'}],
                        'managed_state_file': str(state)}
            with self.assertRaises(ValueError):
                quota_server.collect_accounts(settings, runner=lambda *a, **k: {})
