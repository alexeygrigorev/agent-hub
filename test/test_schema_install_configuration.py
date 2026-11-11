"""Exercise the actual installer atomic configuration updater without installing services."""
import json
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ('hub2/node/alert-evidence.ts', 'hub2/node/main.ts', 'hub2/src/worker.ts')


class SchemaInstallConfigurationTests(unittest.TestCase):
    def setUp(self):
        source = (ROOT / 'deploy/vm/install-hub2-node.sh').read_text()
        self.block = source.split("<<'PY'\n", 1)[1].split('\nPY\n', 1)[0]
        self.temporary_root = ROOT / '.local/tmp'
        self.temporary_root.mkdir(parents=True, exist_ok=True)

    def _create_synthetic_checkout(self, base: Path) -> tuple[Path, str]:
        repo = base / 'checkout'
        repo.mkdir(mode=0o700)
        for rel in ARTIFACTS:
            target = repo / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(('synthetic authority ' + rel).encode('utf-8'))
            target.chmod(0o600)
        stage_dst = repo / 'scripts/stage-credential-monitor.py'
        stage_dst.parent.mkdir(parents=True, exist_ok=True)
        stage_dst.write_bytes((ROOT / 'scripts/stage-credential-monitor.py').read_bytes())
        stage_dst.chmod(0o700)
        subprocess.run(['/usr/bin/git', '-C', str(repo), 'init', '-q'], check=True)
        subprocess.run(['/usr/bin/git', '-C', str(repo), 'add', '.'], check=True)
        subprocess.run(
            [
                '/usr/bin/git', '-C', str(repo),
                '-c', 'user.name=Offline Fixture',
                '-c', 'user.email=fixture@example.invalid',
                '-c', 'commit.gpgsign=false',
                '-c', 'core.hooksPath=/dev/null',
                'commit', '-qm', 'synthetic authority',
            ],
            check=True,
        )
        head = subprocess.check_output(
            ['/usr/bin/git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True
        ).strip()
        return repo, head

    def test_updates_modes_without_changing_secret_lines_or_permissions(self):
        with tempfile.TemporaryDirectory(dir=self.temporary_root) as temporary:
            path = Path(temporary) / '.dev.vars'
            path.write_text('# retained comment\nSYNTHETIC_SECRET=keep-private-fixture\nHUB2_RELEASE_MODE=bootstrap\nHUB2_SCHEMA_MIGRATIONS=defer\nHUB2_SCHEMA_MIGRATIONS=apply\n')
            path.chmod(0o640)
            previous = path.stat()
            def run(mode, schema):
                result = subprocess.run([sys.executable, '-', str(path), mode, schema], input=self.block, text=True, capture_output=True)
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

    def test_active_mode_stages_fresh_credential_monitor_boundary_and_sets_dev_vars(self):
        with tempfile.TemporaryDirectory(dir=self.temporary_root) as temporary:
            base = Path(temporary)
            base.chmod(0o700)
            repo, head = self._create_synthetic_checkout(base)
            evidence_root = base / 'credential-monitor-evidence'
            evidence_root.mkdir(mode=0o700)
            path = base / '.dev.vars'
            path.write_text('# retained comment\nSYNTHETIC_SECRET=keep-private-fixture\nHUB2_RELEASE_MODE=bootstrap\n')
            path.chmod(0o600)

            res1 = subprocess.run(
                [sys.executable, '-', str(path), 'active', 'apply', str(repo), str(evidence_root)],
                input=self.block, text=True, capture_output=True,
            )
            self.assertEqual(res1.returncode, 0, res1.stderr)
            boundaries1 = sorted(evidence_root.iterdir())
            self.assertEqual(len(boundaries1), 1)
            first_boundary = boundaries1[0]
            self.assertTrue(first_boundary.name.startswith(head + '-'))
            self.assertEqual(stat.S_IMODE(first_boundary.stat().st_mode), 0o700)
            enrollment = json.loads((first_boundary / 'enrollment.json').read_text())
            journal = json.loads((first_boundary / 'journal.json').read_text())
            self.assertEqual(enrollment['release_sha'], head)
            self.assertEqual(enrollment['producer'], 'credential_monitor')
            self.assertEqual(enrollment['recipient'], 'dev@dtcdev.click')
            self.assertEqual((journal['occurrence_count'], journal['started_total'], journal['finished_total']), (0, 0, 0))
            contents1 = path.read_text()
            self.assertIn(f'HUB2_ALERT_EVIDENCE_DIR={first_boundary}\n', contents1)
            self.assertIn('# retained comment\nSYNTHETIC_SECRET=keep-private-fixture\n', contents1)

            # A second active run stages a brand-new boundary and preserves the first untouched.
            first_snapshot = {p.name: p.read_bytes() for p in first_boundary.iterdir()}
            res2 = subprocess.run(
                [sys.executable, '-', str(path), 'active', 'defer', str(repo), str(evidence_root)],
                input=self.block, text=True, capture_output=True,
            )
            self.assertEqual(res2.returncode, 0, res2.stderr)
            boundaries2 = sorted(evidence_root.iterdir())
            self.assertEqual(len(boundaries2), 2)
            second_boundary = [b for b in boundaries2 if b != first_boundary][0]
            self.assertEqual(first_snapshot, {p.name: p.read_bytes() for p in first_boundary.iterdir()})
            contents2 = path.read_text()
            self.assertEqual(contents2.count('HUB2_ALERT_EVIDENCE_DIR='), 1)
            self.assertIn(f'HUB2_ALERT_EVIDENCE_DIR={second_boundary}\n', contents2)

    def test_bootstrap_and_schema_only_modes_do_not_stage_or_mutate_alert_evidence_dir(self):
        with tempfile.TemporaryDirectory(dir=self.temporary_root) as temporary:
            base = Path(temporary)
            base.chmod(0o700)
            repo, _head = self._create_synthetic_checkout(base)
            evidence_root = base / 'credential-monitor-evidence'
            evidence_root.mkdir(mode=0o700)
            path = base / '.dev.vars'
            prior_dir = evidence_root / 'prior-boundary'
            prior_dir.mkdir(mode=0o700)
            (prior_dir / 'sentinel.json').write_text('{"retained":true}\n')
            path.write_text(
                f'SYNTHETIC_SECRET=keep-private-fixture\nHUB2_RELEASE_MODE=active\nHUB2_ALERT_EVIDENCE_DIR={prior_dir}\n'
            )
            path.chmod(0o600)

            for mode, schema in (('bootstrap', 'defer'), ('', 'apply')):
                res = subprocess.run(
                    [sys.executable, '-', str(path), mode, schema, str(repo), str(evidence_root)],
                    input=self.block, text=True, capture_output=True,
                )
                self.assertEqual(res.returncode, 0, res.stderr)
                self.assertEqual(list(evidence_root.iterdir()), [prior_dir])
                self.assertIn(f'HUB2_ALERT_EVIDENCE_DIR={prior_dir}\n', path.read_text())

    def test_staging_failure_fails_closed_before_modifying_dev_vars(self):
        with tempfile.TemporaryDirectory(dir=self.temporary_root) as temporary:
            base = Path(temporary)
            base.chmod(0o700)
            repo, _head = self._create_synthetic_checkout(base)
            evidence_root = base / 'credential-monitor-evidence'
            evidence_root.mkdir(mode=0o750)  # non-private parent must be refused
            path = base / '.dev.vars'
            original = 'SYNTHETIC_SECRET=keep-private-fixture\nHUB2_RELEASE_MODE=bootstrap\n'
            path.write_text(original)
            path.chmod(0o600)

            res = subprocess.run(
                [sys.executable, '-', str(path), 'active', 'apply', str(repo), str(evidence_root)],
                input=self.block, text=True, capture_output=True,
            )
            self.assertNotEqual(res.returncode, 0)
            self.assertIn('credential_monitor_staging_failed', res.stderr)
            self.assertEqual(path.read_text(), original)
            self.assertEqual(list(evidence_root.iterdir()), [])

    def test_rollback_restores_previous_dev_vars_and_retains_all_historical_boundaries(self):
        with tempfile.TemporaryDirectory(dir=self.temporary_root) as temporary:
            base = Path(temporary)
            base.chmod(0o700)
            repo, _head = self._create_synthetic_checkout(base)
            evidence_root = base / 'credential-monitor-evidence'
            evidence_root.mkdir(mode=0o700)
            path = base / '.dev.vars'
            backup = base / 'dev.vars.previous'
            path.write_text('SYNTHETIC_SECRET=keep-private-fixture\nHUB2_RELEASE_MODE=bootstrap\n')
            path.chmod(0o600)

            # Release 1 (active)
            res1 = subprocess.run(
                [sys.executable, '-', str(path), 'active', 'apply', str(repo), str(evidence_root)],
                input=self.block, text=True, capture_output=True,
            )
            self.assertEqual(res1.returncode, 0, res1.stderr)
            shutil.copy2(path, backup)
            previous_contents = backup.read_text()

            # Release 2 (active) before rollback
            res2 = subprocess.run(
                [sys.executable, '-', str(path), 'active', 'apply', str(repo), str(evidence_root)],
                input=self.block, text=True, capture_output=True,
            )
            self.assertEqual(res2.returncode, 0, res2.stderr)
            self.assertNotEqual(path.read_text(), previous_contents)
            self.assertEqual(len(list(evidence_root.iterdir())), 2)

            # Rollback restores dev.vars.previous and leaves both boundary directories intact
            shutil.copy2(backup, path)
            self.assertEqual(path.read_text(), previous_contents)
            self.assertEqual(len(list(evidence_root.iterdir())), 2)


if __name__ == '__main__':
    unittest.main()
