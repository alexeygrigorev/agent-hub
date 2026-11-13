"""Offline regression for the two-stage Hub2 host namespace transition."""
import base64
import contextlib
import csv
import hashlib
import importlib.util
import io
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[2]
SCRATCH = REPO / '.local/tmp'
HOST_VERSION = 'c' * 40
PROTOCOL_VERSION = 1
HOST_WHEEL = f'agent_hub_host_v2-2.0.0+g{HOST_VERSION}-py3-none-any.whl'


def load_updater():
    spec = importlib.util.spec_from_file_location(
        'hub2_self_update_namespace_transition', REPO / 'scripts/hub2-self-update.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


updater = load_updater()


def write_wheel(path, *, filename, distribution, version, files):
    """Build a tiny standards-compliant pure-Python wheel without build tools."""
    path.mkdir(parents=True, exist_ok=True)
    dist_info = f'{re.sub(r"[-.]+", "_", distribution)}-{version}.dist-info'
    entries = dict(files)
    entries[f'{dist_info}/METADATA'] = (
        f'Metadata-Version: 2.1\nName: {distribution}\nVersion: {version}\n\n'
    ).encode()
    entries[f'{dist_info}/WHEEL'] = (
        'Wheel-Version: 1.0\nGenerator: namespace-transition-test\n'
        'Root-Is-Purelib: true\nTag: py3-none-any\n'
    ).encode()
    record_path = f'{dist_info}/RECORD'
    record = io.StringIO(newline='')
    writer = csv.writer(record)
    for name, data in sorted(entries.items()):
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b'=').decode()
        writer.writerow((name, 'sha256=' + digest, len(data)))
    writer.writerow((record_path, '', ''))
    entries[record_path] = record.getvalue().encode()
    wheel = path / filename
    with zipfile.ZipFile(wheel, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(entries.items()):
            archive.writestr(name, data)
    return wheel


def namespace_files(namespace, *, host_version=HOST_VERSION,
                    protocol=PROTOCOL_VERSION, native_sessions=True,
                    include_client=True, invalid_release=False):
    if invalid_release:
        release = 'this is not valid Python\n'
    else:
        release = f'HOST_VERSION = {host_version!r}\nPROTOCOL_VERSION = {protocol!r}\n'
    files = {
        f'{namespace}/__init__.py': b'',
        f'{namespace}/_release.py': release.encode(),
        f'{namespace}/cli.py': b'def main():\n    return 0\n',
    }
    if include_client:
        heartbeat = ('labels, engines=None, capabilities=None, native_sessions=None'
                     if native_sessions else 'labels, engines=None, capabilities=None')
        files[f'{namespace}/client.py'] = (
            f'class Client:\n    def heartbeat(self, {heartbeat}):\n        return None\n'
        ).encode()
    return files


def write_host_wheel(path, namespace, *, host_version=HOST_VERSION,
                     protocol=PROTOCOL_VERSION, native_sessions=True,
                     include_client=True, invalid_release=False,
                     distribution='agent-hub-host-v2', filename=HOST_WHEEL):
    version = f'2.0.0+g{HOST_VERSION}'
    files = namespace_files(namespace, host_version=host_version, protocol=protocol,
                            native_sessions=native_sessions, include_client=include_client,
                            invalid_release=invalid_release)
    files[f'{distribution.replace("-", "_")}-{version}.dist-info/entry_points.txt'] = (
        f'[console_scripts]\nhub2 = {namespace}.cli:main\n'
    ).encode()
    return write_wheel(path, filename=filename, distribution=distribution,
                       version=version, files=files)


def artifact_record(path):
    data = path.read_bytes()
    return {'sha256': hashlib.sha256(data).hexdigest(), 'size': len(data)}


def site_packages(target):
    return target / 'lib' / f'python{sys.version_info.major}.{sys.version_info.minor}' / 'site-packages'


def unpack_wheel(wheel, target, *, replace_namespace=None, replace_distribution=None):
    """Install a local fixture wheel without pip or any dependency resolution."""
    site = site_packages(target)
    site.mkdir(parents=True, exist_ok=True)
    if replace_namespace:
        shutil.rmtree(site / replace_namespace, ignore_errors=True)
    if replace_distribution:
        dist = re.sub(r'[-.]+', '_', replace_distribution)
        for installed in site.glob(dist + '-*.dist-info'):
            shutil.rmtree(installed)
    with zipfile.ZipFile(wheel) as archive:
        for entry in archive.infolist():
            relative = Path(entry.filename)
            if relative.is_absolute() or '..' in relative.parts:
                raise AssertionError('fixture wheel contains an unsafe path')
            destination = site / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            if not entry.is_dir():
                destination.write_bytes(archive.read(entry))
            if entry.filename.endswith('.dist-info/entry_points.txt'):
                entry_points = archive.read(entry).decode()
                for line in entry_points.splitlines():
                    if line.startswith('hub2 = '):
                        module, function = line.removeprefix('hub2 = ').split(':', 1)
                        script = target / 'bin/hub2'
                        script.write_text(
                            f'#!{target / "bin/python"}\nfrom {module} import {function} as _main\n'
                            'raise SystemExit(_main())\n')
                        script.chmod(0o755)


def write_operator_zipapp(path):
    with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('__main__.py', 'raise SystemExit(0)\n')


@contextlib.contextmanager
def working_directory(path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


class NamespaceTransitionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        cls.temporary = tempfile.TemporaryDirectory(prefix='hub2-namespace-', dir=SCRATCH)
        cls.root = Path(cls.temporary.name)
        cls.home = cls.root / 'home'
        cls.tmp = cls.root / 'tmp'
        cls.home.mkdir()
        cls.tmp.mkdir()
        cls.environment = patch.dict(os.environ, {
            'HOME': str(cls.home),
            'TMPDIR': str(cls.tmp),
            'PYTHONPATH': '',
        })
        cls.environment.start()

    @classmethod
    def tearDownClass(cls):
        cls.environment.stop()
        cls.temporary.cleanup()

    def candidate(self, name, namespace):
        case = self.root / name
        download = case / 'download'
        download.mkdir(parents=True)
        host = write_host_wheel(download, namespace)
        websockets = write_wheel(
            download, filename='websockets-16.0-py3-none-any.whl',
            distribution='websockets', version='16.0', files={})
        operator = download / 'hub.pyz'
        write_operator_zipapp(operator)
        (download / 'hub2-enroll.py').write_text('# fixture\n')
        target = case / 'releases' / HOST_VERSION
        manifest = {
            'host_version': HOST_VERSION,
            'protocol_version': PROTOCOL_VERSION,
            'artifacts': {'hub.pyz': artifact_record(operator)},
        }
        return target, download, host, websockets, manifest

    def prepare(self, target, download, host):
        target.parent.mkdir(parents=True, exist_ok=True)
        events = []
        real_command = updater.command

        def record_command(args):
            normalized = [str(value) for value in args]
            if (normalized[0] == str(target / 'bin/python')
                    and normalized[1:3] == ['-I', '-c']):
                events.append('namespace-check')
            elif Path(normalized[0]).name == 'hub2':
                events.append('daemon-help')
            if normalized[1:3] == ['-m', 'venv']:
                real_command([args[0], '-m', 'venv', '--without-pip', args[3]])
                return
            if normalized[1:4] == ['-m', 'pip', 'install']:
                self.assertEqual(normalized[4:7], ['--no-index', '--no-deps',
                                                   str(download / 'websockets-16.0-py3-none-any.whl')])
                for wheel in args[6:]:
                    unpack_wheel(Path(wheel), target)
                return
            real_command(args)

        with patch.object(updater, 'command', record_command), \
             patch.object(updater.shutil, 'which', return_value=None):
            updater.prepare_environment(target, download, {
                'host_version': HOST_VERSION,
                'protocol_version': PROTOCOL_VERSION,
                'artifacts': {'hub.pyz': artifact_record(download / 'hub.pyz')},
            }, host.name)
        self.assertLess(events.index('namespace-check'), events.index('daemon-help'))
        return target

    def install_fixture(self, target, wheel):
        unpack_wheel(wheel, target, replace_namespace='hub2_agent_hub_host',
                     replace_distribution='agent-hub-host-v2')

    def test_matching_new_protocol_passes_in_both_namespaces(self):
        for namespace in ('agent_hub_host', 'hub2_agent_hub_host'):
            with self.subTest(namespace=namespace):
                target, download, wheel, _, manifest = self.candidate(
                    'protocol-two-' + namespace, namespace)
                self.prepare(target, download, wheel)
                protocol_two = write_host_wheel(
                    download / 'protocol-two', namespace, protocol=2)
                unpack_wheel(protocol_two, target, replace_namespace=namespace,
                             replace_distribution='agent-hub-host-v2')
                manifest['protocol_version'] = 2
                updater.check_host_package(target, manifest)
                manifest['protocol_version'] = 1
                with self.assertRaises(RuntimeError):
                    updater.check_host_package(target, manifest)

    def test_old_and_new_wheels_pass_before_updater_replacement(self):
        old_target, old_download, old_wheel, _, old_manifest = self.candidate(
            'old-candidate', 'agent_hub_host')
        new_target, new_download, new_wheel, _, new_manifest = self.candidate(
            'new-candidate', 'hub2_agent_hub_host')
        host_root = self.root / 'host'
        host_root.mkdir()
        updater_path = host_root / 'updater.py'
        updater_path.write_bytes(b'pre-bridge updater remains active\n')

        old_target = self.prepare(old_target, old_download, old_wheel)
        new_target = self.prepare(new_target, new_download, new_wheel)
        self.assertEqual(updater_path.read_bytes(), b'pre-bridge updater remains active\n')
        updater.check_host_package(old_target, old_manifest)
        updater.check_host_package(new_target, new_manifest)

        # This is the exact import contract from the preserved 6620027c updater.
        legacy_check = (
            'from agent_hub_host._release import HOST_VERSION, PROTOCOL_VERSION; '
            f'assert HOST_VERSION == {HOST_VERSION!r} and PROTOCOL_VERSION == {PROTOCOL_VERSION}; '
            'import inspect; from agent_hub_host.client import Client; '
            "assert 'native_sessions' in inspect.signature(Client.heartbeat).parameters"
        )
        with working_directory(self.root), patch.dict(os.environ, {'PYTHONPATH': ''}):
            updater.command([old_target / 'bin/python', '-c', legacy_check])
            with self.assertRaises(RuntimeError):
                updater.command([new_target / 'bin/python', '-c', legacy_check])

        # A checkout/PYTHONPATH package cannot shadow a valid new candidate.
        shadow = self.root / 'shadow'
        (shadow / 'agent_hub_host').mkdir(parents=True)
        for name, data in namespace_files('agent_hub_host').items():
            destination = shadow / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
        with working_directory(shadow), patch.dict(os.environ, {'PYTHONPATH': str(shadow)}):
            updater.check_host_package(new_target, new_manifest)

        # Nor can a matching import tree outside the venv make an empty venv pass.
        empty_target = self.root / 'empty-candidate'
        subprocess.run([sys.executable, '-m', 'venv', '--without-pip', str(empty_target)], check=True,
                       cwd=self.root, env=os.environ.copy(), stdout=subprocess.DEVNULL,
                       stderr=subprocess.PIPE, timeout=120)
        with working_directory(shadow), patch.dict(os.environ, {'PYTHONPATH': str(shadow)}):
            with self.assertRaises(RuntimeError):
                updater.check_host_package(empty_target, new_manifest)

        # Each malformed candidate wheel is installed into the real venv and imported by the checker.
        negative_cases = (
            {'host_version': 'd' * 40},
            {'protocol': PROTOCOL_VERSION + 1},
            {'native_sessions': False},
            {'include_client': False},
            {'invalid_release': True},
        )
        for index, options in enumerate(negative_cases):
            with self.subTest(candidate=options):
                invalid_wheel = write_host_wheel(
                    self.root / f'invalid-{index}', 'hub2_agent_hub_host', **options)
                self.install_fixture(new_target, invalid_wheel)
                with self.assertRaises(RuntimeError):
                    updater.check_host_package(new_target, new_manifest)

        # Install a second supported namespace beside a valid new one: ambiguity fails closed.
        self.install_fixture(new_target, new_wheel)
        legacy_extra = write_host_wheel(
            self.root / 'ambiguous', 'agent_hub_host',
            distribution='legacy-namespace-fixture',
            filename=f'legacy_namespace_fixture-2.0.0+g{HOST_VERSION}-py3-none-any.whl')
        unpack_wheel(legacy_extra, new_target)
        with self.assertRaises(RuntimeError):
            updater.check_host_package(new_target, new_manifest)


if __name__ == '__main__':
    unittest.main()
