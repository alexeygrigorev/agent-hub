"""M15d cleanup evidence: bounded pre-stop capture for flagged cleanup ONLY.

Synthetic only: no live sessions, no real worktrees, no Hub. Verifies the
actual call order capture -> durable 0600 file -> stop -> tree for an
explicitly flagged cleanup, the failure path (capture error => stop still
acked, tree kept, zero deletions), and that ordinary stops never capture.
Raw evidence text never enters the Hub ack payload.
"""

import json
import os
import shutil
import stat
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from agent_hub_host import session_executor as se
from agent_hub_host.native_sessions import NativeTarget, SessionControlError

REPO_TMP = Path(__file__).resolve().parents[2] / ".local" / "tmp"


def custody(machine="h"):
    return se.CallerCustody(actor="machine:" + machine, machine_id=machine,
                            credential_family="host-" + machine)


def target_for(workspace):
    return NativeTarget(session_id=str(uuid.uuid4()), tag="t",
                        workspace=str(workspace), created_at_ms=1700000000000)


def claim_for(command_id, authorized):
    return {"command_id": command_id,
            "claim_token": "tok-" + uuid.uuid4().hex + "zz",
            "holder": "h", "generation": 1,
            "expires_at_ms": 2000000000000,
            "cleanup_tree": True if authorized else False}


class TestCleanupEvidenceOrder(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="cleanup-evidence-", dir=str(REPO_TMP)))
        self.journal = self.base / "journal"
        self.journal.mkdir(mode=0o700)
        self.roots = self.base / "roots"
        self.roots.mkdir()
        self.work = self.base / "work"
        self.work.mkdir()

    def tearDown(self):
        shutil.rmtree(str(self.base), ignore_errors=True)

    def test_flagged_cleanup_captures_before_stop_then_tree(self):
        events = []
        cid = str(uuid.uuid4())
        tgt = target_for(self.work)

        def fake_capture(exe, target, custody=None, limit_bytes=0, mode="tail"):
            events.append("capture")
            self.assertEqual(limit_bytes, 64 * 1024)
            return {"session_id": target.session_id, "tag": target.tag, "mode": mode,
                    "text": "tail-line-1\n", "truncated": False, "byte_length": 12,
                    "privacy_unknown": True}

        def fake_stop(exe, target, custody=None):
            events.append("stop")
            return {"session_id": target.session_id, "state": "stopped", "reason": "exited"}

        def fake_tree(exe, target, custody=None, journal_dir=None,
                      cleanup_roots=None, command_id=None):
            events.append("tree")
            return {"state": "removed", "reason": "owned worktree removed"}

        with patch.object(se, "capture_session_output", side_effect=fake_capture), \
             patch.object(se, "stop_controlled_session", side_effect=fake_stop), \
             patch.object(se, "_run_cleanup_tree_after_stop", side_effect=fake_tree):
            out = se._run_claimed_effect(
                "fake", "stop", {"command_id": cid}, tgt, custody=custody(),
                journal_dir=str(self.journal), cleanup_roots=[str(self.roots)],
                cleanup_tree_authorized=True)
        self.assertEqual(events, ["capture", "stop", "tree"])
        self.assertIn("tree", out)
        # Durable private evidence outside the cleanup roots.
        ev_path = out.get("cleanup_evidence_path")
        self.assertIsInstance(ev_path, str)
        self.assertTrue(ev_path.startswith(str(self.journal) + os.sep))
        self.assertFalse(ev_path.startswith(str(self.roots) + os.sep))
        self.assertTrue(os.path.isfile(ev_path))
        mode = stat.S_IMODE(os.stat(ev_path).st_mode)
        self.assertEqual(mode, 0o600)
        with open(ev_path, encoding="utf-8") as fh:
            record = json.load(fh)
        self.assertEqual(record["command_id"], cid)
        self.assertIn("tail-line-1", record["text"])

    def test_capture_failure_keeps_tree_without_deletion(self):
        events = []
        cid = str(uuid.uuid4())
        tgt = target_for(self.work)

        def fake_capture(*a, **k):
            events.append("capture")
            raise SessionControlError("native capture unavailable")

        def fake_stop(exe, target, custody=None):
            events.append("stop")
            return {"session_id": target.session_id, "state": "stopped", "reason": "exited"}

        def fake_tree(*a, **k):
            events.append("tree")
            raise AssertionError("tree must never run without evidence")

        with patch.object(se, "capture_session_output", side_effect=fake_capture), \
             patch.object(se, "stop_controlled_session", side_effect=fake_stop), \
             patch.object(se, "_run_cleanup_tree_after_stop", side_effect=fake_tree):
            out = se._run_claimed_effect(
                "fake", "stop", {"command_id": cid}, tgt, custody=custody(),
                journal_dir=str(self.journal), cleanup_roots=[str(self.roots)],
                cleanup_tree_authorized=True)
        # The stop itself still reports its confirmed outcome; the tree is
        # kept with the capture failure (never a deletion, never a
        # preservation claim).
        self.assertEqual(events, ["capture", "stop"])
        self.assertEqual(out["state"], "stopped")
        self.assertEqual(out["tree"]["state"], "kept")
        self.assertIn("evidence_capture_failed", out["tree"]["reason"])
        self.assertNotIn("cleanup_evidence_path", out)

    def test_ordinary_stop_never_captures(self):
        events = []
        cid = str(uuid.uuid4())
        tgt = target_for(self.work)

        def fake_capture(*a, **k):
            events.append("capture")
            return {"text": "x"}

        def fake_stop(exe, target, custody=None):
            events.append("stop")
            return {"session_id": target.session_id, "state": "stopped", "reason": "exited"}

        with patch.object(se, "capture_session_output", side_effect=fake_capture), \
             patch.object(se, "stop_controlled_session", side_effect=fake_stop):
            out = se._run_claimed_effect(
                "fake", "stop", {"command_id": cid}, tgt, custody=custody(),
                journal_dir=str(self.journal), cleanup_roots=[str(self.roots)],
                cleanup_tree_authorized=False)
        self.assertEqual(events, ["stop"])
        self.assertNotIn("tree", out)
        leftovers = [p for p in os.listdir(str(self.journal)) if p.startswith("cleanup-evidence-")]
        self.assertEqual(leftovers, [])

    def test_handle_claimed_command_journals_path_without_hub_text(self):
        cid = str(uuid.uuid4())
        tgt = target_for(self.work)
        submitted = {}

        def fake_capture(exe, target, custody=None, limit_bytes=0, mode="tail"):
            return {"session_id": target.session_id, "tag": target.tag, "mode": mode,
                    "text": "secret-tail\n", "truncated": False, "byte_length": 12,
                    "privacy_unknown": True}

        def fake_stop(exe, target, custody=None):
            return {"session_id": target.session_id, "state": "stopped", "reason": "exited"}

        def fake_tree(exe, target, custody=None, journal_dir=None,
                      cleanup_roots=None, command_id=None):
            return {"state": "removed", "reason": "owned worktree removed"}

        def fake_request(method, path, payload):
            submitted.update(payload=payload)
            return {"command_id": cid, "state": "acked"}

        with patch.object(se, "capture_session_output", side_effect=fake_capture), \
             patch.object(se, "stop_controlled_session", side_effect=fake_stop), \
             patch.object(se, "_run_cleanup_tree_after_stop", side_effect=fake_tree):
            outcome = se.handle_claimed_command(
                {"op": "stop", "command_id": cid, "generation": 1,
                 "target": {"session_id": tgt.session_id, "tag": tgt.tag,
                            "workspace": tgt.workspace, "created_at_ms": 1700000000000}},
                claim_for(cid, True), custody=custody(), journal_dir=str(self.journal),
                executable="fake", request=fake_request, cleanup_roots=[str(self.roots)])
        # Raw history never reaches the Hub ack.
        payload = submitted.get("payload", {})
        self.assertNotIn("secret-tail", json.dumps(payload))
        self.assertEqual(payload["tree"]["state"], "removed")
        # The local journal carries the durable evidence path; replay reuses it.
        self.assertIn("cleanup_evidence_path", outcome)
        self.assertTrue(os.path.isfile(outcome["cleanup_evidence_path"]))
        again = se.handle_claimed_command(
            {"op": "stop", "command_id": cid, "generation": 1,
             "target": {"session_id": tgt.session_id, "tag": tgt.tag,
                        "workspace": tgt.workspace, "created_at_ms": 1700000000000}},
            claim_for(cid, True), custody=custody(), journal_dir=str(self.journal),
            executable="fake", request=fake_request, cleanup_roots=[str(self.roots)])
        self.assertTrue(again.get("replayed"))


if __name__ == "__main__":
    unittest.main()
