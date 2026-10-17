"""M15 command outcomes: evidence byte-budget, durable diagnostics, safe oracle.

Focused synthetic tests (fakes only, no live sessions, no Hub):
- full 64KiB + JSON-expansion + UTF-8 evidence stays within bounds (split raw+meta);
- preservation failure keeps tree (no deletion) with bounded reason;
- capture/stop refused vs uncertain journal durably (never state null forever);
- same-claim read-only stop reconcile never re-kills, absent catalog alone never stopped.
"""

import hashlib
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
from agent_hub_host.native_sessions import (
    NativeSession,
    NativeTarget,
    SessionControlError,
    SessionUncertain,
    _confirm_workload_exit,
    reconcile_stop_oracle,
)

REPO_TMP = Path(__file__).resolve().parents[2] / ".local" / "tmp"


def custody(machine="h"):
    return se.CallerCustody(actor="machine:" + machine, machine_id=machine,
                            credential_family="host-" + machine)


def target_for(workspace):
    return NativeTarget(session_id=str(uuid.uuid4()), tag="t",
                        workspace=str(workspace), created_at_ms=1700000000000)


def claim_for(command_id, authorized=False):
    return {"command_id": command_id,
            "claim_token": "tok-" + uuid.uuid4().hex + "zz",
            "holder": "h", "generation": 1,
            "expires_at_ms": 2000000000000,
            "cleanup_tree": True if authorized else False}


class TestEvidenceByteBudget(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="cmd-outcomes-", dir=str(REPO_TMP)))
        self.journal = self.base / "journal"
        self.journal.mkdir(mode=0o700)
        self.roots = self.base / "roots"
        self.roots.mkdir()
        self.work = self.base / "work"
        self.work.mkdir()

    def tearDown(self):
        shutil.rmtree(str(self.base), ignore_errors=True)

    def test_full64kib_ansi_utf8_within_bounds(self):
        # 64KiB raw: multi-byte UTF-8 + ANSI escapes that would explode under JSON.
        cid = str(uuid.uuid4())
        tgt = target_for(self.work)
        # 16K repetitions of 4-byte payload: emoji (4B) + ANSI (7B) ~ mixed.
        chunk = "\U0001f600\x1b[31mX\x1b[0m"  # 4B emoji + 7B ANSI + 1B + 4B = ~16B raw
        text = (chunk * 4096)[:65536 // 4]  # tune to ~64KiB raw after encode
        # Ensure exactly 64KiB raw by padding/truncating on bytes.
        raw = text.encode("utf-8")
        if len(raw) < 65536:
            raw = (raw * ((65536 // len(raw)) + 1))[:65536]
        else:
            raw = raw[:65536]
        text = raw.decode("utf-8", "replace")
        self.assertLessEqual(len(text.encode("utf-8")), 65536)

        def fake_capture(exe, target, custody=None, limit_bytes=0, mode="tail"):
            # Simulate adapter returning full 64KiB (real adapter truncates).
            return {"session_id": target.session_id, "tag": target.tag, "mode": mode,
                    "text": text, "truncated": True,
                    "byte_length": len(text.encode("utf-8")),
                    "privacy_unknown": True}

        with patch.object(se, "capture_session_output", side_effect=fake_capture):
            meta_path = se._preserve_cleanup_evidence(
                "fake", tgt, custody=custody(),
                journal_dir=str(self.journal), command_id=cid)
        # Meta small, raw bounded, hash matches, 0600, outside roots.
        self.assertTrue(os.path.isfile(meta_path))
        self.assertLess(os.path.getsize(meta_path), 2048)
        self.assertEqual(stat.S_IMODE(os.stat(meta_path).st_mode), 0o600)
        with open(meta_path, encoding="utf-8") as fh:
            meta = json.load(fh)
        self.assertEqual(meta["command_id"], cid)
        self.assertNotIn("text", json.dumps(meta))
        self.assertIn("sha256", meta)
        raw_path = os.path.join(str(self.journal), meta["raw_file"])
        self.assertTrue(os.path.isfile(raw_path))
        self.assertLessEqual(os.path.getsize(raw_path), 64 * 1024)
        self.assertEqual(stat.S_IMODE(os.stat(raw_path).st_mode), 0o600)
        with open(raw_path, "rb") as fh:
            stored = fh.read()
        self.assertEqual(hashlib.sha256(stored).hexdigest(), meta["sha256"])
        self.assertEqual(meta["byte_length"], len(stored))

    def test_preservation_failure_keeps_tree_no_delete(self):
        cid = str(uuid.uuid4())
        tgt = target_for(self.work)
        events = []

        def fake_capture(*a, **k):
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
        self.assertEqual(events, ["stop"])
        self.assertEqual(out["state"], "stopped")
        self.assertEqual(out["tree"]["state"], "kept")
        self.assertIn("evidence_capture_failed", out["tree"]["reason"])
        self.assertNotIn("cleanup_evidence_path", out)


class TestDurableOutcomes(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="cmd-durable-", dir=str(REPO_TMP)))
        self.journal = self.base / "journal"
        self.journal.mkdir(mode=0o700)
        self.work = self.base / "work"
        self.work.mkdir()

    def tearDown(self):
        shutil.rmtree(str(self.base), ignore_errors=True)

    def test_refused_capture_journals_bounded_reason(self):
        cid = str(uuid.uuid4())
        tgt = target_for(self.work)

        def fake_request(method, path, payload):
            return {"ok": True}

        with patch.object(se, "_run_claimed_effect",
                          side_effect=SessionControlError("Native capture failed")):
            out = se.handle_claimed_command(
                {"op": "capture", "command_id": cid, "generation": 1,
                 "target": {"session_id": tgt.session_id, "tag": tgt.tag,
                            "workspace": tgt.workspace, "created_at_ms": 1700000000000}},
                claim_for(cid, False), custody=custody(),
                journal_dir=str(self.journal), executable="fake",
                request=fake_request)
        self.assertEqual(out["state"], "refused")
        self.assertEqual(out["error_stage"], "capture")
        self.assertTrue(out["error_type"])
        self.assertLessEqual(len(out["reason"]), 300)
        # Journal durable (not null forever), ack replayable.
        journal_file = os.path.join(str(self.journal), "cmd-" + cid + ".json")
        with open(journal_file, encoding="utf-8") as fh:
            rec = json.load(fh)
        self.assertEqual(rec["state"], "refused")
        self.assertIn("error_stage", rec)
        # Replay returns same without new effect.
        again = se.handle_claimed_command(
            {"op": "capture", "command_id": cid, "generation": 1,
             "target": {"session_id": tgt.session_id, "tag": tgt.tag,
                        "workspace": tgt.workspace, "created_at_ms": 1700000000000}},
            claim_for(cid, False), custody=custody(),
            journal_dir=str(self.journal), executable="fake",
            request=fake_request)
        self.assertTrue(again.get("replayed"))

    def test_uncertain_stop_journals_oracle(self):
        cid = str(uuid.uuid4())
        tgt = target_for(self.work)
        oracle = {"version": 1, "session_id": tgt.session_id, "tag": tgt.tag,
                  "workspace": tgt.workspace, "created_at_ms": 1700000000000,
                  "engine": None, "phase": "running",
                  "worker_pid": 999999, "workload_pid": 999998,
                  "processes": 2, "containment_empty": None,
                  "workload_cgroup": None, "last_activity_ms": 1700000000000}
        kill = {"signal": "HUP", "grace_ms": 2000, "killed": True, "result": "still_running"}

        def fake_effect(*a, **k):
            exc = SessionUncertain("Target still running after kill; capacity NOT released")
            exc.oracle = dict(oracle)
            exc.kill = dict(kill)
            raise exc

        def fake_request(method, path, payload):
            return {"ok": True}

        with patch.object(se, "_run_claimed_effect", side_effect=fake_effect):
            out = se.handle_claimed_command(
                {"op": "stop", "command_id": cid, "generation": 1,
                 "target": {"session_id": tgt.session_id, "tag": tgt.tag,
                            "workspace": tgt.workspace, "created_at_ms": 1700000000000}},
                claim_for(cid, False), custody=custody(),
                journal_dir=str(self.journal), executable="fake",
                request=fake_request)
        self.assertEqual(out["state"], "uncertain")
        self.assertEqual(out["error_stage"], "stop")
        self.assertIn("stop_oracle", out)
        self.assertEqual(out["stop_oracle"]["session_id"], tgt.session_id)
        # No raw text, no secrets in journal.
        blob = json.dumps(out)
        self.assertNotIn("argv", blob)
        self.assertNotIn("stdout", blob)


class TestStopOracleReconcile(unittest.TestCase):
    def test_absent_catalog_alone_never_stopped(self):
        # Oracle without any containment proof: absent catalog stays uncertain.
        # No cgroup, no PIDs, no containment flag -> _confirm has no witness.
        oracle = {"version": 1, "session_id": str(uuid.uuid4()), "tag": "t",
                  "workspace": "/srv/wt", "created_at_ms": 1700000000000,
                  "engine": None, "phase": "running",
                  "worker_pid": None, "workload_pid": None,
                  "processes": None, "containment_empty": None,
                  "workload_cgroup": None,
                  "last_activity_ms": 1700000000000}
        with patch("agent_hub_host.native_sessions.list_sessions", return_value=[]):
            confirmed, why = reconcile_stop_oracle("fake", oracle, {"killed": True})
        self.assertFalse(confirmed)
        self.assertIn("reconcile", why)

    def test_confirmed_exit_via_reaped_cgroup_no_rekill(self):
        oracle = {"version": 1, "session_id": str(uuid.uuid4()), "tag": "t",
                  "workspace": "/srv/wt", "created_at_ms": 1700000000000,
                  "engine": None, "phase": "running",
                  "worker_pid": 999999, "workload_pid": 999998,
                  "processes": 2, "containment_empty": None,
                  "workload_cgroup": "/sys/fs/cgroup/definitely-gone-xyz-123",
                  "last_activity_ms": 1700000000000}
        calls = []

        def fake_list(exe, workspace=None, include_exited=False):
            calls.append("list")
            return []

        def fake_pid_dead(pid):
            return True  # both PIDs dead

        with patch("agent_hub_host.native_sessions.list_sessions", side_effect=fake_list), \
             patch("agent_hub_host.native_sessions._pid_dead", side_effect=fake_pid_dead), \
             patch("agent_hub_host.native_sessions._workload_containment_gone", return_value=True), \
             patch("agent_hub_host.native_sessions.run_native",
                   side_effect=AssertionError("must never re-kill")):
            confirmed, why = reconcile_stop_oracle("fake", oracle, {"killed": True})
        self.assertTrue(confirmed)
        self.assertIn("list", calls)  # read-only list used
        # No kill attempted (run_native would have raised).

    def test_live_row_never_confirmed_stopped(self):
        sid = str(uuid.uuid4())
        oracle = {"version": 1, "session_id": sid, "tag": "t",
                  "workspace": "/srv/wt", "created_at_ms": 1700000000000,
                  "engine": None, "phase": "running",
                  "worker_pid": 1234, "workload_pid": 1235,
                  "processes": 2, "containment_empty": False,
                  "workload_cgroup": None, "last_activity_ms": 1700000000000}
        live = NativeSession(session_id=sid, tag="t", workspace="/srv/wt",
                             phase="running", worker_pid=1234, workload_pid=1235,
                             created_at_ms=1700000000000)
        with patch("agent_hub_host.native_sessions.list_sessions", return_value=[live]), \
             patch("agent_hub_host.native_sessions.run_native",
                   side_effect=AssertionError("must never re-kill")):
            confirmed, why = reconcile_stop_oracle("fake", oracle, {"killed": True})
        self.assertFalse(confirmed)
        self.assertIn("still", why)


if __name__ == "__main__":
    unittest.main()
