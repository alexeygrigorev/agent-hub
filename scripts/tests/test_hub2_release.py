"""Targeted release safety checks; no network, live service or live venv writes."""
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[2]


def module(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), REPO / 'scripts' / (name + '.py'))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


updater = module('hub2-self-update')
builder = module('build-hub2-release')
enrollment = module('hub2-enroll')
SHA = 'a' * 40
OLD_SHA = 'b' * 40
HOST = f'agent_hub_host_v2-2.0.0+g{SHA}-py3-none-any.whl'


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        (REPO / '.local/tmp').mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=REPO / '.local/tmp', prefix='test-release-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / 'host'
        self.files = {name: ('artifact:' + name).encode() for name in
                      (HOST, 'websockets-16.0-py3-none-any.whl', 'hub.pyz',
                       'hub2-self-update.py', 'hub2-enroll.py', 'host-install.sh', *updater.UNITS)}
        self.manifest = {'schema_version': 1, 'protocol_version': 1, 'host_version': SHA,
                         'artifacts': {name: {'sha256': hashlib.sha256(data).hexdigest(), 'size': len(data)}
                                       for name, data in self.files.items()}}
        self.calls = []

    def fetch(self, url, limit):
        name = url.rsplit('/', 1)[1]
        return json.dumps(self.manifest).encode() if name == 'host-release.json' else self.files[name]

    def command(self, args):
        self.calls.append([str(value) for value in args])

    def old_release(self):
        old = self.root / 'releases' / OLD_SHA
        old.mkdir(parents=True)
        (self.root / 'current').symlink_to(old)
        return old

    def install(self, **kwargs):
        with patch.object(updater, 'fetch', self.fetch), patch.object(updater, 'command', self.command):
            updater.install('https://example.invalid', self.root, **kwargs)

    def test_valid_update_is_offline_and_keeps_previous(self):
        old = self.old_release()
        self.install()
        self.assertEqual((self.root / 'previous').resolve(), old)
        self.assertEqual((self.root / 'current').resolve().name, SHA)
        self.assertTrue(old.exists())
        pip = next(args for args in self.calls if 'pip' in args)
        self.assertIn('--no-index', pip)
        self.assertIn('--no-deps', pip)
        self.assertFalse(any('systemctl' in args and 'stop' in args for args in self.calls))

    def test_corrupt_artifact_never_touches_current_or_installs(self):
        old = self.old_release()
        self.files[HOST] = b'tampered'
        with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
            self.install()
        self.assertEqual((self.root / 'current').resolve(), old)
        self.assertEqual(self.calls, [])
        self.assertFalse((self.root / 'releases' / SHA).exists())

    def test_restart_failure_restores_previous_and_removes_only_new_release(self):
        old = self.old_release()
        original = self.command

        def fail_restart(args):
            original(args)
            if [str(value) for value in args] == ['systemctl', '--user', 'is-active', '--quiet', 'agent-hub2-host.service']:
                raise RuntimeError('failed restart')

        self.command = fail_restart
        with self.assertRaisesRegex(RuntimeError, 'failed restart'):
            self.install()
        self.assertEqual((self.root / 'current').resolve(), old)
        self.assertTrue(old.exists())
        self.assertFalse((self.root / 'releases' / SHA).exists())
        self.assertEqual(sum('restart' in args for args in self.calls), 2)

    def test_first_install_failure_removes_current_and_new_environment(self):
        def failure(args):
            if 'restart' in args:
                raise RuntimeError('cannot start')
        self.command = failure
        with self.assertRaises(RuntimeError):
            self.install()
        self.assertFalse((self.root / 'current').is_symlink())
        self.assertFalse((self.root / 'releases' / SHA).exists())

    def test_initial_staging_does_not_start_timer_before_enrollment(self):
        with patch.object(Path, 'home', return_value=Path(self.temporary.name)):
            self.install(install_service=True, restart=False)
        self.assertFalse(any('start' in args or 'restart' in args for args in self.calls))

    def test_reject_unsafe_manifest_and_protocol_before_install(self):
        for key, value in [('protocol_version', 2), ('host_version', '../invalid')]:
            with self.subTest(key=key):
                invalid = dict(self.manifest, **{key: value})
                with self.assertRaises(ValueError):
                    updater.validate_manifest(invalid)
        self.manifest['artifacts']['../escape'] = self.manifest['artifacts'][HOST]
        with self.assertRaisesRegex(ValueError, 'unsafe artifact'):
            self.install()
        self.assertEqual(self.calls, [])

    def test_existing_unmanaged_environment_is_preserved(self):
        target = self.root / 'releases' / SHA
        target.mkdir(parents=True)
        marker = target / 'owner-data'
        marker.write_text('preserve')
        with self.assertRaisesRegex(ValueError, 'explicit recovery'):
            self.install()
        self.assertEqual(marker.read_text(), 'preserve')

    def test_export_excludes_tracked_build_output(self):
        data = io.BytesIO()
        with tarfile.open(fileobj=data, mode='w') as archive:
            for name in ['hub2/host/agent_hub_host/client.py', 'hub2/host/build/lib/agent_hub_host/client.py']:
                entry = tarfile.TarInfo(name)
                entry.size = 5
                archive.addfile(entry, io.BytesIO(b'code\n'))
        with patch.object(builder, 'run', side_effect=[(SHA + '\n').encode(), data.getvalue()]):
            builder.export_source(REPO, 'HEAD', Path(self.temporary.name))
        self.assertTrue((Path(self.temporary.name) / 'hub2/host/agent_hub_host/client.py').is_file())
        self.assertFalse((Path(self.temporary.name) / 'hub2/host/build').exists())

    def test_enrollment_defaults_to_central_credentials_and_never_replaces_config(self):
        class Client:
            base_url = 'https://example.invalid'

            def request(self, method, path, body):
                self.request_body = body
                return {'machine_id': 'test-machine', 'token': 'test-secret'}

        client = Client()
        saved = []
        path = Path(self.temporary.name) / 'config.json'
        result = enrollment.enroll(client, lambda path, data: saved.append(data), path,
                                   [self.temporary.name], ['codex'], 'test-code')
        self.assertEqual(result, 'test-machine')
        self.assertEqual(saved[0]['launcher'], {'private_provider_homes': True})
        self.assertEqual(saved[0]['token'], 'test-secret')
        self.assertNotIn('code', saved[0])
        path.write_text('existing')
        with self.assertRaisesRegex(ValueError, 'refusing to replace'):
            enrollment.enroll(client, lambda *_: self.fail('must not save'), path,
                              [self.temporary.name], ['codex'], 'new-code')


if __name__ == '__main__':
    unittest.main()
