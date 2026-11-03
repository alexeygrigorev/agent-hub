"""Stop under load uses one exact native identity, never a host-wide list."""
import json
import subprocess
import unittest
from unittest.mock import patch

from agent_hub_host import native_sessions


def _sid(index):
    return "00000000-0000-4000-8000-%012x" % index


def _row(session_id, *, phase="running", empty=False):
    return {
        "id": session_id, "tag": "hub-under-load", "workspace": "/repo",
        "engine": "codex", "created_at_ms": 123456, "phase": phase,
        "worker_pid": None, "workload_pid": None, "processes": 2,
        "containment_empty": empty,
    }


class DirectStopUnderLoadTests(unittest.TestCase):
    def test_35_stops_use_exact_status_kill_and_empty_containment(self):
        calls = []

        def native(_executable, args, **_kwargs):
            calls.append(list(args))
            if args[0] == "status":
                phase = "exited" if any(c[0] == "kill" and c[1] == args[1] for c in calls) else "running"
                return subprocess.CompletedProcess(args, 0, json.dumps(_row(args[1], phase=phase, empty=phase == "exited")), "")
            if args[0] == "kill":
                return subprocess.CompletedProcess(args, 0, "{}", "")
            raise AssertionError("unexpected native operation")

        with patch.object(native_sessions, "list_sessions", side_effect=AssertionError("global native list under load")):
            with patch.object(native_sessions, "run_native", side_effect=native):
                for index in range(1, 36):
                    target = native_sessions.NativeTarget(
                        session_id=_sid(index), tag="hub-under-load", workspace="/repo",
                        created_at_ms=123456, engine="codex")
                    result = native_sessions.stop_session("aplexer", target)
                    self.assertEqual(result.state, "stopped")
                    self.assertIn("containment", result.reason)

        self.assertEqual(sum(call[0] == "kill" for call in calls), 35)
        self.assertEqual(sum(call[0] == "status" for call in calls), 70)
        self.assertTrue(all(call[0] != "list" for call in calls))
        for call in calls:
            if call[0] == "kill":
                self.assertEqual(call[1], _sid(int(call[1].rsplit("-", 1)[1], 16)))

    def test_kill_exit_zero_without_empty_containment_is_uncertain(self):
        calls = []

        def native(_executable, args, **_kwargs):
            calls.append(list(args))
            if args[0] == "status":
                return subprocess.CompletedProcess(args, 0, json.dumps(_row(args[1], phase="exited", empty=False)), "")
            return subprocess.CompletedProcess(args, 0, "{}", "")

        with patch.object(native_sessions, "list_sessions", side_effect=AssertionError("global native list under load")):
            with patch.object(native_sessions, "run_native", side_effect=native):
                with self.assertRaises(native_sessions.SessionUncertain):
                    native_sessions.stop_session("aplexer", native_sessions.NativeTarget(
                        session_id=_sid(99), tag="hub-under-load", workspace="/repo",
                        created_at_ms=123456, engine="codex"))
        self.assertEqual([call[0] for call in calls], ["status", "kill", "status"])

    def test_status_identity_mismatch_refuses_before_kill(self):
        sid = _sid(100)
        def native(_executable, args, **_kwargs):
            return subprocess.CompletedProcess(args, 0, json.dumps(_row(_sid(101))), "")

        with patch.object(native_sessions, "run_native", side_effect=native) as run:
            with self.assertRaises(native_sessions.SessionControlError):
                native_sessions.stop_session("aplexer", native_sessions.NativeTarget(
                    session_id=sid, tag="hub-under-load", workspace="/repo",
                    created_at_ms=123456, engine="codex"))
        run.assert_called_once()
