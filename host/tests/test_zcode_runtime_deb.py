"""ZCode runtime comes from the Linux deb on every OS, extracted in pure Python (intake 5814e109)."""
import io
import os
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from agent_hub_host import provisioning as p


def _deb(members, data_name='data.tar.xz', mode='w:xz'):
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode=mode) as tar:
        for name, payload in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
    data = raw.getvalue()

    def entry(name, body):
        header = (name + '/').ljust(16).encode() + b'0'.ljust(12) + b'0'.ljust(6) + b'0'.ljust(6) + b'100644'.ljust(8)
        header += str(len(body)).encode().ljust(10) + b'`' + b'\n'
        return header + body + (b'\n' if len(body) & 1 else b'')

    return b'!<arch>' + b'\n' + entry('debian-binary', b'2.0\n') + entry('control.tar.xz', b'x') + entry(data_name, data)


class ExtractDebResources(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_extracts_only_the_resources_tree(self):
        deb = self.tmp / 'z.deb'
        deb.write_bytes(_deb({'./opt/ZCode/resources/glm/zcode.cjs': b'// js', './opt/ZCode/ZCode': b'elf',
                              './usr/share/x': b'y'}))
        out = self.tmp / 'out'
        p._extract_deb_resources(deb, out)
        self.assertEqual((out / 'opt/ZCode/resources/glm/zcode.cjs').read_bytes(), b'// js')
        self.assertFalse((out / 'opt/ZCode/ZCode').exists())
        self.assertFalse((out / 'usr').exists())

    def test_refuses_traversal_and_zstd(self):
        deb = self.tmp / 'bad.deb'
        deb.write_bytes(_deb({'opt/ZCode/resources/../../../evil': b'x'}))
        with self.assertRaises(ValueError):
            p._extract_deb_resources(deb, self.tmp / 'o1')
        deb.write_bytes(_deb({'a': b'b'}, data_name='data.tar.zst'))
        with self.assertRaises(ValueError):
            p._extract_deb_resources(deb, self.tmp / 'o2')
        self.assertFalse((self.tmp / 'evil').exists())


if __name__ == '__main__':
    unittest.main()
