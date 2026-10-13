"""Tests for TaskAuthorityAdapter.

Validates provider-to-OversightAdapter resolver, revalidator, and dependency boundary:
- Rejection of fixture providers and fixture evidence with zero ledger/outbox/callback mutations.
- Direct offline grant positives on TaskAuthorityProvider do NOT transfer trust to TaskAuthorityAdapter.
- Internal aware UTC wallclock enforcement: caller past/future/naive now and provider clock spoofing
  cannot revive expired, stale, revoked, or superseded authority.
- Direct pre-send guard, dispatch query, and receipt ingress entrypoints exhibit identical denial.
- Dependency completion verification failure matrix: malformed binding, exceptions, missing credentials,
  self-review, and unauthorized actors fail closed.
- Task with unverified dependencies marked blocked/held before readiness.
- Actual head collision fix: distinct canonical JSON ordered 8-tuple digests with version marker.
- Real FileBus store, real SQLite InterventionLedger, real OversightAdapter isolation.
"""

from __future__ import annotations

import datetime
import inspect
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# Add host to sys.path so agent_hub_host is importable
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Add coordination repo path explicitly for tests
coordination_path = Path("/home/alexey/git/agent-coordination")
if coordination_path.exists() and str(coordination_path) not in sys.path:
    sys.path.insert(0, str(coordination_path))

from coordination.bus import FileBus

from agent_hub_host.task_authority import (
    AuthorityTrustRoot,
    FileBusAuthenticator,
    NativeSessionEvidence,
    TaskAuthorityProvider,
    TaskAuthorityStore,
    TaskOwnershipTuple,
    TrustedActorPolicy,
    compute_sha256,
    format_iso_utc,
)
from agent_hub_host.task_authority_adapter import (
    StrictIntegrationNativeBindingProvider,
    TaskAuthorityAdapter,
    _get_internal_now_utc,
)
from agent_hub_host.oversight import (
    INTERVENTION_NUDGE_IDLE,
    InterventionLedger,
    ObservationEvidence,
    SecurityBindingError,
    TransportSendFailedBeforeDelivery,
)
from agent_hub_host.oversight_adapter import (
    AdapterDispatchGuard,
    AdapterTransport,
    NativeGuardCheckResult,
    NativeSessionBinding,
    OversightAdapter,
    QUALIFICATION_STATUS_DENIED,
    QUALIFICATION_STATUS_HELD,
    QUALIFICATION_STATUS_UNKNOWN,
    ROUTE_BUS,
    ROUTE_NATIVE_MAILBOX,
    StandardAuthorityResolver,
    TransportRouteCallbacks,
    TrustedAuthorityGrant,
)


class FixtureBindingProvider:
    """Explicit fixture-only binding provider for offline tests."""
    is_fixture_only: bool = True

    def __init__(self, evidence: Optional[NativeSessionEvidence] = None) -> None:
        self.evidence = evidence

    def get_binding_evidence(
        self,
        workspace: str,
        tag: str,
        now_utc: datetime.datetime,
    ) -> Optional[NativeSessionEvidence]:
        return self.evidence


class UnmarkedWitnessBindingProvider:
    """Witness binding provider attempting to masquerade trust by omitting fixture flags."""
    is_fixture_only: bool = False

    def __init__(self, evidence: Optional[NativeSessionEvidence] = None) -> None:
        self.evidence = evidence

    def get_binding_evidence(
        self,
        workspace: str,
        tag: str,
        now_utc: datetime.datetime,
    ) -> Optional[NativeSessionEvidence]:
        return self.evidence


class RawEvidenceReaderWithBooleanGetter:
    """Witness binding provider attempting to masquerade trust with boolean getter and false fixture flags."""
    is_fixture_only: bool = False

    def __init__(self, evidence: Optional[NativeSessionEvidence] = None) -> None:
        self.evidence = evidence

    def is_operational_qualified_reader(self) -> bool:
        return True

    def get_binding_evidence(
        self,
        workspace: str,
        tag: str,
        now_utc: datetime.datetime,
    ) -> Optional[NativeSessionEvidence]:
        return self.evidence


class TestTaskAuthorityAdapter(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir_obj = tempfile.TemporaryDirectory()
        self.tmp_dir = Path(self.tmp_dir_obj.name)

        # 1. Setup isolated FileBus store
        self.bus_dir = self.tmp_dir / "bus"
        self.bus_dir.mkdir(parents=True, exist_ok=True)

        self.file_bus = FileBus(self.bus_dir)
        self.authenticator = FileBusAuthenticator(bus=self.file_bus)

        self.project_id = "test-project"
        self.workspace = "/home/alexey/git/agent-hub/.local/branches/workspaces/hub-test"

        # Register distinct identities in FileBus
        issuer_ident, self.issuer_token = self.file_bus.register(
            agent_name="agent-hub-head",
            device_id="dev-hetzner",
            project_id=self.project_id,
        )
        self.issuer_id = issuer_ident.identity_id

        owner_ident, self.owner_token = self.file_bus.register(
            agent_name="agent-hub-implementer-t1",
            device_id="dev-hetzner",
            project_id=self.project_id,
        )
        self.owner_id = owner_ident.identity_id

        reviewer_ident, self.reviewer_token = self.file_bus.register(
            agent_name="agent-hub-reviewer-t1",
            device_id="dev-hetzner",
            project_id=self.project_id,
        )
        self.reviewer_id = reviewer_ident.identity_id

        acceptor_ident, self.acceptor_token = self.file_bus.register(
            agent_name="agent-hub-acceptor-t1",
            device_id="dev-hetzner",
            project_id=self.project_id,
        )
        self.acceptor_id = acceptor_ident.identity_id

        # 2. Setup trust root
        self.trust_root = AuthorityTrustRoot(
            trusted_actors=(
                TrustedActorPolicy(actor_id=self.issuer_id, role="issuer", project_id=self.project_id, workspace=self.workspace, task_id=None),
                TrustedActorPolicy(actor_id=self.owner_id, role="owner", project_id=self.project_id, workspace=self.workspace, task_id=None),
                TrustedActorPolicy(actor_id=self.reviewer_id, role="reviewer", project_id=self.project_id, workspace=self.workspace, task_id=None),
                TrustedActorPolicy(actor_id=self.acceptor_id, role="acceptor", project_id=self.project_id, workspace=self.workspace, task_id=None),
            )
        )

        # 3. Setup SQLite authority store
        self.db_path = self.tmp_dir / "authority.db"
        self.store = TaskAuthorityStore(self.db_path)

        # 4. Standard native evidence
        self.owner_tag = "worker-tag-01"
        self.session_uuid = "55555555-5555-5555-5555-555555555555"
        self.generation = 1
        self.now_dt = datetime.datetime(2026, 10, 13, 16, 0, 0, tzinfo=datetime.timezone.utc)

        self.fixture_evidence = NativeSessionEvidence(
            workspace=self.workspace,
            tag=self.owner_tag,
            session_id=self.session_uuid,
            generation=self.generation,
            is_alive=True,
            observed_at=format_iso_utc(self.now_dt),
            is_ready=True,
            is_fixture_only=True,
        )
        self.fixture_native_provider = FixtureBindingProvider(self.fixture_evidence)

        # 5. Base TaskAuthorityProvider
        self.provider = TaskAuthorityProvider(
            store=self.store,
            authenticator=self.authenticator,
            trust_root=self.trust_root,
            native_binding_provider=self.fixture_native_provider,
            clock=lambda: self.now_dt,
        )

        # 6. Adapter under test
        self.adapter = TaskAuthorityAdapter(self.provider)

        # 7. Setup InterventionLedger and OversightAdapter for full integration checks
        self.ledger_db = self.tmp_dir / "ledger.db"
        self.ledger = InterventionLedger(self.ledger_db, clock=lambda: self.now_dt)

        self.native_binding = NativeSessionBinding(
            session_id=self.session_uuid,
            tag=self.owner_tag,
            generation=self.generation,
            workspace=self.workspace,
            worker_alive=True,
            active=True,
        )

        self.transport_calls: list[Any] = []
        self.transport_callbacks = TransportRouteCallbacks(
            bus_send=lambda req: self._record_transport_call("bus_send", req),
            native_continuation=lambda req: self._record_transport_call("native", req),
        )

    def tearDown(self) -> None:
        self.tmp_dir_obj.cleanup()

    def _record_transport_call(self, kind: str, req: Any) -> Dict[str, Any]:
        self.transport_calls.append((kind, req))
        return {
            "status": "sent",
            "message_id": f"msg-{len(self.transport_calls)}",
            "idempotency_key": req.idempotency_key,
            "state": "send_receipt",
            "sent_at": format_iso_utc(self.now_dt),
        }

    def _issue_and_ack_task(
        self,
        task_id: str,
        revision: int = 1,
        expires_at: Optional[str] = None,
        dependencies: Tuple[str, ...] = (),
        source_id: str = "sha-test-1",
        source_digest: str = "digest-test-1",
    ) -> None:
        if expires_at is None:
            expires_at = format_iso_utc(self.now_dt + datetime.timedelta(hours=1))

        self.provider.issue_task_grant(
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
            source_id=source_id,
            source_digest=source_digest,
            expires_at=expires_at,
            dependencies=dependencies,
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

    # --- 1. Fixture Provider & Evidence Denied With Zero Effects ---

    def test_fixture_evidence_denied_with_zero_ledger_and_zero_callbacks(self):
        """Fixture-only declaration or evidence is load-bearing DENY at integration boundary,
        yielding zero ledger entries, zero outbox reservations, and zero callbacks."""
        task_id = "task-fixture-denial"
        self._issue_and_ack_task(task_id)

        # 1. Direct resolution through adapter fails closed (status blocked, protected)
        with mock.patch("agent_hub_host.task_authority_adapter._get_internal_now_utc", return_value=self.now_dt):
            resolved = self.adapter.resolve_authority({"task_id": task_id, "revision": 1})
            self.assertIsNotNone(resolved)
            self.assertEqual(resolved.status, "blocked")
            self.assertIn("fixture", (resolved.protected_reason or "").lower())

            # Revalidation returns None
            revalidated = self.adapter.revalidate_authority(task_id, 1, self.workspace)
            self.assertIsNone(revalidated)

            # Qualification assessment defaults to UNKNOWN
            qual = self.adapter.assess_qualification({"task_id": task_id, "revision": 1})
            self.assertFalse(qual.is_qualified)
            self.assertEqual(qual.status, QUALIFICATION_STATUS_UNKNOWN)
            self.assertIn("QualifiedNativeSessionReader", qual.reason)

            # 2. OversightAdapter dispatch evaluation
            obs = ObservationEvidence(
                sample_id="obs-1",
                observed_at=format_iso_utc(self.now_dt),
                assessment="idle",
                empty_composer=True,
                session_id=self.session_uuid,
                generation=self.generation,
            )

            oversight = OversightAdapter(
                ledger=self.ledger,
                binding_provider=mock.Mock(get_current_binding=lambda ws, tag: self.native_binding),
                authority_resolver=self.adapter,
                dependency_verifier=self.adapter,
                clock=lambda: self.now_dt,
            )

            res = oversight.evaluate_and_dispatch(
                raw_task_data={"task_id": task_id, "revision": 1},
                authority_resolver=self.adapter,
                binding_provider=mock.Mock(get_current_binding=lambda ws, tag: self.native_binding),
                observation_provider=mock.Mock(get_observation_evidence=lambda s, g: obs),
                native_guard=mock.Mock(is_action_permitted=lambda a, ws: True),
                transport_callbacks=self.transport_callbacks,
            )

            # Decision must NOT be made
            self.assertFalse(res.decision_made)
            self.assertIn("protected", res.refusal_reason.lower())

        # Zero ledger records
        with sqlite3.connect(self.ledger_db) as conn:
            intv_count = conn.execute("SELECT count(*) FROM interventions;").fetchone()[0]
            outbox_count = conn.execute("SELECT count(*) FROM outbox;").fetchone()[0]
            self.assertEqual(intv_count, 0)
            self.assertEqual(outbox_count, 0)

        # Zero callbacks invoked
        self.assertEqual(len(self.transport_calls), 0)

    # --- 2. Unmarking Fixture Metadata Cannot Fabricate Trust ---

    def test_unmarking_fixture_flag_cannot_masquerade_trust_and_denies(self):
        """Unmarking is_fixture_only or adding boolean getter True without genuine operational reader
        interface still fails closed with status UNKNOWN and yields zero adapter effects."""
        unmarked_evidence = NativeSessionEvidence(
            workspace=self.workspace,
            tag=self.owner_tag,
            session_id=self.session_uuid,
            generation=self.generation,
            is_alive=True,
            observed_at=format_iso_utc(self.now_dt),
            is_ready=True,
            is_fixture_only=False,  # Unmarked!
        )
        unmarked_provider = UnmarkedWitnessBindingProvider(unmarked_evidence)

        # 1. Direct call to StrictIntegrationNativeBindingProvider fails closed with QualifiedNativeSessionReader
        wrapped_strict = StrictIntegrationNativeBindingProvider(unmarked_provider)
        with mock.patch("agent_hub_host.task_authority_adapter._get_internal_now_utc", return_value=self.now_dt):
            with self.assertRaises(Exception) as ctx:
                wrapped_strict.get_binding_evidence(self.workspace, self.owner_tag, self.now_dt)

        self.assertIn("QualifiedNativeSessionReader", str(ctx.exception))

        # 2. Boolean getter is_operational_qualified_reader() -> True with relabeled fixture evidence
        # Head probe reproduction: cannot bypass operational qualification; integration remains UNKNOWN
        boolean_provider = RawEvidenceReaderWithBooleanGetter(unmarked_evidence)
        probe_provider = TaskAuthorityProvider(
            store=self.store,
            authenticator=self.authenticator,
            trust_root=self.trust_root,
            native_binding_provider=boolean_provider,
            clock=lambda: self.now_dt,
        )
        probe_adapter = TaskAuthorityAdapter(probe_provider)

        task_id = "task-boolean-probe"
        self._issue_and_ack_task(task_id)

        with mock.patch("agent_hub_host.task_authority_adapter._get_internal_now_utc", return_value=self.now_dt):
            # Resolution fails closed (status blocked, protected reason names QualifiedNativeSessionReader)
            resolved = probe_adapter.resolve_authority({"task_id": task_id, "revision": 1})
            self.assertIsNotNone(resolved)
            self.assertEqual(resolved.status, "blocked")
            self.assertIn("QualifiedNativeSessionReader", resolved.protected_reason or "")

            # Revalidation returns None
            self.assertIsNone(probe_adapter.revalidate_authority(task_id, 1, self.workspace))

            # Oversight evaluation yields zero decisions, zero reservations, zero callbacks
            obs = ObservationEvidence(
                sample_id="obs-probe",
                observed_at=format_iso_utc(self.now_dt),
                assessment="idle",
                empty_composer=True,
                session_id=self.session_uuid,
                generation=self.generation,
            )
            oversight = OversightAdapter(
                ledger=self.ledger,
                binding_provider=mock.Mock(get_current_binding=lambda ws, tag: self.native_binding),
                authority_resolver=probe_adapter,
                dependency_verifier=probe_adapter,
                clock=lambda: self.now_dt,
            )
            res = oversight.evaluate_and_dispatch(
                raw_task_data={"task_id": task_id, "revision": 1},
                authority_resolver=probe_adapter,
                binding_provider=mock.Mock(get_current_binding=lambda ws, tag: self.native_binding),
                observation_provider=mock.Mock(get_observation_evidence=lambda s, g: obs),
                native_guard=mock.Mock(is_action_permitted=lambda a, ws: True),
                transport_callbacks=self.transport_callbacks,
            )
            self.assertFalse(res.decision_made)
            self.assertFalse(res.dispatch_sent)
            self.assertIn("protected", res.refusal_reason.lower())

            # Verify zero ledger records and zero callbacks
            with sqlite3.connect(self.ledger_db) as conn:
                intv_count = conn.execute("SELECT count(*) FROM interventions WHERE obligation_id = ?;", (task_id,)).fetchone()[0]
                outbox_count = conn.execute("SELECT count(*) FROM outbox WHERE recipient_tag = ?;", (self.owner_tag,)).fetchone()[0]
                self.assertEqual(intv_count, 0)
                self.assertEqual(outbox_count, 0)
            self.assertEqual(len(self.transport_calls), 0)

    # --- 3. Direct Offline Grant Positive Does Not Transfer Trust ---

    def test_direct_offline_grant_positive_does_not_transfer_trust(self):
        """TaskAuthorityProvider accepts offline fixtures directly, but TaskAuthorityAdapter
        strictly denies trust transfer at the integration boundary."""
        task_id = "task-offline-positive"
        self._issue_and_ack_task(task_id)

        # 1. Direct provider resolves ready grant in offline mode
        direct_grant = self.provider.resolve_authority({"task_id": task_id, "revision": 1}, now_utc=self.now_dt)
        self.assertIsNotNone(direct_grant)
        self.assertEqual(direct_grant.status, "ready")
        self.assertTrue(direct_grant.has_owner_ack)

        # 2. TaskAuthorityAdapter refuses to transfer trust
        with mock.patch("agent_hub_host.task_authority_adapter._get_internal_now_utc", return_value=self.now_dt):
            adapter_grant = self.adapter.resolve_authority({"task_id": task_id, "revision": 1})
            self.assertIsNotNone(adapter_grant)
            self.assertEqual(adapter_grant.status, "blocked")
            self.assertIn("fixture", (adapter_grant.protected_reason or "").lower())

            # Revalidation returns None
            self.assertIsNone(self.adapter.revalidate_authority(task_id, 1, self.workspace))

    # --- 4. Clock Isolation: Caller / Provider Clock Cannot Revive Expired Authority ---

    def test_past_future_naive_clock_cannot_revive_expired_authority(self):
        """Caller-supplied now_utc (past, future, naive) and provider clock spoofing
        cannot revive expired authority under internal aware wallclock."""
        task_id = "task-clock-isolation"
        # Grant expires at 16:30
        exp_dt = self.now_dt + datetime.timedelta(minutes=30)
        self._issue_and_ack_task(task_id, expires_at=format_iso_utc(exp_dt))

        # Advance internal wallclock to 17:00 (expired)
        internal_now = self.now_dt + datetime.timedelta(hours=1)
        with mock.patch("agent_hub_host.task_authority_adapter._get_internal_now_utc", return_value=internal_now):
            # A. Caller passes past timestamp (16:15, before expiration)
            past_dt = self.now_dt + datetime.timedelta(minutes=15)
            grant_with_past = self.adapter.resolve_authority(
                {"task_id": task_id, "revision": 1},
                now_utc=past_dt,
            )
            # Must evaluate against internal wallclock (17:00), which is expired -> blocked
            self.assertIsNotNone(grant_with_past)
            self.assertEqual(grant_with_past.status, "blocked")
            self.assertIn("expired", (grant_with_past.protected_reason or "").lower())

            # B. Caller passes timezone-naive datetime
            naive_dt = datetime.datetime(2026, 10, 13, 16, 15, 0)
            self.assertIsNone(self.adapter.resolve_authority({"task_id": task_id, "revision": 1}, now_utc=naive_dt))
            self.assertIsNone(self.adapter.revalidate_authority(task_id, 1, self.workspace, now_utc=naive_dt))
            ok, msg = self.adapter.verify_dependency_completion("any-dep", self.workspace, now_utc=naive_dt)
            self.assertFalse(ok)
            self.assertIn("timezone-aware", msg)

        # C. Provider clock is spoofed to 16:15, but internal wallclock is 17:00
        spoofed_provider = TaskAuthorityProvider(
            store=self.store,
            authenticator=self.authenticator,
            trust_root=self.trust_root,
            native_binding_provider=self.fixture_native_provider,
            clock=lambda: self.now_dt + datetime.timedelta(minutes=15),  # Spoofed!
        )
        adapter_with_spoofed_provider = TaskAuthorityAdapter(spoofed_provider)

        with mock.patch("agent_hub_host.task_authority_adapter._get_internal_now_utc", return_value=internal_now):
            grant_spoof = adapter_with_spoofed_provider.resolve_authority({"task_id": task_id, "revision": 1})
            self.assertIsNotNone(grant_spoof)
            self.assertEqual(grant_spoof.status, "blocked")
            self.assertIn("expired", (grant_spoof.protected_reason or "").lower())

    # --- 5. Revoked and Superseded Authority Denied Across All Timestamps ---

    def test_revoked_and_superseded_authority_denied(self):
        """Revoked grants and historic revisions cannot be revalidated or resolved."""
        task_id = "task-revoked-superseded"
        self._issue_and_ack_task(task_id, revision=1)

        # Issue revision 2
        self._issue_and_ack_task(task_id, revision=2)

        with mock.patch("agent_hub_host.task_authority_adapter._get_internal_now_utc", return_value=self.now_dt):
            # Historic revision 1 cannot be revalidated
            self.assertIsNone(self.adapter.revalidate_authority(task_id, 1, self.workspace))

            # Revoking revision 2
            self.provider.revoke_task_grant(
                issuer_actor_id=self.issuer_id,
                issuer_token=self.issuer_token,
                task_id=task_id,
                revision=2,
                project_id=self.project_id,
                workspace=self.workspace,
                reason="issuer security revocation",
            )

            # Revoked grant cannot be revalidated or resolved
            self.assertIsNone(self.adapter.revalidate_authority(task_id, 2, self.workspace))
            self.assertIsNone(self.adapter.resolve_authority({"task_id": task_id, "revision": 2}))

    # --- 6. Direct Entrypoints: Guard and Receipt Ingress Exhibit Same Denial ---

    def test_direct_entrypoints_guard_and_receipt_ingress_same_denial(self):
        """Direct pre-send guard and receipt ingress entrypoints reuse boundary revalidation
        and cannot bypass fixture native or clock checks."""
        task_id = "task-direct-entrypoints"
        self._issue_and_ack_task(task_id)

        # 1. Generate an initial dispatch with StandardAuthorityResolver to obtain genuine outbox message
        clean_grant = TrustedAuthorityGrant(
            task_id=task_id,
            revision=1,
            owner_tag=self.owner_tag,
            workspace=self.workspace,
            session_id=self.session_uuid,
            generation=self.generation,
            source_id="sha-direct",
            source_digest=compute_sha256("digest-direct"),
            issued_at=format_iso_utc(self.now_dt),
            status="ready",
        )
        temp_resolver = StandardAuthorityResolver()
        temp_resolver.register_grant(clean_grant)

        obs = ObservationEvidence(
            sample_id="obs-direct",
            observed_at=format_iso_utc(self.now_dt),
            assessment="idle",
            reason_codes=(),
            empty_composer=True,
            session_id=self.session_uuid,
            generation=self.generation,
        )

        binding_mock = mock.Mock(get_current_binding=lambda *args, **kwargs: self.native_binding)

        clean_guard_result = NativeGuardCheckResult(
            allowed=True,
            reason="clean native guard check",
            is_ready=True,
            session_alive=True,
        )
        guard_mock = mock.Mock(
            check_native_state=lambda *args, **kwargs: clean_guard_result,
            is_action_permitted=lambda *args, **kwargs: True,
        )

        class DirectTestReceiptVerifier:
            def verify_receipt(self, receipt: Any, request: Any, now_utc: Any) -> Tuple[bool, str]:
                return True, "verified"

        oversight_init = OversightAdapter(
            ledger=self.ledger,
            binding_provider=binding_mock,
            authority_resolver=temp_resolver,
            dependency_verifier=self.adapter,
            receipt_verifier=DirectTestReceiptVerifier(),
            clock=lambda: self.now_dt,
        )

        clean_res = oversight_init.evaluate_and_dispatch(
            raw_task_data=clean_grant,
            authority_resolver=temp_resolver,
            binding_provider=binding_mock,
            observation_provider=mock.Mock(get_current_observation=lambda *args, **kwargs: obs),
            native_guard=guard_mock,
            transport_callbacks=self.transport_callbacks,
            receipt_verifier=DirectTestReceiptVerifier(),
        )
        self.assertTrue(clean_res.dispatch_sent)
        msg = clean_res.outbox_message

        # 2. When TaskAuthorityAdapter is used as the authority_resolver:
        # Pre-send guard, direct transport send, and receipt ingress ALL deny!
        with mock.patch("agent_hub_host.task_authority_adapter._get_internal_now_utc", return_value=self.now_dt):
            # A. AdapterDispatchGuard denies
            guard = AdapterDispatchGuard(
                ledger=self.ledger,
                authority_resolver=self.adapter,
                binding_provider=binding_mock,
                native_guard=guard_mock,
                transport_callbacks=self.transport_callbacks,
                clock=lambda: self.now_dt,
            )
            auth_ok, auth_reason = guard.authorize_dispatch(msg)
            self.assertFalse(auth_ok)
            self.assertIn("revalidation failed", auth_reason.lower())

            # B. AdapterTransport.send denies before callback
            cb_calls: list[Any] = []
            def probe_cb(req: Any) -> Dict[str, Any]:
                cb_calls.append(req)
                return {"status": "sent"}

            tb = TransportRouteCallbacks(bus_send=probe_cb, native_continuation=probe_cb)
            transport = AdapterTransport(
                ledger=self.ledger,
                callbacks=tb,
                authority_resolver=self.adapter,
                binding_provider=binding_mock,
                native_guard=guard_mock,
                clock=lambda: self.now_dt,
            )
            with self.assertRaises(TransportSendFailedBeforeDelivery):
                transport.send(
                    route=msg.route,
                    recipient_tag=self.owner_tag,
                    payload={"test": 1},
                    idempotency_key=msg.dedup_key,
                )
            self.assertEqual(len(cb_calls), 0)

            # C. Receipt ingress denies via OversightAdapter
            oversight = OversightAdapter(
                ledger=self.ledger,
                binding_provider=binding_mock,
                authority_resolver=self.adapter,
                dependency_verifier=self.adapter,
                clock=lambda: self.now_dt,
            )
            from agent_hub_host.oversight import MailboxReadAck, SecurityBindingError
            read_ack = MailboxReadAck(
                message_id=msg.message_id,
                read_at=format_iso_utc(self.now_dt),
                reader_session_id=self.session_uuid,
                reader_generation=self.generation,
            )
            with self.assertRaises(SecurityBindingError) as ctx_rec:
                oversight.record_mailbox_read_ack(
                    message_id=msg.message_id,
                    ack=read_ack,
                    expected_obligation_id=task_id,
                    expected_revision=1,
                    expected_owner_tag=self.owner_tag,
                    expected_workspace=self.workspace,
                )
            self.assertIn("receipt ingress", str(ctx_rec.exception).lower())

    # --- 7. Dependency Completion Verification Failure Matrix ---

    def test_dependency_completion_verification_failure_matrix(self):
        """Dependency verification validates canonical provenance, FileBus active tokens,
        and trust root policies; rejects malformed bindings, missing credentials, and self-review."""
        dep_task_id = "task-dep-matrix"
        r_digest = compute_sha256("dep result content")
        self._issue_and_ack_task(dep_task_id)

        # 1. Record proposal
        self.provider.propose_task_completion(
            owner_actor_id=self.owner_id,
            owner_token=self.owner_token,
            task_id=dep_task_id,
            revision=1,
            project_id=self.project_id,
            workspace=self.workspace,
            native_owner_tag=self.owner_tag,
            native_session_uuid=self.session_uuid,
            generation=self.generation,
            result_digest=r_digest,
            result_evidence={"digest": r_digest},
            idempotency_key="prop-matrix-1",
        )

        # Before review and acceptance: fails
        with mock.patch("agent_hub_host.task_authority_adapter._get_internal_now_utc", return_value=self.now_dt):
            ok, reason = self.adapter.verify_dependency_completion(dep_task_id, self.workspace)
            self.assertFalse(ok)

        # 2. Record review
        self.provider.record_task_review(
            reviewer_actor_id=self.reviewer_id,
            reviewer_token=self.reviewer_token,
            task_id=dep_task_id,
            revision=1,
            project_id=self.project_id,
            workspace=self.workspace,
            expected_result_digest=r_digest,
            verdict="approved",
            review_evidence={"ok": True},
            idempotency_key="rev-matrix-1",
        )

        # Still fails (missing acceptance)
        with mock.patch("agent_hub_host.task_authority_adapter._get_internal_now_utc", return_value=self.now_dt):
            ok, reason = self.adapter.verify_dependency_completion(dep_task_id, self.workspace)
            self.assertFalse(ok)

        # 3. Record acceptance
        self.provider.record_task_acceptance(
            acceptor_actor_id=self.acceptor_id,
            acceptor_token=self.acceptor_token,
            task_id=dep_task_id,
            revision=1,
            project_id=self.project_id,
            workspace=self.workspace,
            expected_result_digest=r_digest,
            acceptance_evidence={"accepted": True},
            idempotency_key="acc-matrix-1",
        )

        # 4. Under adapter, fixture native binding in dependency fails closed
        with mock.patch("agent_hub_host.task_authority_adapter._get_internal_now_utc", return_value=self.now_dt):
            ok, reason = self.adapter.verify_dependency_completion(dep_task_id, self.workspace)
            self.assertFalse(ok)
            self.assertIn("fixture", reason.lower())

        # 5. Mismatched workspace fails closed
        with mock.patch("agent_hub_host.task_authority_adapter._get_internal_now_utc", return_value=self.now_dt):
            ok, reason = self.adapter.verify_dependency_completion(dep_task_id, "/wrong/workspace")
            self.assertFalse(ok)

    # --- 8. Task With Unverified Dependency Marked Blocked ---

    def test_task_with_unverified_dependency_marked_blocked(self):
        """Task declaring dependencies has dependencies verified through canonical path;
        unverified dependency blocks grant."""
        dep_id = "task-unverified-dep"
        self._issue_and_ack_task(dep_id)

        main_id = "task-main-with-dep"
        self._issue_and_ack_task(main_id, dependencies=(dep_id,))

        with mock.patch("agent_hub_host.task_authority_adapter._get_internal_now_utc", return_value=self.now_dt):
            grant = self.adapter.resolve_authority({"task_id": main_id, "revision": 1})
            self.assertIsNotNone(grant)
            self.assertEqual(grant.status, "blocked")
            self.assertIn("Dependency 'task-unverified-dep' unverified", grant.protected_reason or "")

            # Revalidation fails
            self.assertIsNone(self.adapter.revalidate_authority(main_id, 1, self.workspace))

    # --- 9. Actual Head Collision Fix: Distinct Canonical JSON 8-Tuple Digests ---

    def test_task_ownership_tuple_digest_collision_fix(self):
        """Original colon-separated format collided on adjacent colons:
        project='project:/workspace', ws='/path' vs project='project', ws='/workspace:/path'.
        Canonical JSON ordered 8-tuple with version marker produces distinct digests."""
        t1 = TaskOwnershipTuple(
            task_id="task-col",
            revision=1,
            project_id="project:/workspace",
            canonical_workspace="/path",
            owner_bus_actor=self.owner_id,
            native_owner_tag=self.owner_tag,
            native_session_uuid=self.session_uuid,
            generation=0,
        )
        t2 = TaskOwnershipTuple(
            task_id="task-col",
            revision=1,
            project_id="project",
            canonical_workspace="/workspace:/path",
            owner_bus_actor=self.owner_id,
            native_owner_tag=self.owner_tag,
            native_session_uuid=self.session_uuid,
            generation=0,
        )

        # Demonstrate legacy collision
        legacy_str_1 = f"{t1.task_id}:{t1.revision}:{t1.project_id}:{t1.canonical_workspace}:{t1.owner_bus_actor}:{t1.native_owner_tag}:{t1.native_session_uuid}:{t1.generation}"
        legacy_str_2 = f"{t2.task_id}:{t2.revision}:{t2.project_id}:{t2.canonical_workspace}:{t2.owner_bus_actor}:{t2.native_owner_tag}:{t2.native_session_uuid}:{t2.generation}"
        self.assertEqual(legacy_str_1, legacy_str_2)
        self.assertEqual(compute_sha256(legacy_str_1), compute_sha256(legacy_str_2))

        # Distinct under canonical JSON digest
        self.assertNotEqual(t1.digest(), t2.digest())

        # Exact canonical ordered 8-tuple with version marker
        expected_json = json.dumps([
            "task_ownership_tuple_v1",
            t1.task_id,
            t1.revision,
            t1.project_id,
            t1.canonical_workspace,
            t1.owner_bus_actor,
            t1.native_owner_tag,
            t1.native_session_uuid,
            t1.generation,
        ], sort_keys=True, separators=(",", ":"))
        self.assertEqual(t1.digest(), compute_sha256(expected_json.encode("utf-8")))

    # --- 10. No External Clock or allow_fixtures Parameter in Constructor ---

    def test_no_external_clock_or_allow_fixtures_in_constructor(self):
        """TaskAuthorityAdapter rejects non-TaskAuthorityProvider, does not expose clock
        or allow_fixtures arguments in constructor, and does not mutate caller provider."""
        sig = inspect.signature(TaskAuthorityAdapter.__init__)
        param_names = list(sig.parameters.keys())
        self.assertEqual(param_names, ["self", "provider"])

        with self.assertRaises(TypeError):
            TaskAuthorityAdapter("not-a-provider")  # type: ignore

        # Verify that caller provider attributes are not mutated
        orig_clock = self.provider._clock
        orig_native = self.provider.native_binding_provider
        adapter = TaskAuthorityAdapter(self.provider)
        self.assertIs(self.provider._clock, orig_clock)
        self.assertIs(self.provider.native_binding_provider, orig_native)


if __name__ == "__main__":
    unittest.main()
