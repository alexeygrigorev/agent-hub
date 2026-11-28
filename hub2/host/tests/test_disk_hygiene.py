"""Tests for agent_hub_host.disk_hygiene (AC1 & AC5)."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_hub_host import disk_hygiene


def _init_git_worktree(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(['git', 'init', '-q'], cwd=path, check=True)
    subprocess.run(['git', 'config', 'user.email', 'test@example.com'], cwd=path, check=True)
    subprocess.run(['git', 'config', 'user.name', 'Test'], cwd=path, check=True)
    (path / 'tracked.py').write_text('print(1)\n', encoding='utf-8')
    subprocess.run(['git', 'add', 'tracked.py'], cwd=path, check=True)
    subprocess.run(['git', 'commit', '-q', '-m', 'init'], cwd=path, check=True)


class DiskHygieneTests(unittest.TestCase):
    def test_dry_run_and_apply_clean_only_untracked_artifacts_on_idle_finished_worktree(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            wt = home / 'git' / 'demo-wt-123'
            _init_git_worktree(wt)
            # Rebuildable untracked artifacts
            (wt / '.venv' / 'lib').mkdir(parents=True)
            (wt / '.venv' / 'lib' / 'pkg.bin').write_bytes(b'x' * 4096)
            (wt / 'node_modules' / 'pkg').mkdir(parents=True)
            (wt / 'node_modules' / 'pkg' / 'index.js').write_bytes(b'y' * 2048)
            (wt / 'target' / 'debug').mkdir(parents=True)
            (wt / 'target' / 'debug' / 'app').write_bytes(b'z' * 8192)
            (wt / '__pycache__').mkdir()
            (wt / '__pycache__' / 'a.pyc').write_bytes(b'p' * 512)
            # Tracked build/ file must NEVER be deleted
            (wt / 'build').mkdir()
            (wt / 'build' / 'kept.txt').write_text('tracked\n', encoding='utf-8')
            subprocess.run(['git', 'add', 'build/kept.txt'], cwd=wt, check=True)
            subprocess.run(['git', 'commit', '-q', '-m', 'track build'], cwd=wt, check=True)
            # Dirty tracked file must NEVER be deleted
            (wt / 'tracked.py').write_text('print(2)\n', encoding='utf-8')

            now = 1_800_000_000.0
            sessions_file = home / 'sessions.json'
            sessions_file.write_text(json.dumps({
                'items': [{
                    'id': 's1',
                    'state': 'completed',
                    'workspace': str(wt),
                    'updated_at': now - 30 * 3600,
                }],
                'next_cursor': None,
            }), encoding='utf-8')

            dry = disk_hygiene.run_hygiene(
                apply=False, home=home, sessions_json=sessions_file, now_s=now, mount_path=home, include_docker=False,
            )
            self.assertEqual(dry['mode'], 'dry-run')
            self.assertGreater(dry['total_bytes'], 0)
            self.assertTrue((wt / '.venv').exists())
            self.assertTrue((wt / 'target').exists())
            self.assertIn('DAILY_HYGIENE_SUMMARY', dry['daily_line'])

            applied = disk_hygiene.run_hygiene(
                apply=True, home=home, sessions_json=sessions_file, now_s=now, mount_path=home, include_docker=False,
            )
            self.assertEqual(applied['mode'], 'apply')
            self.assertFalse((wt / '.venv').exists())
            self.assertFalse((wt / 'node_modules').exists())
            self.assertFalse((wt / 'target').exists())
            self.assertFalse((wt / '__pycache__').exists())
            # Worktree, tracked build/, and dirty tracked.py are preserved
            self.assertTrue(wt.is_dir())
            self.assertTrue((wt / 'build' / 'kept.txt').exists())
            self.assertEqual((wt / 'tracked.py').read_text(encoding='utf-8'), 'print(2)\n')
            self.assertGreater(applied['categories_bytes']['venv'], 0)
            self.assertGreater(applied['categories_bytes']['rust_target'], 0)

    def test_skips_active_recent_open_and_denylisted_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            wt_recent = home / 'git' / 'demo-wt-recent'
            wt_open = home / 'git' / 'demo-wt-open'
            relay_wt = home / 'git' / 'relay'
            for p in (wt_recent, wt_open, relay_wt):
                _init_git_worktree(p)
                (p / '.venv').mkdir()
                (p / '.venv' / 'x').write_bytes(b'1' * 1024)

            now = 1_800_000_000.0
            sessions_file = home / 'sessions.json'
            sessions_file.write_text(json.dumps({
                'items': [
                    {'id': 's-recent', 'state': 'stopped', 'workspace': str(wt_recent), 'updated_at': now - 2 * 3600},
                    {'id': 's-open', 'state': 'failed', 'workspace': str(wt_open), 'updated_at': now - 48 * 3600},
                    {'id': 's-relay', 'state': 'archived', 'workspace': str(relay_wt), 'updated_at': now - 48 * 3600},
                ],
                'next_cursor': None,
            }), encoding='utf-8')

            with mock.patch.object(disk_hygiene, 'scan_open_paths', return_value=({wt_open}, True)):
                res = disk_hygiene.run_hygiene(
                    apply=True, home=home, sessions_json=sessions_file,
                    extra_worktrees=[relay_wt], now_s=now, mount_path=home, include_docker=False,
                )
            self.assertEqual(res['total_bytes'], 0)
            self.assertTrue((wt_recent / '.venv').exists())
            self.assertTrue((wt_open / '.venv').exists())
            self.assertTrue((relay_wt / '.venv').exists())

    def test_managed_engines_retention_keeps_current_and_prunes_superseded(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            managed = home / '.config' / 'agent-hub' / 'managed-engines'
            managed.mkdir(parents=True)
            cur = managed / 'shared-codex-rev2'
            old = managed / 'shared-codex-rev1'
            cur_cache = cur / 'home' / '.cache' / 'uv'
            cur.mkdir()
            old.mkdir()
            cur_cache.mkdir(parents=True)
            (old / 'bin.dat').write_bytes(b'o' * 4096)
            (cur / 'bin.dat').write_bytes(b'c' * 4096)
            (cur_cache / 'wheel.whl').write_bytes(b'w' * 2048)
            (managed / 'state.json').write_text(json.dumps([
                {'label': 'shared-codex', 'revision': 'rev2', 'provider': 'codex'},
            ]), encoding='utf-8')

            sessions_file = home / 'sessions.json'
            sessions_file.write_text(json.dumps({'items': [], 'next_cursor': None}), encoding='utf-8')

            res = disk_hygiene.run_hygiene(
                apply=True, home=home, sessions_json=sessions_file, managed_root=managed, mount_path=home,
                include_docker=False,
            )
            self.assertTrue(cur.exists())
            self.assertFalse(old.exists())
            self.assertFalse(cur_cache.exists())
            self.assertGreater(res['categories_bytes']['managed_engines'], 0)

    def test_min_used_pct_gate_and_denylist_and_project_dirs(self):
        import os
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            academy = home / 'git' / 'academy'
            relay = home / 'git' / 'relay'
            reports = home / 'tmp' / 'agent-reports'
            recs = home / 'recordings'
            work_proj = home / 'work' / 'proj-a'
            for d in (academy, relay, reports, recs, work_proj):
                (d / 'node_modules' / 'pkg').mkdir(parents=True)
                (d / 'node_modules' / 'pkg' / 'a.js').write_bytes(b'x' * 1024)
            (work_proj / 'data.sqlite').write_bytes(b'db' * 512)
            (work_proj / '.env').write_text('SECRET=1\n', encoding='utf-8')

            now = 1_800_000_000.0
            old_ts = now - 48 * 3600
            for root_dir, _, files in os.walk(work_proj):
                os.utime(root_dir, (old_ts, old_ts))
                for f in files:
                    os.utime(Path(root_dir) / f, (old_ts, old_ts))

            # Below 85% threshold -> skipped
            with mock.patch.object(disk_hygiene, 'measure_mount_used_pct', return_value=80.0), \
                 mock.patch.object(disk_hygiene, 'load_hub_sessions', return_value=None):
                skipped_res = disk_hygiene.run_hygiene(
                    apply=True, home=home, min_used_pct=85.0, include_docker=False, now_s=now, mount_path=home,
                )
            self.assertEqual(skipped_res['total_bytes'], 0)
            self.assertTrue((work_proj / 'node_modules').exists())

            # At >= 85% threshold -> cleans work_proj/node_modules, never touches denylist or DB/.env
            with mock.patch.object(disk_hygiene, 'measure_mount_used_pct', return_value=86.0), \
                 mock.patch.object(disk_hygiene, 'load_hub_sessions', return_value=None):
                res = disk_hygiene.run_hygiene(
                    apply=True, home=home, min_used_pct=85.0, include_docker=False, now_s=now, mount_path=home,
                )
            self.assertFalse((work_proj / 'node_modules').exists())
            self.assertTrue((work_proj / 'data.sqlite').exists())
            self.assertTrue((work_proj / '.env').exists())
            self.assertTrue((academy / 'node_modules').exists())
            self.assertTrue((relay / 'node_modules').exists())
            self.assertTrue((reports / 'node_modules').exists())
            self.assertTrue((recs / 'node_modules').exists())
            self.assertGreater(res['categories_bytes']['node_modules'], 0)

    def test_prunes_merged_clean_worktree_but_preserves_unpushed_worktree(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            origin = home / 'origin.git'
            subprocess.run(['git', 'init', '--bare', '-q', str(origin)], check=True)
            main_repo = home / 'git' / 'demo-repo'
            _init_git_worktree(main_repo)
            subprocess.run(['git', 'branch', '-M', 'main'], cwd=main_repo, check=True)
            subprocess.run(['git', 'remote', 'add', 'origin', str(origin)], cwd=main_repo, check=True)
            subprocess.run(['git', 'push', '-q', '-u', 'origin', 'main'], cwd=main_repo, check=True)

            wt_merged = main_repo / '.local' / 'hub-worktrees' / ('a' * 64)
            wt_unpushed = main_repo / '.local' / 'hub-worktrees' / ('b' * 64)
            wt_merged.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(['git', 'worktree', 'add', '-q', '-b', 'merged-br', str(wt_merged), 'origin/main'], cwd=main_repo, check=True)
            subprocess.run(['git', 'worktree', 'add', '-q', '-b', 'unpushed-br', str(wt_unpushed), 'origin/main'], cwd=main_repo, check=True)
            (wt_unpushed / 'new.py').write_text('print(2)\n', encoding='utf-8')
            subprocess.run(['git', 'add', 'new.py'], cwd=wt_unpushed, check=True)
            subprocess.run(['git', 'commit', '-q', '-m', 'unpushed'], cwd=wt_unpushed, check=True)

            now = 1_800_000_000.0
            sessions_file = home / 'sessions.json'
            sessions_file.write_text(json.dumps({
                'items': [
                    {'id': 's1', 'state': 'completed', 'workspace': str(wt_merged), 'updated_at': now - 48 * 3600},
                    {'id': 's2', 'state': 'completed', 'workspace': str(wt_unpushed), 'updated_at': now - 48 * 3600},
                ],
                'next_cursor': None,
            }), encoding='utf-8')

            res = disk_hygiene.run_hygiene(
                apply=True, home=home, sessions_json=sessions_file, include_docker=False, now_s=now, mount_path=home,
            )
            self.assertFalse(wt_merged.exists())
            self.assertTrue(wt_unpushed.exists())
            self.assertGreater(res['categories_bytes']['stale_worktrees'], 0)


if __name__ == '__main__':
    unittest.main()
