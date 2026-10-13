"""Unit tests for Oversight Policy and Intervention Ledger.

Covers all 11 required scenarios:
1. healthy working with pending work no nudge
2. frozen idle no nudge
3. idle-ready exactly one
4. missed checkpoint tied obligation not heartbeat
5. completion owner acknowledgment flow
6. ambiguous binding/unknown no side effect
7. restart/concurrent duplicate
8. send failure before vs uncertain delivery no replay
9. forged/stale session/custody/first-action rejection
10. principal bus-only
11. capacity honest reject
Plus timeout escalation, sanitization, and terminal pruning.
"""

from __future__ import annotations

import os
import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import datetime
import json
import os
import shutil
import tempfile
import unittest
from typing import Any, Dict, List, Optional, Tuple

from agent_hub_host import oversight
from agent_hub_host.oversight import (
    ActionReceipt,
    CapacityExceededError,
    CustodyAcceptance,
    GuardAuthorizationError,
    InterventionLedger,
    InterventionRecord,
    MailboxReadAck,
    NativeSessionBinding,
    ObservationEvidence,
    OutboxMessage,
    OversightDecision,
    OversightPolicy,
    SecurityBindingError,
    TaskObligation,
    CompletionOutcome,
    TransportError,
    TransportSendFailedBeforeDelivery,
    TransportUncertainError,
    CLASSIFICATION_CURRENT_USEFUL_WORK,
    CLASSIFICATION_DONE,
    CLASSIFICATION_IDLE_WITH_READY_WORK,
    CLASSIFICATION_MISSED_CHECKPOINT,
    CLASSIFICATION_PROTECTED,
    CLASSIFICATION_UNKNOWN_OR_STALE,
    CLASSIFICATION_COMPLETED_AWAITING_OWNER,
    INTERVENTION_CHECKPOINT_MISSED,
    INTERVENTION_NUDGE_IDLE,
    INTERVENTION_RESULT_ACK_PENDING,
    OUTBOX_FAILED,
    OUTBOX_PENDING,
    OUTBOX_SENT,
    OUTBOX_UNCERTAIN,
    ROUTE_BUS,
    ROUTE_NATIVE_MAILBOX,
    STATE_COMPLETED,
    STATE_CUSTODY_ACCEPTED,
    STATE_DISPATCHED_SENT,
    STATE_DISPATCHED_UNCERTAIN,
    STATE_ESCALATION_NEEDED,
    STATE_FENCED,
    STATE_FIRST_ACTION_OBSERVED,
    STATE_MAILBOX_READ_ACK,
    STATE_OUTBOX_PENDING,
)


class MockTransport:
    def __init__(self):
        self.sent_messages: List[Dict[str, Any]] = []
        self.fail_pre_send = False
        self.fail_uncertain = False

    def send(
        self,
        route: str,
        recipient_tag: str,
        payload: Dict[str, Any],
        idempotency_key: str,
    ) -> Dict[str, Any]:
        if self.fail_pre_send:
            raise TransportSendFailedBeforeDelivery("Transport pre-send socket error")
        if self.fail_uncertain:
            raise TransportUncertainError("Transport connection dropped during transmission")
        entry = {
            "route": route,
            "recipient_tag": recipient_tag,
            "payload": payload,
            "idempotency_key": idempotency_key,
        }
        self.sent_messages.append(entry)
        return {"status": "ok", "delivered": True}


class MockGuard:
    def __init__(self, authorized: bool = True, reject_reason: str = "Guard rejected"):
        self.authorized = authorized
        self.reject_reason = reject_reason
        self.checked_messages: List[OutboxMessage] = []

    def authorize_dispatch(self, message: OutboxMessage) -> Tuple[bool, str]:
        self.checked_messages.append(message)
        if not self.authorized:
            return False, self.reject_reason
        return True, "Authorized"


class TestOversightPolicyAndLedger(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="oversight_test_")
        self.db_path = os.path.join(self.test_dir, "ledger.db")
        self.now = datetime.datetime(2026, 10, 13, 12, 0, 0, tzinfo=datetime.timezone.utc)
        self.clock = lambda: self.now
        self.policy = OversightPolicy(freshness_threshold_seconds=300.0)
        self.ledger = InterventionLedger(self.db_path, clock=self.clock, max_active=100, max_outbox=100)

    def tearDown(self):
        self.ledger.close()
        shutil.rmtree(self.test_dir, ignore_errors=True)

    # 1. Healthy working with pending work no nudge
    def test_healthy_working_with_pending_work_no_nudge(self):
        obligation = TaskObligation(
            id="task-01",
            revision=1,
            owner_tag="worker-1",
            workspace=self.test_dir,
            status="in_progress",
        )
        binding = NativeSessionBinding(
            session_id="ses-01",
            tag="worker-1",
            generation=1,
            workspace=self.test_dir,
            worker_alive=True,
            phase="running",
            reported_state="working",
        )
        observation = ObservationEvidence(
            sample_id="obs-01",
            observed_at=self.now.isoformat(),
            assessment="progress_observed",
            reason_codes=("STREAMING_OUTPUT",),
            empty_composer=False,
            active_tool_calls=("write_to_file",),
            recent_output_bytes=512,
            session_id="ses-01",
            generation=1,
        )

        res = self.policy.classify(obligation, binding, observation, self.now)
        self.assertEqual(res.state, CLASSIFICATION_CURRENT_USEFUL_WORK)

        decision = self.policy.decide(obligation, binding, observation, self.now)
        self.assertIsNone(decision, "Healthy working agent should never receive a nudge")

    # 2. Frozen idle no nudge
    def test_frozen_idle_no_nudge(self):
        obligation = TaskObligation(
            id="task-frozen",
            revision=1,
            owner_tag="worker-frozen",
            workspace=self.test_dir,
            status="frozen",
            protected_reason="Contest freeze administrative hold",
        )
        binding = NativeSessionBinding(
            session_id="ses-frozen",
            tag="worker-frozen",
            generation=1,
            workspace=self.test_dir,
            worker_alive=True,
            phase="running",
            reported_state="idle",
        )
        observation = ObservationEvidence(
            sample_id="obs-frozen",
            observed_at=self.now.isoformat(),
            assessment="idle",
            reason_codes=("EMPTY_PROMPT", "SCREEN_STATIC"),
            empty_composer=True,
            session_id="ses-frozen",
            generation=1,
        )

        res = self.policy.classify(obligation, binding, observation, self.now)
        self.assertEqual(res.state, CLASSIFICATION_PROTECTED)
        self.assertIn("protected", res.reason)

        decision = self.policy.decide(obligation, binding, observation, self.now)
        self.assertIsNone(decision, "Frozen/protected idle agent must not receive a nudge")

    # 3. Idle-ready exactly one
    def test_idle_ready_exactly_one(self):
        obligation = TaskObligation(
            id="task-ready-01",
            revision=1,
            owner_tag="worker-idle",
            workspace=self.test_dir,
            status="ready",
        )
        binding = NativeSessionBinding(
            session_id="ses-idle",
            tag="worker-idle",
            generation=1,
            workspace=self.test_dir,
            worker_alive=True,
            phase="running",
            reported_state="idle",
        )
        observation = ObservationEvidence(
            sample_id="obs-idle",
            observed_at=self.now.isoformat(),
            assessment="idle",
            reason_codes=("EMPTY_PROMPT",),
            empty_composer=True,
            session_id="ses-idle",
            generation=1,
        )

        res = self.policy.classify(obligation, binding, observation, self.now)
        self.assertEqual(res.state, CLASSIFICATION_IDLE_WITH_READY_WORK)

        decision = self.policy.decide(obligation, binding, observation, self.now)
        self.assertIsNotNone(decision)
        self.assertEqual(decision.intervention_type, INTERVENTION_NUDGE_IDLE)

        # Record in ledger
        rec1 = self.ledger.record_decision(decision)
        self.assertEqual(rec1.state, STATE_OUTBOX_PENDING)
        self.assertIsNotNone(rec1.outbox_message_id)

        # Record again (simulating repeated cycle or concurrent callers)
        rec2 = self.ledger.record_decision(decision)
        self.assertEqual(rec1.id, rec2.id, "Dedup must return exact same intervention")
        self.assertEqual(rec1.outbox_message_id, rec2.outbox_message_id)

        # Check outbox count: exactly one message queued
        cur = self.ledger._conn.cursor()
        cur.execute("SELECT COUNT(*) FROM outbox")
        outbox_count = cur.fetchone()[0]
        self.assertEqual(outbox_count, 1, "Exactly one outbox message should be created")

    # 4. Missed checkpoint tied obligation not heartbeat
    def test_missed_checkpoint_tied_obligation_not_heartbeat(self):
        checkpoint_due = (self.now - datetime.timedelta(minutes=10)).isoformat()
        obligation = TaskObligation(
            id="task-checkpoint",
            revision=1,
            owner_tag="worker-cp",
            workspace=self.test_dir,
            status="in_progress",
            checkpoint_due=checkpoint_due,
        )
        # Worker has valid PID and heartbeat (reported_state="working", worker_alive=True)
        binding = NativeSessionBinding(
            session_id="ses-cp",
            tag="worker-cp",
            generation=1,
            workspace=self.test_dir,
            worker_alive=True,
            phase="running",
            reported_state="working",
        )
        # Observation shows spinner only (liveness without productivity)
        observation = ObservationEvidence(
            sample_id="obs-cp",
            observed_at=self.now.isoformat(),
            assessment="busy_unconfirmed",
            reason_codes=("SPINNER_ANIMATION", "CLOCK_ONLY_CHANGE"),
            empty_composer=False,
            session_id="ses-cp",
            generation=1,
        )

        res = self.policy.classify(obligation, binding, observation, self.now)
        self.assertEqual(res.state, CLASSIFICATION_MISSED_CHECKPOINT)
        self.assertIn("expired", res.reason)

        decision = self.policy.decide(obligation, binding, observation, self.now)
        self.assertIsNotNone(decision)
        self.assertEqual(decision.intervention_type, INTERVENTION_CHECKPOINT_MISSED)

    # 5. Completion owner acknowledgment flow
    def test_completion_owner_acknowledgment_flow(self):
        obligation = TaskObligation(
            id="task-complete-01",
            revision=1,
            owner_tag="head-owner",
            workspace=self.test_dir,
            status="completed",
            completed_result={"sha": "abc12345"},
            has_owner_ack=False,
        )
        binding = NativeSessionBinding(
            session_id="ses-owner",
            tag="head-owner",
            generation=1,
            workspace=self.test_dir,
            worker_alive=True,
            phase="running",
            reported_state="idle",
        )
        observation = ObservationEvidence(
            sample_id="obs-owner",
            observed_at=self.now.isoformat(),
            assessment="idle",
            empty_composer=True,
            session_id="ses-owner",
            generation=1,
        )

        res = self.policy.classify(obligation, binding, observation, self.now)
        self.assertEqual(res.state, CLASSIFICATION_COMPLETED_AWAITING_OWNER)

        decision = self.policy.decide(obligation, binding, observation, self.now)
        self.assertIsNotNone(decision)
        self.assertEqual(decision.intervention_type, INTERVENTION_RESULT_ACK_PENDING)

        # Record decision
        rec = self.ledger.record_decision(decision)
        msg_id = rec.outbox_message_id

        # 1. Dispatch
        transport = MockTransport()
        sent = self.ledger.dispatch_outbox_message(msg_id, transport, guard=MockGuard())
        self.assertTrue(sent)
        self.assertEqual(self.ledger.get_intervention(rec.id).state, STATE_DISPATCHED_SENT)

        # 2. Mailbox Read ACK
        read_ack = MailboxReadAck(
            message_id=msg_id,
            read_at=self.now.isoformat(),
            reader_session_id="ses-owner",
            reader_generation=1,
        )
        rec_read = self.ledger.record_mailbox_read_ack(msg_id, read_ack)
        self.assertEqual(rec_read.state, STATE_MAILBOX_READ_ACK)

        # 3. Custody Acceptance
        custody = CustodyAcceptance(
            obligation_id="task-complete-01",
            owner_tag="head-owner",
            session_id="ses-owner",
            generation=1,
            accepted_at=self.now.isoformat(),
        )
        rec_custody = self.ledger.record_custody_acceptance(rec.id, custody)
        self.assertEqual(rec_custody.state, STATE_CUSTODY_ACCEPTED)

        # 4. First Action Observed
        action = ActionReceipt(
            receipt_id="act-01",
            session_id="ses-owner",
            generation=1,
            action_type="tool_execution",
            timestamp=self.now.isoformat(),
            details={"tool": "git_commit"},
        )
        rec_act = self.ledger.record_first_action(rec.id, action)
        self.assertEqual(rec_act.state, STATE_FIRST_ACTION_OBSERVED)

        # 5. Completed Outcome
        outcome = CompletionOutcome(
            obligation_id="task-complete-01",
            owner_tag="head-owner",
            session_id="ses-owner",
            generation=1,
            completed_at=self.now.isoformat(),
            result={"accepted": True},
        )
        rec_final = self.ledger.record_completion_outcome(rec.id, outcome)
        self.assertEqual(rec_final.state, STATE_COMPLETED)

    # 6. Ambiguous binding/unknown no side effect
    def test_ambiguous_binding_unknown_no_side_effect(self):
        obligation = TaskObligation(
            id="task-ambig",
            revision=1,
            owner_tag="worker-1",
            workspace=self.test_dir,
            status="ready",
        )
        # Workspace mismatch
        wrong_workspace = tempfile.mkdtemp(prefix="wrong_ws_")
        binding_mismatch = NativeSessionBinding(
            session_id="ses-bad",
            tag="worker-1",
            generation=1,
            workspace=wrong_workspace,
            worker_alive=True,
        )
        obs = ObservationEvidence(
            sample_id="obs-01",
            observed_at=self.now.isoformat(),
            assessment="idle",
            empty_composer=True,
            session_id="ses-bad",
            generation=1,
        )

        res1 = self.policy.classify(obligation, binding_mismatch, obs, self.now)
        self.assertEqual(res1.state, CLASSIFICATION_UNKNOWN_OR_STALE)
        self.assertIn("workspace mismatch", res1.reason)
        self.assertIsNone(self.policy.decide(obligation, binding_mismatch, obs, self.now))

        # Stale observation (> 300s)
        stale_time = (self.now - datetime.timedelta(seconds=350)).isoformat()
        obs_stale = ObservationEvidence(
            sample_id="obs-stale",
            observed_at=stale_time,
            assessment="idle",
            empty_composer=True,
            session_id="ses-valid",
            generation=1,
        )
        binding_valid = NativeSessionBinding(
            session_id="ses-valid",
            tag="worker-1",
            generation=1,
            workspace=self.test_dir,
            worker_alive=True,
        )
        res2 = self.policy.classify(obligation, binding_valid, obs_stale, self.now)
        self.assertEqual(res2.state, CLASSIFICATION_UNKNOWN_OR_STALE)
        self.assertIn("stale", res2.reason)
        self.assertIsNone(self.policy.decide(obligation, binding_valid, obs_stale, self.now))

        # Verify ledger has 0 records
        cur = self.ledger._conn.cursor()
        cur.execute("SELECT COUNT(*) FROM interventions")
        self.assertEqual(cur.fetchone()[0], 0)
        shutil.rmtree(wrong_workspace, ignore_errors=True)

    # 7. Restart/concurrent duplicate
    def test_restart_concurrent_duplicate(self):
        decision = OversightDecision(
            obligation_id="task-dedup",
            obligation_revision=1,
            owner_tag="worker-dedup",
            workspace=self.test_dir,
            session_id="ses-dedup",
            generation=1,
            intervention_type=INTERVENTION_NUDGE_IDLE,
            route=ROUTE_NATIVE_MAILBOX,
            reason="Idle worker",
            evidence_summary="obs=1",
            notification_payload={"test": 1},
        )
        rec1 = self.ledger.record_decision(decision)

        # Cold restart simulation: close DB connection and open new ledger on same DB
        self.ledger.close()
        new_ledger = InterventionLedger(self.db_path, clock=self.clock, max_active=100, max_outbox=100)
        rec2 = new_ledger.record_decision(decision)

        self.assertEqual(rec1.id, rec2.id)
        self.assertEqual(rec1.dedup_key, rec2.dedup_key)
        self.assertEqual(rec1.outbox_message_id, rec2.outbox_message_id)

        cur = new_ledger._conn.cursor()
        cur.execute("SELECT COUNT(*) FROM interventions")
        self.assertEqual(cur.fetchone()[0], 1)
        cur.execute("SELECT COUNT(*) FROM outbox")
        self.assertEqual(cur.fetchone()[0], 1)
        new_ledger.close()
        # Reopen for tearDown
        self.ledger = InterventionLedger(self.db_path, clock=self.clock, max_active=100, max_outbox=100)

    # 8. Send failure before vs uncertain delivery no replay
    def test_send_failure_before_vs_uncertain_delivery_no_replay(self):
        # Case A: Pre-send failure
        dec_fail = OversightDecision(
            obligation_id="task-fail",
            obligation_revision=1,
            owner_tag="worker-fail",
            workspace=self.test_dir,
            session_id="ses-fail",
            generation=1,
            intervention_type=INTERVENTION_NUDGE_IDLE,
            route=ROUTE_NATIVE_MAILBOX,
            reason="pre-send failure test",
            evidence_summary="ev",
            notification_payload={"test": 1},
        )
        rec_fail = self.ledger.record_decision(dec_fail)
        transport_fail = MockTransport()
        transport_fail.fail_pre_send = True

        sent = self.ledger.dispatch_outbox_message(rec_fail.outbox_message_id, transport_fail, guard=MockGuard())
        self.assertFalse(sent)
        outbox_fail = self.ledger.get_outbox_message(rec_fail.outbox_message_id)
        self.assertEqual(outbox_fail.status, OUTBOX_FAILED)

        # Case B: Uncertain delivery
        dec_unc = OversightDecision(
            obligation_id="task-unc",
            obligation_revision=1,
            owner_tag="worker-unc",
            workspace=self.test_dir,
            session_id="ses-unc",
            generation=1,
            intervention_type=INTERVENTION_NUDGE_IDLE,
            route=ROUTE_NATIVE_MAILBOX,
            reason="uncertain delivery test",
            evidence_summary="ev",
            notification_payload={"test": 2},
        )
        rec_unc = self.ledger.record_decision(dec_unc)
        transport_unc = MockTransport()
        transport_unc.fail_uncertain = True

        sent_unc = self.ledger.dispatch_outbox_message(rec_unc.outbox_message_id, transport_unc, guard=MockGuard())
        self.assertFalse(sent_unc)
        outbox_unc = self.ledger.get_outbox_message(rec_unc.outbox_message_id)
        self.assertEqual(outbox_unc.status, OUTBOX_UNCERTAIN)

        intv_unc = self.ledger.get_intervention(rec_unc.id)
        self.assertEqual(intv_unc.state, STATE_DISPATCHED_UNCERTAIN)

        # Attempt to dispatch uncertain message again: must be refused with TransportError!
        with self.assertRaises(TransportError) as ctx:
            self.ledger.dispatch_outbox_message(rec_unc.outbox_message_id, MockTransport(), guard=MockGuard())
        self.assertIn("uncertain", str(ctx.exception).lower())

    # 9. Forged/stale session/custody/first-action rejection
    def test_forged_stale_session_custody_first_action_rejection(self):
        dec = OversightDecision(
            obligation_id="task-sec",
            obligation_revision=1,
            owner_tag="worker-legit",
            workspace=self.test_dir,
            session_id="ses-legit",
            generation=2,
            intervention_type=INTERVENTION_NUDGE_IDLE,
            route=ROUTE_NATIVE_MAILBOX,
            reason="security binding test",
            evidence_summary="ev",
            notification_payload={"test": 1},
        )
        rec = self.ledger.record_decision(dec)
        msg_id = rec.outbox_message_id
        self.ledger.dispatch_outbox_message(msg_id, MockTransport(), guard=MockGuard())

        # Forged reader session in MailboxReadAck
        forged_read = MailboxReadAck(
            message_id=msg_id,
            read_at=self.now.isoformat(),
            reader_session_id="ses-attacker",
            reader_generation=2,
        )
        with self.assertRaises(SecurityBindingError):
            self.ledger.record_mailbox_read_ack(msg_id, forged_read)

        # Stale generation in CustodyAcceptance
        stale_custody = CustodyAcceptance(
            obligation_id="task-sec",
            owner_tag="worker-legit",
            session_id="ses-legit",
            generation=1,  # Stale generation 1 vs 2
            accepted_at=self.now.isoformat(),
        )
        with self.assertRaises(SecurityBindingError):
            self.ledger.record_custody_acceptance(rec.id, stale_custody)

        # Mismatched owner in CustodyAcceptance
        impostor_custody = CustodyAcceptance(
            obligation_id="task-sec",
            owner_tag="worker-impostor",
            session_id="ses-legit",
            generation=2,
            accepted_at=self.now.isoformat(),
        )
        with self.assertRaises(SecurityBindingError):
            self.ledger.record_custody_acceptance(rec.id, impostor_custody)

        # Test session fencing: generation advances to 3
        fenced_count = self.ledger.fence_superseded_sessions(
            owner_tag="worker-legit",
            workspace=self.test_dir,
            new_session_id="ses-new",
            new_generation=3,
        )
        self.assertEqual(fenced_count, 1)

        fenced_rec = self.ledger.get_intervention(rec.id)
        self.assertEqual(fenced_rec.state, STATE_FENCED)

        # Actions on fenced intervention must fail closed
        valid_read = MailboxReadAck(
            message_id=msg_id,
            read_at=self.now.isoformat(),
            reader_session_id="ses-legit",
            reader_generation=2,
        )
        with self.assertRaises(SecurityBindingError):
            self.ledger.record_mailbox_read_ack(msg_id, valid_read)

    # 10. Principal bus-only
    def test_principal_bus_only(self):
        obligation = TaskObligation(
            id="task-principal",
            revision=1,
            owner_tag="codex-principal",
            workspace=self.test_dir,
            status="ready",
            is_principal=True,
        )
        binding = NativeSessionBinding(
            session_id="ses-p",
            tag="codex-principal",
            generation=1,
            workspace=self.test_dir,
            worker_alive=True,
            phase="running",
            reported_state="idle",
        )
        obs = ObservationEvidence(
            sample_id="obs-p",
            observed_at=self.now.isoformat(),
            assessment="idle",
            empty_composer=True,
            session_id="ses-p",
            generation=1,
        )

        decision = self.policy.decide(obligation, binding, obs, self.now)
        self.assertIsNotNone(decision)
        self.assertEqual(decision.route, ROUTE_BUS, "Principal obligations must be routed bus-only")

        # Now test guard enforcement: if someone tampers with the route in outbox, dispatch is refused
        rec = self.ledger.record_decision(decision)
        msg_id = rec.outbox_message_id

        # Tamper outbox row route to 'native_mailbox'
        with self.ledger._conn:
            self.ledger._conn.execute(
                "UPDATE outbox SET route = ? WHERE message_id = ?",
                (ROUTE_NATIVE_MAILBOX, msg_id),
            )

        with self.assertRaises(GuardAuthorizationError) as ctx:
            self.ledger.dispatch_outbox_message(msg_id, MockTransport(), guard=MockGuard())
        self.assertIn("bus-only", str(ctx.exception))

    # 11. Capacity honest reject
    def test_capacity_honest_reject(self):
        small_ledger = InterventionLedger(
            db_path=":memory:",
            clock=self.clock,
            max_active=2,
            max_outbox=10,
        )

        d1 = OversightDecision(
            obligation_id="task-1",
            obligation_revision=1,
            owner_tag="tag-1",
            workspace=self.test_dir,
            session_id="ses-1",
            generation=1,
            intervention_type=INTERVENTION_NUDGE_IDLE,
            route=ROUTE_NATIVE_MAILBOX,
            reason="r1",
            evidence_summary="e1",
            notification_payload={"a": 1},
        )
        d2 = OversightDecision(
            obligation_id="task-2",
            obligation_revision=1,
            owner_tag="tag-2",
            workspace=self.test_dir,
            session_id="ses-2",
            generation=1,
            intervention_type=INTERVENTION_NUDGE_IDLE,
            route=ROUTE_NATIVE_MAILBOX,
            reason="r2",
            evidence_summary="e2",
            notification_payload={"a": 2},
        )
        d3 = OversightDecision(
            obligation_id="task-3",
            obligation_revision=1,
            owner_tag="tag-3",
            workspace=self.test_dir,
            session_id="ses-3",
            generation=1,
            intervention_type=INTERVENTION_NUDGE_IDLE,
            route=ROUTE_NATIVE_MAILBOX,
            reason="r3",
            evidence_summary="e3",
            notification_payload={"a": 3},
        )

        r1 = small_ledger.record_decision(d1)
        r2 = small_ledger.record_decision(d2)

        # 3rd active intervention must be honestly rejected!
        with self.assertRaises(CapacityExceededError):
            small_ledger.record_decision(d3)

        # Existing active records must NOT be pruned or deleted
        self.assertIsNotNone(small_ledger.get_intervention(r1.id))
        self.assertIsNotNone(small_ledger.get_intervention(r2.id))

        # Complete r1 to free active capacity
        with small_ledger._conn:
            small_ledger._conn.execute(
                "UPDATE interventions SET state = 'completed' WHERE id = ?",
                (r1.id,),
            )

        # Now d3 can be accepted
        r3 = small_ledger.record_decision(d3)
        self.assertIsNotNone(r3)
        small_ledger.close()

    # 12. Timeout escalation marks escalation-needed without automatic retry/respawn
    def test_timeout_escalation_no_automatic_retry(self):
        dec = OversightDecision(
            obligation_id="task-timeout",
            obligation_revision=1,
            owner_tag="worker-to",
            workspace=self.test_dir,
            session_id="ses-to",
            generation=1,
            intervention_type=INTERVENTION_NUDGE_IDLE,
            route=ROUTE_NATIVE_MAILBOX,
            reason="timeout test",
            evidence_summary="ev",
            notification_payload={"test": 1},
            timeout_seconds=60.0,
        )
        rec = self.ledger.record_decision(dec)

        # Advance clock by 100 seconds past timeout
        self.now = self.now + datetime.timedelta(seconds=100)

        escalated = self.ledger.check_timeouts()
        self.assertEqual(escalated, [rec.id])

        rec_after = self.ledger.get_intervention(rec.id)
        self.assertEqual(rec_after.state, STATE_ESCALATION_NEEDED)

        # Check outbox: no new messages queued
        cur = self.ledger._conn.cursor()
        cur.execute("SELECT COUNT(*) FROM outbox")
        self.assertEqual(cur.fetchone()[0], 1)

    # 13. Sanitization of secrets and raw screens
    def test_sanitization(self):
        dirty = (
            "token: secret_12345678 and Bearer abc.def password: mypass "
            "quota: 9999 raw user prompt: do something evil \x1b[31mRed Alert\x1b[0m"
        )
        cleaned = oversight.sanitize_text(dirty)
        self.assertNotIn("secret_12345678", cleaned)
        self.assertNotIn("abc.def", cleaned)
        self.assertNotIn("mypass", cleaned)
        self.assertNotIn("\x1b[31m", cleaned)
        self.assertNotIn("do something evil", cleaned)
        self.assertIn("[REDACTED]", cleaned)


    # 14. Repro: Same owner different obligations must yield exactly ONE request & outbox
    def test_repro_same_owner_different_obligations_yields_one_request(self):
        d1 = OversightDecision(
            obligation_id="task-prob-1",
            obligation_revision=1,
            owner_tag="worker-probe",
            workspace=self.test_dir,
            session_id="ses-probe-1",
            generation=1,
            intervention_type=INTERVENTION_NUDGE_IDLE,
            route=ROUTE_NATIVE_MAILBOX,
            reason="idle probe 1",
            evidence_summary="ev1",
            notification_payload={"test": 1},
        )
        d2 = OversightDecision(
            obligation_id="task-prob-2",
            obligation_revision=1,
            owner_tag="worker-probe",
            workspace=self.test_dir,
            session_id="ses-probe-1",
            generation=1,
            intervention_type=INTERVENTION_CHECKPOINT_MISSED,
            route=ROUTE_NATIVE_MAILBOX,
            reason="checkpoint missed probe 2",
            evidence_summary="ev2",
            notification_payload={"test": 2},
        )
        rec1 = self.ledger.record_decision(d1)
        rec2 = self.ledger.record_decision(d2)

        cur = self.ledger._conn.cursor()
        cur.execute("SELECT COUNT(*) FROM interventions WHERE state NOT IN ('completed', 'fenced', 'cancelled')")
        unresolved_count = cur.fetchone()[0]
        self.assertEqual(unresolved_count, 1, "Must have exactly ONE unresolved intervention per owner binding")

        cur.execute("SELECT COUNT(*) FROM outbox")
        outbox_count = cur.fetchone()[0]
        self.assertEqual(outbox_count, 1, "Must have exactly ONE outbox message across different obligations for same owner")
        self.assertEqual(rec1.id, rec2.id, "Second decision on same owner binding must return the existing unresolved reservation")

    # 15. Repro: Timeout prune must NOT delete unresolved escalation_needed or permit duplicate replay
    def test_repro_timeout_prune_must_not_delete_unresolved_or_permit_duplicate_replay(self):
        d1 = OversightDecision(
            obligation_id="task-to-prune",
            obligation_revision=1,
            owner_tag="worker-toprune",
            workspace=self.test_dir,
            session_id="ses-toprune",
            generation=1,
            intervention_type=INTERVENTION_NUDGE_IDLE,
            route=ROUTE_NATIVE_MAILBOX,
            reason="timeout prune test",
            evidence_summary="ev",
            notification_payload={"test": 1},
            timeout_seconds=30.0,
        )
        rec1 = self.ledger.record_decision(d1)
        self.now = self.now + datetime.timedelta(seconds=60)
        escalated = self.ledger.check_timeouts()
        self.assertEqual(escalated, [rec1.id])

        rec_esc = self.ledger.get_intervention(rec1.id)
        self.assertEqual(rec_esc.state, STATE_ESCALATION_NEEDED)

        # Prune terminal retention with 0 seconds
        pruned = self.ledger.prune_terminal_retention(retention_seconds=0)
        self.assertEqual(pruned, 0, "Unresolved timeout (escalation_needed) must NEVER be pruned")

        # Row must still exist
        self.assertIsNotNone(self.ledger.get_intervention(rec1.id))

        # Replay record_decision with same original decision: must NOT create new duplicate
        rec_replay = self.ledger.record_decision(d1)
        self.assertEqual(rec_replay.id, rec1.id)

        cur = self.ledger._conn.cursor()
        cur.execute("SELECT COUNT(*) FROM interventions")
        self.assertEqual(cur.fetchone()[0], 1)
        cur.execute("SELECT COUNT(*) FROM outbox")
        self.assertEqual(cur.fetchone()[0], 1)

    # 16. Repro: Concurrent separate connections with different obligations for same owner yields one outbox
    def test_repro_concurrent_separate_connections_same_owner_yields_one_outbox(self):
        import concurrent.futures

        def record_in_thread(task_id: str, intv_type: str):
            thread_ledger = InterventionLedger(self.db_path, clock=self.clock, max_active=100, max_outbox=100)
            try:
                dec = OversightDecision(
                    obligation_id=task_id,
                    obligation_revision=1,
                    owner_tag="concurrent-worker",
                    workspace=self.test_dir,
                    session_id="ses-concurrent",
                    generation=1,
                    intervention_type=intv_type,
                    route=ROUTE_NATIVE_MAILBOX,
                    reason=f"concurrent {task_id}",
                    evidence_summary="ev",
                    notification_payload={"task": task_id},
                )
                return thread_ledger.record_decision(dec)
            finally:
                thread_ledger.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            f1 = executor.submit(record_in_thread, "task-conc-1", INTERVENTION_NUDGE_IDLE)
            f2 = executor.submit(record_in_thread, "task-conc-2", INTERVENTION_CHECKPOINT_MISSED)
            r1 = f1.result()
            r2 = f2.result()

        cur = self.ledger._conn.cursor()
        cur.execute("SELECT COUNT(*) FROM outbox")
        outbox_count = cur.fetchone()[0]
        self.assertEqual(outbox_count, 1, "Concurrent calls with different obligations for same owner must yield exactly ONE outbox message")
        cur.execute("SELECT COUNT(*) FROM interventions WHERE state NOT IN ('completed', 'fenced', 'cancelled')")
        self.assertEqual(cur.fetchone()[0], 1, "Must have exactly ONE unresolved intervention")
        self.assertEqual(r1.id, r2.id)

    # 17. Completion confirmed releases owner slot for new obligation revision, while retaining old decision idempotency
    def test_completion_releases_owner_slot_for_new_revision_while_retaining_old_idempotency(self):
        d_rev1 = OversightDecision(
            obligation_id="task-lifecycle",
            obligation_revision=1,
            owner_tag="worker-lc",
            workspace=self.test_dir,
            session_id="ses-lc",
            generation=1,
            intervention_type=INTERVENTION_NUDGE_IDLE,
            route=ROUTE_NATIVE_MAILBOX,
            reason="idle rev1",
            evidence_summary="ev1",
            notification_payload={"test": 1},
        )
        rec1 = self.ledger.record_decision(d_rev1)
        msg_id1 = rec1.outbox_message_id

        # While unresolved, rev2 is rejected/coalesced into existing reservation
        d_rev2 = OversightDecision(
            obligation_id="task-lifecycle",
            obligation_revision=2,
            owner_tag="worker-lc",
            workspace=self.test_dir,
            session_id="ses-lc",
            generation=1,
            intervention_type=INTERVENTION_CHECKPOINT_MISSED,
            route=ROUTE_NATIVE_MAILBOX,
            reason="checkpoint missed rev2",
            evidence_summary="ev2",
            notification_payload={"test": 2},
        )
        rec_while_unresolved = self.ledger.record_decision(d_rev2)
        self.assertEqual(rec_while_unresolved.id, rec1.id)
        cur = self.ledger._conn.cursor()
        cur.execute("SELECT COUNT(*) FROM outbox")
        self.assertEqual(cur.fetchone()[0], 1)

        # Complete rec1 through full verified lifecycle
        transport = MockTransport()
        self.ledger.dispatch_outbox_message(msg_id1, transport, guard=MockGuard())

        read_ack = MailboxReadAck(
            message_id=msg_id1,
            read_at=self.now.isoformat(),
            reader_session_id="ses-lc",
            reader_generation=1,
        )
        self.ledger.record_mailbox_read_ack(msg_id1, read_ack)

        custody = CustodyAcceptance(
            obligation_id="task-lifecycle",
            owner_tag="worker-lc",
            session_id="ses-lc",
            generation=1,
            accepted_at=self.now.isoformat(),
        )
        self.ledger.record_custody_acceptance(rec1.id, custody)

        action = ActionReceipt(
            receipt_id="act-lc-01",
            session_id="ses-lc",
            generation=1,
            action_type="useful_progress",
            timestamp=self.now.isoformat(),
            details={"step": "first_action"},
        )
        self.ledger.record_first_action(rec1.id, action)

        outcome = CompletionOutcome(
            obligation_id="task-lifecycle",
            owner_tag="worker-lc",
            session_id="ses-lc",
            generation=1,
            completed_at=self.now.isoformat(),
            result={"status": "accepted"},
        )
        rec_completed = self.ledger.record_completion_outcome(rec1.id, outcome)
        self.assertEqual(rec_completed.state, STATE_COMPLETED)

        # Now owner slot is released: recording d_rev2 creates a NEW intervention and outbox!
        rec2 = self.ledger.record_decision(d_rev2)
        self.assertNotEqual(rec2.id, rec1.id)
        self.assertEqual(rec2.obligation_revision, 2)
        cur.execute("SELECT COUNT(*) FROM outbox")
        self.assertEqual(cur.fetchone()[0], 2)

        # Replaying old d_rev1 decision retains idempotency: returns original completed record!
        rec1_replayed = self.ledger.record_decision(d_rev1)
        self.assertEqual(rec1_replayed.id, rec1.id)
        self.assertEqual(rec1_replayed.state, STATE_COMPLETED)
        # Outbox count unchanged
        cur.execute("SELECT COUNT(*) FROM outbox")
        self.assertEqual(cur.fetchone()[0], 2)

    # 18. Restart, revision change, type change, and uncertain delivery retain reservation
    def test_restart_revision_typechange_uncertaindelivery_retain_reservation(self):
        d1 = OversightDecision(
            obligation_id="task-r1",
            obligation_revision=1,
            owner_tag="worker-retain",
            workspace=self.test_dir,
            session_id="ses-retain",
            generation=1,
            intervention_type=INTERVENTION_NUDGE_IDLE,
            route=ROUTE_NATIVE_MAILBOX,
            reason="idle r1",
            evidence_summary="ev",
            notification_payload={"test": 1},
        )
        rec1 = self.ledger.record_decision(d1)

        # 1. Cold restart retains reservation
        self.ledger.close()
        ledger2 = InterventionLedger(self.db_path, clock=self.clock, max_active=100, max_outbox=100)
        d_type_change = OversightDecision(
            obligation_id="task-r1",
            obligation_revision=1,
            owner_tag="worker-retain",
            workspace=self.test_dir,
            session_id="ses-retain",
            generation=1,
            intervention_type=INTERVENTION_CHECKPOINT_MISSED,
            route=ROUTE_NATIVE_MAILBOX,
            reason="checkpoint missed type change",
            evidence_summary="ev",
            notification_payload={"test": 2},
        )
        rec_type = ledger2.record_decision(d_type_change)
        self.assertEqual(rec_type.id, rec1.id, "Type change must retain existing reservation")

        # 2. Revision change retains reservation
        d_rev_change = OversightDecision(
            obligation_id="task-r1",
            obligation_revision=2,
            owner_tag="worker-retain",
            workspace=self.test_dir,
            session_id="ses-retain",
            generation=1,
            intervention_type=INTERVENTION_NUDGE_IDLE,
            route=ROUTE_NATIVE_MAILBOX,
            reason="idle rev 2",
            evidence_summary="ev",
            notification_payload={"test": 3},
        )
        rec_rev = ledger2.record_decision(d_rev_change)
        self.assertEqual(rec_rev.id, rec1.id, "Revision change must retain existing reservation")

        # 3. Uncertain delivery retains reservation
        transport_unc = MockTransport()
        transport_unc.fail_uncertain = True
        ledger2.dispatch_outbox_message(rec1.outbox_message_id, transport_unc, guard=MockGuard())
        rec_unc = ledger2.get_intervention(rec1.id)
        self.assertEqual(rec_unc.state, STATE_DISPATCHED_UNCERTAIN)

        # Different obligation for same owner while delivery is uncertain
        d_diff_task = OversightDecision(
            obligation_id="task-r2",
            obligation_revision=1,
            owner_tag="worker-retain",
            workspace=self.test_dir,
            session_id="ses-retain",
            generation=1,
            intervention_type=INTERVENTION_NUDGE_IDLE,
            route=ROUTE_NATIVE_MAILBOX,
            reason="idle task 2",
            evidence_summary="ev",
            notification_payload={"test": 4},
        )
        rec_diff = ledger2.record_decision(d_diff_task)
        self.assertEqual(rec_diff.id, rec1.id, "Uncertain delivery must retain owner reservation")

        cur = ledger2._conn.cursor()
        cur.execute("SELECT COUNT(*) FROM outbox")
        self.assertEqual(cur.fetchone()[0], 1)
        ledger2.close()
        self.ledger = InterventionLedger(self.db_path, clock=self.clock, max_active=100, max_outbox=100)

    # 19. Future time events rejected across all lifecycle endpoints
    def test_future_time_events_rejected(self):
        d1 = OversightDecision(
            obligation_id="task-future",
            obligation_revision=1,
            owner_tag="worker-future",
            workspace=self.test_dir,
            session_id="ses-future",
            generation=1,
            intervention_type=INTERVENTION_NUDGE_IDLE,
            route=ROUTE_NATIVE_MAILBOX,
            reason="future test",
            evidence_summary="ev",
            notification_payload={"test": 1},
        )
        rec = self.ledger.record_decision(d1)
        msg_id = rec.outbox_message_id
        self.ledger.dispatch_outbox_message(msg_id, MockTransport(), guard=MockGuard())

        future_ts = (self.now + datetime.timedelta(hours=1)).isoformat()

        # 1. Future read ACK
        with self.assertRaises(SecurityBindingError):
            self.ledger.record_mailbox_read_ack(
                msg_id,
                MailboxReadAck(
                    message_id=msg_id,
                    read_at=future_ts,
                    reader_session_id="ses-future",
                    reader_generation=1,
                ),
            )

        # Record valid read ACK
        self.ledger.record_mailbox_read_ack(
            msg_id,
            MailboxReadAck(
                message_id=msg_id,
                read_at=self.now.isoformat(),
                reader_session_id="ses-future",
                reader_generation=1,
            ),
        )

        # 2. Future custody acceptance
        with self.assertRaises(SecurityBindingError):
            self.ledger.record_custody_acceptance(
                rec.id,
                CustodyAcceptance(
                    obligation_id="task-future",
                    owner_tag="worker-future",
                    session_id="ses-future",
                    generation=1,
                    accepted_at=future_ts,
                ),
            )

        # Record valid custody
        self.ledger.record_custody_acceptance(
            rec.id,
            CustodyAcceptance(
                obligation_id="task-future",
                owner_tag="worker-future",
                session_id="ses-future",
                generation=1,
                accepted_at=self.now.isoformat(),
            ),
        )

        # 3. Future first action
        with self.assertRaises(SecurityBindingError):
            self.ledger.record_first_action(
                rec.id,
                ActionReceipt(
                    receipt_id="act-fut",
                    session_id="ses-future",
                    generation=1,
                    action_type="tool_execution",
                    timestamp=future_ts,
                ),
            )

        # Record valid first action
        self.ledger.record_first_action(
            rec.id,
            ActionReceipt(
                receipt_id="act-valid",
                session_id="ses-future",
                generation=1,
                action_type="tool_execution",
                timestamp=self.now.isoformat(),
            ),
        )

        # 4. Future completion outcome
        with self.assertRaises(SecurityBindingError):
            self.ledger.record_completion_outcome(
                rec.id,
                CompletionOutcome(
                    obligation_id="task-future",
                    owner_tag="worker-future",
                    session_id="ses-future",
                    generation=1,
                    completed_at=future_ts,
                ),
            )

    # 20. Untrusted task authority, observation session/generation mismatch, and screen output bytes alone rejected
    def test_untrusted_task_authority_and_observation_binding_rejected(self):
        binding = NativeSessionBinding(
            session_id="ses-bound",
            tag="worker-auth",
            generation=1,
            workspace=self.test_dir,
            worker_alive=True,
            reported_state="idle",
        )

        # Case A: Untrusted obligation status
        bad_ob = TaskObligation(
            id="task-bad",
            revision=1,
            owner_tag="worker-auth",
            workspace=self.test_dir,
            status="unauthorized_status",
        )
        obs_good = ObservationEvidence(
            sample_id="obs-good",
            observed_at=self.now.isoformat(),
            assessment="idle",
            empty_composer=True,
            session_id="ses-bound",
            generation=1,
        )
        res_a = self.policy.classify(bad_ob, binding, obs_good, self.now)
        self.assertEqual(res_a.state, CLASSIFICATION_UNKNOWN_OR_STALE)
        self.assertIn("Untrusted obligation status", res_a.reason)

        # Case B: Observation session_id mismatch
        good_ob = TaskObligation(
            id="task-good",
            revision=1,
            owner_tag="worker-auth",
            workspace=self.test_dir,
            status="ready",
        )
        obs_bad_session = ObservationEvidence(
            sample_id="obs-bad-ses",
            observed_at=self.now.isoformat(),
            assessment="idle",
            empty_composer=True,
            session_id="ses-attacker",
            generation=1,
        )
        res_b = self.policy.classify(good_ob, binding, obs_bad_session, self.now)
        self.assertEqual(res_b.state, CLASSIFICATION_UNKNOWN_OR_STALE)
        self.assertIn("session mismatch", res_b.reason)

        # Case C: Observation generation mismatch
        obs_bad_gen = ObservationEvidence(
            sample_id="obs-bad-gen",
            observed_at=self.now.isoformat(),
            assessment="idle",
            empty_composer=True,
            session_id="ses-bound",
            generation=999,
        )
        res_c = self.policy.classify(good_ob, binding, obs_bad_gen, self.now)
        self.assertEqual(res_c.state, CLASSIFICATION_UNKNOWN_OR_STALE)
        self.assertIn("generation mismatch", res_c.reason)

        # Case D: Screen output bytes alone without active tool calls or action receipts
        obs_bytes_only = ObservationEvidence(
            sample_id="obs-bytes",
            observed_at=self.now.isoformat(),
            assessment="progress_observed",
            reason_codes=("STREAMING_OUTPUT",),
            empty_composer=False,
            active_tool_calls=(),  # No active tool calls
            recent_output_bytes=4096,  # PTY bytes alone
            session_id="ses-bound",
            generation=1,
        )
        res_d = self.policy.classify(good_ob, binding, obs_bytes_only, self.now, recent_actions=None)
        self.assertEqual(res_d.state, CLASSIFICATION_UNKNOWN_OR_STALE)
        self.assertIn("Screen output bytes alone", res_d.reason)

    # 21. Bounded tombstones: honest rejection when retained history cap reached rather than forgetting old token
    def test_tombstones_bounded_capacity_honest_reject(self):
        small_ledger = InterventionLedger(
            db_path=":memory:",
            clock=self.clock,
            max_active=10,
            max_outbox=10,
            max_retained=2,  # Retained history limit = 2
        )

        d1 = OversightDecision(
            obligation_id="task-tb-1",
            obligation_revision=1,
            owner_tag="worker-tb1",
            workspace=self.test_dir,
            session_id="ses-tb1",
            generation=1,
            intervention_type=INTERVENTION_NUDGE_IDLE,
            route=ROUTE_NATIVE_MAILBOX,
            reason="tb1",
            evidence_summary="ev",
            notification_payload={"test": 1},
        )
        r1 = small_ledger.record_decision(d1)
        # Complete r1 and prune to tombstone
        small_ledger.dispatch_outbox_message(r1.outbox_message_id, MockTransport(), guard=MockGuard())
        small_ledger.record_mailbox_read_ack(
            r1.outbox_message_id,
            MailboxReadAck(
                message_id=r1.outbox_message_id,
                read_at=self.now.isoformat(),
                reader_session_id="ses-tb1",
                reader_generation=1,
            ),
        )
        small_ledger.record_custody_acceptance(
            r1.id,
            CustodyAcceptance(
                obligation_id="task-tb-1",
                owner_tag="worker-tb1",
                session_id="ses-tb1",
                generation=1,
                accepted_at=self.now.isoformat(),
            ),
        )
        small_ledger.record_first_action(
            r1.id,
            ActionReceipt(
                receipt_id="act-tb1",
                session_id="ses-tb1",
                generation=1,
                action_type="tool_execution",
                timestamp=self.now.isoformat(),
            ),
        )
        small_ledger.record_completion_outcome(
            r1.id,
            CompletionOutcome(
                obligation_id="task-tb-1",
                owner_tag="worker-tb1",
                session_id="ses-tb1",
                generation=1,
                completed_at=self.now.isoformat(),
            ),
        )
        small_ledger.prune_terminal_retention(retention_seconds=0)

        # Record second decision
        d2 = OversightDecision(
            obligation_id="task-tb-2",
            obligation_revision=1,
            owner_tag="worker-tb2",
            workspace=self.test_dir,
            session_id="ses-tb2",
            generation=1,
            intervention_type=INTERVENTION_NUDGE_IDLE,
            route=ROUTE_NATIVE_MAILBOX,
            reason="tb2",
            evidence_summary="ev",
            notification_payload={"test": 2},
        )
        r2 = small_ledger.record_decision(d2)

        # Capacity now reached (1 tombstone + 1 active intervention = 2 = max_retained)
        d3 = OversightDecision(
            obligation_id="task-tb-3",
            obligation_revision=1,
            owner_tag="worker-tb3",
            workspace=self.test_dir,
            session_id="ses-tb3",
            generation=1,
            intervention_type=INTERVENTION_NUDGE_IDLE,
            route=ROUTE_NATIVE_MAILBOX,
            reason="tb3",
            evidence_summary="ev",
            notification_payload={"test": 3},
        )
        # Attempting d3 must honestly raise CapacityExceededError!
        with self.assertRaises(CapacityExceededError):
            small_ledger.record_decision(d3)

        # Replaying d1 returns completed tombstone and does NOT create a new duplicate!
        r1_replay = small_ledger.record_decision(d1)
        self.assertEqual(r1_replay.id, r1.id)
        self.assertEqual(r1_replay.state, STATE_COMPLETED)
        small_ledger.close()

    # 22. Policy ActionReceipt validation: future timestamps, wrong task/revision, and liveness action types rejected
    def test_policy_action_receipt_future_and_mismatched_obligation_validation(self):
        due_ts = (self.now - datetime.timedelta(minutes=10)).isoformat()
        future_1day = (self.now + datetime.timedelta(days=1)).isoformat()

        obligation = TaskObligation(
            id="task-val",
            revision=2,
            owner_tag="worker-val",
            workspace=self.test_dir,
            status="in_progress",
            checkpoint_due=due_ts,
        )
        binding = NativeSessionBinding(
            session_id="ses-val",
            tag="worker-val",
            generation=1,
            workspace=self.test_dir,
            worker_alive=True,
            phase="running",
            reported_state="working",
        )
        obs_spinner = ObservationEvidence(
            sample_id="obs-spin",
            observed_at=self.now.isoformat(),
            assessment="busy_unconfirmed",
            reason_codes=("SPINNER_ANIMATION",),
            empty_composer=False,
            session_id="ses-val",
            generation=1,
        )

        # Case 1: Negative future 1 day checkpoint progress receipt does NOT satisfy checkpoint
        act_future = ActionReceipt(
            receipt_id="act-fut-1d",
            session_id="ses-val",
            generation=1,
            action_type="checkpoint_posted",
            timestamp=future_1day,
            obligation_id="task-val",
            obligation_revision=2,
        )
        res_fut = self.policy.classify(
            obligation, binding, obs_spinner, self.now, recent_actions=[act_future]
        )
        self.assertEqual(
            res_fut.state,
            CLASSIFICATION_MISSED_CHECKPOINT,
            "Future 1 day action receipt must NEVER satisfy checkpoint",
        )

        # Case 2: Negative future 1 day useful action receipt does NOT count as current useful work
        obs_idle = ObservationEvidence(
            sample_id="obs-idle",
            observed_at=self.now.isoformat(),
            assessment="idle",
            empty_composer=True,
            session_id="ses-val",
            generation=1,
        )
        res_fut_work = self.policy.classify(
            obligation, binding, obs_idle, self.now, recent_actions=[act_future]
        )
        self.assertNotEqual(
            res_fut_work.state,
            CLASSIFICATION_CURRENT_USEFUL_WORK,
            "Future 1 day action receipt must NEVER be accepted as current useful work",
        )

        # Case 3: Wrong task obligation ID does NOT satisfy checkpoint
        act_wrong_task = ActionReceipt(
            receipt_id="act-wrong-task",
            session_id="ses-val",
            generation=1,
            action_type="checkpoint_posted",
            timestamp=(self.now - datetime.timedelta(minutes=2)).isoformat(),
            obligation_id="task-DIFFERENT",
            obligation_revision=2,
        )
        res_wrong_task = self.policy.classify(
            obligation, binding, obs_spinner, self.now, recent_actions=[act_wrong_task]
        )
        self.assertEqual(
            res_wrong_task.state,
            CLASSIFICATION_MISSED_CHECKPOINT,
            "Action receipt for wrong task ID must not satisfy checkpoint",
        )

        # Case 4: Wrong revision does NOT satisfy checkpoint
        act_wrong_rev = ActionReceipt(
            receipt_id="act-wrong-rev",
            session_id="ses-val",
            generation=1,
            action_type="checkpoint_posted",
            timestamp=(self.now - datetime.timedelta(minutes=2)).isoformat(),
            obligation_id="task-val",
            obligation_revision=1,  # Older revision 1 vs current 2
        )
        res_wrong_rev = self.policy.classify(
            obligation, binding, obs_spinner, self.now, recent_actions=[act_wrong_rev]
        )
        self.assertEqual(
            res_wrong_rev.state,
            CLASSIFICATION_MISSED_CHECKPOINT,
            "Action receipt for old/wrong revision must not satisfy checkpoint",
        )

        # Case 5: Liveness action type (e.g. heartbeat/pty/spinner) does NOT count as useful work
        act_liveness = ActionReceipt(
            receipt_id="act-live",
            session_id="ses-val",
            generation=1,
            action_type="heartbeat",  # Non-useful liveness action
            timestamp=(self.now - datetime.timedelta(seconds=10)).isoformat(),
            obligation_id="task-val",
            obligation_revision=2,
        )
        res_liveness = self.policy.classify(
            obligation, binding, obs_spinner, self.now, recent_actions=[act_liveness]
        )
        self.assertEqual(
            res_liveness.state,
            CLASSIFICATION_MISSED_CHECKPOINT,
            "Heartbeat/liveness action type must not satisfy checkpoint or useful work",
        )

        # Case 6: Valid recent action receipt with matching obligation and revision DOES satisfy checkpoint
        act_valid = ActionReceipt(
            receipt_id="act-valid",
            session_id="ses-val",
            generation=1,
            action_type="checkpoint_posted",
            timestamp=(self.now - datetime.timedelta(minutes=2)).isoformat(),
            obligation_id="task-val",
            obligation_revision=2,
        )
        res_valid = self.policy.classify(
            obligation, binding, obs_spinner, self.now, recent_actions=[act_valid]
        )
        self.assertEqual(
            res_valid.state,
            CLASSIFICATION_CURRENT_USEFUL_WORK,
            "Valid timely action receipt matching obligation must satisfy checkpoint and confirm useful work",
        )

    # 23. Real Checkpoint Reconciliation: adoption of existing pending request, dedup across reason changes, and unknown composer fail-closed
    def test_reconciliation_existing_request_dedup_and_unknown_composer_failclosed(self):
        # Mirrored from parent publication-existing-followthrough-request.json:
        # Existing principal followthrough request already pending for public-journal-site
        existing_msg_id = "01a134a6-772d-7153-89f2-e94f1538c3aa"
        ws = self.test_dir
        owner = "public-journal-site"
        session = "8080563f-a428-4263-a73d-dc439ba5baf9"

        # 1. Existing pending request recorded in ledger
        d_existing = OversightDecision(
            obligation_id="task-pub-01",
            obligation_revision=1,
            owner_tag=owner,
            workspace=ws,
            session_id=session,
            generation=1,
            intervention_type=INTERVENTION_CHECKPOINT_MISSED,
            route=ROUTE_NATIVE_MAILBOX,
            reason="Observed actual correction response; please 11:45 owned checkpoint",
            evidence_summary="existing_principal_request",
            notification_payload={"existing_id": existing_msg_id},
        )
        rec_initial = self.ledger.record_decision(d_existing)
        self.assertEqual(rec_initial.state, STATE_OUTBOX_PENDING)

        # 2. Reason changes to completion reconciliation awaiting owner:
        # Must dedup and retain reservation, NO second message or wake created!
        d_reconciliation = OversightDecision(
            obligation_id="task-pub-01",
            obligation_revision=2,
            owner_tag=owner,
            workspace=ws,
            session_id=session,
            generation=1,
            intervention_type=INTERVENTION_RESULT_ACK_PENDING,
            route=ROUTE_NATIVE_MAILBOX,
            reason="Old writer response+exit confirmed; checker busy/nonterminal awaiting owner outcome",
            evidence_summary="reconciliation_pending",
            notification_payload={"existing_id": existing_msg_id},
        )
        rec_dedup = self.ledger.record_decision(d_reconciliation)
        self.assertEqual(rec_dedup.id, rec_initial.id, "Existing request reservation must dedup across reason changes")

        cur = self.ledger._conn.cursor()
        cur.execute("SELECT COUNT(*) FROM outbox")
        self.assertEqual(cur.fetchone()[0], 1, "Must NOT create a second message, duplicate wake, or duplicate writer")

        # 3. Observation with contradictory PTY / blank capture => UNKNOWN composer fail-closed
        binding = NativeSessionBinding(
            session_id=session,
            tag=owner,
            generation=1,
            workspace=ws,
            worker_alive=True,
            reported_state="idle",  # reported idle
        )
        # Contradicted by non-empty / uncertain composer
        obs_uncertain = ObservationEvidence(
            sample_id="obs-checker-busy",
            observed_at=self.now.isoformat(),
            assessment="possible_stall",  # ambiguous / unconfirmed activity
            reason_codes=("SCREEN_STATIC", "CLOCK_ONLY_CHANGE"),
            empty_composer=False,  # NOT empty composer!
            session_id=session,
            generation=1,
        )
        ob_reconcile = TaskObligation(
            id="task-pub-01",
            revision=2,
            owner_tag=owner,
            workspace=ws,
            status="in_progress",
        )
        res = self.policy.classify(ob_reconcile, binding, obs_uncertain, self.now)
        self.assertEqual(res.state, CLASSIFICATION_UNKNOWN_OR_STALE)
        self.assertIn("fail closed", res.reason)
        # Policy decide returns None => NO native injection!
        self.assertIsNone(self.policy.decide(ob_reconcile, binding, obs_uncertain, self.now))

        # 4. Outcome is only confirmed when genuine owner ACK/outcome proves it through full monotonic chain
        self.ledger.dispatch_outbox_message(rec_initial.outbox_message_id, MockTransport(), guard=MockGuard())
        self.ledger.record_mailbox_read_ack(
            rec_initial.outbox_message_id,
            MailboxReadAck(
                message_id=rec_initial.outbox_message_id,
                read_at=self.now.isoformat(),
                reader_session_id=session,
                reader_generation=1,
            ),
        )
        self.ledger.record_custody_acceptance(
            rec_initial.id,
            CustodyAcceptance(
                obligation_id="task-pub-01",
                owner_tag=owner,
                session_id=session,
                generation=1,
                accepted_at=self.now.isoformat(),
            ),
        )
        self.ledger.record_first_action(
            rec_initial.id,
            ActionReceipt(
                receipt_id="act-pub-01",
                session_id=session,
                generation=1,
                action_type="tool_execution",
                timestamp=self.now.isoformat(),
            ),
        )
        outcome = CompletionOutcome(
            obligation_id="task-pub-01",
            owner_tag=owner,
            session_id=session,
            generation=1,
            completed_at=self.now.isoformat(),
            result={"review_accepted": True},
        )
        rec_done = self.ledger.record_completion_outcome(rec_initial.id, outcome)
        self.assertEqual(rec_done.state, STATE_COMPLETED)

    # 24. Probe 1 Negative: Completion rejected before ACK/Action and with wrong task/owner
    def test_negative_completion_wrong_task_owner_probe1(self):
        d = OversightDecision(
            obligation_id="task-probe1",
            obligation_revision=1,
            owner_tag="worker-probe1",
            workspace=self.test_dir,
            session_id="ses-probe1",
            generation=1,
            intervention_type=INTERVENTION_NUDGE_IDLE,
            route=ROUTE_NATIVE_MAILBOX,
            reason="probe 1 test",
            evidence_summary="probe1",
            notification_payload={"test": 1},
        )
        rec = self.ledger.record_decision(d)
        self.assertEqual(rec.state, STATE_OUTBOX_PENDING)

        # 1. Attempt completion directly from outbox_pending (must fail closed!)
        outcome_premature = CompletionOutcome(
            obligation_id="task-probe1",
            owner_tag="worker-probe1",
            session_id="ses-probe1",
            generation=1,
            completed_at=self.now.isoformat(),
            result={"status": "premature"},
        )
        with self.assertRaises(SecurityBindingError) as ctx:
            self.ledger.record_completion_outcome(rec.id, outcome_premature)
        self.assertIn("first_action_observed", str(ctx.exception))

        # Progress to first_action_observed
        self.ledger.dispatch_outbox_message(rec.outbox_message_id, MockTransport(), guard=MockGuard())
        self.ledger.record_mailbox_read_ack(
            rec.outbox_message_id,
            MailboxReadAck(
                message_id=rec.outbox_message_id,
                read_at=self.now.isoformat(),
                reader_session_id="ses-probe1",
                reader_generation=1,
            ),
        )
        self.ledger.record_custody_acceptance(
            rec.id,
            CustodyAcceptance(
                obligation_id="task-probe1",
                owner_tag="worker-probe1",
                session_id="ses-probe1",
                generation=1,
                accepted_at=self.now.isoformat(),
            ),
        )
        self.ledger.record_first_action(
            rec.id,
            ActionReceipt(
                receipt_id="act-probe1",
                session_id="ses-probe1",
                generation=1,
                action_type="tool_execution",
                timestamp=self.now.isoformat(),
            ),
        )

        # 2. Attempt completion with WRONG_TASK and WRONG_OWNER but correct session/gen
        outcome_wrong_both = CompletionOutcome(
            obligation_id="WRONG_TASK",
            owner_tag="WRONG_OWNER",
            session_id="ses-probe1",
            generation=1,
            completed_at=self.now.isoformat(),
            result={"status": "spoofed"},
        )
        with self.assertRaises(SecurityBindingError) as ctx:
            self.ledger.record_completion_outcome(rec.id, outcome_wrong_both)
        self.assertIn("obligation_id", str(ctx.exception))

        # 3. Attempt completion with matching task but WRONG_OWNER
        outcome_wrong_owner = CompletionOutcome(
            obligation_id="task-probe1",
            owner_tag="WRONG_OWNER",
            session_id="ses-probe1",
            generation=1,
            completed_at=self.now.isoformat(),
            result={"status": "spoofed"},
        )
        with self.assertRaises(SecurityBindingError) as ctx:
            self.ledger.record_completion_outcome(rec.id, outcome_wrong_owner)
        self.assertIn("owner_tag", str(ctx.exception))

        # 4. Valid completion outcome succeeds
        outcome_valid = CompletionOutcome(
            obligation_id="task-probe1",
            owner_tag="worker-probe1",
            session_id="ses-probe1",
            generation=1,
            completed_at=self.now.isoformat(),
            result={"status": "genuine_success"},
        )
        rec_done = self.ledger.record_completion_outcome(rec.id, outcome_valid)
        self.assertEqual(rec_done.state, STATE_COMPLETED)

        # 5. Idempotent identical replay returns completed record
        rec_replay = self.ledger.record_completion_outcome(rec.id, outcome_valid)
        self.assertEqual(rec_replay.id, rec.id)
        self.assertEqual(rec_replay.state, STATE_COMPLETED)

        # 6. Contradictory outcome replay is rejected
        outcome_contradictory = CompletionOutcome(
            obligation_id="task-probe1",
            owner_tag="worker-probe1",
            session_id="ses-probe1",
            generation=1,
            completed_at=self.now.isoformat(),
            result={"status": "different_result"},
        )
        with self.assertRaises(SecurityBindingError):
            self.ledger.record_completion_outcome(rec.id, outcome_contradictory)

    # 25. Probe 2 Negative: Guard mandatory dispatch (None/missing/invalid/false has ZERO transport calls)
    def test_negative_missing_guard_dispatch_probe2(self):
        d = OversightDecision(
            obligation_id="task-probe2",
            obligation_revision=1,
            owner_tag="worker-probe2",
            workspace=self.test_dir,
            session_id="ses-probe2",
            generation=1,
            intervention_type=INTERVENTION_NUDGE_IDLE,
            route=ROUTE_NATIVE_MAILBOX,
            reason="probe 2 test",
            evidence_summary="probe2",
            notification_payload={"test": 2},
        )
        rec = self.ledger.record_decision(d)
        msg_id = rec.outbox_message_id

        # Case A: guard=None -> GuardAuthorizationError, 0 transport calls
        transport_a = MockTransport()
        with self.assertRaises(GuardAuthorizationError) as ctx:
            self.ledger.dispatch_outbox_message(msg_id, transport_a, guard=None)
        self.assertIn("mandatory", str(ctx.exception).lower())
        self.assertEqual(len(transport_a.sent_messages), 0, "guard=None must have ZERO transport calls")

        # Case B: guard returning authorized=False -> GuardAuthorizationError, 0 transport calls
        transport_b = MockTransport()
        guard_reject = MockGuard(authorized=False, reject_reason="Dispatch policy veto")
        with self.assertRaises(GuardAuthorizationError):
            self.ledger.dispatch_outbox_message(msg_id, transport_b, guard=guard_reject)
        self.assertEqual(len(transport_b.sent_messages), 0, "Rejected guard must have ZERO transport calls")

        # Case C: invalid guard (no authorize_dispatch method) -> GuardAuthorizationError, 0 transport calls
        transport_c = MockTransport()
        with self.assertRaises(GuardAuthorizationError):
            self.ledger.dispatch_outbox_message(msg_id, transport_c, guard="not_a_guard")
        self.assertEqual(len(transport_c.sent_messages), 0, "Invalid guard must have ZERO transport calls")

        # Verify outbox message is still pending (not marked sent)
        outbox_msg = self.ledger.get_outbox_message(msg_id)
        self.assertEqual(outbox_msg.status, OUTBOX_PENDING)

    # 26. Probe 3 Negative: Notification payload strict schema/sanitization & honest oversize reject
    def test_negative_payload_sanitization_and_oversize_probe3(self):
        # Case A: Private marker / raw_screen / credential stripping
        dirty_payload = {
            "marker": "PRIVATE_SECRET_MARKER_007",
            "private_marker": "SUPER_SECRET_MARKER_999",
            "raw_screen": "cat /etc/shadow or terminal dump with token: secret_abc",
            "transcript": "agent prompt trace",
            "user_prompt": "run malicious command",
            "token": "tok_xyz987",
            "password": "superpassword",
            "credential": "cred_blob",
            "message": "Valid safe message with marker: REDACTED_MARKER",
            "nested": {
                "raw_screen": "nested bad screen",
                "safe_field": "safe nested content",
            },
        }
        d_dirty = OversightDecision(
            obligation_id="task-probe3-dirty",
            obligation_revision=1,
            owner_tag="worker-probe3",
            workspace=self.test_dir,
            session_id="ses-probe3",
            generation=1,
            intervention_type=INTERVENTION_NUDGE_IDLE,
            route=ROUTE_NATIVE_MAILBOX,
            reason="payload redaction probe",
            evidence_summary="probe3",
            notification_payload=dirty_payload,
        )
        rec = self.ledger.record_decision(d_dirty)
        outbox_msg = self.ledger.get_outbox_message(rec.outbox_message_id)

        # Check deserialized payload
        outbox_payload = json.loads(outbox_msg.payload_json)
        self.assertNotIn("marker", outbox_payload)
        self.assertNotIn("private_marker", outbox_payload)
        self.assertNotIn("raw_screen", outbox_payload)
        self.assertNotIn("transcript", outbox_payload)
        self.assertNotIn("user_prompt", outbox_payload)
        self.assertNotIn("token", outbox_payload)
        self.assertNotIn("password", outbox_payload)
        self.assertNotIn("credential", outbox_payload)

        # Check raw serialized payload in database
        cur = self.ledger._conn.cursor()
        cur.execute("SELECT payload_json FROM outbox WHERE message_id = ?", (rec.outbox_message_id,))
        raw_json = cur.fetchone()[0]
        self.assertNotIn("PRIVATE_SECRET_MARKER_007", raw_json)
        self.assertNotIn("SUPER_SECRET_MARKER_999", raw_json)
        self.assertNotIn("cat /etc/shadow", raw_json)
        self.assertNotIn("tok_xyz987", raw_json)
        self.assertNotIn("superpassword", raw_json)

        # Dispatch and check delivered transport payload
        transport = MockTransport()
        self.ledger.dispatch_outbox_message(rec.outbox_message_id, transport, guard=MockGuard())
        delivered = json.dumps(transport.sent_messages[0]["payload"])
        self.assertNotIn("PRIVATE_SECRET_MARKER_007", delivered)
        self.assertNotIn("SUPER_SECRET_MARKER_999", delivered)

        # Case B: Honest oversize rejection (payload > 64KB)
        huge_payload = {
            "message": "A" * (70 * 1024),
        }
        d_huge = OversightDecision(
            obligation_id="task-probe3-huge",
            obligation_revision=1,
            owner_tag="worker-probe3-huge",
            workspace=self.test_dir,
            session_id="ses-probe3-huge",
            generation=1,
            intervention_type=INTERVENTION_NUDGE_IDLE,
            route=ROUTE_NATIVE_MAILBOX,
            reason="oversize probe",
            evidence_summary="probe3",
            notification_payload=huge_payload,
        )
        with self.assertRaises(CapacityExceededError) as ctx:
            self.ledger.record_decision(d_huge)
        self.assertIn("exceeds", str(ctx.exception).lower())

    # 27. Probe 4 Negative: Expired/escalated pending request late dispatch rejected (ZERO transport calls)
    def test_negative_expired_escalated_request_late_dispatch_probe4(self):
        d_exp = OversightDecision(
            obligation_id="task-probe4-exp",
            obligation_revision=1,
            owner_tag="worker-probe4",
            workspace=self.test_dir,
            session_id="ses-probe4",
            generation=1,
            intervention_type=INTERVENTION_NUDGE_IDLE,
            route=ROUTE_NATIVE_MAILBOX,
            reason="expired dispatch probe",
            evidence_summary="probe4",
            notification_payload={"test": 4},
            timeout_seconds=10.0,
        )
        rec_exp = self.ledger.record_decision(d_exp)
        msg_id = rec_exp.outbox_message_id

        # Advance clock by 20 seconds (past 10s timeout)
        self.now = self.now + datetime.timedelta(seconds=20)

        # Attempt late dispatch
        transport = MockTransport()
        sent = self.ledger.dispatch_outbox_message(msg_id, transport, guard=MockGuard())
        self.assertFalse(sent, "Late dispatch of expired message must return False")
        self.assertEqual(len(transport.sent_messages), 0, "Expired message must have ZERO transport calls")

        # Outbox status cancelled/failed
        outbox_msg = self.ledger.get_outbox_message(msg_id)
        self.assertIn(outbox_msg.status, (oversight.OUTBOX_CANCELLED, OUTBOX_FAILED))

        # Intervention state marked escalation_needed
        intv = self.ledger.get_intervention(rec_exp.id)
        self.assertEqual(intv.state, STATE_ESCALATION_NEEDED)

        # Attempt to dispatch while intervention is in STATE_ESCALATION_NEEDED: 0 transport calls
        transport2 = MockTransport()
        sent2 = self.ledger.dispatch_outbox_message(msg_id, transport2, guard=MockGuard())
        self.assertFalse(sent2)
        self.assertEqual(len(transport2.sent_messages), 0, "Escalated intervention must have ZERO transport calls")

    # 28. Lifecycle stage order, timestamp inversion, and non-useful action rejection
    def test_negative_lifecycle_stage_order_and_inversion(self):
        d = OversightDecision(
            obligation_id="task-stage-ord",
            obligation_revision=1,
            owner_tag="worker-so",
            workspace=self.test_dir,
            session_id="ses-so",
            generation=1,
            intervention_type=INTERVENTION_NUDGE_IDLE,
            route=ROUTE_NATIVE_MAILBOX,
            reason="stage order test",
            evidence_summary="stage_order",
            notification_payload={"test": 1},
        )
        rec = self.ledger.record_decision(d)
        msg_id = rec.outbox_message_id

        # 1. Attempt custody acceptance before mailbox read ACK -> SecurityBindingError
        with self.assertRaises(SecurityBindingError) as ctx:
            self.ledger.record_custody_acceptance(
                rec.id,
                CustodyAcceptance(
                    obligation_id="task-stage-ord",
                    owner_tag="worker-so",
                    session_id="ses-so",
                    generation=1,
                    accepted_at=self.now.isoformat(),
                ),
            )
        self.assertIn("mailbox_read_ack", str(ctx.exception))

        # 2. Attempt first action before custody acceptance -> SecurityBindingError
        with self.assertRaises(SecurityBindingError) as ctx:
            self.ledger.record_first_action(
                rec.id,
                ActionReceipt(
                    receipt_id="act-so-01",
                    session_id="ses-so",
                    generation=1,
                    action_type="tool_execution",
                    timestamp=self.now.isoformat(),
                ),
            )
        self.assertIn("custody_accepted", str(ctx.exception))

        # Now dispatch
        self.ledger.dispatch_outbox_message(msg_id, MockTransport(), guard=MockGuard())

        t0 = self.now
        # Mailbox read ACK at t0
        self.ledger.record_mailbox_read_ack(
            msg_id,
            MailboxReadAck(
                message_id=msg_id,
                read_at=t0.isoformat(),
                reader_session_id="ses-so",
                reader_generation=1,
            ),
        )

        # 3. Timestamp inversion: custody accepted_at earlier than read_at (t0 - 10s)
        t_earlier = (t0 - datetime.timedelta(seconds=10)).isoformat()
        with self.assertRaises(SecurityBindingError) as ctx:
            self.ledger.record_custody_acceptance(
                rec.id,
                CustodyAcceptance(
                    obligation_id="task-stage-ord",
                    owner_tag="worker-so",
                    session_id="ses-so",
                    generation=1,
                    accepted_at=t_earlier,
                ),
            )
        self.assertIn("earlier than read ACK", str(ctx.exception))

        # Valid custody acceptance at t0 + 1s: advance self.now so it's not in future
        self.now = self.now + datetime.timedelta(seconds=2)
        t1 = t0 + datetime.timedelta(seconds=1)
        self.ledger.record_custody_acceptance(
            rec.id,
            CustodyAcceptance(
                obligation_id="task-stage-ord",
                owner_tag="worker-so",
                session_id="ses-so",
                generation=1,
                accepted_at=t1.isoformat(),
            ),
        )

        # 4. Reject heartbeat/spinner/ack as first useful action
        for bad_action in ("heartbeat", "spinner", "ack", "mailbox_read", "pty_activity", "liveness_ping"):
            with self.assertRaises(SecurityBindingError) as ctx:
                self.ledger.record_first_action(
                    rec.id,
                    ActionReceipt(
                        receipt_id=f"act-bad-{bad_action}",
                        session_id="ses-so",
                        generation=1,
                        action_type=bad_action,
                        timestamp=(t1 + datetime.timedelta(seconds=1)).isoformat(),
                    ),
                )
            self.assertIn("not a valid useful action", str(ctx.exception))

        # 5. Timestamp inversion: first action earlier than custody (t1 - 10s)
        with self.assertRaises(SecurityBindingError) as ctx:
            self.ledger.record_first_action(
                rec.id,
                ActionReceipt(
                    receipt_id="act-so-inv",
                    session_id="ses-so",
                    generation=1,
                    action_type="tool_execution",
                    timestamp=t_earlier,
                ),
            )
        self.assertIn("earlier than custody acceptance", str(ctx.exception))

        # Valid first action at t1 + 1s: advance self.now so it's not in future
        self.now = self.now + datetime.timedelta(seconds=2)
        t2 = t1 + datetime.timedelta(seconds=1)
        self.ledger.record_first_action(
            rec.id,
            ActionReceipt(
                receipt_id="act-so-valid",
                session_id="ses-so",
                generation=1,
                action_type="tool_execution",
                timestamp=t2.isoformat(),
            ),
        )

        # 6. Timestamp inversion: completion earlier than first action (t2 - 10s)
        with self.assertRaises(SecurityBindingError) as ctx:
            self.ledger.record_completion_outcome(
                rec.id,
                CompletionOutcome(
                    obligation_id="task-stage-ord",
                    owner_tag="worker-so",
                    session_id="ses-so",
                    generation=1,
                    completed_at=t_earlier,
                ),
            )
        self.assertIn("earlier than first action", str(ctx.exception))

        # Valid completion outcome
        self.now = self.now + datetime.timedelta(seconds=2)
        rec_done = self.ledger.record_completion_outcome(
            rec.id,
            CompletionOutcome(
                obligation_id="task-stage-ord",
                owner_tag="worker-so",
                session_id="ses-so",
                generation=1,
                completed_at=self.now.isoformat(),
            ),
        )
        self.assertEqual(rec_done.state, STATE_COMPLETED)

    # 29. Collision-free deterministic dedup with JSON tuple / SHA-256 hash (no colon aliasing)
    def test_collision_free_colon_dedup(self):
        # Case where colon concatenation would alias:
        # Tuple A: obligation_id="task:foo", workspace="/ws/bar"
        # Tuple B: obligation_id="task", workspace="foo:/ws/bar"
        # Under colon join: "task:foo:/ws/bar" == "task:foo:/ws/bar" (collision!)
        # Under JSON tuple SHA-256: ["task:foo", ..., "/ws/bar"] != ["task", ..., "foo:/ws/bar"]
        dA = OversightDecision(
            obligation_id="task:foo",
            obligation_revision=1,
            owner_tag="worker-col",
            workspace="/ws/bar",
            session_id="ses-col",
            generation=1,
            intervention_type=INTERVENTION_NUDGE_IDLE,
            route=ROUTE_NATIVE_MAILBOX,
            reason="col A",
            evidence_summary="colA",
            notification_payload={"test": "A"},
        )
        dB = OversightDecision(
            obligation_id="task",
            obligation_revision=1,
            owner_tag="worker-col",
            workspace="foo:/ws/bar",
            session_id="ses-col",
            generation=1,
            intervention_type=INTERVENTION_NUDGE_IDLE,
            route=ROUTE_NATIVE_MAILBOX,
            reason="col B",
            evidence_summary="colB",
            notification_payload={"test": "B"},
        )

        # In ledger:
        recA = self.ledger.record_decision(dA)
        recB = self.ledger.record_decision(dB)
        # Because they are different workspaces, they are different owner bindings
        self.assertNotEqual(recA.id, recB.id)
        self.assertNotEqual(recA.dedup_key, recB.dedup_key)

    # 30. Atomic post-send/outcome records: mid-update failure rolls back to consistent reserved state
    def test_atomic_post_send_rollback(self):
        import sqlite3
        d = OversightDecision(
            obligation_id="task-atomic",
            obligation_revision=1,
            owner_tag="worker-atomic",
            workspace=self.test_dir,
            session_id="ses-atomic",
            generation=1,
            intervention_type=INTERVENTION_NUDGE_IDLE,
            route=ROUTE_NATIVE_MAILBOX,
            reason="atomic test",
            evidence_summary="atomic",
            notification_payload={"test": 1},
        )
        rec = self.ledger.record_decision(d)
        msg_id = rec.outbox_message_id

        fail_next = False
        real_conn = self.ledger._conn

        class CursorProxy:
            def __init__(self, real_cursor):
                self._real = real_cursor

            def __getattr__(self, name):
                return getattr(self._real, name)

            def execute(self, sql, *args, **kwargs):
                if fail_next and "dispatched_at = ?" in sql:
                    raise sqlite3.OperationalError("Simulated DB disk/write failure during post-transport commit")
                return self._real.execute(sql, *args, **kwargs)

        class ConnProxy:
            def __init__(self, real_c):
                self._real = real_c

            def __getattr__(self, name):
                return getattr(self._real, name)

            def cursor(self):
                return CursorProxy(self._real.cursor())

        self.ledger._conn = ConnProxy(real_conn)
        try:
            fail_next = True
            transport = MockTransport()
            with self.assertRaises(Exception) as ctx:
                self.ledger.dispatch_outbox_message(msg_id, transport, guard=MockGuard())
            self.assertIn("Simulated DB disk/write failure", str(ctx.exception))
        finally:
            self.ledger._conn = real_conn

        # State must be consistent: outbox remained sending (reserved unknown), not corrupted
        outbox_msg = self.ledger.get_outbox_message(msg_id)
        self.assertEqual(outbox_msg.status, "sending")
        intv = self.ledger.get_intervention(rec.id)
        # Still in pending/sending state, not marked sent or completed prematurely
        self.assertIn(intv.state, (STATE_OUTBOX_PENDING, STATE_DISPATCHED_UNCERTAIN))


if __name__ == "__main__":
    unittest.main()


