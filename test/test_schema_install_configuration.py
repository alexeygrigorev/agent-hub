"""Exercise the actual installer atomic configuration updater without installing services."""
import json
import os
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
        self.installer_source = (ROOT / 'deploy/vm/install-hub2-node.sh').read_text()
        self.block = self.installer_source.split("<<'PY'\n", 1)[1].split('\nPY\n', 1)[0]
        state_idx = self.installer_source.index('echo "== state directories"')
        backup_idx = self.installer_source.index('echo "== daily backup')
        self.installer_state_and_release_section = self.installer_source[state_idx:backup_idx]
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
            # Notice evidence_root does not exist before first active run; Python block creates it O_NOFOLLOW 0700.
            evidence_root = base / 'credential-monitor-evidence'
            path = base / '.dev.vars'
            path.write_text('# retained comment\nSYNTHETIC_SECRET=keep-private-fixture\nHUB2_RELEASE_MODE=bootstrap\n')
            path.chmod(0o600)

            res1 = subprocess.run(
                [sys.executable, '-', str(path), 'active', 'apply', str(repo), str(evidence_root)],
                input=self.block, text=True, capture_output=True,
            )
            self.assertEqual(res1.returncode, 0, res1.stderr)
            self.assertEqual(stat.S_IMODE(evidence_root.lstat().st_mode), 0o700)
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
            evidence_root.mkdir(mode=0o750)  # non-private parent must be refused without repairing mode
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
            self.assertEqual(stat.S_IMODE(evidence_root.lstat().st_mode), 0o750)
            self.assertEqual(path.read_text(), original)
            self.assertEqual(list(evidence_root.iterdir()), [])

    def test_installer_shell_and_python_never_follow_symlink_or_mutate_existing_evidence_root_metadata(self):
        # Ensure shell script never passes CREDENTIAL_MONITOR_EVIDENCE_ROOT to install/mkdir/chmod/chown.
        for line in self.installer_source.splitlines():
            stripped = line.strip()
            if 'CREDENTIAL_MONITOR_EVIDENCE_ROOT' in stripped and not stripped.startswith('#'):
                self.assertFalse(
                    stripped.startswith(('install ', 'mkdir ', 'chmod ', 'chown ')),
                    f'shell must not pre-create or mutate CREDENTIAL_MONITOR_EVIDENCE_ROOT: {stripped}',
                )

        with tempfile.TemporaryDirectory(dir=self.temporary_root) as temporary:
            base = Path(temporary)
            base.chmod(0o700)
            repo, head = self._create_synthetic_checkout(base)
            path = repo / '.dev.vars'
            original = 'SYNTHETIC_SECRET=keep-private-fixture\nHUB2_RELEASE_MODE=bootstrap\n'
            path.write_text(original)
            path.chmod(0o600)

            # Provide an install wrapper in PATH that strips -o/-g when unprivileged and delegates
            # to real /usr/bin/install so any shell `install -d -m 700` on a symlink would mutate target mode.
            bindir = base / 'bin'
            bindir.mkdir(mode=0o700)
            wrapper = bindir / 'install'
            wrapper.write_text(
                '#!/usr/bin/env python3\n'
                'import os, sys\n'
                'args = []\n'
                'it = iter(sys.argv[1:])\n'
                'for a in it:\n'
                '    if a in ("-o", "-g"):\n'
                '        next(it, None)\n'
                '    else:\n'
                '        args.append(a)\n'
                'os.execv("/usr/bin/install", ["/usr/bin/install", *args])\n',
                encoding='utf-8',
            )
            wrapper.chmod(0o755)

            state_dir = base / 'state'
            state_dir.mkdir(mode=0o700)
            db_dir = state_dir / 'hub2-db'
            evidence_root = state_dir / 'credential-monitor-evidence'

            def run_installer_section(ev_root: Path, release_mode: str = 'active') -> subprocess.CompletedProcess[str]:
                env = dict(
                    os.environ,
                    PATH=f"{bindir}:{os.environ['PATH']}",
                    REPO=str(repo),
                    STATE=str(state_dir),
                    DB_DIR=str(db_dir),
                    CREDENTIAL_MONITOR_EVIDENCE_ROOT=str(ev_root),
                    RELEASE_MODE=release_mode,
                    SCHEMA_MIGRATIONS='apply',
                    KEEP_CLI_ASSETS='true',
                )
                return subprocess.run(
                    ['bash', '-euo', 'pipefail', '-c', self.installer_state_and_release_section],
                    env=env,
                    text=True,
                    capture_output=True,
                    timeout=15,
                )

            # 1. Symlink to a 0755 directory: must fail closed and NEVER chmod/chown the 0755 target.
            victim_dir = base / 'victim-dir'
            victim_dir.mkdir(mode=0o755)
            victim_dir.chmod(0o755)
            evidence_root.symlink_to(victim_dir, target_is_directory=True)
            before_victim = victim_dir.lstat()
            before_link = evidence_root.lstat()
            res_sym = run_installer_section(evidence_root)
            self.assertNotEqual(res_sym.returncode, 0)
            self.assertIn('credential_monitor_staging_failed', res_sym.stderr)
            after_victim = victim_dir.lstat()
            after_link = evidence_root.lstat()
            self.assertTrue(evidence_root.is_symlink())
            self.assertEqual(stat.S_IMODE(after_victim.st_mode), 0o755)
            self.assertEqual((after_victim.st_uid, after_victim.st_gid), (before_victim.st_uid, before_victim.st_gid))
            self.assertEqual(after_link.st_ino, before_link.st_ino)
            self.assertEqual(list(victim_dir.iterdir()), [])
            self.assertEqual(path.read_text(), original)
            evidence_root.unlink()

            # 2. Existing non-private directory (0755): must fail closed and preserve 0755 without repair.
            evidence_root.mkdir(mode=0o755)
            evidence_root.chmod(0o755)
            res_nonpriv = run_installer_section(evidence_root)
            self.assertNotEqual(res_nonpriv.returncode, 0)
            self.assertIn('credential_monitor_staging_failed', res_nonpriv.stderr)
            self.assertEqual(stat.S_IMODE(evidence_root.lstat().st_mode), 0o755)
            self.assertEqual(list(evidence_root.iterdir()), [])
            self.assertEqual(path.read_text(), original)
            evidence_root.rmdir()

            # 3. Existing regular file (0644): must fail closed and preserve 0644 + contents.
            evidence_root.write_text('keep-existing-file')
            evidence_root.chmod(0o644)
            res_file = run_installer_section(evidence_root)
            self.assertNotEqual(res_file.returncode, 0)
            self.assertEqual(stat.S_IMODE(evidence_root.lstat().st_mode), 0o644)
            self.assertEqual(evidence_root.read_text(), 'keep-existing-file')
            self.assertEqual(path.read_text(), original)
            evidence_root.unlink()

            # 4. Symlinked ancestor directory: must fail closed and leave victim untouched.
            linked_state = base / 'linked-state'
            linked_state.symlink_to(state_dir, target_is_directory=True)
            res_anc = run_installer_section(linked_state / 'credential-monitor-evidence')
            self.assertNotEqual(res_anc.returncode, 0)
            self.assertFalse(evidence_root.exists())
            self.assertEqual(path.read_text(), original)

            # 5. Clean absent evidence_root: installer creates 0700 directory, stages boundary, updates .dev.vars.
            res_ok = run_installer_section(evidence_root)
            self.assertEqual(res_ok.returncode, 0, res_ok.stderr)
            self.assertTrue(evidence_root.is_dir() and not evidence_root.is_symlink())
            self.assertEqual(stat.S_IMODE(evidence_root.lstat().st_mode), 0o700)
            staged = list(evidence_root.iterdir())
            self.assertEqual(len(staged), 1)
            self.assertTrue(staged[0].name.startswith(head + '-'))
            self.assertIn(f'HUB2_ALERT_EVIDENCE_DIR={staged[0]}\n', path.read_text())

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
