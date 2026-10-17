"""M15d host cleanup suites (synthetic only, no live sessions/worktrees).

All git fixtures are disposable synthetic repos wholly inside the owned
private test TMPDIR (<repo>/.local/tmp); real worktrees, histories, and
sources are never touched. Covers: trusted root confinement, dedicated
registered-worktree checks, clean/merge guards (fresh ls-remote ancestry),
live-use refusal, journal idempotency (restart never repeats deletion), the
fleet `cleanup_roots` allowlist, and the CLI --apply/--tree transport.
"""

import argparse
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_hub_host import fleet, session_control
from agent_hub_host.native_sessions import SessionControlError
from agent_hub_host.session_cleanup import (
    canonical_roots,
    remove_owned_worktree,
    resolve_owned_worktree,
)
from agent_hub_host.session_executor import CallerCustody

REPO_TMP = Path(__file__).resolve().parents[2] / ".local" / "tmp"


def run_git(args, cwd):
    proc = subprocess.run(
        ["git", *args], capture_output=True, text=True, timeout=60, check=False, cwd=str(cwd),
        env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"},
    )
    if proc.returncode != 0:
        raise AssertionError("git %r failed in %s: %s" % (args, cwd, (proc.stderr or "")[-500:]))
    return proc


def custody(machine="h"):
    return CallerCustody(actor="machine:" + machine, machine_id=machine, credential_family="host-" + machine)


def make_repo(base):
    """Synthetic bare origin + main checkout with one pushed main commit."""
    origin = base / "origin.git"
    main = base / "main"
    run_git(["init", "--bare", str(origin)], base)
    run_git(["clone", str(origin), str(main)], base)
    run_git(["config", "user.email", "synth@example.test"], main)
    run_git(["config", "user.name", "synth"], main)
    (main / "seed.txt").write_text("seed\n", encoding="utf-8")
    run_git(["add", "seed.txt"], main)
    run_git(["-c", "user.email=synth@example.test", "-c", "user.name=synth", "commit", "-m", "seed"], main)
    run_git(["push", "origin", "main"], main)
    run_git(["config", "user.email", "synth@example.test"], main)
    run_git(["config", "user.name", "synth"], main)
    return origin, main


def add_merged_worktree(main, base, name, branch):
    wt = base / name
    run_git(["worktree", "add", str(wt), "-b", branch], main)
    run_git(["config", "user.email", "synth@example.test"], wt)
    run_git(["config", "user.name", "synth"], wt)
    (wt / "work.txt").write_text("work\n", encoding="utf-8")
    run_git(["add", "work.txt"], wt)
    run_git(["-c", "user.email=synth@example.test", "-c", "user.name=synth", "commit", "-m", "work"], wt)
    run_git(["checkout", "main"], main)
    run_git(["merge", "--ff-only", branch], main)
    run_git(["push", "origin", "main"], main)
    run_git(["checkout", branch], wt)
    return wt


class TestRootsConfinement(unittest.TestCase):
    def test_roots_validated(self):
        self.assertEqual(canonical_roots(["/srv/a"]), ["/srv/a"])
        for bad in ([], ["rel"], ["/a"] * 17, ["/a", "/a"], ["/a", "rel"], "/a", None):
            with self.assertRaises(SessionControlError, msg=repr(bad)):
                canonical_roots(bad)

    def test_escape_symlink_and_root_refused(self):
        base = Path(tempfile.mkdtemp(prefix="cleanup-roots-", dir=str(REPO_TMP)))
        self.addCleanup(shutil.rmtree, str(base), True)
        inside = base / "wt"
        inside.mkdir()
        self.assertEqual(resolve_owned_worktree(str(inside), [str(base)]), str(inside))
        with self.assertRaises(SessionControlError):
            resolve_owned_worktree(str(base / ".." / "elsewhere"), [str(base)])
        with self.assertRaises(SessionControlError):
            resolve_owned_worktree(str(base), [str(base)])
        link = base / "link"
        try:
            os.symlink(str(inside), str(link))
        except OSError:
            self.skipTest("symlinks unavailable")
        with self.assertRaises(SessionControlError):
            resolve_owned_worktree(str(link), [str(base)])


class TestTreeRemoval(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="cleanup-m15d-", dir=str(REPO_TMP)))
        self.journal = self.base / "journals"
        self.journal.mkdir()
        self.origin, self.main = make_repo(self.base)

    def tearDown(self):
        run_git(["worktree", "prune"], self.main)
        shutil.rmtree(str(self.base), ignore_errors=True)

    def test_clean_merged_tree_removed_then_replayed(self):
        wt = add_merged_worktree(self.main, self.base, "wt1", "feat-x")
        out = remove_owned_worktree(
            custody=custody(), journal_dir=str(self.journal), machine_id="h",
            native_id="22222222-2222-4222-8222-222222222222", claim_holder="h", stop_confirmed=True,
            canonical_workspace=str(wt), cleanup_roots=[str(self.base)], live_paths=[])
        self.assertEqual(out["state"], "tree_removed")
        self.assertFalse(wt.exists())
        listed = run_git(["branch", "--list", "feat-x"], self.main).stdout
        self.assertNotIn("feat-x", listed)
        again = remove_owned_worktree(
            custody=custody(), journal_dir=str(self.journal), machine_id="h",
            native_id="22222222-2222-4222-8222-222222222222", claim_holder="h", stop_confirmed=True,
            canonical_workspace=str(wt), cleanup_roots=[str(self.base)], live_paths=[])
        self.assertEqual(again["state"], "tree_removed")
        self.assertTrue(again["replayed"])

    def test_dirty_tree_kept(self):
        wt = add_merged_worktree(self.main, self.base, "wt1", "feat-x")
        (wt / "dirty.txt").write_text("untracked\n", encoding="utf-8")
        out = remove_owned_worktree(
            custody=custody(), journal_dir=str(self.journal), machine_id="h",
            native_id="33333333-3333-4333-8333-333333333333", claim_holder="h", stop_confirmed=True,
            canonical_workspace=str(wt), cleanup_roots=[str(self.base)], live_paths=[])
        self.assertEqual(out["state"], "refused")
        self.assertIn("dirty", out["reason"])
        self.assertTrue(wt.exists())

    def test_unmerged_tree_kept(self):
        wt = self.base / "wt1"
        run_git(["worktree", "add", str(wt), "-b", "feat-y"], self.main)
        run_git(["config", "user.email", "synth@example.test"], wt)
        run_git(["config", "user.name", "synth"], wt)
        (wt / "solo.txt").write_text("solo\n", encoding="utf-8")
        run_git(["add", "solo.txt"], wt)
        run_git(["-c", "user.email=synth@example.test", "-c", "user.name=synth", "commit", "-m", "solo"], wt)
        out = remove_owned_worktree(
            custody=custody(), journal_dir=str(self.journal), machine_id="h",
            native_id="44444444-4444-4444-8444-444444444444", claim_holder="h", stop_confirmed=True,
            canonical_workspace=str(wt), cleanup_roots=[str(self.base)], live_paths=[])
        self.assertEqual(out["state"], "refused")
        self.assertIn("unmerged", out["reason"])
        self.assertTrue(wt.exists())

    def test_broken_remote_fails_closed(self):
        wt = add_merged_worktree(self.main, self.base, "wt1", "feat-x")
        run_git(["remote", "set-url", "origin", str(self.base / "missing.git")], wt)
        out = remove_owned_worktree(
            custody=custody(), journal_dir=str(self.journal), machine_id="h",
            native_id="55555555-5555-4555-8555-555555555555", claim_holder="h", stop_confirmed=True,
            canonical_workspace=str(wt), cleanup_roots=[str(self.base)], live_paths=[])
        self.assertEqual(out["state"], "refused")
        self.assertIn("remote_unavailable", out["reason"])
        self.assertTrue(wt.exists())

    def test_live_use_and_shared_checkout_refused(self):
        wt = add_merged_worktree(self.main, self.base, "wt1", "feat-x")
        out = remove_owned_worktree(
            custody=custody(), journal_dir=str(self.journal), machine_id="h",
            native_id="66666666-6666-4666-8666-666666666666", claim_holder="h", stop_confirmed=True,
            canonical_workspace=str(wt), cleanup_roots=[str(self.base)], live_paths=[str(wt)])
        self.assertEqual(out["state"], "refused")
        self.assertIn("live session", out["reason"])
        main_out = remove_owned_worktree(
            custody=custody(), journal_dir=str(self.journal), machine_id="h",
            native_id="77777777-7777-4777-8777-777777777777", claim_holder="h", stop_confirmed=True,
            canonical_workspace=str(self.main), cleanup_roots=[str(self.base)], live_paths=[])
        self.assertEqual(main_out["state"], "refused")
        self.assertTrue(self.main.exists())

    def test_branch_checked_out_elsewhere_refused(self):
        wt = add_merged_worktree(self.main, self.base, "wt1", "feat-x")
        wt2 = self.base / "wt2"
        # Fixture construction ONLY: force a second checkout of the same
        # branch to stage the unsafe state. The module under test never uses
        # --force (it must refuse exactly this state).
        run_git(["worktree", "add", "--force", str(wt2), "feat-x"], self.main)
        try:
            out = remove_owned_worktree(
                custody=custody(), journal_dir=str(self.journal), machine_id="h",
                native_id="88888888-8888-4888-8888-888888888888", claim_holder="h", stop_confirmed=True,
                canonical_workspace=str(wt), cleanup_roots=[str(self.base)], live_paths=[])
            self.assertEqual(out["state"], "refused")
            self.assertIn("another worktree", out["reason"])
            self.assertTrue(wt.exists())
        finally:
            run_git(["worktree", "remove", "--force", str(wt2)], self.main)

    def test_branch_discovered_from_worktree_not_caller(self):
        wt = add_merged_worktree(self.main, self.base, "wt1", "feat-x")
        out = remove_owned_worktree(
            custody=custody(), journal_dir=str(self.journal), machine_id="h",
            native_id="99999999-9999-4999-8999-999999999999", claim_holder="h", stop_confirmed=True,
            canonical_workspace=str(wt), cleanup_roots=[str(self.base)], live_paths=[])
        self.assertEqual(out["state"], "tree_removed")
        self.assertEqual(out["branch"], "feat-x")
        self.assertEqual(out["workspace"], str(wt))

    def test_foreign_claim_holder_and_unconfirmed_stop_raise(self):
        wt = add_merged_worktree(self.main, self.base, "wt1", "feat-x")
        with self.assertRaises(SessionControlError):
            remove_owned_worktree(
                custody=custody(), journal_dir=str(self.journal), machine_id="h",
                native_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", claim_holder="other", stop_confirmed=True,
                canonical_workspace=str(wt), cleanup_roots=[str(self.base)], live_paths=[])
        with self.assertRaises(SessionControlError):
            remove_owned_worktree(
                custody=custody(), journal_dir=str(self.journal), machine_id="h",
                native_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", claim_holder="h", stop_confirmed=False,
                canonical_workspace=str(wt), cleanup_roots=[str(self.base)], live_paths=[])
        self.assertTrue(wt.exists())

    def test_detached_head_refused(self):
        wt = self.base / "wt1"
        run_git(["worktree", "add", "--detach", str(wt)], self.main)
        try:
            out = remove_owned_worktree(
                custody=custody(), journal_dir=str(self.journal), machine_id="h",
                native_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", claim_holder="h", stop_confirmed=True,
                canonical_workspace=str(wt), cleanup_roots=[str(self.base)], live_paths=[])
            self.assertEqual(out["state"], "refused")
            self.assertIn("detached", out["reason"])
            self.assertTrue(wt.exists())
        finally:
            run_git(["worktree", "remove", "--force", str(wt)], self.main)

    def test_workspace_idempotency_conflict(self):
        wt = add_merged_worktree(self.main, self.base, "wt1", "feat-x")
        out = remove_owned_worktree(
            custody=custody(), journal_dir=str(self.journal), machine_id="h",
            native_id="cccccccc-cccc-4ccc-8ccc-cccccccccccc", claim_holder="h", stop_confirmed=True,
            canonical_workspace=str(wt), cleanup_roots=[str(self.base)], live_paths=[])
        self.assertEqual(out["state"], "tree_removed")
        with self.assertRaises(SessionControlError):
            remove_owned_worktree(
                custody=custody(), journal_dir=str(self.journal), machine_id="h",
                native_id="cccccccc-cccc-4ccc-8ccc-cccccccccccc", claim_holder="h", stop_confirmed=True,
                canonical_workspace=str(self.base / "other"), cleanup_roots=[str(self.base)], live_paths=[])


class TestFleetAllowlistAndCli(unittest.TestCase):
    def test_cleanup_roots_settings_plumbing(self):
        import json

        cfg = Path(tempfile.mkdtemp(prefix="cleanup-cfg-", dir=str(REPO_TMP)))
        self.addCleanup(shutil.rmtree, str(cfg), True)
        settings = {
            "version": 1, "subscriptions": [], "workspaces": {},
            "cleanup_roots": [str(cfg / "roots" / "a")],
        }
        path = cfg / "subscriptions.json"
        path.write_text(json.dumps(settings), encoding="utf-8")
        loaded = fleet.load_settings(str(path))
        self.assertEqual(loaded["cleanup_roots"], settings["cleanup_roots"])
        path.write_text(json.dumps({**settings, "cleanup_roots": ["rel"]}), encoding="utf-8")
        with self.assertRaises(ValueError):
            fleet.load_settings(str(path))

    def test_cli_apply_posts_key_and_tree_disabled_before_mutation(self):
        root = argparse.ArgumentParser()
        sub = root.add_subparsers(dest="cmd", required=True)
        session_control.add_commands(sub)

        def parse(*args):
            return root.parse_args(["sessions", *args])

        with patch.object(session_control, "request", return_value={"apply": True}) as transport:
            session_control.execute(parse("cleanup", "--apply", "--key", "op-1"))
        self.assertEqual(transport.call_args.args[2], {"apply": True, "key": "op-1"})
        # --tree (bare or fully addressed) is refused BEFORE any POST: no
        # client-local Git execution exists anymore.
        with patch.object(session_control, "request") as transport:
            with self.assertRaises(ValueError):
                session_control.execute(parse("cleanup", "--apply", "--tree"))
        transport.assert_not_called()
        with patch.object(session_control, "request") as transport:
            with self.assertRaises(ValueError):
                session_control.execute(
                    parse("cleanup", "--apply", "--tree", "--machine-id", "h",
                          "--native-id", "22222222-2222-4222-8222-222222222222",
                          "--owner", "demo:assistant", "--worktree", "/srv/wt",
                          "--branch", "feat-x", "--journal-dir", "/srv/j",
                          "--cleanup-root", "/srv"))
        transport.assert_not_called()
        # Defense in depth: the executor stub raises even if called directly.
        with self.assertRaises(ValueError):
            session_control._execute_cleanup_tree(parse("cleanup", "--apply", "--tree"), {})


if __name__ == "__main__":
    unittest.main()
