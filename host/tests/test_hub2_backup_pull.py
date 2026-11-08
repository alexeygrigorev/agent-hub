"""Exercise the actual pull coordinator with hermetic SSH/rsync/alert stubs."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[2] / 'scripts/hub2-backup-pull.sh'


class PullTest(unittest.TestCase):
    def test_success_and_failed_probe_never_mail(self):
        with tempfile.TemporaryDirectory() as root:
            p = Path(root)
            calls = p / 'calls'
            for name, body in {
                'ssh': 'exit "${SSH_RESULT:-0}"',
                'rsync': 'printf "rsync\\n" >> "$CALLS"',
                'monitor': 'printf "%s\\n" "$*" >> "$CALLS"',
            }.items():
                stub = p / name
                stub.write_text('#!/bin/bash\n' + body + '\n')
                stub.chmod(0o755)
            config = p / 'config'
            config.write_text(f'''VM_HOST=fixture.invalid
SSH_KEY={p}/key
BACKUP_DIR={p}/local
STORAGEBOX_DIR={p}/mirror
STATE_DIR={p}/state
STALENESS_SCRIPT={p}/monitor
''')
            env = dict(os.environ, PATH=f'{p}:' + os.environ['PATH'],
                       HUB2_BACKUP_PULL_ENV_FILE=str(config), CALLS=str(calls))
            ok = subprocess.run(['bash', str(SCRIPT), '--no-mail'], env=env, capture_output=True)
            self.assertEqual(ok.returncode, 0, ok.stderr)
            self.assertEqual(calls.read_text().splitlines(), ['rsync', 'rsync', '--check-only'])
            calls.unlink()
            failed = subprocess.run(['bash', str(SCRIPT), '--no-mail'],
                                    env=dict(env, SSH_RESULT='42'), capture_output=True)
            self.assertEqual(failed.returncode, 42)
            self.assertIn('--backup-failed', calls.read_text())
            self.assertIn('--no-mail', calls.read_text())
            self.assertNotIn('rsync', calls.read_text())
            calls.unlink()
            scheduled = subprocess.run(['bash', str(SCRIPT)],
                                       env=dict(env, SSH_RESULT='42'), capture_output=True)
            self.assertEqual(scheduled.returncode, 42)
            self.assertIn('--backup-failed', calls.read_text())
            self.assertNotIn('--no-mail', calls.read_text())
