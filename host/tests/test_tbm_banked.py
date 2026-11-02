import datetime
import unittest
from agent_hub_host.quota_snapshot import sanitize_provider


class BankedGrantSanitizerTest(unittest.TestCase):
    def test_keeps_only_bounded_grant_metadata(self):
        now = datetime.datetime(2026, 10, 26, tzinfo=datetime.timezone.utc)
        raw = {"status": "ok", "details": {"banked_resets": [
            {"expires_at": "2026-10-29T19:01:46Z", "available": True, "label": "Full reset", "private": "discard"},
            {"expires_at": "bad", "available": True, "label": "Full reset"},
        ]}, "windows": {}}
        grant = sanitize_provider("codex", raw, now)["details"]["banked_resets"]
        self.assertEqual(grant, [{"expires_at": "2026-10-29T19:01:46Z", "available": True, "label": "Full reset"}])

    def test_preserves_probe_failure_marker_and_drops_overlong_markers(self):
        # Intake 7bf6f6ee AC3: an ok-status empty grant list with this error
        # is explicit unknown, never "no banked resets".
        now = datetime.datetime(2026, 10, 26, tzinfo=datetime.timezone.utc)
        raw = {"status": "ok", "details": {"banked_resets": [], "banked_resets_error": "quota_unreachable"}, "windows": {}}
        details = sanitize_provider("codex", raw, now)["details"]
        self.assertEqual(details["banked_resets"], [])
        self.assertEqual(details["banked_resets_error"], "quota_unreachable")
        for bad in ("", "x" * 257, 42, True, None):
            details = sanitize_provider("codex", {"status": "ok", "details": {"banked_resets": [], "banked_resets_error": bad}, "windows": {}}, now)["details"]
            self.assertNotIn("banked_resets_error", details)
