"""Exercise the actual installer atomic configuration updater without installing services."""
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]

class SchemaInstallConfigurationTests(unittest.TestCase):
    def test_updates_modes_without_changing_secret_lines_or_permissions(self):
        source = (ROOT / 'deploy/vm/install-hub2-node.sh').read_text()
        block = source.split("<<'PY'\n", 1)[1].split('\nPY\n', 1)[0]
        temporary_root = ROOT / '.local/tmp'
        temporary_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=temporary_root) as temporary:
            path = Path(temporary) / '.dev.vars'
            path.write_text('# retained comment\nSYNTHETIC_SECRET=keep-private-fixture\nHUB2_RELEASE_MODE=bootstrap\nHUB2_SCHEMA_MIGRATIONS=defer\nHUB2_SCHEMA_MIGRATIONS=apply\n')
            path.chmod(0o640)
            previous = path.stat()
            def run(mode, schema):
                result = subprocess.run([sys.executable, '-', str(path), mode, schema], input=block, text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, '')
            run('active', 'defer')
            contents = path.read_text()
            self.assertIn('# retained comment\nSYNTHETIC_SECRET=keep-private-fixture\n', contents)
            self.assertEqual(contents.count('HUB2_SCHEMA_MIGRATIONS='), 1)
            self.assertIn('HUB2_SCHEMA_MIGRATIONS=defer\n', contents)
            self.assertIn('HUB2_RELEASE_MODE=active\n', contents)
            run('', 'apply')
            self.assertIn('HUB2_RELEASE_MODE=active\n', path.read_text())
            run('bootstrap', '')
            self.assertIn('HUB2_SCHEMA_MIGRATIONS=apply\n', path.read_text())
            current = path.stat()
            self.assertEqual(stat.S_IMODE(current.st_mode), 0o640)
            self.assertEqual((current.st_uid, current.st_gid), (previous.st_uid, previous.st_gid))
            self.assertEqual(list(path.parent.glob('.dev.vars.*')), [])

if __name__ == '__main__':
    unittest.main()
