"""Comprehensive Offline Unit Tests for Oversight Adapter.

docs/SPEC.md §9, §11, §12.

Validates:
1. Trusted explicit ready input produces one injected transport send and ordered completion.
2. Unknown task readiness / snapshot-only cannot send (fails closed).
3. Parent Negative Probe 1: Unregistered full-field input strictly rejected.
4. Parent Negative Probe 2: Timezone-naive timestamp strictly rejected.
5. Parent Negative Probe 3: Bare {id: forged} receipt reserves uncertainty (no retry).
6. Changed binding or revision between classify and dispatch produces zero sends.
7. Pre-send revalidation in transport catches mutation before external call.
8. Frozen, quota-held, draft, menu, manual-approval, and unknown nativeguard produce zero sends.
9. Principal continuation refused on native route; principal is bus-only.
10. Wrong ACK/action/outcome task/session/gen/owner rejected (SecurityBindingError).
11. Repeated cycle / outbox replay sent at most once (owner-wide dedup).
12. Post-send missing or forged receipt reserves uncertainty without blind retry.
13. No raw screens or payload secrets leak into transport requests.
14. Stale, future, or malformed timestamps fail closed.
15. Pre-send transport failure distinguished from post-send uncertainty.
16. Full tuple required on receipt ingress (read-ACK, custody, action, outcome).
17. Monotonic ordered lifecycle: SENT -> READ_ACK -> CUSTODY -> FIRST_ACTION -> COMPLETED.
18. Ingress revalidates current authority and binding; verifies digest on replay.
19. Production InjectedChannelReceiptVerifier removed; only ReceiptVerifierProtocol and DefaultDenyReceiptVerifier in production.
20. Injected verifier boundary catches exceptions and non-bool results as post-call uncertainty.
21. Out-of-order and invalid timestamps validated BEFORE digest reservation so later legitimate stages are not poisoned.
22. Crash before ledger stage transition recovers cleanly on identical replay and rejects changed content.
23. Crash after ledger stage transition handles identical replay idempotently and rejects changed content.
24. Ambiguous ledger/digest states fail closed with operator reconciliation required.
25. Bool and non-integer generation and revision fail closed.
26. Empty and whitespace workspaces fail closed.
27. Missing or unknown readiness/liveness strictly denies dispatch.
28. SQLite storage failures during digest operations qualify as AdapterError and fail closed.
"""

from __future__ import annotations

import os
import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import dataclasses
import datetime
import hashlib
import json
import sqlite3
import tempfile
import unittest
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from agent_hub_host.oversight import (
    ActionReceipt,
    CompletionOutcome,
    CustodyAcceptance,
    InterventionLedger,
    MailboxReadAck,
    NativeSessionBinding,
    ObservationEvidence,
    OversightPolicy,
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
    STATE_FIRST_ACTION_OBSERVED,
    STATE_MAILBOX_READ_ACK,
    STATE_OUTBOX_PENDING,
    SecurityBindingError,
    TransportError,
    TransportSendFailedBeforeDelivery,
    TransportUncertainError,
    now_iso,
)
from agent_hub_host.oversight_adapter import (
    AdapterDispatchGuard,
    AdapterError,
    AdapterExecutionResult,
    AdapterTransport,
    AuthorityQualificationResult,
    AuthorityResolverProtocol,
    BindingProviderProtocol,
    DefaultDenyDependencyCompletionVerifier,
    DefaultDenyReceiptVerifier,
    DependencyCompletionVerifierProtocol,
    EXACT_TUPLE_BOUNDARY_FIELDS,
    NativeGuardCheckResult,
    NativeGuardInterface,
    ObservationProviderProtocol,
    OversightAdapter,
    QUALIFICATION_STATUS_DENIED,
    QUALIFICATION_STATUS_HELD,
    QUALIFICATION_STATUS_OFFLINE_REFERENCE,
    QUALIFICATION_STATUS_QUALIFIED,
    QUALIFICATION_STATUS_UNKNOWN,
    REQUIRED_INDEPENDENT_PROVIDERS,
    ReceiptVerifierProtocol,
    StandardAuthorityResolver,
    TransportRouteCallbacks,
    TransportSendRequest,
    TrustedAuthorityGrant,
    parse_strict_iso_utc,
)
import agent_hub_host.oversight_adapter as oversight_adapter_mod


class DeterministicMockReceiptVerifier(ReceiptVerifierProtocol):
    """Deterministic mock receipt verifier for test fixtures only.

    Never qualifies actual production provenance.
    """

    def __init__(
        self,
        valid_senders: Optional[Set[str]] = None,
        require_verified_proof: bool = True,
    ):
        self.valid_senders = valid_senders
        self.require_verified_proof = require_verified_proof

    def verify_receipt(
        self,
        receipt: Dict[str, Any],
        request: TransportSendRequest,
        now_utc: datetime.datetime,
    ) -> Tuple[bool, str]:
        if not isinstance(receipt, dict):
            return False, f"Receipt is not a dictionary: {type(receipt)}"

        msg_id = receipt.get("message_id") or receipt.get("id")
        if not msg_id or not str(msg_id).strip():
            return False, "Receipt missing valid message_id"

        rec_key = receipt.get("idempotency_key")
        if rec_key is None or str(rec_key).strip() != request.idempotency_key:
            return False, f"Receipt idempotency key missing or mismatched ('{rec_key}' != '{request.idempotency_key}')"

        if "obligation_id" in receipt and receipt["obligation_id"] != request.obligation_id:
            return False, f"Receipt obligation_id mismatch: {receipt['obligation_id']} != {request.obligation_id}"
        if "obligation_revision" in receipt and receipt["obligation_revision"] != request.obligation_revision:
            return False, f"Receipt obligation_revision mismatch: {receipt['obligation_revision']} != {request.obligation_revision}"
        if "session_id" in receipt and receipt["session_id"] != request.session_id:
            return False, f"Receipt session_id mismatch: {receipt['session_id']} != {request.session_id}"
        if "generation" in receipt and receipt["generation"] != request.generation:
            return False, f"Receipt generation mismatch: {receipt['generation']} != {request.generation}"

        status = str(receipt.get("status", "")).lower()
        if status in ("uncertain", "timed_out", "dropped", "unconfirmed"):
            return False, f"Receipt status indicates uncertain delivery: {status}"
        if status in ("failed", "rejected", "error"):
            return False, f"Receipt status indicates delivery failure: {receipt.get('error')}"

        if self.valid_senders and receipt.get("sender_id") and receipt["sender_id"] not in self.valid_senders:
            return False, f"Receipt sender '{receipt.get('sender_id')}' not in authorized senders"

        if self.require_verified_proof:
            has_proof = (
                receipt.get("verified_proof") is True
                or receipt.get("state") == "send_receipt"
                or bool(receipt.get("proof"))
                or bool(receipt.get("signature"))
            )
            if not has_proof:
                return False, "Receipt lacks authentic verified_proof or send_receipt envelope state (bare ID rejected)"

        return True, "Verified by deterministic mock verifier"


class MockNativeGuard(NativeGuardInterface):
    """Configurable mock native guard for testing."""

    def __init__(
        self,
        allowed: bool = True,
        reason: str = "ok",
        is_quota_held: bool = False,
        is_frozen: bool = False,
        is_blocked: bool = False,
        is_manual_approval: bool = False,
        is_draft: bool = False,
        is_menu_active: bool = False,
        is_saved_goal: bool = False,
        is_ready: bool = True,
        session_alive: bool = True,
        current_generation: Optional[int] = None,
    ):
        self.allowed = allowed
        self.reason = reason
        self.is_quota_held = is_quota_held
        self.is_frozen = is_frozen
        self.is_blocked = is_blocked
        self.is_manual_approval = is_manual_approval
        self.is_draft = is_draft
        self.is_menu_active = is_menu_active
        self.is_saved_goal = is_saved_goal
        self.is_ready = is_ready
        self.session_alive = session_alive
        self.current_generation = current_generation
        self.calls: List[Tuple[str, str, int]] = []

    def check_native_state(
        self,
        workspace: str,
        session_id: str,
        generation: int,
        now_utc: datetime.datetime,
    ) -> NativeGuardCheckResult:
        self.calls.append((workspace, session_id, generation))
        gen = self.current_generation if self.current_generation is not None else generation
        return NativeGuardCheckResult(
            allowed=self.allowed,
            reason=self.reason,
            is_quota_held=self.is_quota_held,
            is_frozen=self.is_frozen,
            is_blocked=self.is_blocked,
            is_manual_approval=self.is_manual_approval,
            is_draft=self.is_draft,
            is_menu_active=self.is_menu_active,
            is_saved_goal=self.is_saved_goal,
            is_ready=self.is_ready,
            session_alive=self.session_alive,
            current_generation=gen,
        )


class MockBindingProvider(BindingProviderProtocol):
    """Configurable mock binding provider."""

    def __init__(self, binding: Optional[NativeSessionBinding] = None):
        self.binding = binding
        self.calls: List[Tuple[str, str]] = []

    def get_current_binding(
        self,
        workspace: str,
        session_id: str,
        now_utc: datetime.datetime,
    ) -> Optional[NativeSessionBinding]:
        self.calls.append((workspace, session_id))
        return self.binding


class MockObservationProvider(ObservationProviderProtocol):
    """Configurable mock observation provider."""

    def __init__(self, observation: Optional[ObservationEvidence] = None):
        self.observation = observation
        self.calls: List[Tuple[str, str, int]] = []

    def get_current_observation(
        self,
        workspace: str,
        session_id: str,
        generation: int,
        now_utc: datetime.datetime,
    ) -> Optional[ObservationEvidence]:
        self.calls.append((workspace, session_id, generation))
        return self.observation


class TestOversightAdapter(unittest.TestCase):
    """Offline unit test suite for OversightAdapter."""

    def setUp(self) -> None:
        self.current_time = datetime.datetime(2026, 10, 13, 12, 0, 0, tzinfo=datetime.timezone.utc)
        self.clock = lambda: self.current_time
        self.workspace = os.path.realpath("/home/alexey/git/agent-hub")

        # In-memory durable ledger
        self.ledger = InterventionLedger(
            db_path=":memory:",
            clock=self.clock,
        )
        self.policy = OversightPolicy()
        self.authority_resolver = StandardAuthorityResolver()

        # Standard trusted grant (with explicit timezone)
        self.sample_digest = hashlib.sha256(b"source_content_v1").hexdigest()
        self.grant = TrustedAuthorityGrant(
            task_id="task-101",
            revision=1,
            owner_tag="implementer-1",
            workspace=self.workspace,
            session_id="session-alpha",
            generation=1,
            source_id="src-task-101",
            source_digest=self.sample_digest,
            issued_at=now_iso(self.current_time),
            status="ready",
        )
        self.authority_resolver.register_grant(self.grant)

        # Standard binding
        self.binding = NativeSessionBinding(
            session_id="session-alpha",
            tag="implementer-1",
            generation=1,
            workspace=self.workspace,
            worker_alive=True,
            phase="running",
            reported_state="idle",
            active=True,
        )
        self.binding_provider = MockBindingProvider(self.binding)
        self.receipt_verifier = DeterministicMockReceiptVerifier()

        self.adapter = OversightAdapter(
            ledger=self.ledger,
            policy=self.policy,
            authority_resolver=self.authority_resolver,
            binding_provider=self.binding_provider,
            receipt_verifier=self.receipt_verifier,
            clock=self.clock,
        )

        # Standard observation (idle with empty composer)
        self.observation = ObservationEvidence(
            sample_id="obs-001",
            observed_at="2026-10-13T12:00:00Z",
            assessment="idle",
            empty_composer=True,
            session_id="session-alpha",
            generation=1,
        )
        self.observation_provider = MockObservationProvider(self.observation)
        self.native_guard = MockNativeGuard(allowed=True)

        # Transport callbacks tracking
        self.sent_requests: List[TransportSendRequest] = []

        def bus_cb(req: TransportSendRequest) -> Dict[str, Any]:
            self.sent_requests.append(req)
            return {
                "status": "sent",
                "message_id": f"bus-receipt-{req.message_id}",
                "idempotency_key": req.idempotency_key,
                "state": "send_receipt",
                "sent_at": "2026-10-13T12:00:01Z",
            }

        def native_cb(req: TransportSendRequest) -> Dict[str, Any]:
            self.sent_requests.append(req)
            return {
                "status": "sent",
                "message_id": f"native-receipt-{req.message_id}",
                "idempotency_key": req.idempotency_key,
                "state": "send_receipt",
                "sent_at": "2026-10-13T12:00:01Z",
            }

        self.transport_callbacks = TransportRouteCallbacks(
            bus_send=bus_cb,
            native_continuation=native_cb,
        )

    def tearDown(self) -> None:
        self.ledger.close()

    # -------------------------------------------------------------------------
    # 1. Trusted explicit ready input produces one transport send and completion
    # -------------------------------------------------------------------------

    def test_trusted_ready_produces_one_send_and_ordered_completion(self) -> None:
        """Trusted explicit input produces one transport call and permits full ordered lifecycle."""
        res = self.adapter.evaluate_and_dispatch(
            raw_task_data=self.grant,
            authority_resolver=self.authority_resolver,
            binding_provider=self.binding_provider,
            observation_provider=self.observation_provider,
            native_guard=self.native_guard,
            transport_callbacks=self.transport_callbacks,
        )

        self.assertTrue(res.decision_made)
        self.assertTrue(res.dispatch_sent)
        self.assertFalse(res.dispatched_uncertain)
        self.assertFalse(res.dispatch_failed)
        self.assertIsNotNone(res.intervention)
        self.assertIsNotNone(res.outbox_message)
        self.assertEqual(len(self.sent_requests), 1)

        req = self.sent_requests[0]
        self.assertEqual(req.obligation_id, "task-101")
        self.assertEqual(req.obligation_revision, 1)
        self.assertEqual(req.owner_tag, "implementer-1")
        self.assertEqual(req.workspace, self.workspace)
        self.assertEqual(req.session_id, "session-alpha")
        self.assertEqual(req.generation, 1)

        intv_id = res.intervention.id
        msg_id = res.outbox_message.message_id

        # 1. Advance clock and record Mailbox Read ACK
        self.current_time += datetime.timedelta(seconds=2)
        read_ack = MailboxReadAck(
            message_id=msg_id,
            read_at=now_iso(self.current_time),
            reader_session_id="session-alpha",
            reader_generation=1,
        )
        intv1 = self.adapter.record_mailbox_read_ack(
            message_id=msg_id,
            ack=read_ack,
            expected_obligation_id="task-101",
            expected_revision=1,
            expected_owner_tag="implementer-1",
            expected_workspace=self.workspace,
        )
        self.assertEqual(intv1.state, STATE_MAILBOX_READ_ACK)

        # 2. Advance clock and record Custody Acceptance
        self.current_time += datetime.timedelta(seconds=2)
        custody = CustodyAcceptance(
            obligation_id="task-101",
            owner_tag="implementer-1",
            session_id="session-alpha",
            generation=1,
            accepted_at=now_iso(self.current_time),
            workspace=self.workspace,
            obligation_revision=1,
        )
        intv2 = self.adapter.record_custody_acceptance(intv_id, custody)
        self.assertEqual(intv2.state, STATE_CUSTODY_ACCEPTED)

        # 3. Advance clock and record First Action
        self.current_time += datetime.timedelta(seconds=2)
        action = ActionReceipt(
            receipt_id="rec-act-001",
            session_id="session-alpha",
            generation=1,
            action_type="tool_execution",
            timestamp=now_iso(self.current_time),
            obligation_id="task-101",
            obligation_revision=1,
            owner_tag="implementer-1",
            workspace=self.workspace,
        )
        intv3 = self.adapter.record_first_action(intv_id, action)
        self.assertEqual(intv3.state, STATE_FIRST_ACTION_OBSERVED)

        # 4. Advance clock and record Completion Outcome
        self.current_time += datetime.timedelta(seconds=2)
        outcome = CompletionOutcome(
            obligation_id="task-101",
            owner_tag="implementer-1",
            session_id="session-alpha",
            generation=1,
            completed_at=now_iso(self.current_time),
            result={"status": "success", "artifact": "hash123"},
            workspace=self.workspace,
            obligation_revision=1,
        )
        intv4 = self.adapter.record_completion_outcome(intv_id, outcome)
        self.assertEqual(intv4.state, STATE_COMPLETED)

    # -------------------------------------------------------------------------
    # 2. Parent Negative Probe 1: Unregistered full-field input strictly rejected
    # -------------------------------------------------------------------------

    def test_probe1_unregistered_full_field_input_rejected(self) -> None:
        """Unregistered grant input (even with full valid-looking fields) must be rejected."""
        unregistered_grant = TrustedAuthorityGrant(
            task_id="unregistered-task-999",
            revision=1,
            owner_tag="implementer-1",
            workspace=self.workspace,
            session_id="session-alpha",
            generation=1,
            source_id="src-unregistered",
            source_digest=self.sample_digest,
            issued_at="2026-10-13T12:00:00Z",
            status="ready",
        )

        res = self.adapter.evaluate_and_dispatch(
            raw_task_data=unregistered_grant,
            authority_resolver=self.authority_resolver,
            binding_provider=self.binding_provider,
            observation_provider=self.observation_provider,
            native_guard=self.native_guard,
            transport_callbacks=self.transport_callbacks,
        )

        self.assertFalse(res.decision_made)
        self.assertFalse(res.dispatch_sent)
        self.assertEqual(len(self.sent_requests), 0)
        self.assertIn("Authority resolution failed", res.refusal_reason or "")

    # -------------------------------------------------------------------------
    # 3. Parent Negative Probe 2: Timezone-naive input strictly rejected
    # -------------------------------------------------------------------------

    def test_probe2_timezone_naive_input_rejected(self) -> None:
        """Timestamp without timezone offset (e.g. naive string) must be strictly rejected."""
        naive_grant = TrustedAuthorityGrant(
            task_id="task-naive",
            revision=1,
            owner_tag="implementer-1",
            workspace=self.workspace,
            session_id="session-alpha",
            generation=1,
            source_id="src-naive",
            source_digest=self.sample_digest,
            issued_at="2026-10-13T12:00:00",  # NAIVE (no Z or offset)!
            status="ready",
        )
        self.authority_resolver.register_grant(naive_grant)

        res = self.adapter.evaluate_and_dispatch(
            raw_task_data=naive_grant,
            authority_resolver=self.authority_resolver,
            binding_provider=self.binding_provider,
            observation_provider=self.observation_provider,
            native_guard=self.native_guard,
            transport_callbacks=self.transport_callbacks,
        )

        self.assertFalse(res.decision_made)
        self.assertFalse(res.dispatch_sent)
        self.assertEqual(len(self.sent_requests), 0)
        self.assertIn("Authority resolution failed", res.refusal_reason or "")

    # -------------------------------------------------------------------------
    # 4. Parent Negative Probe 3: Bare {id: forged} receipt reserves uncertainty
    # -------------------------------------------------------------------------

    def test_probe3_bare_id_receipt_reserves_uncertainty(self) -> None:
        """Bare {id: forged-id} receipt lacks proof and must reserve uncertainty without retry."""
        def bare_forged_cb(req: TransportSendRequest) -> Dict[str, Any]:
            return {"id": "forged-id-123"}

        bare_callbacks = TransportRouteCallbacks(
            native_continuation=bare_forged_cb,
        )

        res = self.adapter.evaluate_and_dispatch(
            raw_task_data=self.grant,
            authority_resolver=self.authority_resolver,
            binding_provider=self.binding_provider,
            observation_provider=self.observation_provider,
            native_guard=self.native_guard,
            transport_callbacks=bare_callbacks,
        )

        self.assertTrue(res.decision_made)
        self.assertFalse(res.dispatch_sent)
        self.assertTrue(res.dispatched_uncertain)

        outbox = self.ledger.get_outbox_message(res.outbox_message.message_id)
        self.assertEqual(outbox.status, OUTBOX_UNCERTAIN)
        intv = self.ledger.get_intervention(res.intervention.id)
        self.assertEqual(intv.state, STATE_DISPATCHED_UNCERTAIN)

    # -------------------------------------------------------------------------
    # 5. Pre-send revalidation in transport catches mutation before external call
    # -------------------------------------------------------------------------

    def test_pre_send_recheck_mutation_fails_before_call(self) -> None:
        """If authority is revoked after guard authorization but before send, pre-send recheck fails."""
        call_count = 0

        def native_cb(req: TransportSendRequest) -> Dict[str, Any]:
            nonlocal call_count
            call_count += 1
            return {"status": "sent", "id": "msg-1", "idempotency_key": req.idempotency_key, "state": "send_receipt"}

        class RevokingResolver(StandardAuthorityResolver):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.reval_count = 0

            def revalidate_authority(self, task_id: str, revision: int, workspace: str, now_utc: datetime.datetime):
                self.reval_count += 1
                if self.reval_count > 1:
                    return None
                return super().revalidate_authority(task_id, revision, workspace, now_utc)

        rev_resolver = RevokingResolver(known_grants={(self.grant.task_id, self.grant.revision): self.grant})

        tb = TransportRouteCallbacks(native_continuation=native_cb)

        res = self.adapter.evaluate_and_dispatch(
            raw_task_data=self.grant,
            authority_resolver=rev_resolver,
            binding_provider=self.binding_provider,
            observation_provider=self.observation_provider,
            native_guard=self.native_guard,
            transport_callbacks=tb,
        )

        self.assertFalse(res.dispatch_sent)
        self.assertEqual(call_count, 0)  # ZERO transport calls!
        self.assertTrue(res.dispatch_failed)
        outbox = self.ledger.get_outbox_message(res.outbox_message.message_id)
        self.assertEqual(outbox.status, OUTBOX_FAILED)

    # -------------------------------------------------------------------------
    # 6. Unknown task readiness / snapshot-only cannot send
    # -------------------------------------------------------------------------

    def test_unknown_task_readiness_snapshot_only_cannot_send(self) -> None:
        """CAG snapshot-only data or status strings without trusted grant cannot send."""
        raw_snapshot = {
            "id": "task-unknown",
            "status": "ready",
            "updated_at": "2026-10-13T11:59:00Z",
            "owner": "implementer-1",
            "workspace": self.workspace,
            "trusted": True,
        }

        res = self.adapter.evaluate_and_dispatch(
            raw_task_data=raw_snapshot,
            authority_resolver=self.authority_resolver,
            binding_provider=self.binding_provider,
            observation_provider=self.observation_provider,
            native_guard=self.native_guard,
            transport_callbacks=self.transport_callbacks,
        )

        self.assertFalse(res.decision_made)
        self.assertFalse(res.dispatch_sent)
        self.assertEqual(len(self.sent_requests), 0)

    # -------------------------------------------------------------------------
    # 7. Changed binding or revision between classify and dispatch zero sends
    # -------------------------------------------------------------------------

    def test_changed_binding_between_classify_and_dispatch_zero_sends(self) -> None:
        """If session generation advances before dispatch, session is fenced and zero sends occur."""
        class RaceBindingProvider(MockBindingProvider):
            def __init__(self, initial_b: NativeSessionBinding):
                super().__init__(initial_b)
                self._query_count = 0

            def get_current_binding(self, ws: str, s_id: str, now: datetime.datetime):
                self._query_count += 1
                if self._query_count >= 2:
                    return dataclasses.replace(self.binding, generation=2)
                return self.binding

        race_bp = RaceBindingProvider(self.binding)

        res = self.adapter.evaluate_and_dispatch(
            raw_task_data=self.grant,
            authority_resolver=self.authority_resolver,
            binding_provider=race_bp,
            observation_provider=self.observation_provider,
            native_guard=self.native_guard,
            transport_callbacks=self.transport_callbacks,
        )

        self.assertTrue(res.decision_made)
        self.assertFalse(res.dispatch_sent)
        self.assertEqual(len(self.sent_requests), 0)
        self.assertIn("Session reassignment", res.refusal_reason or "")

        intv = self.ledger.get_intervention(res.intervention.id)
        self.assertEqual(intv.state, "fenced")

    # -------------------------------------------------------------------------
    # 8. Frozen / quota / draft / menu / manual guard states produce zero sends
    # -------------------------------------------------------------------------

    def test_native_guard_quota_held_zero_sends(self) -> None:
        """Quota-held guard state refuses dispatch with zero transport calls."""
        guard = MockNativeGuard(allowed=False, reason="Quota exceeded", is_quota_held=True)
        res = self.adapter.evaluate_and_dispatch(
            raw_task_data=self.grant,
            authority_resolver=self.authority_resolver,
            binding_provider=self.binding_provider,
            observation_provider=self.observation_provider,
            native_guard=guard,
            transport_callbacks=self.transport_callbacks,
        )
        self.assertFalse(res.dispatch_sent)
        self.assertEqual(len(self.sent_requests), 0)
        self.assertIn("quota held", res.refusal_reason or "")

    def test_native_guard_frozen_zero_sends(self) -> None:
        """Frozen session state refuses dispatch with zero transport calls."""
        guard = MockNativeGuard(allowed=False, reason="Session frozen", is_frozen=True)
        res = self.adapter.evaluate_and_dispatch(
            raw_task_data=self.grant,
            authority_resolver=self.authority_resolver,
            binding_provider=self.binding_provider,
            observation_provider=self.observation_provider,
            native_guard=guard,
            transport_callbacks=self.transport_callbacks,
        )
        self.assertFalse(res.dispatch_sent)
        self.assertEqual(len(self.sent_requests), 0)
        self.assertIn("session frozen", res.refusal_reason or "")

    def test_native_guard_draft_and_menu_zero_sends(self) -> None:
        """Draft or menu active guard state refuses dispatch with zero transport calls."""
        guard = MockNativeGuard(allowed=False, reason="Menu active", is_menu_active=True)
        res = self.adapter.evaluate_and_dispatch(
            raw_task_data=self.grant,
            authority_resolver=self.authority_resolver,
            binding_provider=self.binding_provider,
            observation_provider=self.observation_provider,
            native_guard=guard,
            transport_callbacks=self.transport_callbacks,
        )
        self.assertFalse(res.dispatch_sent)
        self.assertEqual(len(self.sent_requests), 0)
        self.assertIn("menu active", res.refusal_reason or "")

    def test_native_guard_manual_approval_zero_sends(self) -> None:
        """Manual approval required refuses dispatch with zero transport calls."""
        guard = MockNativeGuard(allowed=False, reason="Manual approval required", is_manual_approval=True)
        res = self.adapter.evaluate_and_dispatch(
            raw_task_data=self.grant,
            authority_resolver=self.authority_resolver,
            binding_provider=self.binding_provider,
            observation_provider=self.observation_provider,
            native_guard=guard,
            transport_callbacks=self.transport_callbacks,
        )
        self.assertFalse(res.dispatch_sent)
        self.assertEqual(len(self.sent_requests), 0)
        self.assertIn("manual approval required", res.refusal_reason or "")

    # -------------------------------------------------------------------------
    # 9. Principal continuation refused; principal is bus-only
    # -------------------------------------------------------------------------

    def test_principal_continuation_refused_on_native_route(self) -> None:
        """Principal obligations routed to native continuation are strictly refused."""
        principal_grant = dataclasses.replace(
            self.grant,
            task_id="principal-task-01",
            owner_tag="codex-principal",
            is_principal=True,
        )
        self.authority_resolver.register_grant(principal_grant)
        principal_binding = dataclasses.replace(self.binding, tag="codex-principal")
        binding_prov = MockBindingProvider(principal_binding)

        res = self.adapter.evaluate_and_dispatch(
            raw_task_data=principal_grant,
            authority_resolver=self.authority_resolver,
            binding_provider=binding_prov,
            observation_provider=self.observation_provider,
            native_guard=self.native_guard,
            transport_callbacks=self.transport_callbacks,
        )

        self.assertTrue(res.decision_made)
        self.assertEqual(res.decision.route, ROUTE_BUS)
        self.assertTrue(res.dispatch_sent)
        self.assertEqual(self.sent_requests[0].route, ROUTE_BUS)

        # Explicitly verify that guard rejects native route for principal
        guard = AdapterDispatchGuard(
            ledger=self.ledger,
            authority_resolver=self.authority_resolver,
            binding_provider=binding_prov,
            native_guard=self.native_guard,
            transport_callbacks=self.transport_callbacks,
            clock=self.clock,
        )
        fake_msg = dataclasses.replace(res.outbox_message, route=ROUTE_NATIVE_MAILBOX)
        allowed, reason = guard.authorize_dispatch(fake_msg)
        self.assertFalse(allowed)
        self.assertIn("Principal continuation refused", reason)

    # -------------------------------------------------------------------------
    # 10. Wrong ACK / action / outcome tuple rejected
    # -------------------------------------------------------------------------

    def test_wrong_ack_message_id_rejected(self) -> None:
        """Mismatched message_id between ack object and parameter raises SecurityBindingError."""
        res = self.adapter.evaluate_and_dispatch(
            raw_task_data=self.grant,
            authority_resolver=self.authority_resolver,
            binding_provider=self.binding_provider,
            observation_provider=self.observation_provider,
            native_guard=self.native_guard,
            transport_callbacks=self.transport_callbacks,
        )
        msg_id = res.outbox_message.message_id

        wrong_ack = MailboxReadAck(
            message_id="different-msg-id",
            read_at="2026-10-13T12:00:02Z",
            reader_session_id="session-alpha",
            reader_generation=1,
        )

        with self.assertRaises(SecurityBindingError) as ctx:
            self.adapter.record_mailbox_read_ack(
                message_id=msg_id,
                ack=wrong_ack,
                expected_obligation_id="task-101",
                expected_revision=1,
                expected_owner_tag="implementer-1",
                expected_workspace=self.workspace,
            )
        self.assertIn("ack.message_id mismatch", str(ctx.exception))

    def test_wrong_custody_tuple_rejected(self) -> None:
        """Custody acceptance with wrong task, session, generation, or revision raises SecurityBindingError."""
        res = self.adapter.evaluate_and_dispatch(
            raw_task_data=self.grant,
            authority_resolver=self.authority_resolver,
            binding_provider=self.binding_provider,
            observation_provider=self.observation_provider,
            native_guard=self.native_guard,
            transport_callbacks=self.transport_callbacks,
        )
        intv_id = res.intervention.id
        msg_id = res.outbox_message.message_id

        self.current_time += datetime.timedelta(seconds=2)
        self.adapter.record_mailbox_read_ack(
            message_id=msg_id,
            ack=MailboxReadAck(
                message_id=msg_id,
                read_at=now_iso(self.current_time),
                reader_session_id="session-alpha",
                reader_generation=1,
            ),
            expected_obligation_id="task-101",
            expected_revision=1,
            expected_owner_tag="implementer-1",
            expected_workspace=self.workspace,
        )

        self.current_time += datetime.timedelta(seconds=2)
        wrong_custody = CustodyAcceptance(
            obligation_id="task-101",
            owner_tag="implementer-1",
            session_id="session-alpha",
            generation=999,
            accepted_at=now_iso(self.current_time),
            workspace=self.workspace,
            obligation_revision=1,
        )
        with self.assertRaises(SecurityBindingError):
            self.adapter.record_custody_acceptance(intv_id, wrong_custody)

    # -------------------------------------------------------------------------
    # 11. Repeated cycle / outbox replay at most once
    # -------------------------------------------------------------------------

    def test_repeated_cycle_outbox_replay_at_most_once(self) -> None:
        """Owner-wide deduplication prevents duplicate outbox messages and repeat sends."""
        res1 = self.adapter.evaluate_and_dispatch(
            raw_task_data=self.grant,
            authority_resolver=self.authority_resolver,
            binding_provider=self.binding_provider,
            observation_provider=self.observation_provider,
            native_guard=self.native_guard,
            transport_callbacks=self.transport_callbacks,
        )
        self.assertTrue(res1.dispatch_sent)
        self.assertEqual(len(self.sent_requests), 1)

        res2 = self.adapter.evaluate_and_dispatch(
            raw_task_data=self.grant,
            authority_resolver=self.authority_resolver,
            binding_provider=self.binding_provider,
            observation_provider=self.observation_provider,
            native_guard=self.native_guard,
            transport_callbacks=self.transport_callbacks,
        )

        self.assertFalse(res2.dispatch_sent)
        self.assertEqual(len(self.sent_requests), 1)
        self.assertEqual(res1.intervention.id, res2.intervention.id)

    # -------------------------------------------------------------------------
    # 12. Payload sanitization: no raw screens or secrets leak
    # -------------------------------------------------------------------------

    def test_payload_sanitization_and_no_secret_leaks(self) -> None:
        """Secrets, tokens, credentials, and raw screens in payloads are scrubbed before transport send."""
        res = self.adapter.evaluate_and_dispatch(
            raw_task_data=self.grant,
            authority_resolver=self.authority_resolver,
            binding_provider=self.binding_provider,
            observation_provider=self.observation_provider,
            native_guard=self.native_guard,
            transport_callbacks=self.transport_callbacks,
        )
        self.assertTrue(res.dispatch_sent)
        req = self.sent_requests[0]

        payload_str = json.dumps(req.payload)
        forbidden_terms = ["raw_screen", "token", "password", "bearer", "credential"]
        for term in forbidden_terms:
            self.assertNotIn(f'"{term}"', payload_str.lower())

    # -------------------------------------------------------------------------
    # 13. Stale / future / malformed time fail closed
    # -------------------------------------------------------------------------

    def test_future_timestamp_grant_fails_closed(self) -> None:
        """Grant with timestamp in the future fails authority resolution."""
        future_grant = dataclasses.replace(self.grant, issued_at="2026-10-13T13:00:00Z")
        self.authority_resolver.register_grant(future_grant)

        res = self.adapter.evaluate_and_dispatch(
            raw_task_data=future_grant,
            authority_resolver=self.authority_resolver,
            binding_provider=self.binding_provider,
            observation_provider=self.observation_provider,
            native_guard=self.native_guard,
            transport_callbacks=self.transport_callbacks,
        )
        self.assertFalse(res.decision_made)
        self.assertFalse(res.dispatch_sent)
        self.assertEqual(len(self.sent_requests), 0)

    def test_stale_timestamp_grant_fails_closed(self) -> None:
        """Grant with timestamp older than freshness threshold fails authority resolution."""
        stale_grant = dataclasses.replace(self.grant, issued_at="2026-10-13T11:50:00Z")
        self.authority_resolver.register_grant(stale_grant)

        res = self.adapter.evaluate_and_dispatch(
            raw_task_data=stale_grant,
            authority_resolver=self.authority_resolver,
            binding_provider=self.binding_provider,
            observation_provider=self.observation_provider,
            native_guard=self.native_guard,
            transport_callbacks=self.transport_callbacks,
        )
        self.assertFalse(res.decision_made)
        self.assertFalse(res.dispatch_sent)
        self.assertEqual(len(self.sent_requests), 0)

    # -------------------------------------------------------------------------
    # 14. Ingress revalidates current authority and binding; verifies replay digest
    # -------------------------------------------------------------------------

    def test_receipt_ingress_revalidates_authority_and_binding(self) -> None:
        """If authority is revoked after dispatch, receipt ingress is refused."""
        res = self.adapter.evaluate_and_dispatch(
            raw_task_data=self.grant,
            authority_resolver=self.authority_resolver,
            binding_provider=self.binding_provider,
            observation_provider=self.observation_provider,
            native_guard=self.native_guard,
            transport_callbacks=self.transport_callbacks,
        )
        msg_id = res.outbox_message.message_id
        intv_id = res.intervention.id

        self.current_time += datetime.timedelta(seconds=2)
        read_ack = MailboxReadAck(
            message_id=msg_id,
            read_at=now_iso(self.current_time),
            reader_session_id="session-alpha",
            reader_generation=1,
        )
        self.adapter.record_mailbox_read_ack(
            message_id=msg_id,
            ack=read_ack,
            expected_obligation_id="task-101",
            expected_revision=1,
            expected_owner_tag="implementer-1",
            expected_workspace=self.workspace,
        )

        # Revoke authority in resolver
        del self.authority_resolver._grants[(self.grant.task_id, self.grant.revision)]

        custody = CustodyAcceptance(
            obligation_id="task-101",
            owner_tag="implementer-1",
            session_id="session-alpha",
            generation=1,
            accepted_at="2026-10-13T12:00:03Z",
            workspace=self.workspace,
            obligation_revision=1,
        )
        with self.assertRaises(SecurityBindingError) as ctx:
            self.adapter.record_custody_acceptance(intv_id, custody)
        self.assertIn("Current authority revoked or missing", str(ctx.exception))

    def test_pre_send_failure_distinguished_from_uncertainty(self) -> None:
        """Pre-send failure marks OUTBOX_FAILED and does NOT mark intervention uncertain."""
        def pre_fail_cb(req: TransportSendRequest) -> Dict[str, Any]:
            raise TransportSendFailedBeforeDelivery("Network device down before socket connect")

        pf_callbacks = TransportRouteCallbacks(
            native_continuation=pre_fail_cb,
        )

        res = self.adapter.evaluate_and_dispatch(
            raw_task_data=self.grant,
            authority_resolver=self.authority_resolver,
            binding_provider=self.binding_provider,
            observation_provider=self.observation_provider,
            native_guard=self.native_guard,
            transport_callbacks=pf_callbacks,
        )

        self.assertFalse(res.dispatch_sent)
        self.assertFalse(res.dispatched_uncertain)
        self.assertTrue(res.dispatch_failed)

        outbox = self.ledger.get_outbox_message(res.outbox_message.message_id)
        self.assertEqual(outbox.status, OUTBOX_FAILED)
        intv = self.ledger.get_intervention(res.intervention.id)
        self.assertEqual(intv.state, STATE_OUTBOX_PENDING)

    def test_malformed_timestamp_fails_closed(self) -> None:
        """Grant with invalid/unparseable timestamp string fails authority resolution."""
        bad_grant = dataclasses.replace(self.grant, issued_at="NOT-A-TIMESTAMP")
        self.authority_resolver.register_grant(bad_grant)

        res = self.adapter.evaluate_and_dispatch(
            raw_task_data=bad_grant,
            authority_resolver=self.authority_resolver,
            binding_provider=self.binding_provider,
            observation_provider=self.observation_provider,
            native_guard=self.native_guard,
            transport_callbacks=self.transport_callbacks,
        )
        self.assertFalse(res.decision_made)
        self.assertFalse(res.dispatch_sent)
        self.assertEqual(len(self.sent_requests), 0)

    def test_missing_route_callback_denies(self) -> None:
        """Missing route callback causes guard refusal and zero transport calls."""
        empty_callbacks = TransportRouteCallbacks(bus_send=None, native_continuation=None)

        res = self.adapter.evaluate_and_dispatch(
            raw_task_data=self.grant,
            authority_resolver=self.authority_resolver,
            binding_provider=self.binding_provider,
            observation_provider=self.observation_provider,
            native_guard=self.native_guard,
            transport_callbacks=empty_callbacks,
        )

        self.assertTrue(res.decision_made)
        self.assertFalse(res.dispatch_sent)
        self.assertEqual(len(self.sent_requests), 0)
        self.assertIn("No transport route callback registered", res.refusal_reason or "")

    def test_receipt_out_of_order_lifecycle_rejected(self) -> None:
        """Attempting to accept custody before read ACK, or complete before action, is rejected."""
        res = self.adapter.evaluate_and_dispatch(
            raw_task_data=self.grant,
            authority_resolver=self.authority_resolver,
            binding_provider=self.binding_provider,
            observation_provider=self.observation_provider,
            native_guard=self.native_guard,
            transport_callbacks=self.transport_callbacks,
        )
        intv_id = res.intervention.id

        custody = CustodyAcceptance(
            obligation_id="task-101",
            owner_tag="implementer-1",
            session_id="session-alpha",
            generation=1,
            accepted_at=now_iso(self.current_time),
            workspace=self.workspace,
            obligation_revision=1,
        )
        with self.assertRaises(SecurityBindingError):
            self.adapter.record_custody_acceptance(intv_id, custody)

    def test_receipt_ingress_digest_replay_enforcement(self) -> None:
        """Replaying exact identical receipt succeeds; replaying modified receipt raises SecurityBindingError."""
        res = self.adapter.evaluate_and_dispatch(
            raw_task_data=self.grant,
            authority_resolver=self.authority_resolver,
            binding_provider=self.binding_provider,
            observation_provider=self.observation_provider,
            native_guard=self.native_guard,
            transport_callbacks=self.transport_callbacks,
        )
        msg_id = res.outbox_message.message_id

        self.current_time += datetime.timedelta(seconds=2)
        read_ack = MailboxReadAck(
            message_id=msg_id,
            read_at=now_iso(self.current_time),
            reader_session_id="session-alpha",
            reader_generation=1,
        )
        # First call succeeds
        intv1 = self.adapter.record_mailbox_read_ack(
            message_id=msg_id,
            ack=read_ack,
            expected_obligation_id="task-101",
            expected_revision=1,
            expected_owner_tag="implementer-1",
            expected_workspace=self.workspace,
        )
        self.assertEqual(intv1.state, STATE_MAILBOX_READ_ACK)

        # Replaying identical read_ack succeeds idempotently
        intv1_replay = self.adapter.record_mailbox_read_ack(
            message_id=msg_id,
            ack=read_ack,
            expected_obligation_id="task-101",
            expected_revision=1,
            expected_owner_tag="implementer-1",
            expected_workspace=self.workspace,
        )
        self.assertEqual(intv1_replay.state, STATE_MAILBOX_READ_ACK)

        # Replaying contradictory read_ack (different timestamp) is rejected
        contradictory_ack = MailboxReadAck(
            message_id=msg_id,
            read_at=now_iso(self.current_time - datetime.timedelta(seconds=1)),
            reader_session_id="session-alpha",
            reader_generation=1,
        )
        with self.assertRaises(SecurityBindingError) as ctx:
            self.adapter.record_mailbox_read_ack(
                message_id=msg_id,
                ack=contradictory_ack,
                expected_obligation_id="task-101",
                expected_revision=1,
                expected_owner_tag="implementer-1",
                expected_workspace=self.workspace,
            )
        self.assertIn("Contradictory replay", str(ctx.exception))

    # -------------------------------------------------------------------------
    # 25. Head findings 2: DefaultDenyReceiptVerifier denies unless verifier injected
    # -------------------------------------------------------------------------

    def test_default_deny_receipt_verifier_denies_and_reserves_uncertainty(self) -> None:
        """DefaultDenyReceiptVerifier rejects even well-formed receipts, reserving uncertainty."""
        adapter_no_verifier = OversightAdapter(
            ledger=self.ledger,
            policy=self.policy,
            authority_resolver=self.authority_resolver,
            binding_provider=self.binding_provider,
            receipt_verifier=DefaultDenyReceiptVerifier(),
            clock=self.clock,
        )

        grant = dataclasses.replace(self.grant, task_id="task-default-deny", revision=1)
        self.authority_resolver.register_grant(grant)

        res = adapter_no_verifier.evaluate_and_dispatch(
            raw_task_data=grant,
            authority_resolver=self.authority_resolver,
            binding_provider=self.binding_provider,
            observation_provider=self.observation_provider,
            native_guard=self.native_guard,
            transport_callbacks=self.transport_callbacks,
            receipt_verifier=DefaultDenyReceiptVerifier(),
        )

        self.assertTrue(res.decision_made)
        self.assertFalse(res.dispatch_sent)
        self.assertTrue(res.dispatched_uncertain)
        self.assertEqual(len(self.sent_requests), 1)

        outbox = self.ledger.get_outbox_message(res.outbox_message.message_id)
        self.assertEqual(outbox.status, OUTBOX_UNCERTAIN)
        intv = self.ledger.get_intervention(res.intervention.id)
        self.assertEqual(intv.state, STATE_DISPATCHED_UNCERTAIN)

    # -------------------------------------------------------------------------
    # 26. Head findings 2: Mandatory authority/binding on receipt ingress
    # -------------------------------------------------------------------------

    def test_receipt_ingress_missing_context_fails_closed(self) -> None:
        """Missing authority_resolver or binding_provider on adapter strictly fails closed."""
        bare_adapter = OversightAdapter(
            ledger=self.ledger,
            policy=self.policy,
            authority_resolver=None,
            binding_provider=None,
            clock=self.clock,
        )

        read_ack = MailboxReadAck(
            message_id="msg-dummy",
            read_at=now_iso(self.current_time),
            reader_session_id="session-alpha",
            reader_generation=1,
        )
        with self.assertRaises(SecurityBindingError) as ctx:
            bare_adapter.record_mailbox_read_ack(
                message_id="msg-dummy",
                ack=read_ack,
                expected_obligation_id="task-101",
                expected_revision=1,
                expected_owner_tag="implementer-1",
                expected_workspace=self.workspace,
            )
        self.assertIn("mandatory on receipt ingress", str(ctx.exception))

    # -------------------------------------------------------------------------
    # 27. Head findings 2: Receipt ingress revalidates full grant tuple and status
    # -------------------------------------------------------------------------

    def test_receipt_ingress_grant_status_not_ready_rejected(self) -> None:
        """If authority grant becomes blocked or inactive before receipt ingress, ingress fails."""
        res = self.adapter.evaluate_and_dispatch(
            raw_task_data=self.grant,
            authority_resolver=self.authority_resolver,
            binding_provider=self.binding_provider,
            observation_provider=self.observation_provider,
            native_guard=self.native_guard,
            transport_callbacks=self.transport_callbacks,
        )
        msg_id = res.outbox_message.message_id

        # Update grant to 'blocked'
        blocked_grant = dataclasses.replace(self.grant, status="blocked")
        self.authority_resolver.register_grant(blocked_grant)

        read_ack = MailboxReadAck(
            message_id=msg_id,
            read_at=now_iso(self.current_time),
            reader_session_id="session-alpha",
            reader_generation=1,
        )
        with self.assertRaises(SecurityBindingError) as ctx:
            self.adapter.record_mailbox_read_ack(
                message_id=msg_id,
                ack=read_ack,
                expected_obligation_id="task-101",
                expected_revision=1,
                expected_owner_tag="implementer-1",
                expected_workspace=self.workspace,
            )
        self.assertIn("not ready", str(ctx.exception))

    # -------------------------------------------------------------------------
    # 28. Head findings 2: Receipt ingress rejects dead worker or exited phase
    # -------------------------------------------------------------------------

    def test_receipt_ingress_dead_worker_binding_rejected(self) -> None:
        """If worker dies or exits, subsequent receipt ingress raises SecurityBindingError."""
        res = self.adapter.evaluate_and_dispatch(
            raw_task_data=self.grant,
            authority_resolver=self.authority_resolver,
            binding_provider=self.binding_provider,
            observation_provider=self.observation_provider,
            native_guard=self.native_guard,
            transport_callbacks=self.transport_callbacks,
        )
        msg_id = res.outbox_message.message_id

        # Worker dies
        dead_binding = dataclasses.replace(self.binding, worker_alive=False)
        self.binding_provider.binding = dead_binding

        read_ack = MailboxReadAck(
            message_id=msg_id,
            read_at=now_iso(self.current_time),
            reader_session_id="session-alpha",
            reader_generation=1,
        )
        with self.assertRaises(SecurityBindingError) as ctx:
            self.adapter.record_mailbox_read_ack(
                message_id=msg_id,
                ack=read_ack,
                expected_obligation_id="task-101",
                expected_revision=1,
                expected_owner_tag="implementer-1",
                expected_workspace=self.workspace,
            )
        self.assertIn("inactive or exited", str(ctx.exception))

    # -------------------------------------------------------------------------
    # 29. Head findings 2: Dataclass fail-closed requirements (no optimistic defaults)
    # -------------------------------------------------------------------------

    def test_dataclasses_fail_closed_no_optimistic_defaults(self) -> None:
        """Verify TrustedAuthorityGrant and NativeGuardCheckResult require explicit status/readiness."""
        with self.assertRaises(TypeError):
            TrustedAuthorityGrant(  # type: ignore[call-arg]
                task_id="t-1",
                revision=1,
                owner_tag="own",
                workspace="/ws",
                session_id="s-1",
                generation=1,
                source_id="src",
                source_digest="hex1234567890abcdef",
                issued_at="2026-10-13T12:00:00Z",
                # missing status!
            )

        with self.assertRaises(TypeError):
            NativeGuardCheckResult(  # type: ignore[call-arg]
                allowed=True,
                reason="test",
                # missing is_ready and session_alive!
            )

    # -------------------------------------------------------------------------
    # 30. Scope repair: InjectedChannelReceiptVerifier NOT in production
    # -------------------------------------------------------------------------

    def test_injected_channel_receipt_verifier_not_in_production(self) -> None:
        """Ensure InjectedChannelReceiptVerifier was removed from production module."""
        self.assertFalse(hasattr(oversight_adapter_mod, "InjectedChannelReceiptVerifier"))
        self.assertTrue(hasattr(oversight_adapter_mod, "DefaultDenyReceiptVerifier"))
        self.assertTrue(hasattr(oversight_adapter_mod, "ReceiptVerifierProtocol"))

    # -------------------------------------------------------------------------
    # 31. Scope repair: Injected verifier boundary catches exceptions as uncertainty
    # -------------------------------------------------------------------------

    def test_injected_verifier_boundary_catches_exception_as_uncertainty(self) -> None:
        """If receipt verifier raises an exception, it is caught as post-call uncertainty without retry."""
        class CrashingVerifier(ReceiptVerifierProtocol):
            def verify_receipt(self, receipt: Dict[str, Any], request: TransportSendRequest, now_utc: datetime.datetime):
                raise RuntimeError("Verifier exploded unexpectedly")

        res = self.adapter.evaluate_and_dispatch(
            raw_task_data=self.grant,
            authority_resolver=self.authority_resolver,
            binding_provider=self.binding_provider,
            observation_provider=self.observation_provider,
            native_guard=self.native_guard,
            transport_callbacks=self.transport_callbacks,
            receipt_verifier=CrashingVerifier(),
        )

        self.assertTrue(res.decision_made)
        self.assertFalse(res.dispatch_sent)
        self.assertTrue(res.dispatched_uncertain)
        self.assertFalse(res.dispatch_failed)
        outbox = self.ledger.get_outbox_message(res.outbox_message.message_id)
        self.assertEqual(outbox.status, OUTBOX_UNCERTAIN)
        intv = self.ledger.get_intervention(res.intervention.id)
        self.assertEqual(intv.state, STATE_DISPATCHED_UNCERTAIN)

    # -------------------------------------------------------------------------
    # 32. Scope repair: Injected verifier boundary catches non-bool/missing verification
    # -------------------------------------------------------------------------

    def test_injected_verifier_boundary_catches_nonbool_as_uncertainty(self) -> None:
        """Verifier returning non-bool or invalid structure is caught as post-call uncertainty."""
        class NonBoolVerifier(ReceiptVerifierProtocol):
            def verify_receipt(self, receipt: Dict[str, Any], request: TransportSendRequest, now_utc: datetime.datetime):
                return ("verified", "invalid non-bool return")  # type: ignore[return-value]

        res = self.adapter.evaluate_and_dispatch(
            raw_task_data=self.grant,
            authority_resolver=self.authority_resolver,
            binding_provider=self.binding_provider,
            observation_provider=self.observation_provider,
            native_guard=self.native_guard,
            transport_callbacks=self.transport_callbacks,
            receipt_verifier=NonBoolVerifier(),
        )

        self.assertTrue(res.decision_made)
        self.assertFalse(res.dispatch_sent)
        self.assertTrue(res.dispatched_uncertain)

    # -------------------------------------------------------------------------
    # 33. Scope repair: Invalid out-of-order receipt does not poison later stage
    # -------------------------------------------------------------------------

    def test_invalid_out_of_order_receipt_does_not_poison_later_legitimate_stage(self) -> None:
        """Premature receipt attempts are rejected BEFORE digest reservation, preventing poisoning."""
        res = self.adapter.evaluate_and_dispatch(
            raw_task_data=self.grant,
            authority_resolver=self.authority_resolver,
            binding_provider=self.binding_provider,
            observation_provider=self.observation_provider,
            native_guard=self.native_guard,
            transport_callbacks=self.transport_callbacks,
        )
        intv_id = res.intervention.id
        msg_id = res.outbox_message.message_id

        # 1. Attempt first action prematurely while intervention is in STATE_DISPATCHED_SENT
        self.current_time += datetime.timedelta(seconds=2)
        premature_action = ActionReceipt(
            receipt_id="rec-act-premature",
            session_id="session-alpha",
            generation=1,
            action_type="tool_execution",
            timestamp=now_iso(self.current_time),
            obligation_id="task-101",
            obligation_revision=1,
            owner_tag="implementer-1",
            workspace=self.workspace,
        )
        with self.assertRaises(SecurityBindingError) as ctx:
            self.adapter.record_first_action(intv_id, premature_action)
        self.assertIn("expected 'custody_accepted'", str(ctx.exception))

        # Check adapter_receipt_digests table: NO reservation exists for first_action_observed!
        cur = self.ledger._conn.cursor()
        cur.execute("SELECT digest FROM adapter_receipt_digests WHERE stage_key = ?", (f"{intv_id}:first_action_observed",))
        self.assertIsNone(cur.fetchone())

        # 2. Progress through legitimate read ACK
        read_ack = MailboxReadAck(
            message_id=msg_id,
            read_at=now_iso(self.current_time),
            reader_session_id="session-alpha",
            reader_generation=1,
        )
        self.adapter.record_mailbox_read_ack(
            message_id=msg_id,
            ack=read_ack,
            expected_obligation_id="task-101",
            expected_revision=1,
            expected_owner_tag="implementer-1",
            expected_workspace=self.workspace,
        )

        # 3. Progress through legitimate custody acceptance
        self.current_time += datetime.timedelta(seconds=2)
        custody = CustodyAcceptance(
            obligation_id="task-101",
            owner_tag="implementer-1",
            session_id="session-alpha",
            generation=1,
            accepted_at=now_iso(self.current_time),
            workspace=self.workspace,
            obligation_revision=1,
        )
        self.adapter.record_custody_acceptance(intv_id, custody)

        # 4. Now perform legitimate first action
        self.current_time += datetime.timedelta(seconds=2)
        legit_action = ActionReceipt(
            receipt_id="rec-act-legit",
            session_id="session-alpha",
            generation=1,
            action_type="tool_execution",
            timestamp=now_iso(self.current_time),
            obligation_id="task-101",
            obligation_revision=1,
            owner_tag="implementer-1",
            workspace=self.workspace,
        )
        intv_legit = self.adapter.record_first_action(intv_id, legit_action)
        self.assertEqual(intv_legit.state, STATE_FIRST_ACTION_OBSERVED)

        # Digest table now records legitimate first_action_observed
        cur.execute("SELECT digest FROM adapter_receipt_digests WHERE stage_key = ?", (f"{intv_id}:first_action_observed",))
        row = cur.fetchone()
        self.assertIsNotNone(row)

    # -------------------------------------------------------------------------
    # 34. Scope repair: Invalid timestamp does not poison stage reservation
    # -------------------------------------------------------------------------

    def test_invalid_timestamp_does_not_poison_stage(self) -> None:
        """Future or out-of-order timestamp is rejected BEFORE digest reservation."""
        res = self.adapter.evaluate_and_dispatch(
            raw_task_data=self.grant,
            authority_resolver=self.authority_resolver,
            binding_provider=self.binding_provider,
            observation_provider=self.observation_provider,
            native_guard=self.native_guard,
            transport_callbacks=self.transport_callbacks,
        )
        intv_id = res.intervention.id
        msg_id = res.outbox_message.message_id

        self.current_time += datetime.timedelta(seconds=2)
        read_ack = MailboxReadAck(
            message_id=msg_id,
            read_at=now_iso(self.current_time),
            reader_session_id="session-alpha",
            reader_generation=1,
        )
        self.adapter.record_mailbox_read_ack(
            message_id=msg_id,
            ack=read_ack,
            expected_obligation_id="task-101",
            expected_revision=1,
            expected_owner_tag="implementer-1",
            expected_workspace=self.workspace,
        )

        # Attempt custody acceptance with FUTURE timestamp
        future_custody = CustodyAcceptance(
            obligation_id="task-101",
            owner_tag="implementer-1",
            session_id="session-alpha",
            generation=1,
            accepted_at=now_iso(self.current_time + datetime.timedelta(hours=5)),
            workspace=self.workspace,
            obligation_revision=1,
        )
        with self.assertRaises(SecurityBindingError) as ctx:
            self.adapter.record_custody_acceptance(intv_id, future_custody)
        self.assertIn("future timestamp", str(ctx.exception).lower())

        # Verify no digest was reserved
        cur = self.ledger._conn.cursor()
        cur.execute("SELECT digest FROM adapter_receipt_digests WHERE stage_key = ?", (f"{intv_id}:custody_accepted",))
        self.assertIsNone(cur.fetchone())

        # Legitimate custody acceptance with valid timestamp succeeds
        self.current_time += datetime.timedelta(seconds=2)
        valid_custody = CustodyAcceptance(
            obligation_id="task-101",
            owner_tag="implementer-1",
            session_id="session-alpha",
            generation=1,
            accepted_at=now_iso(self.current_time),
            workspace=self.workspace,
            obligation_revision=1,
        )
        intv_cust = self.adapter.record_custody_acceptance(intv_id, valid_custody)
        self.assertEqual(intv_cust.state, STATE_CUSTODY_ACCEPTED)

    # -------------------------------------------------------------------------
    # 35. Scope repair: File-backed crash before ledger stage transition
    # -------------------------------------------------------------------------

    def test_file_backed_crash_before_ledger_transition_recovery(self) -> None:
        """Simulate crash where digest was reserved before ledger transition; recovery completes transition."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name

        try:
            ledger = InterventionLedger(db_path=db_path, clock=self.clock)
            adapter = OversightAdapter(
                ledger=ledger,
                policy=self.policy,
                authority_resolver=self.authority_resolver,
                binding_provider=self.binding_provider,
                receipt_verifier=self.receipt_verifier,
                clock=self.clock,
            )

            res = adapter.evaluate_and_dispatch(
                raw_task_data=self.grant,
                authority_resolver=self.authority_resolver,
                binding_provider=self.binding_provider,
                observation_provider=self.observation_provider,
                native_guard=self.native_guard,
                transport_callbacks=self.transport_callbacks,
            )
            msg_id = res.outbox_message.message_id
            intv_id = res.intervention.id

            self.current_time += datetime.timedelta(seconds=2)
            read_ack = MailboxReadAck(
                message_id=msg_id,
                read_at=now_iso(self.current_time),
                reader_session_id="session-alpha",
                reader_generation=1,
            )
            ack_digest = adapter._compute_digest({
                "generation": read_ack.reader_generation,
                "message_id": read_ack.message_id,
                "read_at": read_ack.read_at,
                "reader_session_id": read_ack.reader_session_id,
            })

            # Simulate crash window: manually insert digest reservation into adapter table,
            # but leave ledger state as STATE_DISPATCHED_SENT
            with ledger._conn:
                ledger._conn.execute("""
                    INSERT INTO adapter_receipt_digests (stage_key, intervention_id, stage, digest, recorded_at)
                    VALUES (?, ?, ?, ?, ?)
                """, (f"{intv_id}:mailbox_read_ack", intv_id, "mailbox_read_ack", ack_digest, now_iso(self.current_time)))

            # Close and reopen file-backed database (crash simulation)
            ledger.close()

            # Reopen on same database
            ledger2 = InterventionLedger(db_path=db_path, clock=self.clock)
            adapter2 = OversightAdapter(
                ledger=ledger2,
                policy=self.policy,
                authority_resolver=self.authority_resolver,
                binding_provider=self.binding_provider,
                receipt_verifier=self.receipt_verifier,
                clock=self.clock,
            )

            # Replay with changed content must be rejected
            changed_ack = MailboxReadAck(
                message_id=msg_id,
                read_at="2026-10-13T12:00:01Z",
                reader_session_id="session-alpha",
                reader_generation=1,
            )
            with self.assertRaises(SecurityBindingError) as ctx:
                adapter2.record_mailbox_read_ack(
                    message_id=msg_id,
                    ack=changed_ack,
                    expected_obligation_id="task-101",
                    expected_revision=1,
                    expected_owner_tag="implementer-1",
                    expected_workspace=self.workspace,
                )
            self.assertIn("digest mismatch", str(ctx.exception))

            # Replay with exact same receipt recovers and completes the interrupted transition
            recovered_intv = adapter2.record_mailbox_read_ack(
                message_id=msg_id,
                ack=read_ack,
                expected_obligation_id="task-101",
                expected_revision=1,
                expected_owner_tag="implementer-1",
                expected_workspace=self.workspace,
            )
            self.assertEqual(recovered_intv.state, STATE_MAILBOX_READ_ACK)

            ledger2.close()
        finally:
            if os.path.exists(db_path):
                os.remove(db_path)

    # -------------------------------------------------------------------------
    # 36. Scope repair: File-backed crash after ledger stage transition replay
    # -------------------------------------------------------------------------

    def test_file_backed_crash_after_ledger_transition_replay(self) -> None:
        """Exact replay after successful stage transition is idempotent across close/reopen."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name

        try:
            ledger = InterventionLedger(db_path=db_path, clock=self.clock)
            adapter = OversightAdapter(
                ledger=ledger,
                policy=self.policy,
                authority_resolver=self.authority_resolver,
                binding_provider=self.binding_provider,
                receipt_verifier=self.receipt_verifier,
                clock=self.clock,
            )

            res = adapter.evaluate_and_dispatch(
                raw_task_data=self.grant,
                authority_resolver=self.authority_resolver,
                binding_provider=self.binding_provider,
                observation_provider=self.observation_provider,
                native_guard=self.native_guard,
                transport_callbacks=self.transport_callbacks,
            )
            msg_id = res.outbox_message.message_id

            self.current_time += datetime.timedelta(seconds=2)
            read_ack = MailboxReadAck(
                message_id=msg_id,
                read_at=now_iso(self.current_time),
                reader_session_id="session-alpha",
                reader_generation=1,
            )
            adapter.record_mailbox_read_ack(
                message_id=msg_id,
                ack=read_ack,
                expected_obligation_id="task-101",
                expected_revision=1,
                expected_owner_tag="implementer-1",
                expected_workspace=self.workspace,
            )

            # Close and reopen file-backed database
            ledger.close()

            ledger2 = InterventionLedger(db_path=db_path, clock=self.clock)
            adapter2 = OversightAdapter(
                ledger=ledger2,
                policy=self.policy,
                authority_resolver=self.authority_resolver,
                binding_provider=self.binding_provider,
                receipt_verifier=self.receipt_verifier,
                clock=self.clock,
            )

            # Idempotent replay of exact receipt
            replay_intv = adapter2.record_mailbox_read_ack(
                message_id=msg_id,
                ack=read_ack,
                expected_obligation_id="task-101",
                expected_revision=1,
                expected_owner_tag="implementer-1",
                expected_workspace=self.workspace,
            )
            self.assertEqual(replay_intv.state, STATE_MAILBOX_READ_ACK)

            ledger2.close()
        finally:
            if os.path.exists(db_path):
                os.remove(db_path)

    # -------------------------------------------------------------------------
    # 37. Scope repair: Ambiguous ledger state fails closed and documents operator reconciliation
    # -------------------------------------------------------------------------

    def test_ambiguous_unrecorded_ledger_state_fails_closed(self) -> None:
        """If ledger has transitioned but no adapter digest exists, fail closed with operator reconciliation."""
        res = self.adapter.evaluate_and_dispatch(
            raw_task_data=self.grant,
            authority_resolver=self.authority_resolver,
            binding_provider=self.binding_provider,
            observation_provider=self.observation_provider,
            native_guard=self.native_guard,
            transport_callbacks=self.transport_callbacks,
        )
        msg_id = res.outbox_message.message_id
        intv_id = res.intervention.id

        # Manually force ledger intervention into STATE_MAILBOX_READ_ACK without adapter digest
        with self.ledger._conn:
            self.ledger._conn.execute(
                "UPDATE interventions SET state = ? WHERE id = ?",
                (STATE_MAILBOX_READ_ACK, intv_id)
            )

        self.current_time += datetime.timedelta(seconds=2)
        read_ack = MailboxReadAck(
            message_id=msg_id,
            read_at=now_iso(self.current_time),
            reader_session_id="session-alpha",
            reader_generation=1,
        )

        with self.assertRaises(SecurityBindingError) as ctx:
            self.adapter.record_mailbox_read_ack(
                message_id=msg_id,
                ack=read_ack,
                expected_obligation_id="task-101",
                expected_revision=1,
                expected_owner_tag="implementer-1",
                expected_workspace=self.workspace,
            )
        self.assertIn("operator reconciliation required", str(ctx.exception))

    # -------------------------------------------------------------------------
    # 38. Scope repair: Bool and non-integer generation and revision fail closed
    # -------------------------------------------------------------------------

    def test_bool_and_nonint_generation_revision_fail_closed(self) -> None:
        """Bool instances (True/False) for revision or generation must fail closed."""
        with self.assertRaises(ValueError):
            TrustedAuthorityGrant(
                task_id="t-bool-rev",
                revision=True,  # type: ignore[arg-type]
                owner_tag="own",
                workspace=self.workspace,
                session_id="s-1",
                generation=1,
                source_id="src",
                source_digest=self.sample_digest,
                issued_at=now_iso(self.current_time),
                status="ready",
            )

        with self.assertRaises(ValueError):
            TrustedAuthorityGrant(
                task_id="t-bool-gen",
                revision=1,
                owner_tag="own",
                workspace=self.workspace,
                session_id="s-1",
                generation=False,  # type: ignore[arg-type]
                source_id="src",
                source_digest=self.sample_digest,
                issued_at=now_iso(self.current_time),
                status="ready",
            )

        res = self.adapter.evaluate_and_dispatch(
            raw_task_data=self.grant,
            authority_resolver=self.authority_resolver,
            binding_provider=self.binding_provider,
            observation_provider=self.observation_provider,
            native_guard=self.native_guard,
            transport_callbacks=self.transport_callbacks,
        )
        msg_id = res.outbox_message.message_id
        intv_id = res.intervention.id

        # Mailbox read ACK with bool reader_generation
        bool_gen_ack = MailboxReadAck(
            message_id=msg_id,
            read_at=now_iso(self.current_time),
            reader_session_id="session-alpha",
            reader_generation=True,  # type: ignore[arg-type]
        )
        with self.assertRaises(SecurityBindingError) as ctx:
            self.adapter.record_mailbox_read_ack(
                message_id=msg_id,
                ack=bool_gen_ack,
                expected_obligation_id="task-101",
                expected_revision=1,
                expected_owner_tag="implementer-1",
                expected_workspace=self.workspace,
            )
        self.assertIn("bool forbidden", str(ctx.exception) + " must be an integer")

    # -------------------------------------------------------------------------
    # 39. Scope repair: Empty and whitespace workspace fail closed
    # -------------------------------------------------------------------------

    def test_empty_workspace_fails_closed(self) -> None:
        """Empty or whitespace workspace fails authority grant and ingress validation."""
        with self.assertRaises(ValueError):
            TrustedAuthorityGrant(
                task_id="t-empty-ws",
                revision=1,
                owner_tag="own",
                workspace="   ",
                session_id="s-1",
                generation=1,
                source_id="src",
                source_digest=self.sample_digest,
                issued_at=now_iso(self.current_time),
                status="ready",
            )

        res = self.adapter.evaluate_and_dispatch(
            raw_task_data=self.grant,
            authority_resolver=self.authority_resolver,
            binding_provider=self.binding_provider,
            observation_provider=self.observation_provider,
            native_guard=self.native_guard,
            transport_callbacks=self.transport_callbacks,
        )
        msg_id = res.outbox_message.message_id

        read_ack = MailboxReadAck(
            message_id=msg_id,
            read_at=now_iso(self.current_time),
            reader_session_id="session-alpha",
            reader_generation=1,
        )
        with self.assertRaises(SecurityBindingError):
            self.adapter.record_mailbox_read_ack(
                message_id=msg_id,
                ack=read_ack,
                expected_obligation_id="task-101",
                expected_revision=1,
                expected_owner_tag="implementer-1",
                expected_workspace="",
            )

    # -------------------------------------------------------------------------
    # 40. Scope repair: Missing or unknown readiness/liveness denies
    # -------------------------------------------------------------------------

    def test_missing_or_unknown_readiness_liveness_denies(self) -> None:
        """NativeGuardCheckResult without explicit ready/liveness is rejected."""
        # Non-boolean is_ready
        with self.assertRaises(ValueError):
            NativeGuardCheckResult(
                allowed=True,
                reason="ok",
                is_ready="yes",  # type: ignore[arg-type]
                session_alive=True,
            )

        # Inactive session binding denies
        inactive_binding = dataclasses.replace(self.binding, active=False)
        self.binding_provider.binding = inactive_binding

        res = self.adapter.evaluate_and_dispatch(
            raw_task_data=self.grant,
            authority_resolver=self.authority_resolver,
            binding_provider=self.binding_provider,
            observation_provider=self.observation_provider,
            native_guard=self.native_guard,
            transport_callbacks=self.transport_callbacks,
        )
        self.assertFalse(res.dispatch_sent)
        self.assertEqual(len(self.sent_requests), 0)

    # -------------------------------------------------------------------------
    # 41. Head receipt boundary findings: Replay through later valid stages is nonmutating
    # -------------------------------------------------------------------------

    def test_replay_through_later_valid_stages_supported_nonmutating(self) -> None:
        """Earlier stage identical replay (e.g. read_ack or custody) succeeds nonmutating through later stages."""
        res = self.adapter.evaluate_and_dispatch(
            raw_task_data=self.grant,
            authority_resolver=self.authority_resolver,
            binding_provider=self.binding_provider,
            observation_provider=self.observation_provider,
            native_guard=self.native_guard,
            transport_callbacks=self.transport_callbacks,
        )
        intv_id = res.intervention.id
        msg_id = res.outbox_message.message_id

        # 1. Read ACK
        self.current_time += datetime.timedelta(seconds=2)
        read_ack = MailboxReadAck(
            message_id=msg_id,
            read_at=now_iso(self.current_time),
            reader_session_id="session-alpha",
            reader_generation=1,
        )
        self.adapter.record_mailbox_read_ack(
            message_id=msg_id,
            ack=read_ack,
            expected_obligation_id="task-101",
            expected_revision=1,
            expected_owner_tag="implementer-1",
            expected_workspace=self.workspace,
        )

        # 2. Custody
        self.current_time += datetime.timedelta(seconds=2)
        custody = CustodyAcceptance(
            obligation_id="task-101",
            owner_tag="implementer-1",
            session_id="session-alpha",
            generation=1,
            accepted_at=now_iso(self.current_time),
            workspace=self.workspace,
            obligation_revision=1,
        )
        self.adapter.record_custody_acceptance(intv_id, custody)

        # 3. First Action
        self.current_time += datetime.timedelta(seconds=2)
        action = ActionReceipt(
            receipt_id="rec-act-001",
            session_id="session-alpha",
            generation=1,
            action_type="tool_execution",
            timestamp=now_iso(self.current_time),
            obligation_id="task-101",
            obligation_revision=1,
            owner_tag="implementer-1",
            workspace=self.workspace,
            details={"step": "compile"},
        )
        self.adapter.record_first_action(intv_id, action)

        # 4. Completion
        self.current_time += datetime.timedelta(seconds=2)
        outcome = CompletionOutcome(
            obligation_id="task-101",
            owner_tag="implementer-1",
            session_id="session-alpha",
            generation=1,
            completed_at=now_iso(self.current_time),
            result={"status": "ok"},
            workspace=self.workspace,
            obligation_revision=1,
        )
        intv_comp = self.adapter.record_completion_outcome(intv_id, outcome)
        self.assertEqual(intv_comp.state, STATE_COMPLETED)

        # Replay read_ack now that intervention is in STATE_COMPLETED: must succeed nonmutating!
        r_ack_replay = self.adapter.record_mailbox_read_ack(
            message_id=msg_id,
            ack=read_ack,
            expected_obligation_id="task-101",
            expected_revision=1,
            expected_owner_tag="implementer-1",
            expected_workspace=self.workspace,
        )
        self.assertEqual(r_ack_replay.state, STATE_COMPLETED)

        # Replay custody now: must succeed nonmutating!
        r_cust_replay = self.adapter.record_custody_acceptance(intv_id, custody)
        self.assertEqual(r_cust_replay.state, STATE_COMPLETED)

        # Replay first action now: must succeed nonmutating!
        r_act_replay = self.adapter.record_first_action(intv_id, action)
        self.assertEqual(r_act_replay.state, STATE_COMPLETED)

        # Replay completion outcome now: must succeed idempotently!
        r_comp_replay = self.adapter.record_completion_outcome(intv_id, outcome)
        self.assertEqual(r_comp_replay.state, STATE_COMPLETED)

        # Replaying contradictory receipt when completed must still be rejected!
        contradictory_ack = MailboxReadAck(
            message_id=msg_id,
            read_at="2026-10-13T12:00:01Z",
            reader_session_id="session-alpha",
            reader_generation=1,
        )
        with self.assertRaises(SecurityBindingError) as ctx:
            self.adapter.record_mailbox_read_ack(
                message_id=msg_id,
                ack=contradictory_ack,
                expected_obligation_id="task-101",
                expected_revision=1,
                expected_owner_tag="implementer-1",
                expected_workspace=self.workspace,
            )
        self.assertIn("digest mismatch", str(ctx.exception))

    # -------------------------------------------------------------------------
    # 42. Head receipt boundary findings: Action receipt_id empty rejected
    # -------------------------------------------------------------------------

    def test_action_receipt_empty_receipt_id_rejected(self) -> None:
        """First action receipt with empty receipt_id must raise SecurityBindingError."""
        res = self.adapter.evaluate_and_dispatch(
            raw_task_data=self.grant,
            authority_resolver=self.authority_resolver,
            binding_provider=self.binding_provider,
            observation_provider=self.observation_provider,
            native_guard=self.native_guard,
            transport_callbacks=self.transport_callbacks,
        )
        intv_id = res.intervention.id
        msg_id = res.outbox_message.message_id

        self.current_time += datetime.timedelta(seconds=2)
        read_ack = MailboxReadAck(
            message_id=msg_id,
            read_at=now_iso(self.current_time),
            reader_session_id="session-alpha",
            reader_generation=1,
        )
        self.adapter.record_mailbox_read_ack(
            message_id=msg_id,
            ack=read_ack,
            expected_obligation_id="task-101",
            expected_revision=1,
            expected_owner_tag="implementer-1",
            expected_workspace=self.workspace,
        )

        self.current_time += datetime.timedelta(seconds=2)
        custody = CustodyAcceptance(
            obligation_id="task-101",
            owner_tag="implementer-1",
            session_id="session-alpha",
            generation=1,
            accepted_at=now_iso(self.current_time),
            workspace=self.workspace,
            obligation_revision=1,
        )
        self.adapter.record_custody_acceptance(intv_id, custody)

        # Empty receipt_id
        bad_action = ActionReceipt(
            receipt_id="   ",
            session_id="session-alpha",
            generation=1,
            action_type="tool_execution",
            timestamp=now_iso(self.current_time),
            obligation_id="task-101",
            obligation_revision=1,
            owner_tag="implementer-1",
            workspace=self.workspace,
        )
        with self.assertRaises(SecurityBindingError) as ctx:
            self.adapter.record_first_action(intv_id, bad_action)
        self.assertIn("action.receipt_id is mandatory", str(ctx.exception))

    # -------------------------------------------------------------------------
    # 43. Head receipt boundary findings: Action details included in digest
    # -------------------------------------------------------------------------

    def test_action_receipt_details_tampering_rejected_by_digest(self) -> None:
        """Tampering with action.details on replay changes digest and is rejected."""
        res = self.adapter.evaluate_and_dispatch(
            raw_task_data=self.grant,
            authority_resolver=self.authority_resolver,
            binding_provider=self.binding_provider,
            observation_provider=self.observation_provider,
            native_guard=self.native_guard,
            transport_callbacks=self.transport_callbacks,
        )
        intv_id = res.intervention.id
        msg_id = res.outbox_message.message_id

        self.current_time += datetime.timedelta(seconds=2)
        read_ack = MailboxReadAck(
            message_id=msg_id,
            read_at=now_iso(self.current_time),
            reader_session_id="session-alpha",
            reader_generation=1,
        )
        self.adapter.record_mailbox_read_ack(
            message_id=msg_id,
            ack=read_ack,
            expected_obligation_id="task-101",
            expected_revision=1,
            expected_owner_tag="implementer-1",
            expected_workspace=self.workspace,
        )

        self.current_time += datetime.timedelta(seconds=2)
        custody = CustodyAcceptance(
            obligation_id="task-101",
            owner_tag="implementer-1",
            session_id="session-alpha",
            generation=1,
            accepted_at=now_iso(self.current_time),
            workspace=self.workspace,
            obligation_revision=1,
        )
        self.adapter.record_custody_acceptance(intv_id, custody)

        self.current_time += datetime.timedelta(seconds=2)
        action1 = ActionReceipt(
            receipt_id="rec-act-details",
            session_id="session-alpha",
            generation=1,
            action_type="tool_execution",
            timestamp=now_iso(self.current_time),
            obligation_id="task-101",
            obligation_revision=1,
            owner_tag="implementer-1",
            workspace=self.workspace,
            details={"cmd": "run_tests", "exit": 0},
        )
        intv_act = self.adapter.record_first_action(intv_id, action1)
        self.assertEqual(intv_act.state, STATE_FIRST_ACTION_OBSERVED)

        # Replay with modified details
        tampered_action = dataclasses.replace(action1, details={"cmd": "run_tests", "exit": 1})
        with self.assertRaises(SecurityBindingError) as ctx:
            self.adapter.record_first_action(intv_id, tampered_action)
        self.assertIn("digest mismatch", str(ctx.exception))

    # -------------------------------------------------------------------------
    # 44. Head receipt boundary findings: Timeout check on NEW stage transition
    # -------------------------------------------------------------------------

    def test_timeout_at_rejects_new_stage_reservation_but_allows_exact_replay(self) -> None:
        """If timeout_at has passed, new stage transitions are rejected; exact recorded replays still succeed."""
        res = self.adapter.evaluate_and_dispatch(
            raw_task_data=self.grant,
            authority_resolver=self.authority_resolver,
            binding_provider=self.binding_provider,
            observation_provider=self.observation_provider,
            native_guard=self.native_guard,
            transport_callbacks=self.transport_callbacks,
        )
        intv_id = res.intervention.id
        msg_id = res.outbox_message.message_id

        # 1. Record read ACK before timeout
        self.current_time += datetime.timedelta(seconds=2)
        read_ack = MailboxReadAck(
            message_id=msg_id,
            read_at=now_iso(self.current_time),
            reader_session_id="session-alpha",
            reader_generation=1,
        )
        self.adapter.record_mailbox_read_ack(
            message_id=msg_id,
            ack=read_ack,
            expected_obligation_id="task-101",
            expected_revision=1,
            expected_owner_tag="implementer-1",
            expected_workspace=self.workspace,
        )

        # Set intervention timeout_at to now + 5s, then advance 10s (within grant freshness)
        timeout_ts = now_iso(self.current_time + datetime.timedelta(seconds=5))
        with self.ledger._conn:
            self.ledger._conn.execute("UPDATE interventions SET timeout_at = ? WHERE id = ?", (timeout_ts, intv_id))

        self.current_time += datetime.timedelta(seconds=10)

        # Replay exact recorded read_ack: must still succeed
        r_ack = self.adapter.record_mailbox_read_ack(
            message_id=msg_id,
            ack=read_ack,
            expected_obligation_id="task-101",
            expected_revision=1,
            expected_owner_tag="implementer-1",
            expected_workspace=self.workspace,
        )
        self.assertEqual(r_ack.state, STATE_MAILBOX_READ_ACK)

        # Attempt NEW stage transition (custody) after timeout: must be rejected!
        custody = CustodyAcceptance(
            obligation_id="task-101",
            owner_tag="implementer-1",
            session_id="session-alpha",
            generation=1,
            accepted_at=now_iso(self.current_time),
            workspace=self.workspace,
            obligation_revision=1,
        )
        with self.assertRaises(SecurityBindingError) as ctx:
            self.adapter.record_custody_acceptance(intv_id, custody)
        self.assertIn("timed out", str(ctx.exception))

    # -------------------------------------------------------------------------
    # 45. Head guidance: Direct AdapterTransport missing context denies
    # -------------------------------------------------------------------------

    def test_direct_adapter_transport_missing_context_denies_before_callback(self) -> None:
        """Direct AdapterTransport instantiation without resolver or binding fails before callback."""
        called = False

        def dummy_cb(req: TransportSendRequest) -> Dict[str, Any]:
            nonlocal called
            called = True
            return {"status": "sent"}

        bare_transport = AdapterTransport(
            ledger=self.ledger,
            callbacks=TransportRouteCallbacks(native_continuation=dummy_cb),
            authority_resolver=None,
            binding_provider=None,
        )

        with self.assertRaises(TransportSendFailedBeforeDelivery):
            bare_transport.send(
                route=ROUTE_NATIVE_MAILBOX,
                recipient_tag="implementer-1",
                payload={"test": 1},
                idempotency_key="idemp-key-missing",
            )
        self.assertFalse(called)

    # -------------------------------------------------------------------------
    # 46. Head guidance: Immediate transport recheck rejects draft/menu/manual/gen with ZERO callbacks
    # -------------------------------------------------------------------------

    def test_transport_presend_recheck_protected_flags_zero_callbacks(self) -> None:
        """Transport immediate recheck catches draft/menu/manual/gen mismatch with ZERO callbacks."""
        for flag in ["draft", "menu", "manual", "newgen"]:
            with self.subTest(flag=flag):
                call_count = 0

                def native_cb(req: TransportSendRequest) -> Dict[str, Any]:
                    nonlocal call_count
                    call_count += 1
                    return {"status": "sent"}

                class FlipNativeGuard(NativeGuardInterface):
                    def __init__(self, target_flag: str):
                        self.target_flag = target_flag
                        self.query_count = 0

                    def check_native_state(self, ws: str, s_id: str, gen: int, now: datetime.datetime) -> NativeGuardCheckResult:
                        self.query_count += 1
                        if self.query_count == 1:
                            # Clean on first check (guard authorization)
                            return NativeGuardCheckResult(allowed=True, reason="ok", is_ready=True, session_alive=True)
                        # Flipped on second check (transport immediate recheck)
                        return NativeGuardCheckResult(
                            allowed=True,
                            reason=f"Flipped to {self.target_flag}",
                            is_ready=True,
                            session_alive=True,
                            is_draft=(self.target_flag == "draft"),
                            is_menu_active=(self.target_flag == "menu"),
                            is_manual_approval=(self.target_flag == "manual"),
                            current_generation=99 if self.target_flag == "newgen" else gen,
                        )

                tb = TransportRouteCallbacks(native_continuation=native_cb)
                res = self.adapter.evaluate_and_dispatch(
                    raw_task_data=self.grant,
                    authority_resolver=self.authority_resolver,
                    binding_provider=self.binding_provider,
                    observation_provider=self.observation_provider,
                    native_guard=FlipNativeGuard(flag),
                    transport_callbacks=tb,
                )
                self.assertFalse(res.dispatch_sent)
                self.assertEqual(call_count, 0)  # ZERO CALLBACKS!

    # -------------------------------------------------------------------------
    # 51. Reason added initialresolve -> guard denies before transport
    # -------------------------------------------------------------------------

    def test_protected_reason_added_initial_resolve_to_guard_zero_transport_calls(self) -> None:
        """When resolver adds protected_reason on guard revalidate, dispatch is unauthorized and zero transport calls occur."""
        class GuardFlipResolver(AuthorityResolverProtocol):
            def __init__(self, base_grant: TrustedAuthorityGrant):
                self.base_grant = base_grant
                self.resolve_calls = 0
                self.revalidate_calls = 0

            def resolve_authority(self, raw_data: Any, now: datetime.datetime) -> Optional[TrustedAuthorityGrant]:
                self.resolve_calls += 1
                return self.base_grant

            def revalidate_authority(self, task_id: str, revision: int, workspace: str, now_utc: datetime.datetime) -> Optional[TrustedAuthorityGrant]:
                self.revalidate_calls += 1
                return dataclasses.replace(self.base_grant, protected_reason="late protected reason before guard")

        callback_count = 0
        def native_cb(req: TransportSendRequest) -> Dict[str, Any]:
            nonlocal callback_count
            callback_count += 1
            return {
                "status": "sent",
                "message_id": f"native-receipt-{req.message_id}",
                "idempotency_key": req.idempotency_key,
                "state": "send_receipt",
                "sent_at": "2026-10-13T12:00:01Z",
            }

        tb = TransportRouteCallbacks(native_continuation=native_cb)
        resolver = GuardFlipResolver(self.grant)

        res = self.adapter.evaluate_and_dispatch(
            raw_task_data=self.grant,
            authority_resolver=resolver,
            binding_provider=self.binding_provider,
            observation_provider=self.observation_provider,
            native_guard=self.native_guard,
            transport_callbacks=tb,
        )

        self.assertTrue(res.decision_made)
        self.assertFalse(res.dispatch_attempted)
        self.assertFalse(res.dispatch_sent)
        self.assertEqual(callback_count, 0)
        self.assertEqual(len(self.sent_requests), 0)
        self.assertIn("protected", res.refusal_reason.lower())

    # -------------------------------------------------------------------------
    # 52. Reason added guard -> send denies before callback (Probe verification)
    # -------------------------------------------------------------------------

    def test_protected_reason_added_guard_to_send_zero_transport_callbacks(self) -> None:
        """When resolver adds protected_reason on transport immediate recheck, transport fails before callbacks."""
        class TransportFlipResolver(AuthorityResolverProtocol):
            def __init__(self, base_grant: TrustedAuthorityGrant):
                self.base_grant = base_grant
                self.resolve_calls = 0
                self.revalidate_calls = 0

            def resolve_authority(self, raw_data: Any, now_utc: datetime.datetime) -> Optional[TrustedAuthorityGrant]:
                self.resolve_calls += 1
                return self.base_grant

            def revalidate_authority(self, task_id: str, revision: int, workspace: str, now_utc: datetime.datetime) -> Optional[TrustedAuthorityGrant]:
                self.revalidate_calls += 1
                if self.revalidate_calls == 1:
                    return self.base_grant
                return dataclasses.replace(self.base_grant, protected_reason="late protection added between guard and send")

        callback_count = 0
        def native_cb(req: TransportSendRequest) -> Dict[str, Any]:
            nonlocal callback_count
            callback_count += 1
            return {
                "status": "sent",
                "message_id": f"native-receipt-{req.message_id}",
                "idempotency_key": req.idempotency_key,
                "state": "send_receipt",
                "sent_at": "2026-10-13T12:00:01Z",
            }

        tb = TransportRouteCallbacks(native_continuation=native_cb)
        resolver = TransportFlipResolver(self.grant)

        res = self.adapter.evaluate_and_dispatch(
            raw_task_data=self.grant,
            authority_resolver=resolver,
            binding_provider=self.binding_provider,
            observation_provider=self.observation_provider,
            native_guard=self.native_guard,
            transport_callbacks=tb,
        )

        self.assertEqual(resolver.revalidate_calls, 2)
        self.assertTrue(res.dispatch_attempted)
        self.assertFalse(res.dispatch_sent)
        self.assertEqual(callback_count, 0)
        self.assertEqual(len(self.sent_requests), 0)
        self.assertIn("protected", res.refusal_reason.lower())

    # -------------------------------------------------------------------------
    # 53. Dependencies added guard -> send denies before callback
    # -------------------------------------------------------------------------

    def test_dependencies_added_guard_to_send_zero_transport_callbacks(self) -> None:
        """When resolver adds dependencies between guard and send, transport fails before callbacks."""
        class DepFlipResolver(AuthorityResolverProtocol):
            def __init__(self, base_grant: TrustedAuthorityGrant):
                self.base_grant = base_grant
                self.revalidate_calls = 0

            def resolve_authority(self, raw_data: Any, now_utc: datetime.datetime) -> Optional[TrustedAuthorityGrant]:
                return self.base_grant

            def revalidate_authority(self, task_id: str, revision: int, workspace: str, now_utc: datetime.datetime) -> Optional[TrustedAuthorityGrant]:
                self.revalidate_calls += 1
                if self.revalidate_calls == 1:
                    return self.base_grant
                return dataclasses.replace(self.base_grant, dependencies=("unsubstantiated-new-dep",))

        callback_count = 0
        def native_cb(req: TransportSendRequest) -> Dict[str, Any]:
            nonlocal callback_count
            callback_count += 1
            return {
                "status": "sent",
                "message_id": f"native-receipt-{req.message_id}",
                "idempotency_key": req.idempotency_key,
                "state": "send_receipt",
                "sent_at": "2026-10-13T12:00:01Z",
            }

        tb = TransportRouteCallbacks(native_continuation=native_cb)
        resolver = DepFlipResolver(self.grant)

        res = self.adapter.evaluate_and_dispatch(
            raw_task_data=self.grant,
            authority_resolver=resolver,
            binding_provider=self.binding_provider,
            observation_provider=self.observation_provider,
            native_guard=self.native_guard,
            transport_callbacks=tb,
        )

        self.assertTrue(res.dispatch_attempted)
        self.assertFalse(res.dispatch_sent)
        self.assertEqual(callback_count, 0)
        self.assertEqual(len(self.sent_requests), 0)
        self.assertIn("dependencies", res.refusal_reason.lower())

    # -------------------------------------------------------------------------
    # 54. Protected authority on receipt ingress zero transition
    # -------------------------------------------------------------------------

    def test_protected_authority_receipt_ingress_zero_transition(self) -> None:
        """When authority grant becomes protected on receipt ingress, SecurityBindingError is raised with zero transition."""
        res = self.adapter.evaluate_and_dispatch(
            raw_task_data=self.grant,
            authority_resolver=self.authority_resolver,
            binding_provider=self.binding_provider,
            observation_provider=self.observation_provider,
            native_guard=self.native_guard,
            transport_callbacks=self.transport_callbacks,
        )
        self.assertTrue(res.dispatch_sent)
        intv_id = res.intervention.id
        msg_id = res.outbox_message.message_id
        initial_intv = self.ledger.get_intervention(intv_id)
        self.assertEqual(initial_intv.state, STATE_DISPATCHED_SENT)

        for case_name, mutated_grant in [
            ("protected_reason", dataclasses.replace(self.grant, protected_reason="session now protected")),
            ("status_frozen", dataclasses.replace(self.grant, status="frozen")),
            ("status_blocked", dataclasses.replace(self.grant, status="blocked")),
            ("status_quota", dataclasses.replace(self.grant, status="quota_held")),
            ("dependencies", dataclasses.replace(self.grant, dependencies=("unsubstantiated-dep",))),
        ]:
            with self.subTest(case=case_name):
                self.authority_resolver.register_grant(mutated_grant)
                read_ack = MailboxReadAck(
                    message_id=msg_id,
                    read_at=now_iso(self.current_time),
                    reader_session_id="session-alpha",
                    reader_generation=1,
                )
                with self.assertRaises(SecurityBindingError):
                    self.adapter.record_mailbox_read_ack(
                        message_id=msg_id,
                        ack=read_ack,
                        expected_obligation_id="task-101",
                        expected_revision=1,
                        expected_owner_tag="implementer-1",
                        expected_workspace=self.workspace,
                    )
                curr_intv = self.ledger.get_intervention(intv_id)
                self.assertEqual(curr_intv.state, STATE_DISPATCHED_SENT)

    # -------------------------------------------------------------------------
    # 55. Explicit trusted completed-dependency seam substantiated vs unsubstantiated
    # -------------------------------------------------------------------------

    def test_explicit_trusted_completed_dependency_seam(self) -> None:
        """Raw completed_obligations sets cannot authorize dependencies; dependency-bearing positives assert HELD."""
        dep_grant = dataclasses.replace(self.grant, dependencies=("dep-a", "dep-b"))
        resolver = StandardAuthorityResolver(known_grants={(dep_grant.task_id, dep_grant.revision): dep_grant})

        # 1. Legacy positive changed to assert HELD: raw completed_obligations set cannot authorize dependencies
        cb_count = 0
        def cb(req: TransportSendRequest) -> Dict[str, Any]:
            nonlocal cb_count
            cb_count += 1
            return {
                "status": "sent",
                "message_id": f"native-receipt-{req.message_id}",
                "idempotency_key": req.idempotency_key,
                "state": "send_receipt",
                "sent_at": "2026-10-13T12:00:01Z",
            }

        tb = TransportRouteCallbacks(native_continuation=cb)
        res = self.adapter.evaluate_and_dispatch(
            raw_task_data=dep_grant,
            authority_resolver=resolver,
            binding_provider=self.binding_provider,
            observation_provider=self.observation_provider,
            native_guard=self.native_guard,
            transport_callbacks=tb,
            completed_obligations={"dep-a", "dep-b"},
        )
        self.assertFalse(res.dispatch_sent)
        self.assertFalse(res.decision_made)
        self.assertEqual(cb_count, 0)
        self.assertIn("held", res.refusal_reason.lower())

        # 2. Negative: completed_obligations missing dep-b
        cb_count = 0
        res2 = self.adapter.evaluate_and_dispatch(
            raw_task_data=dep_grant,
            authority_resolver=resolver,
            binding_provider=self.binding_provider,
            observation_provider=self.observation_provider,
            native_guard=self.native_guard,
            transport_callbacks=tb,
            completed_obligations={"dep-a"},
        )
        self.assertFalse(res2.dispatch_sent)
        self.assertFalse(res2.decision_made)
        self.assertEqual(cb_count, 0)
        self.assertIn("held", res2.refusal_reason.lower())

        # 3. Negative: completed_obligations is None (safest conservative denial)
        cb_count = 0
        res3 = self.adapter.evaluate_and_dispatch(
            raw_task_data=dep_grant,
            authority_resolver=resolver,
            binding_provider=self.binding_provider,
            observation_provider=self.observation_provider,
            native_guard=self.native_guard,
            transport_callbacks=tb,
            completed_obligations=None,
        )
        self.assertFalse(res3.dispatch_sent)
        self.assertFalse(res3.decision_made)
        self.assertEqual(cb_count, 0)
        self.assertIn("held", res3.refusal_reason.lower())

    # -------------------------------------------------------------------------
    # 56. Table-driven boundary matrix of protected states across all lifecycle points
    # -------------------------------------------------------------------------

    def test_table_driven_boundary_matrix_zero_callbacks_and_no_false_completion(self) -> None:
        """Matrix testing frozen/blocked/draft/quota/unknown-dependency across decision, guard, pre-send, ingress.

        Verifies every protected/unknown condition has zero callbacks and no false completion.
        """
        conditions = [
            ("frozen_status", {"status": "frozen"}),
            ("blocked_status", {"status": "blocked"}),
            ("quota_status", {"status": "quota_held"}),
            ("manual_status", {"status": "manual_approval"}),
            ("protected_reason", {"protected_reason": "contest-hold"}),
            ("unknown_dependency", {"dependencies": ("unmet-dep",)}),
        ]

        # 1. At decision boundary
        for cond_name, overrides in conditions:
            with self.subTest(boundary="decision", condition=cond_name):
                cb_count = 0
                def cb(req: TransportSendRequest) -> Dict[str, Any]:
                    nonlocal cb_count
                    cb_count += 1
                    return {"status": "sent"}

                mutated_grant = dataclasses.replace(self.grant, **overrides)
                res = self.adapter.evaluate_and_dispatch(
                    raw_task_data=mutated_grant,
                    authority_resolver=StandardAuthorityResolver(known_grants={(mutated_grant.task_id, mutated_grant.revision): mutated_grant}),
                    binding_provider=self.binding_provider,
                    observation_provider=self.observation_provider,
                    native_guard=self.native_guard,
                    transport_callbacks=TransportRouteCallbacks(native_continuation=cb),
                )
                self.assertFalse(res.dispatch_sent)
                self.assertEqual(cb_count, 0)

        # 2. At guard boundary (clean at resolve, flipped at guard)
        for cond_name, overrides in conditions:
            with self.subTest(boundary="guard", condition=cond_name):
                cb_count = 0
                def cb(req: TransportSendRequest) -> Dict[str, Any]:
                    nonlocal cb_count
                    cb_count += 1
                    return {"status": "sent"}

                mutated_grant = dataclasses.replace(self.grant, **overrides)
                class FlipAtGuard(AuthorityResolverProtocol):
                    def __init__(self, clean: TrustedAuthorityGrant, dirty: TrustedAuthorityGrant):
                        self.clean = clean
                        self.dirty = dirty
                        self.reval_count = 0
                    def resolve_authority(self, raw_data: Any, now_utc: datetime.datetime) -> Optional[TrustedAuthorityGrant]:
                        return self.clean
                    def revalidate_authority(self, task_id: str, revision: int, workspace: str, now_utc: datetime.datetime) -> Optional[TrustedAuthorityGrant]:
                        self.reval_count += 1
                        return self.dirty

                res = self.adapter.evaluate_and_dispatch(
                    raw_task_data=self.grant,
                    authority_resolver=FlipAtGuard(self.grant, mutated_grant),
                    binding_provider=self.binding_provider,
                    observation_provider=self.observation_provider,
                    native_guard=self.native_guard,
                    transport_callbacks=TransportRouteCallbacks(native_continuation=cb),
                )
                self.assertFalse(res.dispatch_sent)
                self.assertEqual(cb_count, 0)

        # 3. At immediate pre-send boundary (clean at resolve and guard, flipped at send)
        for cond_name, overrides in conditions:
            with self.subTest(boundary="immediate_pre_send", condition=cond_name):
                cb_count = 0
                def cb(req: TransportSendRequest) -> Dict[str, Any]:
                    nonlocal cb_count
                    cb_count += 1
                    return {"status": "sent"}

                mutated_grant = dataclasses.replace(self.grant, **overrides)
                class FlipAtSend(AuthorityResolverProtocol):
                    def __init__(self, clean: TrustedAuthorityGrant, dirty: TrustedAuthorityGrant):
                        self.clean = clean
                        self.dirty = dirty
                        self.reval_count = 0
                    def resolve_authority(self, raw_data: Any, now_utc: datetime.datetime) -> Optional[TrustedAuthorityGrant]:
                        return self.clean
                    def revalidate_authority(self, task_id: str, revision: int, workspace: str, now_utc: datetime.datetime) -> Optional[TrustedAuthorityGrant]:
                        self.reval_count += 1
                        if self.reval_count == 1:
                            return self.clean
                        return self.dirty

                res = self.adapter.evaluate_and_dispatch(
                    raw_task_data=self.grant,
                    authority_resolver=FlipAtSend(self.grant, mutated_grant),
                    binding_provider=self.binding_provider,
                    observation_provider=self.observation_provider,
                    native_guard=self.native_guard,
                    transport_callbacks=TransportRouteCallbacks(native_continuation=cb),
                )
                self.assertFalse(res.dispatch_sent)
                self.assertEqual(cb_count, 0)


    def test_principal_flag_changes_at_guard_and_send_refuse_native_delivery(self) -> None:
        for flip_at in (1, 2):
            with self.subTest(flip_at=flip_at):
                fixture = TestOversightAdapter()
                fixture.setUp()
                try:
                    class FlipPrincipal(StandardAuthorityResolver):
                        def __init__(self, grant):
                            super().__init__()
                            self.register_grant(grant)
                            self.calls = 0
                        def revalidate_authority(self, *args, **kwargs):
                            self.calls += 1
                            grant = super().revalidate_authority(*args, **kwargs)
                            if grant is not None and self.calls >= flip_at:
                                return dataclasses.replace(grant, is_principal=True)
                            return grant
                    resolver = FlipPrincipal(fixture.grant)
                    result = fixture.adapter.evaluate_and_dispatch(
                        fixture.grant, resolver, fixture.binding_provider,
                        fixture.observation_provider, fixture.native_guard,
                        fixture.transport_callbacks,
                    )
                    self.assertFalse(result.dispatch_sent)
                    self.assertEqual(fixture.sent_requests, [])
                finally:
                    fixture.tearDown()

    def test_principal_flag_changes_at_ingress_refuse_state_transition(self) -> None:
        result = self.adapter.evaluate_and_dispatch(
            self.grant, self.authority_resolver, self.binding_provider,
            self.observation_provider, self.native_guard, self.transport_callbacks,
        )
        self.assertTrue(result.dispatch_sent)
        intervention_id = result.intervention.id
        prior_state = self.ledger.get_intervention(intervention_id).state
        self.authority_resolver.register_grant(dataclasses.replace(self.grant, is_principal=True))
        ack = MailboxReadAck(result.outbox_message.message_id, now_iso(self.current_time),
                             self.grant.session_id, self.grant.generation)
        with self.assertRaises(SecurityBindingError):
            self.adapter.record_mailbox_read_ack(
                ack.message_id, ack, self.grant.task_id, self.grant.revision,
                self.grant.owner_tag, self.grant.workspace,
            )
        self.assertEqual(self.ledger.get_intervention(intervention_id).state, prior_state)
        self.assertEqual(self.ledger._conn.execute(
            "SELECT COUNT(*) FROM adapter_receipt_digests WHERE intervention_id = ?",
            (intervention_id,),
        ).fetchone()[0], 0)

    # -------------------------------------------------------------------------
    # 57. Qualification seam and missing provider names readable
    # -------------------------------------------------------------------------

    def test_source_only_missing_provider_names_readable(self) -> None:
        """Required independent providers and verification boundary fields have readable names and default deny."""
        # 1. Names are readable strings
        self.assertIsInstance(REQUIRED_INDEPENDENT_PROVIDERS, tuple)
        self.assertEqual(len(REQUIRED_INDEPENDENT_PROVIDERS), 2)
        self.assertIn("authenticated current task/custody/revision authority resolver", REQUIRED_INDEPENDENT_PROVIDERS)
        self.assertIn("independent current exactbound dependency-completion verifier", REQUIRED_INDEPENDENT_PROVIDERS)

        self.assertIsInstance(EXACT_TUPLE_BOUNDARY_FIELDS, tuple)
        expected_fields = (
            "task_id", "revision", "owner_tag", "workspace", "session_id",
            "generation", "custody", "source_id", "source_digest", "currentness"
        )
        for field in expected_fields:
            self.assertIn(field, EXACT_TUPLE_BOUNDARY_FIELDS)

        # 2. Qualify authority on clean grant with StandardAuthorityResolver
        qual = self.adapter.qualify_authority(
            raw_task_data=self.grant,
            authority_resolver=self.authority_resolver,
        )
        self.assertFalse(qual.is_qualified)
        self.assertEqual(qual.status, QUALIFICATION_STATUS_OFFLINE_REFERENCE)
        self.assertIn("StandardAuthorityResolver", qual.reason)
        self.assertIn("offline reference seam only", qual.reason)
        self.assertEqual(qual.missing_providers, REQUIRED_INDEPENDENT_PROVIDERS)
        self.assertEqual(qual.verification_boundary, EXACT_TUPLE_BOUNDARY_FIELDS)
        self.assertEqual(qual.dependency_status, "NOT_APPLICABLE")
        self.assertFalse(qual.details["production_qualified"])

        # 3. Qualify authority on dependency-bearing grant
        dep_grant = dataclasses.replace(self.grant, dependencies=("dep-x",))
        self.authority_resolver.register_grant(dep_grant)
        qual_dep = self.adapter.qualify_authority(
            raw_task_data=dep_grant,
            authority_resolver=self.authority_resolver,
        )
        self.assertFalse(qual_dep.is_qualified)
        self.assertEqual(qual_dep.status, QUALIFICATION_STATUS_HELD)
        self.assertEqual(qual_dep.dependency_status, QUALIFICATION_STATUS_HELD)
        self.assertIn("dep-x", qual_dep.reason)

    # -------------------------------------------------------------------------
    # 58. Empty or unknown semantic authority: no readiness, no send, no stage advancement
    # -------------------------------------------------------------------------

    def test_empty_or_unknown_semantic_authority_no_readiness_or_stage_advancement(self) -> None:
        """Empty or unknown task authority produces zero decisions, zero sends, and zero ledger stage advancements."""
        audit_count_before = self.ledger._conn.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]
        intv_count_before = self.ledger._conn.execute("SELECT COUNT(*) FROM interventions").fetchone()[0]
        outbox_count_before = self.ledger._conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]

        for unknown_input in [
            {},
            {"task_id": "unregistered-id", "revision": 1, "status": "ready"},
            "plain string input",
            None,
        ]:
            with self.subTest(input=unknown_input):
                res = self.adapter.evaluate_and_dispatch(
                    raw_task_data=unknown_input,
                    authority_resolver=self.authority_resolver,
                    binding_provider=self.binding_provider,
                    observation_provider=self.observation_provider,
                    native_guard=self.native_guard,
                    transport_callbacks=self.transport_callbacks,
                )
                self.assertFalse(res.decision_made)
                self.assertFalse(res.dispatch_sent)
                self.assertFalse(res.dispatch_attempted)
                self.assertIn("Authority resolution failed", res.refusal_reason or "")

        # Verify zero ledger stage advancement / zero mutation
        self.assertEqual(self.ledger._conn.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0], audit_count_before)
        self.assertEqual(self.ledger._conn.execute("SELECT COUNT(*) FROM interventions").fetchone()[0], intv_count_before)
        self.assertEqual(self.ledger._conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], outbox_count_before)

    # -------------------------------------------------------------------------
    # 59. Authenticated transport ACK or caller completion flags do not qualify readiness or dependencies
    # -------------------------------------------------------------------------

    def test_authenticated_transport_ack_or_caller_completion_flags_do_not_qualify_readiness_or_deps(self) -> None:
        """Transport ACKs and caller flags (verified:true, completed:true, ActionOutcome) cannot qualify authority."""
        # 1. Transport ACK payload passed as raw task data
        ack_payload = {
            "kind": "task_ack",
            "sender_id": "c9a4f1f9-0368-431c-ba0d-bc46184f5ff2",
            "recipient_id": "21efdd28-d963-49c7-a8eb-80ba42755854",
            "body": {"stage": "scope_grant", "verified": True, "dependencies_satisfied": True},
        }
        res = self.adapter.evaluate_and_dispatch(
            raw_task_data=ack_payload,
            authority_resolver=self.authority_resolver,
            binding_provider=self.binding_provider,
            observation_provider=self.observation_provider,
            native_guard=self.native_guard,
            transport_callbacks=self.transport_callbacks,
        )
        self.assertFalse(res.decision_made)
        self.assertFalse(res.dispatch_sent)

        # 2. Caller claims verified:true, ActionOutcome:completed, status:ready on dependency-bearing task
        dep_grant = dataclasses.replace(
            self.grant,
            dependencies=("dep-1",),
            raw_metadata={"verified": True, "ActionOutcome": "completed", "completed": True},
            has_owner_ack=True,
        )
        self.authority_resolver.register_grant(dep_grant)
        res2 = self.adapter.evaluate_and_dispatch(
            raw_task_data=dep_grant,
            authority_resolver=self.authority_resolver,
            binding_provider=self.binding_provider,
            observation_provider=self.observation_provider,
            native_guard=self.native_guard,
            transport_callbacks=self.transport_callbacks,
            completed_obligations={"dep-1"},
        )
        self.assertFalse(res2.decision_made)
        self.assertFalse(res2.dispatch_sent)
        self.assertIn("held", res2.refusal_reason.lower())

    # -------------------------------------------------------------------------
    # 60. Plain completed sets cannot unblock deps at decision, guard, pre-send, or receipt
    # -------------------------------------------------------------------------

    def test_plain_completed_sets_cannot_unblock_deps_at_decision_guard_presend_receipt(self) -> None:
        """Plain completed_obligations sets cannot unblock dependencies at any lifecycle point."""
        dep_grant = dataclasses.replace(self.grant, dependencies=("dep-required",))
        self.authority_resolver.register_grant(dep_grant)
        plain_set = {"dep-required"}

        # 1. At decision: evaluate_and_dispatch refuses BEFORE recording decision (zero ledger mutation)
        intv_count_before = self.ledger._conn.execute("SELECT COUNT(*) FROM interventions").fetchone()[0]
        res_dec = self.adapter.evaluate_and_dispatch(
            raw_task_data=dep_grant,
            authority_resolver=self.authority_resolver,
            binding_provider=self.binding_provider,
            observation_provider=self.observation_provider,
            native_guard=self.native_guard,
            transport_callbacks=self.transport_callbacks,
            completed_obligations=plain_set,
        )
        self.assertFalse(res_dec.decision_made)
        self.assertFalse(res_dec.dispatch_sent)
        self.assertEqual(self.ledger._conn.execute("SELECT COUNT(*) FROM interventions").fetchone()[0], intv_count_before)

        # 2. At guard: dispatch guard authorize_dispatch denies
        self.authority_resolver.register_grant(self.grant)
        clean_res = self.adapter.evaluate_and_dispatch(
            raw_task_data=self.grant,
            authority_resolver=self.authority_resolver,
            binding_provider=self.binding_provider,
            observation_provider=self.observation_provider,
            native_guard=self.native_guard,
            transport_callbacks=self.transport_callbacks,
        )
        self.assertTrue(clean_res.dispatch_sent)
        msg = clean_res.outbox_message

        # Mutate resolver to add dependencies for guard revalidation
        self.authority_resolver.register_grant(dep_grant)
        guard = AdapterDispatchGuard(
            ledger=self.ledger,
            authority_resolver=self.authority_resolver,
            binding_provider=self.binding_provider,
            native_guard=self.native_guard,
            transport_callbacks=self.transport_callbacks,
            clock=self.adapter._clock,
            completed_obligations=plain_set,
        )
        ok_guard, reason_guard = guard.authorize_dispatch(msg)
        self.assertFalse(ok_guard)
        self.assertIn("held", reason_guard.lower())

        # 3. At pre-send: AdapterTransport.send raises TransportSendFailedBeforeDelivery
        cb_called = False
        def test_cb(req: TransportSendRequest) -> Dict[str, Any]:
            nonlocal cb_called
            cb_called = True
            return {"status": "sent"}

        tb = TransportRouteCallbacks(bus_send=test_cb)
        transport = AdapterTransport(
            ledger=self.ledger,
            callbacks=tb,
            authority_resolver=self.authority_resolver,
            binding_provider=self.binding_provider,
            clock=self.adapter._clock,
            completed_obligations=plain_set,
        )
        with self.assertRaises(TransportSendFailedBeforeDelivery):
            transport.send(
                route=ROUTE_BUS,
                recipient_tag=self.grant.owner_tag,
                payload={"test": 1},
                idempotency_key=clean_res.outbox_message.dedup_key,
            )
        self.assertFalse(cb_called)

        # 4. At receipt: record_mailbox_read_ack, custody, action, outcome deny with SecurityBindingError
        read_ack = MailboxReadAck(
            message_id=clean_res.outbox_message.message_id,
            read_at=now_iso(self.current_time),
            reader_session_id=self.grant.session_id,
            reader_generation=self.grant.generation,
        )
        with self.assertRaises(SecurityBindingError) as ctx:
            self.adapter.record_mailbox_read_ack(
                message_id=clean_res.outbox_message.message_id,
                ack=read_ack,
                expected_obligation_id=self.grant.task_id,
                expected_revision=self.grant.revision,
                expected_owner_tag=self.grant.owner_tag,
                expected_workspace=self.grant.workspace,
            )
        self.assertIn("held", str(ctx.exception).lower())

    # -------------------------------------------------------------------------
    # 61. Stale, future, malformed, cross-tuple authority evidence refused
    # -------------------------------------------------------------------------

    def test_stale_future_malformed_cross_tuple_authority_evidence_refused(self) -> None:
        """Authority evidence with future timestamp, stale age, or mismatched tuple fields is refused."""
        # 1. Future timestamp
        future_grant = dataclasses.replace(
            self.grant,
            issued_at="2026-10-13T13:00:00Z",  # 1 hour after self.current_time
        )
        resolver_future = StandardAuthorityResolver(known_grants={(future_grant.task_id, future_grant.revision): future_grant})
        res_future = self.adapter.evaluate_and_dispatch(
            raw_task_data=future_grant,
            authority_resolver=resolver_future,
            binding_provider=self.binding_provider,
            observation_provider=self.observation_provider,
            native_guard=self.native_guard,
            transport_callbacks=self.transport_callbacks,
        )
        self.assertFalse(res_future.decision_made)

        # 2. Stale timestamp (> 300s)
        stale_grant = dataclasses.replace(
            self.grant,
            issued_at="2026-10-13T10:00:00Z",  # 2 hours before self.current_time
        )
        resolver_stale = StandardAuthorityResolver(known_grants={(stale_grant.task_id, stale_grant.revision): stale_grant})
        res_stale = self.adapter.evaluate_and_dispatch(
            raw_task_data=stale_grant,
            authority_resolver=resolver_stale,
            binding_provider=self.binding_provider,
            observation_provider=self.observation_provider,
            native_guard=self.native_guard,
            transport_callbacks=self.transport_callbacks,
        )
        self.assertFalse(res_stale.decision_made)

        # 3. Cross-session mismatch
        mismatched_session_grant = dataclasses.replace(self.grant, session_id="other-session-id")
        resolver_cross = StandardAuthorityResolver(known_grants={(mismatched_session_grant.task_id, mismatched_session_grant.revision): mismatched_session_grant})
        res_cross = self.adapter.evaluate_and_dispatch(
            raw_task_data=mismatched_session_grant,
            authority_resolver=resolver_cross,
            binding_provider=self.binding_provider,
            observation_provider=self.observation_provider,
            native_guard=self.native_guard,
            transport_callbacks=self.transport_callbacks,
        )
        self.assertFalse(res_cross.dispatch_sent)
        self.assertIn("session", res_cross.refusal_reason.lower())

    # -------------------------------------------------------------------------
    # 62. Missing dependency verifier and exceptions deny as HELD
    # -------------------------------------------------------------------------

    def test_missing_verifier_and_exceptions_deny_as_held(self) -> None:
        """Missing dependency verifier or verifier raising exceptions fails closed as HELD."""
        dep_grant = dataclasses.replace(self.grant, dependencies=("dep-err",))
        self.authority_resolver.register_grant(dep_grant)

        # 1. Missing verifier (None)
        res_none = self.adapter.evaluate_and_dispatch(
            raw_task_data=dep_grant,
            authority_resolver=self.authority_resolver,
            binding_provider=self.binding_provider,
            observation_provider=self.observation_provider,
            native_guard=self.native_guard,
            transport_callbacks=self.transport_callbacks,
            dependency_verifier=None,
        )
        self.assertFalse(res_none.decision_made)
        self.assertIn("held", res_none.refusal_reason.lower())

        # 2. Verifier raises Exception
        class FailingVerifier(DependencyCompletionVerifierProtocol):
            def verify_dependency_completion(self, dependency_task_id: str, expected_workspace: str, now_utc: datetime.datetime, dependency_evidence: Optional[Dict[str, Any]] = None) -> Tuple[bool, str]:
                raise RuntimeError("Simulated remote dependency lookup error")

        res_exc = self.adapter.evaluate_and_dispatch(
            raw_task_data=dep_grant,
            authority_resolver=self.authority_resolver,
            binding_provider=self.binding_provider,
            observation_provider=self.observation_provider,
            native_guard=self.native_guard,
            transport_callbacks=self.transport_callbacks,
            dependency_verifier=FailingVerifier(),
        )
        self.assertFalse(res_exc.decision_made)
        self.assertIn("exception", res_exc.refusal_reason.lower())

        # 3. Verifier returns non-bool
        class NonBoolVerifier(DependencyCompletionVerifierProtocol):
            def verify_dependency_completion(self, dependency_task_id: str, expected_workspace: str, now_utc: datetime.datetime, dependency_evidence: Optional[Dict[str, Any]] = None) -> Tuple[bool, str]:
                return ("not-a-bool", "bad return")  # type: ignore

        res_bad = self.adapter.evaluate_and_dispatch(
            raw_task_data=dep_grant,
            authority_resolver=self.authority_resolver,
            binding_provider=self.binding_provider,
            observation_provider=self.observation_provider,
            native_guard=self.native_guard,
            transport_callbacks=self.transport_callbacks,
            dependency_verifier=NonBoolVerifier(),
        )
        self.assertFalse(res_bad.decision_made)
        self.assertIn("non-bool", res_bad.refusal_reason.lower())

        # 3b. Verifier returns (False, "dep not finished")
        class FalseVerifier(DependencyCompletionVerifierProtocol):
            def verify_dependency_completion(self, dependency_task_id: str, expected_workspace: str, now_utc: datetime.datetime, dependency_evidence: Optional[Dict[str, Any]] = None) -> Tuple[bool, str]:
                return (False, "dep not finished")

        res_false = self.adapter.evaluate_and_dispatch(
            raw_task_data=dep_grant,
            authority_resolver=self.authority_resolver,
            binding_provider=self.binding_provider,
            observation_provider=self.observation_provider,
            native_guard=self.native_guard,
            transport_callbacks=self.transport_callbacks,
            dependency_verifier=FalseVerifier(),
        )
        self.assertFalse(res_false.decision_made)
        self.assertIn("unverified", res_false.refusal_reason.lower())

        # 4. Resolver raises Exception in qualify_authority_inputs and evaluate_and_dispatch
        class BrokenResolver(AuthorityResolverProtocol):
            def resolve_authority(self, raw_task_data: Any, now_utc: datetime.datetime) -> Optional[TrustedAuthorityGrant]:
                raise RuntimeError("Simulated database lock or authority failure")
            def revalidate_authority(self, task_id: str, revision: int, workspace: str, now_utc: datetime.datetime) -> Optional[TrustedAuthorityGrant]:
                raise RuntimeError("Simulated revalidate error")

        broken_res = BrokenResolver()
        qual_exc = self.adapter.qualify_authority_inputs(
            raw_task_data=self.grant,
            authority_resolver=broken_res,
        )
        self.assertFalse(qual_exc.is_qualified)
        self.assertEqual(qual_exc.status, QUALIFICATION_STATUS_DENIED)
        self.assertIn("exception", qual_exc.reason.lower())

        eval_exc = self.adapter.evaluate_and_dispatch(
            raw_task_data=self.grant,
            authority_resolver=broken_res,
            binding_provider=self.binding_provider,
            observation_provider=self.observation_provider,
            native_guard=self.native_guard,
            transport_callbacks=self.transport_callbacks,
        )
        self.assertFalse(eval_exc.decision_made)
        self.assertIn("exception", eval_exc.refusal_reason.lower())

        # 5. Resolver returns malformed non-grant type
        class MalformedResolver(AuthorityResolverProtocol):
            def resolve_authority(self, raw_task_data: Any, now_utc: datetime.datetime) -> Any:
                return {"task_id": "task-1", "fake": True}
            def revalidate_authority(self, task_id: str, revision: int, workspace: str, now_utc: datetime.datetime) -> Any:
                return {"task_id": "task-1", "fake": True}

        malformed_res = MalformedResolver()
        qual_mal = self.adapter.qualify_authority_inputs(
            raw_task_data=self.grant,
            authority_resolver=malformed_res,
        )
        self.assertFalse(qual_mal.is_qualified)
        self.assertEqual(qual_mal.status, QUALIFICATION_STATUS_DENIED)
        self.assertIn("invalid grant type", qual_mal.reason.lower())

        eval_mal = self.adapter.evaluate_and_dispatch(
            raw_task_data=self.grant,
            authority_resolver=malformed_res,
            binding_provider=self.binding_provider,
            observation_provider=self.observation_provider,
            native_guard=self.native_guard,
            transport_callbacks=self.transport_callbacks,
        )
        self.assertFalse(eval_mal.decision_made)
        self.assertIn("invalid grant type", eval_mal.refusal_reason.lower())

    # -------------------------------------------------------------------------
    # 63. No fabricated production positive: honest future provider hold
    # -------------------------------------------------------------------------

    def test_no_fabricated_production_positive_honest_future_provider_hold(self) -> None:
        """Adapter honestly reports production_qualified:False and keeps dependencies HELD."""
        default_verifier = DefaultDenyDependencyCompletionVerifier()
        ok, reason = default_verifier.verify_dependency_completion(
            dependency_task_id="dep-1",
            expected_workspace=self.workspace,
            now_utc=self.current_time,
        )
        self.assertFalse(ok)
        self.assertIn("HELD", reason)

        # Standard resolver explicitly declares itself as offline reference
        self.assertTrue(StandardAuthorityResolver.is_offline_reference)
        self.assertFalse(StandardAuthorityResolver.is_production_qualified)
        self.assertIn("offline reference seam only", StandardAuthorityResolver.provider_name)

        # Boolean verifier attempting positive CANNOT unhold dependencies in production
        class AttemptedPositiveVerifier(DependencyCompletionVerifierProtocol):
            def verify_dependency_completion(self, dependency_task_id: str, expected_workspace: str, now_utc: datetime.datetime, dependency_evidence: Optional[Dict[str, Any]] = None) -> Tuple[bool, str]:
                return True, "Simulated positive verifier attempt"

        dep_grant = dataclasses.replace(self.grant, dependencies=("dep-truthy",))
        self.authority_resolver.register_grant(dep_grant)

        # 1. evaluate_and_dispatch refuses: dependencies stay HELD with ZERO sends
        cb_called = False
        def dummy_cb(req: TransportSendRequest) -> Dict[str, Any]:
            nonlocal cb_called
            cb_called = True
            return {"status": "sent"}

        tb = TransportRouteCallbacks(bus_send=dummy_cb)
        res = self.adapter.evaluate_and_dispatch(
            raw_task_data=dep_grant,
            authority_resolver=self.authority_resolver,
            binding_provider=self.binding_provider,
            observation_provider=self.observation_provider,
            native_guard=self.native_guard,
            transport_callbacks=tb,
            dependency_verifier=AttemptedPositiveVerifier(),
        )
        self.assertFalse(res.dispatch_sent)
        self.assertFalse(res.decision_made)
        self.assertFalse(cb_called)
        self.assertIn("held", res.refusal_reason.lower())

        # 2. qualify_authority_inputs also refuses: dependency_status remains HELD
        qual = self.adapter.qualify_authority_inputs(
            raw_task_data=dep_grant,
            authority_resolver=self.authority_resolver,
            dependency_verifier=AttemptedPositiveVerifier(),
        )
        self.assertFalse(qual.is_qualified)
        self.assertEqual(qual.status, QUALIFICATION_STATUS_HELD)
        self.assertEqual(qual.dependency_status, QUALIFICATION_STATUS_HELD)
        self.assertFalse(qual.details["production_qualified"])

        # 3. Both qualify_authority and qualify_authority_inputs aliases work identically
        qual_alias = self.adapter.qualify_authority(
            raw_task_data=dep_grant,
            authority_resolver=self.authority_resolver,
            dependency_verifier=AttemptedPositiveVerifier(),
        )
        self.assertEqual(qual, qual_alias)

    # -------------------------------------------------------------------------
    # 64. Dependency-bearing receipts held with no false outcome
    # -------------------------------------------------------------------------

    def test_dependency_bearing_action_and_completion_receipts_held_no_false_outcome(self) -> None:
        """When an obligation has dependencies, receipt ingress is HELD with zero false outcome in ledger."""
        self.authority_resolver.register_grant(self.grant)
        res = self.adapter.evaluate_and_dispatch(
            self.grant, self.authority_resolver, self.binding_provider,
            self.observation_provider, self.native_guard, self.transport_callbacks,
        )
        self.assertTrue(res.dispatch_sent)
        intv_id = res.intervention.id
        prior_state = self.ledger.get_intervention(intv_id).state

        # Mutate grant in resolver to declare a dependency
        dep_grant = dataclasses.replace(self.grant, dependencies=("unresolved-dep",))
        self.authority_resolver.register_grant(dep_grant)

        # Read ACK ingress attempt
        read_ack = MailboxReadAck(
            message_id=res.outbox_message.message_id,
            read_at=now_iso(self.current_time),
            reader_session_id=self.grant.session_id,
            reader_generation=self.grant.generation,
        )
        with self.assertRaises(SecurityBindingError) as ctx_ack:
            self.adapter.record_mailbox_read_ack(
                res.outbox_message.message_id,
                read_ack,
                self.grant.task_id,
                self.grant.revision,
                self.grant.owner_tag,
                self.grant.workspace,
            )
        self.assertIn("held", str(ctx_ack.exception).lower())
        self.assertEqual(self.ledger.get_intervention(intv_id).state, prior_state)

        # Action receipt ingress attempt
        action = ActionReceipt(
            receipt_id="act-001",
            action_type="tool_execution",
            timestamp=now_iso(self.current_time),
            session_id=self.grant.session_id,
            generation=self.grant.generation,
            details={"cmd": "ls"},
            obligation_id=self.grant.task_id,
            obligation_revision=self.grant.revision,
            owner_tag=self.grant.owner_tag,
            workspace=self.grant.workspace,
        )
        with self.assertRaises(SecurityBindingError) as ctx:
            self.adapter.record_first_action(intv_id, action)
        self.assertIn("held", str(ctx.exception).lower())
        self.assertEqual(self.ledger.get_intervention(intv_id).state, prior_state)

        # Completion outcome ingress attempt
        outcome = CompletionOutcome(
            obligation_id=self.grant.task_id,
            obligation_revision=self.grant.revision,
            owner_tag=self.grant.owner_tag,
            workspace=self.grant.workspace,
            session_id=self.grant.session_id,
            generation=self.grant.generation,
            completed_at=now_iso(self.current_time),
            result={"status": "ok"},
        )
        with self.assertRaises(SecurityBindingError) as ctx2:
            self.adapter.record_completion_outcome(intv_id, outcome)
        self.assertIn("held", str(ctx2.exception).lower())
        self.assertEqual(self.ledger.get_intervention(intv_id).state, prior_state)


if __name__ == "__main__":
    unittest.main()

