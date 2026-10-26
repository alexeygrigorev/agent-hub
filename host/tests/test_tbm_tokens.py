import json
import tempfile
import unittest
from pathlib import Path
from agent_hub_host.token_usage import native_session_tokens


class NativeTokensTest(unittest.TestCase):
    def test_uses_cumulative_totals_without_double_counting_cached_tokens(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'rollout.jsonl'
            path.write_text('\n'.join(json.dumps(e) for e in [
                {'type': 'token_usage_record', 'payload': {'usage': {'input_tokens': 50, 'output_tokens': 3},
                    'thread_token_usage': {'input_tokens': 500, 'cached_input_tokens': 200, 'output_tokens': 30}}},
                {'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {
                    'total_token_usage': {'input_tokens': 600, 'cached_input_tokens': 250,
                        'output_tokens': 40, 'reasoning_output_tokens': 10}}}},
            ]))
            class Locator:
                def locate(self, row): return path
            row = {'id': 'native-test', 'engine': 'codex', 'created_at_ms': 123}
            self.assertEqual(native_session_tokens(row, Locator()), {'input_tokens': 600, 'output_tokens': 40})
            self.assertNotIn('path', native_session_tokens(row, Locator()))

    def test_unknown_identity_and_unsupported_engines_stay_unknown(self):
        class Locator:
            def locate(self, row): return None
        self.assertIsNone(native_session_tokens({'id': 'x', 'engine': 'codex', 'created_at_ms': 123}, Locator()))
        self.assertIsNone(native_session_tokens({'id': 'x', 'engine': 'gemini', 'created_at_ms': 123}, Locator()))
