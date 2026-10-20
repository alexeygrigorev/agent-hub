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
        self.assertNotIn('999', text)

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
