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

    def test_helper_refuses_owned_temp_replacement_between_mkdir_and_stat_or_open(self):
        # Ensure ensure_private_evidence_root never drops process capabilities, switches process euid/egid,
        # or performs name-based rmdir/unlink under the service-writable parent directory.
        helper_src = self.block.split('def ensure_private_evidence_root', 1)[1].split('if mode == "active"', 1)[0]
        for forbidden in ('capset', 'prctl', 'seteuid', 'setegid', 'fchmod', 'rmdir', 'unlink', 'time.sleep'):
            self.assertNotIn(forbidden, helper_src)
        self.assertEqual(helper_src.count('os.fchown('), 1)
        self.assertIn('os.fchown(child_fd, owner_uid, owner_gid)', helper_src)

        with tempfile.TemporaryDirectory(dir=self.temporary_root) as temporary:
            base = Path(temporary)
            base.chmod(0o700)
            repo, head = self._create_synthetic_checkout(base)
            path = base / '.dev.vars'
            original = 'SYNTHETIC_SECRET=keep-private-fixture\nHUB2_RELEASE_MODE=bootstrap\n'
            path.write_text(original)
            path.chmod(0o600)

            # 1. Simulated root execution: parent capabilities remain untouched through _drop_privileges and .dev.vars chown,
            # and the private 0700 .stage-box-<uuid> container is retained deliberately without name-based rmdir.
            root_state_dir = base / 'state-simulated-root'
            root_state_dir.mkdir(mode=0o700)
            root_evidence = root_state_dir / 'credential-monitor-evidence'
            trace_file = base / 'root-trace.json'
            root_harness = (
                'import json, os, stat, sys\n'
                f'trace_file = {str(trace_file)!r}\n'
                'real_fstat = os.fstat\n'
                'real_lstat = os.lstat\n'
                'real_stat = os.stat\n'
                'SERVICE_UID = 4242\n'
                'SERVICE_GID = 4242\n'
                'chowned_inodes = set()\n'
                'events = []\n'
                'class _StatProxy:\n'
                '    def __init__(self, st, uid, gid):\n'
                '        self._st = st\n'
                '        self.st_uid = uid\n'
                '        self.st_gid = gid\n'
                '    def __getattr__(self, name):\n'
                '        return getattr(self._st, name)\n'
                'def _wrap_stat(st, force_service=False):\n'
                '    if force_service or st.st_ino in chowned_inodes:\n'
                '        return _StatProxy(st, SERVICE_UID, SERVICE_GID)\n'
                '    return _StatProxy(st, 0, 0)\n'
                'os.geteuid = lambda: 0\n'
                'os.getegid = lambda: 0\n'
                'def fake_lstat(p):\n'
                '    st = real_lstat(p)\n'
                '    if str(p) == sys.argv[1]:\n'
                '        return _wrap_stat(st, force_service=True)\n'
                '    return _wrap_stat(st)\n'
                'def fake_stat(p, *, dir_fd=None, follow_symlinks=True):\n'
                '    st = real_stat(p, dir_fd=dir_fd, follow_symlinks=follow_symlinks)\n'
                '    return _wrap_stat(st)\n'
                'def fake_fstat(fd):\n'
                '    st = real_fstat(fd)\n'
                '    return _wrap_stat(st)\n'
                'def fake_fchown(fd, uid, gid):\n'
                '    st = real_fstat(fd)\n'
                '    events.append(("fchown", st.st_ino, uid, gid))\n'
                '    chowned_inodes.add(st.st_ino)\n'
                'def fake_chown(p, uid, gid):\n'
                '    events.append(("chown_dev_vars", uid, gid))\n'
                'def fake_setgroups(groups):\n'
                '    events.append(("setgroups", list(groups)))\n'
                'def fake_setresgid(r, e, s):\n'
                '    events.append(("setresgid", r, e, s))\n'
                'def fake_setresuid(r, e, s):\n'
                '    events.append(("setresuid", r, e, s))\n'
                'os.lstat = fake_lstat\n'
                'os.stat = fake_stat\n'
                'os.fstat = fake_fstat\n'
                'os.fchown = fake_fchown\n'
                'os.chown = fake_chown\n'
                'os.setgroups = fake_setgroups\n'
                'os.setresgid = fake_setresgid\n'
                'os.setresuid = fake_setresuid\n'
            ) + self.block + (
                '\nwith open(trace_file, "w", encoding="utf-8") as tf:\n'
                '    json.dump(events, tf)\n'
            )
            res_root = subprocess.run(
                [sys.executable, '-', str(path), 'active', 'apply', str(repo), str(root_evidence)],
                input=root_harness,
                text=True,
                capture_output=True,
                timeout=15,
            )
            self.assertEqual(res_root.returncode, 0, res_root.stderr)
            events = json.loads(trace_file.read_text(encoding='utf-8'))
            ev_ino = root_evidence.lstat().st_ino
            self.assertIn(['fchown', ev_ino, 4242, 4242], events)
            self.assertIn(['chown_dev_vars', 4242, 4242], events)
            self.assertEqual([e for e in events if e[0].startswith('box')], [])
            boxes = [p for p in root_state_dir.iterdir() if p.name.startswith('.stage-box-')]
            self.assertEqual(len(boxes), 1)
            self.assertEqual(stat.S_IMODE(boxes[0].lstat().st_mode), 0o700)
            self.assertEqual(list(boxes[0].iterdir()), [])
            self.assertTrue(root_evidence.is_dir() and not root_evidence.is_symlink())
            staged = list(root_evidence.iterdir())
            self.assertEqual(len(staged), 1)
            self.assertTrue(staged[0].name.startswith(head + '-'))
            path.write_text(original)

            # 2. Cleanup replacement race: replacing .stage-box-<uuid> after publish with an unrelated empty or
            # populated directory must leave the replacement directory byte- and metadata-present (never rmdir'd).
            for populated in (False, True):
                with self.subTest(cleanup_replacement_populated=populated):
                    cleanup_state_dir = base / f'state-cleanup-{"pop" if populated else "empty"}'
                    cleanup_state_dir.mkdir(mode=0o700)
                    cleanup_evidence = cleanup_state_dir / 'credential-monitor-evidence'
                    unrelated_dir = base / f'unrelated-{"pop" if populated else "empty"}'
                    unrelated_dir.mkdir(mode=0o755)
                    unrelated_dir.chmod(0o755)
                    if populated:
                        marker_file = unrelated_dir / 'keep.txt'
                        marker_file.write_text('preserve-unrelated-bytes', encoding='utf-8')
                        marker_file.chmod(0o640)
                        before_marker_stat = marker_file.lstat()
                    before_unrelated_stat = unrelated_dir.lstat()

                    cleanup_harness = (
                        'import os\n'
                        f'cleanup_state_dir = {str(cleanup_state_dir)!r}\n'
                        f'unrelated_dir = {str(unrelated_dir)!r}\n'
                        'real_fsync = os.fsync\n'
                        'swapped = False\n'
                        'def hooked_fsync(fd):\n'
                        '    global swapped\n'
                        '    real_fsync(fd)\n'
                        '    if not swapped and os.path.exists(os.path.join(cleanup_state_dir, "credential-monitor-evidence")):\n'
                        '        boxes = [d for d in os.listdir(cleanup_state_dir) if d.startswith(".stage-box-")]\n'
                        '        if boxes:\n'
                        '            swapped = True\n'
                        '            box_path = os.path.join(cleanup_state_dir, boxes[0])\n'
                        '            os.rename(box_path, box_path + "-moved")\n'
                        '            os.rename(unrelated_dir, box_path)\n'
                        'os.fsync = hooked_fsync\n'
                    ) + self.block

                    res_cleanup = subprocess.run(
                        [sys.executable, '-', str(path), 'active', 'apply', str(repo), str(cleanup_evidence)],
                        input=cleanup_harness,
                        text=True,
                        capture_output=True,
                        timeout=15,
                    )
                    self.assertEqual(res_cleanup.returncode, 0, res_cleanup.stderr)
                    replaced_boxes = [
                        p for p in cleanup_state_dir.iterdir()
                        if p.name.startswith('.stage-box-') and not p.name.endswith('-moved')
                    ]
                    self.assertEqual(len(replaced_boxes), 1)
                    after_unrelated_stat = replaced_boxes[0].lstat()
                    self.assertEqual(after_unrelated_stat.st_ino, before_unrelated_stat.st_ino)
                    self.assertEqual(stat.S_IMODE(after_unrelated_stat.st_mode), 0o755)
                    self.assertEqual(
                        (after_unrelated_stat.st_uid, after_unrelated_stat.st_gid),
                        (before_unrelated_stat.st_uid, before_unrelated_stat.st_gid),
                    )
                    if populated:
                        replaced_marker = replaced_boxes[0] / 'keep.txt'
                        self.assertTrue(replaced_marker.is_file())
                        self.assertEqual(replaced_marker.read_text(encoding='utf-8'), 'preserve-unrelated-bytes')
                        self.assertEqual(replaced_marker.lstat().st_ino, before_marker_stat.st_ino)
                        self.assertEqual(stat.S_IMODE(replaced_marker.lstat().st_mode), 0o640)
                    else:
                        self.assertEqual(list(replaced_boxes[0].iterdir()), [])
                    path.write_text(original)

            # 3. Race scenarios: replacement of box, replacement of child before fchown, pre-created leaf, post-publish leaf swap.
            scenarios = (
                'replace_box_with_service_owned_dir',
                'replace_child_with_service_owned_dir_before_fchown',
                'replace_box_entry_before_publish',
                'create_leaf_before_publish',
                'replace_leaf_after_publish',
            )
            for scenario in scenarios:
                with self.subTest(scenario=scenario):
                    state_dir = base / f'state-{scenario}'
                    state_dir.mkdir(mode=0o700)
                    evidence_root = state_dir / 'credential-monitor-evidence'
                    pre_existing = base / f'pre-{scenario}'
                    pre_existing.mkdir(mode=0o755)
                    pre_existing.chmod(0o755)
                    pre_stat = pre_existing.lstat()
                    pre_ino = pre_stat.st_ino

                    harness = (
                        'import os, sys\n'
                        f'scenario = {scenario!r}\n'
                        f'state_dir = {str(state_dir)!r}\n'
                        f'pre_existing = {str(pre_existing)!r}\n'
                        f'pre_ino = {pre_ino!r}\n'
                        'real_mkdir = os.mkdir\n'
                        'real_stat = os.stat\n'
                        'real_fstat = os.fstat\n'
                        'real_lstat = os.lstat\n'
                        'published = False\n'
                        'SERVICE_UID = 4242\n'
                        'SERVICE_GID = 4242\n'
                        'service_inodes = {pre_ino}\n'
                        'fchown_calls = []\n'
                        'class _StatProxy:\n'
                        '    def __init__(self, st, uid, gid):\n'
                        '        self._st = st\n'
                        '        self.st_uid = uid\n'
                        '        self.st_gid = gid\n'
                        '    def __getattr__(self, name):\n'
                        '        return getattr(self._st, name)\n'
                        'def _wrap(st, force_service=False):\n'
                        '    if force_service or st.st_ino in service_inodes:\n'
                        '        return _StatProxy(st, SERVICE_UID, SERVICE_GID)\n'
                        '    return _StatProxy(st, 0, 0)\n'
                        'os.geteuid = lambda: 0\n'
                        'os.getegid = lambda: 0\n'
                        'os.lstat = lambda p: _wrap(real_lstat(p), force_service=(str(p) == sys.argv[1]))\n'
                        'os.fstat = lambda fd: _wrap(real_fstat(fd))\n'
                        'def fake_fchown(fd, uid, gid):\n'
                        '    st = real_fstat(fd)\n'
                        '    fchown_calls.append(st.st_ino)\n'
                        '    service_inodes.add(st.st_ino)\n'
                        'os.fchown = fake_fchown\n'
                        'def hooked_mkdir(name, mode=0o777, *, dir_fd=None):\n'
                        '    real_mkdir(name, mode, dir_fd=dir_fd)\n'
                        '    if isinstance(name, str) and name.startswith(".stage-box-"):\n'
                        '        if scenario == "replace_box_with_service_owned_dir":\n'
                        '            os.rmdir(name, dir_fd=dir_fd)\n'
                        '            os.rename(pre_existing, os.path.join(state_dir, name))\n'
                        '    elif name == "evidence-root":\n'
                        '        if scenario == "replace_child_with_service_owned_dir_before_fchown":\n'
                        '            os.rmdir(name, dir_fd=dir_fd)\n'
                        '            boxes = [d for d in os.listdir(state_dir) if d.startswith(".stage-box-")]\n'
                        '            os.rename(pre_existing, os.path.join(state_dir, boxes[0], name))\n'
                        '        elif scenario == "replace_box_entry_before_publish":\n'
                        '            boxes = [d for d in os.listdir(state_dir) if d.startswith(".stage-box-")]\n'
                        '            os.rename(os.path.join(state_dir, boxes[0]), os.path.join(state_dir, boxes[0] + "-moved"))\n'
                        '            os.rename(pre_existing, os.path.join(state_dir, boxes[0]))\n'
                        '        elif scenario == "create_leaf_before_publish":\n'
                        '            os.rename(pre_existing, os.path.join(state_dir, "credential-monitor-evidence"))\n'
                        'def hooked_stat(path_arg, *, dir_fd=None, follow_symlinks=True):\n'
                        '    global published\n'
                        '    if scenario == "replace_leaf_after_publish" and path_arg == "credential-monitor-evidence" and os.path.exists(os.path.join(state_dir, "credential-monitor-evidence")) and not published:\n'
                        '        published = True\n'
                        '        os.rmdir(os.path.join(state_dir, "credential-monitor-evidence"))\n'
                        '        os.rename(pre_existing, os.path.join(state_dir, "credential-monitor-evidence"))\n'
                        '    return _wrap(real_stat(path_arg, dir_fd=dir_fd, follow_symlinks=follow_symlinks))\n'
                        'os.mkdir = hooked_mkdir\n'
                        'os.stat = hooked_stat\n'
                    ) + self.block

                    res = subprocess.run(
                        [sys.executable, '-', str(path), 'active', 'apply', str(repo), str(evidence_root)],
                        input=harness,
                        text=True,
                        capture_output=True,
                        timeout=15,
                    )
                    self.assertNotEqual(res.returncode, 0, f'{scenario} should fail closed')
                    self.assertIn('credential_monitor_staging_failed', res.stderr)
                    self.assertEqual(path.read_text(), original)
                    # Verify pre_existing directory was never deleted or mutated regardless of where it was moved.
                    matching_dirs = [
                        p for p in state_dir.rglob('*')
                        if p.is_dir() and p.lstat().st_ino == pre_ino
                    ]
                    self.assertEqual(len(matching_dirs), 1, f'unrelated replacement deleted in {scenario}')
                    after_pre = matching_dirs[0].lstat()
                    self.assertEqual(stat.S_IMODE(after_pre.st_mode), 0o755)
                    self.assertEqual((after_pre.st_uid, after_pre.st_gid), (pre_stat.st_uid, pre_stat.st_gid))
                    self.assertEqual(list(matching_dirs[0].iterdir()), [])

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
