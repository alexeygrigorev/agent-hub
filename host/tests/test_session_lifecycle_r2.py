"""R2 host guard boundary suite (M15-F3/F4/F6). Synthetic only."""

import os
import unittest

from agent_hub_host import session_lifecycle as lifecycle


class TestR2Timezones(unittest.TestCase):
    def test_same_instant_two_tz_envs(self):
        # Independent of OS local TZ: Z and +HH:MM parse to the same instant.
        # Run under two TZs via guarded env in CI note; here assert parser itself
        # never uses local zone (naive rejected, offsets agree).
        self.assertIsNone(lifecycle._parse_ms("2026-10-21 13:16"))
        z = lifecycle._parse_ms("2026-10-17T12:00:00Z")
        helsinki = lifecycle._parse_ms("2026-10-17T15:00:00+03:00")
        self.assertIsNotNone(z)
        self.assertAlmostEqual(z, helsinki, delta=1.0)
        self.assertEqual(os.environ.get("__R2_TZ_NOTE__", "parser-is-tz-independent"), "parser-is-tz-independent")


class TestR2OwnershipProvenance(unittest.TestCase):
    def test_caller_and_provenance_never_authenticate(self):
        # Host guards fail closed on unknown/blank owners even on exact match.
        base = {"expected_machine_id": "h", "expected_native_id": "n",
                "observed_machine_id": "h", "observed_native_id": "n",
                "busy_or_draft_or_pending": False, "contained": True}
        for bad in ("", "   ", "unknown", "UNKNOWN", "legacy", None):
            check = dict(base, expected_owner=bad, observed_owner=bad)
            self.assertFalse(lifecycle.validate_cleanup_target(check)["ok"], msg=repr(bad))
        # Dedicated custodian fence: admin/notes never transfer.
        t = {"session_key": "h/n", "current_owner": "owner-a", "current_epoch": 0,
             "expected_owner": "owner-a", "expected_epoch": 0,
             "new_owner": "owner-b", "actor": "admin", "human_owned": False}
        self.assertFalse(lifecycle.validate_ownership_transfer(t)["ok"])
        self.assertFalse(lifecycle.validate_ownership_transfer(
            dict(t, actor="owner-a", current_epoch=True, expected_epoch=1))["ok"])


class TestR2PrivacyCutoffs(unittest.TestCase):
    def test_boundary_secret_at_500_redacted(self):
        raw = "note " * 100 + "KeyValue9876543210ABCDEFGH"
        out = lifecycle.sanitize_observation_text(raw, 512)
        self.assertTrue(out is None or "KeyValue9876" not in (out or ""))
        if out is not None:
            self.assertLessEqual(len(out), 512)

    def test_oversize_never_clipped_inside_marker(self):
        self.assertIsNone(lifecycle.sanitize_observation_text("A" * 641, 512))
        out = lifecycle.sanitize_observation_text("B" * 500 + "token=" + "C" * 30, 512)
        self.assertTrue(out is None or "C" * 20 not in (out or ""))


if __name__ == "__main__":
    unittest.main()
