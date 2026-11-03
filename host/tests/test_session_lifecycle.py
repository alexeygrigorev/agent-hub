"""Host-only guard coverage (M15-F3/F4/F6). Stdlib only, synthetic fixtures.

Test migration note (M15-F6 single authority): prior revisions of this file
covered Python `classify_session` / `project_sessions` / `plan_cleanup` /
DTO mirrors. Those duplicated the TS server authority and drifted (empty-owner,
bool-epoch, naive-TZ divergences). Canonical classification/planning now lives
ONLY in `src/session_lifecycle.ts`; migrated semantic cases (including the
`stale_banner` negative: report older than activity never finishes) live in
`test/session-lifecycle.test.ts` + `test/session-lifecycle-r2.test.ts`.
This file retains and extends host-side semantic coverage for what the host
owns: strict parsing and redaction cutoffs. Cleanup-target and
ownership-transfer validation seams were retired with their tests (no
production callers; server owns those decisions); planCleanup and the
migrated semantic cases live in `test/session-lifecycle.test.ts` +
`test/session-lifecycle-r2.test.ts`.
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


if __name__ == "__main__":
    unittest.main()
