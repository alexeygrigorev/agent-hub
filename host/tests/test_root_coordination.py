import fcntl
import json
import os
import pathlib
import shutil
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from agent_hub_host.guarded_root import (
    GuardedRootBridge,
    IncumbentStatus,
    HB_STALE_SEC,
    WINDOW_SEC,
    GRACE_SEC,
)


class MockClock:
    def __init__(self, start_time: float = 1700000000.0):
        self._time = start_time

    def time(self) -> float:
        return self._time

    def sleep(self, seconds: float) -> None:
        self._time += seconds


class TestRootCoordination(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="test_root_coord_")
        self.state_dir = pathlib.Path(self.tmp_dir)

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_flock_mutual_exclusion(self):
        clock = MockClock()
        bridge1 = GuardedRootBridge(state_dir=str(self.state_dir), clock=clock)
        bridge2 = GuardedRootBridge(state_dir=str(self.state_dir), clock=clock)

        res1 = bridge1.acquire(hub_job_id="job-1", hub_lease_epoch=1, hub_url="https://hub.local")
        self.assertTrue(res1["acquired"])
        self.assertEqual(res1["epoch"], 1)

        # Second attempt should see lock held and healthy incumbent
        res2 = bridge2.acquire(hub_job_id="job-2", hub_lease_epoch=2, hub_url="https://hub.local")
        self.assertFalse(res2["acquired"])
        self.assertTrue(res2["skip"])

        bridge1.finish(status="succeeded", summary_text="done")

        # Now lock is free, bridge2 can acquire
        res3 = bridge2.acquire(hub_job_id="job-3", hub_lease_epoch=3, hub_url="https://hub.local")
        self.assertTrue(res3["acquired"])
        self.assertEqual(res3["epoch"], 2)
        bridge2.finish(status="succeeded")

    def test_healthy_incumbent_skipped_with_note(self):
        clock = MockClock()
        bridge1 = GuardedRootBridge(state_dir=str(self.state_dir), clock=clock)
        res1 = bridge1.acquire(hub_job_id="job-1", hub_lease_epoch=1)
        self.assertTrue(res1["acquired"])
        run1_id = res1["run_id"]

        # Advance clock by 30 seconds (well within 180s heartbeat freshness)
        clock.sleep(30.0)

        signals_sent = []
        def mock_kill(pgid, sig):
            signals_sent.append((pgid, sig))

        bridge2 = GuardedRootBridge(state_dir=str(self.state_dir), clock=clock, kill_handler=mock_kill)
        res2 = bridge2.acquire(hub_job_id="job-2", hub_lease_epoch=2)

        self.assertFalse(res2["acquired"])
        self.assertTrue(res2["skip"])
        self.assertIn("previous root healthy", res2["reason"])
        self.assertEqual(len(signals_sent), 0)

        # Check that skip note was written to run1's inbox.md
        inbox_file = self.state_dir / "runs" / f"{run1_id}.inbox.md"
        self.assertTrue(inbox_file.exists())
        content = inbox_file.read_text()
        self.assertIn("started and skipped: you were healthy", content)

        bridge1.finish()

    def test_stale_incumbent_takeover_and_superseded(self):
        clock = MockClock(start_time=1700000000.0)
        bridge1 = GuardedRootBridge(state_dir=str(self.state_dir), clock=clock)
        res1 = bridge1.acquire(hub_job_id="job-1", hub_lease_epoch=1)
        self.assertTrue(res1["acquired"])
        run1_id = res1["run_id"]

        # Advance clock by 200 seconds (older than HB_STALE_SEC = 180s)
        clock.sleep(200.0)

        signals_sent = []
        def mock_kill(pgid, sig):
            signals_sent.append((pgid, sig))
            # Simulate incumbent process dying upon receiving signal
            bridge1.release()

        bridge2 = GuardedRootBridge(state_dir=str(self.state_dir), clock=clock, kill_handler=mock_kill)
        res2 = bridge2.acquire(hub_job_id="job-2", hub_lease_epoch=2)

        self.assertTrue(res2["acquired"])
        self.assertEqual(res2["epoch"], 2)
        self.assertGreater(len(signals_sent), 0)

        # Check superseded record for run1
        superseded_file = self.state_dir / "runs" / f"{run1_id}.superseded.json"
        self.assertTrue(superseded_file.exists())
        data = json.loads(superseded_file.read_text())
        self.assertEqual(data["run_id"], run1_id)
        self.assertEqual(data["status"], "superseded")
        self.assertEqual(data["superseded_by"], res2["run_id"])

        bridge2.finish()

    def test_crashed_incumbent_detection(self):
        clock = MockClock()
        bridge1 = GuardedRootBridge(state_dir=str(self.state_dir), clock=clock)
        res1 = bridge1.acquire(hub_job_id="job-1", hub_lease_epoch=1)
        run1_id = res1["run_id"]

        # Simulate crash: release file lock without calling finish(), leaving status='running' in state.json
        bridge1.release()

        bridge2 = GuardedRootBridge(state_dir=str(self.state_dir), clock=clock)
        res2 = bridge2.acquire(hub_job_id="job-2", hub_lease_epoch=2)

        self.assertTrue(res2["acquired"])
        self.assertEqual(res2["epoch"], 2)

        # Check crashed record created for run1
        superseded_file = self.state_dir / "runs" / f"{run1_id}.superseded.json"
        self.assertTrue(superseded_file.exists())
        data = json.loads(superseded_file.read_text())
        self.assertEqual(data["run_id"], run1_id)
        self.assertEqual(data["status"], "crashed")
        self.assertEqual(data["seen_by"], res2["run_id"])

        bridge2.finish()

    def test_native_epoch_and_hub_lease_epoch_binding(self):
        clock = MockClock()
        bridge = GuardedRootBridge(state_dir=str(self.state_dir), clock=clock)
        res = bridge.acquire(
            hub_job_id="hub-job-999",
            hub_lease_epoch=42,
            hub_url="https://hub.cloudflare.dev"
        )
        self.assertTrue(res["acquired"])

        state = bridge.read_state()
        self.assertEqual(state["epoch"], 1)
        self.assertEqual(state["hub_job_id"], "hub-job-999")
        self.assertEqual(state["hub_lease_epoch"], 42)
        self.assertEqual(state["hub_url"], "https://hub.cloudflare.dev")
        self.assertEqual(state["status"], "running")
        self.assertEqual(state["run_id"], res["run_id"])

        bridge.finish()

    def test_heartbeat_loop_and_fencing(self):
        clock = MockClock()
        bridge = GuardedRootBridge(state_dir=str(self.state_dir), clock=clock)
        res = bridge.acquire(hub_job_id="hub-1", hub_lease_epoch=1)
        self.assertTrue(res["acquired"])

        self.assertFalse(bridge.is_fenced())

        # Simulate state overwrite by a foreign competitor (split brain)
        bridge.write_state({"run_id": "foreign-run-xyz", "epoch": 99})

        self.assertTrue(bridge.is_fenced())
        bridge.finish()

    def test_finish_and_handoff_generation(self):
        clock = MockClock()
        bridge = GuardedRootBridge(state_dir=str(self.state_dir), clock=clock)
        res = bridge.acquire(hub_job_id="hub-finish-test", hub_lease_epoch=10)
        run_id = res["run_id"]

        clock.sleep(125.0)

        summary = bridge.finish(
            status="succeeded",
            exit_code=0,
            summary_text="## Handoff\n- Completed maintenance run cleanly.",
            agent_json={"summary": "all green"},
            evidence_refs=["/path/to/evidence.txt"]
        )

        self.assertEqual(summary["status"], "ok")
        self.assertEqual(summary["duration_s"], 125)
        self.assertEqual(summary["hub_job_id"], "hub-finish-test")
        self.assertEqual(summary["hub_lease_epoch"], 10)

        # Check handoff and summary files
        handoff_file = self.state_dir / "runs" / f"{run_id}.handoff.md"
        summary_file = self.state_dir / "runs" / f"{run_id}.json"
        agent_file = self.state_dir / "runs" / f"{run_id}.agent.json"

        self.assertTrue(handoff_file.exists())
        self.assertIn("Completed maintenance run", handoff_file.read_text())
        self.assertTrue(summary_file.exists())
        self.assertTrue(agent_file.exists())

        state = bridge.read_state()
        self.assertEqual(state["status"], "finished")
        self.assertEqual(state["result"], "ok")

    def test_dedicated_supervisor_and_starting_holder_skip(self):
        clock = MockClock()
        bridge1 = GuardedRootBridge(state_dir=str(self.state_dir), clock=clock)
        res1 = bridge1.acquire(hub_job_id="job-ded-1", hub_lease_epoch=1)
        self.assertTrue(res1["acquired"])
        self.assertIsNone(bridge1._supervisor_proc, "Must not spawn dummy sleep(1500) child in acquire()")

        state1 = bridge1.read_state()
        self.assertIn("pid_namespace", state1)
        self.assertNotIn("starttime_ticks", state1)
        self.assertIn("uid", state1)

        # Contender while lock held detects HEALTHY/STARTING and skips without signaling
        kills = []
        bridge2 = GuardedRootBridge(state_dir=str(self.state_dir), clock=clock, kill_handler=lambda pgid, sig: kills.append((pgid, sig)))
        res2 = bridge2.acquire(hub_job_id="job-ded-2", hub_lease_epoch=2)
        self.assertFalse(res2["acquired"])
        self.assertTrue(res2["skip"])
        self.assertEqual(len(kills), 0)

        bridge1.finish(status="ok")
        cleared_state = bridge1.read_state()
        self.assertNotIn("hub_job_id", cleared_state)
        self.assertNotIn("hub_lease_epoch", cleared_state)
        self.assertNotIn("hub_url", cleared_state)

    def test_fence_releases_flock_and_closes_lock_fd(self):
        clock = MockClock()
        bridge = GuardedRootBridge(state_dir=str(self.state_dir), clock=clock)
        res = bridge.acquire(hub_job_id="job-fence-test", hub_lease_epoch=1)
        self.assertTrue(res["acquired"])
        self.assertTrue(bridge.is_lock_held())

        bridge.fence("test-fence")
        self.assertTrue(bridge.is_fenced())
        self.assertFalse(bridge.is_lock_held(), "GuardedRootBridge.fence() must release root.lock flock!")

    def test_acquire_generates_unique_run_ids_within_same_second(self):
        clock = MockClock()
        clock.now = 1700000000.0
        bridge1 = GuardedRootBridge(state_dir=str(self.state_dir), clock=clock)
        res1 = bridge1.acquire(hub_job_id="job-uniq-1", hub_lease_epoch=1)
        self.assertTrue(res1["acquired"])
        run_id_1 = res1["run_id"]
        bridge1.finish(status="ok")

        bridge2 = GuardedRootBridge(state_dir=str(self.state_dir), clock=clock)
        res2 = bridge2.acquire(hub_job_id="job-uniq-2", hub_lease_epoch=2)
        self.assertTrue(res2["acquired"])
        run_id_2 = res2["run_id"]
        bridge2.finish(status="ok")

        self.assertNotEqual(run_id_1, run_id_2, "Two acquisitions within same second must have unique run_ids!")

    def test_default_kill_pgid_rejects_pid_reuse_via_ticks(self):
        import subprocess
        bridge = GuardedRootBridge(state_dir=str(self.state_dir), kill_handler=lambda pgid, sig: None)
        dummy = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
        try:
            pgid = os.getpgid(dummy.pid)
            from agent_hub_host.guarded_root import get_pid_starttime_ticks
            real_ticks = get_pid_starttime_ticks(dummy.pid) or 1000
            (self.state_dir / "state.json").write_text(
                json.dumps({
                    "run_id": "run-pid-reuse",
                    "epoch": 1,
                    "pid": dummy.pid,
                    "pgid": pgid,
                    "starttime_ticks": real_ticks + 999999,  # Mismatched ticks
                }),
                encoding="utf-8"
            )
            with patch("os.killpg") as mock_killpg:
                bridge._default_kill_pgid(pgid, 15)
                mock_killpg.assert_not_called()
        finally:
            dummy.terminate()
            try:
                dummy.wait(timeout=2)
            except Exception:
                dummy.kill()
                dummy.wait(timeout=1)

    def test_f3_inspect_incumbent_resolves_lock_held_none_before_corrupt_or_future_state(self):
        import subprocess
        clock = MockClock(start_time=1700000000.0)
        bridge = GuardedRootBridge(state_dir=str(self.state_dir), clock=clock)
        (self.state_dir / "state.json").write_text("{corrupt json", encoding="utf-8")
        holder = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import fcntl, sys, time; f=open(sys.argv[1], 'w'); fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB); sys.stdout.write('LOCKED\\n'); sys.stdout.flush(); time.sleep(30)",
                str(bridge.lock_path),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            self.assertEqual(holder.stdout.readline().strip(), "LOCKED")
            status, _ = bridge.inspect_incumbent("run-next", lock_held=None)
            self.assertEqual(status, IncumbentStatus.STALE)
        finally:
            try:
                holder.stdout.close()
                holder.stderr.close()
            except Exception:
                pass
            holder.terminate()
            try:
                holder.wait(timeout=2)
            except Exception:
                holder.kill()
                holder.wait(timeout=1)


if __name__ == "__main__":
    unittest.main()
