import os
import tempfile
import unittest

from agent_hub_host import orphan_sweeper as sweeper


class TestOrphanSweeper(unittest.TestCase):
    def test_strip_deleted_suffix(self):
        with tempfile.TemporaryDirectory() as tmp:
            real = os.path.join(tmp, "wt")
            os.mkdir(real)
            # Existing literal "(deleted)" path is kept.
            literal = real + " (deleted)"
            os.mkdir(literal)
            self.assertEqual(sweeper._strip_deleted_suffix(literal), literal)

    def test_workspace_missing(self):
        self.assertTrue(sweeper.workspace_missing("/nonexistent-orphan-wt-xyz"))
        self.assertFalse(sweeper.workspace_missing(None))
        self.assertFalse(sweeper.workspace_missing(""))
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(sweeper.workspace_missing(tmp))
            self.assertTrue(sweeper.workspace_missing(tmp + " (deleted)"))

    def test_unknown_workspace_is_not_missing(self):
        self.assertFalse(sweeper.native_missing({"id": "x", "tag": "task-hub-x"}))
        self.assertFalse(sweeper.native_missing({"id": "x", "tag": "task-hub-x", "workspace": ""}))
        self.assertTrue(sweeper.is_interactive({"tag": ""}, None))
        self.assertTrue(sweeper.is_interactive({}, None))

    def test_proc_cwd_deleted(self):
        self.assertIsNone(sweeper.proc_cwd_deleted(-1))
        self.assertIsNone(sweeper.proc_cwd_deleted(99999999))
        self.assertTrue(sweeper.proc_cwd_deleted(123, readlink=lambda p: "/repo (deleted)"))
        self.assertFalse(sweeper.proc_cwd_deleted(123, readlink=lambda p: "/repo"))

    def test_interactive_exempt(self):
        headless = {"tag": "task-hub-abc", "id": "x"}
        manual = {"tag": "my-notes", "id": "y"}
        self.assertFalse(sweeper.is_interactive(headless, {"human_owned": False}))
        self.assertTrue(sweeper.is_interactive(headless, {"human_owned": True}))
        self.assertTrue(sweeper.is_interactive(headless, {"starter": "admin"}))
        self.assertTrue(sweeper.is_interactive(headless, {"starter": "human:alexey"}))
        self.assertTrue(sweeper.is_interactive(manual, None))
        self.assertTrue(sweeper.is_interactive(manual, {"human_owned": False}))

    def test_classify_grace(self):
        row_missing = {"id": "a" * 32, "tag": "task-hub-x", "workspace": "/nonexistent-xyz"}
        self.assertEqual(sweeper.classify(row_missing, {"human_owned": False}, None, 1000.0, 7200.0), "waiting")
        self.assertEqual(sweeper.classify(row_missing, {"human_owned": False}, 1000.0 - 7200.0 - 1, 1000.0, 7200.0), "stop")
        self.assertEqual(sweeper.classify(row_missing, {"human_owned": False}, 999.0, 1000.0, 7200.0), "waiting")
        manual = {"id": "b" * 32, "tag": "notes", "workspace": "/nonexistent-xyz"}
        self.assertEqual(sweeper.classify(manual, None, 0.0, 100000.0, 7200.0), "report")

    def test_sweep_once_stop_and_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing_ws = os.path.join(tmp, "gone")
            headless = {"id": "c" * 32, "tag": "task-hub-1", "workspace": missing_ws,
                        "cwd": missing_ws, "workload_pid": 99999999, "worker_pid": 99999999}
            manual = {"id": "d" * 32, "tag": "notes", "workspace": missing_ws,
                      "cwd": missing_ws, "workload_pid": 99999999, "worker_pid": 99999999}
            stopped = []
            observed = []

            def stop_fn(row):
                stopped.append(row["id"])
                return True

            def observe_fn(row, hub_info):
                observed.append(row["id"])
                return True

            # First pass: waiting (grace not expired).
            out = []
            res = sweeper.sweep_once(
                sessions=[headless, manual],
                hub_lookup=lambda sid: {"human_owned": False} if sid == headless["id"] else {"human_owned": True},
                now=1000000.0, grace_hours=2.0,
                state_path=os.path.join(tmp, "state.json"),
                stop_fn=stop_fn, observe_fn=observe_fn,
                out=out.append,
            )
            self.assertEqual(res["waiting"], 1)
            self.assertEqual(res["report"], 1)
            self.assertEqual(stopped, [])

            # Second pass after grace, dry-run by default: would-stop, no kill.
            out2 = []
            res2 = sweeper.sweep_once(
                sessions=[headless, manual],
                hub_lookup=lambda sid: {"human_owned": False} if sid == headless["id"] else {"human_owned": True},
                now=1000000.0 + 7200.0 + 5, grace_hours=2.0,
                state_path=os.path.join(tmp, "state.json"),
                stop_fn=stop_fn, observe_fn=observe_fn,
                out=out2.append,
            )
            self.assertEqual(res2["would_stop"], 1)
            self.assertEqual(stopped, [])
            self.assertIn(headless["id"], observed)

            # Explicit apply=True permits the stop.
            out3 = []
            res3 = sweeper.sweep_once(
                sessions=[headless, manual],
                hub_lookup=lambda sid: {"human_owned": False} if sid == headless["id"] else {"human_owned": True},
                now=1000000.0 + 7200.0 + 5, grace_hours=2.0,
                state_path=os.path.join(tmp, "state.json"),
                stop_fn=stop_fn, observe_fn=observe_fn,
                out=out3.append, apply=True,
            )
            self.assertEqual(res3["stopped"], 1)
            self.assertIn(headless["id"], stopped)
            self.assertIn(manual["id"], observed)


if __name__ == "__main__":
    unittest.main()
