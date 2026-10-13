"""Offline Trusted-Obligation and Guarded-Transport Adapter for Agent Hub.

docs/SPEC.md §9, §11, §12; coordination/continuation-runtime/REQUEST-TO-OUTCOME.md.

Provides:
- Standalone stdlib library adapter callable once on demand.
- Zero subprocess / native execution, no background timers, threads, pollers, or daemons.
- Strict requirement for explicit trusted grant provenance resolved against
  authoritative registered grants by caller-owned authority resolver.
  (Unregistered task snapshots, updated_at/status strings, and 'trusted: true' switches are untrusted).
- Timezone-naive timestamps strictly rejected; explicit status required (no default ready).
- Non-integer generation and revision (including bools) fail closed; empty workspaces fail closed.
- Guard revalidates CURRENT authority, binding, and native state immediately
  before dispatch inside the ledger transaction boundary (zero mutation inside guard).
- Fences session reassignment before dispatch.
- Transport revalidates CURRENT authority, session binding, and native state immediately
  before callback execution (detecting any mutation between guard and send; zero callback calls on failure).
  Missing context on direct AdapterTransport construction denies before callback.
- Strict principal bus-only route enforcement; native continuation refused.
- Guard uncertainty on quota, frozen, blocked, manual approval, draft, menu,
  or saved-goal states causes ZERO transport calls.
- Missing or unknown readiness/liveness on guard or transport strictly denies; explicit ready/liveness mandatory.
- Receipt verifiers: DefaultDenyReceiptVerifier in production; ReceiptVerifierProtocol for injection.
  Injected verifier boundary catches exceptions and missing/nonbool verification as post-call uncertainty without retry.
- Full exact tuple binding required on ALL receipt ingress and replay (read-ACK, custody acceptance,
  first useful action, completion outcome); authority_resolver and binding_provider are mandatory on ingress.
- Adapter-owned durable table (adapter_receipt_digests) for exact-content replay enforcement.
- Validates stage, binding, and ordered timestamps before reservation so invalid out-of-order receipts
  cannot poison legitimate later stages.
- Replaying exact recorded receipts through later valid stages is supported nonmutating; changed content rejected.
- Qualifies actual SQLite storage failures, file-backed close/reopen crash windows before and after ledger stage transition;
  ambiguous states fail closed with operator reconciliation documented. Zero destructive mutation of accepted audit tables.
"""

from __future__ import annotations

import dataclasses
import datetime
import hashlib
import json
import os
import sqlite3
from typing import Any, Callable, Dict, List, Optional, Protocol, Sequence, Set, Tuple, Union

from .oversight import (
    # Core Dataclasses
    ActionReceipt,
    ClassificationResult,
    CompletionOutcome,
    CustodyAcceptance,
    InterventionRecord,
    MailboxReadAck,
    NativeSessionBinding,
    ObservationEvidence,
    OutboxMessage,
    OversightDecision,
    TaskObligation,
    # Protocols
    GuardProtocol,
    TransportProtocol,
    # Classes
    InterventionLedger,
    OversightPolicy,
    # Constants
    CLASSIFICATION_COMPLETED_AWAITING_OWNER,
    CLASSIFICATION_CURRENT_USEFUL_WORK,
    CLASSIFICATION_DONE,
    CLASSIFICATION_IDLE_WITH_READY_WORK,
    CLASSIFICATION_MISSED_CHECKPOINT,
    CLASSIFICATION_PROTECTED,
    CLASSIFICATION_UNKNOWN_OR_STALE,
    DEFAULT_TIMEOUT_SECONDS,
    FRESHNESS_THRESHOLD_SECONDS,
    INTERVENTION_CHECKPOINT_MISSED,
    INTERVENTION_NUDGE_IDLE,
    INTERVENTION_RESULT_ACK_PENDING,
    MAX_PAYLOAD_BYTES,
    OUTBOX_CANCELLED,
    OUTBOX_FAILED,
    OUTBOX_PENDING,
    OUTBOX_SENDING,
    OUTBOX_SENT,
    OUTBOX_UNCERTAIN,
    ROUTE_BUS,
    ROUTE_NATIVE_MAILBOX,
    STATE_CANCELLED,
    STATE_COMPLETED,
    STATE_CUSTODY_ACCEPTED,
    STATE_DISPATCHED_SENT,
    STATE_DISPATCHED_UNCERTAIN,
    STATE_ESCALATION_NEEDED,
    STATE_FAILED,
    STATE_FENCED,
    STATE_FIRST_ACTION_OBSERVED,
    STATE_MAILBOX_READ_ACK,
    STATE_OUTBOX_PENDING,
    TERMINAL_STATES,
    VALID_USEFUL_ACTION_TYPES,
    # Exceptions
    CapacityExceededError,
    GuardAuthorizationError,
    OversightError,
    SecurityBindingError,
    TransportError,
    TransportSendFailedBeforeDelivery,
    TransportUncertainError,
    # Helper functions
    now_iso,
    parse_iso_utc,
    sanitize_payload,
    sanitize_text,
)


STAGE_ORDER: Dict[str, int] = {
    STATE_OUTBOX_PENDING: 0,
    STATE_DISPATCHED_SENT: 1,
    STATE_MAILBOX_READ_ACK: 2,
    STATE_CUSTODY_ACCEPTED: 3,
    STATE_FIRST_ACTION_OBSERVED: 4,
    STATE_COMPLETED: 5,
}


def _stage_index(state: str) -> int:
    return STAGE_ORDER.get(state, -1)


# --- Qualification Constants and Verification Boundaries ---

REQUIRED_INDEPENDENT_PROVIDERS: Tuple[str, ...] = (
    "authenticated current task/custody/revision authority resolver",
    "independent current exactbound dependency-completion verifier",
)

EXACT_TUPLE_BOUNDARY_FIELDS: Tuple[str, ...] = (
    "task_id",
    "revision",
    "owner_tag",
    "workspace",
    "session_id",
    "generation",
    "custody",
    "source_id",
    "source_digest",
    "currentness",
)

QUALIFICATION_STATUS_QUALIFIED = "QUALIFIED"
QUALIFICATION_STATUS_HELD = "HELD"
QUALIFICATION_STATUS_DENIED = "DENIED"
QUALIFICATION_STATUS_UNKNOWN = "UNKNOWN"
QUALIFICATION_STATUS_OFFLINE_REFERENCE = "OFFLINE_REFERENCE"


# --- Strict Type and Time Helpers ---

def _is_valid_int(val: Any) -> bool:
    """Return True if val is strictly an int and not a boolean."""
    return type(val) is int and not isinstance(val, bool)


def parse_strict_iso_utc(ts: str) -> datetime.datetime:
    """Parse an ISO8601 string into a UTC-aware datetime.

    Strictly rejects timezone-naive timestamps (no default UTC assumption)
    and non-string inputs.
    """
    if not isinstance(ts, str):
        raise ValueError(f"Timestamp must be a string, got {type(ts)}")
    clean = ts.strip()
    if not clean:
        raise ValueError("Timestamp cannot be empty")
    has_tz = clean.endswith("Z") or "+" in clean or ("-" in clean[10:] and ":" in clean[10:])
    if not has_tz:
        raise ValueError(f"Timestamp '{ts}' is timezone-naive; explicit timezone offset is required")

    if clean.endswith("Z"):
        clean = clean[:-1] + "+00:00"

    dt = datetime.datetime.fromisoformat(clean)
    if dt.tzinfo is None:
        raise ValueError(f"Timestamp '{ts}' parsed as timezone-naive")

    return dt.astimezone(datetime.timezone.utc)


def _validate_authority_protection_and_dependencies(
    grant: Any,
    completed_obligations: Optional[Set[str]] = None,
    expected_dependencies: Optional[Sequence[str]] = None,
    dependency_verifier: Optional[DependencyCompletionVerifierProtocol] = None,
    now_utc: Optional[datetime.datetime] = None,
) -> Tuple[bool, str]:
    """Validate that an authority grant is not protected, has ready status, and dependencies are qualified.

    Enforces:
    - Known mandatory protected reasons nonempty deny.
    - Malformed/nonstring/unknown protection deny.
    - Status must be strictly 'ready' or 'in_progress'; protected statuses (frozen, blocked, quota_held, manual_approval) and non-strings deny.
    - Dependencies cannot be silently ignored:
      * Unmet/unknown dependencies deny.
      * Dependencies changed after initial classification deny.
      * Plain/raw completed_obligations sets cannot authorize non-empty dependencies (raw-set authority path removed).
      * Non-empty dependencies deny/HELD unless qualified by an independent dependency-completion verifier provider.
      * Default/missing/unknown/unqualified provider denies readiness/dependency receipt BEFORE any transport or ledger stage/digest mutation.
    """
    # 1. Protected reason check
    prot_reason = getattr(grant, "protected_reason", None)
    if prot_reason is not None:
        if not isinstance(prot_reason, str):
            return False, f"Authority protected_reason must be a string (got {type(prot_reason).__name__})"
        clean_reason = prot_reason.strip()
        if not clean_reason:
            return False, "Authority protected_reason cannot be empty or whitespace"
        return False, f"Authority is protected: {clean_reason}"

    # 2. Status check
    status = getattr(grant, "status", None)
    if not isinstance(status, str) or not status.strip():
        return False, f"Authority status must be a non-empty string; got {status!r}"
    if status in ("frozen", "blocked", "quota_held", "manual_approval"):
        return False, f"Authority status not ready (protected: {status})"
    if status not in ("ready", "in_progress"):
        return False, f"Authority status not ready: {status}"

    # 3. Dependencies check
    raw_deps = getattr(grant, "dependencies", ())
    if raw_deps is None:
        raw_deps = ()
    if not isinstance(raw_deps, (tuple, list, set)):
        return False, f"Authority dependencies must be a tuple or list; got {type(raw_deps).__name__}"

    deps: List[str] = []
    for d in raw_deps:
        if not isinstance(d, str) or not d.strip():
            return False, f"Authority dependency item must be a non-empty string; got {d!r}"
        deps.append(d.strip())
    deps_tuple = tuple(deps)

    if expected_dependencies is not None:
        exp_tuple = tuple(expected_dependencies)
        if deps_tuple != exp_tuple:
            return False, f"Authority dependencies changed after initial classification ({deps_tuple} != {exp_tuple})"

    if deps_tuple:
        # Non-empty dependencies fail closed: independent completion authority provider
        # is absent on host. Plain completed sets, caller flags, and boolean verifiers
        # cannot unhold actual dependencies.
        if dependency_verifier is not None:
            verifier_now = now_utc or datetime.datetime.now(datetime.timezone.utc)
            workspace = getattr(grant, "workspace", "")
            for d in deps_tuple:
                try:
                    verif_res = dependency_verifier.verify_dependency_completion(
                        dependency_task_id=d,
                        expected_workspace=workspace,
                        now_utc=verifier_now,
                    )
                    if not isinstance(verif_res, tuple) or len(verif_res) != 2:
                        return False, f"Dependency verifier returned invalid result for '{d}': {verif_res!r}"
                    ok_dep, dep_reason = verif_res
                    if not isinstance(ok_dep, bool):
                        return False, f"Dependency verifier returned non-bool for '{d}': {ok_dep!r}"
                    if not ok_dep:
                        return False, f"Dependency '{d}' completion unverified (HELD): {dep_reason}"
                except Exception as exc:
                    return False, f"Dependency verifier exception for '{d}': {exc}"

        # Production dependencies remain explicitly HELD absent an actual installed independent provider.
        # Plain completed sets, caller flags, and boolean verifiers cannot unhold dependencies.
        return False, (
            f"Authority has non-empty dependencies {deps_tuple}: dependency authority is HELD "
            f"(independent current exactbound dependency-completion verifier absent on host; "
            f"raw completed sets, caller flags, and boolean verifiers cannot unhold dependencies)"
        )

    return True, ""


# --- Exceptions ---

class AdapterError(OversightError):
    """Base exception for oversight adapter operations."""
    pass


class AuthorityResolutionError(AdapterError):
    """Raised when task authority cannot be resolved or provenance is invalid."""
    pass


class RevalidationError(AdapterError):
    """Raised when pre-dispatch revalidation detects a state or binding contradiction."""
    pass


class ReceiptValidationError(AdapterError):
    """Raised when an incoming or returned receipt fails verification."""
    pass


# --- Dataclasses ---

@dataclasses.dataclass(frozen=True)
class TrustedAuthorityGrant:
    """Explicit trusted grant resolved by caller-owned authority."""
    task_id: str
    revision: int
    owner_tag: str
    workspace: str
    session_id: str
    generation: int
    source_id: str
    source_digest: str
    issued_at: str  # ISO8601 UTC with explicit timezone
    status: str  # Mandatory explicit status (ready, in_progress, etc.; no default)
    expires_at: Optional[str] = None  # ISO8601 UTC with explicit timezone
    checkpoint_due: Optional[str] = None
    completed_result: Optional[Dict[str, Any]] = None
    is_principal: bool = False
    protected_reason: Optional[str] = None
    dependencies: Tuple[str, ...] = ()
    has_owner_ack: bool = False
    raw_metadata: Dict[str, Any] = dataclasses.field(default_factory=dict)

    def __post_init__(self) -> None:
        if not _is_valid_int(self.revision) or self.revision < 0:
            raise ValueError(f"revision must be an integer >= 0 (bool forbidden); got {self.revision!r}")
        if not _is_valid_int(self.generation) or self.generation < 0:
            raise ValueError(f"generation must be an integer >= 0 (bool forbidden); got {self.generation!r}")
        if not isinstance(self.workspace, str) or not self.workspace.strip():
            raise ValueError(f"workspace must be a non-empty string; got {self.workspace!r}")
        if not isinstance(self.status, str) or not self.status.strip():
            raise ValueError(f"status must be a non-empty string; got {self.status!r}")
        if self.protected_reason is not None and (
            not isinstance(self.protected_reason, str) or not self.protected_reason.strip()
        ):
            raise ValueError(f"protected_reason must be None or a non-empty string; got {self.protected_reason!r}")
        if self.dependencies is not None:
            if not isinstance(self.dependencies, (tuple, list)):
                raise ValueError(f"dependencies must be a tuple or list; got {self.dependencies!r}")
            for d in self.dependencies:
                if not isinstance(d, str) or not d.strip():
                    raise ValueError(f"dependency item must be a non-empty string; got {d!r}")
            if isinstance(self.dependencies, list):
                object.__setattr__(self, "dependencies", tuple(self.dependencies))

    def to_obligation(self) -> TaskObligation:
        return TaskObligation(
            id=self.task_id,
            revision=self.revision,
            owner_tag=self.owner_tag,
            workspace=self.workspace,
            status=self.status,
            dependencies=self.dependencies,
            checkpoint_due=self.checkpoint_due,
            completed_result=self.completed_result,
            is_principal=self.is_principal,
            protected_reason=self.protected_reason,
            created_at=self.issued_at,
            updated_at=self.issued_at,
            has_owner_ack=self.has_owner_ack,
        )


@dataclasses.dataclass(frozen=True)
class NativeGuardCheckResult:
    """Result of native guard inspection, checked immediately before send."""
    allowed: bool
    reason: str
    is_ready: bool  # Mandatory explicit readiness (no default True)
    session_alive: bool  # Mandatory explicit liveness (no default True)
    is_quota_held: bool = False
    is_frozen: bool = False
    is_blocked: bool = False
    is_manual_approval: bool = False
    is_draft: bool = False
    is_menu_active: bool = False
    is_saved_goal: bool = False
    current_generation: Optional[int] = None
    details: Dict[str, Any] = dataclasses.field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.is_ready, bool):
            raise ValueError(f"is_ready must be a boolean; got {self.is_ready!r}")
        if not isinstance(self.session_alive, bool):
            raise ValueError(f"session_alive must be a boolean; got {self.session_alive!r}")
        if not isinstance(self.allowed, bool):
            raise ValueError(f"allowed must be a boolean; got {self.allowed!r}")


@dataclasses.dataclass(frozen=True)
class TransportSendRequest:
    """Full trusted tuple passed to injected transport callbacks."""
    route: str
    recipient_tag: str
    payload: Dict[str, Any]
    idempotency_key: str
    obligation_id: str
    obligation_revision: int
    owner_tag: str
    workspace: str
    session_id: str
    generation: int
    intervention_type: str
    is_principal: bool
    intervention_id: str
    message_id: str


@dataclasses.dataclass(frozen=True)
class AdapterExecutionResult:
    """Outcome of evaluate_and_dispatch execution."""
    decision_made: bool
    decision: Optional[OversightDecision] = None
    intervention: Optional[InterventionRecord] = None
    outbox_message: Optional[OutboxMessage] = None
    dispatch_attempted: bool = False
    dispatch_sent: bool = False
    dispatched_uncertain: bool = False
    dispatch_failed: bool = False
    refusal_reason: Optional[str] = None
    receipt: Optional[Dict[str, Any]] = None


@dataclasses.dataclass(frozen=True)
class AuthorityQualificationResult:
    """Read-only capability and qualification assessment of authority and dependency providers."""
    is_qualified: bool
    status: str  # QUALIFIED, HELD, DENIED, UNKNOWN, OFFLINE_REFERENCE
    reason: str
    missing_providers: Tuple[str, ...]
    verification_boundary: Tuple[str, ...]
    dependency_status: str  # HELD, NOT_APPLICABLE, UNQUALIFIED, UNKNOWN
    details: Dict[str, Any] = dataclasses.field(default_factory=dict)


# --- Protocols ---

class AuthorityResolverProtocol(Protocol):
    """Protocol for resolving untrusted caller data into a verified authority grant."""

    def resolve_authority(
        self,
        raw_task_data: Any,
        now_utc: datetime.datetime,
    ) -> Optional[TrustedAuthorityGrant]:
        ...

    def revalidate_authority(
        self,
        task_id: str,
        revision: int,
        workspace: str,
        now_utc: datetime.datetime,
    ) -> Optional[TrustedAuthorityGrant]:
        ...


class DependencyCompletionVerifierProtocol(Protocol):
    """Protocol for independent current exactbound dependency-completion verifier.

    Requires full exact tuple verification boundary:
    (task_id, revision, owner_tag, workspace, session_id, generation,
     custody, source_id, source_digest, currentness).

    DO NOT implement fake positive provider.
    Default/missing/unknown/unqualified provider denies readiness/dependency receipt
    BEFORE any transport or ledger stage/digest mutation.
    """

    def verify_dependency_completion(
        self,
        dependency_task_id: str,
        expected_workspace: str,
        now_utc: datetime.datetime,
        dependency_evidence: Optional[Dict[str, Any]] = None,
    ) -> Tuple[bool, str]:
        ...


class DefaultDenyDependencyCompletionVerifier:
    """Default dependency completion verifier that strictly fails closed as HELD.

    No independent completion authority provider is installed on host.
    """

    def verify_dependency_completion(
        self,
        dependency_task_id: str,
        expected_workspace: str,
        now_utc: datetime.datetime,
        dependency_evidence: Optional[Dict[str, Any]] = None,
    ) -> Tuple[bool, str]:
        return False, (
            f"DefaultDenyDependencyCompletionVerifier: independent current exactbound "
            f"dependency-completion verifier absent on host; dependency '{dependency_task_id}' remains HELD"
        )


class NativeGuardInterface(Protocol):
    """Interface for delegating actual native environment and session inspection."""

    def check_native_state(
        self,
        workspace: str,
        session_id: str,
        generation: int,
        now_utc: datetime.datetime,
    ) -> NativeGuardCheckResult:
        ...


class BindingProviderProtocol(Protocol):
    """Protocol for providing current native session binding."""

    def get_current_binding(
        self,
        workspace: str,
        session_id: str,
        now_utc: datetime.datetime,
    ) -> Optional[NativeSessionBinding]:
        ...


class ObservationProviderProtocol(Protocol):
    """Protocol for providing current observation evidence."""

    def get_current_observation(
        self,
        workspace: str,
        session_id: str,
        generation: int,
        now_utc: datetime.datetime,
    ) -> Optional[ObservationEvidence]:
        ...


class ReceiptVerifierProtocol(Protocol):
    """Protocol for verifying returned transport message receipts."""

    def verify_receipt(
        self,
        receipt: Dict[str, Any],
        request: TransportSendRequest,
        now_utc: datetime.datetime,
    ) -> Tuple[bool, str]:
        ...


@dataclasses.dataclass
class TransportRouteCallbacks:
    """Injected route transport callbacks."""
    bus_send: Optional[Callable[[TransportSendRequest], Dict[str, Any]]] = None
    native_continuation: Optional[Callable[[TransportSendRequest], Dict[str, Any]]] = None
    custom_routes: Dict[str, Callable[[TransportSendRequest], Dict[str, Any]]] = dataclasses.field(default_factory=dict)

    def get_callback(self, route: str) -> Optional[Callable[[TransportSendRequest], Dict[str, Any]]]:
        if route == ROUTE_BUS:
            return self.bus_send
        elif route == ROUTE_NATIVE_MAILBOX:
            return self.native_continuation
        return self.custom_routes.get(route)


# --- Standard Reference Authority Resolver ---

class StandardAuthorityResolver:
    """Reference authority resolver resolving strictly against registered grants.

    Explicitly an offline reference seam only; does not establish production trust.
    Rejects unregistered caller inputs, timezone-naive timestamps, bool/non-int
    revision/generation, empty workspaces, and unprovenanced claims.
    """

    is_production_qualified: bool = False
    is_offline_reference: bool = True
    provider_name: str = "StandardAuthorityResolver (offline reference seam only)"

    def __init__(
        self,
        freshness_threshold_seconds: float = FRESHNESS_THRESHOLD_SECONDS,
        known_grants: Optional[Dict[Tuple[str, int], TrustedAuthorityGrant]] = None,
    ):
        self.freshness_threshold_seconds = freshness_threshold_seconds
        self._grants: Dict[Tuple[str, int], TrustedAuthorityGrant] = dict(known_grants or {})

    def register_grant(self, grant: TrustedAuthorityGrant) -> None:
        self._grants[(grant.task_id, grant.revision)] = grant

    def resolve_authority(
        self,
        raw_task_data: Any,
        now_utc: datetime.datetime,
    ) -> Optional[TrustedAuthorityGrant]:
        # Extract task_id and revision query keys from input
        if isinstance(raw_task_data, TrustedAuthorityGrant):
            task_id = raw_task_data.task_id
            revision = raw_task_data.revision
            caller_grant = raw_task_data
        elif isinstance(raw_task_data, dict):
            task_id_raw = raw_task_data.get("task_id") or raw_task_data.get("id")
            rev_raw = raw_task_data.get("revision")
            if not task_id_raw or rev_raw is None:
                return None
            if not _is_valid_int(rev_raw):
                return None
            try:
                task_id = str(task_id_raw)
                revision = int(rev_raw)
            except Exception:
                return None
            caller_grant = None
        else:
            return None

        if not _is_valid_int(revision):
            return None

        # Authoritative lookup against registered grants
        key = (task_id, revision)
        registered = self._grants.get(key)
        if registered is None:
            # Unregistered input is strictly rejected (caller cannot self-authorize)
            return None

        # Validate types on the authoritative registered grant
        if not _is_valid_int(registered.revision) or registered.revision < 0:
            return None
        if not _is_valid_int(registered.generation) or registered.generation < 0:
            return None
        if not isinstance(registered.workspace, str) or not registered.workspace.strip():
            return None

        # Verify caller-provided data matches registered grant exactly
        if caller_grant is not None:
            if caller_grant != registered:
                return None
        elif isinstance(raw_task_data, dict):
            # Check consistency of critical fields
            for field in ("owner_tag", "workspace", "session_id", "generation", "source_id", "source_digest", "status"):
                if field in raw_task_data:
                    if str(raw_task_data[field]) != str(getattr(registered, field, "")):
                        return None

        # Validate time bounds on the authoritative registered grant
        if not self._validate_grant_time(registered, now_utc):
            return None

        # Validate source digest non-empty and valid hex
        if not registered.source_id or not registered.source_digest or len(registered.source_digest) < 16:
            return None

        # Status check: must be explicitly provided in known trusted statuses
        trusted_statuses = {
            "ready", "in_progress", "blocked", "frozen",
            "quota_held", "manual_approval", "completed", "done", "accepted"
        }
        if registered.status not in trusted_statuses:
            return None

        return registered

    def revalidate_authority(
        self,
        task_id: str,
        revision: int,
        workspace: str,
        now_utc: datetime.datetime,
    ) -> Optional[TrustedAuthorityGrant]:
        if not _is_valid_int(revision):
            return None
        if not isinstance(workspace, str) or not workspace.strip():
            return None
        key = (task_id, revision)
        grant = self._grants.get(key)
        if grant is None:
            return None
        if not _is_valid_int(grant.revision) or not _is_valid_int(grant.generation):
            return None
        if not isinstance(grant.workspace, str) or not grant.workspace.strip():
            return None
        if os.path.realpath(os.path.abspath(grant.workspace)) != os.path.realpath(os.path.abspath(workspace)):
            return None
        if not self._validate_grant_time(grant, now_utc):
            return None
        return grant

    def _validate_grant_time(self, grant: TrustedAuthorityGrant, now_utc: datetime.datetime) -> bool:
        try:
            issued_dt = parse_strict_iso_utc(grant.issued_at)
        except Exception:
            return False

        # Future timestamp check
        if issued_dt > now_utc:
            return False

        # Freshness threshold check
        age = (now_utc - issued_dt).total_seconds()
        if age > self.freshness_threshold_seconds:
            return False

        # Expiration check: strict boundary >=
        if grant.expires_at:
            try:
                exp_dt = parse_strict_iso_utc(grant.expires_at)
                if now_utc >= exp_dt:
                    return False
            except Exception:
                return False

        return True


# --- Receipt Verifiers ---

class DefaultDenyReceiptVerifier:
    """Default receipt verifier that strictly denies receipts unless an authentic verifier is injected."""

    def verify_receipt(
        self,
        receipt: Dict[str, Any],
        request: TransportSendRequest,
        now_utc: datetime.datetime,
    ) -> Tuple[bool, str]:
        return False, "DefaultDenyReceiptVerifier: no authentic receipt verifier injected; receipt denied"


# --- Adapter Dispatch Guard ---

class AdapterDispatchGuard(GuardProtocol):
    """Guard implementation revalidating CURRENT authority, binding, and native state.

    CRITICAL INVARIANT: Runs inside InterventionLedger.dispatch_outbox_message's
    BEGIN IMMEDIATE transaction. MUST NOT MUTATE LEDGER.
    """

    def __init__(
        self,
        ledger: InterventionLedger,
        authority_resolver: AuthorityResolverProtocol,
        binding_provider: BindingProviderProtocol,
        native_guard: NativeGuardInterface,
        transport_callbacks: TransportRouteCallbacks,
        clock: Optional[Callable[[], datetime.datetime]] = None,
        completed_obligations: Optional[Set[str]] = None,
        expected_dependencies: Optional[Sequence[str]] = None,
        dependency_verifier: Optional[DependencyCompletionVerifierProtocol] = None,
    ):
        self.ledger = ledger
        self.authority_resolver = authority_resolver
        self.binding_provider = binding_provider
        self.native_guard = native_guard
        self.transport_callbacks = transport_callbacks
        self._clock = clock or (lambda: datetime.datetime.now(datetime.timezone.utc))
        self.completed_obligations = completed_obligations
        self.expected_dependencies = tuple(expected_dependencies) if expected_dependencies is not None else None
        self.dependency_verifier = dependency_verifier

    def _now(self) -> datetime.datetime:
        dt = self._clock()
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.astimezone(datetime.timezone.utc)

    def authorize_dispatch(self, message: OutboxMessage) -> Tuple[bool, str]:
        now = self._now()

        # 1. Resolve full trusted tuple from ledger intervention record
        intv = self.ledger.get_intervention(message.intervention_id)
        if not intv:
            return False, f"Intervention {message.intervention_id} not found in ledger"

        if not _is_valid_int(intv.obligation_revision) or not _is_valid_int(intv.generation):
            return False, "Intervention record has non-integer revision or generation"
        if not intv.workspace or not intv.workspace.strip():
            return False, "Intervention record has empty workspace"

        # 2. Check route validity and callback presence
        cb = self.transport_callbacks.get_callback(message.route)
        if cb is None:
            return False, f"No transport route callback registered for route '{message.route}'; default allow denied"

        # 3. Route policy: Principal is strictly bus-only
        is_principal = (
            intv.is_principal
            or message.is_principal
            or intv.owner_tag.strip().lower() == "codex-principal"
            or "principal" in intv.owner_tag.strip().lower()
        )
        if is_principal and message.route != ROUTE_BUS:
            return False, "Principal continuation refused: principal must use bus-only route"

        # 4. Revalidate CURRENT authority provenance
        try:
            grant = self.authority_resolver.revalidate_authority(
                task_id=intv.obligation_id,
                revision=intv.obligation_revision,
                workspace=intv.workspace,
                now_utc=now,
            )
        except Exception as exc:
            return False, f"Authority revalidation error: {exc}"

        if grant is None:
            return False, "Current authority revalidation failed: grant missing, expired, or invalid"

        if not _is_valid_int(grant.revision) or not _is_valid_int(grant.generation):
            return False, "Authority grant has non-integer revision or generation"
        if not grant.workspace or not grant.workspace.strip():
            return False, "Authority grant has empty workspace"

        # Check tuple equality against current authority grant
        if not isinstance(grant.is_principal, bool) or grant.is_principal != intv.is_principal:
            return False, "Authority principal designation mismatch"
        if grant.task_id != intv.obligation_id:
            return False, f"Authority revalidation task mismatch ({grant.task_id} != {intv.obligation_id})"
        if grant.revision != intv.obligation_revision:
            return False, f"Authority revalidation revision mismatch ({grant.revision} != {intv.obligation_revision})"
        if grant.owner_tag != intv.owner_tag:
            return False, f"Authority revalidation owner mismatch ({grant.owner_tag} != {intv.owner_tag})"
        if os.path.realpath(os.path.abspath(grant.workspace)) != intv.workspace:
            return False, "Authority revalidation workspace mismatch"
        if grant.session_id != intv.session_id:
            return False, f"Authority revalidation session mismatch ({grant.session_id} != {intv.session_id})"
        if grant.generation != intv.generation:
            return False, f"Authority revalidation generation mismatch ({grant.generation} != {intv.generation})"

        ok, reason = _validate_authority_protection_and_dependencies(
            grant=grant,
            completed_obligations=self.completed_obligations,
            expected_dependencies=self.expected_dependencies,
            dependency_verifier=self.dependency_verifier,
            now_utc=now,
        )
        if not ok:
            return False, f"Authority revalidation failed: {reason}"

        # 5. Revalidate CURRENT session binding
        try:
            curr_binding = self.binding_provider.get_current_binding(
                workspace=intv.workspace,
                session_id=intv.session_id,
                now_utc=now,
            )
        except Exception as exc:
            return False, f"Binding provider error: {exc}"

        if curr_binding is None:
            return False, "Current session binding missing"
        if not _is_valid_int(curr_binding.generation):
            return False, "Current session binding has non-integer generation"
        if not curr_binding.workspace or not curr_binding.workspace.strip():
            return False, "Current session binding has empty workspace"
        if curr_binding.active is not True or curr_binding.worker_alive is not True or curr_binding.phase == "exited":
            return False, f"Current session binding inactive or exited (active={curr_binding.active}, alive={curr_binding.worker_alive}, phase={curr_binding.phase})"
        if curr_binding.session_id != intv.session_id:
            return False, f"Session binding changed ({curr_binding.session_id} != {intv.session_id})"
        if curr_binding.generation != intv.generation:
            return False, f"Session generation changed ({curr_binding.generation} != {intv.generation})"
        if curr_binding.tag != intv.owner_tag:
            return False, f"Session owner tag mismatch ({curr_binding.tag} != {intv.owner_tag})"
        if os.path.realpath(os.path.abspath(curr_binding.workspace)) != intv.workspace:
            return False, "Session binding workspace mismatch"

        # 6. Revalidate CURRENT native guard state (for native continuation route)
        if message.route == ROUTE_NATIVE_MAILBOX:
            if self.native_guard is None:
                return False, "Native guard interface missing for native mailbox route"
            try:
                ng_res = self.native_guard.check_native_state(
                    workspace=intv.workspace,
                    session_id=intv.session_id,
                    generation=intv.generation,
                    now_utc=now,
                )
            except Exception as exc:
                return False, f"Native guard check raised exception: {exc}"

            if ng_res is None:
                return False, "Native guard returned None; explicit readiness and liveness mandatory"
            if ng_res.is_quota_held:
                return False, f"Native guard refused: quota held ({ng_res.reason})"
            if ng_res.is_frozen:
                return False, f"Native guard refused: session frozen ({ng_res.reason})"
            if ng_res.is_blocked:
                return False, f"Native guard refused: session blocked ({ng_res.reason})"
            if ng_res.is_manual_approval:
                return False, f"Native guard refused: manual approval required ({ng_res.reason})"
            if ng_res.is_draft:
                return False, f"Native guard refused: draft state ({ng_res.reason})"
            if ng_res.is_menu_active:
                return False, f"Native guard refused: menu active ({ng_res.reason})"
            if ng_res.is_saved_goal:
                return False, f"Native guard refused: saved goal state ({ng_res.reason})"
            if not isinstance(ng_res.is_ready, bool) or ng_res.is_ready is not True:
                return False, f"Native guard refused: not ready ({ng_res.reason})"
            if not isinstance(ng_res.session_alive, bool) or ng_res.session_alive is not True:
                return False, "Native guard refused: session not alive"
            if ng_res.current_generation is not None:
                if not _is_valid_int(ng_res.current_generation) or ng_res.current_generation != intv.generation:
                    return False, f"Native guard generation mismatch ({ng_res.current_generation} != {intv.generation})"
            if not isinstance(ng_res.allowed, bool) or ng_res.allowed is not True:
                return False, f"Native guard disallowed dispatch: {ng_res.reason}"

        return True, "Dispatch authorized by adapter guard"


# --- Adapter Transport ---

class AdapterTransport(TransportProtocol):
    """Transport protocol implementation that preserves idempotency key, canonical payload,

    resolves full trusted tuple from ledger intervention, performs immediate pre-send revalidation,
    and verifies returned message receipts via an authentic verifier.
    """

    def __init__(
        self,
        ledger: InterventionLedger,
        callbacks: TransportRouteCallbacks,
        authority_resolver: Optional[AuthorityResolverProtocol] = None,
        binding_provider: Optional[BindingProviderProtocol] = None,
        native_guard: Optional[NativeGuardInterface] = None,
        receipt_verifier: Optional[ReceiptVerifierProtocol] = None,
        clock: Optional[Callable[[], datetime.datetime]] = None,
        completed_obligations: Optional[Set[str]] = None,
        expected_dependencies: Optional[Sequence[str]] = None,
        dependency_verifier: Optional[DependencyCompletionVerifierProtocol] = None,
    ):
        self.ledger = ledger
        self.callbacks = callbacks
        self.authority_resolver = authority_resolver
        self.binding_provider = binding_provider
        self.native_guard = native_guard
        self.receipt_verifier = receipt_verifier or DefaultDenyReceiptVerifier()
        self._clock = clock or (lambda: datetime.datetime.now(datetime.timezone.utc))
        self.completed_obligations = completed_obligations
        self.expected_dependencies = tuple(expected_dependencies) if expected_dependencies is not None else None
        self.dependency_verifier = dependency_verifier
        self.last_sent_request: Optional[TransportSendRequest] = None
        self.last_receipt: Optional[Dict[str, Any]] = None

    def _now(self) -> datetime.datetime:
        dt = self._clock()
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.astimezone(datetime.timezone.utc)

    def send(
        self,
        route: str,
        recipient_tag: str,
        payload: Dict[str, Any],
        idempotency_key: str,
    ) -> Dict[str, Any]:
        now = self._now()

        # Context mandatory check on direct transport execution: missing authority or binding strictly fails before callback
        if self.authority_resolver is None or self.binding_provider is None:
            raise TransportSendFailedBeforeDelivery(
                "Missing authority_resolver or binding_provider on transport; cannot verify provenance before callback"
            )

        if route == ROUTE_NATIVE_MAILBOX and self.native_guard is None:
            raise TransportSendFailedBeforeDelivery(
                "Missing native_guard on transport for native mailbox route; cannot verify native state before callback"
            )

        # 1. Resolve full trusted tuple from ledger intervention record via outbox/dedup
        cur = self.ledger._conn.cursor()
        cur.execute("SELECT intervention_id, message_id FROM outbox WHERE dedup_key = ?", (idempotency_key,))
        row = cur.fetchone()
        if not row:
            raise TransportSendFailedBeforeDelivery(f"Outbox record not found for idempotency key '{idempotency_key}'")

        intervention_id, message_id = row[0], row[1]
        intv = self.ledger.get_intervention(intervention_id)
        if not intv:
            raise TransportSendFailedBeforeDelivery(f"Intervention '{intervention_id}' not found in ledger")

        # 2. Check route callback
        callback = self.callbacks.get_callback(route)
        if callback is None:
            raise TransportSendFailedBeforeDelivery(
                f"Missing route callback for route '{route}'; default allow denied"
            )

        # 3. Check principal route
        if intv.is_principal and route != ROUTE_BUS:
            raise TransportSendFailedBeforeDelivery(
                "Principal continuation refused: principal messages must use bus-only route"
            )

        # 4. Immediate pre-send revalidation: re-check current authority, binding, and guard state
        # Detects any mutation that occurred between guard authorization and send execution.
        # If any check fails, zero callback calls occur.
        try:
            grant = self.authority_resolver.revalidate_authority(
                task_id=intv.obligation_id,
                revision=intv.obligation_revision,
                workspace=intv.workspace,
                now_utc=now,
            )
        except Exception as exc:
            raise TransportSendFailedBeforeDelivery(f"Pre-send authority recheck error: {exc}") from exc

        if grant is None:
            raise TransportSendFailedBeforeDelivery("Pre-send authority recheck failed: authority revoked or missing")
        if not _is_valid_int(grant.revision) or not _is_valid_int(grant.generation):
            raise TransportSendFailedBeforeDelivery("Pre-send authority recheck failed: non-integer revision or generation")
        if not grant.workspace or not grant.workspace.strip():
            raise TransportSendFailedBeforeDelivery("Pre-send authority recheck failed: empty workspace")
        if (
            grant.task_id != intv.obligation_id
            or grant.revision != intv.obligation_revision
            or grant.owner_tag != intv.owner_tag
            or os.path.realpath(os.path.abspath(grant.workspace)) != intv.workspace
            or grant.session_id != intv.session_id
            or grant.generation != intv.generation
            or not isinstance(grant.is_principal, bool)
            or grant.is_principal != intv.is_principal
        ):
            raise TransportSendFailedBeforeDelivery("Pre-send authority recheck failed: authority tuple mismatch")

        ok, reason = _validate_authority_protection_and_dependencies(
            grant=grant,
            completed_obligations=self.completed_obligations,
            expected_dependencies=self.expected_dependencies,
            dependency_verifier=self.dependency_verifier,
            now_utc=now,
        )
        if not ok:
            raise TransportSendFailedBeforeDelivery(f"Pre-send authority recheck failed: {reason}")

        try:
            curr_b = self.binding_provider.get_current_binding(
                workspace=intv.workspace,
                session_id=intv.session_id,
                now_utc=now,
            )
        except Exception as exc:
            raise TransportSendFailedBeforeDelivery(f"Pre-send binding recheck error: {exc}") from exc

        if curr_b is None:
            raise TransportSendFailedBeforeDelivery("Pre-send binding recheck failed: binding missing")
        if not _is_valid_int(curr_b.generation):
            raise TransportSendFailedBeforeDelivery("Pre-send binding recheck failed: non-integer generation")
        if not curr_b.workspace or not curr_b.workspace.strip():
            raise TransportSendFailedBeforeDelivery("Pre-send binding recheck failed: empty workspace")
        if (
            curr_b.active is not True
            or curr_b.worker_alive is not True
            or curr_b.phase == "exited"
            or curr_b.generation != intv.generation
        ):
            raise TransportSendFailedBeforeDelivery(
                "Pre-send binding recheck failed: session inactive, dead, exited, or generation advanced"
            )
        if curr_b.session_id != intv.session_id or curr_b.tag != intv.owner_tag:
            raise TransportSendFailedBeforeDelivery("Pre-send binding recheck failed: binding tuple mismatch")
        if os.path.realpath(os.path.abspath(curr_b.workspace)) != intv.workspace:
            raise TransportSendFailedBeforeDelivery("Pre-send binding recheck failed: workspace mismatch")

        if route == ROUTE_NATIVE_MAILBOX:
            try:
                ng = self.native_guard.check_native_state(
                    workspace=intv.workspace,
                    session_id=intv.session_id,
                    generation=intv.generation,
                    now_utc=now,
                )
            except Exception as exc:
                raise TransportSendFailedBeforeDelivery(f"Pre-send native guard check raised exception: {exc}") from exc

            if ng is None:
                raise TransportSendFailedBeforeDelivery("Pre-send native guard check failed: returned None")
            if ng.is_quota_held:
                raise TransportSendFailedBeforeDelivery(f"Pre-send native guard recheck failed: quota held ({ng.reason})")
            if ng.is_frozen:
                raise TransportSendFailedBeforeDelivery(f"Pre-send native guard recheck failed: session frozen ({ng.reason})")
            if ng.is_blocked:
                raise TransportSendFailedBeforeDelivery(f"Pre-send native guard recheck failed: session blocked ({ng.reason})")
            if ng.is_manual_approval:
                raise TransportSendFailedBeforeDelivery(f"Pre-send native guard recheck failed: manual approval required ({ng.reason})")
            if ng.is_draft:
                raise TransportSendFailedBeforeDelivery(f"Pre-send native guard recheck failed: draft state ({ng.reason})")
            if ng.is_menu_active:
                raise TransportSendFailedBeforeDelivery(f"Pre-send native guard recheck failed: menu active ({ng.reason})")
            if ng.is_saved_goal:
                raise TransportSendFailedBeforeDelivery(f"Pre-send native guard recheck failed: saved goal state ({ng.reason})")
            if not isinstance(ng.is_ready, bool) or ng.is_ready is not True:
                raise TransportSendFailedBeforeDelivery(
                    f"Pre-send native guard recheck failed: not ready ({ng.reason})"
                )
            if not isinstance(ng.session_alive, bool) or ng.session_alive is not True:
                raise TransportSendFailedBeforeDelivery(
                    "Pre-send native guard recheck failed: session not alive"
                )
            if ng.current_generation is not None:
                if not _is_valid_int(ng.current_generation) or ng.current_generation != intv.generation:
                    raise TransportSendFailedBeforeDelivery("Pre-send native guard recheck failed: generation mismatch")
            if not isinstance(ng.allowed, bool) or ng.allowed is not True:
                raise TransportSendFailedBeforeDelivery(f"Pre-send native guard recheck disallowed: {ng.reason}")

        # 5. Construct TransportSendRequest with canonical payload and full tuple
        canonical_payload = sanitize_payload(payload)
        request = TransportSendRequest(
            route=route,
            recipient_tag=recipient_tag,
            payload=canonical_payload,
            idempotency_key=idempotency_key,
            obligation_id=intv.obligation_id,
            obligation_revision=intv.obligation_revision,
            owner_tag=intv.owner_tag,
            workspace=intv.workspace,
            session_id=intv.session_id,
            generation=intv.generation,
            intervention_type=intv.intervention_type,
            is_principal=intv.is_principal,
            intervention_id=intervention_id,
            message_id=message_id,
        )

        # 6. Invoke transport callback
        try:
            raw_receipt = callback(request)
        except TransportSendFailedBeforeDelivery:
            # Re-raise pre-send failures directly (zero uncertainty recorded)
            raise
        except TransportUncertainError:
            # Re-raise uncertain errors directly
            raise
        except Exception as exc:
            # Any unexpected error or timeout during/after execution represents uncertain delivery!
            raise TransportUncertainError(f"Transport send call error: {exc}") from exc

        # 7. Validate returned receipt proof via injected receipt verifier
        # Injected verifier boundary must catch exceptions and missing/nonbool verification as post-call uncertainty, no retry.
        try:
            verif_res = self.receipt_verifier.verify_receipt(raw_receipt, request, now)
            if not isinstance(verif_res, tuple) or len(verif_res) != 2:
                raise TransportUncertainError(
                    f"Post-call uncertainty: receipt verifier returned invalid result {verif_res!r}; expected Tuple[bool, str]"
                )
            is_verified, reason = verif_res
            if not isinstance(is_verified, bool) or is_verified is not True:
                raise TransportUncertainError(
                    f"Post-call uncertainty: receipt verification failed: {reason}"
                )
        except TransportUncertainError:
            raise
        except Exception as exc:
            raise TransportUncertainError(
                f"Post-call uncertainty: receipt verifier raised exception: {exc}"
            ) from exc

        self.last_sent_request = request
        self.last_receipt = raw_receipt
        return raw_receipt


# --- Oversight Adapter ---

class OversightAdapter:
    """Offline trusted-obligation and guarded-transport adapter for Agent Hub."""

    def __init__(
        self,
        ledger: InterventionLedger,
        policy: Optional[OversightPolicy] = None,
        authority_resolver: Optional[AuthorityResolverProtocol] = None,
        binding_provider: Optional[BindingProviderProtocol] = None,
        receipt_verifier: Optional[ReceiptVerifierProtocol] = None,
        clock: Optional[Callable[[], datetime.datetime]] = None,
        dependency_verifier: Optional[DependencyCompletionVerifierProtocol] = None,
    ):
        self.ledger = ledger
        self.policy = policy or OversightPolicy()
        self.authority_resolver = authority_resolver
        self.binding_provider = binding_provider
        self.receipt_verifier = receipt_verifier or DefaultDenyReceiptVerifier()
        self._clock = clock or (lambda: datetime.datetime.now(datetime.timezone.utc))
        self.dependency_verifier = dependency_verifier

        # Initialize adapter-owned durable table for receipt digest persistence
        # Zero destructive mutation of accepted ledger tables. Qualify actual SQLite storage failure.
        try:
            with self.ledger._conn:
                self.ledger._conn.execute("""
                    CREATE TABLE IF NOT EXISTS adapter_receipt_digests (
                        stage_key TEXT PRIMARY KEY,
                        intervention_id TEXT NOT NULL,
                        stage TEXT NOT NULL,
                        digest TEXT NOT NULL,
                        recorded_at TEXT NOT NULL
                    );
                """)
                self.ledger._conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_adapter_digests_intv
                    ON adapter_receipt_digests(intervention_id, stage);
                """)
        except sqlite3.Error as exc:
            raise AdapterError(f"SQLite storage failure initializing adapter tables: {exc}") from exc

    def _now(self) -> datetime.datetime:
        dt = self._clock()
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.astimezone(datetime.timezone.utc)

    def qualify_authority_inputs(
        self,
        raw_task_data: Any,
        authority_resolver: Optional[AuthorityResolverProtocol] = None,
        dependency_verifier: Optional[DependencyCompletionVerifierProtocol] = None,
        now_utc: Optional[datetime.datetime] = None,
    ) -> AuthorityQualificationResult:
        """Read-only capability and qualification assessment of authority and dependency providers.

        Does not mutate ledger or send messages. Evaluates whether production trust is qualified
        against exact tuple boundaries and required independent providers.
        """
        now = now_utc or self._now()
        resolver = authority_resolver or self.authority_resolver
        dep_verifier = dependency_verifier or self.dependency_verifier

        if resolver is None:
            return AuthorityQualificationResult(
                is_qualified=False,
                status=QUALIFICATION_STATUS_DENIED,
                reason="authority_resolver is missing",
                missing_providers=REQUIRED_INDEPENDENT_PROVIDERS,
                verification_boundary=EXACT_TUPLE_BOUNDARY_FIELDS,
                dependency_status=QUALIFICATION_STATUS_UNKNOWN,
            )

        try:
            grant = resolver.resolve_authority(raw_task_data, now)
        except Exception as exc:
            return AuthorityQualificationResult(
                is_qualified=False,
                status=QUALIFICATION_STATUS_DENIED,
                reason=f"Authority resolution exception: {exc}",
                missing_providers=REQUIRED_INDEPENDENT_PROVIDERS,
                verification_boundary=EXACT_TUPLE_BOUNDARY_FIELDS,
                dependency_status=QUALIFICATION_STATUS_UNKNOWN,
                details={"exception": str(exc)},
            )

        if grant is None:
            return AuthorityQualificationResult(
                is_qualified=False,
                status=QUALIFICATION_STATUS_DENIED,
                reason="Authority resolution failed: untrusted, unregistered, or malformed task provenance",
                missing_providers=REQUIRED_INDEPENDENT_PROVIDERS,
                verification_boundary=EXACT_TUPLE_BOUNDARY_FIELDS,
                dependency_status=QUALIFICATION_STATUS_UNKNOWN,
            )

        if not isinstance(grant, TrustedAuthorityGrant):
            return AuthorityQualificationResult(
                is_qualified=False,
                status=QUALIFICATION_STATUS_DENIED,
                reason=f"Authority resolution returned invalid grant type: {type(grant).__name__}",
                missing_providers=REQUIRED_INDEPENDENT_PROVIDERS,
                verification_boundary=EXACT_TUPLE_BOUNDARY_FIELDS,
                dependency_status=QUALIFICATION_STATUS_UNKNOWN,
            )

        # Check dependencies
        deps = grant.dependencies or ()
        if deps:
            # Independent completion authority provider is absent on host.
            # Dependencies remain explicitly HELD. Raw bools/caller flags cannot unhold dependencies.
            dep_status = QUALIFICATION_STATUS_HELD
            dep_reason = (
                f"Authority has non-empty dependencies {deps}: dependency authority is HELD "
                f"(independent current exactbound dependency-completion verifier absent on host; "
                f"caller flags and boolean verifiers cannot establish semantic trust)"
            )
            if dep_verifier is not None:
                for d in deps:
                    try:
                        v_res = dep_verifier.verify_dependency_completion(
                            dependency_task_id=d,
                            expected_workspace=grant.workspace,
                            now_utc=now,
                        )
                        if not isinstance(v_res, tuple) or len(v_res) != 2:
                            dep_reason = f"Dependency verifier returned invalid result for '{d}': {v_res!r}"
                            break
                        ok_b, r_text = v_res
                        if not isinstance(ok_b, bool):
                            dep_reason = f"Dependency verifier returned non-bool for '{d}': {ok_b!r}"
                            break
                        if not ok_b:
                            dep_reason = f"Dependency '{d}' unverified (HELD): {r_text}"
                            break
                    except Exception as e:
                        dep_reason = f"Dependency verifier exception for '{d}': {e}"
                        break
        else:
            dep_status = "NOT_APPLICABLE"
            dep_reason = "No dependencies declared"

        # Production qualification assessment:
        # StandardAuthorityResolver is explicitly offline reference only
        is_offline_ref = getattr(resolver, "is_offline_reference", False) or isinstance(resolver, StandardAuthorityResolver)

        details = {
            "task_id": grant.task_id,
            "revision": grant.revision,
            "owner_tag": grant.owner_tag,
            "workspace": grant.workspace,
            "session_id": grant.session_id,
            "generation": grant.generation,
            "source_id": grant.source_id,
            "source_digest": grant.source_digest,
            "status": grant.status,
            "is_principal": grant.is_principal,
            "dependencies": grant.dependencies,
            "is_offline_reference": is_offline_ref,
            "production_provider_present": False,
            "production_qualified": False,
        }

        if deps and dep_status == QUALIFICATION_STATUS_HELD:
            return AuthorityQualificationResult(
                is_qualified=False,
                status=QUALIFICATION_STATUS_HELD,
                reason=dep_reason,
                missing_providers=REQUIRED_INDEPENDENT_PROVIDERS,
                verification_boundary=EXACT_TUPLE_BOUNDARY_FIELDS,
                dependency_status=dep_status,
                details=details,
            )

        if is_offline_ref:
            return AuthorityQualificationResult(
                is_qualified=False,
                status=QUALIFICATION_STATUS_OFFLINE_REFERENCE,
                reason="StandardAuthorityResolver memory registration is offline reference seam only; production trust not established",
                missing_providers=REQUIRED_INDEPENDENT_PROVIDERS,
                verification_boundary=EXACT_TUPLE_BOUNDARY_FIELDS,
                dependency_status=dep_status,
                details=details,
            )

        return AuthorityQualificationResult(
            is_qualified=False,
            status=QUALIFICATION_STATUS_UNKNOWN,
            reason="Independent authenticated task/custody authority provider absent on host",
            missing_providers=REQUIRED_INDEPENDENT_PROVIDERS,
            verification_boundary=EXACT_TUPLE_BOUNDARY_FIELDS,
            dependency_status=dep_status,
            details=details,
        )

    # Alias qualify_authority to qualify_authority_inputs
    qualify_authority = qualify_authority_inputs

    def evaluate_and_dispatch(
        self,
        raw_task_data: Any,
        authority_resolver: AuthorityResolverProtocol,
        binding_provider: BindingProviderProtocol,
        observation_provider: ObservationProviderProtocol,
        native_guard: NativeGuardInterface,
        transport_callbacks: TransportRouteCallbacks,
        receipt_verifier: Optional[ReceiptVerifierProtocol] = None,
        completed_obligations: Optional[Set[str]] = None,
        recent_actions: Optional[Sequence[ActionReceipt]] = None,
        dependency_verifier: Optional[DependencyCompletionVerifierProtocol] = None,
    ) -> AdapterExecutionResult:
        """Execute a single offline evaluation and guarded dispatch cycle."""
        now = self._now()
        self.authority_resolver = authority_resolver
        self.binding_provider = binding_provider
        verifier = receipt_verifier or self.receipt_verifier
        dep_verifier = dependency_verifier or self.dependency_verifier

        if authority_resolver is None:
            return AdapterExecutionResult(
                decision_made=False,
                refusal_reason="authority_resolver is missing",
            )

        # 1. Resolve explicit trusted authority grant against authoritative registration
        try:
            grant = authority_resolver.resolve_authority(raw_task_data, now)
        except Exception as exc:
            return AdapterExecutionResult(
                decision_made=False,
                refusal_reason=f"Authority resolution exception: {exc}",
            )

        if grant is None:
            return AdapterExecutionResult(
                decision_made=False,
                refusal_reason="Authority resolution failed: untrusted, unregistered, or malformed task provenance",
            )

        if not isinstance(grant, TrustedAuthorityGrant):
            return AdapterExecutionResult(
                decision_made=False,
                refusal_reason=f"Authority resolution returned invalid grant type: {type(grant).__name__}",
            )

        if grant.protected_reason is not None and (
            not isinstance(grant.protected_reason, str) or not grant.protected_reason.strip()
        ):
            return AdapterExecutionResult(
                decision_made=False,
                refusal_reason="Authority resolution failed: malformed protected reason",
            )

        # 2. Pre-decision fail-closed authority protection and dependency qualification
        # Default/missing/unknown/unqualified provider denies readiness/dependency receipt
        # BEFORE any transport or ledger stage/digest mutation.
        ok, reason = _validate_authority_protection_and_dependencies(
            grant=grant,
            completed_obligations=completed_obligations,
            expected_dependencies=None,
            dependency_verifier=dep_verifier,
            now_utc=now,
        )
        if not ok:
            return AdapterExecutionResult(
                decision_made=False,
                refusal_reason=f"Authority pre-decision validation refused: {reason}",
            )

        obligation = grant.to_obligation()

        # 2. Resolve native session binding
        binding = binding_provider.get_current_binding(grant.workspace, grant.session_id, now)
        if binding is None:
            return AdapterExecutionResult(
                decision_made=False,
                refusal_reason="Native session binding missing",
            )

        # 3. Resolve observation evidence
        observation = observation_provider.get_current_observation(
            grant.workspace, grant.session_id, grant.generation, now
        )

        # 4. Classify and decide via accepted OversightPolicy
        decision = self.policy.decide(
            obligation=obligation,
            binding=binding,
            observation=observation,
            now_utc=now,
            completed_obligations=completed_obligations,
            recent_actions=recent_actions,
        )

        if decision is None:
            return AdapterExecutionResult(
                decision_made=False,
                refusal_reason="No intervention warranted by policy classification",
            )

        # 5. Record decision in durable ledger
        intv = self.ledger.record_decision(decision)

        if intv.outbox_message_id is None:
            # Deterministic dedup or tombstone replay without new outbox message
            return AdapterExecutionResult(
                decision_made=True,
                decision=decision,
                intervention=intv,
                refusal_reason="Intervention recorded but outbox message already resolved or deduplicated",
            )

        outbox_msg = self.ledger.get_outbox_message(intv.outbox_message_id)
        if outbox_msg is None or outbox_msg.status != OUTBOX_PENDING:
            return AdapterExecutionResult(
                decision_made=True,
                decision=decision,
                intervention=intv,
                outbox_message=outbox_msg,
                refusal_reason="Outbox message not pending dispatch",
            )

        # 6. Fence check: if session generation advanced between classify and dispatch, fence immediately!
        curr_b = binding_provider.get_current_binding(grant.workspace, grant.session_id, now)
        if curr_b is not None and (curr_b.generation > intv.generation or curr_b.session_id != intv.session_id):
            self.ledger.fence_superseded_sessions(
                owner_tag=intv.owner_tag,
                workspace=intv.workspace,
                new_session_id=curr_b.session_id,
                new_generation=curr_b.generation,
            )
            return AdapterExecutionResult(
                decision_made=True,
                decision=decision,
                intervention=intv,
                outbox_message=outbox_msg,
                refusal_reason="Session reassignment detected before dispatch; fenced session",
            )

        # 7. Construct guard and transport with immediate pre-send revalidation
        initial_deps = tuple(grant.dependencies) if grant.dependencies else ()
        guard = AdapterDispatchGuard(
            ledger=self.ledger,
            authority_resolver=authority_resolver,
            binding_provider=binding_provider,
            native_guard=native_guard,
            transport_callbacks=transport_callbacks,
            clock=self._clock,
            completed_obligations=completed_obligations,
            expected_dependencies=initial_deps,
            dependency_verifier=dep_verifier,
        )
        transport = AdapterTransport(
            ledger=self.ledger,
            callbacks=transport_callbacks,
            authority_resolver=authority_resolver,
            binding_provider=binding_provider,
            native_guard=native_guard,
            receipt_verifier=verifier,
            clock=self._clock,
            completed_obligations=completed_obligations,
            expected_dependencies=initial_deps,
            dependency_verifier=dep_verifier,
        )

        # 8. Dispatch outbox message
        try:
            sent = self.ledger.dispatch_outbox_message(
                message_id=outbox_msg.message_id,
                transport=transport,
                guard=guard,
            )
        except GuardAuthorizationError as exc:
            return AdapterExecutionResult(
                decision_made=True,
                decision=decision,
                intervention=intv,
                outbox_message=outbox_msg,
                dispatch_attempted=False,
                dispatch_sent=False,
                refusal_reason=f"Guard authorization refused: {exc}",
            )
        except TransportError as exc:
            final_intv = self.ledger.get_intervention(intv.id)
            final_outbox = self.ledger.get_outbox_message(outbox_msg.message_id)
            is_uncertain = (final_outbox.status == OUTBOX_UNCERTAIN) if final_outbox else False
            return AdapterExecutionResult(
                decision_made=True,
                decision=decision,
                intervention=final_intv or intv,
                outbox_message=final_outbox or outbox_msg,
                dispatch_attempted=True,
                dispatch_sent=False,
                dispatched_uncertain=is_uncertain,
                dispatch_failed=not is_uncertain,
                refusal_reason=f"Transport error: {exc}",
            )

        final_intv = self.ledger.get_intervention(intv.id)
        final_outbox = self.ledger.get_outbox_message(outbox_msg.message_id)
        is_uncertain = (final_outbox.status == OUTBOX_UNCERTAIN) if final_outbox else False
        is_failed = (final_outbox.status == OUTBOX_FAILED) if final_outbox else False

        refusal_reason = None
        if not sent:
            if is_uncertain:
                cur = self.ledger._conn.cursor()
                cur.execute(
                    "SELECT detail FROM audit_events WHERE intervention_id = ? AND event_type = 'dispatch_uncertain' ORDER BY id DESC LIMIT 1",
                    (intv.id,)
                )
                ev = cur.fetchone()
                refusal_reason = f"Dispatch uncertain: {ev[0]}" if ev and ev[0] else "Dispatch uncertain"
            elif is_failed:
                cur = self.ledger._conn.cursor()
                cur.execute(
                    "SELECT detail FROM audit_events WHERE intervention_id = ? AND event_type = 'dispatch_failed_before_send' ORDER BY id DESC LIMIT 1",
                    (intv.id,)
                )
                ev = cur.fetchone()
                refusal_reason = f"Dispatch failed before send: {ev[0]}" if ev and ev[0] else "Dispatch failed before send"
            else:
                refusal_reason = "Dispatch was not confirmed sent"

        return AdapterExecutionResult(
            decision_made=True,
            decision=decision,
            intervention=final_intv or intv,
            outbox_message=final_outbox or outbox_msg,
            dispatch_attempted=True,
            dispatch_sent=sent,
            dispatched_uncertain=is_uncertain,
            dispatch_failed=is_failed,
            refusal_reason=refusal_reason,
            receipt=transport.last_receipt,
        )

    # --- Receipt Ingress Verification and Recording ---

    def _compute_digest(self, payload: Any) -> str:
        """Compute canonical JSON sha256 digest of payload for replay protection."""
        canon = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return hashlib.sha256(canon.encode("utf-8")).hexdigest()

    def _revalidate_ingress_authority_and_binding(
        self,
        intv: InterventionRecord,
        now: datetime.datetime,
    ) -> None:
        """Mandatory authority and session binding revalidation on ALL receipt ingress and replay."""
        if self.authority_resolver is None or self.binding_provider is None:
            raise SecurityBindingError(
                "authority_resolver and binding_provider are mandatory on receipt ingress; missing context forbidden"
            )

        # 1. Authority revalidation
        try:
            grant = self.authority_resolver.revalidate_authority(
                task_id=intv.obligation_id,
                revision=intv.obligation_revision,
                workspace=intv.workspace,
                now_utc=now,
            )
        except Exception as exc:
            raise SecurityBindingError(f"Authority revalidation error: {exc}") from exc

        if grant is None:
            raise SecurityBindingError("Current authority revoked or missing on receipt ingress")

        if not _is_valid_int(grant.revision) or not _is_valid_int(grant.generation):
            raise SecurityBindingError("Authority grant has non-integer revision or generation")
        if not grant.workspace or not grant.workspace.strip():
            raise SecurityBindingError("Authority grant has empty workspace")

        if (
            grant.task_id != intv.obligation_id
            or grant.revision != intv.obligation_revision
            or grant.owner_tag != intv.owner_tag
            or os.path.realpath(os.path.abspath(grant.workspace)) != intv.workspace
            or grant.session_id != intv.session_id
            or grant.generation != intv.generation
            or not isinstance(grant.is_principal, bool)
            or grant.is_principal != intv.is_principal
        ):
            raise SecurityBindingError("Current authority tuple mismatch on receipt ingress")

        ok, reason = _validate_authority_protection_and_dependencies(
            grant=grant,
            completed_obligations=None,
            expected_dependencies=None,
            dependency_verifier=self.dependency_verifier,
            now_utc=now,
        )
        if not ok:
            raise SecurityBindingError(f"Current authority failed validation on receipt ingress: {reason}")

        # 2. Binding revalidation
        try:
            curr_b = self.binding_provider.get_current_binding(
                workspace=intv.workspace,
                session_id=intv.session_id,
                now_utc=now,
            )
        except Exception as exc:
            raise SecurityBindingError(f"Binding provider error: {exc}") from exc

        if curr_b is None:
            raise SecurityBindingError("Current session binding missing on receipt ingress")
        if not _is_valid_int(curr_b.generation):
            raise SecurityBindingError("Current session binding has non-integer generation")
        if not curr_b.workspace or not curr_b.workspace.strip():
            raise SecurityBindingError("Current session binding has empty workspace")
        if curr_b.active is not True or curr_b.worker_alive is not True or curr_b.phase == "exited":
            raise SecurityBindingError(
                f"Current session binding inactive or exited on receipt ingress (active={curr_b.active}, alive={curr_b.worker_alive}, phase={curr_b.phase})"
            )
        if (
            curr_b.session_id != intv.session_id
            or curr_b.generation != intv.generation
            or curr_b.tag != intv.owner_tag
            or os.path.realpath(os.path.abspath(curr_b.workspace)) != intv.workspace
        ):
            raise SecurityBindingError("Current session binding tuple mismatch on receipt ingress")

    def _prepare_and_reserve_stage(
        self,
        intv: InterventionRecord,
        stage: str,
        prior_state: str,
        target_state: str,
        digest: str,
        receipt_dt: datetime.datetime,
        preceding_dt: Optional[datetime.datetime],
        preceding_desc: str,
        now: datetime.datetime,
        now_str: str,
        check_freshness: bool = False,
    ) -> bool:
        """Validate stage, binding, and ordered timestamp BEFORE digest reservation.

        Returns True if this is an idempotent replay of an already-recorded stage (nonmutating return).
        Returns False if caller must proceed to execute ledger stage transition.
        Prevents invalid out-of-order receipts from poisoning legitimate later stages.
        Supports nonmutating exact replay through later valid stages; rejects changed content.
        Qualifies SQLite storage failures, file-backed crash windows, and timeout checks.
        """
        # 1. Mandatory current authority and session binding revalidation on all ingress and replay
        self._revalidate_ingress_authority_and_binding(intv, now)

        # 2. Ordered timestamp validation
        if receipt_dt > now:
            raise SecurityBindingError(
                f"Contradictory future timestamp in {stage} receipt: {receipt_dt.isoformat()}"
            )
        if check_freshness:
            age_s = (now - receipt_dt).total_seconds()
            if age_s > self.policy.freshness_threshold_seconds:
                raise SecurityBindingError(
                    f"{stage} receipt timestamp is stale ({age_s:.1f}s > {self.policy.freshness_threshold_seconds}s)"
                )
        if preceding_dt is not None and receipt_dt < preceding_dt:
            raise SecurityBindingError(
                f"{stage} timestamp ({receipt_dt.isoformat()}) earlier than {preceding_desc} ({preceding_dt.isoformat()})"
            )

        # 3. Check existing reservation in adapter-owned durable table
        stage_key = f"{intv.id}:{stage}"
        try:
            cur = self.ledger._conn.cursor()
            cur.execute(
                "SELECT digest, recorded_at FROM adapter_receipt_digests WHERE stage_key = ?",
                (stage_key,)
            )
            row = cur.fetchone()
        except sqlite3.Error as exc:
            raise AdapterError(f"SQLite storage failure querying receipt digests: {exc}") from exc

        if row is not None:
            existing_digest, recorded_at = row[0], row[1]
            if existing_digest != digest:
                raise SecurityBindingError(
                    f"Contradictory replay for {stage} on intervention {intv.id}: "
                    f"digest mismatch ({existing_digest} != {digest})"
                )
            # Exact recorded replay!
            target_idx = _stage_index(target_state)
            current_idx = _stage_index(intv.state)

            if intv.state == target_state or (target_idx != -1 and current_idx >= target_idx):
                # Replay of already completed stage, or replay through later valid stages (nonmutating)
                return True
            elif intv.state == prior_state:
                # Crash before ledger stage transition: digest reserved, proceed to transition ledger
                return False
            else:
                # Ambiguous state: fail closed and document operator reconciliation
                raise SecurityBindingError(
                    f"Ambiguous state for replay of {stage} on intervention {intv.id} "
                    f"(current state: '{intv.state}', expected '{target_state}' or '{prior_state}'); "
                    "operator reconciliation required"
                )
        else:
            # No prior reservation exists -> NEW stage reservation/transition!
            # Check timeout_at before NEW stage reservation/transition
            if intv.timeout_at:
                try:
                    timeout_dt = parse_strict_iso_utc(intv.timeout_at)
                    if now >= timeout_dt:
                        raise SecurityBindingError(
                            f"Intervention {intv.id} has timed out at {intv.timeout_at} (now {now.isoformat()}); "
                            f"new stage {stage} transition rejected"
                        )
                except SecurityBindingError:
                    raise
                except Exception:
                    raise SecurityBindingError(
                        f"Intervention {intv.id} has malformed timeout_at '{intv.timeout_at}'; new stage transition rejected"
                    )

            if intv.state in TERMINAL_STATES or intv.state == STATE_ESCALATION_NEEDED:
                raise SecurityBindingError(
                    f"Intervention {intv.id} is in terminal or escalated state ({intv.state}); receipt ingress rejected"
                )

            if intv.state == target_state:
                # Ledger in target state without adapter reservation: ambiguous state
                raise SecurityBindingError(
                    f"Ambiguous unrecorded ledger state for {stage} on intervention {intv.id}; "
                    "operator reconciliation required"
                )
            if intv.state != prior_state:
                raise SecurityBindingError(
                    f"Cannot record {stage} from state '{intv.state}'; expected '{prior_state}'"
                )

            # Reserve digest before ledger transition
            try:
                with self.ledger._conn:
                    self.ledger._conn.execute("""
                        INSERT INTO adapter_receipt_digests (stage_key, intervention_id, stage, digest, recorded_at)
                        VALUES (?, ?, ?, ?, ?)
                    """, (stage_key, intv.id, stage, digest, now_str))
            except sqlite3.Error as exc:
                raise AdapterError(
                    f"SQLite storage failure during digest reservation for {stage}: {exc}"
                ) from exc

            return False

    def record_mailbox_read_ack(
        self,
        message_id: str,
        ack: MailboxReadAck,
        expected_obligation_id: str,
        expected_revision: int,
        expected_owner_tag: str,
        expected_workspace: str,
    ) -> InterventionRecord:
        """Record mailbox read ACK with mandatory authority/binding revalidation and replay digest check."""
        now = self._now()
        now_str = now_iso(now)

        if self.authority_resolver is None or self.binding_provider is None:
            raise SecurityBindingError(
                "authority_resolver and binding_provider are mandatory on receipt ingress; missing context forbidden"
            )

        if not message_id or not ack:
            raise SecurityBindingError("message_id and ack are required")

        if ack.message_id != message_id:
            raise SecurityBindingError(
                f"ack.message_id mismatch: '{ack.message_id}' != '{message_id}'"
            )

        if not _is_valid_int(ack.reader_generation):
            raise SecurityBindingError(f"ack.reader_generation must be an integer (bool forbidden); got {ack.reader_generation!r}")
        if not _is_valid_int(expected_revision):
            raise SecurityBindingError(f"expected_revision must be an integer (bool forbidden); got {expected_revision!r}")
        if not expected_workspace or not expected_workspace.strip():
            raise SecurityBindingError("expected_workspace cannot be empty")

        # Lookup intervention and outbox from ledger
        cur = self.ledger._conn.cursor()
        cur.execute("SELECT intervention_id, dispatched_at FROM outbox WHERE message_id = ?", (message_id,))
        row = cur.fetchone()
        if not row:
            raise SecurityBindingError(f"No outbox message found for message_id '{message_id}'")

        intervention_id, dispatched_at_str = row[0], row[1]
        intv = self.ledger.get_intervention(intervention_id)
        if not intv:
            raise SecurityBindingError(f"Intervention '{intervention_id}' not found")

        # Validate caller expected parameters against intv
        canon_expected_ws = os.path.realpath(os.path.abspath(expected_workspace))
        if intv.obligation_id != expected_obligation_id:
            raise SecurityBindingError(f"Obligation ID mismatch ({intv.obligation_id} != {expected_obligation_id})")
        if intv.obligation_revision != expected_revision:
            raise SecurityBindingError(f"Revision mismatch ({intv.obligation_revision} != {expected_revision})")
        if intv.owner_tag != expected_owner_tag:
            raise SecurityBindingError(f"Owner tag mismatch ({intv.owner_tag} != {expected_owner_tag})")
        if intv.workspace != canon_expected_ws:
            raise SecurityBindingError(f"Workspace mismatch ({intv.workspace} != {canon_expected_ws})")

        # Validate ack binding against intv
        if ack.reader_session_id != intv.session_id or ack.reader_generation != intv.generation:
            raise SecurityBindingError(
                f"Reader session/generation mismatch: expected ({intv.session_id}, {intv.generation}), "
                f"got ({ack.reader_session_id}, {ack.reader_generation})"
            )

        # Parse read_at strictly
        try:
            read_dt = parse_strict_iso_utc(ack.read_at)
        except Exception as exc:
            raise SecurityBindingError(f"Malformed or timezone-naive read_at: {exc}") from exc

        disp_dt = None
        if dispatched_at_str:
            try:
                disp_dt = parse_strict_iso_utc(dispatched_at_str)
            except Exception:
                disp_dt = None

        # Compute canonical digest
        ack_digest = self._compute_digest({
            "generation": ack.reader_generation,
            "message_id": ack.message_id,
            "read_at": ack.read_at,
            "reader_session_id": ack.reader_session_id,
        })

        is_completed_replay = self._prepare_and_reserve_stage(
            intv=intv,
            stage="mailbox_read_ack",
            prior_state=STATE_DISPATCHED_SENT,
            target_state=STATE_MAILBOX_READ_ACK,
            digest=ack_digest,
            receipt_dt=read_dt,
            preceding_dt=disp_dt,
            preceding_desc="dispatch",
            now=now,
            now_str=now_str,
        )

        if is_completed_replay and intv.state != STATE_MAILBOX_READ_ACK:
            # Replay through later valid stage: return intv nonmutating
            return intv

        return self.ledger.record_mailbox_read_ack(message_id, ack)

    def record_custody_acceptance(
        self,
        intervention_id: str,
        custody: CustodyAcceptance,
    ) -> InterventionRecord:
        """Record custody acceptance, requiring all tuple fields including workspace and revision."""
        now = self._now()
        now_str = now_iso(now)

        if self.authority_resolver is None or self.binding_provider is None:
            raise SecurityBindingError(
                "authority_resolver and binding_provider are mandatory on receipt ingress; missing context forbidden"
            )

        if not intervention_id or not custody:
            raise SecurityBindingError("intervention_id and custody are required")

        if custody.workspace is None or not str(custody.workspace).strip():
            raise SecurityBindingError("custody.workspace is mandatory on adapter ingress and cannot be empty")
        if not _is_valid_int(custody.obligation_revision):
            raise SecurityBindingError(f"custody.obligation_revision must be an integer (bool forbidden); got {custody.obligation_revision!r}")
        if not _is_valid_int(custody.generation):
            raise SecurityBindingError(f"custody.generation must be an integer (bool forbidden); got {custody.generation!r}")

        intv = self.ledger.get_intervention(intervention_id)
        if not intv:
            raise SecurityBindingError(f"Intervention '{intervention_id}' not found")

        # Full tuple check
        canon_ws = os.path.realpath(os.path.abspath(custody.workspace))
        if intv.workspace != canon_ws:
            raise SecurityBindingError(f"Custody workspace mismatch: {intv.workspace} != {canon_ws}")
        if intv.obligation_revision != custody.obligation_revision:
            raise SecurityBindingError(f"Custody revision mismatch: {intv.obligation_revision} != {custody.obligation_revision}")
        if intv.obligation_id != custody.obligation_id:
            raise SecurityBindingError(f"Custody obligation_id mismatch: {intv.obligation_id} != {custody.obligation_id}")
        if intv.owner_tag != custody.owner_tag:
            raise SecurityBindingError(f"Custody owner_tag mismatch: {intv.owner_tag} != {custody.owner_tag}")
        if intv.session_id != custody.session_id:
            raise SecurityBindingError(f"Custody session_id mismatch: {intv.session_id} != {custody.session_id}")
        if intv.generation != custody.generation:
            raise SecurityBindingError(f"Custody generation mismatch: {intv.generation} != {custody.generation}")

        # Parse accepted_at strictly
        try:
            acc_dt = parse_strict_iso_utc(custody.accepted_at)
        except Exception as exc:
            raise SecurityBindingError(f"Malformed or timezone-naive accepted_at: {exc}") from exc

        # Preceding timestamp: mailbox_read_ack
        cur = self.ledger._conn.cursor()
        cur.execute("""
            SELECT recorded_at FROM audit_events
            WHERE intervention_id = ? AND event_type = 'mailbox_read_ack'
            ORDER BY id DESC LIMIT 1
        """, (intervention_id,))
        read_row = cur.fetchone()
        read_dt = None
        if read_row and read_row[0]:
            try:
                read_dt = parse_strict_iso_utc(read_row[0])
            except Exception:
                read_dt = None

        cust_digest = self._compute_digest({
            "accepted_at": custody.accepted_at,
            "generation": custody.generation,
            "obligation_id": custody.obligation_id,
            "obligation_revision": custody.obligation_revision,
            "owner_tag": custody.owner_tag,
            "session_id": custody.session_id,
            "workspace": canon_ws,
        })

        is_completed_replay = self._prepare_and_reserve_stage(
            intv=intv,
            stage="custody_accepted",
            prior_state=STATE_MAILBOX_READ_ACK,
            target_state=STATE_CUSTODY_ACCEPTED,
            digest=cust_digest,
            receipt_dt=acc_dt,
            preceding_dt=read_dt,
            preceding_desc="read ACK",
            now=now,
            now_str=now_str,
        )

        if is_completed_replay and intv.state != STATE_CUSTODY_ACCEPTED:
            # Replay through later valid stage: return intv nonmutating
            return intv

        return self.ledger.record_custody_acceptance(intervention_id, custody)

    def record_first_action(
        self,
        intervention_id: str,
        action: ActionReceipt,
    ) -> InterventionRecord:
        """Record genuine first useful action receipt, requiring all tuple fields."""
        now = self._now()
        now_str = now_iso(now)

        if self.authority_resolver is None or self.binding_provider is None:
            raise SecurityBindingError(
                "authority_resolver and binding_provider are mandatory on receipt ingress; missing context forbidden"
            )

        if not intervention_id or not action:
            raise SecurityBindingError("intervention_id and action are required")

        if not action.receipt_id or not isinstance(action.receipt_id, str) or not action.receipt_id.strip():
            raise SecurityBindingError("action.receipt_id is mandatory on adapter ingress and cannot be empty")
        if action.obligation_id is None:
            raise SecurityBindingError("action.obligation_id is mandatory on adapter ingress")
        if not _is_valid_int(action.obligation_revision):
            raise SecurityBindingError(f"action.obligation_revision must be an integer (bool forbidden); got {action.obligation_revision!r}")
        if action.owner_tag is None:
            raise SecurityBindingError("action.owner_tag is mandatory on adapter ingress")
        if action.workspace is None or not str(action.workspace).strip():
            raise SecurityBindingError("action.workspace is mandatory on adapter ingress and cannot be empty")
        if not _is_valid_int(action.generation):
            raise SecurityBindingError(f"action.generation must be an integer (bool forbidden); got {action.generation!r}")

        intv = self.ledger.get_intervention(intervention_id)
        if not intv:
            raise SecurityBindingError(f"Intervention '{intervention_id}' not found")

        # Full tuple check
        canon_ws = os.path.realpath(os.path.abspath(action.workspace))
        if intv.workspace != canon_ws:
            raise SecurityBindingError(f"Action workspace mismatch: {intv.workspace} != {canon_ws}")
        if intv.obligation_id != action.obligation_id:
            raise SecurityBindingError(f"Action obligation mismatch: {intv.obligation_id} != {action.obligation_id}")
        if intv.obligation_revision != action.obligation_revision:
            raise SecurityBindingError(f"Action revision mismatch: {intv.obligation_revision} != {action.obligation_revision}")
        if intv.owner_tag != action.owner_tag:
            raise SecurityBindingError(f"Action owner mismatch: {intv.owner_tag} != {action.owner_tag}")
        if intv.session_id != action.session_id:
            raise SecurityBindingError(f"Action session mismatch: {intv.session_id} != {action.session_id}")
        if intv.generation != action.generation:
            raise SecurityBindingError(f"Action generation mismatch: {intv.generation} != {action.generation}")

        # Useful action type check (heartbeat, spinner, and ACK are strictly forbidden)
        if action.action_type not in VALID_USEFUL_ACTION_TYPES:
            raise SecurityBindingError(
                f"Action type '{action.action_type}' is not a valid useful action (heartbeat, spinner, and ACK actions are forbidden)"
            )

        # Parse timestamp strictly
        try:
            act_dt = parse_strict_iso_utc(action.timestamp)
        except Exception as exc:
            raise SecurityBindingError(f"Malformed or timezone-naive action timestamp: {exc}") from exc

        # Preceding timestamp: custody_accepted
        cur = self.ledger._conn.cursor()
        cur.execute("""
            SELECT recorded_at FROM audit_events
            WHERE intervention_id = ? AND event_type = 'custody_accepted'
            ORDER BY id DESC LIMIT 1
        """, (intervention_id,))
        cust_row = cur.fetchone()
        cust_dt = None
        if cust_row and cust_row[0]:
            try:
                cust_dt = parse_strict_iso_utc(cust_row[0])
            except Exception:
                cust_dt = None

        act_digest = self._compute_digest({
            "action_type": action.action_type,
            "details": action.details or {},
            "generation": action.generation,
            "obligation_id": action.obligation_id,
            "obligation_revision": action.obligation_revision,
            "owner_tag": action.owner_tag,
            "receipt_id": action.receipt_id.strip(),
            "session_id": action.session_id,
            "timestamp": action.timestamp,
            "workspace": canon_ws,
        })

        is_completed_replay = self._prepare_and_reserve_stage(
            intv=intv,
            stage="first_action_observed",
            prior_state=STATE_CUSTODY_ACCEPTED,
            target_state=STATE_FIRST_ACTION_OBSERVED,
            digest=act_digest,
            receipt_dt=act_dt,
            preceding_dt=cust_dt,
            preceding_desc="custody acceptance",
            now=now,
            now_str=now_str,
            check_freshness=True,
        )

        if is_completed_replay and intv.state != STATE_FIRST_ACTION_OBSERVED:
            # Replay through later valid stage: return intv nonmutating
            return intv

        return self.ledger.record_first_action(intervention_id, action)

    def record_completion_outcome(
        self,
        intervention_id: str,
        outcome: CompletionOutcome,
    ) -> InterventionRecord:
        """Record completed outcome, requiring all tuple fields."""
        now = self._now()
        now_str = now_iso(now)

        if self.authority_resolver is None or self.binding_provider is None:
            raise SecurityBindingError(
                "authority_resolver and binding_provider are mandatory on receipt ingress; missing context forbidden"
            )

        if not intervention_id or not outcome:
            raise SecurityBindingError("intervention_id and outcome are required")

        if outcome.workspace is None or not str(outcome.workspace).strip():
            raise SecurityBindingError("outcome.workspace is mandatory on adapter ingress and cannot be empty")
        if not _is_valid_int(outcome.obligation_revision):
            raise SecurityBindingError(f"outcome.obligation_revision must be an integer (bool forbidden); got {outcome.obligation_revision!r}")
        if not _is_valid_int(outcome.generation):
            raise SecurityBindingError(f"outcome.generation must be an integer (bool forbidden); got {outcome.generation!r}")

        intv = self.ledger.get_intervention(intervention_id)
        if not intv:
            raise SecurityBindingError(f"Intervention '{intervention_id}' not found")

        # Full tuple check
        canon_ws = os.path.realpath(os.path.abspath(outcome.workspace))
        if intv.workspace != canon_ws:
            raise SecurityBindingError(f"Completion outcome workspace mismatch: {intv.workspace} != {canon_ws}")
        if intv.obligation_id != outcome.obligation_id:
            raise SecurityBindingError(f"Completion outcome obligation mismatch: {intv.obligation_id} != {outcome.obligation_id}")
        if intv.obligation_revision != outcome.obligation_revision:
            raise SecurityBindingError(f"Completion outcome revision mismatch: {intv.obligation_revision} != {outcome.obligation_revision}")
        if intv.owner_tag != outcome.owner_tag:
            raise SecurityBindingError(f"Completion outcome owner mismatch: {intv.owner_tag} != {outcome.owner_tag}")
        if intv.session_id != outcome.session_id:
            raise SecurityBindingError(f"Completion outcome session mismatch: {intv.session_id} != {outcome.session_id}")
        if intv.generation != outcome.generation:
            raise SecurityBindingError(f"Completion outcome generation mismatch: {intv.generation} != {outcome.generation}")

        # Parse completed_at strictly
        try:
            comp_dt = parse_strict_iso_utc(outcome.completed_at)
        except Exception as exc:
            raise SecurityBindingError(f"Malformed or timezone-naive completed_at: {exc}") from exc

        # Preceding timestamp: first_action_observed
        cur = self.ledger._conn.cursor()
        cur.execute("""
            SELECT recorded_at FROM audit_events
            WHERE intervention_id = ? AND event_type = 'first_action_observed'
            ORDER BY id DESC LIMIT 1
        """, (intervention_id,))
        act_row = cur.fetchone()
        act_dt = None
        if act_row and act_row[0]:
            try:
                act_dt = parse_strict_iso_utc(act_row[0])
            except Exception:
                act_dt = None

        comp_digest = self._compute_digest({
            "completed_at": outcome.completed_at,
            "generation": outcome.generation,
            "obligation_id": outcome.obligation_id,
            "obligation_revision": outcome.obligation_revision,
            "owner_tag": outcome.owner_tag,
            "result": outcome.result,
            "session_id": outcome.session_id,
            "workspace": canon_ws,
        })

        is_completed_replay = self._prepare_and_reserve_stage(
            intv=intv,
            stage="completion_outcome",
            prior_state=STATE_FIRST_ACTION_OBSERVED,
            target_state=STATE_COMPLETED,
            digest=comp_digest,
            receipt_dt=comp_dt,
            preceding_dt=act_dt,
            preceding_desc="first action",
            now=now,
            now_str=now_str,
        )

        return self.ledger.record_completion_outcome(intervention_id, outcome)
