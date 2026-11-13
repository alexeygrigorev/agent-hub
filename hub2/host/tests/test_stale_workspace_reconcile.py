import tempfile
import unittest
from itertools import count
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from hub2_agent_hub_host.agent import Bridge, Journal


class _StopServe(BaseException):
    pass


class _Socket:
    def __init__(self):
        self.entered = False

    def __enter__(self):
        self.entered = True
        return self

    def __exit__(self, *_):
        return False

    def recv(self, timeout=None):
        raise _StopServe()


class _Client:
    def __init__(self, socket):
        self.socket_value = socket
        self.socket_calls = []
        self.heartbeats = []

    def heartbeat(self, labels, engines=None, capabilities=None, native_sessions=None):
        self.heartbeats.append((labels, engines, capabilities, native_sessions))

    def socket(self, machine_id):
        self.socket_calls.append(machine_id)
        return self.socket_value

    def request(self, method, path, body=None, key=None):
        return {}


class StaleWorkspaceReconcileTests(unittest.TestCase):
    def test_missing_workspace_row_does_not_block_heartbeat_or_websocket(self):
        self.assert_unbound_workspace_stays_connected()

    def test_unavailable_native_census_does_not_block_heartbeat_or_websocket(self):
        inventory = Mock(side_effect=RuntimeError('native_census_unavailable'))
        self.assert_unbound_workspace_stays_connected(inventory, census_thread=True)
        # Collection is never run inline by the control loop.
        inventory.assert_not_called()

    def assert_unbound_workspace_stays_connected(self, inventory=None, census_thread=False):
        with tempfile.TemporaryDirectory() as temp:
            journal = Journal(Path(temp) / "journal.sqlite")
            self.addCleanup(journal.db.close)
            missing_workspace = Path(temp) / "removed-worktree"
            with journal.db:
                journal.db.execute(
                    "INSERT INTO launches(operation_id,session_id,epoch,digest,workspace,tag,native_id,state,interactive) "
                    "VALUES(?,?,?,?,?,?,NULL,'aborting',0)",
                    ("stale-operation", "stale-session", 1, "digest", str(missing_workspace),
                     "task-hub2-stale-session"),
                )

            socket = _Socket()
            client = _Client(socket)
            bridge = Bridge.__new__(Bridge)
            bridge.client = client
            bridge.journal = journal
            bridge.launcher = SimpleNamespace(
                capabilities=lambda: {"shell": {"headless"}},
                aborted_identity=Mock(side_effect=FileNotFoundError),
                status=Mock(side_effect=FileNotFoundError),
            )
            if inventory is not None:
                bridge.launcher.inventory = inventory
            bridge.engines = {"shell"}
            bridge.machine_id = None
            bridge.budget_paused_until = 0
            bridge.budget_safety_paused_until = 0
            bridge.executable = "unused"
            bridge.runner = None
            bridge.lookup = Mock(side_effect=AssertionError("unavailable workspace must not be queried"))

            ticks = count(0, 31)
            with patch("hub2_agent_hub_host.agent.time.monotonic", side_effect=lambda: next(ticks)), patch(
                    "hub2_agent_hub_host.agent.threading.Thread") as thread, patch(
                    "hub2_agent_hub_host.agent.time.sleep",
                    side_effect=AssertionError("unexpected daemon reconnect")):
                with self.assertLogs("hub2_agent_hub_host.agent", level="WARNING") as captured:
                    with self.assertRaises(_StopServe):
                        bridge.serve("machine-1", ["headless"])

            # Heartbeat thread, plus (only with a census) the single background observer.
            self.assertEqual(thread.return_value.start.call_count, 2 if census_thread else 1)
            self.assertGreaterEqual(len(client.heartbeats), 2)
            self.assertEqual(client.heartbeats[0][3], [{
                "session_id": "stale-session",
                "native_id": None,
                "phase": "unknown",
                "last_activity_at": None,
                "exit_code": None,
                "containment_empty": False,
            }])
            self.assertEqual(client.socket_calls, ["machine-1"])
            self.assertTrue(socket.entered)
            self.assertEqual(len(captured.records), 1 if census_thread else 2)
            self.assertIn("unavailable workspace", captured.output[0])
            if not census_thread:
                self.assertIn("Native census unavailable", captured.output[1])
            bridge.launcher.aborted_identity.assert_not_called()
            bridge.launcher.status.assert_not_called()
            bridge.lookup.assert_not_called()
            state = journal.db.execute(
                "SELECT state,native_id,legacy_failure_json FROM launches WHERE session_id='stale-session'"
            ).fetchone()
            self.assertEqual(tuple(state), ("aborting", None, None))
            self.assertFalse(journal.db.execute("SELECT 1 FROM outbox").fetchone())

    def test_missing_workspace_does_not_report_native_process_stopped(self):
        with tempfile.TemporaryDirectory() as temp:
            journal = Journal(Path(temp) / "journal.sqlite")
            self.addCleanup(journal.db.close)
            missing_workspace = Path(temp) / "removed-worktree"
            native_id = "d6cf9872-8597-48a5-be64-990cdef4db31"
            with journal.db:
                journal.db.execute(
                    "INSERT INTO launches(operation_id,session_id,epoch,digest,workspace,tag,native_id,state,interactive) "
                    "VALUES(?,?,?,?,?,?,?,'running',0)",
                    ("running-operation", "running-session", 1, "digest", str(missing_workspace),
                     "task-hub2-running-session", native_id),
                )

            socket = _Socket()
            client = _Client(socket)
            bridge = Bridge.__new__(Bridge)
            bridge.client = client
            bridge.journal = journal
            bridge.launcher = SimpleNamespace(
                capabilities=lambda: {"shell": {"headless"}},
                aborted_identity=Mock(),
                status=Mock(),
                retired=Mock(return_value=None),
            )
            bridge.engines = {"shell"}
            bridge.machine_id = None
            bridge.budget_paused_until = 0
            bridge.budget_safety_paused_until = 0
            bridge.executable = "unused"
            bridge.runner = None
            bridge.lookup = Mock(side_effect=AssertionError("unavailable workspace must not be queried"))

            with patch("hub2_agent_hub_host.agent.threading.Thread") as thread, patch(
                    "hub2_agent_hub_host.agent.time.sleep",
                    side_effect=AssertionError("unexpected daemon reconnect")):
                with self.assertLogs("hub2_agent_hub_host.agent", level="WARNING") as captured:
                    with self.assertRaises(_StopServe):
                        bridge.serve("machine-1", ["headless"])

            thread.return_value.start.assert_called_once_with()
            self.assertEqual(client.heartbeats[0][3], [{
                "session_id": "running-session",
                "native_id": native_id,
                "phase": "unknown",
                "last_activity_at": None,
                "exit_code": None,
                "containment_empty": False,
            }])
            self.assertEqual(client.socket_calls, ["machine-1"])
            self.assertTrue(socket.entered)
            self.assertEqual(len(captured.records), 1)
            bridge.launcher.aborted_identity.assert_not_called()
            bridge.launcher.status.assert_not_called()
            bridge.lookup.assert_not_called()
            bridge.launcher.retired.assert_called_once()
            selected, expected = bridge.launcher.retired.call_args.args
            self.assertEqual(selected, native_id)
            self.assertEqual(expected["workspace"], str(missing_workspace))
            state = journal.db.execute(
                "SELECT state FROM launches WHERE session_id='running-session'"
            ).fetchone()[0]
            self.assertEqual(state, "running")


if __name__ == "__main__":
    unittest.main()
