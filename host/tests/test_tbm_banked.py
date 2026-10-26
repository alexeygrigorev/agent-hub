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
