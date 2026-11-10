import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from agent_hub_host.agent import Bridge, Journal


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
            bridge.engines = {"shell"}
            bridge.machine_id = None
            bridge.budget_paused_until = 0
            bridge.budget_safety_paused_until = 0
            bridge.executable = "unused"
            bridge.runner = None
            bridge.lookup = Mock(side_effect=AssertionError("unavailable workspace must not be queried"))

            with patch("agent_hub_host.agent.threading.Thread") as thread:
                with self.assertLogs("agent_hub_host.agent", level="WARNING") as captured:
                    with self.assertRaises(_StopServe):
                        bridge.serve("machine-1", ["headless"])

            thread.return_value.start.assert_called_once_with()
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
            self.assertEqual(len(captured.records), 1)
            self.assertIn("unavailable workspace", captured.output[0])
            bridge.launcher.aborted_identity.assert_not_called()
            bridge.launcher.status.assert_not_called()
            bridge.lookup.assert_not_called()
            state = journal.db.execute(
                "SELECT state FROM launches WHERE session_id='stale-session'"
            ).fetchone()[0]
            self.assertEqual(state, "aborting")
            self.assertFalse(journal.db.execute("SELECT 1 FROM outbox").fetchone())


if __name__ == "__main__":
    unittest.main()
