import json
import importlib.util
import os
import subprocess
import sys
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
            legacy = update.legacy_switches(list(self.unit_dir.glob('*.conf')), self.unit_dir, self.root)
            update.retire_switches(legacy, self.root.parent / '.host-release-switch-journal.jsonl')
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

    def test_launcher_repoint_refuses_writable_built_release_and_keeps_dropin(self):
        original = self.run_command
        switch_script = Path(__file__).resolve().parents[2] / 'scripts' / 'switch-host-release.py'
        switch_results = []

        def run(argv, **kwargs):
            if len(argv) > 1 and argv[1].endswith('switch-host-release.py'):
                kwargs.pop('umask', None)
                result = subprocess.run([sys.executable, str(switch_script), *argv[2:]], **kwargs)
                switch_results.append(result)
                return result
            result = original(argv, **kwargs)
            if len(argv) > 1 and argv[1].endswith('build-host-release.py'):
                writable = self.root.parent.joinpath(self.target, 'writable')
                writable.write_text('unsafe release')
                writable.chmod(0o664)
            return result

        with patch.object(self, 'run_command', side_effect=run):
            self.assertEqual(self.apply(), ('failed', 'release_switch_failed', self.before))
        self.assertEqual(len(switch_results), 1)
        self.assertNotEqual(switch_results[0].returncode, 0)
        self.assertIn('group/world-writable', switch_results[0].stderr)
        self.assertEqual(self.template.read_text(), self.text)
        self.assertFalse((self.unit_dir / update.DROPIN).exists())

    def test_never_overwrites_an_unmanaged_destination(self):
        destination = self.unit_dir / update.DROPIN
        destination.write_text('another owner\n')
        self.assertEqual(self.apply(), ('refused', 'release_dropin_conflict', self.before))
        self.assertEqual(destination.read_text(), 'another owner\n')

    def test_legacy_switch_is_preserved_and_next_release_becomes_current(self):
        self.template = self.unit_dir / ('zz~~~~' + self.before[:8] + '-switch-release.conf')
        self.template.write_text(self.text)
        original = self.template
        self.assertEqual(self.apply(), ('updated', None, self.before))
        self.assertFalse(original.exists())
        self.assertEqual(original.with_suffix('.conf.disabled').read_text(), self.text)
        journal = self.root.parent / '.host-release-switch-journal.jsonl'
        self.assertEqual(json.loads(journal.read_text())['source'], str(original))
        self.before = self.target
        self.root = self.root.parent / self.target
        self.assertEqual(self.apply(), ('current', None, self.target))

    def test_later_foreign_override_is_refused_without_changes(self):
        self.template = self.unit_dir / 'zz~~~~foreign.conf'
        self.template.write_text(self.text)
        self.assertEqual(self.apply(), ('refused', 'release_dropin_precedence', self.before))
        self.assertEqual(self.template.read_text(), self.text)
        self.assertFalse(any('build-host-release.py' in str(c) for c in self.calls))

    def test_legacy_backup_collision_refuses_without_build(self):
        self.template = self.unit_dir / ('zz~~~~' + self.before[:8] + '-switch-release.conf')
        self.template.write_text(self.text)
        self.template.with_suffix('.conf.disabled').write_text('preserve')
        self.assertEqual(self.apply(), ('refused', 'release_dropin_backup_conflict', self.before))

    def test_failed_build_keeps_legacy_override_active(self):
        self.template = self.unit_dir / ('zz~~~~' + self.before[:8] + '-switch-release.conf')
        self.template.write_text(self.text)
        self.fail_build = True
        self.assertEqual(self.apply(), ('failed', 'release_build_failed', self.before))
        self.assertEqual(self.template.read_text(), self.text)
        self.assertFalse(self.template.with_suffix('.conf.disabled').exists())

    def test_legacy_name_with_foreign_content_is_not_adopted(self):
        self.template = self.unit_dir / ('zz~~~~' + self.target[:8] + '-switch-release.conf')
        self.template.write_text(self.text)
        self.assertEqual(self.apply(), ('refused', 'release_dropin_precedence', self.before))
        self.assertEqual(self.template.read_text(), self.text)

    def test_backup_created_during_build_is_preserved(self):
        self.template = self.unit_dir / ('zz~~~~' + self.before[:8] + '-switch-release.conf')
        self.template.write_text(self.text)
        original_run = self.run_command
        def run(argv, **kwargs):
            result = original_run(argv, **kwargs)
            if len(argv) > 1 and argv[1].endswith('build-host-release.py'):
                self.template.with_suffix('.conf.disabled').write_text('sentinel')
            return result
        with patch.object(self, 'run_command', side_effect=run):
            self.assertEqual(self.apply(), ('refused', 'release_dropin_backup_conflict', self.before))
        self.assertEqual(self.template.read_text(), self.text)
        self.assertEqual(self.template.with_suffix('.conf.disabled').read_text(), 'sentinel')

    def test_source_changed_during_build_is_preserved(self):
        self.template = self.unit_dir / ('zz~~~~' + self.before[:8] + '-switch-release.conf')
        self.template.write_text(self.text)
        original_run = self.run_command
        def run(argv, **kwargs):
            result = original_run(argv, **kwargs)
            if len(argv) > 1 and argv[1].endswith('build-host-release.py'):
                self.template.write_text(self.text + '# operator edit\n')
            return result
        with patch.object(self, 'run_command', side_effect=run):
            self.assertEqual(self.apply(), ('refused', 'release_dropin_source_changed', self.before))
        self.assertTrue(self.template.exists())
        self.assertFalse(self.template.with_suffix('.conf.disabled').exists())

    def test_migration_lock_rejects_competing_writer_and_accepts_inherited_fd(self):
        with update.migration_lock(self.unit_dir) as fd:
            with self.assertRaisesRegex(update.UpdateRefused, 'release_migration_busy'):
                with update.migration_lock(self.unit_dir):
                    self.fail('competing migration acquired lock')
            with update.migration_lock(self.unit_dir, fd):
                pass

    def test_backup_appearing_at_atomic_rename_is_never_overwritten(self):
        self.template = self.unit_dir / ('zz~~~~' + self.before[:8] + '-switch-release.conf')
        self.template.write_text(self.text)
        backup = self.template.with_suffix('.conf.disabled')
        legacy = update.legacy_switches([self.template], self.unit_dir, self.root)
        real_validate = update.validate_switches
        validations = 0
        def validate(entries):
            nonlocal validations
            real_validate(entries)
            validations += 1
            if validations == 2:
                backup.write_text('racing sentinel')
        with update.migration_lock(self.unit_dir), patch.object(update, 'validate_switches', side_effect=validate):
            with self.assertRaisesRegex(update.UpdateRefused, 'release_dropin_backup_conflict'):
                update.retire_switches(legacy, self.root.parent / 'journal.jsonl')
        self.assertEqual(backup.read_text(), 'racing sentinel')
        self.assertEqual(self.template.read_text(), self.text)

    def manual_switch(self):
        target_release = self.root.parent / self.target
        target_release.mkdir(parents=True, exist_ok=True)
        target_release.chmod(0o755)
        spec = importlib.util.spec_from_file_location('switch_release',
            Path(__file__).resolve().parents[2] / 'scripts' / 'switch-host-release.py')
        switch = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(switch)
        argv = ['switch', '--release', str(self.root.parent / self.target),
                '--template', str(self.template), '--dropin', str(self.unit_dir / update.DROPIN)]
        def command(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0,
                str(self.template) if 'show' in argv else '', '')
        with patch('sys.argv', argv), patch.object(switch.subprocess, 'run', side_effect=command):
            return switch.main()

    def test_manual_bootstrap_changes_effective_execstart(self):
        self.template = self.unit_dir / ('zz~~~~' + self.before[:8] + '-switch-release.conf')
        self.template.write_text(self.text)
        self.assertEqual(self.manual_switch(), 0)
        effective = sorted(self.unit_dir.glob('*.conf'))[-1].read_text()
        self.assertIn(str(self.root.parent / self.target), effective)
        self.assertEqual(self.template.with_suffix('.conf.disabled').read_text(), self.text)

    def test_release_bump_switches_fleet_to_hub2_heartbeat_and_moves_quota_dropin(self):
        """The fleet timer uses Hub2; the disabled Hub1 quota service follows its release."""
        self.template = self.unit_dir / ('zz~~~~' + self.before[:8] + '-switch-release.conf')
        self.template.write_text(self.text)
        user_dir = self.unit_dir.parent
        companion = (update.MARKER + '[Service]\nWorkingDirectory=' + str(self.root) + '\nExecStart=\nExecStart='
                     + str(self.root) + '/.venv/bin/python -m agent_hub_host.quota_server --config /keep/quota.json\n')
        for name in ('agent-hub-fleet-host.service.d', 'agent-hub-quota-server.service.d'):
            (user_dir / name).mkdir()
            (user_dir / name / update.DROPIN).write_text(companion)
        unmanaged = user_dir / 'agent-hub-fleet-host.service.d' / 'zz~~other.conf'
        unmanaged.write_text('[Service]\nExecStart=' + str(self.root) + '/x\n')
        self.assertEqual(self.manual_switch(), 0)
        new_release = str(self.root.parent / self.target)
        fleet_text = (user_dir / 'agent-hub-fleet-host.service.d' / update.DROPIN).read_text()
        self.assertIn('hub2-host.json', fleet_text)
        self.assertIn('heartbeat', fleet_text)
        self.assertIn('StandardOutput=null', fleet_text)
        self.assertNotIn('fleet-host --cycles', fleet_text)
        self.assertNotIn(str(self.root), fleet_text)
        quota_text = (user_dir / 'agent-hub-quota-server.service.d' / update.DROPIN).read_text()
        self.assertIn(new_release, quota_text)
        self.assertNotIn(str(self.root), quota_text)
        self.assertIn('--config /keep/quota.json', quota_text)
        self.assertIn(str(self.root), unmanaged.read_text())

    def test_manual_bootstrap_refuses_foreign_later_override(self):
        self.template = self.unit_dir / 'zz~~~~foreign.conf'
        self.template.write_text(self.text)
        with self.assertRaisesRegex(SystemExit, 'release_dropin_precedence'):
            self.manual_switch()
        self.assertEqual(self.template.read_text(), self.text)
        self.assertFalse((self.unit_dir / update.DROPIN).exists())

    def test_manual_switch_uses_managed_marker_and_rejects_legacy_destination(self):
        target_release = self.root.parent / self.target
        target_release.mkdir(parents=True, exist_ok=True)
        target_release.chmod(0o755)
        spec = importlib.util.spec_from_file_location('switch_release',
            Path(__file__).resolve().parents[2] / 'scripts' / 'switch-host-release.py')
        switch = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(switch)
        destination = self.unit_dir / update.DROPIN
        argv = ['switch', '--release', str(self.root.parent / self.target),
                '--template', str(self.template), '--dropin', str(destination)]
        with patch('sys.argv', argv), patch.object(switch.subprocess, 'run',
                return_value=subprocess.CompletedProcess([], 0, '', '')):
            self.assertEqual(switch.main(), 0)
        self.assertTrue(destination.read_text().startswith(update.MARKER))
        self.assertIn(str(self.root.parent / self.target), destination.read_text())
        with patch('sys.argv', argv[:-1] + [str(self.unit_dir / 'zz~~~~legacy.conf')]):
            with self.assertRaisesRegex(SystemExit, 'use managed'):
                switch.main()
