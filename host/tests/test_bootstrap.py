import hashlib
import io
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from agent_hub_host import bootstrap as b


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_checksum_requires_exact_asset_and_content(self):
        asset = self.root / 'runtime'
        asset.write_bytes(b'checked runtime')
        digest = hashlib.sha256(asset.read_bytes()).hexdigest()
        b.verify(asset, digest + '  runtime\n', 'runtime')
        for sums in (digest + '  other\n', digest + '  runtime\n' + digest + '  runtime\n',
                     '0' * 64 + '  runtime\n'):
            with self.assertRaises(ValueError):
                b.verify(asset, sums, 'runtime')

    def test_zip_rejects_traversal_windows_paths_and_links(self):
        for name in ('../escape', '/escape', 'C:/escape', 'dir\\escape', 'link'):
            archive = self.root / 'bad.zip'
            with zipfile.ZipFile(archive, 'w') as z:
                entry = zipfile.ZipInfo(name)
                if name == 'link':
                    entry.external_attr = 0o120777 << 16
                z.writestr(entry, 'bad')
            with self.assertRaises(ValueError):
                b.unpack(archive, self.root / 'out')

    def test_tar_does_not_follow_runtime_symlinks(self):
        archive = self.root / 'runtime.tar.gz'
        with tarfile.open(archive, 'w:gz') as t:
            entry = tarfile.TarInfo('node/bin/node')
            entry.size = 4
            t.addfile(entry, io.BytesIO(b'node'))
            entry = tarfile.TarInfo('node/bin/npm')
            entry.type = tarfile.SYMTYPE
            entry.linkname = '/outside'
            t.addfile(entry)
        b.unpack(archive, self.root / 'out')
        self.assertEqual((self.root / 'out/node/bin/node').read_bytes(), b'node')
        self.assertFalse((self.root / 'out/node/bin/npm').exists())

    def test_arm64_emulation_selects_native_node(self):
        from agent_hub_host import system
        with patch.dict(os.environ, {'PROCESSOR_ARCHITEW6432': 'ARM64',
                                     'PROCESSOR_ARCHITECTURE': 'AMD64'}), \
             patch.object(system.platform, 'system', return_value='Windows'), \
             patch.object(system, '_windows_native_arch', return_value='arm64'):
            self.assertEqual(b.architecture(), 'arm64')

    def test_engine_install_is_user_prefix_and_explicit_allowlist(self):
        with patch.object(b, 'run') as run:
            env = b.install_engines(self.root, self.root / 'bin/node', self.root / 'npm-cli.js')
        argv = run.call_args.args[0]
        self.assertIn(str(self.root / 'engines'), [str(x) for x in argv])
        self.assertEqual(argv[-4:], list(b.ENGINES.values()))
        self.assertEqual(env['npm_config_prefix'], str(self.root / 'engines'))

    def test_windows_portable_git_is_extracted_without_executing_installer(self):
        (self.root / 'tmp').mkdir()
        commands = []
        def run(argv, **kwargs):
            commands.append([str(v) for v in argv])
            if str(argv[0]).endswith('7zr.exe'):
                dest = Path(str(argv[3])[2:])
                for name in ('cmd/git.exe', 'usr/bin/bash.exe'):
                    p = dest / name
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.touch()
        with patch.object(b.sys, 'platform', 'win32'), \
             patch.object(b, 'architecture', return_value='arm64'), \
             patch.object(b, 'download'), patch.object(b, 'verify') as verify, \
             patch.object(b, 'run', side_effect=run), patch.dict(os.environ):
            target = b.ensure_git(self.root)
            self.assertEqual(os.environ['CLAUDE_CODE_GIT_BASH_PATH'], str(target / 'usr/bin/bash.exe'))
        self.assertTrue((target / 'cmd/git.exe').is_file())
        self.assertEqual(verify.call_count, 2)
        self.assertFalse(any('PortableGit' in c[0] for c in commands))
        self.assertTrue(commands[0][0].endswith('7zr.exe'))

    def test_existing_user_git_is_reused_without_system_git_probe(self):
        git = self.root / 'runtimes/git/bin/git'
        git.parent.mkdir(parents=True)
        git.touch()
        with patch.object(b.sys, 'platform', 'darwin'), patch.object(b, 'run') as run, \
             patch.object(b, 'download') as download, patch.dict(os.environ):
            b.ensure_git(self.root)
        download.assert_not_called()
        self.assertEqual(run.call_args.args[0], [git, '--version'])

    def test_user_service_preserves_paths_with_spaces(self):
        unusual = self.root / 'a user % space'
        unusual.mkdir()
        with patch.object(b.sys, 'platform', 'linux'), patch.object(b.Path, 'home', return_value=self.root), \
             patch.object(b, 'run') as run, patch.dict(os.environ, {'PYTHONPATH': str(unusual)}):
            b.install_service(unusual, ['/a path/python', '-m', 'controller'])
        unit = (self.root / '.config/systemd/user/agent-hub-controller.service').read_text()
        self.assertIn('ExecStart="/a path/python" "-m" "controller"', unit)
        self.assertIn('a user %% space', unit)
        self.assertEqual(run.call_args.args[0][:3], ['systemctl', '--user', 'restart'])
        self.assertNotIn('sudo', unit)

    def test_no_second_login_for_existing_host(self):
        from agent_hub_host import api, auth, config, fleet, provisioning
        install_root = self.root / '.local/share/agent-hub'
        package = install_root / 'package/host/agent_hub_host'
        package.mkdir(parents=True)
        for name in ('cli.py', 'host_supervisor.py'):
            (package / name).write_text('')
        python = install_root / 'venv/bin/python'
        python.parent.mkdir(parents=True)
        python.touch()
        settings = self.root / 'subscriptions.json'
        settings.write_text('{}')
        with patch.object(b, 'root', return_value=install_root), \
             patch.object(b.Path, 'home', return_value=self.root), \
             patch.object(b.sys, 'executable', str(python)), \
             patch.object(config, 'load_host_config', return_value={'access_token': 'synthetic', 'base_url': 'https://hub.test', 'machine_id': 'existing'}), \
             patch.object(config, 'get_host_config_path', return_value=self.root / 'host.json'), \
             patch.object(auth, 'device_login') as login, patch.object(api, 'register_machine') as register, \
             patch.object(fleet, 'subscriptions_path', return_value=settings), patch.object(fleet, 'load_settings'), \
             patch.object(b, 'publish_user_path'), \
             patch.object(b, 'configure_default_engine'), \
             patch.object(provisioning, 'sync'), patch.object(b, 'install_service', return_value='systemd_user'), patch.dict(os.environ):
            b.configure('https://hub.test')
        login.assert_not_called()
        register.assert_called_once_with('https://hub.test')
        self.assertEqual(settings.read_text(), '{}')

    def test_cross_hub_reuse_refused_before_effects(self):
        from agent_hub_host import config, auth
        with patch.object(b, 'root', return_value=self.root), patch.object(b, 'validate_persistent_install'), \
             patch.object(config, 'load_host_config', return_value={'base_url': 'https://other.test'}), \
             patch.object(auth, 'device_login') as login, patch.dict(os.environ):
            with self.assertRaises(ValueError):
                b.configure('https://hub.test')
        login.assert_not_called()

    def test_ambient_human_config_is_not_used_for_host_enrollment(self):
        from agent_hub_host import config, auth
        host = self.root / '.config/agent-hub/host.json'
        def load():
            self.assertEqual(os.environ['HUB_CONFIG_FILE'], str(host))
            return {'base_url': 'https://another.test'}
        with patch.object(b, 'validate_persistent_install'), \
             patch.object(b.Path, 'home', return_value=self.root), \
             patch.object(config, 'load_host_config', side_effect=load), \
             patch.dict(os.environ, {'HUB_CONFIG_FILE': str(self.root / 'human.json'),
                                     'XDG_CONFIG_HOME': str(self.root / '.config')}), \
             patch.object(auth, 'device_login') as login:
            with self.assertRaises(ValueError):
                b.configure('https://hub.test')
        login.assert_not_called()

    def test_temp_package_cannot_replace_live_wrapper_when_home_not_isolated(self):
        # Reproduces the 02:59 incident: a test substitutes a temp install
        # root while the wrapper destination still resolves to real HOME.
        # Guard refusal must happen before even loading credentials.
        from agent_hub_host import config
        fake_home = self.root / 'fake-home'
        wrapper = fake_home / '.local/bin/hub'
        wrapper.parent.mkdir(parents=True)
        wrapper.write_bytes(b'existing working launcher')
        with patch.object(b.Path, 'home', return_value=fake_home), \
             patch.object(b, 'root', return_value=self.root / 'staging'), \
             patch.object(config, 'load_host_config') as load:
            with self.assertRaisesRegex(ValueError, 'persistent per-user'):
                b.configure('https://hub.test')
        load.assert_not_called()
        self.assertEqual(wrapper.read_bytes(), b'existing working launcher')

    def test_persistent_install_refuses_ambient_python_before_wrapper_write(self):
        fake_home = self.root / 'fake-home'
        install_root = fake_home / '.local/share/agent-hub'
        with patch.object(b.Path, 'home', return_value=fake_home):
            with self.assertRaisesRegex(ValueError, 'temporary or ambient Python'):
                b.validate_persistent_install(install_root)

    def test_runtime_path_persists_all_engines_and_quse_idempotently(self):
        install_root = self.root / '.local/share/agent-hub'
        with patch.object(b.sys, 'platform', 'linux'), \
             patch.object(b.Path, 'home', return_value=self.root), \
             patch.object(b, 'architecture', return_value='x64'), patch.dict(os.environ):
            b.publish_user_path(install_root)
            first = (self.root / '.profile').read_text()
            b.publish_user_path(install_root)
            self.assertEqual(first, (self.root / '.profile').read_text())
            script = (install_root / 'path.sh').read_text()
            for part in ('engines/bin', 'venv/bin', 'node-v' + b.NODE_VERSION, '.local/bin'):
                self.assertIn(part, script)

    def test_wrapper_publish_does_not_follow_existing_symlink(self):
        target, wrapper = self.root / 'other', self.root / 'hub'
        target.write_text('preserved')
        wrapper.symlink_to(target)
        b.write_private(wrapper, 'new wrapper')
        self.assertEqual(target.read_text(), 'preserved')
        self.assertFalse(wrapper.is_symlink())
        self.assertEqual(wrapper.read_text(), 'new wrapper')

    def test_default_opus_preserves_existing_profiles_and_claude_settings(self):
        import json
        import tomllib
        native = self.root / '.config/aplexer/config.toml'
        native.parent.mkdir(parents=True)
        native.write_text('version = 1\ndefault_engine = "codex"\n[profiles.personal]\nengine = "codex"\n')
        settings = self.root / '.claude/settings.json'
        settings.parent.mkdir()
        settings.write_text('{"hooks": {"Stop": []}, "model": "sonnet"}')
        with patch.object(b.sys, 'platform', 'linux'), \
             patch.object(b.Path, 'home', return_value=self.root), \
             patch.dict(os.environ, {'XDG_CONFIG_HOME': str(self.root / '.config')}):
            b.configure_default_engine()
            b.configure_default_engine()
        config = tomllib.loads(native.read_text())
        self.assertEqual(config['default_engine'], 'claude')
        self.assertEqual(config['profiles']['personal']['engine'], 'codex')
        self.assertEqual(json.loads(settings.read_text()), {'model': 'opus', 'hooks': {'Stop': []}})

    def test_package_matches_supervisor_workspace_import_contract(self):
        install_root = self.root / 'install'
        archive = self.root / 'hub.pyz'
        with zipfile.ZipFile(archive, 'w') as z:
            z.writestr('agent_hub_host/cli.py', 'content')
        with patch.object(b, 'run'), patch.object(b.sys, 'platform', 'linux'):
            python, package = b.install_package(archive, install_root, self.root / 'uv')
            with patch.object(b.sys, 'executable', str(python)):
                command = b.service_command(install_root, self.root / 'settings')
        workspace = Path(command[command.index('--workspace') + 1])
        self.assertEqual(workspace / 'host', package)
        self.assertTrue((package / 'agent_hub_host/cli.py').is_file())


if __name__ == '__main__':
    unittest.main()
