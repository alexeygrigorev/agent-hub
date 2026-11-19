"""Synthetic normal releases and owned interpreter subprocesses, offline.

The fake publisher/transport are NOT CI provenance or live Host qualification.
The real maintained bundle verifier runs against this synthetic fixture.
"""
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import warnings
from unittest.mock import patch
import zipfile

from agent_hub_host import quota_dispatch_entry as entry
from agent_hub_host import alert_authority as ram

ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location('natural_archive_builder', ROOT / 'scripts/build-quota-dispatcher.py')
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def wheel_bytes(contents):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        for name, raw in sorted(contents.items()):
            archive.writestr(name, raw)
    return buffer.getvalue()


class Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='qna-')
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.repo = self.directory / 'synthetic-publisher'
        self.repo.mkdir()
        self.release = self.directory / 'release'
        self.release.mkdir()
        self.transport = wheel_bytes({
            'websockets/__init__.py': b'__version__="16.0"\n',
            'websockets/exceptions.py': b'class WebSocketException(Exception):pass\nclass InvalidStatus(Exception):pass\n',
            'websockets/sync/__init__.py': b'',
            'websockets/sync/client.py': b'def connect(*a,**k):raise AssertionError("fake transport must not connect")\n',
        })
        (self.repo / 'scripts').mkdir()
        self.payloads = {}
        for name in builder.NATURAL_PAYLOADS:
            raw = (ROOT / 'scripts' / name).read_bytes()
            (self.repo / 'scripts' / name).write_bytes(raw)
            self.payloads[name] = raw
        self.make_release()

    def git(self, *args):
        result = subprocess.run(['git', '-C', str(self.repo), *args],
            check=True, capture_output=True, timeout=10)
        return result.stdout.decode().strip()

    def make_release(self, *, bad_lock=False):
        lock = ('version=1\n[[package]]\nname="websockets"\nversion="16.0"\n'
            'wheels=[{url="https://synthetic.invalid/websockets-16.0-py3-none-any.whl",'
            f'hash="sha256:{"0" * 64 if bad_lock else digest(self.transport)}",size={len(self.transport)}}}]\n')
        (self.repo / 'uv.lock').write_text(lock)
        if not (self.repo / '.git').exists():
            self.git('init', '-q')
        self.git('add', 'scripts', 'uv.lock')
        self.git('-c', 'user.name=Synthetic fixture', '-c', 'user.email=fixture@synthetic.invalid',
                 '-c', 'commit.gpgsign=false', 'commit', '-q', '-m',
                 'Synthetic release fixture\n\nScope: in')
        self.commit = self.git('rev-parse', 'HEAD')
        host = {'agent_hub_host/' + path.name: path.read_bytes()
                for path in (ROOT / 'hub2/host/agent_hub_host').glob('*.py')}
        host['agent_hub_host/_release.py'] = f'HOST_VERSION = {self.commit!r}\nPROTOCOL_VERSION = 1\n'.encode()
        host_name = f'agent_hub_host_v2-2.0.0+g{self.commit}-py3-none-any.whl'
        self.host_name = host_name
        self.host = host
        cli = wheel_bytes({'agent_hub_host/_build_info.py': f'BUILD_COMMIT = "{self.commit}"\n'.encode()})
        data = {name: b'synthetic artifact\n' for name in (
            'host-install.sh', 'hub2-self-update.py', 'hub2-enroll.py', 'install.sh', 'install.ps1',
            'agent-hub2-host.service', 'agent-hub2-host-update.service', 'agent-hub2-host-update.timer')}
        data.update({host_name: wheel_bytes(host), 'websockets-16.0-py3-none-any.whl': self.transport,
            'hub.pyz': cli, 'hub.pyz.sha256': f'{digest(cli)}  hub.pyz\n'.encode(),
            'manifest.json': json.dumps({'built_from_commit': self.commit, 'sha256': digest(cli)}).encode()})
        # A second fixture revision has a different wheel basename; remove only
        # this test's prior fixture artifact, never a shared release.
        for path in self.release.glob('agent_hub_host_v2-*.whl'):
            path.unlink()
        for name, raw in data.items():
            (self.release / name).write_bytes(raw)
        manifest = {'schema_version': 1, 'host_version': self.commit, 'protocol_version': 1,
            'python_requires': '>=3.11', 'artifacts': {name: {'size': len(raw), 'sha256': digest(raw)}
                                                   for name, raw in data.items()}}
        raw = json.dumps(manifest).encode()
        (self.release / 'host-release.json').write_bytes(raw)
        self.manifest_sha = digest(raw)

    def build(self, output=None):
        output = output or self.directory / 'host.pyz'
        with patch.object(builder, 'REPO_ROOT', self.repo):
            result = builder.build_host_archive(output, self.release, self.commit, self.manifest_sha)
        return output, result

    @unittest.skipUnless(os.name == 'posix' and os.getuid() > 0, 'unprivileged Host entry')
    def test_real_verifier_and_full_archive_version_are_deterministic_and_same_commit(self):
        # Dirty payloads must not leak into a named release archive.
        (self.repo / 'scripts/hub-quota-publish.py').write_bytes(b'wrong dirty payload\n')
        first, result = self.build()
        second, result2 = self.build(self.directory / 'second.pyz')
        self.assertEqual(first.read_bytes(), second.read_bytes())
        self.assertEqual(result['sha256'], result2['sha256'])
        with zipfile.ZipFile(first) as archive:
            for name, raw in self.host.items():
                self.assertEqual(archive.read(name), raw)
            for name, raw in self.payloads.items():
                self.assertEqual(archive.read('natural/' + name), raw)
            self.assertEqual(archive.read('websockets/sync/__init__.py'), b'')
            self.assertEqual(json.loads(archive.read('MANIFEST.json'))['host_release']['commit'], self.commit)
        process = subprocess.run([sys.executable, '-I', str(first), 'natural-host',
            '--config', str(self.directory / 'absent.json'), 'version'],
            capture_output=True, text=True, timeout=10)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(json.loads(process.stdout)['host_version'], self.commit)

    def test_changed_normal_release_artifact_is_refused_without_output(self):
        (self.release / self.host_name).write_bytes(b'changed wheel')
        with self.assertRaises(ValueError):
            self.build()
        self.assertFalse((self.directory / 'host.pyz').exists())

    def test_valid_manifest_cannot_override_committed_dependency_lock(self):
        self.make_release(bad_lock=True)
        with self.assertRaisesRegex(ValueError, 'committed lock'):
            self.build()
        self.assertFalse((self.directory / 'host.pyz').exists())

    def test_bytes_changed_after_verification_are_rechecked_before_zip_read(self):
        original = builder._release_blob
        def changed(directory, name, record):
            (directory / name).write_bytes(b'changed after verify')
            return original(directory, name, record)
        with patch.object(builder, '_release_blob', side_effect=changed), self.assertRaisesRegex(ValueError, 'bytes changed'):
            self.build()

    def test_ambiguous_and_unsafe_wheel_members_are_refused(self):
        for name in ('../escape.py', 'websockets/../escape.py', 'websockets/line\nbreak.py'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                builder._wheel_python(wheel_bytes({'websockets/__init__.py': b'', name: b'pass'}), 'websockets')
        buffer = io.BytesIO()
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', UserWarning)
            with zipfile.ZipFile(buffer, 'w') as archive:
                archive.writestr('websockets/__init__.py', b'')
                archive.writestr('websockets/__init__.py', b'pass')
        with self.assertRaises(ValueError):
            builder._wheel_python(buffer.getvalue(), 'websockets')

    def test_root_entry_is_refused_before_host_main(self):
        with patch.object(entry.os, 'getuid', return_value=0), \
             patch('agent_hub_host.cli.main') as host:
            with self.assertRaises(ram.Unknown):
                entry.main(['natural-host', 'version'])
            host.assert_not_called()

    @unittest.skipUnless(sys.platform == 'linux' and os.getuid() > 0, 'owned Linux FD interpreter')
    def test_owned_fd_exec_uses_verified_canonical_interpreter_for_closed_fd_child(self):
        archive, _ = self.build()
        executable = str(Path('/proc/self/exe').resolve())
        fd = os.open(executable, os.O_RDONLY)
        try:
            script = '''import hashlib,json,os,subprocess,sys
sys.path.insert(0,sys.argv[1])
from agent_hub_host import quota_dispatch_manager as m,quota_natural_startup as s,alert_authority as r
path=sys.argv[2];fd=int(sys.argv[3]);directory=os.open(os.path.dirname(path),os.O_RDONLY|os.O_DIRECTORY)
info=os.fstat(fd);raw=os.pread(fd,info.st_size+1,0)
held=m.HeldExecutableFD(fd,directory,os.path.basename(path),raw,m._natural_stamp(info),r.SourcePin(info.st_dev,info.st_ino,hashlib.sha256(raw).hexdigest()))
before=sys.executable;sys.executable=s._child_interpreter(held,path)
child=subprocess.run([sys.executable,'-I','-c','print("owned-child")'],close_fds=True,capture_output=True,text=True,timeout=5)
print(json.dumps({'before':before,'canonical':sys.executable,'child_rc':child.returncode,'child':child.stdout.strip()}))
os.close(directory)
'''
            process = subprocess.run([f'/proc/self/fd/{fd}', '-I', '-c', script,
                str(archive), executable, str(fd)], pass_fds=(fd,),
                capture_output=True, text=True, timeout=10)
        finally:
            os.close(fd)
        self.assertEqual(process.returncode, 0, process.stderr)
        result = json.loads(process.stdout)
        self.assertEqual((result['canonical'], result['child_rc'], result['child']), (executable, 0, 'owned-child'))


if __name__ == '__main__':
    unittest.main()
