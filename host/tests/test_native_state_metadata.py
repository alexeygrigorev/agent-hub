import unittest
import time

from agent_hub_host.native_sessions import project_session
from agent_hub_host.status_collect import classify_session
from agent_hub_host.status_runtime import native_to_entry


class NativeStateMetadataTests(unittest.TestCase):
    def row(self, **fields):
        return {"id": "owned-session", "tag": "owned", "workspace": "/owned",
                "phase": "running", **fields}

    def test_installed_hook_state_reaches_status_and_retains_timestamp(self):
        stamp = int(time.time() * 1000)
        row = project_session(self.row(reported_state="waiting", reported_state_at_ms=stamp))
        self.assertEqual(row.agent_state_at_ms, stamp)
        self.assertEqual(classify_session(native_to_entry(row))["state"], "waiting")

    def test_legacy_state_keeps_its_own_timestamp(self):
        row = project_session(self.row(agent_state="working", agent_state_at_ms=12,
                                       reported_state="waiting", reported_state_at_ms=34))
        self.assertEqual(row.agent_state_at_ms, 12)
        self.assertEqual(row.agent_state, "working")

    def test_stale_installed_hook_is_unknown_in_current_status(self):
        row = project_session(self.row(reported_state="idle", reported_state_at_ms=1234))
        entry = native_to_entry(row)
        self.assertEqual(classify_session(entry)["state"], "unknown")
        self.assertIn("native_hook_state_stale", entry["note"])

    def test_future_hook_is_not_a_current_state(self):
        stamp = int(time.time() * 1000) + 120000
        row = project_session(self.row(reported_state="working", reported_state_at_ms=stamp))
        self.assertEqual(classify_session(native_to_entry(row))["state"], "unknown")

    def test_process_phase_is_not_an_agent_hook(self):
        row = project_session(self.row())
        self.assertIsNone(row.agent_state_at_ms)
        self.assertEqual(classify_session(native_to_entry(row))["state"], "unknown")

    def test_malformed_hook_is_not_guessed_and_bool_is_not_a_timestamp(self):
        row = project_session(self.row(reported_state=True, reported_state_at_ms=True))
        self.assertIsNone(row.agent_state_at_ms)
        self.assertEqual(classify_session(native_to_entry(row))["state"], "unknown")


if __name__ == "__main__":
    unittest.main()
