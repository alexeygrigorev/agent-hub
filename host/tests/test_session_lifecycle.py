"""Synthetic M15 lifecycle coverage. Fake sessions/paths only; no real worktrees."""

import unittest

from agent_hub_host import session_lifecycle as lifecycle

NOW = 1_792_238_400_000


def base(**overrides):
    item = {
        "machine_id": "synth-host",
        "native_id": "11111111-1111-4111-8111-111111111111",
        "tag": "synth:alpha",
        "engine": "synth-engine",
        "workspace": "/synth/ws",
        "starter": "synth-starter",
        "owner": "synth-starter",
        "owner_provenance": "starter",
        "human_owned": False,
        "task_refs": ["synth-task"],
        "last_activity_at": "2026-10-17T10:00:00Z",
        "observed_at": "2026-10-17T10:05:00Z",
        "phase": "running",
    }
    item.update(overrides)
    return item


def at_now():
    return "2026-10-17T12:00:00Z"


class TestClassifySynthetic(unittest.TestCase):
    def test_finished_requires_authoritative_proof_not_banner_alone(self):
        proven = base(report_written=True, screen_done=True, report_at="2026-10-17T11:00:00Z",
                      last_activity_at="2026-10-17T10:00:00Z",
                      authoritative_no_pending_proof=True)
        self.assertEqual(lifecycle.classify_session(proven, now_ms=NOW)["state"], "finished")
        terminal_only = base(terminal_evidence="explicit_completion",
                             terminal_evidence_at="2026-10-17T11:30:00Z",
                             last_activity_at="2026-10-17T10:00:00Z",
                             authoritative_no_pending_proof=True)
        self.assertEqual(lifecycle.classify_session(terminal_only, now_ms=NOW)["state"], "finished")
        unproven = base(report_written=True, screen_done=True, report_at="2026-10-17T11:00:00Z",
                        last_activity_at="2026-10-17T10:00:00Z")
        self.assertNotEqual(lifecycle.classify_session(unproven, now_ms=NOW)["group"], "finished_stale")
        stale_banner = base(report_written=True, screen_done=True, report_at="2026-10-17T09:00:00Z",
                            last_activity_at="2026-10-17T11:30:00Z",
                            authoritative_no_pending_proof=True)
        self.assertNotEqual(lifecycle.classify_session(stale_banner, now_ms=NOW)["group"], "finished_stale")
        blocked = base(report_written=True, screen_done=True, report_at="2026-10-17T11:00:00Z",
                       last_activity_at="2026-10-17T10:00:00Z",
                       authoritative_no_pending_proof=True, pending_obligations=True)
        self.assertNotEqual(lifecycle.classify_session(blocked, now_ms=NOW)["group"], "finished_stale")

    def test_limit_waiting_idle_and_unknown_branches(self):
        limited = base(limit_hit=True, limit_window="5h", limit_reset_at="2026-10-17T14:00:00Z")
        self.assertEqual(lifecycle.classify_session(limited, now_ms=NOW)["group"], "limit_blocked")
        unknown_reset = base(limit_hit=True)
        self.assertEqual(lifecycle.classify_session(unknown_reset, now_ms=NOW)["state"],
                         "limit_blocked_unknown_reset")
        waiting = base(awaiting_alexey=True, awaiting_alexey_at="2026-10-17T11:30:00Z")
        self.assertEqual(lifecycle.classify_session(waiting, now_ms=NOW)["group"], "waiting_alexey")
        missing = base(worktree_missing=True)
        self.assertEqual(lifecycle.classify_session(missing, now_ms=NOW)["state"],
                         "missing_worktree_unknown")
        idle = base(last_activity_at="2026-10-15T10:00:00Z")
        self.assertEqual(lifecycle.classify_session(idle, now_ms=NOW)["state"], "idle")
        self.assertEqual(lifecycle.classify_session(idle, now_ms=NOW)["group"], "active")

    def test_flags_preserved_and_text_opt_in_only(self):
        both = base(worktree_missing=True, awaiting_alexey=True,
                    awaiting_alexey_at="2026-10-17T11:30:00Z",
                    has_unsubmitted_draft=True,
                    observation_note="token=ABCDEFGHIJKLMNOPQRSTUVWXYZ1234")
        classified = lifecycle.classify_session(both, now_ms=NOW)
        self.assertIn("missing_worktree", classified["flags"])
        self.assertIn("waiting_alexey_fresh", classified["flags"])
        self.assertIn("unsubmitted_draft", classified["obligations"])
        self.assertIsNone(classified["sanitized_note"])
        opted = base(observation_note="screen shows done token=ABCDEFGHIJKLMNOPQRSTUVWXYZ1234 ok",
                      note_text_opt_in=True)
        redacted = lifecycle.classify_session(opted, now_ms=NOW)["sanitized_note"] or ""
        self.assertNotIn("ABCDEFGHIJKLMNOPQRSTUVWXYZ1234", redacted)
        inputs = [base(native_id="00000000-0000-4000-8000-%012d" % i,
                       awaiting_alexey=(i == 1), awaiting_alexey_at=at_now() if i == 1 else None,
                       limit_hit=(i == 0), limit_window="5h" if i == 0 else None)
                  for i in range(4)]
        first = lifecycle.project_sessions(inputs, now_ms=NOW, limit=2)
        second = lifecycle.project_sessions(inputs, now_ms=NOW, limit=2)
        self.assertEqual(first, second)
        self.assertEqual(first["total"], 4)
        self.assertIsNotNone(first["next_cursor"])
        paged = lifecycle.project_sessions(inputs, now_ms=NOW, limit=2, cursor=first["next_cursor"])
        self.assertEqual(paged["total"], 4)

    def test_cleanup_plan_only_finished_owned_without_obligations(self):
        finished = dict(lifecycle.classify_session(
            base(report_written=True, screen_done=True, report_at="2026-10-17T11:00:00Z",
                 last_activity_at="2026-10-17T10:00:00Z",
                 authoritative_no_pending_proof=True), now_ms=NOW),
            owner="synth-starter", human_owned=False, pending_obligations=False)
        plan = lifecycle.plan_cleanup([finished], "synth-starter")
        self.assertEqual(len(plan["eligible"]), 1)
        idle_only = dict(lifecycle.classify_session(base(last_activity_at="2026-10-15T10:00:00Z"), now_ms=NOW),
                         owner="synth-starter", human_owned=False, pending_obligations=False)
        shielded = dict(finished, owner="other-coord")
        human = dict(finished, owner="synth-starter", human_owned=True)
        plan = lifecycle.plan_cleanup([idle_only, shielded, human], "synth-starter")
        self.assertEqual(plan["eligible"], [])
        self.assertEqual(len(plan["ineligible"]), 3)

    def test_executor_and_ownership_transfer_seams(self):
        good = {"expected_machine_id": "h", "expected_native_id": "n", "expected_owner": "o",
                "observed_machine_id": "h", "observed_native_id": "n", "observed_owner": "o",
                "busy_or_draft_or_pending": False, "contained": True}
        self.assertTrue(lifecycle.validate_cleanup_target(good)["ok"])
        for key, value in [("observed_native_id", "other"), ("observed_owner", "other"),
                           ("busy_or_draft_or_pending", True), ("contained", False)]:
            bad = dict(good, **{key: value})
            self.assertFalse(lifecycle.validate_cleanup_target(bad)["ok"])
        transfer = {"session_key": "h/n", "current_owner": "synth-starter", "current_epoch": 3,
                    "expected_owner": "synth-starter", "expected_epoch": 3,
                    "new_owner": "synth-next", "actor": "synth-starter", "human_owned": False}
        self.assertTrue(lifecycle.validate_ownership_transfer(transfer)["ok"])
        for key, value in [("expected_epoch", 2), ("actor", "admin"),
                           ("new_owner", "unknown"), ("human_owned", True)]:
            refused = dict(transfer, **{key: value})
            self.assertFalse(lifecycle.validate_ownership_transfer(refused)["ok"])

    def test_status_handoff_dto_seam(self):
        classified = lifecycle.classify_session(base(), now_ms=NOW)
        status = lifecycle.to_status_dto(classified)
        handoff = lifecycle.to_handoff_dto(classified)
        self.assertEqual(status["session_key"], "synth-host/11111111-1111-4111-8111-111111111111")
        self.assertIn("flags", status)
        self.assertIn("obligations", status)
        self.assertTrue(handoff["text_as_data"])


if __name__ == "__main__":
    unittest.main()
