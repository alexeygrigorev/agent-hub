"""Owned USER image/FIFO fixtures; positive Root metadata is simulated."""
import hashlib
import io
import os
from pathlib import Path
import tempfile
import time
import types
import unittest
from unittest.mock import patch
import zipfile

from agent_hub_host import quota_natural_bootstrap as bootstrap
from agent_hub_host import quota_natural_transfer as transfer


COMMIT = 'a' * 40


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='natural-images-')
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.image = self.root / 'interpreter'
        self.image.write_bytes(b'\x7fELF' + b'A' * 300000)
        self.image.chmod(0o700)
        self.archive = self.root / 'host.pyz'
        with zipfile.ZipFile(self.archive, 'w') as z:
            z.writestr('agent_hub_host/_release.py',
                       f'HOST_VERSION = {COMMIT!r}\nPROTOCOL_VERSION = 1\n')
        self.slots = [os.open(self.image, os.O_RDONLY | os.O_CLOEXEC),
                      os.open(self.archive, os.O_RDONLY | os.O_CLOEXEC)]
        self.slots.extend(os.open('/dev/null', os.O_RDONLY | os.O_CLOEXEC) for _ in range(11))
        read, write = os.pipe2(os.O_CLOEXEC)
        self.slots.append(read)
        self.addCleanup(os.close, write)
        self.donation = transfer.ReceivedDonation(self.slots, time.monotonic_ns() + 5_000_000_000)
        self.addCleanup(self.donation.close)
        # Keep real OS operations on owned fixture FDs. Replace only this
        # module's fstat identity; production Root guards remain untouched.
        self.real_os = os
        self.os_proxy = types.SimpleNamespace(**vars(os))

    def root_info(self, fd):
        info = os.fstat(fd)
        values = {key: getattr(info, key) for key in dir(info) if key.startswith('st_')}
        values['st_uid'] = 0
        return types.SimpleNamespace(**values)

    def graph(self, paths):
        return {str(path): os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
                for path in paths}, ()

    def positive_context(self):
        self.os_proxy.fstat = self.root_info
        self.os_proxy.readlink = lambda _: '/usr/bin/python3.11'
        self.os_proxy.read = lambda *args: self.fail('Root manifest must stay unread')
        return patch.object(bootstrap, 'os', self.os_proxy)

    def validate_fixture(self):
        with self.positive_context(), patch.object(bootstrap.startup, '_root_graph', side_effect=self.graph), \
                patch.object(bootstrap.startup, '_image') as checked:
            result = bootstrap.validate_donated_images(self.donation, COMMIT)
            self.assertEqual(checked.call_count, 2)
            return result

    def test_full_images_checked_without_reading_root_manifest_or_changing_inheritance(self):
        result = self.validate_fixture()
        self.addCleanup(result.close)
        self.assertEqual(result.sections[0]['size'], self.image.stat().st_size)
        self.assertGreater(result.sections[0]['size'], 262144)
        self.assertEqual(result.sections[0]['sha256'], hashlib.sha256(self.image.read_bytes()).hexdigest())
        self.assertEqual(result.sections[1]['path'], f'{bootstrap.RELEASE_ROOT}/{COMMIT}/host.pyz')
        for fd in self.slots:
            self.assertFalse(os.get_inheritable(fd))
        self.assertEqual(self.donation._owned, self.slots)
        self.assertTrue(result.close())
        self.assertTrue(result.close())
        for fd in self.slots:
            os.fstat(fd)
        with self.assertRaises(transfer.TransferError):
            result.recheck()

    def test_real_user_created_pipe_is_not_root_donation(self):
        with patch.object(bootstrap.startup, '_root_graph', side_effect=AssertionError('opened graph')):
            with self.assertRaises(transfer.TransferError):
                bootstrap.validate_donated_images(self.donation, COMMIT)
        os.fstat(self.donation.manifest_fd)

    def test_inheritable_donated_descriptor_rejected_before_opening_graph(self):
        os.set_inheritable(self.slots[2], True)
        with patch.object(bootstrap.startup, '_root_graph', side_effect=AssertionError('opened graph')):
            with self.assertRaises(transfer.TransferError):
                bootstrap.validate_donated_images(self.donation, COMMIT)

    def test_missing_donated_descriptor_rejected_before_opening_graph(self):
        lost = self.slots[2]
        os.close(lost)
        # The receiver owns the set, but this test deliberately removed one.
        self.donation._owned.remove(lost)
        with patch.object(bootstrap.startup, '_root_graph', side_effect=AssertionError('opened graph')):
            with self.assertRaises(transfer.TransferError):
                bootstrap.validate_donated_images(self.donation, COMMIT)

    def test_invalid_commit_refused_before_os_probes(self):
        with patch.object(bootstrap, 'os', types.SimpleNamespace(getuid=lambda: self.fail('probed uid'))):
            for value in ['main', 'a' * 39, '../' + COMMIT, COMMIT.upper()]:
                with self.assertRaises(transfer.TransferError):
                    bootstrap.validate_donated_images(self.donation, value)

    def test_original_transfer_deadline_is_not_renewed(self):
        self.donation.deadline_ns = time.monotonic_ns() - 1
        with self.assertRaises(transfer.TransferError):
            bootstrap.validate_donated_images(self.donation, COMMIT)

    def test_nonapproved_interpreter_name_refused(self):
        with self.positive_context():
            self.os_proxy.readlink = lambda _: '/usr/bin/python3.11 (deleted)'
            with patch.object(bootstrap.startup, '_root_graph', side_effect=AssertionError('opened graph')):
                with self.assertRaises(transfer.TransferError):
                    bootstrap.validate_donated_images(self.donation, COMMIT)

    def test_wrong_archive_release_marker_closes_only_new_graph(self):
        with zipfile.ZipFile(self.archive, 'w') as z:
            z.writestr('agent_hub_host/_release.py', "HOST_VERSION = 'wrong'\nPROTOCOL_VERSION = 1\n")
        nodes = []
        def graph(paths):
            value = self.graph(paths)
            nodes.extend(value[0].values())
            return value
        with self.positive_context(), patch.object(bootstrap.startup, '_root_graph', side_effect=graph):
            with self.assertRaises(transfer.TransferError):
                bootstrap.validate_donated_images(self.donation, COMMIT)
        for fd in nodes:
            with self.assertRaises(OSError):
                os.fstat(fd)
        for fd in self.slots:
            os.fstat(fd)

    def test_recheck_reuses_full_root_name_fd_and_byte_verifier(self):
        result = self.validate_fixture()
        self.addCleanup(result.close)
        with patch.object(bootstrap.startup, '_image', side_effect=ValueError('changed image')) as verify:
            with self.assertRaisesRegex(ValueError, 'changed image'):
                result.recheck()
            self.assertEqual(verify.call_args.args[0], result.sections[0])

    def test_section_rejects_nonroot_or_writable_images(self):
        deadline = time.monotonic_ns() + 5_000_000_000
        with self.assertRaises(transfer.TransferError):
            bootstrap._section(self.slots[0], '/usr/bin/python3.11', deadline, executable=True)
        self.image.chmod(0o722)
        with self.positive_context():
            with self.assertRaises(transfer.TransferError):
                bootstrap._section(self.slots[0], '/usr/bin/python3.11', deadline, executable=True)

    def test_section_rejects_mid_read_expiry(self):
        deadline = time.monotonic_ns() + 5_000_000_000
        times = iter([deadline - 1, deadline - 1, deadline + 1])
        with self.positive_context(), patch.object(bootstrap.time, 'monotonic_ns', side_effect=lambda: next(times)):
            with self.assertRaises(transfer.TransferError):
                bootstrap._section(self.slots[0], '/usr/bin/python3.11', deadline, executable=True)


if __name__ == '__main__':
    unittest.main()
