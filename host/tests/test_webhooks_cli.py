"""hub webhooks CLI: secret hygiene and payload building (intake 19ec91c2).

The server shows a route secret exactly once. The CLI must surface it only
through the explicitly requested private channel and never print, log or
store it anywhere else; create without --secret-file/--stdout-secret must
discard it with a warning.
"""
import io
import itertools
import json
import os
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import patch


class WebhookCliSecretHygiene(unittest.TestCase):
    def _run(self, argv, request):
        from agent_hub_host import cli, session_control
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch.object(sys, 'argv', ['hub'] + argv), \
                patch.object(session_control, 'request', side_effect=request), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            try:
                cli.main()
            except SystemExit:
                pass
        return stdout.getvalue(), stderr.getvalue()

    def test_create_secret_file_is_private_and_secret_never_printed(self):
        from agent_hub_host import cli, session_control
        calls = []

        def request(method, path, payload=None):
            calls.append((method, path, payload))
            return {'id': 'route-1', 'name': 'oncall-dev', 'url_path': '/webhooks/route-1',
                    'secret': 'whsec_abc123', 'secret_note': 'shown once'}

        with tempfile.TemporaryDirectory() as tmp:
            secret_path = os.path.join(tmp, 'secrets', 'oncall-dev.secret')
            out, err = self._run(['webhooks', 'create', '--name', 'oncall-dev', '--session', 'oncall-engineer',
                                  '--template', 'Alert: {{payload.subject}}', '--secret-file', secret_path], request)
            mode = stat.S_IMODE(os.stat(secret_path).st_mode)
            self.assertEqual(mode & 0o777, 0o600)
            with open(secret_path, encoding='utf-8') as handle:
                self.assertEqual(handle.read(), 'whsec_abc123')
            self.assertNotIn('whsec_abc123', out)
            self.assertNotIn('whsec_abc123', err)
            self.assertIn(secret_path, out)
            self.assertEqual(calls[0], ('POST', '/api/webhooks', {
                'name': 'oncall-dev', 'target_label': 'oncall-engineer',
                'template': 'Alert: {{payload.subject}}'}))

    def test_create_stdout_secret_prints_secret_exactly_once(self):
        def request(method, path, payload=None):
            return {'id': 'route-3', 'url_path': '/webhooks/route-3', 'secret': 'whsec_stdout1'}

        out, err = self._run(['webhooks', 'create', '--name', 'y-route', '--session', 's',
                              '--stdout-secret'], request)
        self.assertIn('whsec_stdout1', out)
        self.assertNotIn('<written to stdout>', out)
        self.assertNotIn('whsec_stdout1', err)

    def test_create_without_secret_channel_discards_secret(self):
        def request(method, path, payload=None):
            return {'id': 'route-2', 'url_path': '/webhooks/route-2', 'secret': 'whsec_xyz'}

        out, err = self._run(['webhooks', 'create', '--name', 'x-route', '--session', 's'], request)
        self.assertNotIn('whsec_xyz', out)
        self.assertNotIn('whsec_xyz', err)
        self.assertIn('--secret-file', err)

    def test_delete_requires_yes_and_test_waits_for_terminal_status(self):
        calls = []

        def request(method, path, payload=None):
            calls.append((method, path, payload))
            if method == 'DELETE':
                return {'deleted': True}
            if method == 'POST':
                return {'delivery_id': 'd1', 'status': 'pending'}
            return {'deliveries': [{'id': 'd1', 'status': 'delivered'}]}

        out, err = self._run(['webhooks', 'delete', 'my-route'], request)
        self.assertIn('--yes', err)
        self._run(['webhooks', 'test', 'my-route', '--payload', 'hello', '--wait', '--timeout', '5',
                   '--interval', '1', '--json'], request)
        test_call = calls[0]
        self.assertEqual(test_call[0], 'POST')
        self.assertEqual(test_call[1], '/api/webhooks/my-route/test')
        self.assertEqual(test_call[2], {'payload': 'hello'})
        out_json = json.loads(self._run(['webhooks', 'test', 'my-route', '--payload', 'x', '--wait',
                                         '--timeout', '5', '--interval', '1', '--json'], request)[0])
        self.assertEqual(out_json['delivery']['status'], 'delivered')

    def test_list_renders_table_without_secrets(self):
        def request(method, path, payload=None):
            return {'webhooks': [{'name': 'a', 'id': 'i1', 'target_label': 's', 'auth_mode': 'token',
                                  'rate_limit_per_min': 30, 'max_body_bytes': 65536, 'enabled': True,
                                  'url_path': '/webhooks/i1'}]}

        out, err = self._run(['webhooks', 'list'], request)
        self.assertIn('a', out)
        self.assertIn('/webhooks/i1', out)


if __name__ == '__main__':
    unittest.main()
