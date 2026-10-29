import json
import os
import subprocess
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_hub_host import immutable_release as update


class ImmutableReleaseTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.before, self.target = '1' * 40, '2' * 40
        self.root = self.home / 'releases' / self.before
        self.root.mkdir(parents=True)
        (self.root / 'release-manifest.json').write_text(json.dumps({'release_rev': self.before}))
        scripts = self.root / 'scripts'
        scripts.mkdir()
        for name in ['build-host-release.py', 'verify-host-release.py', 'switch-host-release.py']:
            (scripts / name).touch()
        self.source = self.home / 'git' / 'agent-hub'
        (self.source / '.git').mkdir(parents=True)
        (self.source / '.git' / 'HEAD').write_text('ref: refs/heads/another-agent\n')
        self.unit_dir = self.home / '.config' / 'systemd' / 'user' / (update.SERVICE + '.d')
        self.unit_dir.mkdir(parents=True)
        self.template = self.unit_dir / 'route-fix.conf'
        self.text = '[Service]\nExecStart=' + str(self.root) + '/.venv/bin/python supervisor --credential-config keep-private-path\n'
        self.template.write_text(self.text)
        self.calls = []
        self.origin = 'https://github.com/alexeygrigorev/agent-hub.git'
        self.free_gb = 100
        self.config = {'disk_floor_gb': 15}
        self.current_target = self.target
        self.fail_build = False

    def tearDown(self):
        self.temp.cleanup()

    def request(self, method, path, body=None):
        if path.endswith('/watchdog'):
            return {'config': self.config}
        return {'sha': self.current_target}

    def run_command(self, argv, **kwargs):
        self.calls.append(argv)
        output = ''
        code = 0
        if argv[0] == 'git' and 'get-url' in argv:
            output = self.origin
        elif argv[0] == 'systemctl':
            output = str(self.template)
        elif len(argv) > 1 and argv[1].endswith('build-host-release.py'):
            if self.fail_build:
                code = 1
            else:
                release = self.root.parent / self.target
                release.mkdir()
                (release / 'release-manifest.json').write_text(json.dumps({'release_rev': self.target}))
        elif len(argv) > 1 and argv[1].endswith('switch-host-release.py'):
            template = Path(argv[argv.index('--template') + 1]).read_text()
            destination = Path(argv[argv.index('--dropin') + 1])
            destination.write_text(template.replace(str(self.root), str(self.root.parent / self.target)))
        return subprocess.CompletedProcess(argv, code, output, '')

    def apply(self):
        with patch.object(Path, 'home', return_value=self.home), patch.dict(os.environ, {'XDG_CONFIG_HOME': str(self.home / '.config')}):
            return update.apply(self.target, self.before, self.request, root=self.root,
                source=self.source, run=self.run_command, python='python',
                disk_usage=lambda _: types.SimpleNamespace(free=self.free_gb * 1024 ** 3))

    def test_builds_from_objects_and_switches_user_unit_without_changing_checkout(self):
        self.assertEqual(self.apply(), ('updated', None, self.before))
        destination = self.unit_dir / update.DROPIN
        text = destination.read_text()
        self.assertTrue(text.startswith(update.MARKER))
        self.assertIn(str(self.root.parent / self.target), text)
        self.assertIn('--credential-config keep-private-path', text)
        self.assertEqual(self.template.read_text(), self.text)
        self.assertEqual((self.source / '.git' / 'HEAD').read_text(), 'ref: refs/heads/another-agent\n')
        self.assertFalse(any('merge' in c or 'checkout' in c or 'reset' in c for c in self.calls))
        self.assertFalse(any('--restart' in c for c in self.calls))
        self.assertEqual(list((self.root / '.local' / 'tmp').iterdir()), [])

    def test_refuses_untrusted_origin_before_build(self):
        self.origin = 'https://example.invalid/other.git'
        self.assertEqual(self.apply(), ('refused', 'untrusted_release_origin', self.before))
        self.assertFalse((self.unit_dir / update.DROPIN).exists())

    def test_requires_valid_disk_floor_and_preserves_headroom(self):
        self.config = {}
        self.assertEqual(self.apply(), ('refused', 'release_disk_config_unavailable', self.before))
        self.config = {'disk_floor_gb': 15}
        self.free_gb = 15.5
        self.assertEqual(self.apply(), ('refused', 'release_disk_headroom', self.before))

    def test_superseded_target_does_not_activate_built_release(self):
        self.current_target = '3' * 40
        self.assertEqual(self.apply(), ('refused', 'release_target_changed', self.before))
        self.assertFalse((self.unit_dir / update.DROPIN).exists())

    def test_build_failure_preserves_existing_service_configuration(self):
        self.fail_build = True
        self.assertEqual(self.apply(), ('failed', 'release_build_failed', self.before))
        self.assertEqual(self.template.read_text(), self.text)
        self.assertFalse((self.unit_dir / update.DROPIN).exists())

    def test_never_overwrites_an_unmanaged_destination(self):
        destination = self.unit_dir / update.DROPIN
        destination.write_text('another owner\n')
        self.assertEqual(self.apply(), ('refused', 'release_dropin_conflict', self.before))
        self.assertEqual(destination.read_text(), 'another owner\n')
