import unittest

from agent_hub_host.native_sessions import project_session
from agent_hub_host.status_collect import classify_session
from agent_hub_host.status_runtime import native_to_entry


class NativeStateMetadataTests(unittest.TestCase):
    def row(self, **fields):
        return {"id": "owned-session", "tag": "owned", "workspace": "/owned",
                "phase": "running", **fields}

    def test_installed_hook_state_reaches_status_and_retains_timestamp(self):
        row = project_session(self.row(reported_state="waiting", reported_state_at_ms=1234))
        self.assertEqual(row.agent_state_at_ms, 1234)
        self.assertEqual(classify_session(native_to_entry(row))["state"], "waiting")

    def test_legacy_state_keeps_its_own_timestamp(self):
        row = project_session(self.row(agent_state="working", agent_state_at_ms=12,
                                       reported_state="waiting", reported_state_at_ms=34))
        self.assertEqual(row.agent_state_at_ms, 12)
        self.assertEqual(classify_session(native_to_entry(row))["state"], "working")

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
