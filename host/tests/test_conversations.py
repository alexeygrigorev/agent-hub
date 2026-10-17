import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_hub_host import cli, conversations


class TestBodies(unittest.TestCase):
    def test_utf8_stdin_preserves_whitespace(self):
        with patch('sys.stdin', io.StringIO('  hello\n世界\n')):
            self.assertEqual(conversations.read_body('-'), '  hello\n世界\n')

    def test_stdin_limits_utf8_bytes_not_characters(self):
        for value in (' ', 'é' * 8193, 'a' * 16385):
            with patch('sys.stdin', io.StringIO(value)), self.assertRaises(ValueError):
                conversations.read_body('-')
        with patch('sys.stdin', io.StringIO('é' * 8192)):
            self.assertEqual(len(conversations.read_body('-').encode()), 16384)

    def test_file_is_bounded_and_strict_utf8(self):
        root = Path(__file__).resolve().parents[2] / '.local' / 'tmp'
        root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as directory:
            path = Path(directory) / 'body'
            path.write_bytes(b'hello\n')
            self.assertEqual(conversations.read_body(path), 'hello\n')
            path.write_bytes(b'x' * 16385)
            with self.assertRaises(ValueError):
                conversations.read_body(path)
            path.write_bytes(b'\xff')
            with self.assertRaises(UnicodeError):
                conversations.read_body(path)


class TestCli(unittest.TestCase):
    def run_cli(self, *args):
        output, error = io.StringIO(), io.StringIO()
        with patch('sys.argv', ['hub', 'conversations', *args]), \
             patch('sys.stdout', output), patch('sys.stderr', error):
            try:
                cli.main()
                code = 0
            except SystemExit as exc:
                code = exc.code
        return code, output.getvalue(), error.getvalue()

    def test_requires_retry_key_and_forbids_from_override(self):
        with patch('agent_hub_host.conversations.request') as request:
            for args in (('create', '--title', 'title'),
                         ('send', 'c1', '--body-file', '-'),
                         ('list', '--from', 'spoof')):
                self.assertEqual(self.run_cli(*args)[0], 2)
            request.assert_not_called()

    def test_errors_never_echo_exception_or_body(self):
        with patch.object(conversations, 'execute', side_effect=RuntimeError('secret-token BODY filename')):
            code, output, error = self.run_cli('list')
        self.assertEqual(code, 1)
        self.assertEqual(output, '')
        self.assertNotIn('secret-token', error)
        self.assertNotIn('BODY', error)
        self.assertNotIn('filename', error)


class TestConversationContract(unittest.TestCase):
    run_cli = TestCli.run_cli

    def call(self, *args, response=None):
        with patch('agent_hub_host.conversations.request', return_value=response or {'ok': True}) as transport:
            result = self.run_cli(*args)
        self.assertEqual(result[0], 0, result[2])
        args = transport.call_args.args
        return args + (None,) if len(args) == 2 else args, result[1]

    def test_create_get_and_registered_participants(self):
        call, _ = self.call('create', '--title', 'Coordination', '--kind', 'direct',
                            '--member', 'registered-agent', '--key', 'create-1')
        self.assertEqual(call, ('POST', '/api/conversations', {
            'kind': 'direct', 'title': 'Coordination', 'members': ['registered-agent'],
            'idempotency_key': 'create-1'}))
        self.assertEqual(self.call('get', 'c1')[0], ('GET', '/api/conversations/c1', None))
        self.assertEqual(self.call('participants')[0], ('GET', '/api/conversations/participants', None))

    def test_list_encodes_cursor_without_injecting_query(self):
        call, _ = self.call('list', '--cursor', 'date|id&state=all', '--state', 'archived', '--limit', '2')
        self.assertEqual(call, ('GET', '/api/conversations?state=archived&limit=2&cursor=date%7Cid%26state%3Dall', None))

    def test_members_uses_owner_routes_without_author_override(self):
        self.assertEqual(self.call('members', 'c1')[0], ('GET', '/api/conversations/c1/members', None))
        self.assertEqual(self.call('members', 'c1', '--include-removed')[0],
                         ('GET', '/api/conversations/c1/members?include_removed=true', None))
        self.assertEqual(self.call('members', 'c1', '--add', 'registered-agent')[0],
                         ('POST', '/api/conversations/c1/members', {'actor': 'registered-agent', 'role': 'member'}))
        self.assertEqual(self.call('members', 'c1', '--remove', 'registered-agent')[0],
                         ('POST', '/api/conversations/c1/members/remove', {'actor': 'registered-agent'}))

    def test_history_is_read_only_and_reconnect_cursor_is_explicit(self):
        call, _ = self.call('history', 'c1', '--after-seq', '7', '--limit', '3')
        self.assertEqual(call, ('GET', '/api/conversations/c1/messages?limit=3&after_seq=7', None))
        self.assertEqual(self.call('history', 'c1', '--before-seq', '4')[0],
                         ('GET', '/api/conversations/c1/messages?limit=50&before_seq=4', None))
        self.assertEqual(self.call('read', 'c1', '--seq', '7')[0],
                         ('POST', '/api/conversations/c1/read', {'seq': 7}))
        self.assertEqual(self.run_cli('read', 'c1')[0], 2)

    def test_invalid_paths_keys_bounds_never_reach_transport(self):
        for args in (('get', '..'), ('get', '../participants'), ('get', 'c1?admin=true'),
                     ('list', '--limit', '0'), ('history', 'c1', '--after-seq', '-1'),
                     ('history', 'c1', '--before-seq', '0'),
                     ('read', 'c1', '--seq', '9007199254740992'),
                     ('create', '--title', 't', '--key', 'invalid key')):
            with self.subTest(args=args), patch('agent_hub_host.conversations.request') as request:
                self.assertNotEqual(self.run_cli(*args)[0], 0)
                request.assert_not_called()

    def test_uncertain_send_retry_retains_key_body_and_reply(self):
        # Fake durable server commits before losing the first response. A fresh CLI
        # invocation must reuse the same key rather than create a second message.
        stored, calls = {}, []
        def server(method, path, payload):
            calls.append((method, path, dict(payload)))
            key = payload['idempotency_key']
            if key not in stored:
                stored[key] = dict(payload)
                raise ConnectionError('synthetic secret server response')
            self.assertEqual(stored[key], payload)
            return {'id': 'm1', 'seq': 1, 'replay': True}
        with patch('agent_hub_host.conversations.request', side_effect=server):
            results = []
            for _ in range(2):
                with patch('sys.stdin', io.StringIO('exact body\n')):
                    results.append(self.run_cli('send', 'c1', '--key', 'stable-1', '--body-file', '-',
                                                '--kind', 'reply', '--reply-to', 'parent'))
        self.assertEqual([r[0] for r in results], [1, 0])
        self.assertNotIn('synthetic secret', results[0][2])
        self.assertEqual(len(stored), 1)
        self.assertEqual(calls[0], calls[1])
        self.assertEqual(calls[0][2], {'body': 'exact body\n', 'kind': 'reply',
                                     'idempotency_key': 'stable-1', 'reply_to': 'parent'})

    def test_removed_membership_fails_without_cache_or_read_ack(self):
        with patch('agent_hub_host.conversations.request', side_effect=[{'messages': [{'seq': 1}]},
                   ValueError('HTTP request failed with status 403')]) as transport:
            self.assertEqual(self.run_cli('history', 'c1', '--after-seq', '0')[0], 0)
            code, output, _ = self.run_cli('history', 'c1', '--after-seq', '1')
        self.assertEqual(code, 1)
        self.assertEqual(output, '')
        self.assertEqual([call.args[0] for call in transport.call_args_list], ['GET', 'GET'])

    def test_bridge_cli_is_an_explicit_nonzero_hold(self):
        with patch('agent_hub_host.conversations.request') as transport:
            code, output, error = self.run_cli('bridge', '--cycles', '2')
        self.assertEqual(code, 1)
        self.assertEqual(output, '')
        self.assertIn('held for M3b', error)
        transport.assert_not_called()


class TestAuthenticatedHttp(unittest.TestCase):
    """Real stdlib HTTP transport against a loopback-only synthetic server."""
    def setUp(self):
        import http.server
        import threading
        self.calls, self.responses = [], []
        owner = self
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.handle_request()
            def do_POST(self):
                self.handle_request()
            def handle_request(self):
                import json
                size = int(self.headers.get('Content-Length', '0'))
                raw = self.rfile.read(size)
                owner.calls.append((self.command, self.path, dict(self.headers),
                                    json.loads(raw) if raw else None))
                status, headers, body = owner.responses.pop(0)
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                for key, value in headers.items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *args):
                pass
        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.thread.join, 2)
        self.addCleanup(self.server.shutdown)
        self.env = patch.dict(os.environ, {'HUB_URL': f'http://127.0.0.1:{self.server.server_port}'}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.cfg = patch('agent_hub_host.config.load_config', return_value={})
        self.cfg.start()
        self.addCleanup(self.cfg.stop)

    def test_refresh_retries_identical_payload_and_key(self):
        self.responses = [(401, {}, b'{"error":"expired"}'), (200, {}, b'{"id":"m1","replay":true}')]
        payload = {'body': 'private synthetic body', 'idempotency_key': 'retry-1'}
        with patch('agent_hub_host.auth.get_access_token', side_effect=['synthetic-old', 'synthetic-new']), \
             patch('agent_hub_host.auth.refresh_token') as refresh:
            self.assertEqual(conversations.request('POST', '/api/conversations/c1/messages', payload)['id'], 'm1')
        refresh.assert_called_once()
        self.assertEqual([c[3] for c in self.calls], [payload, payload])
        self.assertEqual([c[2]['Authorization'] for c in self.calls],
                         ['Bearer synthetic-old', 'Bearer synthetic-new'])

    def test_redirect_never_forwards_credentials_or_body(self):
        self.responses = [(302, {'Location': '/credential-trap'}, b'{}'), (200, {}, b'{}')]
        with patch.dict(os.environ, {'HUB_TOKEN': 'synthetic-agent'}):
            with self.assertRaises(ValueError):
                conversations.request('POST', '/api/conversations/c1/messages', {'body': 'synthetic'})
        self.assertEqual(len(self.calls), 1)
        self.assertNotEqual(self.calls[0][1], '/credential-trap')

    def test_agent_token_is_not_refreshed_and_server_error_is_not_echoed(self):
        self.responses = [(403, {}, b'{"error":"synthetic-secret-body"}')]
        with patch.dict(os.environ, {'HUB_TOKEN': 'synthetic-agent'}), \
             patch('agent_hub_host.auth.refresh_token') as refresh:
            code, output, error = TestCli.run_cli(self, 'list')
        self.assertEqual(code, 1)
        self.assertEqual(output, '')
        self.assertNotIn('synthetic-secret-body', error)
        self.assertNotIn('synthetic-agent', error)
        refresh.assert_not_called()

    def test_insecure_remote_origin_refused_before_credential_lookup(self):
        with patch.dict(os.environ, {'HUB_URL': 'http://example.invalid'}), \
             patch('agent_hub_host.auth.get_access_token') as token:
            with self.assertRaises(ValueError):
                conversations.request('GET', '/api/conversations')
        token.assert_not_called()
        self.assertEqual(self.calls, [])

    def test_max_unicode_body_uses_utf8_json_under_http_bound(self):
        import json
        self.responses = [(201, {}, b'{"id":"m1"}')]
        body = 'é' * 8192
        with patch.dict(os.environ, {'HUB_TOKEN': 'synthetic-agent'}):
            conversations.request('POST', '/api/conversations/c1/messages',
                                  {'body': body, 'idempotency_key': 'unicode-1'})
        self.assertEqual(self.calls[0][3]['body'], body)
        self.assertLess(int(self.calls[0][2]['Content-Length']), 32768)

    def test_escaped_request_bound_is_checked_before_authentication(self):
        with patch('agent_hub_host.auth.get_access_token') as token:
            with self.assertRaises(ValueError):
                conversations.request('POST', '/api/conversations/c1/messages', {'body': '\x01' * 16384})
        token.assert_not_called()
        self.assertEqual(self.calls, [])
