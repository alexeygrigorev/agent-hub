"""hub asks CLI (intake 55c10deb): the fallback writer stores text byte-exact
and the read/maintenance verbs hit the agreed /api/asks routes."""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import patch


class AsksCli(unittest.TestCase):
    def _run(self, argv, response=None, stdin=None):
        from agent_hub_host import cli, session_control
        calls = []

        def request(method, path, payload=None):
            calls.append((method, path, payload))
            return response if response is not None else {'ok': True}

        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(sys, 'argv', ['hub'] + argv), \
                patch.object(session_control, 'request', side_effect=request), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            if stdin is not None:
                fake = io.TextIOWrapper(io.BytesIO(stdin.encode('utf-8')), encoding='utf-8')
                with patch.object(sys, 'stdin', fake):
                    code = self._main(cli)
            else:
                code = self._main(cli)
        return calls, stdout.getvalue(), stderr.getvalue(), code

    @staticmethod
    def _main(cli):
        try:
            cli.main()
            return 0
        except SystemExit as exc:
            return exc.code

    def test_record_sends_verbatim_text_from_stdin(self):
        text = '  Сделай дашборд\n\n1. usage over time  \n'
        calls, out, err, code = self._run(['asks', 'record', '--ref', 't1985u', '--agent', 'e96e39a2', '--agent-name', 'Developer',
                                           '--intake', '55c10deb', '--text-file', '-'], {'ok': True, 'accepted': 1}, stdin=text)
        self.assertEqual(code, 0, err)
        method, path, payload = calls[0]
        self.assertEqual((method, path), ('POST', '/api/asks/ingest'))
        self.assertEqual(payload['source'], 'bot')
        message = payload['agents'][0]['messages'][0]
        self.assertEqual(message, {'msg_id': 't1985u', 'role': 'user', 'kind': 'message', 'text': text, 'intake_id': '55c10deb'})
        self.assertEqual(json.loads(out)['accepted'], 1)

    def test_record_refuses_empty_and_oversized_text_instead_of_truncating(self):
        calls, _, err, code = self._run(['asks', 'record', '--ref', 't1u', '--agent', 'dev', '--text-file', '-'], stdin='   \n')
        self.assertNotEqual(code, 0)
        self.assertIn('empty', err)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'big.txt')
            with open(path, 'w', encoding='utf-8') as fh:
                fh.write('x' * 30000)
            calls, _, err, code = self._run(['asks', 'record', '--ref', 't2u', '--agent', 'dev', '--text-file', path])
        self.assertNotEqual(code, 0)
        self.assertIn('exceeds capacity', err)
        self.assertEqual(calls, [])

    def test_list_map_dismiss_verify_routes(self):
        listing = {'counts': {'unmapped': 1}, 'asks': [{'id': 'abcdef123456', 'state': 'unmapped', 'agent_id': 'dev', 'agent_name': 'Developer',
                                                       't_id': 't9u', 'intake_id': None, 'ac_line': None, 'text': 'Build\nthe page'}]}
        calls, out, err, code = self._run(['asks', 'list', '--state', 'unmapped'], listing)
        self.assertEqual(code, 0, err)
        self.assertEqual(calls[0][:2], ('GET', '/api/asks?state=unmapped&limit=100'))
        self.assertIn('abcdef12  unmapped', out)
        self.assertIn('Build the page', out)
        calls, *_ = self._run(['asks', 'map', 'abcdef', '--intake', '55c10deb', '--ac', '3'])
        self.assertEqual(calls[0], ('POST', '/api/asks/abcdef/map', {'intake_id': '55c10deb', 'ac_line': 3}))
        calls, *_ = self._run(['asks', 'dismiss', 'abcdef', '--reason', 'greeting'])
        self.assertEqual(calls[0], ('POST', '/api/asks/abcdef/dismiss', {'reason': 'greeting'}))
        calls, *_ = self._run(['asks', 'verify', 'abcdef', '--evidence', 'live'])
        self.assertEqual(calls[0], ('POST', '/api/asks/abcdef/verify', {'evidence': 'live'}))
        calls, *_ = self._run(['asks', 'settings', '--reconcile-intake', 'none'])
        self.assertEqual(calls[0], ('POST', '/api/asks/settings', {'reconcile_intake': None}))

    def test_tail_and_search_print_jsonl_oldest_first(self):
        resp = {'messages': [{'msg_id': 't2u', 'text': 'Second about stats'}, {'msg_id': 't1u', 'text': 'first'}]}
        calls, out, err, code = self._run(['asks', 'tail', '--role', 'user'], resp)
        self.assertEqual(code, 0, err)
        self.assertEqual(calls[0][1], '/api/asks/messages?role=user&limit=100')
        self.assertEqual([json.loads(l)['msg_id'] for l in out.splitlines()], ['t1u', 't2u'])
        calls, out, *_ = self._run(['asks', 'search', 'STATS'], resp)
        self.assertEqual([json.loads(l)['msg_id'] for l in out.splitlines()], ['t2u'])


if __name__ == '__main__':
    unittest.main()
