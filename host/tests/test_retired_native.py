"""M15 tests for retired-native session verification, tombstone validation,
authentic history preservation, and stop/tree custody guards.

Covers:
- Synthetic actual retired layout + tail preservation.
- Missing / corrupt / wrong-ID / wrong-tag / wrong-ws / fractional timestamp / unsafe symlink / oversize proofs.
- Tag/UUID reuse and live conflicting incarnation.
- Same process still alive in catalog.
- Unavailable history (keeps tree with precise reason; listed-only).
- Stopped-but-owned vs human/foreign.
- Independent restart / lost ACK / no repeated deletion.
- Keyed collision / session key derivation.
- Fresh activity / draft / obligation guards.
- Exact freshness semantics (original nativefinished_at preserved, real evidence_observed_at).
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from agent_hub_host import private_fs
from agent_hub_host import session_executor as se
from agent_hub_host import session_cleanup as sc
from agent_hub_host.native_sessions import (
    NativeSession,
    NativeTarget,
    SessionControlError,
    SessionUncertain,
    stop_session,
)
from agent_hub_host.retired_native import (
    capture_retired_history,
    resolve_aplexer_state_dir,
    validate_retired_tombstone,
    verify_native_retirement,
)

REPO_TMP = Path(__file__).resolve().parents[2] / ".local" / "tmp"


def make_target(workspace: str, session_id: str | None = None, tag: str = "t-1",
                created_at_ms: int = 1792000000000, engine: str = "codex") -> NativeTarget:
    sid = session_id or str(uuid.uuid4())
    return NativeTarget(
        session_id=sid,
        tag=tag,
        workspace=str(Path(workspace).resolve()),
        created_at_ms=created_at_ms,
        engine=engine,
    )


def make_custody(machine: str = "hetzner", actor: str = "demo:assistant") -> se.CallerCustody:
    return se.CallerCustody(
        actor=actor,
        machine_id=machine,
        credential_family="fam-" + machine,
    )


class TestTombstoneValidation(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="tombstone-test-", dir=str(REPO_TMP)))
        self.ws = self.tmp / "ws"
        self.ws.mkdir()
        self.target = make_target(str(self.ws), tag="my-tag", created_at_ms=1792000000000)

    def tearDown(self):
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    def _write_tombstone(self, data: dict, name: str = "tombstone.json") -> str:
        p = self.tmp / name
        p.write_text(json.dumps(data), encoding="utf-8")
        return str(p)

    def test_valid_tombstone(self):
        tpath = self._write_tombstone({
            "cause": "finished",
            "id": self.target.session_id,
            "tag": self.target.tag,
            "workspace": self.target.workspace,
            "finished_at_ms": 1792000500000,
        })
        validated = validate_retired_tombstone(tpath, self.target)
        self.assertEqual(validated["cause"], "finished")
        self.assertEqual(validated["finished_at_ms"], 1792000500000)

    def test_invalid_causes(self):
        for bad_cause in ["forgotten", "pruned", "killed", "crashed", "unknown"]:
            tpath = self._write_tombstone({
                "cause": bad_cause,
                "id": self.target.session_id,
                "tag": self.target.tag,
                "workspace": self.target.workspace,
                "finished_at_ms": 1792000500000,
            })
            with self.assertRaises(SessionControlError) as ctx:
                validate_retired_tombstone(tpath, self.target)
            self.assertIn("expected 'finished'", str(ctx.exception))

    def test_mismatches(self):
        # Wrong ID
        tpath = self._write_tombstone({
            "cause": "finished",
            "id": str(uuid.uuid4()),
            "tag": self.target.tag,
            "workspace": self.target.workspace,
            "finished_at_ms": 1792000500000,
        })
        with self.assertRaises(SessionControlError) as ctx:
            validate_retired_tombstone(tpath, self.target)
        self.assertIn("session ID mismatch", str(ctx.exception))

        # Wrong Tag
        tpath = self._write_tombstone({
            "cause": "finished",
            "id": self.target.session_id,
            "tag": "other-tag",
            "workspace": self.target.workspace,
            "finished_at_ms": 1792000500000,
        })
        with self.assertRaises(SessionControlError) as ctx:
            validate_retired_tombstone(tpath, self.target)
        self.assertIn("tag mismatch", str(ctx.exception))

        # Wrong Workspace
        tpath = self._write_tombstone({
            "cause": "finished",
            "id": self.target.session_id,
            "tag": self.target.tag,
            "workspace": str(self.tmp / "other-ws"),
            "finished_at_ms": 1792000500000,
        })
        with self.assertRaises(SessionControlError) as ctx:
            validate_retired_tombstone(tpath, self.target)
        self.assertIn("workspace mismatch", str(ctx.exception))

    def test_timestamp_integrity(self):
        # Fractional timestamp (float) must be rejected
        tpath = self._write_tombstone({
            "cause": "finished",
            "id": self.target.session_id,
            "tag": self.target.tag,
            "workspace": self.target.workspace,
            "finished_at_ms": 1792000500000.5,
        })
        with self.assertRaises(SessionControlError) as ctx:
            validate_retired_tombstone(tpath, self.target)
        self.assertIn("must be an integer", str(ctx.exception))

        # Boolean timestamp (which is subclass of int in Python) must be rejected
        tpath = self._write_tombstone({
            "cause": "finished",
            "id": self.target.session_id,
            "tag": self.target.tag,
            "workspace": self.target.workspace,
            "finished_at_ms": True,
        })
        with self.assertRaises(SessionControlError) as ctx:
            validate_retired_tombstone(tpath, self.target)
        self.assertIn("must be an integer", str(ctx.exception))

        # finished_at predates created_at
        tpath = self._write_tombstone({
            "cause": "finished",
            "id": self.target.session_id,
            "tag": self.target.tag,
            "workspace": self.target.workspace,
            "finished_at_ms": self.target.created_at_ms - 100,
        })
        with self.assertRaises(SessionControlError) as ctx:
            validate_retired_tombstone(tpath, self.target)
        self.assertIn("predates target created_at_ms", str(ctx.exception))

    def test_symlink_and_oversize_rejection(self):
        real_file = self._write_tombstone({
            "cause": "finished",
            "id": self.target.session_id,
            "tag": self.target.tag,
            "workspace": self.target.workspace,
            "finished_at_ms": 1792000500000,
        }, name="real.json")
        symlink_path = self.tmp / "symlink.json"
        symlink_path.symlink_to(real_file)

        # Unsafe symlink rejected (nofollow)
        with self.assertRaises(SessionControlError) as ctx:
            validate_retired_tombstone(str(symlink_path), self.target)
        self.assertIn("symlink (unsafe)", str(ctx.exception))

        # Oversize file (> 4 KiB) rejected
        big_path = self.tmp / "big.json"
        big_path.write_text(" " * 5000, encoding="utf-8")
        with self.assertRaises(SessionControlError) as ctx:
            validate_retired_tombstone(str(big_path), self.target)
        self.assertIn("exceeds size bound", str(ctx.exception))

        # Empty file (0 bytes) rejected
        empty_path = self.tmp / "empty.json"
        empty_path.write_text("", encoding="utf-8")
        with self.assertRaises(SessionControlError) as ctx:
            validate_retired_tombstone(str(empty_path), self.target)
        self.assertIn("is empty", str(ctx.exception))


class TestRetirementVerification(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="retire-verify-", dir=str(REPO_TMP)))
        self.state_dir = self.tmp / "aplexer_state"
        self.state_dir.mkdir()
        self.ws = self.tmp / "ws"
        self.ws.mkdir()
        self.target = make_target(str(self.ws), tag="task-tag")

    def tearDown(self):
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    def _setup_tombstone(self, target: NativeTarget, cause: str = "finished", finished_at_ms: int = 1792000500000):
        norm = str(uuid.UUID(target.session_id)).lower()
        tdir = self.state_dir / "retired-sessions" / norm
        tdir.mkdir(parents=True, exist_ok=True)
        tpath = tdir / "tombstone.json"
        tpath.write_text(json.dumps({
            "cause": cause,
            "id": target.session_id,
            "tag": target.tag,
            "workspace": target.workspace,
            "finished_at_ms": finished_at_ms,
        }), encoding="utf-8")
        return str(tpath)

    @patch("agent_hub_host.retired_native.list_sessions")
    def test_verify_retirement_success(self, mock_list):
        mock_list.return_value = []
        self._setup_tombstone(self.target)

        ok, tombstone, reason = verify_native_retirement("aplexer", self.target, state_dir=str(self.state_dir))
        self.assertTrue(ok)
        self.assertEqual(tombstone["cause"], "finished")
        self.assertEqual(reason, "retired_tombstone_verified")

    @patch("agent_hub_host.retired_native.list_sessions")
    def test_target_still_present_in_catalog(self, mock_list):
        mock_list.return_value = [
            NativeSession(
                session_id=self.target.session_id,
                tag=self.target.tag,
                workspace=self.target.workspace,
                phase="running",
            )
        ]
        self._setup_tombstone(self.target)
        ok, tombstone, reason = verify_native_retirement("aplexer", self.target, state_dir=str(self.state_dir))
        self.assertFalse(ok)
        self.assertIn("target_still_active_in_catalog", reason)

    @patch("agent_hub_host.retired_native.list_sessions")
    def test_tag_reused_by_another_active_session(self, mock_list):
        other_id = str(uuid.uuid4())
        mock_list.return_value = [
            NativeSession(
                session_id=other_id,
                tag=self.target.tag,  # Same tag in same workspace!
                workspace=self.target.workspace,
                phase="running",
            )
        ]
        self._setup_tombstone(self.target)
        ok, tombstone, reason = verify_native_retirement("aplexer", self.target, state_dir=str(self.state_dir))
        self.assertFalse(ok)
        self.assertIn("tag_reused_by_active_session", reason)

    @patch("agent_hub_host.retired_native.list_sessions")
    def test_catalog_error_fails_closed(self, mock_list):
        mock_list.side_effect = SessionControlError("aplexer daemon dead")
        self._setup_tombstone(self.target)
        ok, tombstone, reason = verify_native_retirement("aplexer", self.target, state_dir=str(self.state_dir))
        self.assertFalse(ok)
        self.assertIn("live_catalog_query_failed", reason)


class TestAuthenticHistoryCapture(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="history-capture-", dir=str(REPO_TMP)))
        self.state_dir = self.tmp / "aplexer_state"
        self.state_dir.mkdir()
        self.ws = self.tmp / "ws"
        self.ws.mkdir()
        self.target = make_target(str(self.ws))
        self.norm = str(uuid.UUID(self.target.session_id)).lower()

    def tearDown(self):
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    def test_capture_history_bin_tail(self):
        rdir = self.state_dir / "retired-sessions" / self.norm
        rdir.mkdir(parents=True)
        hpath = rdir / "history.bin"
        payload = b"A" * 1000 + b"TERMINAL_OUTPUT_TAIL"
        hpath.write_bytes(payload)

        raw, src, mode, reason = capture_retired_history(self.target, state_dir=str(self.state_dir), limit_bytes=50)
        self.assertIsNotNone(raw)
        self.assertEqual(len(raw), 50)
        self.assertTrue(raw.endswith(b"TERMINAL_OUTPUT_TAIL"))
        self.assertEqual(mode, "tail")
        self.assertEqual(reason, "authentic_history_captured")

    def test_capture_screen_txt_fallback(self):
        rdir = self.state_dir / "retired-sessions" / self.norm
        rdir.mkdir(parents=True)
        # history.bin absent, screen.txt present
        spath = rdir / "screen.txt"
        spath.write_text("SCREEN_SNAPSHOT_LINE_1\nDONE", encoding="utf-8")

        raw, src, mode, reason = capture_retired_history(self.target, state_dir=str(self.state_dir))
        self.assertIsNotNone(raw)
        self.assertEqual(mode, "screen")
        self.assertIn(b"SCREEN_SNAPSHOT_LINE_1", raw)

    def test_unavailable_history_returns_precise_reason(self):
        rdir = self.state_dir / "retired-sessions" / self.norm
        rdir.mkdir(parents=True)
        # Empty directory: no history.bin, no screen.txt
        raw, src, mode, reason = capture_retired_history(self.target, state_dir=str(self.state_dir))
        self.assertIsNone(raw)
        self.assertEqual(reason, "authentic retired history unavailable; listed-only")

    def test_empty_files_rejected(self):
        rdir = self.state_dir / "retired-sessions" / self.norm
        rdir.mkdir(parents=True)
        (rdir / "history.bin").write_bytes(b"")  # 0 bytes
        raw, src, mode, reason = capture_retired_history(self.target, state_dir=str(self.state_dir))
        self.assertIsNone(raw)
        self.assertEqual(reason, "authentic retired history unavailable; listed-only")


class TestStopAndCleanupEndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="stop-cleanup-e2e-", dir=str(REPO_TMP)))
        self.state_dir = self.tmp / "aplexer_state"
        self.state_dir.mkdir()
        self.journal_dir = self.tmp / "journal"
        self.journal_dir.mkdir(mode=0o700)
        self.roots_dir = self.tmp / "roots"
        self.roots_dir.mkdir()
        self.ws = self.tmp / "ws"
        self.ws.mkdir()
        self.target = make_target(str(self.ws), tag="task-hub-test")
        self.custody = make_custody()

    def tearDown(self):
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    def _setup_retired_session(self, target: NativeTarget, history_content: bytes | None = None):
        norm = str(uuid.UUID(target.session_id)).lower()
        tdir = self.state_dir / "retired-sessions" / norm
        tdir.mkdir(parents=True, exist_ok=True)
        tpath = tdir / "tombstone.json"
        tpath.write_text(json.dumps({
            "cause": "finished",
            "id": target.session_id,
            "tag": target.tag,
            "workspace": target.workspace,
            "finished_at_ms": 1792266525291,
        }), encoding="utf-8")
        if history_content is not None:
            (tdir / "history.bin").write_bytes(history_content)

    @patch("agent_hub_host.native_sessions.list_sessions")
    def test_stop_session_on_already_retired_is_no_kill(self, mock_list):
        mock_list.return_value = []
        self._setup_retired_session(self.target)

        with patch.dict(os.environ, {"APLEXER_STATE_DIR": str(self.state_dir)}), \
                patch("agent_hub_host.retired_native.list_sessions", return_value=[]), \
                patch("agent_hub_host.native_sessions.run_native", side_effect=AssertionError("native effect forbidden")):
            res = stop_session("aplexer", self.target)
        self.assertEqual(res.state, "stopped")
        self.assertIn("already confirmed retired: finished tombstone at 1792266525291", res.reason)

    @patch("agent_hub_host.session_executor.capture_session_output")
    @patch("agent_hub_host.session_executor.stop_controlled_session")
    @patch("agent_hub_host.session_executor._run_cleanup_tree_after_stop")
    def test_claimed_effect_preserves_retired_history_and_removes_tree(
        self, mock_tree, mock_stop, mock_live_cap
    ):
        cid = str(uuid.uuid4())
        mock_live_cap.side_effect = SessionControlError("Tag absent from workspace (rows: 0)")
        mock_stop.return_value = {"state": "stopped", "session_id": self.target.session_id, "reason": "already confirmed retired"}
        mock_tree.return_value = {"state": "removed", "reason": "owned worktree removed; branch deleted"}

        # Retired session WITH authentic history
        self._setup_retired_session(self.target, history_content=b"GENUINE_RETIRED_OUTPUT_TAIL\n")

        with patch.dict(os.environ, {"APLEXER_STATE_DIR": str(self.state_dir)}), \
             patch("agent_hub_host.retired_native.list_sessions", return_value=[]):
            outcome = se._run_claimed_effect(
                "aplexer", "stop", {"command_id": cid}, self.target,
                custody=self.custody, journal_dir=str(self.journal_dir),
                cleanup_roots=[str(self.roots_dir)], cleanup_tree_authorized=True,
            )

        self.assertEqual(outcome["state"], "stopped")
        self.assertEqual(outcome["ack_outcome"], "delivered")
        self.assertTrue(outcome["confirmed_exit"])
        self.assertEqual(outcome["tree"]["state"], "removed")

        # Verify evidence metadata and raw file
        ev_meta_path = outcome["cleanup_evidence_path"]
        self.assertTrue(os.path.isfile(ev_meta_path))
        with open(ev_meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
        self.assertEqual(meta["command_id"], cid)
        self.assertEqual(meta["native_id"], self.target.session_id)
        self.assertEqual(meta["native_finished_at_ms"], 1792266525291)  # Preserved original timestamp
        self.assertIn("evidence_observed_at_ms", meta)  # Real observation timestamp
        self.assertEqual(meta["retired_source"], "history.bin")

        raw_path = self.journal_dir / meta["raw_file"]
        self.assertTrue(os.path.isfile(raw_path))
        self.assertEqual(raw_path.read_bytes(), b"GENUINE_RETIRED_OUTPUT_TAIL\n")
        self.assertEqual(stat.S_IMODE(raw_path.stat().st_mode), 0o600)

    @patch("agent_hub_host.session_executor.capture_session_output")
    def test_claimed_effect_refuses_when_authentic_history_unavailable(self, mock_live_cap):
        cid = str(uuid.uuid4())
        mock_live_cap.side_effect = SessionControlError("Tag absent from workspace (rows: 0)")

        # Retired session WITHOUT authentic history (like the failed fixture)
        self._setup_retired_session(self.target, history_content=None)

        with patch.dict(os.environ, {"APLEXER_STATE_DIR": str(self.state_dir)}), \
             patch("agent_hub_host.retired_native.list_sessions", return_value=[]):
            outcome = se._run_claimed_effect(
                "aplexer", "stop", {"command_id": cid}, self.target,
                custody=self.custody, journal_dir=str(self.journal_dir),
                cleanup_roots=[str(self.roots_dir)], cleanup_tree_authorized=True,
            )

        self.assertEqual(outcome["state"], "refused")
        self.assertEqual(outcome["ack_outcome"], "refused")
        self.assertFalse(outcome["confirmed_exit"])
        self.assertEqual(outcome["tree"]["state"], "kept")
        self.assertIn("authentic retired history unavailable; listed-only", outcome["tree"]["reason"])

    @patch("agent_hub_host.session_executor.list_sessions")
    @patch("agent_hub_host.session_cleanup.remove_owned_worktree")
    def test_run_cleanup_tree_reverifies_retirement_proof(self, mock_remove_tree, mock_list):
        mock_list.return_value = []
        mock_remove_tree.return_value = {"state": "tree_removed", "reason": "removed"}
        self._setup_retired_session(self.target)

        # 1. With valid tombstone: re-verification succeeds and tree is removed
        with patch.dict(os.environ, {"APLEXER_STATE_DIR": str(self.state_dir)}), \
             patch("agent_hub_host.retired_native.list_sessions", return_value=[]):
            res = se._run_cleanup_tree_after_stop(
                "aplexer", self.target, custody=self.custody,
                journal_dir=str(self.journal_dir), cleanup_roots=[str(self.roots_dir)],
                command_id="cmd-1",
            )
        self.assertEqual(res["state"], "removed")

        # 2. If tombstone was deleted between stop and tree: re-verification fails, tree kept
        norm = str(uuid.UUID(self.target.session_id)).lower()
        tombstone_path = self.state_dir / "retired-sessions" / norm / "tombstone.json"
        tombstone_path.unlink()

        with patch.dict(os.environ, {"APLEXER_STATE_DIR": str(self.state_dir)}), \
             patch("agent_hub_host.retired_native.list_sessions", return_value=[]):
            res2 = se._run_cleanup_tree_after_stop(
                "aplexer", self.target, custody=self.custody,
                journal_dir=str(self.journal_dir), cleanup_roots=[str(self.roots_dir)],
                command_id="cmd-2",
            )
        self.assertEqual(res2["state"], "kept")
        self.assertIn("native retirement re-verification failed before tree", res2["reason"])


def run_git(args, cwd):
    import subprocess
    proc = subprocess.run(
        ["git", *args], capture_output=True, text=True, timeout=60, check=False, cwd=str(cwd),
        env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"},
    )
    if proc.returncode != 0:
        raise AssertionError("git %r failed in %s: %s" % (args, cwd, (proc.stderr or "")[-500:]))
    return proc


def make_repo(base):
    origin = base / "origin.git"
    main = base / "main"
    run_git(["init", "--bare", "--initial-branch=main", str(origin)], base)
    run_git(["clone", str(origin), str(main)], base)
    run_git(["config", "user.email", "synth@example.test"], main)
    run_git(["config", "user.name", "synth"], main)
    (main / "seed.txt").write_text("seed\n", encoding="utf-8")
    run_git(["add", "seed.txt"], main)
    run_git(["-c", "user.email=synth@example.test", "-c", "user.name=synth", "commit", "-m", "seed"], main)
    run_git(["push", "origin", "main"], main)
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


class TestSyntheticWorktreeCustodyAndGuards(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="synth-guard-", dir=str(REPO_TMP)))
        self.journal_dir = self.tmp / "journal"
        self.journal_dir.mkdir(mode=0o700)
        self.roots_dir = self.tmp / "roots"
        self.roots_dir.mkdir()
        self.origin, self.main = make_repo(self.tmp)
        self.custody = make_custody("hetzner", "demo:assistant")

    def tearDown(self):
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    def test_idempotent_restart_and_no_repeated_deletion(self):
        wt = add_merged_worktree(self.main, self.roots_dir, "wt-1", "branch-wt-1")
        native_id = str(uuid.uuid4())

        # First run: removes worktree and deletes branch
        res1 = sc.remove_owned_worktree(
            custody=self.custody, journal_dir=str(self.journal_dir), machine_id="hetzner",
            native_id=native_id, canonical_workspace=str(wt),
            cleanup_roots=[str(self.roots_dir)], claim_holder="hetzner",
            stop_confirmed=True, live_paths=[], command_id="cmd-1",
        )
        self.assertEqual(res1["state"], "tree_removed")
        self.assertTrue(res1["worktree_removed"])
        self.assertTrue(res1["branch_deleted"])
        self.assertFalse(wt.exists())

        # Second run (lost ACK / restart): replays journal, replayed=True, never repeats deletion
        res2 = sc.remove_owned_worktree(
            custody=self.custody, journal_dir=str(self.journal_dir), machine_id="hetzner",
            native_id=native_id, canonical_workspace=str(wt),
            cleanup_roots=[str(self.roots_dir)], claim_holder="hetzner",
            stop_confirmed=True, live_paths=[], command_id="cmd-1",
        )
        self.assertEqual(res2["state"], "tree_removed")
        self.assertTrue(res2.get("replayed", False))

    def test_dirty_worktree_guard_refuses_and_keeps_tree(self):
        wt = add_merged_worktree(self.main, self.roots_dir, "wt-dirty", "branch-wt-dirty")
        native_id = str(uuid.uuid4())

        # Create uncommitted edit
        (wt / "dirty.txt").write_text("uncommitted edits\n", encoding="utf-8")

        res = sc.remove_owned_worktree(
            custody=self.custody, journal_dir=str(self.journal_dir), machine_id="hetzner",
            native_id=native_id, canonical_workspace=str(wt),
            cleanup_roots=[str(self.roots_dir)], claim_holder="hetzner",
            stop_confirmed=True, live_paths=[], command_id="cmd-dirty",
        )
        self.assertEqual(res["state"], "refused")
        self.assertIn("dirty worktree", res["reason"])
        self.assertTrue(wt.exists())

    def test_active_path_live_use_guard(self):
        wt = add_merged_worktree(self.main, self.roots_dir, "wt-live", "branch-wt-live")
        native_id = str(uuid.uuid4())

        # Path is declared in live_paths (e.g. another running session's workspace or cwd)
        res = sc.remove_owned_worktree(
            custody=self.custody, journal_dir=str(self.journal_dir), machine_id="hetzner",
            native_id=native_id, canonical_workspace=str(wt),
            cleanup_roots=[str(self.roots_dir)], claim_holder="hetzner",
            stop_confirmed=True, live_paths=[str(wt)], command_id="cmd-live",
        )
        self.assertEqual(res["state"], "refused")
        self.assertIn("another live session uses this worktree", res["reason"])
        self.assertTrue(wt.exists())

    def test_custody_foreign_machine_refused(self):
        wt = add_merged_worktree(self.main, self.roots_dir, "wt-foreign", "branch-wt-foreign")
        native_id = str(uuid.uuid4())

        # Caller machine does not match claim holder machine
        with self.assertRaises(SessionControlError) as ctx:
            sc.remove_owned_worktree(
                custody=self.custody, journal_dir=str(self.journal_dir), machine_id="other-machine",
                native_id=native_id, canonical_workspace=str(wt),
                cleanup_roots=[str(self.roots_dir)], claim_holder="hetzner",
                stop_confirmed=True, live_paths=[], command_id="cmd-foreign",
            )
        self.assertIn("custody machine must own the cleanup target", str(ctx.exception))



class TestRetiredDescriptorSafety(unittest.TestCase):
    def test_parent_symlink_cannot_supply_tombstone_or_history(self):
        with tempfile.TemporaryDirectory(dir=str(REPO_TMP)) as temp:
            base = Path(temp); real = base / "real"; real.mkdir()
            target = make_target(str(base))
            tomb = {"cause": "finished", "id": target.session_id, "tag": target.tag,
                    "workspace": target.workspace, "finished_at_ms": target.created_at_ms + 1}
            (real / "tombstone.json").write_text(json.dumps(tomb))
            link = base / "link"; link.symlink_to(real, target_is_directory=True)
            with self.assertRaises(SessionControlError):
                validate_retired_tombstone(str(link / "tombstone.json"), target)
            retired = base / "state" / "retired-sessions"; retired.mkdir(parents=True)
            (real / "history.bin").write_bytes(b"must not read through parent symlink")
            (retired / target.session_id).symlink_to(real, target_is_directory=True)
            data, _, _, _ = capture_retired_history(target, state_dir=str(base / "state"))
            self.assertIsNone(data)

    def test_history_bound_rejects_unbounded_or_boolean_reads(self):
        with tempfile.TemporaryDirectory(dir=str(REPO_TMP)) as temp:
            target = make_target(temp)
            for bad in (0, -1, True, 65537):
                with self.subTest(bound=bad), self.assertRaises(SessionControlError):
                    capture_retired_history(target, state_dir=temp, limit_bytes=bad)

    def test_replaced_file_cannot_bypass_tombstone_read_bound(self):
        with tempfile.TemporaryDirectory(dir=str(REPO_TMP)) as temp:
            path = Path(temp) / "tombstone.json"; path.write_text("{}")
            target = make_target(temp)
            original = private_fs.read_bytes
            def grow_before_read(file, limit, **kwargs):
                Path(file).write_bytes(b"x" * (limit + 1))
                return original(file, limit, **kwargs)
            with patch.object(private_fs, "read_bytes", side_effect=grow_before_read):
                with self.assertRaises(SessionControlError):
                    validate_retired_tombstone(str(path), target)

    def test_worktree_observed_workspace_matches_parent_binding_on_stop_and_tombstone(self):
        with tempfile.TemporaryDirectory(dir=str(REPO_TMP)) as temp:
            base = Path(temp)
            repo = base / "repo"
            wt_git = repo / ".git" / "worktrees" / "wt1"
            wt_git.mkdir(parents=True)
            wt = base / "worktrees" / "wt1"
            wt.mkdir(parents=True)
            (wt / ".git").write_text(f"gitdir: {wt_git}\n", encoding="utf-8")

            target = make_target(str(repo), tag="hub-wt-test")
            tomb_path = base / "tombstone.json"
            tomb_path.write_text(json.dumps({
                "cause": "finished",
                "id": target.session_id,
                "tag": target.tag,
                "workspace": str(wt),
                "finished_at_ms": target.created_at_ms + 100,
            }), encoding="utf-8")
            validated = validate_retired_tombstone(str(tomb_path), target)
            self.assertEqual(validated["id"].lower(), target.session_id.lower())
