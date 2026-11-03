"""Quota normalization remains available to the Hub-server collector only."""

import datetime
import unittest

from agent_hub_host import quota_snapshot


class TestServerQuotaNormalization(unittest.TestCase):
    def test_snapshot_sanitizes_a_hub_server_account_reading(self):
        now = datetime.datetime(2026, 10, 14, 5, 31, tzinfo=datetime.timezone.utc)
        raw = {
            "codex": {
                "status": "ok",
                "error": None,
                "observed_at": "2026-10-14T05:30:00Z",
                "details": {"limit_reached": False, "token": "must-not-escape"},
                "windows": {
                    "7d": {"percent_remaining": 54.0, "reset_at": "2026-10-21T00:00:00Z"}
                },
            }
        }
        snapshot = quota_snapshot.create_quota_snapshot(raw, "hub-server", current_time=now)
        self.assertIn("codex", snapshot["routes"])
        self.assertEqual(snapshot["routes"]["codex"]["windows"]["7d"]["percent_remaining"], 54.0)
        self.assertNotIn("token", str(snapshot))

    def test_host_collection_and_submission_apis_are_removed(self):
        for name in ("QuotaCollector", "post_quota_snapshot", "get_quota_snapshot", "quse_argv"):
            with self.subTest(name=name):
                self.assertFalse(hasattr(quota_snapshot, name))

    def test_snapshot_serializer_rejects_machine_identity(self):
        with self.assertRaisesRegex(ValueError, "Hub server"):
            quota_snapshot.create_quota_snapshot({}, "machine-1")


if __name__ == "__main__":
    unittest.main()
