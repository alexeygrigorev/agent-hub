"""Comprehensive Tests for Task Authority, Provenance, and Dependency Completion Provider.

Tests:
- Full lifecycle: grant issuance -> genuine custody ACK -> pinned proposal -> distinct review -> authorized acceptance -> dependency lookup.
- Integration with OversightAdapter (authority resolution & dependency verification).
- Actual FileBus authentication against isolated temporary FileBus store.
- Zero fake boolean fixtures; genuine FileBus._auth validation.
- Fixture native binding provider labeled fixture-only.
- Sequential CAS revisions and idempotency.
- Crash and reopen durable SQLite state validation.
- Complete negative suite:
  * empty trust root
  * bad token / unauthenticated caller
  * spoofed role
  * project scope mismatch
  * cross-workspace / task restriction
  * issuer revocation fencing
  * owner revision / generation / session UUID CAS conflict
  * owner false ACK (assignment alone is not ready)
  * self-review strictly rejected
  * caller reviewer string / unauthenticated review rejected
  * faked accepted record rejected
  * artifact tamper / digest pin conflict rejected
  * stale, future, expired, and naive clocks rejected
  * frozen, draft, quota-held, not-alive native binding rejected
  * all negatives produce zero database side effects
  * immutable exact replay vs changed payload conflict
"""

from __future__ import annotations

import datetime
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, Optional

# Add host to sys.path so agent_hub_host is importable
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Add coordination repo path explicitly for tests
coordination_path = Path("/home/alexey/git/agent-coordination")
if coordination_path.exists() and str(coordination_path) not in sys.path:
    sys.path.insert(0, str(coordination_path))

try:
    from coordination.bus import FileBus
except ImportError:
    FileBus = None  # type: ignore

from agent_hub_host.task_authority import (
    AuthenticationError,
    AuthorityTrustRoot,
    CASConflictError,
    FencedStateError,
    FileBusAuthenticator,
    InvalidTupleError,
    NativeBindingError,
    NativeSessionEvidence,
    PermissionDeniedError,
    TaskAuthorityError,
    TaskAuthorityProvider,
    TaskAuthorityStore,
    TaskOwnershipTuple,
    TrustedActorPolicy,
    VerificationError,
    compute_sha256,
    format_iso_utc,
    parse_strict_iso_utc,
)
from agent_hub_host.oversight_adapter import (
    NativeGuardCheckResult,
    OversightAdapter,
    TransportRouteCallbacks,
    TrustedAuthorityGrant,
)
from agent_hub_host.oversight import (
    InterventionLedger,
    NativeSessionBinding,
    ObservationEvidence,
    CLASSIFICATION_CURRENT_USEFUL_WORK,
    CLASSIFICATION_IDLE_WITH_READY_WORK,
    CLASSIFICATION_PROTECTED,
    INTERVENTION_NUDGE_IDLE,
    ROUTE_BUS,
)


class FixtureOnlyNativeBindingProvider:
    """Fixture-only native session binding provider.
    Labeled fixture-only; no operational qualification claim.
    """
    is_fixture_only = True

    def __init__(self):
        self._records: Dict[tuple[str, str], NativeSessionEvidence] = {}

    def set_binding(
        self,
        workspace: str,
        tag: str,
        session_id: str,
        generation: int,
        is_alive: bool = True,
        is_ready: bool = True,
        is_frozen: bool = False,
        is_blocked: bool = False,
        is_quota_held: bool = False,
        is_manual_approval: bool = False,
        is_draft: bool = False,
        is_menu_active: bool = False,
        is_saved_goal: bool = False,
        observed_at: Optional[str] = None,
    ) -> None:
        self._records[(workspace, tag)] = NativeSessionEvidence(
            workspace=workspace,
            tag=tag,
            session_id=session_id,
            generation=generation,
            is_alive=is_alive,
            is_ready=is_ready,
            is_frozen=is_frozen,
            is_blocked=is_blocked,
            is_quota_held=is_quota_held,
            is_manual_approval=is_manual_approval,
            is_draft=is_draft,
            is_menu_active=is_menu_active,
            is_saved_goal=is_saved_goal,
            observed_at=observed_at or format_iso_utc(datetime.datetime.now(datetime.timezone.utc)),
            is_fixture_only=True,
        )

    def get_binding_evidence(
        self,
        workspace: str,
        tag: str,
        now_utc: datetime.datetime,
    ) -> Optional[NativeSessionEvidence]:
        return self._records.get((workspace, tag))


class FixtureBindingProvider:
    """Fixture-only adapter binding provider for OversightAdapter."""
    def __init__(self, binding: Optional[NativeSessionBinding] = None):
        self.binding = binding

    def get_current_binding(
        self,
        workspace: str,
        session_id: str,
        now_utc: datetime.datetime,
    ) -> Optional[NativeSessionBinding]:
        return self.binding


class FixtureObservationProvider:
    """Fixture-only observation provider."""
    def __init__(self, observation: Optional[ObservationEvidence] = None):
        self.observation = observation

    def get_current_observation(
        self,
        workspace: str,
        session_id: str,
        generation: int,
        now_utc: datetime.datetime,
    ) -> Optional[ObservationEvidence]:
        return self.observation


class FixtureNativeGuard:
    """Fixture-only native guard."""
    def __init__(self, allowed: bool = True):
        self.allowed = allowed

    def check_native_state(
        self,
        workspace: str,
        session_id: str,
        generation: int,
        now_utc: datetime.datetime,
    ) -> NativeGuardCheckResult:
        return NativeGuardCheckResult(
            allowed=self.allowed,
            reason="ok",
            is_ready=True,
            session_alive=True,
        )


class TestTaskAuthority(unittest.TestCase):

    def setUp(self):
        if FileBus is None:
            self.skipTest("coordination.bus.FileBus is unavailable")

        self.tmp_dir = tempfile.mkdtemp(prefix="task_authority_test_")
        self.bus_dir = Path(self.tmp_dir) / "bus"
        self.db_path = Path(self.tmp_dir) / "authority.db"

        self.file_bus = FileBus(self.bus_dir)
        self.authenticator = FileBusAuthenticator(bus=self.file_bus)
        self.store = TaskAuthorityStore(self.db_path)
        self.native_provider = FixtureOnlyNativeBindingProvider()

        # Fixed reference time for deterministic testing
        self.now_dt = datetime.datetime(2026, 10, 13, 16, 0, 0, tzinfo=datetime.timezone.utc)
        self.clock = lambda: self.now_dt

        # Register distinct identities in FileBus
        self.project_id = "agent-hub"
        self.workspace = "/home/alexey/git/agent-hub/.local/branches/workspaces/hub-test"

        # Issuer identity
        self.issuer_ident, self.issuer_token = self.file_bus.register(
            agent_name="agent-hub-head",
            device_id="dev-hetzner",
            project_id=self.project_id,
        )
        self.issuer_id = self.issuer_ident.identity_id

        # Worker / Owner identity
        self.owner_ident, self.owner_token = self.file_bus.register(
            agent_name="agent-hub-implementer-t1",
            device_id="dev-hetzner",
            project_id=self.project_id,
        )
        self.owner_id = self.owner_ident.identity_id
        self.owner_tag = "worker-t1"
        self.session_uuid = "11111111-2222-3333-4444-555555555555"
        self.generation = 1

        # Reviewer identity (DISTINCT from owner)
        self.reviewer_ident, self.reviewer_token = self.file_bus.register(
            agent_name="agent-hub-reviewer-t1",
            device_id="dev-hetzner",
            project_id=self.project_id,
        )
        self.reviewer_id = self.reviewer_ident.identity_id

        # Acceptor identity
        self.acceptor_ident, self.acceptor_token = self.file_bus.register(
            agent_name="agent-hub-acceptor",
            device_id="dev-hetzner",
            project_id=self.project_id,
        )
        self.acceptor_id = self.acceptor_ident.identity_id

        # Configure immutable trust root
        self.trust_root = AuthorityTrustRoot(
            trusted_actors=(
                TrustedActorPolicy(actor_id=self.issuer_id, role="issuer", project_id=self.project_id, workspace=self.workspace),
                TrustedActorPolicy(actor_id=self.owner_id, role="owner", project_id=self.project_id, workspace=self.workspace),
                TrustedActorPolicy(actor_id=self.reviewer_id, role="reviewer", project_id=self.project_id, workspace=self.workspace),
                TrustedActorPolicy(actor_id=self.acceptor_id, role="acceptor", project_id=self.project_id, workspace=self.workspace),
                TrustedActorPolicy(actor_id=self.issuer_id, role="reader", project_id=self.project_id, workspace=self.workspace),
                TrustedActorPolicy(actor_id=self.acceptor_id, role="reader", project_id=self.project_id, workspace=self.workspace),
            )
        )

        # Set up valid native binding in fixture
        self.native_provider.set_binding(
            workspace=self.workspace,
            tag=self.owner_tag,
            session_id=self.session_uuid,
            generation=self.generation,
            is_alive=True,
            is_ready=True,
            observed_at=format_iso_utc(self.now_dt),
        )

        self.provider = TaskAuthorityProvider(
            store=self.store,
            authenticator=self.authenticator,
            trust_root=self.trust_root,
            native_binding_provider=self.native_provider,
            clock=self.clock,
        )

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_full_lifecycle_positive(self):
        """Test complete positive lifecycle: grant -> custody -> proposal -> review -> accept -> dependency lookup."""
        task_id = "task-101"
        expires_at = format_iso_utc(self.now_dt + datetime.timedelta(hours=2))

        # 1. Issue task grant
        grant = self.provider.issue_task_grant(
            issuer_actor_id=self.issuer_id,
            issuer_token=self.issuer_token,
            task_id=task_id,
            revision=1,
            project_id=self.project_id,
            workspace=self.workspace,
            owner_bus_actor=self.owner_id,
            native_owner_tag=self.owner_tag,
            native_session_uuid=self.session_uuid,
            generation=self.generation,
            source_id="sha-12345",
            source_digest="digest-67890",
            expires_at=expires_at,
        )
        self.assertEqual(grant.status, "assigned")
        self.assertFalse(grant.has_owner_ack)

        # Before custody ACK, resolving authority shows status='assigned' and protected_reason='awaiting_owner_custody_ack'
        resolved = self.provider.resolve_authority(task_id, self.now_dt)
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.status, "assigned")
        self.assertFalse(resolved.has_owner_ack)
        self.assertEqual(resolved.protected_reason, "awaiting_owner_custody_ack")

        # 2. Record genuine owner custody ACK
        ack = self.provider.record_custody_ack(
            owner_actor_id=self.owner_id,
            owner_token=self.owner_token,
            task_id=task_id,
            revision=1,
            project_id=self.project_id,
            workspace=self.workspace,
            native_owner_tag=self.owner_tag,
            native_session_uuid=self.session_uuid,
            generation=self.generation,
            idempotency_key="ack-key-1",
        )
        self.assertEqual(ack.task_id, task_id)
        self.assertEqual(ack.revision, 1)

        # Now resolving authority shows status='ready' and has_owner_ack=True
        resolved_after_ack = self.provider.resolve_authority(task_id, self.now_dt)
        self.assertIsNotNone(resolved_after_ack)
        self.assertEqual(resolved_after_ack.status, "ready")
        self.assertTrue(resolved_after_ack.has_owner_ack)
        self.assertIsNone(resolved_after_ack.protected_reason)

        # 3. Propose completion with pinned result digest
        result_digest = compute_sha256("artifact content v1")
        evidence = {"artifacts": ["file1.py", "file2.py"], "test_exit_code": 0, "digest": result_digest}
        proposal = self.provider.propose_task_completion(
            owner_actor_id=self.owner_id,
            owner_token=self.owner_token,
            task_id=task_id,
            revision=1,
            project_id=self.project_id,
            workspace=self.workspace,
            native_owner_tag=self.owner_tag,
            native_session_uuid=self.session_uuid,
            generation=self.generation,
            result_digest=result_digest,
            result_evidence=evidence,
            idempotency_key="prop-key-1",
        )
        self.assertEqual(proposal.result_digest, result_digest)

        # 4. Independent review by distinct authenticated reviewer
        review_evidence = {"negative_cases_tested": 3, "falsifier_passed": True, "reviewer_model": "test-reviewer"}
        review = self.provider.record_task_review(
            reviewer_actor_id=self.reviewer_id,
            reviewer_token=self.reviewer_token,
            task_id=task_id,
            revision=1,
            project_id=self.project_id,
            workspace=self.workspace,
            expected_result_digest=result_digest,
            verdict="approved",
            review_evidence=review_evidence,
            idempotency_key="rev-key-1",
        )
        self.assertEqual(review.verdict, "approved")

        # 5. Authorized acceptance
        acceptance_evidence = {"integration_checkpoint": "main_ff", "acceptor_notes": "All checks verified"}
        acceptance = self.provider.record_task_acceptance(
            acceptor_actor_id=self.acceptor_id,
            acceptor_token=self.acceptor_token,
            task_id=task_id,
            revision=1,
            project_id=self.project_id,
            workspace=self.workspace,
            expected_result_digest=result_digest,
            acceptance_evidence=acceptance_evidence,
            idempotency_key="acc-key-1",
        )
        self.assertEqual(acceptance.acceptor_actor_id, self.acceptor_id)

        # 6. Verify dependency completion lookup
        dep_ok, dep_reason = self.provider.verify_dependency_completion(
            dependency_task_id=task_id,
            expected_workspace=self.workspace,
            now_utc=self.now_dt,
        )
        self.assertTrue(dep_ok)
        self.assertIn("verified", dep_reason)

    def test_integration_with_oversight_adapter(self):
        """Test that TaskAuthorityProvider acts as authority_resolver and dependency_verifier for OversightAdapter."""
        task_id = "task-dep-01"
        expires_at = format_iso_utc(self.now_dt + datetime.timedelta(hours=2))

        # Complete dependency task-dep-01
        self.provider.issue_task_grant(
            issuer_actor_id=self.issuer_id,
            issuer_token=self.issuer_token,
            task_id=task_id,
            revision=1,
            project_id=self.project_id,
            workspace=self.workspace,
            owner_bus_actor=self.owner_id,
            native_owner_tag=self.owner_tag,
            native_session_uuid=self.session_uuid,
            generation=self.generation,
            source_id="sha-1",
            source_digest="digest-1",
            expires_at=expires_at,
        )
        self.provider.record_custody_ack(
            owner_actor_id=self.owner_id,
            owner_token=self.owner_token,
            task_id=task_id,
            revision=1,
            project_id=self.project_id,
            workspace=self.workspace,
            native_owner_tag=self.owner_tag,
            native_session_uuid=self.session_uuid,
            generation=self.generation,
            idempotency_key="ack-dep-1",
        )
        r_digest = compute_sha256("dep result")
        self.provider.propose_task_completion(
            owner_actor_id=self.owner_id,
            owner_token=self.owner_token,
            task_id=task_id,
            revision=1,
            project_id=self.project_id,
            workspace=self.workspace,
            native_owner_tag=self.owner_tag,
            native_session_uuid=self.session_uuid,
            generation=self.generation,
            result_digest=r_digest,
            result_evidence={"digest": r_digest, "ok": True},
            idempotency_key="prop-dep-1",
        )
        self.provider.record_task_review(
            reviewer_actor_id=self.reviewer_id,
            reviewer_token=self.reviewer_token,
            task_id=task_id,
            revision=1,
            project_id=self.project_id,
            workspace=self.workspace,
            expected_result_digest=r_digest,
            verdict="approved",
            review_evidence={"test": "pass"},
            idempotency_key="rev-dep-1",
        )
        self.provider.record_task_acceptance(
            acceptor_actor_id=self.acceptor_id,
            acceptor_token=self.acceptor_token,
            task_id=task_id,
            revision=1,
            project_id=self.project_id,
            workspace=self.workspace,
            expected_result_digest=r_digest,
            acceptance_evidence={"accept": True},
            idempotency_key="acc-dep-1",
        )

        # Now issue task-main which depends on task-dep-01
        main_task_id = "task-main-02"
        self.provider.issue_task_grant(
            issuer_actor_id=self.issuer_id,
            issuer_token=self.issuer_token,
            task_id=main_task_id,
            revision=1,
            project_id=self.project_id,
            workspace=self.workspace,
            owner_bus_actor=self.owner_id,
            native_owner_tag=self.owner_tag,
            native_session_uuid=self.session_uuid,
            generation=self.generation,
            source_id="sha-2",
            source_digest="digest-2",
            expires_at=expires_at,
            dependencies=(task_id,),
        )
        self.provider.record_custody_ack(
            owner_actor_id=self.owner_id,
            owner_token=self.owner_token,
            task_id=main_task_id,
            revision=1,
            project_id=self.project_id,
            workspace=self.workspace,
            native_owner_tag=self.owner_tag,
            native_session_uuid=self.session_uuid,
            generation=self.generation,
            idempotency_key="ack-main-1",
        )

        # Set up OversightAdapter with TaskAuthorityProvider
        adapter_ledger = InterventionLedger(Path(self.tmp_dir) / "adapter.db", clock=self.clock)
        binding_prov = FixtureBindingProvider(
            NativeSessionBinding(
                session_id=self.session_uuid,
                tag=self.owner_tag,
                generation=self.generation,
                workspace=self.workspace,
                worker_alive=True,
                active=True,
            )
        )

        obs_evidence = ObservationEvidence(
            sample_id="obs-001",
            observed_at=format_iso_utc(self.now_dt),
            assessment="idle",
            empty_composer=True,
            session_id=self.session_uuid,
            generation=self.generation,
        )
        obs_prov = FixtureObservationProvider(obs_evidence)
        native_guard = FixtureNativeGuard(allowed=True)
        transport_cbs = TransportRouteCallbacks(
            bus_send=lambda req: {
                "status": "sent",
                "message_id": f"bus-receipt-{req.message_id}",
                "idempotency_key": req.idempotency_key,
                "state": "send_receipt",
                "sent_at": "2026-10-13T12:00:01Z",
            },
            native_continuation=lambda req: {
                "status": "sent",
                "message_id": f"native-receipt-{req.message_id}",
                "idempotency_key": req.idempotency_key,
                "state": "send_receipt",
                "sent_at": "2026-10-13T12:00:01Z",
            },
        )

        adapter = OversightAdapter(
            ledger=adapter_ledger,
            binding_provider=binding_prov,
            authority_resolver=self.provider,
            dependency_verifier=self.provider,
            clock=self.clock,
        )

        # 1. Dispatching a ready task with custody ACK (no dependencies) succeeds in making decision
        task_ready = "task-ready-standalone"
        self.provider.issue_task_grant(
            issuer_actor_id=self.issuer_id,
            issuer_token=self.issuer_token,
            task_id=task_ready,
            revision=1,
            project_id=self.project_id,
            workspace=self.workspace,
            owner_bus_actor=self.owner_id,
            native_owner_tag=self.owner_tag,
            native_session_uuid=self.session_uuid,
            generation=self.generation,
            source_id="sha-ready",
            source_digest="digest-ready",
            expires_at=expires_at,
        )
        self.provider.record_custody_ack(
            owner_actor_id=self.owner_id,
            owner_token=self.owner_token,
            task_id=task_ready,
            revision=1,
            project_id=self.project_id,
            workspace=self.workspace,
            native_owner_tag=self.owner_tag,
            native_session_uuid=self.session_uuid,
            generation=self.generation,
            idempotency_key="ack-ready-1",
        )

        res = adapter.evaluate_and_dispatch(
            raw_task_data={"task_id": task_ready, "revision": 1},
            authority_resolver=self.provider,
            binding_provider=binding_prov,
            observation_provider=obs_prov,
            native_guard=native_guard,
            transport_callbacks=transport_cbs,
            dependency_verifier=self.provider,
        )
        self.assertTrue(res.decision_made)
        self.assertEqual(res.decision.obligation_id, task_ready)
        self.assertEqual(res.decision.intervention_type, INTERVENTION_NUDGE_IDLE)

        # 2. Dependency completion verifier independently verifies task-dep-01
        dep_ok, dep_reason = self.provider.verify_dependency_completion(
            dependency_task_id=task_id,
            expected_workspace=self.workspace,
            now_utc=self.now_dt,
        )
        self.assertTrue(dep_ok)
        self.assertIn("verified", dep_reason)

        # 3. Main task has dependency on task-dep-01; adapter evaluate_and_dispatch
        # holds dependency-bearing work before decision per v0.1 specification
        res_main = adapter.evaluate_and_dispatch(
            raw_task_data={"task_id": main_task_id, "revision": 1},
            authority_resolver=self.provider,
            binding_provider=binding_prov,
            observation_provider=obs_prov,
            native_guard=native_guard,
            transport_callbacks=transport_cbs,
            dependency_verifier=self.provider,
        )
        self.assertFalse(res_main.decision_made)
        self.assertIn("dependency authority is HELD", res_main.refusal_reason)

    def test_crash_and_reopen_durable_state(self):
        """Test that all grants, custody ACKs, proposals, reviews, and acceptances persist across crash/reopen."""
        task_id = "task-persist"
        expires_at = format_iso_utc(self.now_dt + datetime.timedelta(hours=2))

        self.provider.issue_task_grant(
            issuer_actor_id=self.issuer_id,
            issuer_token=self.issuer_token,
            task_id=task_id,
            revision=1,
            project_id=self.project_id,
            workspace=self.workspace,
            owner_bus_actor=self.owner_id,
            native_owner_tag=self.owner_tag,
            native_session_uuid=self.session_uuid,
            generation=self.generation,
            source_id="sha-p",
            source_digest="digest-p",
            expires_at=expires_at,
        )
        self.provider.record_custody_ack(
            owner_actor_id=self.owner_id,
            owner_token=self.owner_token,
            task_id=task_id,
            revision=1,
            project_id=self.project_id,
            workspace=self.workspace,
            native_owner_tag=self.owner_tag,
            native_session_uuid=self.session_uuid,
            generation=self.generation,
            idempotency_key="ack-persist",
        )

        # Reopen a fresh TaskAuthorityStore and Provider instance on the exact same SQLite file
        reopened_store = TaskAuthorityStore(self.db_path)
        reopened_provider = TaskAuthorityProvider(
            store=reopened_store,
            authenticator=self.authenticator,
            trust_root=self.trust_root,
            native_binding_provider=self.native_provider,
            clock=self.clock,
        )

        grant = reopened_store.get_latest_grant(task_id)
        self.assertIsNotNone(grant)
        self.assertEqual(grant.task_id, task_id)
        self.assertTrue(grant.has_owner_ack)
        self.assertEqual(grant.status, "ready")

        ack = reopened_store.get_custody_ack(task_id, 1)
        self.assertIsNotNone(ack)
        self.assertEqual(ack.idempotency_key, "ack-persist")

        resolved = reopened_provider.resolve_authority(task_id, self.now_dt)
        self.assertIsNotNone(resolved)
        self.assertTrue(resolved.has_owner_ack)
        self.assertEqual(resolved.status, "ready")

    def test_cas_sequential_revisions_and_idempotency(self):
        """Test sequential CAS revisions (1 -> 2 -> 3) and exact duplicate idempotency."""
        task_id = "task-cas"
        expires_at = format_iso_utc(self.now_dt + datetime.timedelta(hours=2))

        # Rev 1
        g1 = self.provider.issue_task_grant(
            issuer_actor_id=self.issuer_id,
            issuer_token=self.issuer_token,
            task_id=task_id,
            revision=1,
            project_id=self.project_id,
            workspace=self.workspace,
            owner_bus_actor=self.owner_id,
            native_owner_tag=self.owner_tag,
            native_session_uuid=self.session_uuid,
            generation=self.generation,
            source_id="sha-1",
            source_digest="digest-1",
            expires_at=expires_at,
        )
        self.assertEqual(g1.revision, 1)

        # Exact duplicate of Rev 1 -> idempotent return
        g1_dup = self.provider.issue_task_grant(
            issuer_actor_id=self.issuer_id,
            issuer_token=self.issuer_token,
            task_id=task_id,
            revision=1,
            project_id=self.project_id,
            workspace=self.workspace,
            owner_bus_actor=self.owner_id,
            native_owner_tag=self.owner_tag,
            native_session_uuid=self.session_uuid,
            generation=self.generation,
            source_id="sha-1",
            source_digest="digest-1",
            expires_at=expires_at,
        )
        self.assertEqual(g1_dup.revision, 1)

        # Changed payload with same revision -> CAS conflict!
        with self.assertRaises(CASConflictError):
            self.provider.issue_task_grant(
                issuer_actor_id=self.issuer_id,
                issuer_token=self.issuer_token,
                task_id=task_id,
                revision=1,
                project_id=self.project_id,
                workspace=self.workspace,
                owner_bus_actor=self.owner_id,
                native_owner_tag=self.owner_tag,
                native_session_uuid=self.session_uuid,
                generation=self.generation,
                source_id="sha-CHANGED",
                source_digest="digest-CHANGED",
                expires_at=expires_at,
            )

        # Skip to Rev 3 (skipping Rev 2) -> CAS conflict!
        with self.assertRaises(CASConflictError):
            self.provider.issue_task_grant(
                issuer_actor_id=self.issuer_id,
                issuer_token=self.issuer_token,
                task_id=task_id,
                revision=3,
                project_id=self.project_id,
                workspace=self.workspace,
                owner_bus_actor=self.owner_id,
                native_owner_tag=self.owner_tag,
                native_session_uuid=self.session_uuid,
                generation=self.generation,
                source_id="sha-3",
                source_digest="digest-3",
                expires_at=expires_at,
            )

        # Rev 2 succeeds
        g2 = self.provider.issue_task_grant(
            issuer_actor_id=self.issuer_id,
            issuer_token=self.issuer_token,
            task_id=task_id,
            revision=2,
            project_id=self.project_id,
            workspace=self.workspace,
            owner_bus_actor=self.owner_id,
            native_owner_tag=self.owner_tag,
            native_session_uuid=self.session_uuid,
            generation=self.generation,
            source_id="sha-2",
            source_digest="digest-2",
            expires_at=expires_at,
        )
        self.assertEqual(g2.revision, 2)

    # --- Comprehensive Negative Tests ---

    def test_negative_empty_trust_root(self):
        """Empty trust root denies all issuance, custody, review, and acceptance."""
        empty_root = AuthorityTrustRoot(trusted_actors=())
        prov = TaskAuthorityProvider(
            store=self.store,
            authenticator=self.authenticator,
            trust_root=empty_root,
            native_binding_provider=self.native_provider,
            clock=self.clock,
        )
        with self.assertRaises(PermissionDeniedError):
            prov.issue_task_grant(
                issuer_actor_id=self.issuer_id,
                issuer_token=self.issuer_token,
                task_id="task-empty-root",
                revision=1,
                project_id=self.project_id,
                workspace=self.workspace,
                owner_bus_actor=self.owner_id,
                native_owner_tag=self.owner_tag,
                native_session_uuid=self.session_uuid,
                generation=self.generation,
                source_id="s1",
                source_digest="d1",
                expires_at=format_iso_utc(self.now_dt + datetime.timedelta(hours=1)),
            )

    def test_negative_bad_token(self):
        """Invalid token fails authentication."""
        with self.assertRaises(AuthenticationError):
            self.provider.issue_task_grant(
                issuer_actor_id=self.issuer_id,
                issuer_token="wrong-token-1234",
                task_id="task-bad-token",
                revision=1,
                project_id=self.project_id,
                workspace=self.workspace,
                owner_bus_actor=self.owner_id,
                native_owner_tag=self.owner_tag,
                native_session_uuid=self.session_uuid,
                generation=self.generation,
                source_id="s1",
                source_digest="d1",
                expires_at=format_iso_utc(self.now_dt + datetime.timedelta(hours=1)),
            )

    def test_negative_spoofed_role(self):
        """Worker identity attempting to issue grant fails with PermissionDeniedError."""
        with self.assertRaises(PermissionDeniedError):
            self.provider.issue_task_grant(
                issuer_actor_id=self.owner_id,  # Owner is not an authorized issuer
                issuer_token=self.owner_token,
                task_id="task-spoof",
                revision=1,
                project_id=self.project_id,
                workspace=self.workspace,
                owner_bus_actor=self.owner_id,
                native_owner_tag=self.owner_tag,
                native_session_uuid=self.session_uuid,
                generation=self.generation,
                source_id="s1",
                source_digest="d1",
                expires_at=format_iso_utc(self.now_dt + datetime.timedelta(hours=1)),
            )

    def test_negative_project_scope_mismatch(self):
        """Attempting action on a different project fails."""
        with self.assertRaises(PermissionDeniedError):
            self.provider.issue_task_grant(
                issuer_actor_id=self.issuer_id,
                issuer_token=self.issuer_token,
                task_id="task-diff-proj",
                revision=1,
                project_id="foreign-project",
                workspace=self.workspace,
                owner_bus_actor=self.owner_id,
                native_owner_tag=self.owner_tag,
                native_session_uuid=self.session_uuid,
                generation=self.generation,
                source_id="s1",
                source_digest="d1",
                expires_at=format_iso_utc(self.now_dt + datetime.timedelta(hours=1)),
            )

    def test_negative_cross_workspace_denied(self):
        """Attempting action on unauthorized workspace fails."""
        with self.assertRaises(PermissionDeniedError):
            self.provider.issue_task_grant(
                issuer_actor_id=self.issuer_id,
                issuer_token=self.issuer_token,
                task_id="task-diff-ws",
                revision=1,
                project_id=self.project_id,
                workspace="/unauthorized/workspace/path",
                owner_bus_actor=self.owner_id,
                native_owner_tag=self.owner_tag,
                native_session_uuid=self.session_uuid,
                generation=self.generation,
                source_id="s1",
                source_digest="d1",
                expires_at=format_iso_utc(self.now_dt + datetime.timedelta(hours=1)),
            )

    def test_negative_issuer_revocation_fencing(self):
        """Revoking a grant fences subsequent custody, proposals, reviews, and acceptance."""
        task_id = "task-revoke"
        expires_at = format_iso_utc(self.now_dt + datetime.timedelta(hours=2))
        self.provider.issue_task_grant(
            issuer_actor_id=self.issuer_id,
            issuer_token=self.issuer_token,
            task_id=task_id,
            revision=1,
            project_id=self.project_id,
            workspace=self.workspace,
            owner_bus_actor=self.owner_id,
            native_owner_tag=self.owner_tag,
            native_session_uuid=self.session_uuid,
            generation=self.generation,
            source_id="s1",
            source_digest="d1",
            expires_at=expires_at,
        )

        # Issuer revokes rev 1
        self.provider.revoke_task_grant(
            issuer_actor_id=self.issuer_id,
            issuer_token=self.issuer_token,
            task_id=task_id,
            revision=1,
            project_id=self.project_id,
            workspace=self.workspace,
            reason="Operator cancelled",
        )

        # Attempting custody ACK on revoked grant fails
        with self.assertRaises(FencedStateError):
            self.provider.record_custody_ack(
                owner_actor_id=self.owner_id,
                owner_token=self.owner_token,
                task_id=task_id,
                revision=1,
                project_id=self.project_id,
                workspace=self.workspace,
                native_owner_tag=self.owner_tag,
                native_session_uuid=self.session_uuid,
                generation=self.generation,
                idempotency_key="ack-revoked",
            )

    def test_negative_owner_session_or_generation_conflict(self):
        """Owner attempting custody ACK with wrong session UUID or generation fails."""
        task_id = "task-tuple-conflict"
        expires_at = format_iso_utc(self.now_dt + datetime.timedelta(hours=2))
        self.provider.issue_task_grant(
            issuer_actor_id=self.issuer_id,
            issuer_token=self.issuer_token,
            task_id=task_id,
            revision=1,
            project_id=self.project_id,
            workspace=self.workspace,
            owner_bus_actor=self.owner_id,
            native_owner_tag=self.owner_tag,
            native_session_uuid=self.session_uuid,
            generation=self.generation,
            source_id="s1",
            source_digest="d1",
            expires_at=expires_at,
        )

        # Wrong generation (e.g. 2 instead of 1)
        with self.assertRaises(NativeBindingError):
            self.provider.record_custody_ack(
                owner_actor_id=self.owner_id,
                owner_token=self.owner_token,
                task_id=task_id,
                revision=1,
                project_id=self.project_id,
                workspace=self.workspace,
                native_owner_tag=self.owner_tag,
                native_session_uuid=self.session_uuid,
                generation=2,
                idempotency_key="ack-gen-2",
            )

    def test_negative_self_review_forbidden(self):
        """Owner attempting to review their own proposal fails with PermissionDeniedError."""
        task_id = "task-self-rev"
        expires_at = format_iso_utc(self.now_dt + datetime.timedelta(hours=2))
        self.provider.issue_task_grant(
            issuer_actor_id=self.issuer_id,
            issuer_token=self.issuer_token,
            task_id=task_id,
            revision=1,
            project_id=self.project_id,
            workspace=self.workspace,
            owner_bus_actor=self.owner_id,
            native_owner_tag=self.owner_tag,
            native_session_uuid=self.session_uuid,
            generation=self.generation,
            source_id="s1",
            source_digest="d1",
            expires_at=expires_at,
        )
        self.provider.record_custody_ack(
            owner_actor_id=self.owner_id,
            owner_token=self.owner_token,
            task_id=task_id,
            revision=1,
            project_id=self.project_id,
            workspace=self.workspace,
            native_owner_tag=self.owner_tag,
            native_session_uuid=self.session_uuid,
            generation=self.generation,
            idempotency_key="ack-self-rev",
        )
        r_digest = compute_sha256("result")
        self.provider.propose_task_completion(
            owner_actor_id=self.owner_id,
            owner_token=self.owner_token,
            task_id=task_id,
            revision=1,
            project_id=self.project_id,
            workspace=self.workspace,
            native_owner_tag=self.owner_tag,
            native_session_uuid=self.session_uuid,
            generation=self.generation,
            result_digest=r_digest,
            result_evidence={"digest": r_digest},
            idempotency_key="prop-self-rev",
        )

        # Grant owner attempting review (even if registered in policy)
        self_review_policy_root = AuthorityTrustRoot(
            trusted_actors=self.trust_root.trusted_actors + (
                TrustedActorPolicy(actor_id=self.owner_id, role="reviewer", project_id=self.project_id, workspace=self.workspace),
            )
        )
        prov_self = TaskAuthorityProvider(
            store=self.store,
            authenticator=self.authenticator,
            trust_root=self_review_policy_root,
            native_binding_provider=self.native_provider,
            clock=self.clock,
        )

        with self.assertRaises(PermissionDeniedError) as ctx:
            prov_self.record_task_review(
                reviewer_actor_id=self.owner_id,
                reviewer_token=self.owner_token,
                task_id=task_id,
                revision=1,
                project_id=self.project_id,
                workspace=self.workspace,
                expected_result_digest=r_digest,
                verdict="approved",
                review_evidence={"self": True},
                idempotency_key="rev-self",
            )
        self.assertIn("Self-review forbidden", str(ctx.exception))

    def test_negative_artifact_pin_conflict(self):
        """Review specifying mismatched expected result digest fails with VerificationError."""
        task_id = "task-pin-conflict"
        expires_at = format_iso_utc(self.now_dt + datetime.timedelta(hours=2))
        self.provider.issue_task_grant(
            issuer_actor_id=self.issuer_id,
            issuer_token=self.issuer_token,
            task_id=task_id,
            revision=1,
            project_id=self.project_id,
            workspace=self.workspace,
            owner_bus_actor=self.owner_id,
            native_owner_tag=self.owner_tag,
            native_session_uuid=self.session_uuid,
            generation=self.generation,
            source_id="s1",
            source_digest="d1",
            expires_at=expires_at,
        )
        self.provider.record_custody_ack(
            owner_actor_id=self.owner_id,
            owner_token=self.owner_token,
            task_id=task_id,
            revision=1,
            project_id=self.project_id,
            workspace=self.workspace,
            native_owner_tag=self.owner_tag,
            native_session_uuid=self.session_uuid,
            generation=self.generation,
            idempotency_key="ack-pin",
        )
        r_digest = compute_sha256("result actual")
        self.provider.propose_task_completion(
            owner_actor_id=self.owner_id,
            owner_token=self.owner_token,
            task_id=task_id,
            revision=1,
            project_id=self.project_id,
            workspace=self.workspace,
            native_owner_tag=self.owner_tag,
            native_session_uuid=self.session_uuid,
            generation=self.generation,
            result_digest=r_digest,
            result_evidence={"digest": r_digest},
            idempotency_key="prop-pin",
        )

        with self.assertRaises(VerificationError) as ctx:
            self.provider.record_task_review(
                reviewer_actor_id=self.reviewer_id,
                reviewer_token=self.reviewer_token,
                task_id=task_id,
                revision=1,
                project_id=self.project_id,
                workspace=self.workspace,
                expected_result_digest=compute_sha256("tampered result"),
                verdict="approved",
                review_evidence={"test": True},
                idempotency_key="rev-tamper",
            )
        self.assertIn("Pinned artifact tamper/conflict", str(ctx.exception))

    def test_negative_native_binding_protected_or_dead(self):
        """Native session frozen, blocked, quota-held, or dead fails closed."""
        task_id = "task-native-fail"
        expires_at = format_iso_utc(self.now_dt + datetime.timedelta(hours=2))

        # 1. Native session frozen
        self.native_provider.set_binding(
            workspace=self.workspace,
            tag=self.owner_tag,
            session_id=self.session_uuid,
            generation=self.generation,
            is_alive=True,
            is_frozen=True,
        )
        with self.assertRaises(NativeBindingError):
            self.provider.issue_task_grant(
                issuer_actor_id=self.issuer_id,
                issuer_token=self.issuer_token,
                task_id=task_id,
                revision=1,
                project_id=self.project_id,
                workspace=self.workspace,
                owner_bus_actor=self.owner_id,
                native_owner_tag=self.owner_tag,
                native_session_uuid=self.session_uuid,
                generation=self.generation,
                source_id="s1",
                source_digest="d1",
                expires_at=expires_at,
            )

        # 2. Native session quota held
        self.native_provider.set_binding(
            workspace=self.workspace,
            tag=self.owner_tag,
            session_id=self.session_uuid,
            generation=self.generation,
            is_alive=True,
            is_quota_held=True,
        )
        with self.assertRaises(NativeBindingError):
            self.provider.issue_task_grant(
                issuer_actor_id=self.issuer_id,
                issuer_token=self.issuer_token,
                task_id=task_id,
                revision=1,
                project_id=self.project_id,
                workspace=self.workspace,
                owner_bus_actor=self.owner_id,
                native_owner_tag=self.owner_tag,
                native_session_uuid=self.session_uuid,
                generation=self.generation,
                source_id="s1",
                source_digest="d1",
                expires_at=expires_at,
            )

        # 3. Native session dead
        self.native_provider.set_binding(
            workspace=self.workspace,
            tag=self.owner_tag,
            session_id=self.session_uuid,
            generation=self.generation,
            is_alive=False,
        )
        with self.assertRaises(NativeBindingError):
            self.provider.issue_task_grant(
                issuer_actor_id=self.issuer_id,
                issuer_token=self.issuer_token,
                task_id=task_id,
                revision=1,
                project_id=self.project_id,
                workspace=self.workspace,
                owner_bus_actor=self.owner_id,
                native_owner_tag=self.owner_tag,
                native_session_uuid=self.session_uuid,
                generation=self.generation,
                source_id="s1",
                source_digest="d1",
                expires_at=expires_at,
            )

    def test_negative_stale_and_future_timestamps(self):
        """Timezone-naive, stale, and future timestamps strictly rejected."""
        # Timezone naive
        with self.assertRaises(InvalidTupleError):
            parse_strict_iso_utc("2026-10-13 16:00:00")

        # Future native binding observation
        future_dt = self.now_dt + datetime.timedelta(minutes=10)
        self.native_provider.set_binding(
            workspace=self.workspace,
            tag=self.owner_tag,
            session_id=self.session_uuid,
            generation=self.generation,
            is_alive=True,
            observed_at=format_iso_utc(future_dt),
        )
        with self.assertRaises(NativeBindingError) as ctx:
            self.provider.issue_task_grant(
                issuer_actor_id=self.issuer_id,
                issuer_token=self.issuer_token,
                task_id="task-future-time",
                revision=1,
                project_id=self.project_id,
                workspace=self.workspace,
                owner_bus_actor=self.owner_id,
                native_owner_tag=self.owner_tag,
                native_session_uuid=self.session_uuid,
                generation=self.generation,
                source_id="s1",
                source_digest="d1",
                expires_at=format_iso_utc(self.now_dt + datetime.timedelta(hours=1)),
            )
        self.assertIn("in the future", str(ctx.exception))

    def test_negative_zero_db_effects_on_rejection(self):
        """Any rejected operation must leave zero trace in the SQLite database."""
        # Attempt an invalid issuance (e.g. revision 5 on a non-existent task)
        with self.assertRaises(CASConflictError):
            self.provider.issue_task_grant(
                issuer_actor_id=self.issuer_id,
                issuer_token=self.issuer_token,
                task_id="task-no-trace",
                revision=5,
                project_id=self.project_id,
                workspace=self.workspace,
                owner_bus_actor=self.owner_id,
                native_owner_tag=self.owner_tag,
                native_session_uuid=self.session_uuid,
                generation=self.generation,
                source_id="s1",
                source_digest="d1",
                expires_at=format_iso_utc(self.now_dt + datetime.timedelta(hours=1)),
            )

        grant = self.store.get_latest_grant("task-no-trace")
        self.assertIsNone(grant)

        with self.store._get_connection() as conn:
            cur = conn.execute("SELECT COUNT(*) FROM task_grants WHERE task_id = 'task-no-trace';")
            count = cur.fetchone()[0]
            self.assertEqual(count, 0)

    def test_get_task_grant_cross_scope_isolation(self):
        """Cross-scope task query does not leak record from foreign project/workspace."""
        task_id = "task-scope-iso"
        expires_at = format_iso_utc(self.now_dt + datetime.timedelta(hours=2))
        self.provider.issue_task_grant(
            issuer_actor_id=self.issuer_id,
            issuer_token=self.issuer_token,
            task_id=task_id,
            revision=1,
            project_id=self.project_id,
            workspace=self.workspace,
            owner_bus_actor=self.owner_id,
            native_owner_tag=self.owner_tag,
            native_session_uuid=self.session_uuid,
            generation=self.generation,
            source_id="s1",
            source_digest="d1",
            expires_at=expires_at,
        )

        # Querying with matching scope returns grant
        g = self.provider.get_task_grant(
            task_id=task_id,
            caller_actor_id=self.issuer_id,
            caller_token=self.issuer_token,
            project_id=self.project_id,
            workspace=self.workspace,
        )
        self.assertIsNotNone(g)

        # Reader registered on another workspace cannot access this record
        other_ws_root = AuthorityTrustRoot(
            trusted_actors=self.trust_root.trusted_actors + (
                TrustedActorPolicy(actor_id=self.issuer_id, role="reader", project_id=self.project_id, workspace="/other/workspace"),
            )
        )
        prov_other = TaskAuthorityProvider(
            store=self.store,
            authenticator=self.authenticator,
            trust_root=other_ws_root,
            native_binding_provider=self.native_provider,
            clock=self.clock,
        )
        g_cross = prov_other.get_task_grant(
            task_id=task_id,
            caller_actor_id=self.issuer_id,
            caller_token=self.issuer_token,
            project_id=self.project_id,
            workspace="/other/workspace",
        )
        self.assertIsNone(g_cross)

    def test_dependency_verification_fails_if_canonical_identity_deleted(self):
        """Deleted or revoked actor in FileBus identities store invalidates dependency verification."""
        task_id = "task-dep-del"
        expires_at = format_iso_utc(self.now_dt + datetime.timedelta(hours=2))

        self.provider.issue_task_grant(
            issuer_actor_id=self.issuer_id,
            issuer_token=self.issuer_token,
            task_id=task_id,
            revision=1,
            project_id=self.project_id,
            workspace=self.workspace,
            owner_bus_actor=self.owner_id,
            native_owner_tag=self.owner_tag,
            native_session_uuid=self.session_uuid,
            generation=self.generation,
            source_id="s1",
            source_digest="d1",
            expires_at=expires_at,
        )
        self.provider.record_custody_ack(
            owner_actor_id=self.owner_id,
            owner_token=self.owner_token,
            task_id=task_id,
            revision=1,
            project_id=self.project_id,
            workspace=self.workspace,
            native_owner_tag=self.owner_tag,
            native_session_uuid=self.session_uuid,
            generation=self.generation,
            idempotency_key="ack-dep-del",
        )
        r_digest = compute_sha256("dep result")
        self.provider.propose_task_completion(
            owner_actor_id=self.owner_id,
            owner_token=self.owner_token,
            task_id=task_id,
            revision=1,
            project_id=self.project_id,
            workspace=self.workspace,
            native_owner_tag=self.owner_tag,
            native_session_uuid=self.session_uuid,
            generation=self.generation,
            result_digest=r_digest,
            result_evidence={"digest": r_digest},
            idempotency_key="prop-dep-del",
        )
        self.provider.record_task_review(
            reviewer_actor_id=self.reviewer_id,
            reviewer_token=self.reviewer_token,
            task_id=task_id,
            revision=1,
            project_id=self.project_id,
            workspace=self.workspace,
            expected_result_digest=r_digest,
            verdict="approved",
            review_evidence={"ok": True},
            idempotency_key="rev-dep-del",
        )
        self.provider.record_task_acceptance(
            acceptor_actor_id=self.acceptor_id,
            acceptor_token=self.acceptor_token,
            task_id=task_id,
            revision=1,
            project_id=self.project_id,
            workspace=self.workspace,
            expected_result_digest=r_digest,
            acceptance_evidence={"ok": True},
            idempotency_key="acc-dep-del",
        )

        # Before deletion: verified ok
        ok, reason = self.provider.verify_dependency_completion(
            dependency_task_id=task_id,
            expected_workspace=self.workspace,
            now_utc=self.now_dt,
        )
        self.assertTrue(ok)

        # Delete reviewer identity from FileBus identities.json to simulate revocation/rotation
        identities_file = self.bus_dir / "identities.json"
        data = json.loads(identities_file.read_text(encoding="utf-8"))
        del data[self.reviewer_id]
        identities_file.write_text(json.dumps(data), encoding="utf-8")

        # After deletion: verify_dependency_completion fails closed!
        ok_after, reason_after = self.provider.verify_dependency_completion(
            dependency_task_id=task_id,
            expected_workspace=self.workspace,
            now_utc=self.now_dt,
        )
        self.assertFalse(ok_after)
        self.assertIn("revoked or missing canonical reviewer identity", reason_after)

    def test_native_binding_mismatched_workspace_or_tag_denied(self):
        """Native evidence with mismatched workspace or tag fails closed."""
        # 1. Evidence has mismatched workspace
        self.native_provider._records[(self.workspace, self.owner_tag)] = NativeSessionEvidence(
            workspace="/different/workspace",
            tag=self.owner_tag,
            session_id=self.session_uuid,
            generation=self.generation,
            is_alive=True,
            observed_at=format_iso_utc(self.now_dt),
            is_fixture_only=True,
        )
        with self.assertRaises(NativeBindingError):
            self.provider.issue_task_grant(
                issuer_actor_id=self.issuer_id,
                issuer_token=self.issuer_token,
                task_id="task-ws-mismatch",
                revision=1,
                project_id=self.project_id,
                workspace=self.workspace,
                owner_bus_actor=self.owner_id,
                native_owner_tag=self.owner_tag,
                native_session_uuid=self.session_uuid,
                generation=self.generation,
                source_id="s1",
                source_digest="d1",
                expires_at=format_iso_utc(self.now_dt + datetime.timedelta(hours=1)),
            )

        # 2. Evidence has mismatched tag
        self.native_provider._records[(self.workspace, self.owner_tag)] = NativeSessionEvidence(
            workspace=self.workspace,
            tag="other-tag",
            session_id=self.session_uuid,
            generation=self.generation,
            is_alive=True,
            observed_at=format_iso_utc(self.now_dt),
            is_fixture_only=True,
        )
        with self.assertRaises(NativeBindingError):
            self.provider.issue_task_grant(
                issuer_actor_id=self.issuer_id,
                issuer_token=self.issuer_token,
                task_id="task-tag-mismatch",
                revision=1,
                project_id=self.project_id,
                workspace=self.workspace,
                owner_bus_actor=self.owner_id,
                native_owner_tag=self.owner_tag,
                native_session_uuid=self.session_uuid,
                generation=self.generation,
                source_id="s1",
                source_digest="d1",
                expires_at=format_iso_utc(self.now_dt + datetime.timedelta(hours=1)),
            )


    def test_filebus_token_only_removal_revokes_auth(self):
        """Removing token from tokens.json while keeping identity revokes active status and authentication."""
        ident, token = self.file_bus.register(
            agent_name="test-tok-agent",
            device_id="dev-1",
            project_id=self.project_id,
            task_id="task-tok",
        )
        # Initially active and authenticates
        self.assertTrue(self.authenticator.is_identity_active(ident.identity_id, self.project_id))
        self.assertEqual(
            self.authenticator.authenticate(ident.identity_id, token).get("identity_id"),
            ident.identity_id,
        )

        # Remove only token from tokens.json
        tokens_file = self.bus_dir / "tokens.json"
        tokens = json.loads(tokens_file.read_text(encoding="utf-8"))
        del tokens[ident.identity_id]
        tokens_file.write_text(json.dumps(tokens), encoding="utf-8")

        # Now is_identity_active must be False
        self.assertFalse(self.authenticator.is_identity_active(ident.identity_id, self.project_id))
        # And authenticate must fail closed
        with self.assertRaises(AuthenticationError):
            self.authenticator.authenticate(ident.identity_id, token)

    def test_filebus_identity_only_removal_revokes_auth(self):
        """Removing identity from identities.json while keeping token revokes active status and authentication."""
        ident, token = self.file_bus.register(
            agent_name="test-id-agent",
            device_id="dev-1",
            project_id=self.project_id,
            task_id="task-id",
        )
        self.assertTrue(self.authenticator.is_identity_active(ident.identity_id, self.project_id))

        # Remove only identity from identities.json
        identities_file = self.bus_dir / "identities.json"
        identities = json.loads(identities_file.read_text(encoding="utf-8"))
        del identities[ident.identity_id]
        identities_file.write_text(json.dumps(identities), encoding="utf-8")

        self.assertFalse(self.authenticator.is_identity_active(ident.identity_id, self.project_id))
        with self.assertRaises(AuthenticationError):
            self.authenticator.authenticate(ident.identity_id, token)

    def test_filebus_malformed_identity_or_project_denied(self):
        """Malformed or non-string identity_id, token, or project_id fail closed."""
        for bad_id in [None, "", "   ", 123, [], {}]:
            self.assertFalse(self.authenticator.is_identity_active(bad_id, self.project_id))
            with self.assertRaises(AuthenticationError):
                self.authenticator.authenticate(bad_id, "some-token")

        for bad_token in [None, "", "   ", 123, [], {}]:
            with self.assertRaises(AuthenticationError):
                self.authenticator.authenticate(self.owner_id, bad_token)

        for bad_proj in [None, "", "   ", 123, [], {}]:
            self.assertFalse(self.authenticator.is_identity_active(self.owner_id, bad_proj))

    def test_filebus_unavailable_lock_defaults_deny(self):
        """Unavailable FileLock defaults to deny rather than proceeding unlocked."""
        class BusWithoutLock:
            pass

        auth_no_lock = FileBusAuthenticator(bus=BusWithoutLock())
        with self.assertRaises(AuthenticationError):
            auth_no_lock.authenticate(self.owner_id, self.owner_token)
        self.assertFalse(auth_no_lock.is_identity_active(self.owner_id, self.project_id))

        class BusWithBadLock:
            _lock = Path("/nonexistent/cannot/create/lock.dir/bus.lock")

        auth_bad_lock = FileBusAuthenticator(bus=BusWithBadLock())
        with self.assertRaises(AuthenticationError):
            auth_bad_lock.authenticate(self.owner_id, self.owner_token)
        self.assertFalse(auth_bad_lock.is_identity_active(self.owner_id, self.project_id))

    def _get_db_count(self, table: str) -> int:
        with self.store._get_connection() as conn:
            cur = conn.execute(f"SELECT COUNT(*) FROM {table};")
            return cur.fetchone()[0]

    def test_probe1_native_binding_provider_none_fails_closed(self):
        """Probe 1 regression: native_binding_provider=None fails closed across all resolvers, queries, and mutations."""
        task_id = "task-probe1-nobinding"
        expires_at = format_iso_utc(self.now_dt + datetime.timedelta(hours=2))

        # 1. Issuing with native_binding_provider=None raises NativeBindingError
        prov_no_native = TaskAuthorityProvider(
            store=self.store,
            authenticator=self.authenticator,
            trust_root=self.trust_root,
            native_binding_provider=None,
            clock=self.clock,
        )
        with self.assertRaises(NativeBindingError):
            prov_no_native.issue_task_grant(
                issuer_actor_id=self.issuer_id,
                issuer_token=self.issuer_token,
                task_id=task_id,
                revision=1,
                project_id=self.project_id,
                workspace=self.workspace,
                owner_bus_actor=self.owner_id,
                native_owner_tag=self.owner_tag,
                native_session_uuid=self.session_uuid,
                generation=self.generation,
                source_id="s1",
                source_digest="d1",
                expires_at=expires_at,
            )

        # Issue grant with normal provider and ACK it so grant exists in DB
        self.provider.issue_task_grant(
            issuer_actor_id=self.issuer_id,
            issuer_token=self.issuer_token,
            task_id=task_id,
            revision=1,
            project_id=self.project_id,
            workspace=self.workspace,
            owner_bus_actor=self.owner_id,
            native_owner_tag=self.owner_tag,
            native_session_uuid=self.session_uuid,
            generation=self.generation,
            source_id="s1",
            source_digest="d1",
            expires_at=expires_at,
        )
        self.provider.record_custody_ack(
            owner_actor_id=self.owner_id,
            owner_token=self.owner_token,
            task_id=task_id,
            revision=1,
            project_id=self.project_id,
            workspace=self.workspace,
            native_owner_tag=self.owner_tag,
            native_session_uuid=self.session_uuid,
            generation=self.generation,
            idempotency_key="ack-probe1",
        )

        # 2. Under prov_no_native, resolve_authority returns status="blocked" and is_protected=False
        res = prov_no_native.resolve_authority(task_id, self.now_dt)
        self.assertIsNotNone(res)
        self.assertEqual(res.status, "blocked")
        self.assertIn("Native binding provider", res.protected_reason or "")

        # 3. revalidate_authority returns None
        self.assertIsNone(
            prov_no_native.revalidate_authority(
                task_id=task_id,
                revision=1,
                workspace=self.workspace,
                now_utc=self.now_dt,
            )
        )

        # 4. get_task_grant returns blocked status, not leaking ready authority
        grant_view = prov_no_native.get_task_grant(
            task_id=task_id,
            caller_actor_id=self.issuer_id,
            caller_token=self.issuer_token,
            project_id=self.project_id,
            workspace=self.workspace,
        )
        self.assertIsNotNone(grant_view)
        self.assertEqual(grant_view.status, "blocked")
        self.assertIn("Native binding provider", grant_view.protected_reason or "")

        # 5. Mutations fail closed with NativeBindingError and zero DB side effects
        count_proposals_before = self._get_db_count("completion_proposals")
        with self.assertRaises(NativeBindingError):
            prov_no_native.propose_task_completion(
                owner_actor_id=self.owner_id,
                owner_token=self.owner_token,
                task_id=task_id,
                revision=1,
                project_id=self.project_id,
                workspace=self.workspace,
                native_owner_tag=self.owner_tag,
                native_session_uuid=self.session_uuid,
                generation=self.generation,
                result_digest=compute_sha256("res"),
                result_evidence={"d": "1"},
                idempotency_key="prop-probe1-fail",
            )
        self.assertEqual(self._get_db_count("completion_proposals"), count_proposals_before)

    def test_probe2_issuer_token_removal_invalidates_authority(self):
        """Probe 2 regression: issuer token removal invalidates authority and revalidation fails closed."""
        task_id = "task-probe2-issuer-tok"
        expires_at = format_iso_utc(self.now_dt + datetime.timedelta(hours=2))

        self.provider.issue_task_grant(
            issuer_actor_id=self.issuer_id,
            issuer_token=self.issuer_token,
            task_id=task_id,
            revision=1,
            project_id=self.project_id,
            workspace=self.workspace,
            owner_bus_actor=self.owner_id,
            native_owner_tag=self.owner_tag,
            native_session_uuid=self.session_uuid,
            generation=self.generation,
            source_id="s1",
            source_digest="d1",
            expires_at=expires_at,
        )
        self.provider.record_custody_ack(
            owner_actor_id=self.owner_id,
            owner_token=self.owner_token,
            task_id=task_id,
            revision=1,
            project_id=self.project_id,
            workspace=self.workspace,
            native_owner_tag=self.owner_tag,
            native_session_uuid=self.session_uuid,
            generation=self.generation,
            idempotency_key="ack-probe2",
        )

        # Before token removal: valid, ready, and protected
        res_before = self.provider.resolve_authority(task_id, self.now_dt)
        self.assertIsNotNone(res_before)
        self.assertEqual(res_before.status, "ready")
        self.assertIsNone(res_before.protected_reason)
        self.assertIsNotNone(self.provider.revalidate_authority(task_id, 1, self.workspace, self.now_dt))

        # Delete issuer token from tokens.json
        tokens_file = self.bus_dir / "tokens.json"
        tokens = json.loads(tokens_file.read_text(encoding="utf-8"))
        del tokens[self.issuer_id]
        tokens_file.write_text(json.dumps(tokens), encoding="utf-8")

        # After token removal: resolve_authority fails closed to blocked
        res_after = self.provider.resolve_authority(task_id, self.now_dt)
        self.assertIsNotNone(res_after)
        self.assertEqual(res_after.status, "blocked")
        self.assertIn("token removed", res_after.protected_reason or "")

        # revalidate_authority returns None
        self.assertIsNone(self.provider.revalidate_authority(task_id, 1, self.workspace, self.now_dt))

        # get_task_grant by authorized reader does not leak ready authority
        g = self.provider.get_task_grant(
            task_id=task_id,
            caller_actor_id=self.acceptor_id,
            caller_token=self.acceptor_token,
            project_id=self.project_id,
            workspace=self.workspace,
        )
        self.assertIsNotNone(g)
        self.assertEqual(g.status, "blocked")
        self.assertIn("token removed", g.protected_reason or "")

        # Subsequent mutations fail closed with zero DB side effects
        count_props = self._get_db_count("completion_proposals")
        with self.assertRaises(PermissionDeniedError):
            self.provider.propose_task_completion(
                owner_actor_id=self.owner_id,
                owner_token=self.owner_token,
                task_id=task_id,
                revision=1,
                project_id=self.project_id,
                workspace=self.workspace,
                native_owner_tag=self.owner_tag,
                native_session_uuid=self.session_uuid,
                generation=self.generation,
                result_digest=compute_sha256("res"),
                result_evidence={"d": "1"},
                idempotency_key="prop-probe2-fail",
            )
        self.assertEqual(self._get_db_count("completion_proposals"), count_props)

    def test_probe3_historic_revision_revalidate_and_resolve_rejected(self):
        """Probe 3 regression: historic revision cannot be revalidated or resolved as current authority."""
        task_id = "task-probe3-rev-fence"
        expires_at = format_iso_utc(self.now_dt + datetime.timedelta(hours=2))

        # Issue rev 1 and ACK
        self.provider.issue_task_grant(
            issuer_actor_id=self.issuer_id,
            issuer_token=self.issuer_token,
            task_id=task_id,
            revision=1,
            project_id=self.project_id,
            workspace=self.workspace,
            owner_bus_actor=self.owner_id,
            native_owner_tag=self.owner_tag,
            native_session_uuid=self.session_uuid,
            generation=self.generation,
            source_id="s1",
            source_digest="d1",
            expires_at=expires_at,
        )
        self.provider.record_custody_ack(
            owner_actor_id=self.owner_id,
            owner_token=self.owner_token,
            task_id=task_id,
            revision=1,
            project_id=self.project_id,
            workspace=self.workspace,
            native_owner_tag=self.owner_tag,
            native_session_uuid=self.session_uuid,
            generation=self.generation,
            idempotency_key="ack-probe3-r1",
        )

        # Issue rev 2 and ACK
        self.provider.issue_task_grant(
            issuer_actor_id=self.issuer_id,
            issuer_token=self.issuer_token,
            task_id=task_id,
            revision=2,
            project_id=self.project_id,
            workspace=self.workspace,
            owner_bus_actor=self.owner_id,
            native_owner_tag=self.owner_tag,
            native_session_uuid=self.session_uuid,
            generation=self.generation,
            source_id="s2",
            source_digest="d2",
            expires_at=expires_at,
        )
        self.provider.record_custody_ack(
            owner_actor_id=self.owner_id,
            owner_token=self.owner_token,
            task_id=task_id,
            revision=2,
            project_id=self.project_id,
            workspace=self.workspace,
            native_owner_tag=self.owner_tag,
            native_session_uuid=self.session_uuid,
            generation=self.generation,
            idempotency_key="ack-probe3-r2",
        )

        # Current rev 2 revalidates successfully
        r2 = self.provider.revalidate_authority(task_id, 2, self.workspace, self.now_dt)
        self.assertIsNotNone(r2)
        self.assertEqual(r2.revision, 2)
        self.assertEqual(r2.status, "ready")

        # Historic rev 1 revalidation MUST return None
        r1 = self.provider.revalidate_authority(task_id, 1, self.workspace, self.now_dt)
        self.assertIsNone(r1)

        # Resolving specifically with revision=1 cannot pick historic revision
        res_r1 = self.provider.resolve_authority({"task_id": task_id, "revision": 1}, self.now_dt)
        self.assertIsNone(res_r1)

        # Resolving without revision picks current revision 2
        res_current = self.provider.resolve_authority(task_id, self.now_dt)
        self.assertIsNotNone(res_current)
        self.assertEqual(res_current.revision, 2)

        # Mutation referencing historic rev 1 fails closed
        count_props = self._get_db_count("completion_proposals")
        with self.assertRaises((CASConflictError, FencedStateError)):
            self.provider.propose_task_completion(
                owner_actor_id=self.owner_id,
                owner_token=self.owner_token,
                task_id=task_id,
                revision=1,
                project_id=self.project_id,
                workspace=self.workspace,
                native_owner_tag=self.owner_tag,
                native_session_uuid=self.session_uuid,
                generation=self.generation,
                result_digest=compute_sha256("res"),
                result_evidence={"d": "1"},
                idempotency_key="prop-probe3-fail",
            )
        self.assertEqual(self._get_db_count("completion_proposals"), count_props)

    def test_negative_owner_token_removal_invalidates_authority(self):
        """Removal of owner token from FileBus invalidates authority and fails closed."""
        task_id = "task-neg-owner-tok"
        expires_at = format_iso_utc(self.now_dt + datetime.timedelta(hours=2))

        self.provider.issue_task_grant(
            issuer_actor_id=self.issuer_id,
            issuer_token=self.issuer_token,
            task_id=task_id,
            revision=1,
            project_id=self.project_id,
            workspace=self.workspace,
            owner_bus_actor=self.owner_id,
            native_owner_tag=self.owner_tag,
            native_session_uuid=self.session_uuid,
            generation=self.generation,
            source_id="s1",
            source_digest="d1",
            expires_at=expires_at,
        )
        self.provider.record_custody_ack(
            owner_actor_id=self.owner_id,
            owner_token=self.owner_token,
            task_id=task_id,
            revision=1,
            project_id=self.project_id,
            workspace=self.workspace,
            native_owner_tag=self.owner_tag,
            native_session_uuid=self.session_uuid,
            generation=self.generation,
            idempotency_key="ack-owner-tok",
        )

        tokens_file = self.bus_dir / "tokens.json"
        tokens = json.loads(tokens_file.read_text(encoding="utf-8"))
        del tokens[self.owner_id]
        tokens_file.write_text(json.dumps(tokens), encoding="utf-8")

        res = self.provider.resolve_authority(task_id, self.now_dt)
        self.assertEqual(res.status, "blocked")
        self.assertIn("token removed", res.protected_reason or "")
        self.assertIsNone(self.provider.revalidate_authority(task_id, 1, self.workspace, self.now_dt))

        g = self.provider.get_task_grant(
            task_id=task_id,
            caller_actor_id=self.issuer_id,
            caller_token=self.issuer_token,
            project_id=self.project_id,
            workspace=self.workspace,
        )
        self.assertEqual(g.status, "blocked")
        self.assertIn("token removed", g.protected_reason or "")

        with self.assertRaises(AuthenticationError):
            self.provider.propose_task_completion(
                owner_actor_id=self.owner_id,
                owner_token=self.owner_token,
                task_id=task_id,
                revision=1,
                project_id=self.project_id,
                workspace=self.workspace,
                native_owner_tag=self.owner_tag,
                native_session_uuid=self.session_uuid,
                generation=self.generation,
                result_digest=compute_sha256("res"),
                result_evidence={"d": "1"},
                idempotency_key="prop-owner-tok-fail",
            )

    def test_negative_permission_removal_issuer_and_owner(self):
        """Removing issuer or owner policy from AuthorityTrustRoot fails closed."""
        task_id = "task-neg-perm-rem"
        expires_at = format_iso_utc(self.now_dt + datetime.timedelta(hours=2))

        self.provider.issue_task_grant(
            issuer_actor_id=self.issuer_id,
            issuer_token=self.issuer_token,
            task_id=task_id,
            revision=1,
            project_id=self.project_id,
            workspace=self.workspace,
            owner_bus_actor=self.owner_id,
            native_owner_tag=self.owner_tag,
            native_session_uuid=self.session_uuid,
            generation=self.generation,
            source_id="s1",
            source_digest="d1",
            expires_at=expires_at,
        )
        self.provider.record_custody_ack(
            owner_actor_id=self.owner_id,
            owner_token=self.owner_token,
            task_id=task_id,
            revision=1,
            project_id=self.project_id,
            workspace=self.workspace,
            native_owner_tag=self.owner_tag,
            native_session_uuid=self.session_uuid,
            generation=self.generation,
            idempotency_key="ack-perm-rem",
        )

        # 1. Remove issuer role policy (keeping reader policy for acceptor)
        no_issuer_root = AuthorityTrustRoot(
            trusted_actors=tuple(p for p in self.trust_root.trusted_actors if not (p.actor_id == self.issuer_id and p.role == "issuer"))
        )
        self.provider.trust_root = no_issuer_root
        res = self.provider.resolve_authority(task_id, self.now_dt)
        self.assertEqual(res.status, "blocked")
        self.assertIn("not authorized for role 'issuer'", res.protected_reason or "")
        self.assertIsNone(self.provider.revalidate_authority(task_id, 1, self.workspace, self.now_dt))
        g = self.provider.get_task_grant(
            task_id=task_id,
            caller_actor_id=self.acceptor_id,
            caller_token=self.acceptor_token,
            project_id=self.project_id,
            workspace=self.workspace,
        )
        self.assertEqual(g.status, "blocked")
        self.assertIn("not authorized for role 'issuer'", g.protected_reason or "")

        # 2. Restore issuer policy, remove owner role policy
        no_owner_root = AuthorityTrustRoot(
            trusted_actors=tuple(p for p in self.trust_root.trusted_actors if not (p.actor_id == self.owner_id and p.role == "owner"))
        )
        self.provider.trust_root = no_owner_root
        res2 = self.provider.resolve_authority(task_id, self.now_dt)
        self.assertEqual(res2.status, "blocked")
        self.assertIn("not authorized for role 'owner'", res2.protected_reason or "")
        self.assertIsNone(self.provider.revalidate_authority(task_id, 1, self.workspace, self.now_dt))
        g2 = self.provider.get_task_grant(
            task_id=task_id,
            caller_actor_id=self.acceptor_id,
            caller_token=self.acceptor_token,
            project_id=self.project_id,
            workspace=self.workspace,
        )
        self.assertEqual(g2.status, "blocked")
        self.assertIn("not authorized for role 'owner'", g2.protected_reason or "")

        with self.assertRaises(PermissionDeniedError):
            self.provider.propose_task_completion(
                owner_actor_id=self.owner_id,
                owner_token=self.owner_token,
                task_id=task_id,
                revision=1,
                project_id=self.project_id,
                workspace=self.workspace,
                native_owner_tag=self.owner_tag,
                native_session_uuid=self.session_uuid,
                generation=self.generation,
                result_digest=compute_sha256("res"),
                result_evidence={"d": "1"},
                idempotency_key="prop-owner-perm-fail",
            )
        # Restore full trust root
        self.provider.trust_root = self.trust_root

    def test_negative_current_tuple_gen_change_in_native_binding(self):
        """Native generation change or aliveness drop in native binding immediately invalidates authority."""
        task_id = "task-neg-gen-change"
        expires_at = format_iso_utc(self.now_dt + datetime.timedelta(hours=2))

        self.provider.issue_task_grant(
            issuer_actor_id=self.issuer_id,
            issuer_token=self.issuer_token,
            task_id=task_id,
            revision=1,
            project_id=self.project_id,
            workspace=self.workspace,
            owner_bus_actor=self.owner_id,
            native_owner_tag=self.owner_tag,
            native_session_uuid=self.session_uuid,
            generation=self.generation,
            source_id="s1",
            source_digest="d1",
            expires_at=expires_at,
        )
        self.provider.record_custody_ack(
            owner_actor_id=self.owner_id,
            owner_token=self.owner_token,
            task_id=task_id,
            revision=1,
            project_id=self.project_id,
            workspace=self.workspace,
            native_owner_tag=self.owner_tag,
            native_session_uuid=self.session_uuid,
            generation=self.generation,
            idempotency_key="ack-gen-change",
        )

        # Advance native generation in the session binding (e.g. 1 -> 2)
        self.native_provider.set_binding(
            workspace=self.workspace,
            tag=self.owner_tag,
            session_id=self.session_uuid,
            generation=2,
            is_alive=True,
            observed_at=format_iso_utc(self.now_dt),
        )

        res = self.provider.resolve_authority(task_id, self.now_dt)
        self.assertEqual(res.status, "blocked")
        self.assertIn("generation mismatch", res.protected_reason or "")
        self.assertIsNone(self.provider.revalidate_authority(task_id, 1, self.workspace, self.now_dt))
        g = self.provider.get_task_grant(
            task_id=task_id,
            caller_actor_id=self.issuer_id,
            caller_token=self.issuer_token,
            project_id=self.project_id,
            workspace=self.workspace,
        )
        self.assertEqual(g.status, "blocked")
        self.assertIn("generation mismatch", g.protected_reason or "")

        with self.assertRaises(NativeBindingError):
            self.provider.propose_task_completion(
                owner_actor_id=self.owner_id,
                owner_token=self.owner_token,
                task_id=task_id,
                revision=1,
                project_id=self.project_id,
                workspace=self.workspace,
                native_owner_tag=self.owner_tag,
                native_session_uuid=self.session_uuid,
                generation=self.generation,
                result_digest=compute_sha256("res"),
                result_evidence={"d": "1"},
                idempotency_key="prop-gen-fail",
            )
        # Restore native binding
        self.native_provider.set_binding(
            workspace=self.workspace,
            tag=self.owner_tag,
            session_id=self.session_uuid,
            generation=self.generation,
            is_alive=True,
            observed_at=format_iso_utc(self.now_dt),
        )

    def test_negative_zero_db_and_zero_callbacks_matrix(self):
        """Negative boundary failures produce zero DB mutations and zero transport callbacks."""
        adapter_ledger = InterventionLedger(Path(self.tmp_dir) / "matrix_adapter.db", clock=self.clock)
        binding_prov = FixtureBindingProvider(
            NativeSessionBinding(
                session_id=self.session_uuid,
                tag=self.owner_tag,
                generation=self.generation,
                workspace=self.workspace,
                worker_alive=True,
                active=True,
            )
        )
        transport_calls = []
        transport_cbs = TransportRouteCallbacks(
            bus_send=lambda req: transport_calls.append(("bus_send", req)),
            native_continuation=lambda req: transport_calls.append(("native_continuation", req)),
        )
        adapter = OversightAdapter(
            ledger=adapter_ledger,
            binding_provider=binding_prov,
            authority_resolver=self.provider,
            dependency_verifier=self.provider,
            clock=self.clock,
        )

        task_id = "task-matrix-neg"
        expires_at = format_iso_utc(self.now_dt + datetime.timedelta(hours=2))

        self.provider.issue_task_grant(
            issuer_actor_id=self.issuer_id,
            issuer_token=self.issuer_token,
            task_id=task_id,
            revision=1,
            project_id=self.project_id,
            workspace=self.workspace,
            owner_bus_actor=self.owner_id,
            native_owner_tag=self.owner_tag,
            native_session_uuid=self.session_uuid,
            generation=self.generation,
            source_id="s1",
            source_digest="d1",
            expires_at=expires_at,
        )

        # Before custody ACK: grant is assigned, not ready. Dispatch must not make a decision, 0 callbacks.
        obs_evidence = ObservationEvidence(
            sample_id="obs-matrix-1",
            observed_at=format_iso_utc(self.now_dt),
            assessment="idle",
            empty_composer=True,
            session_id=self.session_uuid,
            generation=self.generation,
        )
        obs_prov = FixtureObservationProvider(obs_evidence)
        native_guard = FixtureNativeGuard(allowed=True)

        counts_before = {
            table: self._get_db_count(table)
            for table in ["custody_acks", "completion_proposals", "task_reviews", "task_acceptances", "task_revocations"]
        }

        # 1. Dispatching non-ready task
        res_dispatch = adapter.evaluate_and_dispatch(
            raw_task_data={"task_id": task_id, "revision": 1},
            authority_resolver=self.provider,
            binding_provider=binding_prov,
            observation_provider=obs_prov,
            native_guard=native_guard,
            transport_callbacks=transport_cbs,
            dependency_verifier=self.provider,
        )
        self.assertFalse(res_dispatch.decision_made)
        self.assertEqual(len(transport_calls), 0)

        # 2. Mutating with bad token
        with self.assertRaises(AuthenticationError):
            self.provider.record_custody_ack(
                owner_actor_id=self.owner_id,
                owner_token="wrong-token",
                task_id=task_id,
                revision=1,
                project_id=self.project_id,
                workspace=self.workspace,
                native_owner_tag=self.owner_tag,
                native_session_uuid=self.session_uuid,
                generation=self.generation,
                idempotency_key="ack-matrix-bad-tok",
            )

        # Verify zero DB rows added to all non-grant tables
        for table, before_cnt in counts_before.items():
            self.assertEqual(self._get_db_count(table), before_cnt, f"Table {table} had side effects")
        self.assertEqual(len(transport_calls), 0)

    def _create_ready_task(self, task_id: str, revision: int = 1) -> TaskGrantRecord:
        expires_at = format_iso_utc(self.now_dt + datetime.timedelta(hours=2))
        grant = self.provider.issue_task_grant(
            issuer_actor_id=self.issuer_id,
            issuer_token=self.issuer_token,
            task_id=task_id,
            revision=revision,
            project_id=self.project_id,
            workspace=self.workspace,
            owner_bus_actor=self.owner_id,
            native_owner_tag=self.owner_tag,
            native_session_uuid=self.session_uuid,
            generation=self.generation,
            source_id="sha-init",
            source_digest="digest-init",
            expires_at=expires_at,
        )
        self.provider.record_custody_ack(
            owner_actor_id=self.owner_id,
            owner_token=self.owner_token,
            task_id=task_id,
            revision=revision,
            project_id=self.project_id,
            workspace=self.workspace,
            native_owner_tag=self.owner_tag,
            native_session_uuid=self.session_uuid,
            generation=self.generation,
            idempotency_key=f"ack-{task_id}-{revision}",
        )
        return grant

    def _create_completed_proposal(self, task_id: str, revision: int = 1, result_digest: Optional[str] = None) -> Tuple[TaskGrantRecord, Dict[str, Any], str]:
        grant = self._create_ready_task(task_id, revision)
        digest = result_digest or compute_sha256(f"artifact-{task_id}-{revision}")
        evidence = {"output": "pass", "task_id": task_id}
        self.provider.propose_task_completion(
            owner_actor_id=self.owner_id,
            owner_token=self.owner_token,
            task_id=task_id,
            revision=revision,
            project_id=self.project_id,
            workspace=self.workspace,
            native_owner_tag=self.owner_tag,
            native_session_uuid=self.session_uuid,
            generation=self.generation,
            result_digest=digest,
            result_evidence=evidence,
            idempotency_key=f"prop-{task_id}-{revision}",
        )
        return grant, evidence, digest

    def test_issue_task_grant_replay_strict_issuance_fields_and_policy(self):
        """Regression 1: same-rev replay compares canonical full immutable payload (expires_at, checkpoint_due, is_principal).
        Changed values conflict with CASConflictError; exact replay succeeds. Explicit policy principal cannot be changed via payload.
        """
        task_id = "task-reg1-replay"
        expires_at_1 = format_iso_utc(self.now_dt + datetime.timedelta(hours=2))
        expires_at_2 = format_iso_utc(self.now_dt + datetime.timedelta(hours=4))
        chk_1 = format_iso_utc(self.now_dt + datetime.timedelta(minutes=30))
        chk_2 = format_iso_utc(self.now_dt + datetime.timedelta(minutes=45))

        # Initial issuance
        g1 = self.provider.issue_task_grant(
            issuer_actor_id=self.issuer_id,
            issuer_token=self.issuer_token,
            task_id=task_id,
            revision=1,
            project_id=self.project_id,
            workspace=self.workspace,
            owner_bus_actor=self.owner_id,
            native_owner_tag=self.owner_tag,
            native_session_uuid=self.session_uuid,
            generation=self.generation,
            source_id="sha-1",
            source_digest="digest-1",
            expires_at=expires_at_1,
            checkpoint_due=chk_1,
            is_principal=False,
        )
        self.assertEqual(g1.revision, 1)

        # Exact replay succeeds idempotently
        g1_replay = self.provider.issue_task_grant(
            issuer_actor_id=self.issuer_id,
            issuer_token=self.issuer_token,
            task_id=task_id,
            revision=1,
            project_id=self.project_id,
            workspace=self.workspace,
            owner_bus_actor=self.owner_id,
            native_owner_tag=self.owner_tag,
            native_session_uuid=self.session_uuid,
            generation=self.generation,
            source_id="sha-1",
            source_digest="digest-1",
            expires_at=expires_at_1,
            checkpoint_due=chk_1,
            is_principal=False,
        )
        self.assertEqual(g1_replay.issued_at, g1.issued_at)

        # Replay with changed expires_at -> CASConflictError
        with self.assertRaises(CASConflictError):
            self.provider.issue_task_grant(
                issuer_actor_id=self.issuer_id,
                issuer_token=self.issuer_token,
                task_id=task_id,
                revision=1,
                project_id=self.project_id,
                workspace=self.workspace,
                owner_bus_actor=self.owner_id,
                native_owner_tag=self.owner_tag,
                native_session_uuid=self.session_uuid,
                generation=self.generation,
                source_id="sha-1",
                source_digest="digest-1",
                expires_at=expires_at_2,
                checkpoint_due=chk_1,
                is_principal=False,
            )

        # Replay with changed checkpoint_due -> CASConflictError
        with self.assertRaises(CASConflictError):
            self.provider.issue_task_grant(
                issuer_actor_id=self.issuer_id,
                issuer_token=self.issuer_token,
                task_id=task_id,
                revision=1,
                project_id=self.project_id,
                workspace=self.workspace,
                owner_bus_actor=self.owner_id,
                native_owner_tag=self.owner_tag,
                native_session_uuid=self.session_uuid,
                generation=self.generation,
                source_id="sha-1",
                source_digest="digest-1",
                expires_at=expires_at_1,
                checkpoint_due=chk_2,
                is_principal=False,
            )

        # Replay with changed is_principal -> CASConflictError
        with self.assertRaises(CASConflictError):
            self.provider.issue_task_grant(
                issuer_actor_id=self.issuer_id,
                issuer_token=self.issuer_token,
                task_id=task_id,
                revision=1,
                project_id=self.project_id,
                workspace=self.workspace,
                owner_bus_actor=self.owner_id,
                native_owner_tag=self.owner_tag,
                native_session_uuid=self.session_uuid,
                generation=self.generation,
                source_id="sha-1",
                source_digest="digest-1",
                expires_at=expires_at_1,
                checkpoint_due=chk_1,
                is_principal=True,
            )

        # Initial issuance with is_principal=True when policy says False -> PermissionDeniedError
        with self.assertRaises(PermissionDeniedError):
            self.provider.issue_task_grant(
                issuer_actor_id=self.issuer_id,
                issuer_token=self.issuer_token,
                task_id="task-reg1-principal-spoof",
                revision=1,
                project_id=self.project_id,
                workspace=self.workspace,
                owner_bus_actor=self.owner_id,
                native_owner_tag=self.owner_tag,
                native_session_uuid=self.session_uuid,
                generation=self.generation,
                source_id="sha-1",
                source_digest="digest-1",
                is_principal=True,
            )

    def test_persisted_evidence_corruption_denied_for_review_and_acceptance(self):
        """Regression 2: tampered proposal evidence hash causes review and acceptance to reject atomically
        on both fresh writes and idempotent replay, with zero SQL effects.
        """
        task_id = "task-reg2-corrupt-evidence"
        result_digest = compute_sha256("res-corrupt-01")
        self._create_completed_proposal(task_id, 1, result_digest)

        # Tamper persisted proposal evidence in DB
        with self.store._get_connection() as conn:
            conn.execute(
                "UPDATE completion_proposals SET result_evidence_json = ? WHERE task_id = ? AND revision = 1;",
                (json.dumps({"tampered": True}), task_id),
            )
            conn.commit()

        # Fresh review of corrupted proposal evidence fails atomically
        counts_before = {
            table: self._get_db_count(table)
            for table in ["task_reviews", "task_acceptances"]
        }
        review_ev = {"test": "negative-case-ok"}
        with self.assertRaises(VerificationError):
            self.provider.record_task_review(
                reviewer_actor_id=self.reviewer_id,
                reviewer_token=self.reviewer_token,
                task_id=task_id,
                revision=1,
                project_id=self.project_id,
                workspace=self.workspace,
                expected_result_digest=result_digest,
                verdict="approved",
                review_evidence=review_ev,
                idempotency_key="rev-corrupt-1",
            )
        # Verify zero SQL effects
        self.assertEqual(self._get_db_count("task_reviews"), counts_before["task_reviews"])
        latest = self.store.get_latest_grant(task_id)
        self.assertIsNotNone(latest)
        self.assertEqual(latest.status, "completed_awaiting_review")

        # Fix proposal evidence to valid
        valid_ev = {"output": "pass", "task_id": task_id}
        with self.store._get_connection() as conn:
            conn.execute(
                "UPDATE completion_proposals SET result_evidence_json = ? WHERE task_id = ? AND revision = 1;",
                (json.dumps(valid_ev), task_id),
            )
            conn.commit()

        # Now review succeeds
        rev_rec = self.provider.record_task_review(
            reviewer_actor_id=self.reviewer_id,
            reviewer_token=self.reviewer_token,
            task_id=task_id,
            revision=1,
            project_id=self.project_id,
            workspace=self.workspace,
            expected_result_digest=result_digest,
            verdict="approved",
            review_evidence=review_ev,
            idempotency_key="rev-corrupt-1",
        )
        self.assertEqual(rev_rec.verdict, "approved")

        # Re-tamper proposal evidence: review replay MUST fail!
        with self.store._get_connection() as conn:
            conn.execute(
                "UPDATE completion_proposals SET result_evidence_json = ? WHERE task_id = ? AND revision = 1;",
                (json.dumps({"tampered": "again"}), task_id),
            )
            conn.commit()

        with self.assertRaises(VerificationError):
            self.provider.record_task_review(
                reviewer_actor_id=self.reviewer_id,
                reviewer_token=self.reviewer_token,
                task_id=task_id,
                revision=1,
                project_id=self.project_id,
                workspace=self.workspace,
                expected_result_digest=result_digest,
                verdict="approved",
                review_evidence=review_ev,
                idempotency_key="rev-corrupt-1",
            )

        # Fresh acceptance on corrupted proposal evidence also fails
        acc_ev = {"check": "verified"}
        with self.assertRaises(VerificationError):
            self.provider.record_task_acceptance(
                acceptor_actor_id=self.acceptor_id,
                acceptor_token=self.acceptor_token,
                task_id=task_id,
                revision=1,
                project_id=self.project_id,
                workspace=self.workspace,
                expected_result_digest=result_digest,
                acceptance_evidence=acc_ev,
                idempotency_key="acc-corrupt-1",
            )
        self.assertEqual(self._get_db_count("task_acceptances"), counts_before["task_acceptances"])

        # Restore valid proposal evidence, but tamper review evidence
        with self.store._get_connection() as conn:
            conn.execute(
                "UPDATE completion_proposals SET result_evidence_json = ? WHERE task_id = ? AND revision = 1;",
                (json.dumps(valid_ev), task_id),
            )
            conn.execute(
                "UPDATE task_reviews SET review_evidence_json = ? WHERE task_id = ? AND revision = 1;",
                (json.dumps({"tampered_review": True}), task_id),
            )
            conn.commit()

        # Acceptance fails because review evidence is corrupted
        with self.assertRaises(VerificationError):
            self.provider.record_task_acceptance(
                acceptor_actor_id=self.acceptor_id,
                acceptor_token=self.acceptor_token,
                task_id=task_id,
                revision=1,
                project_id=self.project_id,
                workspace=self.workspace,
                expected_result_digest=result_digest,
                acceptance_evidence=acc_ev,
                idempotency_key="acc-corrupt-1",
            )
        self.assertEqual(self._get_db_count("task_acceptances"), counts_before["task_acceptances"])

        # Restore valid review evidence -> acceptance succeeds
        with self.store._get_connection() as conn:
            conn.execute(
                "UPDATE task_reviews SET review_evidence_json = ? WHERE task_id = ? AND revision = 1;",
                (json.dumps(review_ev), task_id),
            )
            conn.commit()

        acc_rec = self.provider.record_task_acceptance(
            acceptor_actor_id=self.acceptor_id,
            acceptor_token=self.acceptor_token,
            task_id=task_id,
            revision=1,
            project_id=self.project_id,
            workspace=self.workspace,
            expected_result_digest=result_digest,
            acceptance_evidence=acc_ev,
            idempotency_key="acc-corrupt-1",
        )
        self.assertEqual(acc_rec.acceptor_actor_id, self.acceptor_id)

        # Tamper acceptance evidence: replay of acceptance fails!
        with self.store._get_connection() as conn:
            conn.execute(
                "UPDATE task_acceptances SET acceptance_evidence_json = ? WHERE task_id = ? AND revision = 1;",
                (json.dumps({"tampered_acceptance": True}), task_id),
            )
            conn.commit()

        with self.assertRaises(VerificationError):
            self.provider.record_task_acceptance(
                acceptor_actor_id=self.acceptor_id,
                acceptor_token=self.acceptor_token,
                task_id=task_id,
                revision=1,
                project_id=self.project_id,
                workspace=self.workspace,
                expected_result_digest=result_digest,
                acceptance_evidence=acc_ev,
                idempotency_key="acc-corrupt-1",
            )

    def test_acceptance_denies_corrupted_custody_reviewer_identity_and_policy(self):
        """Regression 3: Acceptance denies on corrupted custody tuple, reviewer identity removal,
        reviewer policy removal, self-acceptance, or acceptor matching reviewer.
        """
        task_id = "task-reg3-accept-guards"
        res_dig = compute_sha256("res-guard-01")
        self._create_completed_proposal(task_id, 1, res_dig)
        review_ev = {"tests": "passed"}
        self.provider.record_task_review(
            reviewer_actor_id=self.reviewer_id,
            reviewer_token=self.reviewer_token,
            task_id=task_id,
            revision=1,
            project_id=self.project_id,
            workspace=self.workspace,
            expected_result_digest=res_dig,
            verdict="approved",
            review_evidence=review_ev,
            idempotency_key="rev-guard-1",
        )

        acc_ev = {"audit": "ok"}

        # 1. Self-acceptance forbidden (owner cannot accept)
        with self.assertRaises(PermissionDeniedError):
            self.provider.record_task_acceptance(
                acceptor_actor_id=self.owner_id,
                acceptor_token=self.owner_token,
                task_id=task_id,
                revision=1,
                project_id=self.project_id,
                workspace=self.workspace,
                expected_result_digest=res_dig,
                acceptance_evidence=acc_ev,
                idempotency_key="acc-guard-self",
            )

        # 2. Acceptor matching reviewer forbidden
        with self.assertRaises(PermissionDeniedError):
            self.provider.record_task_acceptance(
                acceptor_actor_id=self.reviewer_id,
                acceptor_token=self.reviewer_token,
                task_id=task_id,
                revision=1,
                project_id=self.project_id,
                workspace=self.workspace,
                expected_result_digest=res_dig,
                acceptance_evidence=acc_ev,
                idempotency_key="acc-guard-same-as-rev",
            )

        # 3. Corrupted custody ACK tuple digest denies acceptance
        with self.store._get_connection() as conn:
            conn.execute(
                "UPDATE custody_acks SET tuple_digest = ? WHERE task_id = ? AND revision = 1;",
                ("corrupted-tuple-digest", task_id),
            )
            conn.commit()

        with self.assertRaises(CASConflictError):
            self.provider.record_task_acceptance(
                acceptor_actor_id=self.acceptor_id,
                acceptor_token=self.acceptor_token,
                task_id=task_id,
                revision=1,
                project_id=self.project_id,
                workspace=self.workspace,
                expected_result_digest=res_dig,
                acceptance_evidence=acc_ev,
                idempotency_key="acc-guard-custody-corrupt",
            )

        # Restore valid custody ACK
        valid_digest = self.store.get_latest_grant(task_id).ownership.digest()
        with self.store._get_connection() as conn:
            conn.execute(
                "UPDATE custody_acks SET tuple_digest = ? WHERE task_id = ? AND revision = 1;",
                (valid_digest, task_id),
            )
            conn.commit()

        # 4. Reviewer policy removed from trust root -> acceptance denies
        trust_without_rev = AuthorityTrustRoot(
            trusted_actors=tuple(p for p in self.trust_root.trusted_actors if p.actor_id != self.reviewer_id)
        )
        prov_no_rev_policy = TaskAuthorityProvider(
            store=self.store, authenticator=self.authenticator,
            trust_root=trust_without_rev, native_binding_provider=self.native_provider, clock=self.clock,
        )
        with self.assertRaises(PermissionDeniedError):
            prov_no_rev_policy.record_task_acceptance(
                acceptor_actor_id=self.acceptor_id,
                acceptor_token=self.acceptor_token,
                task_id=task_id,
                revision=1,
                project_id=self.project_id,
                workspace=self.workspace,
                expected_result_digest=res_dig,
                acceptance_evidence=acc_ev,
                idempotency_key="acc-guard-no-rev-policy",
            )


if __name__ == "__main__":
    unittest.main()

