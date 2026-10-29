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
        self.assertEqual(argv[-len(b.ENGINES):], list(b.ENGINES.values()))
        self.assertEqual(env['npm_config_prefix'], str(self.root / 'engines'))

    def prepare_aplexer_alias(self):
        binary_dir = self.root / 'bin'
        binary_dir.mkdir()
        binary = binary_dir / 'aplexer'
        binary.write_bytes(b'current runtime')
        binary.chmod(0o755)
        (binary_dir / 'aplexer-version').write_text(b.APLEXER_VERSION)
        alias = binary_dir / 'a'
        alias.write_bytes(b'previous runtime')
        return binary, alias

    def test_aplexer_repair_updates_stale_alias_without_downloading(self):
        binary, alias = self.prepare_aplexer_alias()
        with patch.object(b.sys, 'platform', 'linux'), \
             patch.object(b, 'architecture', return_value='x64'), \
             patch.object(b, 'download') as download:
            b.ensure_aplexer(self.root)
        download.assert_not_called()
        self.assertEqual(alias.read_bytes(), binary.read_bytes())
        self.assertTrue(alias.stat().st_mode & 0o111)

    def test_aplexer_repair_preserves_identical_alias(self):
        binary, alias = self.prepare_aplexer_alias()
        alias.write_bytes(binary.read_bytes())
        with patch.object(b.sys, 'platform', 'linux'), \
             patch.object(b, 'architecture', return_value='x64'), \
             patch.object(b.shutil, 'copy2') as copy:
            b.ensure_aplexer(self.root)
        copy.assert_not_called()

    def test_failed_aplexer_alias_copy_preserves_previous_executable(self):
        _, alias = self.prepare_aplexer_alias()
        previous = alias.read_bytes()
        def interrupted(source, target):
            Path(target).write_bytes(b'partial')
            raise OSError('interrupted copy')
        with patch.object(b.sys, 'platform', 'linux'), \
             patch.object(b, 'architecture', return_value='x64'), \
             patch.object(b.shutil, 'copy2', side_effect=interrupted):
            with self.assertRaises(OSError):
                b.ensure_aplexer(self.root)
        self.assertEqual(alias.read_bytes(), previous)
        self.assertEqual(sorted(p.name for p in alias.parent.iterdir()),
                         ['a', 'aplexer', 'aplexer-version'])

    def test_aplexer_upgrade_resumes_after_interrupted_alias_publication(self):
        binary, alias = self.prepare_aplexer_alias()
        binary.write_bytes(alias.read_bytes())
        (alias.parent / 'aplexer-version').write_text('previous version')
        (self.root / 'tmp').mkdir()
        previous = alias.read_bytes()
        def download(url, target):
            Path(target).write_bytes(b'upgraded runtime')
        def interrupted(source, target):
            Path(target).write_bytes(b'partial')
            raise OSError('interrupted copy')
        with patch.object(b.sys, 'platform', 'linux'), \
             patch.object(b, 'architecture', return_value='x64'), \
             patch.object(b, 'download', side_effect=download), \
             patch.object(b, 'verify'), patch.object(b, 'run'), \
             patch.object(b.shutil, 'copy2', side_effect=interrupted):
            with self.assertRaises(OSError):
                b.ensure_aplexer(self.root)
        self.assertEqual(alias.read_bytes(), previous)
        self.assertEqual(binary.read_bytes(), b'upgraded runtime')
        with patch.object(b.sys, 'platform', 'linux'), \
             patch.object(b, 'architecture', return_value='x64'), \
             patch.object(b, 'download') as fetch:
            b.ensure_aplexer(self.root)
        fetch.assert_not_called()
        self.assertEqual(alias.read_bytes(), b'upgraded runtime')

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

    def test_node_recreates_npm_and_npx_launchers_after_skipping_archive_links(self):
        node_root = self.root / 'runtimes' / ('node-v' + b.NODE_VERSION + '-linux-x64')
        node = node_root / 'bin/node'
        npm = node_root / 'lib/node_modules/npm/bin/npm-cli.js'
        node.parent.mkdir(parents=True)
        npm.parent.mkdir(parents=True)
        node.touch()
        npm.touch()
        with patch.object(b.sys, 'platform', 'linux'), \
             patch.object(b, 'architecture', return_value='x64'), \
             patch.object(b, 'run'), patch.object(b, 'download') as download:
            b.ensure_node(self.root)
        download.assert_not_called()
        for name in ('npm', 'npx'):
            launcher = node.parent / name
            self.assertIn(str(node), launcher.read_text())
            self.assertIn(name + '-cli.js', launcher.read_text())
            self.assertTrue(launcher.stat().st_mode & 0o111)

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
        base = install_root / 'python/cpython-3.12'
        base.mkdir(parents=True)
        settings = self.root / 'subscriptions.json'
        settings.write_text('{}')
        with patch.object(b, 'root', return_value=install_root), \
             patch.object(b.Path, 'home', return_value=self.root), \
             patch.object(b.sys, 'executable', str(python)), \
             patch.object(b.sys, 'base_prefix', str(base)), \
             patch.object(b, 'validate_side_effect_context'), \
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
             patch.object(b, 'validate_side_effect_context'), \
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
             patch.object(b, 'validate_side_effect_context'), \
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

    def test_durable_venv_with_temporary_base_cannot_replace_wrapper(self):
        from agent_hub_host import config
        home = self.root / 'home'
        install_root = home / '.local/share/agent-hub'
        python = install_root / 'venv/bin/python'
        python.parent.mkdir(parents=True)
        python.touch()
        package = install_root / 'package/host/agent_hub_host'
        package.mkdir(parents=True)
        for name in ['cli.py', 'host_supervisor.py']:
            (package / name).touch()
        wrapper = home / '.local/bin/hub'
        wrapper.parent.mkdir()
        wrapper.write_text('existing working launcher')
        temporary_base = self.root / 'temporary-python'
        temporary_base.mkdir()
        with patch.object(b, 'root', return_value=install_root), \
             patch.object(b.Path, 'home', return_value=home), \
             patch.object(b.sys, 'platform', 'linux'), \
             patch.object(b.sys, 'executable', str(python)), \
             patch.object(b.sys, 'base_prefix', str(temporary_base)), \
             patch.object(config, 'load_host_config') as load:
            with self.assertRaisesRegex(ValueError, 'outside the persistent managed runtime'):
                b.configure('https://hub.test')
        load.assert_not_called()
        self.assertEqual(wrapper.read_text(), 'existing working launcher')

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

    def test_service_context_refuses_overridden_home_before_os_effects(self):
        with patch.object(b.Path, 'home', return_value=self.root / 'isolated-home'), \
             patch.object(b.sys, 'platform', 'linux'), patch.object(b, 'run') as run:
            with self.assertRaisesRegex(ValueError, 'overridden user profile'):
                b.validate_side_effect_context(self.root / 'isolated-home/.local/share/agent-hub')
        run.assert_not_called()

    def test_failed_package_extraction_preserves_installed_package(self):
        package = self.root / 'package/host/agent_hub_host'
        package.mkdir(parents=True)
        (package / 'cli.py').write_text('previous complete package')
        def fail(archive, destination):
            target = destination / 'agent_hub_host'
            target.mkdir()
            (target / 'cli.py').write_text('incomplete replacement')
            raise OSError('disk write failed')
        with patch.object(b, 'run'), patch.object(b, 'unpack', side_effect=fail):
            with self.assertRaisesRegex(OSError, 'disk write failed'):
                b.install_package(self.root / 'archive', self.root, self.root / 'uv')
        self.assertEqual((package / 'cli.py').read_text(), 'previous complete package')
        self.assertEqual(list(package.parent.parent.glob('.package-*')), [])

    def test_failed_package_publication_restores_previous_tree(self):
        package = self.root / 'package/host/agent_hub_host'
        package.mkdir(parents=True)
        (package / 'cli.py').write_text('previous complete package')
        archive = self.root / 'archive.zip'
        with zipfile.ZipFile(archive, 'w') as z:
            z.writestr('agent_hub_host/cli.py', 'replacement')
        replace = os.replace
        def publish(source, destination):
            if Path(source).name == 'host' and Path(source).parent.name.startswith('.package-'):
                raise OSError('publication failed')
            return replace(source, destination)
        with patch.object(b, 'run'), patch.object(b.os, 'replace', side_effect=publish):
            with self.assertRaisesRegex(OSError, 'publication failed'):
                b.install_package(archive, self.root, self.root / 'uv')
        self.assertEqual((package / 'cli.py').read_text(), 'previous complete package')

    def test_windows_split_home_cannot_change_live_appdata_or_registry(self):
        from agent_hub_host import config
        home = self.root / 'home'
        local = self.root / 'other-user/AppData/Local'
        install_root = local / 'Programs/agent-hub'
        with patch.object(b.sys, 'platform', 'win32'), \
             patch.object(b.Path, 'home', return_value=home), \
             patch.object(b, 'root', return_value=install_root), \
             patch.dict(os.environ, {'LOCALAPPDATA': str(local), 'APPDATA': str(home / 'AppData/Roaming')}), \
             patch.object(config, 'load_host_config') as load, patch.object(b, 'run') as run:
            with self.assertRaisesRegex(ValueError, 'split Windows user profile'):
                b.configure('https://hub.test')
        load.assert_not_called()
        run.assert_not_called()

    def test_partial_venv_without_interpreter_is_repaired(self):
        root = self.root / 'install'
        (root / 'venv').mkdir(parents=True)
        archive = self.root / 'archive.zip'
        with zipfile.ZipFile(archive, 'w') as z:
            z.writestr('agent_hub_host/cli.py', '')
        with patch.object(b, 'run') as run:
            b.install_package(archive, root, self.root / 'uv')
        self.assertIn('--allow-existing', run.call_args_list[0].args[0])

    def test_macos_repair_reloads_existing_launch_agent(self):
        from types import SimpleNamespace
        with patch.object(b.sys, 'platform', 'darwin'), \
             patch.object(b.Path, 'home', return_value=self.root), \
             patch.object(b.subprocess, 'run', return_value=SimpleNamespace(returncode=0)), \
             patch.object(b, 'run') as run:
            b.install_service(self.root / 'installation', ['/user/python', '-m', 'supervisor'])
        self.assertEqual([c.args[0][1] for c in run.call_args_list], ['bootout', 'bootstrap', 'kickstart'])
        self.assertIn('-k', run.call_args.args[0])

    def test_macos_uses_published_native_release_instead_of_linux_assets(self):
        (self.root / 'bin').mkdir()
        (self.root / 'tmp').mkdir()
        urls = []
        def download(url, target):
            urls.append(url)
            Path(target).write_bytes(b'fixture')
        with patch.object(b.sys, 'platform', 'darwin'), \
             patch.object(b, 'architecture', return_value='arm64'), \
             patch.object(b, 'download', side_effect=download), patch.object(b, 'verify'), \
             patch.object(b, 'run'):
            b.ensure_aplexer(self.root)
        self.assertIn('/v0.1.22/aplexer-macos-arm64', urls[0])
        self.assertEqual((self.root / 'bin/aplexer-version').read_text(), '0.1.22')

    def test_macos_installs_matching_verified_github_wheel_not_missing_pypi_wheel(self):
        (self.root / 'tmp').mkdir()
        archive = self.root / 'hub.pyz'
        with zipfile.ZipFile(archive, 'w') as z:
            z.writestr('agent_hub_host/cli.py', '')
        urls = []
        def download(url, target):
            urls.append(url)
            Path(target).write_bytes(b'fixture')
        with patch.object(b.sys, 'platform', 'darwin'), \
             patch.object(b, 'architecture', return_value='arm64'), \
             patch.object(b, 'download', side_effect=download), patch.object(b, 'verify') as verify, \
             patch.object(b, 'run') as run:
            b.install_package(archive, self.root, self.root / 'uv')
        self.assertIn('aplexer_client-0.1.22-cp311-abi3-macosx_11_0_arm64.whl', urls[0])
        verify.assert_called_once()
        self.assertTrue(str(run.call_args.args[0][-1]).endswith('.whl'))


if __name__ == '__main__':
    unittest.main()
