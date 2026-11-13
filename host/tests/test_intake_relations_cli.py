"""`hub intake depend` and `hub task-artifact`/`artifact`: Hub2 shapes and named failures.

Transport is synthetic; files live in a private temporary directory.
"""
import base64
import io
import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from agent_hub_host import cli, intake, routing

PNG_HEADER = b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR'


class Run:
    def __init__(self, *args):
        self.out, self.err, self.code = io.StringIO(), io.StringIO(), 0
        with patch('sys.argv', ['hub', *args]), patch('sys.stdout', self.out), patch('sys.stderr', self.err):
            try:
                cli.main()
            except SystemExit as exc:
                self.code = exc.code


def http_error(status, body):
    return urllib.error.HTTPError('https://hub.test/x', status, 'x', {}, io.BytesIO(json.dumps(body).encode()))


class DependCommand(unittest.TestCase):
    def test_hub2_show_shape_supplies_control_version(self):
        shown = {'intake': {'id': 'c15'}, 'task': {'id': 'c15', 'updated_at': 1794552401229, 'control': {'version': 1794552401229}}}
        with patch.object(intake, 'request', side_effect=[shown, {'action': 'added'}]) as request:
            run = Run('intake', 'depend', 'c15ec6d2', '--on', '69aed9c3', '--key', 'k1')
        self.assertEqual(run.code, 0, run.err.getvalue())
        self.assertEqual(request.call_args_list[1].args[2]['expected_version'], 1794552401229)

    def test_missing_version_is_named_not_generic(self):
        with patch.object(intake, 'request', return_value={'intake': {'id': 'x'}}):
            run = Run('intake', 'depend', 'c15ec6d2', '--on', '69aed9c3')
        self.assertEqual(run.code, 1)
        self.assertIn('task.control.version', run.err.getvalue())
        self.assertNotIn('unexpected error details withheld', run.err.getvalue())

    def test_server_refusal_keeps_slug_and_detail(self):
        body = {'error': 'prerequisite_not_found', 'detail': 'prerequisite intake 69aed9c3 does not exist or is not visible to this caller'}
        with patch.object(intake, 'request', side_effect=intake.http_error(http_error(404, body), 'POST', {'key': 'k'})):
            run = Run('intake', 'depend', 'c15ec6d2', '--on', '69aed9c3', '--version', '5', '--key', 'k')
        self.assertEqual(run.code, 1)
        self.assertIn('prerequisite_not_found', run.err.getvalue())
        self.assertIn('not visible to this caller', run.err.getvalue())


class ArtifactCommands(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def png(self, name, size, mode=0o644):
        path = self.dir / name
        path.write_bytes(PNG_HEADER + b'\0' * (size - len(PNG_HEADER)))
        os.chmod(path, mode)
        return path

    def upload(self, path, name='shot'):
        return Run('task-artifact', 'b29f8382-a8b2-4e4b-bfe1-a6b00f829f8c', '--name', name, '--file', str(path), '--sanitized')

    def test_normal_screenshot_sizes_upload_to_the_intake(self):
        for size in (963369, 1855328, 967418):
            path = self.png(f's{size}.png', size)
            with patch.object(intake, 'request', return_value={'name': 'shot', 'bytes': size}) as request:
                run = self.upload(path)
            self.assertEqual(run.code, 0, run.err.getvalue())
            method, url, payload = request.call_args.args
            self.assertEqual((method, url), ('POST', '/api/intake/tasks/b29f8382-a8b2-4e4b-bfe1-a6b00f829f8c/artifacts'))
            self.assertEqual(len(base64.b64decode(payload['data_base64'])), size)
            self.assertTrue(payload['sanitized'])
            self.assertGreater(request.call_args.kwargs['max_request'], len(payload['data_base64']))

    def assert_check(self, run, check):
        message = run.err.getvalue()
        self.assertEqual(run.code, 1)
        self.assertIn(f'PNG rejected ({check} check)', message)
        self.assertNotIn('host admission', message)
        self.assertNotIn('bounded regular private file', message)

    def test_each_local_refusal_names_its_check(self):
        with patch.object(intake, 'request') as request:
            self.assert_check(self.upload(self.png('big.png', routing.ARTIFACT_MAX_BYTES + 1)), 'size')
            link = self.dir / 'link.png'
            link.symlink_to(self.png('real.png', 100))
            self.assert_check(self.upload(link), 'symlink')
            (self.dir / 'notpng.png').write_bytes(b'GIF89a' + b'\0' * 50)
            self.assert_check(self.upload(self.dir / 'notpng.png'), 'type')
            self.assert_check(self.upload(self.dir / 'missing.png'), 'exists')
            self.assert_check(self.upload(self.dir), 'regular-file')
            self.assert_check(self.upload(self.png('ok.png', 100), name='../bad'), 'name')
        request.assert_not_called()

    def test_server_refusal_names_the_check(self):
        body = {'error': 'artifact_too_large', 'detail': 'size check failed: 7000000 bytes exceeds the 6291456-byte (6 MiB) limit'}
        with patch.object(intake, 'request', side_effect=intake.http_error(http_error(413, body), 'POST', {'key': 'k'})):
            run = self.upload(self.png('ok.png', 100))
        self.assertEqual(run.code, 1)
        self.assertIn('artifact_too_large', run.err.getvalue())
        self.assertIn('size check failed', run.err.getvalue())
        self.assertNotIn('host admission', run.err.getvalue())

    def test_download_writes_a_new_private_file(self):
        data = PNG_HEADER + b'\1' * 2_000_000
        out = self.dir / 'out.png'
        with patch.object(intake, 'request', return_value={'data_base64': base64.b64encode(data).decode(), 'sha256': 'abc'}) as request:
            run = Run('artifact', 'b29f8382-a8b2-4e4b-bfe1-a6b00f829f8c', 'shot', '--output', str(out))
        self.assertEqual(run.code, 0, run.err.getvalue())
        self.assertEqual(request.call_args.args, ('GET', '/api/intake/tasks/b29f8382-a8b2-4e4b-bfe1-a6b00f829f8c/artifacts/shot'))
        self.assertEqual(out.read_bytes(), data)
        self.assertEqual(out.stat().st_mode & 0o777, 0o600)
        with patch.object(intake, 'request', return_value={'data_base64': base64.b64encode(data).decode()}):
            again = Run('artifact', 'b29f8382-a8b2-4e4b-bfe1-a6b00f829f8c', 'shot', '--output', str(out))
        self.assert_check(again, 'output')


if __name__ == '__main__':
    unittest.main()
