"""Offline-only staging proof; actual git/files/fsync/rename, synthetic inputs."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'scripts' / 'stage-credential-monitor.py'
spec = importlib.util.spec_from_file_location('stage_credential_monitor', SCRIPT)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class StagingTests(unittest.TestCase):
    def setUp(self):
        tmp_root = ROOT / '.local' / 'tmp'
        tmp_root.mkdir(parents=True, exist_ok=True)
        self.base = Path(tempfile.mkdtemp(prefix='offline-', dir=tmp_root))
        self.addCleanup(shutil.rmtree, self.base)
        self.repo = self.base / 'checkout'; self.repo.mkdir(mode=0o700)
        self.parent = self.base / 'boundaries'; self.parent.mkdir(mode=0o700)
        self.target = self.parent / 'new-boundary'
        for name in m.ARTIFACTS:
            path = self.repo / name; path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(('synthetic authority ' + name).encode()); path.chmod(0o600)
        self.git('init', '-q')
        self.git('add', '.')
        self.git('-c', 'user.name=Offline Fixture', '-c', 'user.email=fixture@example.invalid',
                 '-c', 'commit.gpgsign=false', '-c', 'core.hooksPath=/dev/null', 'commit', '-qm', 'synthetic authority')
        self.commit = self.git('rev-parse', 'HEAD').strip()
        self.hashes = {p: hashlib.sha256((self.repo / p).read_bytes()).hexdigest() for p in m.ARTIFACTS}

    def git(self, *args):
        return subprocess.check_output(['/usr/bin/git', '-C', str(self.repo), *args], stderr=subprocess.DEVNULL, text=True)

    def stage(self, **changes):
        return m.stage(changes.get('checkout', self.repo), changes.get('destination', self.target),
                       changes.get('expected_commit', self.commit), changes.get('expected_hashes', self.hashes))

    def refused(self, **changes):
        with self.assertRaises((m.Refused, OSError)):
            self.stage(**changes)

    def test_exact_new_boundary_canonical_private_and_not_activation(self):
        receipt = self.stage()
        self.assertIs(receipt['activated'], False); self.assertIs(receipt['historical_coverage'], False)
        self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), 0o700)
        self.assertEqual(set(p.name for p in self.target.iterdir()), {'enrollment.json', 'journal.json'})
        values = {}
        for p in self.target.iterdir():
            raw = p.read_bytes(); values[p.name] = json.loads(raw)
            self.assertEqual(raw, m.canonical(values[p.name]) + b'\n')
            self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)
            self.assertEqual(p.stat().st_nlink, 1)
        e, j = values['enrollment.json'], values['journal.json']
        self.assertEqual(e['source_id'], hashlib.sha256(m.canonical(e['artifacts'])).hexdigest())
        self.assertEqual(e['release_sha'], self.commit)
        self.assertEqual([a['path'] for a in e['artifacts']], [str(self.repo / p) for p in m.ARTIFACTS])
        self.assertEqual((j['source_id'], j['enrollment_id']), (e['source_id'], e['enrollment_id']))
        self.assertEqual((j['occurrence_count'], j['started_total'], j['finished_total']), (0, 0, 0))
        before = {p.name: p.read_bytes() for p in self.target.iterdir()}
        self.refused()
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.target.iterdir()})

    def test_wrong_commit_hash_missing_or_extra_artifacts_refused(self):
        self.refused(expected_commit='f' * 40)
        self.refused(expected_commit='HEAD')
        self.refused(expected_hashes={**self.hashes, m.ARTIFACTS[0]: 'f' * 64})
        self.refused(expected_hashes={k: v for k, v in self.hashes.items() if k != m.ARTIFACTS[0]})
        self.refused(expected_hashes={**self.hashes, 'secret': 'a' * 64})
        self.assertFalse(self.target.exists())
        self.assertEqual(list(self.parent.iterdir()), [])

    def test_artifact_symlink_hardlink_fifo_and_writable_refused(self):
        p = self.repo / m.ARTIFACTS[0]; raw = p.read_bytes()
        victim = self.base / 'synthetic-victim'; victim.write_bytes(raw); victim.chmod(0o600)
        p.unlink(); p.symlink_to(victim); self.refused(); p.unlink()
        os.link(victim, p); self.refused(); p.unlink()
        os.mkfifo(p, 0o600); self.refused(); p.unlink()
        p.write_bytes(raw); p.chmod(0o620); self.refused()
        self.assertEqual(victim.read_bytes(), raw)
        self.assertFalse(self.target.exists())

    def test_artifact_owner_refused(self):
        real = m.os.fstat
        def fstat(fd):
            info = real(fd)
            if stat.S_ISREG(info.st_mode):
                fields = list(info); fields[4] = 123456
                return os.stat_result(fields)
            return info
        with mock.patch.object(m.os, 'fstat', side_effect=fstat):
            self.refused()
        self.assertFalse(self.target.exists())

    def test_symlink_ancestors_and_nonprivate_parent_refused(self):
        alias = self.base / 'alias'; alias.symlink_to(self.repo, target_is_directory=True)
        self.refused(checkout=alias)
        link = self.base / 'linked-parent'; link.symlink_to(self.parent, target_is_directory=True)
        self.refused(destination=link / 'new-boundary')
        self.parent.chmod(0o750); self.refused()
        self.assertFalse(self.target.exists())

    def test_existing_symlink_regular_fifo_or_directory_never_overwritten(self):
        victim = self.base / 'victim'; victim.write_text('untouched')
        self.target.symlink_to(victim); self.refused(); self.target.unlink()
        self.target.write_text('existing'); self.refused(); self.assertEqual(self.target.read_text(), 'existing'); self.target.unlink()
        os.mkfifo(self.target, 0o600); self.refused(); self.target.unlink()
        self.target.mkdir(mode=0o700); self.refused()
        self.assertEqual(victim.read_text(), 'untouched')

    def test_raced_target_publication_is_exclusive(self):
        publish = m.publish
        def race(parent, staging, target):
            os.mkdir(target, 0o700, dir_fd=parent)
            return publish(parent, staging, target)
        with mock.patch.object(m, 'publish', side_effect=race):
            self.refused()
        self.assertEqual(list(self.target.iterdir()), [])
        self.assertEqual(len(list(self.parent.glob('.stage-*'))), 1)

    def test_rechecks_commit_and_bytes_before_publication(self):
        real = m.write_new
        def changed(fd, name, value):
            real(fd, name, value)
            if name == 'journal.json':
                (self.repo / m.ARTIFACTS[0]).write_text('changed after first verification')
        with mock.patch.object(m, 'write_new', side_effect=changed):
            self.refused()
        self.assertFalse(self.target.exists())
        with mock.patch.object(m, 'head', side_effect=[self.commit, 'f' * 40]):
            # Restore exact bytes to isolate the changed-HEAD check.
            (self.repo / m.ARTIFACTS[0]).write_bytes(('synthetic authority ' + m.ARTIFACTS[0]).encode())
            self.refused()
        self.assertFalse(self.target.exists())

    def test_file_directory_fsync_failure_never_reports_success(self):
        real = m.os.fsync
        for phase in ('file', 'directory', 'after_publish'):
            with self.subTest(phase=phase):
                if self.target.exists(): shutil.rmtree(self.target)
                def failing(fd):
                    info = os.fstat(fd)
                    match = (phase == 'file' and stat.S_ISREG(info.st_mode) or
                             phase == 'directory' and stat.S_ISDIR(info.st_mode) or
                             phase == 'after_publish' and self.target.exists())
                    if match: raise OSError('synthetic fsync failure')
                    return real(fd)
                with mock.patch.object(m.os, 'fsync', side_effect=failing): self.refused()
                if phase == 'after_publish':
                    self.assertTrue(self.target.exists())
                    self.refused()  # No retry can bless uncertain published state.
                else: self.assertFalse(self.target.exists())

    def test_sigkill_incomplete_staging_never_appears_as_target(self):
        code = '''import importlib.util,os,signal,sys
spec=importlib.util.spec_from_file_location('m',sys.argv[1]);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
original=m.write_new
def interrupted(fd,name,value):
 original(fd,name,value)
 os.kill(os.getpid(),signal.SIGKILL)
m.write_new=interrupted
m.stage(sys.argv[2],sys.argv[3],sys.argv[4],dict(zip(m.ARTIFACTS,sys.argv[5:])))
'''
        result = subprocess.run([sys.executable, '-I', '-S', '-c', code, str(SCRIPT), str(self.repo), str(self.target),
                                 self.commit, *[self.hashes[p] for p in m.ARTIFACTS]], timeout=10,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        self.assertEqual(result.returncode, -signal.SIGKILL)
        self.assertFalse(self.target.exists())
        partial = list(self.parent.glob('.stage-*')); self.assertEqual(len(partial), 1)
        self.assertEqual(set(p.name for p in partial[0].iterdir()), {'enrollment.json'})
        receipt = self.stage()  # A fresh boundary; no adoption/healing of partial.
        self.assertIs(receipt['historical_coverage'], False)
        self.assertTrue(partial[0].exists())

    def test_cli_only_stages_and_refuses_replay(self):
        argv = [sys.executable, '-I', '-S', str(SCRIPT), '--checkout', str(self.repo), '--destination', str(self.target),
                '--expected-commit', self.commit, '--observer-sha256', self.hashes[m.ARTIFACTS[0]],
                '--main-sha256', self.hashes[m.ARTIFACTS[1]], '--worker-sha256', self.hashes[m.ARTIFACTS[2]]]
        result = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIs(json.loads(result.stdout)['activated'], False)
        result = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
        self.assertEqual(result.returncode, 2); self.assertEqual(result.stdout, b'')


if __name__ == '__main__': unittest.main()
