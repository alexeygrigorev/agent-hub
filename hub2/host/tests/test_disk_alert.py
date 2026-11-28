"""Tests for agent_hub_host.disk_alert (AC2) and ops/lib/send.sh ENOSPC fallback (AC4)."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from agent_hub_host import disk_alert

REPO_ROOT = Path(__file__).resolve().parents[3]
SEND_SH = REPO_ROOT / 'ops' / 'lib' / 'send.sh'


class DiskAlertAndSendTests(unittest.TestCase):
    def test_early_warn_crit_and_24h_projected_fill_thresholds(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / 'state'
            fake_oncall = Path(tmp) / 'oncall-send'
            captured = Path(tmp) / 'captured.txt'
            fake_oncall.write_text(
                f'#!/usr/bin/env bash\necho "$@" >> "{captured}"\necho "STATUS=ok"\nexit 2\n',
                encoding='utf-8',
            )
            fake_oncall.chmod(0o755)

            # 85% -> WARN
            res_warn = disk_alert.run_check(
                mount=Path('/'), state_dir=state, force_used_pct=86.0, oncall_bin=str(fake_oncall),
            )
            self.assertEqual(res_warn['report']['level'], 'WARN')
            self.assertTrue(res_warn['delivery']['ok'])

            # 92% -> CRIT
            res_crit = disk_alert.run_check(
                mount=Path('/'), state_dir=state, force_used_pct=93.0, oncall_bin=str(fake_oncall),
            )
            self.assertEqual(res_crit['report']['level'], 'CRIT')
            self.assertTrue(res_crit['delivery']['ok'])

            # Projected fill <= 24h -> CRIT even if used% is low
            res_fill = disk_alert.run_check(
                mount=Path('/'), state_dir=state, force_used_pct=50.0, force_fill_hours=12.0,
                oncall_bin=str(fake_oncall),
            )
            self.assertEqual(res_fill['report']['level'], 'CRIT')
            text = captured.read_text(encoding='utf-8')
            self.assertIn('[DISK WARN]', text)
            self.assertIn('[DISK CRIT]', text)
            self.assertIn('projected fill in 12.0h', text)

    def test_oncall_send_enospc_fallback_to_tmpfs_and_drain(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bin_dir = root / 'bin'
            bin_dir.mkdir()
            oncall_bin = bin_dir / 'oncall-send'
            oncall_bin.write_text(SEND_SH.read_text(encoding='utf-8'), encoding='utf-8')
            oncall_bin.chmod(0o755)

            # Stub `a` CLI so send.sh exits 2 cleanly without touching live sessions
            fake_a = bin_dir / 'a'
            fake_a.write_text('#!/usr/bin/env bash\necho "[]"\n', encoding='utf-8')
            fake_a.chmod(0o755)

            primary = root / 'primary_inbox'
            tmpfs = root / 'tmpfs_inbox'
            env = {
                **os.environ,
                'HOME': str(root),
                'PATH': f"{bin_dir}:{os.environ.get('PATH', '')}",
                'OPS_INBOX_DIR': str(primary),
                'OPS_TMPFS_INBOX_DIR': str(tmpfs),
                'OPS_SIMULATE_ENOSPC': '1',
            }
            p1 = subprocess.run([str(oncall_bin), 'disk full alert'], env=env, capture_output=True, text=True, check=False)
            self.assertIn(p1.returncode, (0, 2))
            self.assertIn('STATUS=fallback_tmpfs', p1.stdout)
            self.assertIn('INBOX=saved_tmpfs', p1.stdout)
            self.assertEqual(len(list(tmpfs.glob('*.md'))), 1)
            self.assertEqual(len(list(primary.glob('*.md'))), 0)

            # Space returns: next send drains tmpfs into primary inbox
            env['OPS_SIMULATE_ENOSPC'] = '0'
            p2 = subprocess.run([str(oncall_bin), 'space recovered'], env=env, capture_output=True, text=True, check=False)
            self.assertIn(p2.returncode, (0, 2))
            self.assertIn('DRAINED=1', p2.stdout)
            self.assertIn('STATUS=ok', p2.stdout)
            self.assertEqual(len(list(tmpfs.glob('*.md'))), 0)
            self.assertEqual(len(list(primary.glob('*.md'))), 2)


if __name__ == '__main__':
    unittest.main()
