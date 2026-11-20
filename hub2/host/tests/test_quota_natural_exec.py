"""Owned USER natural-Host startup tests; Root helper/peer/image facts are simulated."""
import array
import json
import os
from pathlib import Path
import socket
import struct
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch
import zipfile

from agent_hub_host import quota_natural_exec as natural_exec
from agent_hub_host import quota_natural_transfer as transfer


COMMIT = 'b' * 40
INVOCATION_ID = '0123456789abcdef0123456789abcdef'


class NaturalExecTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='natural-exec-')
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.uid = os.getuid()
        if self.uid == 0:
            self.skipTest('requires non-root UID')

        self.runtime_root = self.root / 'run' / 'user'
        self.uid_dir = self.runtime_root / str(self.uid)
        self.uid_dir.mkdir(parents=True)
        for p in (self.root, self.root / 'run', self.runtime_root):
            p.chmod(0o755)
        self.uid_dir.chmod(0o700)

        self.helper = self.root / 'hub-alert-natural-donor.py'
        self.helper.write_text('#!/usr/bin/env python3\n', encoding='utf-8')
        self.helper.chmod(0o755)

        self.interp = self.root / 'python3.12'
        self.interp.write_bytes(b'\x7fELF' + b'X' * 4096)
        self.interp.chmod(0o755)

        self.archive = self.root / 'host.pyz'
        with zipfile.ZipFile(self.archive, 'w') as zf:
            zf.writestr('agent_hub_host/_release.py',
                        f'HOST_VERSION = {COMMIT!r}\nPROTOCOL_VERSION = 1\n')
        self.archive.chmod(0o644)

    def _root_stat(self, info):
        values = {k: getattr(info, k) for k in dir(info) if k.startswith('st_')}
        values['st_uid'] = 0
        values['st_mode'] = info.st_mode & ~0o022
        return types.SimpleNamespace(**values)

    def _simulate_donor(self, sock_path, manifest_r, write_fds):
        client = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        dir_fd = os.open(sock_path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            client.connect(f'/proc/self/fd/{dir_fd}/{sock_path.name}')
            raw_req, _, _, _ = client.recvmsg(transfer.MAX_PACKET)
            req = json.loads(raw_req.decode())
            fds = [
                os.open(self.interp, os.O_RDONLY | os.O_CLOEXEC),
                os.open(self.archive, os.O_RDONLY | os.O_CLOEXEC),
                *(os.open('/dev/null', os.O_RDONLY | os.O_CLOEXEC) for _ in range(11)),
                manifest_r,
            ]
            try:
                reply = json.dumps({
                    'v': 1,
                    'kind': 'natural_donation_rights_v1',
                    'nonce': req['nonce'],
                    'target_uid': req['target_uid'],
                    'deadline_ns': req['deadline_ns'],
                    'roles': list(transfer.ROLES),
                }, separators=(',', ':')).encode()
                client.sendmsg([reply], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array('i', fds))])
            finally:
                for fd in fds[:-1]:
                    os.close(fd)
        finally:
            os.close(dir_fd)
            client.close()
            for fd in write_fds:
                try:
                    os.close(fd)
                except OSError:
                    pass

    def test_positive_donation_exec_sanitizes_env_sets_inheritance_and_cleans_endpoint(self):
        manifest_r, manifest_w = os.pipe2(os.O_CLOEXEC)
        self.addCleanup(lambda: os.close(manifest_r) if manifest_r >= 0 else None)
        sock_path = self.uid_dir / natural_exec.RUNTIME_SUBDIR / f'{INVOCATION_ID}-{os.getpid()}.sock'

        donor_thread = None
        seen = {}

        def fake_runner(cmd, **kwargs):
            seen['cmd'] = cmd
            seen['runner_kwargs'] = kwargs
            nonlocal donor_thread
            donor_thread = threading.Thread(
                target=self._simulate_donor,
                args=(sock_path, manifest_r, [manifest_w]),
                daemon=True,
            )
            donor_thread.start()
            return types.SimpleNamespace(returncode=0)

        def fake_exec(fd, argv, env):
            seen['exec_fd'] = fd
            seen['exec_argv'] = argv
            seen['exec_env'] = env
            seen['inheritable_during_exec'] = {
                s: os.get_inheritable(s) for s in seen_slots_from_argv(argv, fd)
            }
            seen['sock_exists_at_exec'] = sock_path.exists()
            raise OSError('simulated_exec_failure')

        def seen_slots_from_argv(argv, fd):
            archive_fd = int(argv[2].rsplit('/', 1)[-1])
            manifest_fd = int(argv[-1])
            return (fd, archive_fd, manifest_fd)

        real_os_open = os.open
        real_os_fstat = os.fstat
        real_os_readlink = os.readlink

        def exec_fstat(fd):
            info = real_os_fstat(fd)
            return self._root_stat(info) if fd != os.open else info

        def bootstrap_fstat(fd):
            return self._root_stat(real_os_fstat(fd))

        os_proxy = types.SimpleNamespace(**vars(os))
        os_proxy.fstat = bootstrap_fstat
        os_proxy.readlink = lambda _p: '/usr/bin/python3.12'

        exec_os_proxy = types.SimpleNamespace(**vars(os))

        def open_for_runtime(path, flags, mode=0o777, *, dir_fd=None):
            return real_os_open(path, flags, mode, dir_fd=dir_fd)

        exec_os_proxy.open = open_for_runtime

        root_parents = {str(p) for p in Path(self.runtime_root).parents} | {str(self.runtime_root), str(self.helper)}

        def runtime_fstat(fd):
            info = real_os_fstat(fd)
            # Keep nat_dir_fd and sock owned by self.uid; root dirs simulated as uid 0
            if fd >= 0:
                try:
                    p = real_os_readlink(f'/proc/self/fd/{fd}')
                except OSError:
                    p = ''
                if p in root_parents:
                    return self._root_stat(info)
            return info

        exec_os_proxy.fstat = runtime_fstat

        orig_getsockopt = socket.socket.getsockopt

        def fake_getsockopt(sock_self, level, optname, buflen=0):
            if level == socket.SOL_SOCKET and optname == socket.SO_PEERCRED:
                return struct.pack('3i', os.getpid(), 0, 0)
            return orig_getsockopt(sock_self, level, optname, buflen) if buflen else orig_getsockopt(sock_self, level, optname)

        env_patch = {
            'INVOCATION_ID': INVOCATION_ID,
            'PYTHONPATH': '/tmp/evil',
            'LD_PRELOAD': '/tmp/evil.so',
            'ENV': '/tmp/evil.sh',
            'BASH_ENV': '/tmp/evil.sh',
            'HUB2_TOKEN': 'ordinary-config-token',
            'XDG_RUNTIME_DIR': str(self.runtime_root),
        }

        with patch.dict(os.environ, env_patch, clear=False), \
             patch.object(natural_exec, 'sys', types.SimpleNamespace(platform='linux')), \
             patch.object(natural_exec, 'host_release', return_value=(COMMIT, 1)), \
             patch.object(natural_exec.startup, '_root_graph', side_effect=lambda paths: ({str(p): real_os_open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC) for p in paths}, ())), \
             patch.object(natural_exec.startup, '_image') as checked_image, \
             patch.object(natural_exec.bootstrap, 'os', os_proxy), \
             patch.object(natural_exec, 'os', exec_os_proxy), \
             patch.object(socket.socket, 'getsockopt', fake_getsockopt):
            with self.assertRaises(OSError):
                natural_exec.maybe_exec_natural_host(
                    ['--config', '/tmp/c.json', 'serve', '--label', 'x'],
                    helper_path=str(self.helper),
                    runtime_root=str(self.runtime_root),
                    runner=fake_runner,
                    exec_fn=fake_exec,
                )

        if donor_thread is not None:
            donor_thread.join(timeout=2)
        os.close(manifest_r)
        manifest_r = -1

        self.assertEqual(checked_image.call_count, 4)  # 2 in validate + 2 in recheck
        self.assertEqual(seen['cmd'][:8], [
            natural_exec.SYSTEMD_RUN_BIN, '--user', '--collect',
            f'--unit={natural_exec.UNIT_PREFIX}-{INVOCATION_ID}-{os.getpid()}',
            '--property=RuntimeMaxSec=5s', '--property=TimeoutStopSec=1s',
            '--property=KillMode=control-group', '--no-block',
        ])
        self.assertEqual(seen['cmd'][8:], [
            '--', natural_exec.SUDO_BIN, '-n', natural_exec.DONOR_PYTHON_BIN,
            '-I', '-S', '-B', str(self.helper), '--donate',
        ])
        argv = seen['exec_argv']
        self.assertEqual(argv[0], f"/proc/self/fd/{seen['exec_fd']}")
        self.assertEqual(argv[1], '-I')
        self.assertTrue(argv[2].startswith('/proc/self/fd/'))
        self.assertEqual(argv[3:-2], ['natural-host', '--config', '/tmp/c.json', 'serve', '--label', 'x'])
        self.assertEqual(argv[-2], '--alert-handoff-fd')
        self.assertTrue(all(seen['inheritable_during_exec'].values()))
        self.assertFalse(seen['sock_exists_at_exec'])
        self.assertFalse(sock_path.exists())
        self.assertNotIn('PYTHONPATH', seen['exec_env'])
        self.assertNotIn('LD_PRELOAD', seen['exec_env'])
        self.assertNotIn('ENV', seen['exec_env'])
        self.assertNotIn('BASH_ENV', seen['exec_env'])
        self.assertEqual(seen['exec_env']['HUB2_TOKEN'], 'ordinary-config-token')
        # Taken FDs must be closed after exec failure
        for fd in seen['inheritable_during_exec']:
            with self.assertRaises(OSError):
                os.fstat(fd)

    def test_preexisting_endpoint_is_not_unlinked_and_rejects(self):
        nat_dir = self.uid_dir / natural_exec.RUNTIME_SUBDIR
        nat_dir.mkdir(mode=0o700)
        sock_path = nat_dir / f'{INVOCATION_ID}-{os.getpid()}.sock'
        sock_path.write_text('preexisting', encoding='utf-8')
        real_fstat = os.fstat
        real_readlink = os.readlink
        exec_os = types.SimpleNamespace(**vars(os))

        root_parents = {str(p) for p in Path(self.runtime_root).parents} | {str(self.runtime_root), str(self.helper)}

        def runtime_fstat(fd):
            info = real_fstat(fd)
            try:
                p = real_readlink(f'/proc/self/fd/{fd}')
            except OSError:
                p = ''
            if p in root_parents:
                return self._root_stat(info)
            return info

        exec_os.fstat = runtime_fstat
        with patch.dict(os.environ, {'INVOCATION_ID': INVOCATION_ID}, clear=False), \
             patch.object(natural_exec, 'sys', types.SimpleNamespace(platform='linux')), \
             patch.object(natural_exec, 'host_release', return_value=(COMMIT, 1)), \
             patch.object(natural_exec.startup, '_root_graph', side_effect=lambda paths: ({str(p): os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC) for p in paths}, ())), \
             patch.object(natural_exec, 'os', exec_os):
            with self.assertRaises(transfer.TransferError):
                natural_exec.maybe_exec_natural_host(
                    ['serve'],
                    helper_path=str(self.helper),
                    runtime_root=str(self.runtime_root),
                    runner=lambda *a, **k: self.fail('runner must not run'),
                )
        self.assertEqual(sock_path.read_text(encoding='utf-8'), 'preexisting')

    def test_guards_reject_once_handoff_fd_missing_invocation_or_unreleased(self):
        with patch.dict(os.environ, {'INVOCATION_ID': INVOCATION_ID}, clear=False), \
             patch.object(natural_exec, 'sys', types.SimpleNamespace(platform='linux')):
            for argv in (['serve', '--once'], ['serve', '--alert-handoff-fd', '9'], ['heartbeat']):
                with self.assertRaises(transfer.TransferError):
                    natural_exec.maybe_exec_natural_host(argv)
            with patch.object(natural_exec, 'host_release', return_value=(None, None)):
                with self.assertRaises(transfer.TransferError):
                    natural_exec.maybe_exec_natural_host(['serve'])
        with patch.dict(os.environ, {'INVOCATION_ID': 'not-hex'}, clear=False), \
             patch.object(natural_exec, 'sys', types.SimpleNamespace(platform='linux')):
            with self.assertRaises(transfer.TransferError):
                natural_exec.maybe_exec_natural_host(['serve'])


if __name__ == '__main__':
    unittest.main()
