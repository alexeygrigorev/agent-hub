"""Host-only guard coverage (M15-F3/F4/F6). Stdlib only, synthetic fixtures.

Test migration note (M15-F6 single authority): prior revisions of this file
covered Python `classify_session` / `project_sessions` / `plan_cleanup` /
DTO mirrors. Those duplicated the TS server authority and drifted (empty-owner,
bool-epoch, naive-TZ divergences). Canonical classification/planning now lives
ONLY in `src/session_lifecycle.ts`; migrated semantic cases (including the
`stale_banner` negative: report older than activity never finishes) live in
`test/session-lifecycle.test.ts` + `test/session-lifecycle-r2.test.ts`.
This file retains and extends host-side semantic coverage for what the host
owns: strict parsing, redaction cutoffs, target binding, and epoch fencing.
No failures were deleted to hide control examples; divergences are now
explicit failure assertions below.
"""

import unittest

from agent_hub_host import session_lifecycle as lifecycle


class TestStrictParse(unittest.TestCase):
    def test_naive_missing_malformed_rejected_no_local_guess(self):
        self.assertIsNone(lifecycle._parse_ms(None))
        self.assertIsNone(lifecycle._parse_ms(""))
        self.assertIsNone(lifecycle._parse_ms("corrupt"))
        self.assertIsNone(lifecycle._parse_ms("2026-10-21 13:16"))
        self.assertIsNone(lifecycle._parse_ms("2026-10-17T14:00:00"))
        self.assertIsNotNone(lifecycle._parse_ms("2026-10-17T14:00:00Z"))
        self.assertIsNotNone(lifecycle._parse_ms("2026-10-17T14:00:00+02:00"))

    def test_aware_timestamps_agree_independent_of_local_tz(self):
        # Same instant expressed with Z vs +02:00 must parse to the same ms.
        z = lifecycle._parse_ms("2026-10-17T12:00:00Z")
        plus2 = lifecycle._parse_ms("2026-10-17T14:00:00+02:00")
        self.assertIsNotNone(z)
        self.assertAlmostEqual(z, plus2, delta=1.0)


class TestSanitizeCutoffs(unittest.TestCase):
    def test_short_secret_redacted(self):
        redacted = lifecycle.sanitize_observation_text(
            "screen shows done token=ABCDEFGHIJKLMNOPQRSTUVWXYZ1234 ok") or ""
        self.assertNotIn("ABCDEFGHIJKLMNOPQRSTUVWXYZ1234", redacted)

    def test_truncation_prefix_leak_closed(self):
        # Secret starting at index 500 must not leak its 12-char prefix.
        raw = "note " * 100 + "KeyValue9876543210ABCDEFGH"
        out = lifecycle.sanitize_observation_text(raw, 512)
        self.assertTrue(out is None or "KeyValue9876" not in out)

    def test_oversize_dropped_not_clipped(self):
        raw = "x" * 640 + "KeyValue9876543210ABCDEFGH" + "y" * 100
        self.assertIsNone(lifecycle.sanitize_observation_text(raw, 512))

    def test_no_partial_marker_tail(self):
        raw = "note " * 100 + "KeyValue9876543210ABCDEFGH"
        out = lifecycle.sanitize_observation_text(raw, 512) or ""
        # Either full marker or no bracket fragment at the cut edge.
        tail = out[-10:]
        self.assertFalse(tail.startswith("[") and tail != "[redacted]" and "[redacted]".startswith(tail))


class TestExecutorAndOwnershipGuards(unittest.TestCase):
    def test_target_binding_strict(self):
        good = {"expected_machine_id": "h", "expected_native_id": "n", "expected_owner": "o",
                "observed_machine_id": "h", "observed_native_id": "n", "observed_owner": "o",
                "busy_or_draft_or_pending": False, "contained": True}
        self.assertTrue(lifecycle.validate_cleanup_target(good)["ok"])
        # Blank/None/unknown bindings fail closed (C3b).
        self.assertFalse(lifecycle.validate_cleanup_target({"contained": True})["ok"])
        blank = dict(good, expected_machine_id="", expected_native_id="",
                     expected_owner="", observed_machine_id="",
                     observed_native_id="", observed_owner="")
        self.assertFalse(lifecycle.validate_cleanup_target(blank)["ok"])
        ws = dict(good, expected_owner="   ", observed_owner="   ")
        self.assertFalse(lifecycle.validate_cleanup_target(ws)["ok"])
        unknown = dict(good, expected_owner="unknown", observed_owner="unknown")
        self.assertFalse(lifecycle.validate_cleanup_target(unknown)["ok"])
        legacy = dict(good, expected_owner="legacy", observed_owner="legacy")
        self.assertFalse(lifecycle.validate_cleanup_target(legacy)["ok"])
        for key, value in [("observed_native_id", "other"), ("observed_owner", "other"),
                           ("busy_or_draft_or_pending", True), ("contained", False)]:
            bad = dict(good, **{key: value})
            self.assertFalse(lifecycle.validate_cleanup_target(bad)["ok"])

    def test_ownership_epochs_strict_no_bool_negative(self):
        transfer = {"session_key": "h/n", "current_owner": "synth-starter", "current_epoch": 3,
                    "expected_owner": "synth-starter", "expected_epoch": 3,
                    "new_owner": "synth-next", "actor": "synth-starter", "human_owned": False}
        self.assertTrue(lifecycle.validate_ownership_transfer(transfer)["ok"])
        for key, value in [("expected_epoch", 2), ("actor", "admin"),
                           ("new_owner", "unknown"), ("new_owner", ""),
                           ("new_owner", "   "), ("new_owner", "legacy"),
                           ("human_owned", True)]:
            refused = dict(transfer, **{key: value})
            self.assertFalse(lifecycle.validate_ownership_transfer(refused)["ok"])
        # Python bool-is-int pitfall: True must NOT pass as epoch 1.
        self.assertFalse(lifecycle.validate_ownership_transfer(
            dict(transfer, current_epoch=True, expected_epoch=1))["ok"])
        self.assertFalse(lifecycle.validate_ownership_transfer(
            dict(transfer, current_epoch=False, expected_epoch=0))["ok"])
        # Negatives and non-safe/non-int fail.
        self.assertFalse(lifecycle.validate_ownership_transfer(
            dict(transfer, current_epoch=-1, expected_epoch=-1))["ok"])
        self.assertFalse(lifecycle.validate_ownership_transfer(
            dict(transfer, current_epoch=3.5, expected_epoch=3.5))["ok"])
        self.assertFalse(lifecycle.validate_ownership_transfer(
            dict(transfer, current_epoch="3", expected_epoch="3"))["ok"])


if __name__ == "__main__":
    unittest.main()
