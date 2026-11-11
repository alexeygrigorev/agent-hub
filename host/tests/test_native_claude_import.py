import argparse
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from agent_hub_host import native_claude_import as n
from agent_hub_host import provisioning as p


class NativeImportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ['TMPDIR'])
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': str(self.home), 'HUB_URL': 'https://hub.test'})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.path = self.home / '.credentials.json'
        self.oauth = {'accessToken': 'synthetic-access', 'refreshToken': 'synthetic-refresh', 'expiresAt': int(time.time() * 1000) + 3600000}
        self.write({'claudeAiOauth': self.oauth, 'mcpOAuth': {'private': 'must-not-import'}})
        self.args = argparse.Namespace(provider='claude', label='hub-claude')

    def write(self, value):
        self.path.write_text(json.dumps(value))
        self.path.chmod(0o600)

    def test_collect_preserves_native_source_and_excludes_mcp(self):
        before = self.path.read_bytes()
        result = n.collect()
        self.assertEqual(json.loads(result[n.AUTH_PATH]), {'claudeAiOauth': self.oauth})
        self.assertEqual(self.path.read_bytes(), before)
        self.assertNotIn('must-not-import', repr(result))

    def test_unsafe_sources_and_invalid_credentials_are_sanitized(self):
        good = self.path.read_bytes()
        self.path.chmod(0o644)
        with self.assertRaisesRegex(ValueError, 'unsafe or expired'):
            n.collect()
        self.path.chmod(0o600)
        self.path.rename(self.home / 'other')
        self.path.symlink_to(self.home / 'other')
        with self.assertRaises(ValueError):
            n.collect()
        self.path.unlink()
        os.link(self.home / 'other', self.path)
        with self.assertRaises(ValueError):
            n.collect()
        self.path.unlink()
        for value in ({'claudeAiOauth': dict(self.oauth, expiresAt=1)}, {'claudeAiOauth': {}}, ['synthetic-secret']):
            self.write(value)
            with self.assertRaises(ValueError) as exc:
                n.collect()
            self.assertNotIn('synthetic', str(exc.exception))
        self.path.write_bytes(good)

    def test_managed_home_is_refused(self):
        with patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': str(self.home / 'managed-engines' / 'home/.claude')}):
            with self.assertRaisesRegex(ValueError, 'Native Claude home required'):
                n.collect()

    def run_command(self, *, incoming=5, receipt=5, ack=5, central=5, fail_upload=False, fail_sync=False, fail_verify=False):
        calls = []
        cfg = {'base_url': 'https://hub.test', 'machine_id': 'test-host'}
        before = {'credentials': [{'label': 'hub-claude', 'generation': 4, 'hosts': ['test-host']}]}
        after = {'sync_status': [{'machine_id': 'test-host', 'accounts': [{'label': 'hub-claude', 'credential_generation': central, 'acked_generation': ack}]}]}
        def operator(method, path, payload=None):
            calls.append((method, path, payload))
            if method == 'GET':
                if fail_verify and len(calls) > 1:
                    raise OSError('synthetic-private-provider-output')
                return before if len(calls) == 1 else after
            if fail_upload:
                raise ValueError('generation_conflict')
            self.assertEqual(payload['expected_generation'], 4)
            self.assertNotIn('must-not-import', repr(payload))
            return {'credential_generation': receipt}
        def host(method, path, payload=None):
            calls.append((method, path, payload))
            return {'engines': [{'label': 'hub-claude', 'credential_generation': incoming}], 'logins': [{'id': 'do-not-complete'}]}
        def reconcile(request):
            if fail_sync:
                raise OSError('synthetic-private-provider-output')
            response = request('GET', '/api/engines/sync')
            self.assertEqual(response['logins'], [])
            return {'status': 'synced'}
        with patch.object(n.config, 'load_config', return_value=cfg), patch.object(n.config, 'load_host_config', return_value=cfg), patch.object(n.bus, '_do_request', side_effect=operator), patch.object(n.provisioning, 'host_request', side_effect=host), patch.object(n.provisioning, 'sync', side_effect=reconcile) as sync:
            try:
                result = n.command(self.args)
            finally:
                self.calls = calls
                self.sync_called = sync.called
        return result

    def test_upload_precedes_sync_and_ack_is_verified(self):
        result = self.run_command()
        self.assertEqual(result['status'], 'imported')
        self.assertEqual(result['acked_generation'], 5)
        self.assertNotIn('synthetic', repr(result))
        self.assertEqual([call[0] for call in self.calls], ['GET', 'POST', 'GET', 'GET'])

    def test_stale_upload_never_syncs(self):
        with self.assertRaisesRegex(ValueError, 'generation_conflict'):
            self.run_command(fail_upload=True)
        self.assertFalse(self.sync_called)

    def test_bad_receipt_never_syncs(self):
        with self.assertRaisesRegex(ValueError, 'no sync attempted'):
            self.run_command(receipt=4)
        self.assertFalse(self.sync_called)

    def test_expired_central_generation_is_not_materialized(self):
        with self.assertRaisesRegex(ValueError, 'saved at generation 5'):
            self.run_command(incoming=4)

    def test_post_upload_failures_preserve_saved_receipt_without_provider_output(self):
        for flags in ({'fail_sync': True}, {'fail_verify': True}):
            with self.assertRaisesRegex(ValueError, 'saved at generation 5') as exc:
                self.run_command(**flags)
            self.assertNotIn('synthetic-private', str(exc.exception))

    def test_missing_or_stale_ack_is_not_success(self):
        for ack, central in ((None, 5), (4, 5), (5, 6)):
            with self.assertRaisesRegex(ValueError, 'acknowledgement is pending'):
                self.run_command(ack=ack, central=central)

    def test_https_and_host_origin_required_before_collect(self):
        for base in ('http://localhost', 'https://other.test'):
            with patch.dict(os.environ, {'HUB_URL': base}), patch.object(n.config, 'load_config', return_value={}), patch.object(n.config, 'load_host_config', return_value={'base_url': 'https://hub.test', 'machine_id': 'test-host'}), patch.object(n, 'collect') as collect:
                with self.assertRaisesRegex(ValueError, 'HTTPS'):
                    n.command(self.args)
                collect.assert_not_called()

    def test_cli_dispatch(self):
        parser = argparse.ArgumentParser()
        p.add_commands(parser.add_subparsers(dest='cmd'))
        args = parser.parse_args(['engines', 'import-native', '--provider', 'claude', '--label', 'hub-claude'])
        with patch.object(n, 'command', return_value={'status': 'imported'}) as command:
            self.assertEqual(p.command(args), {'status': 'imported'})
            command.assert_called_once_with(args)


if __name__ == '__main__':
    unittest.main()
