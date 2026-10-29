"""Real-journal relay regressions; no native process, network or credentials."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from agent_hub_host import session_messages as sm


class Utf8RelayTests(unittest.TestCase):
    def relay_case(self, body, expected):
        with tempfile.TemporaryDirectory() as tmp:
            journal = str(Path(tmp) / 'msg-owned.json')
            rec = {'state': 'delivered', 'envelope_id': 'native-envelope',
                   'sender_workspace': 'sender-workspace', 'hub_session_id': 'owned-session',
                   'from_actor': 'agent:developer', 'created_ms': 1000,
                   'target': {'session_id': 'owned-native'}, 'message_id': 'hub-message'}
            sm._write_journal(journal, rec)
            env = {'id': 'native-reply', 'from': {'session_id': 'owned-native'},
                   'reply_to': 'native-envelope', 'body': body}
            sends = []
            def request(method, path, payload):
                self.assertEqual((method, path), ('POST', '/api/bus/send'))
                self.assertLessEqual(len(payload['body'].encode('utf-8')), 16384)
                self.assertEqual(payload['body'], expected)
                self.assertEqual(payload['from_actor'], 'session:owned-session')
                self.assertEqual(payload['to_actor'], 'agent:developer')
                self.assertEqual(payload['reply_to'], 'hub-message')
                sends.append(payload)
                return {'id': 'hub-reply'}
            with patch.object(sm, 'fetch_message_log', return_value=[env]):
                first = sm.relay_replies(journal_dir=tmp, executable='unused', request=request, now_ms=1000)
                self.assertEqual(first, [{'message_id': 'hub-message', 'reply_id': 'hub-reply'}])
                self.assertEqual(sm.relay_replies(journal_dir=tmp, executable='unused', request=request, now_ms=1000), [])
            self.assertEqual(len(sends), 1)
            self.assertEqual(sends[0]['idempotency_key'], 'native-native-reply')
            self.assertEqual(json.loads(Path(journal).read_text())['relayed'], ['native-reply'])

    def test_short_unicode_unchanged(self):
        self.relay_case('Tabo says Привет 😀', 'Tabo says Привет 😀')

    def test_ascii_at_byte_limit_unchanged(self):
        self.relay_case('a' * 16384, 'a' * 16384)

    def test_multibyte_at_byte_limit_unchanged(self):
        self.relay_case('€' * 5461 + 'a', '€' * 5461 + 'a')

    def test_four_byte_characters_at_limit_unchanged(self):
        self.relay_case('😀' * 4096, '😀' * 4096)

    def test_multibyte_over_limit_truncates_safely(self):
        self.relay_case('€' * 6000, '€' * 5461)

    def test_cut_codepoint_removed_without_replacement(self):
        self.relay_case('a' * 16383 + '€', 'a' * 16383)

    def test_ascii_over_limit(self):
        self.relay_case('a' * 17000, 'a' * 16384)


if __name__ == '__main__':
    unittest.main()
