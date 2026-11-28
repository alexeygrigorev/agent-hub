"""Pure enrollment preflight: invalid roots must not consume one-use pairing."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from agent_hub_host import cli


class EnrollPreflightTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=os.environ['TMPDIR'])
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.root = self.home / 'work'
        self.root.mkdir()
        self.config = self.home / 'config.json'
        self.client = Mock()
        self.client.request.return_value = {'token': 'SYNTHETIC_NEW', 'machine_id': 'new-machine'}

    def invoke(self, roots, engines=()):
        argv = ['--config', str(self.config), 'enroll']
        for root in roots:
            argv += ['--root', str(root)]
        for engine in engines:
            argv += ['--engine', engine]
        output, errors = io.StringIO(), io.StringIO()
        env = {'HOME': str(self.home), 'USERPROFILE': str(self.home), 'HUB2_PAIRING_CODE': 'SYNTHETIC_CODE'}
        with patch.dict(os.environ, env, clear=True), patch.object(cli, 'Client', return_value=self.client) as constructor, \
                patch.object(cli, 'save_config') as save, patch.object(cli.getpass, 'getpass') as prompt, \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            result = cli.main(argv)
        return result, constructor, save, prompt, output.getvalue(), errors.getvalue()

    def test_invalid_roots_precede_pairing_and_config_publication(self):
        file = self.home / 'file'
        file.write_text('synthetic ordinary file')
        for invalid in (self.home / 'absent', file):
            with self.subTest(root=invalid.name):
                self.client.reset_mock()
                status, constructor, save, prompt, output, errors = self.invoke([self.root, invalid])
                self.client.request.assert_not_called()
                self.assertEqual(status, 1)
                save.assert_not_called()
                prompt.assert_not_called()
                self.assertEqual(output, '')
                self.assertNotIn('SYNTHETIC_CODE', errors)
                self.assertFalse(self.config.exists())

    def test_invalid_root_precedes_existing_credential_refresh(self):
        before = json.dumps({'server': 'https://synthetic.example.invalid', 'token': 'SYNTHETIC_OLD',
                             'refresh_token': 'SYNTHETIC_REFRESH', 'expires_at': 0}).encode()
        self.config.write_bytes(before)
        with patch('agent_hub_host.auth.refresh', return_value=self.client) as refresh:
            status, _, save, _, _, _ = self.invoke([self.home / 'absent'])
        self.assertEqual(status, 1)
        refresh.assert_not_called()
        self.client.request.assert_not_called()
        save.assert_not_called()
        self.assertEqual(self.config.read_bytes(), before)

    def test_valid_roots_pair_once_preserving_payload_order_and_duplicates(self):
        other = self.home / 'other'
        other.mkdir()
        status, constructor, save, prompt, output, _ = self.invoke(
            [other, self.root / '..' / 'work', other], ['codex', 'zai'])
        self.assertEqual(status, 0)
        constructor.assert_called_once_with(cli.DEFAULT_SERVER, '')
        self.client.request.assert_called_once_with('POST', '/api/enrollments/complete', {'code': 'SYNTHETIC_CODE'})
        save.assert_called_once_with(self.config, {'server': cli.DEFAULT_SERVER, 'token': 'SYNTHETIC_NEW',
            'machine_id': 'new-machine', 'roots': [str(other), str(self.root), str(other)], 'engines': ['codex', 'zai']})
        prompt.assert_not_called()
        self.assertEqual(json.loads(output), {'enrolled': 'new-machine'})

    def test_existing_configuration_can_reenroll_and_omit_engine(self):
        before = json.dumps({'server': 'https://synthetic.example.invalid', 'token': 'SYNTHETIC_OLD',
                             'machine_id': 'old-machine', 'opaque': 'synthetic'}).encode()
        self.config.write_bytes(before)
        status, constructor, save, _, output, _ = self.invoke([self.root])
        self.assertEqual(status, 0)
        constructor.assert_called_once_with('https://synthetic.example.invalid', 'SYNTHETIC_OLD')
        self.client.request.assert_called_once_with('POST', '/api/enrollments/complete', {'code': 'SYNTHETIC_CODE'})
        self.assertEqual(save.call_args.args[1], {'server': 'https://synthetic.example.invalid', 'token': 'SYNTHETIC_NEW',
                         'machine_id': 'new-machine', 'roots': [str(self.root)], 'engines': []})
        self.assertEqual(json.loads(output), {'enrolled': 'new-machine'})
        self.assertEqual(self.config.read_bytes(), before, 'publication is mocked, never an actual enrollment')

    def test_malformed_config_input_precedes_pairing(self):
        self.config.write_text('{invalid synthetic JSON')
        status, constructor, save, _, output, _ = self.invoke([self.root])
        self.assertEqual(status, 1)
        constructor.assert_not_called()
        self.client.request.assert_not_called()
        save.assert_not_called()
        self.assertEqual(output, '')


if __name__ == '__main__':
    unittest.main()
