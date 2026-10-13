"""Task Authority Adapter for Agent Hub.

Bridges TaskAuthorityProvider to OversightAdapter protocols:
- AuthorityResolverProtocol (resolve_authority, revalidate_authority)
- DependencyCompletionVerifierProtocol (verify_dependency_completion)

docs/SPEC.md §9, §11, §12; Issue #11.

Enforces:
- Standalone source-only module: no CLI, host, runtime, HTTP, or listener import wiring.
- Strict internal aware UTC wallclock: never delegates to caller-supplied now_utc
  nor provider's injected/test clock. No constructor clock argument, no allow_fixtures switch.
- Read-only internal view of TaskAuthorityProvider reusing store, authenticator, and trust root,
  without mutating caller provider or global fields.
- Load-bearing native evidence filter at integration boundary:
  Rejects fixture-only provider declarations and fixture evidence (is_fixture_only != False).
  Rejects mocked witness components attempting to satisfy production trust merely by labels.
  Fails closed with UNKNOWN / DENIED when native provider metadata cannot establish
  authenticated operational qualification (states exact missing interface).
- Independent dependency-own acceptance verification through canonical provider path.
- Pre-send guard and receipt ingress entrypoints reuse boundary revalidation; cannot bypass
  fixture, clock, or currentness checks.
- Zero mutations to intervention ledger, outbox reservations, or transport callbacks on denial.
"""

from __future__ import annotations

import dataclasses
import datetime
from typing import Any, Dict, Optional, Tuple

from .task_authority import (
    CASConflictError,
    FencedStateError,
    InvalidTupleError,
    NativeBindingError,
    NativeBindingProviderProtocol,
    NativeSessionEvidence,
    PermissionDeniedError,
    TaskAuthorityError,
    TaskAuthorityProvider,
    TaskGrantRecord,
    VerificationError,
    compute_sha256,
    format_iso_utc,
    parse_strict_iso_utc,
)
from .oversight_adapter import (
    AuthorityQualificationResult,
    AuthorityResolverProtocol,
    DependencyCompletionVerifierProtocol,
    EXACT_TUPLE_BOUNDARY_FIELDS,
    QUALIFICATION_STATUS_DENIED,
    QUALIFICATION_STATUS_HELD,
    QUALIFICATION_STATUS_OFFLINE_REFERENCE,
    QUALIFICATION_STATUS_QUALIFIED,
    QUALIFICATION_STATUS_UNKNOWN,
    REQUIRED_INDEPENDENT_PROVIDERS,
    TrustedAuthorityGrant,
)


def _get_internal_now_utc() -> datetime.datetime:
    """Obtain internal aware UTC wallclock for the integration boundary.

    Cannot be injected via constructor argument or configuration switch.
    Tests may patch this module-level function with unittest.mock.
    """
    return datetime.datetime.now(datetime.timezone.utc)


class StrictIntegrationNativeBindingProvider(NativeBindingProviderProtocol):
    """Strict integration wrapper around native binding provider.

    Load-bearing at integration boundary:
    - Rejects provider fixture-only declaration or fixture classes.
    - Rejects evidence with is_fixture_only True, unknown, None, or malformed.
    - Rejects mocked witness components attempting to masquerade trust by unmarking metadata.
    - Requires complete current native session aliveness, readiness, and explicit unblocked flags.
    - States exact missing interface when operational qualification cannot be established.
    """

    def __init__(self, underlying_provider: Optional[NativeBindingProviderProtocol]) -> None:
        self.underlying_provider = underlying_provider

    def get_binding_evidence(
        self,
        workspace: str,
        tag: str,
        now_utc: Optional[datetime.datetime] = None,
    ) -> NativeSessionEvidence:
        """Evaluate native binding evidence under strict integration requirements.

        Enforces internal UTC wallclock, never delegating to caller-supplied now_utc.
        Rejects fixture providers, fixture evidence, and unqualified readers.
        Fails closed unconditionally with UNKNOWN since QualifiedNativeSessionReader
        is absent on host (repair owner: native host runtime).
        """
        now = _get_internal_now_utc()

        if self.underlying_provider is None:
            raise NativeBindingError(
                "Native binding provider is not configured (missing QualifiedNativeSessionReader on host; "
                "status UNKNOWN; repair owner: native host runtime)"
            )

        # 1. Reject provider fixture-only declarations or fixture class names
        if (
            getattr(self.underlying_provider, "is_fixture_only", False) is True
            or getattr(self.underlying_provider, "is_fixture", False) is True
            or "fixture" in self.underlying_provider.__class__.__name__.lower()
        ):
            raise NativeBindingError(
                f"Native binding provider '{self.underlying_provider.__class__.__name__}' declared as fixture-only; "
                f"denied at integration boundary"
            )

        # 2. Query underlying provider
        evidence = self.underlying_provider.get_binding_evidence(
            workspace=workspace,
            tag=tag,
            now_utc=now,
        )
        if evidence is None:
            raise NativeBindingError(
                f"Native session binding missing for tag '{tag}' in workspace '{workspace}'"
            )

        # 3. Reject fixture evidence flag (must be strictly False, not True, not None, not non-bool)
        ev_fixture = getattr(evidence, "is_fixture_only", None)
        if type(ev_fixture) is not bool or ev_fixture is not False:
            raise NativeBindingError(
                f"Native evidence has fixture flag active or malformed (is_fixture_only={ev_fixture!r}); "
                f"denied at integration boundary"
            )

        # 4. Strictly validate workspace, tag, session_id, generation
        if not isinstance(evidence.workspace, str) or evidence.workspace != workspace:
            raise NativeBindingError(
                f"Native evidence workspace mismatch: expected '{workspace}', got '{getattr(evidence, 'workspace', None)}'"
            )
        if not isinstance(evidence.tag, str) or evidence.tag != tag:
            raise NativeBindingError(
                f"Native evidence tag mismatch: expected '{tag}', got '{getattr(evidence, 'tag', None)}'"
            )
        if not isinstance(evidence.session_id, str) or not evidence.session_id.strip():
            raise NativeBindingError("Native evidence session_id must be a non-empty string")
        if type(evidence.generation) is not int or evidence.generation < 0:
            raise NativeBindingError(f"Native evidence generation must be an integer >= 0; got {evidence.generation!r}")

        # 5. Strict boolean aliveness and readiness
        if type(evidence.is_alive) is not bool or evidence.is_alive is not True:
            raise NativeBindingError(f"Native session '{evidence.session_id}' is not alive")
        if type(evidence.is_ready) is not bool or evidence.is_ready is not True:
            raise NativeBindingError(f"Native session '{evidence.session_id}' is not ready")

        # 6. Strict boolean checks for all known protection flags
        for flag_name in (
            "is_frozen", "is_blocked", "is_quota_held", "is_manual_approval",
            "is_draft", "is_menu_active", "is_saved_goal"
        ):
            val = getattr(evidence, flag_name, None)
            if type(val) is not bool or val is not False:
                raise NativeBindingError(f"Native session protected state active: {flag_name}={val}")

        # 7. Timestamp validation and freshness against internal wallclock
        if not isinstance(evidence.observed_at, str) or not evidence.observed_at.strip():
            raise NativeBindingError("Native binding observation timestamp is mandatory")
        obs_dt = parse_strict_iso_utc(evidence.observed_at)
        diff = (now - obs_dt).total_seconds()
        if diff < -5.0:
            raise NativeBindingError(f"Native binding observation timestamp is in the future: {evidence.observed_at}")
        if diff > 300.0:
            raise NativeBindingError(f"Native binding observation timestamp is stale: {evidence.observed_at}")

        # 8. Operational qualification check:
        # NativeSessionRecord on host lacks semantic generation and full protected observation.
        # No installed qualified reader exists on host => default UNKNOWN.
        # Callable getters returning True, unmarking fixture metadata, or class labels
        # cannot authenticate semantic native authority.
        # Fails closed unconditionally with status UNKNOWN until genuine authenticated source
        # interface is separately implemented.
        raise NativeBindingError(
            "Native session provider lacks qualified operational reader interface for semantic "
            "generation and protected observation (missing QualifiedNativeSessionReader on host; "
            "status UNKNOWN; repair owner: native host runtime)"
        )


class TaskAuthorityAdapter:
    """Standalone provider-to-OversightAdapter resolver, revalidator, and dependency boundary.

    Adapts TaskAuthorityProvider to AuthorityResolverProtocol and
    DependencyCompletionVerifierProtocol for OversightAdapter.
    """

    # OversightAdapter checks getattr(resolver, "is_offline_reference", False)
    is_offline_reference: bool = False

    def __init__(self, provider: TaskAuthorityProvider) -> None:
        if not isinstance(provider, TaskAuthorityProvider):
            raise TypeError(f"provider must be an instance of TaskAuthorityProvider; got {type(provider).__name__}")

        self._underlying_provider = provider

        # Read-only internal view reusing store, authenticator, and trust root,
        # but binding clock to internal wallclock and wrapping native provider with strict filter.
        # The caller's provider and its attributes are NEVER mutated.
        self._strict_native_provider = StrictIntegrationNativeBindingProvider(
            provider.native_binding_provider
        )
        self._internal_provider = TaskAuthorityProvider(
            store=provider.store,
            authenticator=provider.authenticator,
            trust_root=provider.trust_root,
            native_binding_provider=self._strict_native_provider,
            clock=_get_internal_now_utc,
        )

    def _get_now(self) -> datetime.datetime:
        """Obtain internal aware UTC wallclock."""
        return _get_internal_now_utc()

    def resolve_authority(
        self,
        raw_task_data: Any,
        now_utc: Optional[datetime.datetime] = None,
    ) -> Optional[TrustedAuthorityGrant]:
        """Resolve untrusted task data into a verified TrustedAuthorityGrant.

        Enforces:
        - Internal wallclock only: caller-supplied now_utc is ignored (timezone-naive rejected).
        - Underlying provider injected/test clock is not inherited.
        - Strict integration native evidence filter: fixture providers/evidence fail closed.
        - Independent dependency completion verification before returning ready grant.
        """
        # Reject timezone-naive datetime if supplied
        if now_utc is not None and now_utc.tzinfo is None:
            return None

        # Always use internal aware UTC wallclock
        now = self._get_now()

        if not raw_task_data:
            return None

        try:
            grant = self._internal_provider.resolve_authority(raw_task_data, now_utc=now)
        except Exception:
            return None

        if grant is None:
            return None

        # Check dependencies through current dependency-own canonical provider path
        if grant.dependencies:
            for dep_id in grant.dependencies:
                dep_ok, dep_reason = self.verify_dependency_completion(
                    dependency_task_id=dep_id,
                    expected_workspace=grant.workspace,
                    now_utc=now,
                )
                if not dep_ok:
                    return dataclasses.replace(
                        grant,
                        status="blocked",
                        protected_reason=f"Dependency '{dep_id}' unverified: {dep_reason}",
                    )

        # If grant is not ready or has a protected reason, return as-is so caller observes hold/blocked status
        if grant.status != "ready" or grant.protected_reason is not None or not grant.has_owner_ack:
            return grant

        return grant

    def revalidate_authority(
        self,
        task_id: str,
        revision: int,
        workspace: str,
        now_utc: Optional[datetime.datetime] = None,
    ) -> Optional[TrustedAuthorityGrant]:
        """Revalidate current authority grant at exact revision and workspace.

        Used across dispatch query, pre-send guard, and receipt ingress.
        Returns None on any expiration, revocation, fixture native, or boundary failure.
        """
        if now_utc is not None and now_utc.tzinfo is None:
            return None

        now = self._get_now()

        if not isinstance(task_id, str) or not task_id.strip():
            return None
        if not isinstance(revision, int) or revision < 1:
            return None
        if not isinstance(workspace, str) or not workspace.strip():
            return None

        try:
            grant = self._internal_provider.revalidate_authority(
                task_id=task_id.strip(),
                revision=revision,
                workspace=workspace.strip(),
                now_utc=now,
            )
        except Exception:
            return None

        if grant is None:
            return None

        # Check dependencies through current dependency-own canonical provider path
        if grant.dependencies:
            for dep_id in grant.dependencies:
                dep_ok, _ = self.verify_dependency_completion(
                    dependency_task_id=dep_id,
                    expected_workspace=grant.workspace,
                    now_utc=now,
                )
                if not dep_ok:
                    return None

        return grant

    def verify_dependency_completion(
        self,
        dependency_task_id: str,
        expected_workspace: str,
        now_utc: Optional[datetime.datetime] = None,
        dependency_evidence: Optional[Dict[str, Any]] = None,
    ) -> Tuple[bool, str]:
        """Verify DEPENDENCY's own current revision, owner, workspace, session, generation,
        issuer, protected state, pinned result, review, acceptance record,
        and current canonical identity presence and policy in FileBus.

        Never trusts caller metadata, transport receipts, or plain completed set.
        """
        if now_utc is not None and now_utc.tzinfo is None:
            return False, "now_utc must be timezone-aware UTC"

        now = self._get_now()

        if not isinstance(dependency_task_id, str) or not dependency_task_id.strip():
            return False, "dependency_task_id must be a non-empty string"
        if not isinstance(expected_workspace, str) or not expected_workspace.strip():
            return False, "expected_workspace must be a non-empty string"

        try:
            return self._internal_provider.verify_dependency_completion(
                dependency_task_id=dependency_task_id.strip(),
                expected_workspace=expected_workspace.strip(),
                now_utc=now,
                dependency_evidence=dependency_evidence,
            )
        except Exception as exc:
            return False, f"Dependency verification exception for '{dependency_task_id}': {exc}"

    def assess_qualification(
        self,
        raw_task_data: Any = None,
    ) -> AuthorityQualificationResult:
        """Read-only capability and qualification assessment of authority provider.

        Evaluates whether production trust is established. Default UNKNOWN;
        states exact missing interface when operational qualification cannot be established.
        """
        now = self._get_now()

        # If raw_task_data is provided, attempt resolution
        grant = None
        if raw_task_data is not None:
            try:
                grant = self.resolve_authority(raw_task_data, now_utc=now)
            except Exception:
                grant = None

        details: Dict[str, Any] = {
            "production_provider_present": False,
            "production_qualified": False,
            "missing_interface": "QualifiedNativeSessionReader (semantic generation, full protected observation)",
        }
        if grant is not None:
            details.update({
                "task_id": grant.task_id,
                "revision": grant.revision,
                "status": grant.status,
                "protected_reason": grant.protected_reason,
            })

        return AuthorityQualificationResult(
            is_qualified=False,
            status=QUALIFICATION_STATUS_UNKNOWN,
            reason=(
                "Native session provider lacks qualified operational reader interface for semantic "
                "generation and protected observation (missing QualifiedNativeSessionReader on host; status UNKNOWN)"
            ),
            missing_providers=REQUIRED_INDEPENDENT_PROVIDERS,
            verification_boundary=EXACT_TUPLE_BOUNDARY_FIELDS,
            dependency_status=QUALIFICATION_STATUS_UNKNOWN,
            details=details,
        )
