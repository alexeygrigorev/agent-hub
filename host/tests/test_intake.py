"""Synthetic public CLI/HTTP coverage; never loads operator configuration."""
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from agent_hub_host import cli, intake

ROOT = Path(__file__).resolve().parents[2]


class TestIntakeCli(unittest.TestCase):
    def run_cli(self, *args):
        output, error = io.StringIO(), io.StringIO()
        with patch('sys.argv', ['hub', *args]), patch('sys.stdout', output), patch('sys.stderr', error):
            try:
                cli.main()
                code = 0
            except SystemExit as exc:
                code = exc.code
        return code, output.getvalue(), error.getvalue()

    def test_list_all_pins_snapshot_authorization_and_filters(self):
        first = {'items': [{'id': 'a'}], 'next': 'a', 'snapshot_version': 7,
                 'authorization_valid_until': 12345, 'total': 2, 'effective_limit': 1}
        last = dict(first, items=[{'id': 'b'}], next=None)
        with patch.object(intake, 'request', side_effect=[first, last]) as transport:
            code, out, error = self.run_cli('intake', 'list', '--all', '--limit', '1', '--project', 'demo')
        self.assertEqual(code, 0, error)
        result = json.loads(out)
        self.assertEqual([t['id'] for t in result['items']], ['a', 'b'])
        self.assertEqual(result['total'], 2)
        self.assertIsNone(result['next'])
        from urllib.parse import urlparse, parse_qs
        query = parse_qs(urlparse(transport.call_args_list[1].args[1]).query)
        self.assertEqual(query, {'all': ['true'], 'project': ['demo'], 'limit': ['1'],
                                'after': ['a'], 'revision': ['7'], 'authorization_valid_until': ['12345']})

    def test_forgotten_both_spellings(self):
        for spelling in ('intake', 'intakes'):
            with patch.object(intake, 'request', return_value={'items': [], 'forgotten_count': 0}) as transport:
                code, out, error = self.run_cli(spelling, 'forgotten', '--project', 'demo')
                self.assertEqual(code, 0, error)
                self.assertEqual(json.loads(out)['forgotten_count'], 0)
                transport.assert_called_once_with('GET', '/api/intake/forgotten?project=demo')

    def test_archive_and_audit_commands(self):
        with patch.object(intake, 'request', return_value={'items': []}) as transport:
            self.assertEqual(self.run_cli('intake', 'list', '--archived')[0], 0)
            self.assertIn('archived=true', transport.call_args.args[1])
            self.assertEqual(self.run_cli('intake', 'audit', 'abcdef01')[0], 0)
            self.assertIn('/tasks/abcdef01/audit?', transport.call_args.args[1])
            self.assertEqual(self.run_cli('intake', 'audit')[0], 0)
            self.assertIn('/intake/audit?', transport.call_args.args[1])

    def test_list_all_rejects_inconsistent_or_incomplete_snapshots(self):
        first = {'items': [{'id': 'a'}], 'next': 'a', 'snapshot_version': 7,
                 'authorization_valid_until': None, 'total': 2, 'effective_limit': 1}
        for last in [dict(first, items=[{'id': 'b'}], next=None, snapshot_version=8),
                     dict(first, items=[], next=None),
                     dict(first, next=None),
                     dict(first, items=[{'id': 'b'}], next=None, authorization_valid_until=99),
                     intake.StaleSnapshot('Snapshot changed')]:
            with self.subTest(last=last), patch.object(intake, 'request', side_effect=[first, last]):
                code, out, error = self.run_cli('intake', 'list', '--all')
                self.assertEqual(code, 1)
                self.assertEqual(out, '')
        with patch.object(intake, 'request') as transport:
            self.assertEqual(self.run_cli('intake', 'list', '--all', '--after', 'a')[0], 1)
            transport.assert_not_called()

    def test_list_single_page_passes_integer_limit(self):
        with patch.object(intake, 'request', return_value={'effective_limit': 100}) as transport:
            code, out, error = self.run_cli('intake', 'list', '--limit', '100')
        self.assertEqual(code, 0, error)
        self.assertIn('limit=100', transport.call_args.args[1])
        self.assertEqual(json.loads(out)['effective_limit'], 100)

    def test_public_module_help_and_existing_commands(self):
        fixture = dict(os.environ, PYTHONPATH=str(ROOT/'host'), PYTHONDONTWRITEBYTECODE='1',
                       HUB_CONFIG_FILE=str(ROOT/'.local/intake/absent-fixture.json'))
        for arguments in [[], ['intake'], ['intake', 'create'], ['handoff']]:
            result = subprocess.run([sys.executable, '-m', 'agent_hub_host', *arguments, '--help'],
                                    env=fixture, capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('usage:', result.stdout)
        result = subprocess.run([sys.executable, '-m', 'agent_hub_host.cli', '--help'],
                                env=fixture, capture_output=True, text=True, timeout=20)
        for command in ('intake', 'handoff', 'submit', 'task-result', 'conversations'):
            self.assertIn(command, result.stdout)

    def test_create_preserves_verbatim_utf8_and_unknown_provenance(self):
        with patch.object(intake, 'read_file', return_value='  synthetic\n世界\n'), patch.object(intake, 'request', return_value={'ok': True}) as transport:
            code, out, error = self.run_cli('intake', 'create', '--project', 'demo', '--title', 'Synthetic', '--text-file', '-', '--key', 'capture')
        self.assertEqual(code, 0, error)
        payload = transport.call_args.args[2]
        self.assertEqual(payload['text'], '  synthetic\n世界\n')
        self.assertEqual(payload['source'], {'type': 'unknown', 'reference': None, 'timestamp': None})
        self.assertEqual(json.loads(out), {'ok': True})

    def test_terminal_and_unicode_controls_are_escaped_without_rewriting_json_data(self):
        text = 'Synthetic\x1b[2J\x9b31m\u202e\n世界'
        with patch.object(intake, 'execute', return_value={'original': text}):
            code, output, error = self.run_cli('handoff')
        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(output)['original'], text)
        for control in ('\x1b', '\x9b', '\u202e'):
            self.assertNotIn(control, output)

    def test_voice_pending_capture_never_transcribes(self):
        with patch.object(intake, 'request', return_value={}) as transport:
            code, _, error = self.run_cli('intake', 'create', '--project', 'demo', '--title', 'Voice', '--voice-pending', '--source-type', 'voice', '--source-reference', 'fixture:media', '--key', 'voice')
        self.assertEqual(code, 0, error)
        self.assertIsNone(transport.call_args.args[2]['text'])
        self.assertEqual(transport.call_args.args[2]['transcription'], 'pending')

    def test_update_event_and_continue_require_cas_and_stable_key(self):
        with patch.object(intake, 'request', return_value={}) as transport:
            result = self.run_cli('intake', 'update', 'task-1', '--version', '3', '--key', 'retry', '--status', 'blocked', '--next-step', 'Review')
            self.assertEqual(result[0], 0, result[2])
            self.assertEqual(transport.call_args.args[2], {'key': 'retry', 'expected_version': 3, 'status': 'blocked', 'next_step': 'Review'})
            self.assertEqual(self.run_cli('intake', 'continue', 'task-1', '--version', '3', '--key', 'start')[0], 0)
            self.assertEqual(transport.call_args.args, ('POST', '/api/intake/tasks/task-1/continue', {'key': 'start', 'expected_version': 3}))
        for args in [('intake', 'append', 'task-1', '--version', '1')]:
            with patch.object(intake, 'request') as transport:
                self.assertEqual(self.run_cli(*args)[0], 2)
                transport.assert_not_called()

    def test_append_preserves_event_text_and_optional_source(self):
        for event in ({'kind': 'note', 'text': 'И в этом случае тоже'},
                      {'kind': 'note', 'text': 'И в этом случае тоже',
                       'source': {'type': 'chat', 'reference': 't753u', 'timestamp': '2026-10-20T13:26:00+02:00'}}):
            with self.subTest(event=event), patch('sys.stdin', io.TextIOWrapper(io.BytesIO(json.dumps(event).encode()), encoding='utf-8')), \
                 patch.object(intake, 'request', return_value={}) as transport:
                code, _, error = self.run_cli('intake', 'append', 'task-1', '--version', '1',
                                              '--key', 'append-note', '--event-file', '-')
                self.assertEqual(code, 0, error)
                self.assertEqual(transport.call_args.args, ('POST', '/api/intake/tasks/task-1/events',
                                 {'key': 'append-note', 'expected_version': 1, 'event': event}))

    def test_update_auto_version_retries_only_cas_with_one_generated_key(self):
        calls = []
        def send(method, path, payload=None):
            calls.append((method, path, payload))
            if method == 'GET':
                return {'task': {'control': {'version': len(calls)}}}
            if len(calls) == 2:
                raise intake.IntakeHTTPError(409, 'version_conflict')
            return {'ok': True}
        with patch.object(intake, 'request', side_effect=send):
            code, output, error = self.run_cli('intake', 'update', 'task-1', '--owner', 'demo')
        self.assertEqual(code, 0, error)
        writes = [c[2] for c in calls if c[0] == 'POST']
        self.assertEqual([p['expected_version'] for p in writes], [1, 3])
        self.assertEqual(writes[0]['key'], writes[1]['key'])
        self.assertTrue(writes[0]['key'].startswith('update-'))

    def test_auto_update_conflicts_are_bounded_and_explicit_versions_never_retry(self):
        for explicit, expected_calls in ((False, 6), (True, 1)):
            def send(method, path, payload=None):
                if method == 'GET':
                    return {'task': {'control': {'version': 1}}}
                raise intake.IntakeHTTPError(409, 'version_conflict')
            arguments = ['intake', 'update', 'task-1', '--status', 'blocked']
            if explicit:
                arguments += ['--version', '1']
            with patch.object(intake, 'request', side_effect=send) as transport:
                self.assertEqual(self.run_cli(*arguments)[0], 1)
            self.assertEqual(transport.call_count, expected_calls)
        with patch.object(intake, 'request', side_effect=[{'task': {'control': {'version': 1}}},
                          intake.IntakeHTTPError(409, 'idempotency_conflict')]) as transport:
            self.assertEqual(self.run_cli('intake', 'update', 'task-1', '--owner', 'demo')[0], 1)
            self.assertEqual(transport.call_count, 2)

    def test_update_uncertain_transport_exposes_safe_recovery_key_without_retry(self):
        with patch.object(intake, 'request', side_effect=[{'task': {'control': {'version': 7}}},
                          urllib.error.URLError('private-error')]) as transport:
            code, output, error = self.run_cli('intake', 'update', 'task-1', '--key', 'recover-fixture', '--owner', 'demo')
        self.assertEqual(code, 1)
        self.assertIn('outcome unknown', error)
        self.assertIn('recover-fixture', error)
        self.assertIn('attempted version: 7', error)
        self.assertNotIn('private-error', error)
        self.assertEqual(transport.call_count, 2)

    def test_update_uncertain_post_responses_retain_recovery_key_and_version(self):
        for kind in ('truncated', 'invalid_json', 'bad_content_type'):
            writes = []
            class Handler(BaseHTTPRequestHandler):
                def log_message(self, *args):
                    pass
                def do_GET(self):
                    body = b'{"task":{"control":{"version":7}}}'
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                def do_POST(self):
                    writes.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                    if kind == 'truncated':
                        self.send_response(200)
                        self.send_header('Content-Type', 'application/json')
                        self.send_header('Content-Length', '100')
                        self.end_headers()
                        self.wfile.write(b'{"task":')
                        self.close_connection = True
                    elif kind == 'invalid_json':
                        raw = b'{"task": corrupt json'
                        self.send_response(200)
                        self.send_header('Content-Type', 'application/json')
                        self.send_header('Content-Length', str(len(raw)))
                        self.end_headers()
                        self.wfile.write(raw)
                    elif kind == 'bad_content_type':
                        raw = b'<html>Gateway Error</html>'
                        self.send_response(200)
                        self.send_header('Content-Type', 'text/html')
                        self.send_header('Content-Length', str(len(raw)))
                        self.end_headers()
                        self.wfile.write(raw)
            server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with patch.object(intake.config, 'load_config', return_value={'base_url': f'http://127.0.0.1:{server.server_port}'}), \
                     patch.dict(os.environ, {'HUB_TOKEN': 'synthetic-fixture', 'HUB_URL': '', 'HUB_BASE_URL': ''}):
                    code, out, err = self.run_cli('intake', 'update', 'fixture-task', '--owner', 'fixture-owner')
            finally:
                server.shutdown()
                server.server_close()
                thread.join()
            with self.subTest(kind=kind):
                self.assertEqual(code, 1)
                self.assertEqual(len(writes), 1, f'{kind} should not auto-retry')
                self.assertTrue(writes[0]['key'].startswith('update-'))
                self.assertIn(writes[0]['key'], err, f'{kind} must preserve generated recovery key')
                self.assertIn('attempted version: 7', err, f'{kind} must preserve attempted version')
                self.assertIn('outcome unknown', err)

    def test_http_error_diagnostics_allow_only_safe_codes(self):
        for raw, expected in ((b'{"error":"version_conflict"}', 'version_conflict'),
                              (b'{"error":"private-token"}', 'request_failed')):
            exc = urllib.error.HTTPError('https://fixture.test', 409, 'private', {}, io.BytesIO(raw))
            result = intake.http_error(exc, 'POST', {'key': 'fixture-retry'})
            self.assertEqual(result.code, expected)
            with patch.object(intake, 'execute', side_effect=result):
                code, _, error = self.run_cli('intake', 'list')
            self.assertEqual(code, 1)
            self.assertIn(expected, error)
            self.assertIn('fixture-retry', error)
            self.assertNotIn('private-token', error)

    def test_http_error_surfaces_server_detail_slugs(self):
        # Auth/grant/operator detail the server actually emits must survive
        # instead of collapsing to generic request_failed (t1035u).
        for raw, expected in ((b'{"error":"unauthorized_or_forbidden"}', 'unauthorized_or_forbidden'),
                              (b'{"error":"operator_required"}', 'operator_required'),
                              (b'{"error":"unknown_field","field":"notes"}', 'unknown_field'),
                              (b'{"error":"not_found"}', 'not_found'),
                              (b'{"error":"invalid_grantee"}', 'invalid_grantee'),
                              (b'{"error":"unknown_code"}', 'unknown_code')):
            exc = urllib.error.HTTPError('https://fixture.test', 403, 'private', {}, io.BytesIO(raw))
            self.assertEqual(intake.http_error(exc, 'POST', {'key': 'k'}).code, expected)
        # Tokens, URLs, controls, uppercase and oversized values still collapse.
        for raw in (b'{"error":"Bearer abc123"}', b'{"error":"https://x.test/y"}',
                    b'{"error":"has space"}', b'{"error":"UPPER"}', b'{"error":"a-b"}',
                    b'{"error":"' + b'x' * 64 + b'"}', b'{"error":123}',
                    b'{"error":null}', b'not json', b'["version_conflict"]'):
            exc = urllib.error.HTTPError('https://fixture.test', 400, 'private', {}, io.BytesIO(raw))
            result = intake.http_error(exc, 'POST', {'key': 'k'})
            self.assertEqual(result.code, 'request_failed', raw)
            self.assertNotIn('Bearer', str(result))

    def test_http_error_passes_capacity_codes_through_on_507(self):
        for raw, expected in ((b'{"error":"record_capacity"}', 'record_capacity'),
                              (b'{"error":"storage_capacity"}', 'storage_capacity'),
                              (b'{"error":"request_capacity"}', 'request_capacity'),
                              (b'{"error":"record_byte_capacity"}', 'record_byte_capacity')):
            exc = urllib.error.HTTPError('https://fixture.test', 507, 'private', {}, io.BytesIO(raw))
            result = intake.http_error(exc, 'POST', {'key': 'fixture-retry'})
            self.assertEqual(result.status, 507)
            self.assertEqual(result.code, expected)
        unknown = urllib.error.HTTPError('https://fixture.test', 507, 'private', {}, io.BytesIO(b'{"error":"unknown_code"}'))
        self.assertEqual(intake.http_error(unknown, 'POST', None).code, 'unknown_code')

    def test_assign_builds_canonical_session_payload(self):
        with patch.object(intake, 'request', return_value={}) as transport:
            code, _, error = self.run_cli('intake', 'assign', 'task-1', '--version', '2', '--key', 'assign',
                                          '--workspace', 'demo', '--engine', 'codex', '--reason', 'Synthetic override for assign test.', '--tier', 'daily',
                                          '--next-step', 'Watch binding')
        self.assertEqual(code, 0, error)
        self.assertEqual(transport.call_args.args[0], 'POST')
        self.assertEqual(transport.call_args.args[1], '/api/intake/tasks/task-1/assign')
        payload = transport.call_args.args[2]
        self.assertEqual(payload['key'], 'assign')
        self.assertEqual(payload['expected_version'], 2)
        self.assertEqual(payload['session'], {'workspace': 'demo', 'engine': 'codex', 'engine_override_reason': 'Synthetic override for assign test.', 'tier': 'daily', 'mode': 'headless'})
        self.assertNotIn('parent_id', payload['session'])
        self.assertEqual(payload['provenance'], {'type': 'unknown', 'reference': None, 'timestamp': None})
        self.assertEqual(payload['next_step'], 'Watch binding')

    def test_assign_with_parent_carries_parent_id_in_session(self):
        with patch.object(intake, 'request', return_value={}) as transport:
            code, _, error = self.run_cli('intake', 'assign', 'task-1', '--version', '2', '--key', 'assign-parent',
                                          '--workspace', 'demo', '--engine', 'codex', '--reason', 'Synthetic override for parent assign.',
                                          '--parent', 'e3f61b6c-9fda-4bcc-bf3d-cf66cb7ef363')
        self.assertEqual(code, 0, error)
        payload = transport.call_args.args[2]
        self.assertEqual(payload['session']['parent_id'], 'e3f61b6c-9fda-4bcc-bf3d-cf66cb7ef363')
        self.assertEqual(payload['session']['workspace'], 'demo')

    def test_assign_engine_without_reason_is_refused(self):
        with patch.object(intake, 'request', return_value={}) as transport:
            code, _, error = self.run_cli('intake', 'assign', 'task-1', '--version', '2', '--key', 'assign-no-reason',
                                          '--workspace', 'demo', '--engine', 'codex')
        self.assertNotEqual(code, 0)
        transport.assert_not_called()

    def test_create_and_update_carry_acceptance_criteria(self):
        with patch.object(intake, 'read_file', return_value='  synthetic\n世界\n'), \
                patch.object(intake, 'request', return_value={}) as transport:
            code, _, error = self.run_cli('intake', 'create', '--project', 'demo', '--title', 'Synthetic',
                                          '--text-file', '-', '--key', 'capture-ac',
                                          '--acceptance-criteria', 'Review posted; findings addressed.')
        self.assertEqual(code, 0, error)
        self.assertEqual(transport.call_args.args[2]['acceptance_criteria'], 'Review posted; findings addressed.')
        with patch.object(intake, 'request', return_value={}) as transport:
            code, _, error = self.run_cli('intake', 'update', 'task-1', '--version', '2', '--key', 'update-ac',
                                          '--acceptance-criteria', 'Done means X.')
        self.assertEqual(code, 0, error)
        self.assertEqual(transport.call_args.args[2]['acceptance_criteria'], 'Done means X.')
        with patch.object(intake, 'request', return_value={}) as transport:
            code, _, error = self.run_cli('intake', 'update', 'task-1', '--version', '2', '--key', 'clear-ac',
                                          '--clear', 'acceptance_criteria')
        self.assertEqual(code, 0, error)
        self.assertIsNone(transport.call_args.args[2]['acceptance_criteria'])

    def test_acceptance_criteria_gate_error_code_survives_transport(self):
        raw = b'{"error":"acceptance_criteria_required"}'
        exc = urllib.error.HTTPError('https://fixture.test', 400, 'private', {}, io.BytesIO(raw))
        result = intake.http_error(exc, 'POST', {'key': 'fixture-retry'})
        self.assertEqual(result.status, 400)
        self.assertEqual(result.code, 'acceptance_criteria_required')

    def test_create_with_acceptance_criteria_file_reads_bounded_file_t1035u(self):
        # t1035u: intake create --acceptance-criteria-file never reached the
        # server (read_file took 1 arg, call site passed 2 -> TypeError hidden
        # behind the generic CLI message). File and stdin forms must work.
        with tempfile.TemporaryDirectory(dir=ROOT/'.local/tmp') as directory:
            body = Path(directory)/'body.txt'
            body.write_text('hello body', encoding='utf-8')
            criteria = Path(directory)/'ac.txt'
            criteria.write_text('Do X then Y.', encoding='utf-8')
            with patch.object(intake, 'request', return_value={}) as transport:
                code, _, error = self.run_cli('intake', 'create', '--project', 'demo', '--title', 'T',
                                              '--text-file', str(body), '--key', 'k-ac-file',
                                              '--acceptance-criteria-file', str(criteria))
            self.assertEqual(code, 0, error)
            self.assertEqual(transport.call_args.args[2]['acceptance_criteria'], 'Do X then Y.')
            with patch.object(intake, 'request', return_value={}) as transport, \
                 patch('sys.stdin', io.TextIOWrapper(io.BytesIO('Do Z.'.encode()), encoding='utf-8')):
                code, _, error = self.run_cli('intake', 'create', '--project', 'demo', '--title', 'T',
                                              '--text-file', str(body), '--key', 'k-ac-stdin',
                                              '--acceptance-criteria-file', '-')
            self.assertEqual(code, 0, error)
            self.assertEqual(transport.call_args.args[2]['acceptance_criteria'], 'Do Z.')
        with tempfile.TemporaryDirectory(dir=ROOT/'.local/tmp') as directory:
            oversized = Path(directory)/'big.txt'
            oversized.write_bytes(b'x' * 8193)
            body = Path(directory)/'body.txt'
            body.write_text('b', encoding='utf-8')
            with self.assertRaises(ValueError):
                intake.read_file(oversized, 8192)

    def test_grantees_builds_operator_query_url(self):
        with patch.object(intake, 'request', return_value={'items': []}) as transport:
            code, out, error = self.run_cli('intake', 'grantees', '--actor', 'demo:assistant')
            self.assertEqual(code, 0, error)
            self.assertEqual(transport.call_args.args, ('GET', '/api/intake/grantees?actor=demo%3Aassistant&limit=25'))
            self.assertEqual(json.loads(out), {'items': []})
        with patch.object(intake, 'request', return_value={'items': []}) as transport:
            code, out, error = self.run_cli('intake', 'grantees', '--actor', 'demo:assistant', '--family', 'fam-1',
                                            '--limit', '10', '--after', 'cred-1', '--revision', '2')
            self.assertEqual(code, 0, error)
            self.assertEqual(transport.call_args.args, ('GET', '/api/intake/grantees?actor=demo%3Aassistant&limit=10&family_id=fam-1&after=cred-1&revision=2'))
        with patch.object(intake, 'request') as transport:
            code, _, _ = self.run_cli('intake', 'grantees')
            self.assertEqual(code, 2)
            transport.assert_not_called()

    def test_uncertainty_can_be_explicitly_cleared_with_cas(self):
        with patch.object(intake, 'request', return_value={}) as transport:
            code, _, error = self.run_cli('intake', 'update', 'task-1', '--version', '3', '--key', 'clear', '--clear', 'uncertainty')
        self.assertEqual(code, 0, error)
        self.assertIsNone(transport.call_args.args[2]['uncertainty'])

    def test_context_history_links_rules_grants_and_revoke_use_same_api(self):
        for command in ('show', 'context', 'history', 'links'):
            with self.subTest(command=command), patch.object(intake, 'request', return_value={}) as transport:
                self.assertEqual(self.run_cli('intake', command, 'task-1')[0], 0)
                self.assertIn('/tasks/task-1', transport.call_args.args[1])
        for command in ('rules', 'grants'):
            with patch.object(intake, 'request', return_value={}) as transport:
                self.assertEqual(self.run_cli('intake', command)[0], 0)
                self.assertEqual(transport.call_args.args, ('GET', '/api/intake/' + command, None))
        with patch.object(intake, 'json_file', return_value={'key': 'revoke', 'source': {}}), patch.object(intake, 'request', return_value={}) as transport:
            self.assertEqual(self.run_cli('intake', 'revoke', 'g1', '--payload-file', '-')[0], 0)
            self.assertEqual(transport.call_args.args[1], '/api/intake/grants/g1/revoke')

    def test_invalid_id_and_exception_never_echo_private_values(self):
        for identifier in ('..', '../rules', 'task?admin=true', 'task/other'):
            with patch.object(intake, 'request') as transport:
                self.assertEqual(self.run_cli('intake', 'show', identifier)[0], 1)
                transport.assert_not_called()
        with patch.object(intake, 'execute', side_effect=ValueError('synthetic-private-token text filename')):
            code, out, error = self.run_cli('intake', 'list')
        self.assertEqual(code, 1)
        self.assertEqual(out, '')
        self.assertNotIn('synthetic-private', error)
        self.assertNotIn('filename', error)

    def test_file_stdin_bytes_bounded_and_utf8_strict(self):
        stream = io.TextIOWrapper(io.BytesIO('  世界\n'.encode()), encoding='utf-8')
        with patch('sys.stdin', stream):
            self.assertEqual(intake.read_file('-'), '  世界\n')
        with tempfile.TemporaryDirectory(dir=ROOT/'.local/tmp') as directory:
            path = Path(directory)/'fixture'
            path.write_bytes(b'x'*(intake.MAX_REQUEST+1))
            with self.assertRaises(ValueError):
                intake.read_file(path)
            path.write_bytes(b'\xff')
            with self.assertRaises(UnicodeError):
                intake.read_file(path)

    def test_create_inline_file_and_stdin_match_mcp_payload(self):
        text, criteria = '  Verbatim 世界\n', 'Review 世界\n'
        with tempfile.TemporaryDirectory(dir=ROOT/'.local/tmp') as directory:
            body, ac = Path(directory)/'body', Path(directory)/'criteria'
            body.write_text(text, encoding='utf-8')
            ac.write_text(criteria, encoding='utf-8')
            for text_args, ac_args, stdin in [
                    (['--text', text], ['--acceptance-criteria', criteria], ''),
                    (['--text-file', str(body)], ['--acceptance-criteria-file', str(ac)], ''),
                    (['--text-file', '-'], ['--acceptance-criteria-file', str(ac)], text),
                    (['--text-file', str(body)], ['--acceptance-criteria-file', '-'], criteria)]:
                with self.subTest(text_args=text_args), patch.object(intake, 'request', return_value={}) as send, \
                     patch('sys.stdin', io.TextIOWrapper(io.BytesIO(stdin.encode()), encoding='utf-8')):
                    code, _, error = self.run_cli('intake', 'create', '--project', 'demo', '--title', 'Title',
                                                  '--key', 'gha:run-123', '--source-type', 'chat',
                                                  '--source-reference', 'fixture:chat', '--source-time', '2026-10-24T00:00:00Z',
                                                  *text_args, *ac_args)
                self.assertEqual(code, 0, error)
                self.assertEqual(send.call_args.args[2], {
                    'key': 'gha:run-123', 'project': 'demo', 'title': 'Title', 'text': text,
                    'acceptance_criteria': criteria, 'transcription': 'not_applicable',
                    'source': {'type': 'chat', 'reference': 'fixture:chat', 'timestamp': '2026-10-24T00:00:00Z'}})

    def test_create_input_diagnostics_are_specific_without_private_values(self):
        common = ['intake', 'create', '--project', 'demo', '--title', 'Title']
        cases = [(['--text', 'body', '--key', 'gha/run 123'], 'Invalid retry key'),
                 (['--text-file', '/absent/private-token', '--key', 'good'], '--text-file: file not found'),
                 (['--text', 'body', '--key', 'good', '--acceptance-criteria-file', '/absent/private-token'], '--acceptance-criteria-file: file not found'),
                 (['--text', '界' * 2731, '--key', 'good'], '8192 UTF-8 bytes'),
                 (['--text-file', '-', '--acceptance-criteria-file', '-', '--key', 'good'], 'Cannot read both')]
        for args, expected in cases:
            with self.subTest(expected=expected), patch.object(intake, 'request') as send:
                code, output, error = self.run_cli(*common, *args)
            self.assertEqual(code, 1)
            self.assertEqual(output, '')
            self.assertIn(expected, error)
            self.assertNotIn('private-token', error)
            self.assertNotIn('Intake operation failed', error)
            send.assert_not_called()
        with patch('pathlib.Path.open', side_effect=PermissionError(13, 'private-token')):
            code, _, error = self.run_cli(*common, '--text-file', 'private-token', '--key', 'good')
        self.assertEqual(code, 1)
        self.assertIn('permission denied', error)
        self.assertNotIn('private-token', error)

    def test_transport_and_response_failures_are_specific_and_safe(self):
        cases = [(TimeoutError('private-token'), 'timed out'),
                 (urllib.error.URLError(TimeoutError('private-token')), 'timed out'),
                 (urllib.error.URLError('https://private-token'), 'transport failed'),
                 (RuntimeError('Expected JSON response'), 'Expected JSON response'),
                 (json.JSONDecodeError('private-token', 'private-token', 0), 'invalid JSON')]
        for exc, expected in cases:
            with self.subTest(expected=expected), \
                 patch.object(intake.config, 'load_config', return_value={'base_url': 'https://fixture.test'}), \
                 patch.dict(os.environ, {'HUB_TOKEN': 'private-token', 'HUB_URL': '', 'HUB_BASE_URL': ''}), \
                 patch.object(intake.api, 'safe_http_json', side_effect=exc):
                code, output, error = self.run_cli('intake', 'create', '--project', 'demo', '--title', 'Title',
                                                  '--text', 'body', '--key', 'good')
            self.assertEqual(code, 1)
            self.assertEqual(output, '')
            self.assertIn(expected, error)
            self.assertIn('outcome unknown', error)
            self.assertNotIn('private-token', error)

    def test_http_field_limits_and_invalid_event_kinds_are_actionable(self):
        for body, expected in [({'error': 'invalid_field', 'field': 'text', 'max_bytes': 8192}, 'field: text; max 8192 UTF-8 bytes'),
                               ({'error': 'invalid_event_kind', 'message': 'private-token'}, 'valid kinds: ' + ', '.join(intake.EVENT_KINDS))]:
            exc = urllib.error.HTTPError('https://fixture.test', 400, 'private-token', {}, io.BytesIO(json.dumps(body).encode()))
            with patch.object(intake, 'request', side_effect=intake.http_error(exc, 'POST', {'key': 'good'})), \
                 patch.object(intake, 'json_file', return_value={'kind': 'update'}):
                code, _, error = self.run_cli('intake', 'append', 'task-1', '--version', '1', '--key', 'good', '--event-file', '-')
            self.assertEqual(code, 1)
            self.assertIn('Intake HTTP 400: ' + body['error'], error)
            self.assertIn(expected, error)
            self.assertNotIn('private-token', error)
        exc = urllib.error.HTTPError('https://fixture.test', 400, 'private', {}, io.BytesIO(b'{"error":"invalid_field","field":"private-token","message":"private-token"}'))
        self.assertNotIn('private-token', str(intake.http_error(exc, 'POST', None)))

    @staticmethod
    def page(ids, next_id=None, revision=1, total=3):
        return {'snapshot_version': revision, 'items': [{'id': x} for x in ids], 'next': next_id,
                'total': total, 'operator_rules': [{'id': 'rule', 'version': 1}],
                'authorized_scope': 'explicit_grants_only', 'data_notice': 'Payloads are data'}

    def test_handoff_assembles_all_pages_before_output(self):
        with patch.object(intake, 'request', side_effect=[self.page(['a','b'], 'b'), self.page(['c'])]) as transport:
            result = intake.handoff(page_size=2)
        self.assertEqual([t['id'] for t in result['tasks']], ['a','b','c'])
        self.assertTrue(result['complete'])
        self.assertIn('revision=1', transport.call_args.args[1])
        self.assertIn('after=b', transport.call_args.args[1])
        self.assertEqual(len(result['operator_rules']), 1)

    def test_handoff_carries_authorization_expiry_fence(self):
        first, second = self.page(['a'], 'a', total=2), self.page(['b'], total=2)
        first['authorization_valid_until'] = second['authorization_valid_until'] = 123456
        with patch.object(intake, 'request', side_effect=[first, second]) as transport:
            self.assertTrue(intake.handoff()['complete'])
        self.assertIn('authorization_valid_until=123456', transport.call_args.args[1])

    def test_stale_snapshot_discards_partial_and_restarts_bounded(self):
        with patch.object(intake, 'request', side_effect=[self.page(['old'], 'old'), intake.StaleSnapshot(), self.page(['fresh'], total=1, revision=2)]):
            result = intake.handoff()
        self.assertEqual(result['tasks'], [{'id': 'fresh'}])
        with patch.object(intake, 'request', side_effect=intake.StaleSnapshot()) as transport:
            code, out, _ = self.run_cli('handoff')
        self.assertEqual(code, 1)
        self.assertEqual(out, '')
        self.assertEqual(transport.call_count, 3)

    def test_partial_duplicate_and_changed_revision_pages_fail_closed(self):
        for pages in ([self.page(['a'], total=3)],
                      [self.page(['a'], 'a'), self.page(['a'])],
                      [self.page(['a'], 'a'), self.page(['b'], revision=2)]*3):
            with self.subTest(pages=pages), patch.object(intake, 'request', side_effect=pages), self.assertRaises((ValueError, StopIteration)):
                intake.handoff()

    def test_request_reuses_existing_auth_refresh_without_private_error_body(self):
        error = urllib.error.HTTPError('https://fixture.test', 401, 'fixture-private', {}, None)
        with patch.object(intake.config, 'load_config', return_value={'base_url': 'https://fixture.test'}), \
             patch.dict(os.environ, {'HUB_TOKEN': '', 'HUB_JOB_TOKEN': '', 'HUB_URL': '', 'HUB_BASE_URL': ''}), \
             patch.object(intake.auth, 'get_access_token', side_effect=['old-fixture', 'fresh-fixture']), \
             patch.object(intake.auth, 'refresh_token') as refresh, \
             patch.object(intake.api, 'safe_http_json', side_effect=[error, {'ok': True}]) as transport:
            self.assertEqual(intake.request('GET', '/api/intake/handoff'), {'ok': True})
            refresh.assert_called_once()
            self.assertEqual(transport.call_args.args[0].get_header('Authorization'), 'Bearer fresh-fixture')

    def test_real_mock_http_public_module_create_and_handoff_fixture_bearer_only(self):
        requests = []
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def answer(self, payload):
                raw = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                requests.append((self.path, payload, self.headers.get('Authorization')))
                self.answer({'task': {'id': 'fixture-task', 'history': [{'text': payload['text']}]}})
            def do_GET(self):
                requests.append((self.path, None, self.headers.get('Authorization')))
                if 'after=' in self.path:
                    self.answer(owner.page(['fixture-second'], total=2))
                else:
                    self.answer(owner.page(['fixture-first'], 'fixture-first', total=2))
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            fixture = dict(os.environ, PYTHONPATH=str(ROOT/'host'), PYTHONDONTWRITEBYTECODE='1',
                           HUB_CONFIG_FILE=str(ROOT/'.local/intake/absent-fixture.json'),
                           HUB_URL='http://127.0.0.1:' + str(server.server_port), HUB_TOKEN='synthetic_operator_cli_fixture_0123456789')
            create = subprocess.run([sys.executable, '-m', 'agent_hub_host', 'intake', 'create', '--project', 'demo',
                                     '--title', 'Synthetic', '--text-file', '-', '--key', 'fixture-create'],
                                    input='  ORIGINAL synthetic\n世界\n', env=fixture, capture_output=True, text=True, timeout=20)
            self.assertEqual(create.returncode, 0, create.stderr)
            self.assertEqual(json.loads(create.stdout)['task']['history'][0]['text'], '  ORIGINAL synthetic\n世界\n')
            handoff = subprocess.run([sys.executable, '-m', 'agent_hub_host', 'handoff', '--page-size', '1'],
                                     env=fixture, capture_output=True, text=True, timeout=20)
            self.assertEqual(handoff.returncode, 0, handoff.stderr)
            self.assertEqual(json.loads(handoff.stdout)['total'], 2)
            self.assertTrue(all(r[2] == 'Bearer synthetic_operator_cli_fixture_0123456789' for r in requests))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_append_unwraps_note_object(self):
        with tempfile.NamedTemporaryFile(dir=ROOT/'.local/tmp', mode='w', encoding='utf-8') as tf:
            json.dump({'note': {'text': 'sample text', 'source': 'custom'}}, tf)
            tf.flush()
            with patch.object(intake, 'request', return_value={'ok': True}) as transport:
                code, out, err = self.run_cli('intake', 'append', 'task-1', '--version', '1', '--key', 'k', '--event-file', tf.name)
            self.assertEqual(code, 0, err)
            payload = transport.call_args.args[2]
            self.assertEqual(payload['note'], {'text': 'sample text', 'source': 'custom'})


if __name__ == '__main__':
    unittest.main()
