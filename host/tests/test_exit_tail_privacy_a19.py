import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_hub_host.launch_diagnostics import extract_crash_tail, read_native_screen_crash_tail
from agent_hub_host.runtime_diagnostics import _sanitize_tail


NATIVE = '68cfc61d-c60a-4b9d-9f69-970b4ad1cf7e'
TRANSPORT = 'Transport: The socket connection was closed unexpectedly'


class ExitTailPrivacy(unittest.TestCase):
    def test_transport_failure_survives_without_inventing_oom(self):
        self.assertEqual(extract_crash_tail(TRANSPORT), TRANSPORT)

    def test_secret_assignments_redacted_before_tail_and_line_clipping(self):
        for value in ('password=SYNTHETIC_ONLY_123',
                      '"password":"SYNTHETIC_ONLY_123"',
                      "'refresh_token': 'SYNTHETIC_ONLY_123'",
                      'aws_secret_access_key=SYNTHETIC_ONLY_123',
                      'Authorization: Bearer SYNTHETIC_ONLY_123',
                      'Cookie: harmless=value; session=SYNTHETIC_ONLY_123',
                      'https://user:SYNTHETIC_ONLY_123@example.invalid/',
                      'https://example.invalid/?code=SYNTHETIC_ONLY_123'):
            with self.subTest(value=value):
                text = 'panic(main thread): abort() called ' + value
                self.assertNotIn('SYNTHETIC_ONLY_123', extract_crash_tail(text))
                self.assertNotIn('SYNTHETIC_ONLY_123', _sanitize_tail(text, 30))
        secret = 'Z' * 9000
        self.assertNotIn('Z' * 20, _sanitize_tail('password=' + secret))

    def test_oversized_input_and_sanitizer_failure_never_return_raw(self):
        self.assertEqual(_sanitize_tail('password=' + 'z' * 65536), '')
        with patch('agent_hub_host.runtime_diagnostics._sanitize_tail', side_effect=ValueError):
            self.assertEqual(extract_crash_tail('panic: password=SYNTHETIC_ONLY_123'), '')

    def test_multiline_private_key_is_scrubbed_before_signature_selection(self):
        text = '-----BEGIN PRIVATE KEY-----\npanic: SYNTHETIC_ONLY_123\n-----END PRIVATE KEY-----\n' + TRANSPORT
        self.assertEqual(extract_crash_tail(text), TRANSPORT)

    def test_native_state_env_and_parent_symlink_refusal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            native = root / 'sessions' / NATIVE
            native.mkdir(parents=True)
            (native / 'screen.txt').write_text(TRANSPORT)
            with patch.dict(os.environ, {'APLEXER_STATE_DIR': str(root)}):
                self.assertEqual(read_native_screen_crash_tail(None, NATIVE), TRANSPORT)
            alias = root / 'alias'
            alias.symlink_to(root / 'sessions', target_is_directory=True)
            other = root / 'other'
            other.mkdir()
            (other / 'sessions').symlink_to(alias, target_is_directory=True)
            self.assertEqual(read_native_screen_crash_tail(str(other), NATIVE), '')

    def test_partial_first_line_is_not_forwarded(self):
        with tempfile.TemporaryDirectory() as tmp:
            native = Path(tmp) / 'sessions' / NATIVE
            native.mkdir(parents=True)
            (native / 'screen.txt').write_text('password=' + 'z' * 100 + 'panic: secret-fragment\n' + TRANSPORT)
            tail = read_native_screen_crash_tail(tmp, NATIVE, max_bytes=100)
            self.assertEqual(tail, TRANSPORT)


if __name__ == '__main__':
    unittest.main()
