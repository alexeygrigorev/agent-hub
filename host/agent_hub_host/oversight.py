"""Oversight Policy and Durable Intervention Ledger for Agent Hub.

docs/SPEC.md §9, §11, §12.
Provides:
- Source-only stdlib oversight policy and durable SQLite intervention ledger.
- Strict distinction between liveness (PID, heartbeat, spinner, PTY bytes)
  and genuine productivity (useful action/tool receipts, unexpired checkpoint deadlines).
- Multi-state classification of obligations:
  * UNKNOWN_OR_STALE
  * PROTECTED (frozen, blocked, quota-held, manual-approval)
  * CURRENT_USEFUL_WORK
  * IDLE_WITH_READY_WORK
  * MISSED_CHECKPOINT
  * COMPLETED_AWAITING_OWNER
  * DONE
- Deterministic deduplication per exact:
  (obligation_id, obligation_revision, owner_tag, workspace, session_id, generation, intervention_type).
- Transactional bounded SQLite ledger + durable outbox (decision + owed notification saved together).
- Multi-step intervention lifecycle:
  DECISION -> DISPATCH_ATTEMPT -> (SENT / UNCERTAIN / FAILED)
  -> MAILBOX_READ_ACK -> CUSTODY_ACCEPTED -> FIRST_ACTION_OBSERVED -> COMPLETED.
- Escalation on timeout: marks ESCALATION_NEEDED without automatic retry or respawn.
- Session reassignment & generation advance fences old ACK/action/outcomes.
- Strict rejection of forged or stale session/generation/custody attempts.
- Principal obligations routed bus-only.
- Sanitized outgoing messages (no raw screens, secrets, or tokens; length-capped).
- Injectable transport, guard authorization, and trusted clock.
- Bounded capacity with honest rejection (CapacityExceededError); never deletes active obligations.
- No background timers, threads, or pollers (synchronous stdlib library).
"""

from __future__ import annotations

import dataclasses
import datetime
import hashlib
import json
import os
import re
import sqlite3
import uuid
from typing import Any, Callable, Dict, List, Optional, Protocol, Sequence, Set, Tuple

# --- Intervention Types ---
INTERVENTION_NUDGE_IDLE = "nudge_idle"
INTERVENTION_CHECKPOINT_MISSED = "checkpoint_missed"
INTERVENTION_RESULT_ACK_PENDING = "result_ack_pending"

# --- Classification States ---
CLASSIFICATION_UNKNOWN_OR_STALE = "unknown_or_stale"
CLASSIFICATION_PROTECTED = "protected"
CLASSIFICATION_CURRENT_USEFUL_WORK = "current_useful_work"
CLASSIFICATION_IDLE_WITH_READY_WORK = "idle_with_ready_work"
CLASSIFICATION_MISSED_CHECKPOINT = "missed_checkpoint"
CLASSIFICATION_COMPLETED_AWAITING_OWNER = "completed_awaiting_owner"
CLASSIFICATION_DONE = "done"

# --- Intervention Lifecycle States ---
STATE_CREATED = "created"
STATE_OUTBOX_PENDING = "outbox_pending"
STATE_DISPATCHED_UNCERTAIN = "dispatched_uncertain"
STATE_DISPATCHED_SENT = "dispatched_sent"
STATE_MAILBOX_READ_ACK = "mailbox_read_ack"
STATE_CUSTODY_ACCEPTED = "custody_accepted"
STATE_FIRST_ACTION_OBSERVED = "first_action_observed"
STATE_COMPLETED = "completed"
STATE_ESCALATION_NEEDED = "escalation_needed"
STATE_FENCED = "fenced"
STATE_CANCELLED = "cancelled"
STATE_FAILED = "failed"

TERMINAL_STATES = frozenset([
    STATE_COMPLETED,
    STATE_CANCELLED,
    STATE_FENCED,
])

# --- Outbox Statuses ---
OUTBOX_PENDING = "pending"
OUTBOX_SENDING = "sending"
OUTBOX_SENT = "sent"
OUTBOX_UNCERTAIN = "uncertain"
OUTBOX_FAILED = "failed"
OUTBOX_CANCELLED = "cancelled"

# --- Routes ---
ROUTE_BUS = "bus"
ROUTE_NATIVE_MAILBOX = "native_mailbox"

# --- System Bounds & Constraints ---
MAX_ACTIVE_INTERVENTIONS = 500
MAX_OUTBOX_PENDING = 1000
MAX_RETAINED_HISTORY = 5000
MAX_AUDIT_EVENTS = 20000
MAX_EVIDENCE_LENGTH = 1024
MAX_REASON_LENGTH = 512
MAX_PAYLOAD_BYTES = 32 * 1024
FRESHNESS_THRESHOLD_SECONDS = 300.0
DEFAULT_TIMEOUT_SECONDS = 600.0


# --- Exceptions ---

class OversightError(Exception):
    """Base exception for oversight subsystem."""
    pass


class CapacityExceededError(OversightError):
    """Raised when active interventions or pending outbox exceed capacity limits."""
    pass


class SecurityBindingError(OversightError):
    """Raised when session, generation, or custody identity is forged or stale."""
    pass


class TransportError(OversightError):
    """Base exception for transport operations."""
    pass


class TransportSendFailedBeforeDelivery(TransportError):
    """Raised when transport fails before attempting message transmission."""
    pass


class TransportUncertainError(TransportError):
    """Raised when transport delivery state is uncertain (e.g. dropped socket or timeout)."""
    pass


class GuardAuthorizationError(OversightError):
    """Raised when guard refuses authorization for dispatch."""
    pass


# --- Sanitization & String Scrubbing ---

_SECRET_PATTERNS = [
    re.compile(r"\b(token[:=\s]+)[A-Za-z0-9_\-\.]{8,}", re.IGNORECASE),
    re.compile(r"\b(bearer\s+)[A-Za-z0-9_\-\.]+", re.IGNORECASE),
    re.compile(r"\b(secret_[a-zA-Z0-9_\-]+)\b", re.IGNORECASE),
    re.compile(r"\b(password[:=\s]+)\S+", re.IGNORECASE),
    re.compile(r"\b(quota[:=\s]+)\d+", re.IGNORECASE),
    re.compile(r"\b(key[:=\s]+)[A-Za-z0-9_\-\.]{16,}", re.IGNORECASE),
    re.compile(r"\b(raw user prompt[:=\s]+).*$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"\b(transcript[:=\s]+).*$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"\b(raw_?screen[:=\s]+).*$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"\b(private\s+marker[:=\s]*)\S+", re.IGNORECASE),
    re.compile(r"\b(marker[:=\s]+)\S+", re.IGNORECASE),
    re.compile(r"\b[A-Za-z0-9_]*marker[A-Za-z0-9_]*\b", re.IGNORECASE),
    re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])"),  # ANSI escapes
]

FORBIDDEN_KEY_PATTERNS = [
    re.compile(r"(raw_?screen|transcript|user_?prompt|token|password|credential|marker)", re.IGNORECASE)
]

ALLOWED_PAYLOAD_KEYS = frozenset([
    "type",
    "obligation_id",
    "obligation_revision",
    "checkpoint_due",
    "reason",
    "issued_at",
    "workspace",
    "existing_id",
    "details",
    "status",
])


def sanitize_text(text: Optional[str], max_len: int = MAX_EVIDENCE_LENGTH) -> str:
    """Scrub sensitive patterns, tokens, and ANSI escapes from text, truncating to max_len."""
    if not text:
        return ""
    s = str(text)
    for pat in _SECRET_PATTERNS:
        s = pat.sub("[REDACTED]", s)
    s = s.strip()
    if len(s) > max_len:
        s = s[:max_len]
    return s


def sanitize_payload(payload: Any, depth: int = 0) -> Any:
    """Sanitize notification payload against strict known-safe schema and strip secrets/markers."""
    if depth > 5:
        return "[REDACTED_MAX_DEPTH]"
    if isinstance(payload, dict):
        cleaned: Dict[str, Any] = {}
        for k, v in payload.items():
            k_str = str(k)
            # Drop forbidden keys
            if any(pat.search(k_str) for pat in FORBIDDEN_KEY_PATTERNS):
                continue
            # Top-level keys must match known safe schema
            if depth == 0 and k_str not in ALLOWED_PAYLOAD_KEYS:
                continue
            cleaned[k_str] = sanitize_payload(v, depth + 1)
        return cleaned
    elif isinstance(payload, (list, tuple)):
        return [sanitize_payload(x, depth + 1) for x in payload]
    elif isinstance(payload, str):
        if len(payload.encode("utf-8")) > MAX_PAYLOAD_BYTES:
            raise CapacityExceededError(
                f"Notification payload string field exceeds maximum size ({len(payload)} > {MAX_PAYLOAD_BYTES})"
            )
        return sanitize_text(payload, max_len=MAX_PAYLOAD_BYTES)
    elif isinstance(payload, (int, float, bool)) or payload is None:
        return payload
    else:
        # Non-primitive dropped/rejected
        return None


def parse_iso_utc(ts: str) -> datetime.datetime:
    """Parse an ISO8601 string into a UTC-aware datetime."""
    clean = ts.strip()
    if clean.endswith("Z"):
        clean = clean[:-1] + "+00:00"
    dt = datetime.datetime.fromisoformat(clean)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(datetime.timezone.utc)


def now_iso(dt: Optional[datetime.datetime] = None) -> str:
    """Format datetime as UTC ISO8601 string."""
    if dt is None:
        dt = datetime.datetime.now(datetime.timezone.utc)
    elif dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(datetime.timezone.utc).isoformat()


# --- Core Dataclasses ---

@dataclasses.dataclass(frozen=True)
class TaskObligation:
    id: str
    revision: int = 1
    owner_tag: str = ""
    workspace: str = ""
    status: str = "ready"  # ready, in_progress, blocked, frozen, completed, done, accepted
    dependencies: Tuple[str, ...] = ()
    checkpoint_due: Optional[str] = None  # ISO8601 UTC
    completed_result: Optional[Dict[str, Any]] = None
    is_principal: bool = False
    protected_reason: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    has_owner_ack: bool = False


@dataclasses.dataclass(frozen=True)
class NativeSessionBinding:
    session_id: str
    tag: str
    generation: int
    workspace: str
    worker_alive: bool = True
    phase: str = "running"
    reported_state: str = "idle"  # working, idle, waiting, etc.
    active: bool = True


@dataclasses.dataclass(frozen=True)
class ObservationEvidence:
    sample_id: str
    observed_at: str  # ISO8601 UTC
    assessment: str  # progress_observed, busy_unconfirmed, possible_stall, idle, etc.
    reason_codes: Tuple[str, ...] = ()
    empty_composer: bool = False
    active_tool_calls: Tuple[str, ...] = ()
    recent_output_bytes: int = 0
    screen_digest: Optional[str] = None
    session_id: Optional[str] = None
    generation: Optional[int] = None


VALID_USEFUL_ACTION_TYPES = frozenset([
    "useful_progress",
    "tool_execution",
    "task_result",
    "checkpoint_posted",
])


@dataclasses.dataclass(frozen=True)
class ActionReceipt:
    receipt_id: str
    session_id: str
    generation: int
    action_type: str  # e.g., "tool_execution", "checkpoint_posted", "task_result"
    timestamp: str
    details: Dict[str, Any] = dataclasses.field(default_factory=dict)
    obligation_id: Optional[str] = None
    obligation_revision: Optional[int] = None
    owner_tag: Optional[str] = None
    workspace: Optional[str] = None


@dataclasses.dataclass(frozen=True)
class MailboxReadAck:
    message_id: str
    read_at: str
    reader_session_id: str
    reader_generation: int


@dataclasses.dataclass(frozen=True)
class CustodyAcceptance:
    obligation_id: str
    owner_tag: str
    session_id: str
    generation: int
    accepted_at: str
    workspace: Optional[str] = None
    obligation_revision: Optional[int] = None


@dataclasses.dataclass(frozen=True)
class CompletionOutcome:
    obligation_id: str
    owner_tag: str
    session_id: str
    generation: int
    completed_at: str
    result: Dict[str, Any] = dataclasses.field(default_factory=dict)
    workspace: Optional[str] = None
    obligation_revision: Optional[int] = None


@dataclasses.dataclass(frozen=True)
class ClassificationResult:
    state: str
    reason: str
    details: Dict[str, Any] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass(frozen=True)
class OversightDecision:
    obligation_id: str
    obligation_revision: int
    owner_tag: str
    workspace: str
    session_id: str
    generation: int
    intervention_type: str
    route: str  # ROUTE_BUS or ROUTE_NATIVE_MAILBOX
    reason: str
    evidence_summary: str
    notification_payload: Dict[str, Any]
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    is_principal: bool = False


@dataclasses.dataclass(frozen=True)
class InterventionRecord:
    id: str
    dedup_key: str
    obligation_id: str
    obligation_revision: int
    owner_tag: str
    workspace: str
    session_id: str
    generation: int
    intervention_type: str
    state: str
    created_at: str
    updated_at: str
    timeout_at: Optional[str]
    reason: str
    evidence_summary: str
    outbox_message_id: Optional[str]
    is_principal: bool = False


@dataclasses.dataclass(frozen=True)
class OutboxMessage:
    message_id: str
    intervention_id: str
    dedup_key: str
    route: str
    recipient_tag: str
    payload_json: str
    status: str
    attempt_count: int
    created_at: str
    dispatched_at: Optional[str]
    uncertain_at: Optional[str]
    is_principal: bool = False


# --- Protocols / Interfaces ---

class TransportProtocol(Protocol):
    def send(
        self,
        route: str,
        recipient_tag: str,
        payload: Dict[str, Any],
        idempotency_key: str,
    ) -> Dict[str, Any]:
        ...


class GuardProtocol(Protocol):
    def authorize_dispatch(self, message: OutboxMessage) -> Tuple[bool, str]:
        ...


# --- Oversight Policy ---

class OversightPolicy:
    """Pure evaluation policy distinguishing liveness from productivity."""

    def __init__(self, freshness_threshold_seconds: float = FRESHNESS_THRESHOLD_SECONDS):
        self.freshness_threshold_seconds = freshness_threshold_seconds

    def classify(
        self,
        obligation: TaskObligation,
        binding: Optional[NativeSessionBinding],
        observation: Optional[ObservationEvidence],
        now_utc: datetime.datetime,
        completed_obligations: Optional[Set[str]] = None,
        recent_actions: Optional[Sequence[ActionReceipt]] = None,
    ) -> ClassificationResult:
        """Classify obligation state based on evidence and binding."""
        # 0. Trusted Task Obligation Authority
        if not obligation or not obligation.id or not obligation.workspace:
            return ClassificationResult(
                state=CLASSIFICATION_UNKNOWN_OR_STALE,
                reason="Missing trusted task obligation authority",
            )
        trusted_statuses = {
            "ready", "in_progress", "blocked", "frozen",
            "quota_held", "manual_approval", "completed", "done", "accepted"
        }
        if obligation.status not in trusted_statuses:
            return ClassificationResult(
                state=CLASSIFICATION_UNKNOWN_OR_STALE,
                reason=f"Untrusted obligation status: {obligation.status}",
            )

        # 1. Authoritative Binding Qualification
        if binding is None:
            return ClassificationResult(
                state=CLASSIFICATION_UNKNOWN_OR_STALE,
                reason="Missing native session binding",
            )

        # Workspace canonicalization / exact match
        b_ws = os.path.realpath(os.path.abspath(binding.workspace))
        o_ws = os.path.realpath(os.path.abspath(obligation.workspace))
        if b_ws != o_ws:
            return ClassificationResult(
                state=CLASSIFICATION_UNKNOWN_OR_STALE,
                reason=f"Ambiguous binding: workspace mismatch ({b_ws} != {o_ws})",
            )

        if not binding.worker_alive or binding.phase == "exited" or not binding.active:
            return ClassificationResult(
                state=CLASSIFICATION_UNKNOWN_OR_STALE,
                reason=f"Worker not active (worker_alive={binding.worker_alive}, phase={binding.phase})",
            )

        # 2. Observation Evidence Qualification
        if observation is None:
            return ClassificationResult(
                state=CLASSIFICATION_UNKNOWN_OR_STALE,
                reason="Missing observation evidence",
            )

        # Exact session and generation binding check: missing/None is UNKNOWN!
        if observation.session_id is None or observation.generation is None:
            return ClassificationResult(
                state=CLASSIFICATION_UNKNOWN_OR_STALE,
                reason="Observation missing explicit session_id or generation binding",
            )
        if observation.session_id != binding.session_id:
            return ClassificationResult(
                state=CLASSIFICATION_UNKNOWN_OR_STALE,
                reason=f"Observation session mismatch ({observation.session_id} != {binding.session_id})",
            )
        if observation.generation != binding.generation:
            return ClassificationResult(
                state=CLASSIFICATION_UNKNOWN_OR_STALE,
                reason=f"Observation generation mismatch ({observation.generation} != {binding.generation})",
            )

        try:
            obs_dt = parse_iso_utc(observation.observed_at)
        except Exception:
            return ClassificationResult(
                state=CLASSIFICATION_UNKNOWN_OR_STALE,
                reason="Malformed observation timestamp",
            )

        age_seconds = (now_utc - obs_dt).total_seconds()
        if age_seconds < 0:
            # Future timestamp contradiction
            return ClassificationResult(
                state=CLASSIFICATION_UNKNOWN_OR_STALE,
                reason=f"Contradictory future observation timestamp: {observation.observed_at}",
            )
        if age_seconds > self.freshness_threshold_seconds:
            return ClassificationResult(
                state=CLASSIFICATION_UNKNOWN_OR_STALE,
                reason=f"Observation is stale ({age_seconds:.1f}s > {self.freshness_threshold_seconds}s)",
            )

        # Check for contradictions in observation
        if observation.assessment == "unknown":
            return ClassificationResult(
                state=CLASSIFICATION_UNKNOWN_OR_STALE,
                reason="Observation assessment is unknown",
            )

        if observation.empty_composer and observation.assessment == "progress_observed":
            return ClassificationResult(
                state=CLASSIFICATION_UNKNOWN_OR_STALE,
                reason="Contradictory observation: empty composer claimed with progress_observed",
            )

        # 3. Protected States (frozen, blocked, quota-held, manual-approval, unsatisfied deps)
        if obligation.status in ("frozen", "blocked", "quota_held", "manual_approval"):
            return ClassificationResult(
                state=CLASSIFICATION_PROTECTED,
                reason=f"Obligation status is protected ({obligation.status})",
                details={"status": obligation.status},
            )

        if obligation.protected_reason and obligation.protected_reason.strip():
            return ClassificationResult(
                state=CLASSIFICATION_PROTECTED,
                reason=f"Obligation protected: {sanitize_text(obligation.protected_reason, 128)}",
                details={"protected_reason": obligation.protected_reason},
            )

        if obligation.dependencies:
            comp = completed_obligations or set()
            unsatisfied = [d for d in obligation.dependencies if d not in comp]
            if unsatisfied:
                return ClassificationResult(
                    state=CLASSIFICATION_PROTECTED,
                    reason=f"Blocked on unsatisfied dependencies: {unsatisfied}",
                    details={"unsatisfied_dependencies": unsatisfied},
                )

        # 4. Completed Awaiting Owner Acknowledgment
        if obligation.status == "completed" or obligation.completed_result is not None:
            if not obligation.has_owner_ack:
                return ClassificationResult(
                    state=CLASSIFICATION_COMPLETED_AWAITING_OWNER,
                    reason="Task completed; awaiting owner result receipt and acknowledgment",
                    details={"result_present": obligation.completed_result is not None},
                )
            return ClassificationResult(
                state=CLASSIFICATION_DONE,
                reason="Task completed and owner acknowledgment verified",
            )

        if obligation.status in ("done", "accepted"):
            return ClassificationResult(
                state=CLASSIFICATION_DONE,
                reason=f"Obligation is terminal ({obligation.status})",
            )

        # 5. Missed Checkpoint Check (Liveness alone does NOT satisfy a checkpoint)
        if obligation.checkpoint_due is not None:
            try:
                due_dt = parse_iso_utc(obligation.checkpoint_due)
            except Exception:
                return ClassificationResult(
                    state=CLASSIFICATION_UNKNOWN_OR_STALE,
                    reason="Malformed checkpoint_due timestamp",
                )

            if now_utc > due_dt:
                # Check if there is an unexpired recent progress receipt from the current session/generation
                has_progress_receipt = False
                if recent_actions:
                    for act in recent_actions:
                        if (
                            act.session_id == binding.session_id
                            and act.generation == binding.generation
                            and act.action_type in ("checkpoint_posted", "task_result", "useful_progress")
                        ):
                            # Must match obligation and revision if provided
                            if act.obligation_id is not None and act.obligation_id != obligation.id:
                                continue
                            if act.obligation_revision is not None and act.obligation_revision != obligation.revision:
                                continue
                            try:
                                act_dt = parse_iso_utc(act.timestamp)
                                # Must be on or after due date and strictly NOT in the future!
                                if due_dt <= act_dt <= now_utc:
                                    has_progress_receipt = True
                                    break
                            except Exception:
                                pass

                if not has_progress_receipt:
                    return ClassificationResult(
                        state=CLASSIFICATION_MISSED_CHECKPOINT,
                        reason=f"Promised checkpoint deadline expired at {obligation.checkpoint_due} without progress receipt",
                        details={"checkpoint_due": obligation.checkpoint_due, "now": now_iso(now_utc)},
                    )

        # 6. Current Useful Work vs Spinner/Liveness
        has_spinner_evidence = (
            any(rc in ("SPINNER_ANIMATION", "CLOCK_ONLY_CHANGE") for rc in observation.reason_codes)
            or (
                bool(observation.reason_codes)
                and set(observation.reason_codes).issubset({"SPINNER_ANIMATION", "CLOCK_ONLY_CHANGE", "SCREEN_STATIC"})
            )
        )
        is_spinner_only = (
            has_spinner_evidence
            and len(observation.active_tool_calls) == 0
            and observation.recent_output_bytes == 0
        )

        has_useful_action = False
        if recent_actions:
            for act in recent_actions:
                if (
                    act.session_id == binding.session_id
                    and act.generation == binding.generation
                    and act.action_type in VALID_USEFUL_ACTION_TYPES
                ):
                    # Must match obligation and revision if provided
                    if act.obligation_id is not None and act.obligation_id != obligation.id:
                        continue
                    if act.obligation_revision is not None and act.obligation_revision != obligation.revision:
                        continue
                    try:
                        act_dt = parse_iso_utc(act.timestamp)
                        age = (now_utc - act_dt).total_seconds()
                        # Strictly bounded: non-negative and not stale
                        if 0 <= age <= self.freshness_threshold_seconds:
                            has_useful_action = True
                            break
                    except Exception:
                        pass

        has_verified_tool_work = len(observation.active_tool_calls) > 0 and not is_spinner_only

        if has_useful_action or has_verified_tool_work:
            return ClassificationResult(
                state=CLASSIFICATION_CURRENT_USEFUL_WORK,
                reason="Current useful work confirmed with verified output or tool receipt",
                details={
                    "assessment": observation.assessment,
                    "tool_calls": len(observation.active_tool_calls),
                },
            )

        # Genuine idle contradiction / spinner fail-closed:
        # If spinner/animation evidence exists without useful actions, fail closed even if reported idle
        if has_spinner_evidence and not has_useful_action and not has_verified_tool_work:
            return ClassificationResult(
                state=CLASSIFICATION_UNKNOWN_OR_STALE,
                reason="Spinner/animation evidence without verified useful progress; fail closed even if reported idle",
            )

        if observation.assessment == "progress_observed":
            # Screen output bytes alone without active tool calls or verified receipts is not proof
            return ClassificationResult(
                state=CLASSIFICATION_UNKNOWN_OR_STALE,
                reason="Screen output bytes alone without active tool calls or verified receipts is not proof of useful work",
            )

        # 7. Idle with Ready Work
        # Requires genuine idle assessment AND verified empty composer without contradictions
        if (
            (observation.assessment == "idle" and observation.empty_composer)
            or (observation.empty_composer and binding.reported_state == "idle" and observation.assessment not in ("busy_unconfirmed", "possible_stall", "progress_observed"))
        ):
            if obligation.status in ("ready", "in_progress"):
                return ClassificationResult(
                    state=CLASSIFICATION_IDLE_WITH_READY_WORK,
                    reason="Native session idle with ready work and dependencies satisfied",
                    details={"empty_composer": observation.empty_composer},
                )

        # 8. Unconfirmed Busy / Possible Stall / Inconclusive Liveness
        if observation.assessment in ("busy_unconfirmed", "possible_stall") or is_spinner_only:
            # If checkpoint is not yet due, do not nudge or guess; fail closed
            return ClassificationResult(
                state=CLASSIFICATION_UNKNOWN_OR_STALE,
                reason="Unconfirmed activity or animation only without progress receipts; fail closed",
            )

        # 9. Fallback fail-closed
        return ClassificationResult(
            state=CLASSIFICATION_UNKNOWN_OR_STALE,
            reason=f"Inconclusive observation state: assessment={observation.assessment}",
        )

    def decide(
        self,
        obligation: TaskObligation,
        binding: Optional[NativeSessionBinding],
        observation: Optional[ObservationEvidence],
        now_utc: datetime.datetime,
        completed_obligations: Optional[Set[str]] = None,
        recent_actions: Optional[Sequence[ActionReceipt]] = None,
    ) -> Optional[OversightDecision]:
        """Evaluate evidence and return an intervention decision if warranted."""
        res = self.classify(
            obligation=obligation,
            binding=binding,
            observation=observation,
            now_utc=now_utc,
            completed_obligations=completed_obligations,
            recent_actions=recent_actions,
        )

        if binding is None:
            return None

        # Determine route: Principal is strictly bus-only
        is_principal = (
            obligation.is_principal
            or obligation.owner_tag.strip().lower() == "codex-principal"
            or obligation.owner_tag.strip().lower().endswith("principal")
            or "principal" in obligation.owner_tag.strip().lower()
        )
        route = ROUTE_BUS if is_principal else ROUTE_NATIVE_MAILBOX

        evidence_summary = sanitize_text(
            f"obs={observation.sample_id if observation else 'none'}; "
            f"assess={observation.assessment if observation else 'none'}; "
            f"reason={','.join(observation.reason_codes) if observation else 'none'}",
            max_len=MAX_EVIDENCE_LENGTH,
        )

        if res.state == CLASSIFICATION_IDLE_WITH_READY_WORK:
            payload = {
                "type": INTERVENTION_NUDGE_IDLE,
                "obligation_id": obligation.id,
                "obligation_revision": obligation.revision,
                "reason": sanitize_text(res.reason, MAX_REASON_LENGTH),
                "issued_at": now_iso(now_utc),
                "workspace": obligation.workspace,
            }
            return OversightDecision(
                obligation_id=obligation.id,
                obligation_revision=obligation.revision,
                owner_tag=obligation.owner_tag or binding.tag,
                workspace=obligation.workspace,
                session_id=binding.session_id,
                generation=binding.generation,
                intervention_type=INTERVENTION_NUDGE_IDLE,
                route=route,
                reason=sanitize_text(res.reason, MAX_REASON_LENGTH),
                evidence_summary=evidence_summary,
                notification_payload=payload,
                is_principal=is_principal,
            )

        elif res.state == CLASSIFICATION_MISSED_CHECKPOINT:
            payload = {
                "type": INTERVENTION_CHECKPOINT_MISSED,
                "obligation_id": obligation.id,
                "obligation_revision": obligation.revision,
                "checkpoint_due": obligation.checkpoint_due,
                "reason": sanitize_text(res.reason, MAX_REASON_LENGTH),
                "issued_at": now_iso(now_utc),
                "workspace": obligation.workspace,
            }
            return OversightDecision(
                obligation_id=obligation.id,
                obligation_revision=obligation.revision,
                owner_tag=obligation.owner_tag or binding.tag,
                workspace=obligation.workspace,
                session_id=binding.session_id,
                generation=binding.generation,
                intervention_type=INTERVENTION_CHECKPOINT_MISSED,
                route=route,
                reason=sanitize_text(res.reason, MAX_REASON_LENGTH),
                evidence_summary=evidence_summary,
                notification_payload=payload,
                is_principal=is_principal,
            )

        elif res.state == CLASSIFICATION_COMPLETED_AWAITING_OWNER:
            payload = {
                "type": INTERVENTION_RESULT_ACK_PENDING,
                "obligation_id": obligation.id,
                "obligation_revision": obligation.revision,
                "reason": sanitize_text(res.reason, MAX_REASON_LENGTH),
                "issued_at": now_iso(now_utc),
                "workspace": obligation.workspace,
            }
            return OversightDecision(
                obligation_id=obligation.id,
                obligation_revision=obligation.revision,
                owner_tag=obligation.owner_tag or binding.tag,
                workspace=obligation.workspace,
                session_id=binding.session_id,
                generation=binding.generation,
                intervention_type=INTERVENTION_RESULT_ACK_PENDING,
                route=route,
                reason=sanitize_text(res.reason, MAX_REASON_LENGTH),
                evidence_summary=evidence_summary,
                notification_payload=payload,
                is_principal=is_principal,
            )

        # For all other classifications (protected, current work, unknown/stale, done): no intervention
        return None


# --- Durable SQLite Intervention Ledger & Outbox ---

class InterventionLedger:
    """Transactional SQLite intervention ledger with durable outbox and capacity controls."""

    def __init__(
        self,
        db_path: str,
        clock: Optional[Callable[[], datetime.datetime]] = None,
        max_active: int = MAX_ACTIVE_INTERVENTIONS,
        max_outbox: int = MAX_OUTBOX_PENDING,
        max_retained: int = MAX_RETAINED_HISTORY,
        max_audit_events: int = MAX_AUDIT_EVENTS,
        freshness_threshold_seconds: float = FRESHNESS_THRESHOLD_SECONDS,
    ):
        self.db_path = db_path
        self._clock = clock or (lambda: datetime.datetime.now(datetime.timezone.utc))
        self.max_active = max_active
        self.max_outbox = max_outbox
        self.max_retained = max_retained
        self.max_audit_events = max_audit_events
        self.freshness_threshold_seconds = freshness_threshold_seconds

        # Ensure directory exists if not in-memory
        if db_path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)

        self._conn = sqlite3.connect(
            self.db_path,
            timeout=30.0,
            isolation_level=None,  # Autocommit mode; we manage explicit BEGIN transactions
            check_same_thread=False,
        )
        self._init_db()

    def _now(self) -> datetime.datetime:
        dt = self._clock()
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.astimezone(datetime.timezone.utc)

    def _init_db(self) -> None:
        with self._conn:
            cur = self._conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL;")
            cur.execute("PRAGMA foreign_keys=ON;")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS interventions (
                    id TEXT PRIMARY KEY,
                    dedup_key TEXT UNIQUE NOT NULL,
                    obligation_id TEXT NOT NULL,
                    obligation_revision INTEGER NOT NULL,
                    owner_tag TEXT NOT NULL,
                    workspace TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    intervention_type TEXT NOT NULL,
                    state TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    timeout_at TEXT,
                    reason TEXT NOT NULL,
                    evidence_summary TEXT,
                    outbox_message_id TEXT,
                    is_principal INTEGER NOT NULL DEFAULT 0
                );
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_intv_state ON interventions(state);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_intv_owner ON interventions(owner_tag, workspace, session_id, generation);")

            # Owner-wide uniqueness: strictly ONE unresolved intervention per owner binding!
            cur.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_owner_unresolved
                ON interventions(owner_tag, workspace, session_id, generation)
                WHERE state NOT IN ('completed', 'fenced', 'cancelled');
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS outbox (
                    message_id TEXT PRIMARY KEY,
                    intervention_id TEXT NOT NULL REFERENCES interventions(id),
                    dedup_key TEXT NOT NULL,
                    route TEXT NOT NULL,
                    recipient_tag TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    dispatched_at TEXT,
                    uncertain_at TEXT,
                    is_principal INTEGER NOT NULL DEFAULT 0
                );
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_outbox_status ON outbox(status);")

            cur.execute("""
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    intervention_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    from_state TEXT,
                    to_state TEXT,
                    detail TEXT,
                    recorded_at TEXT NOT NULL
                );
            """)

            # Dedup tombstones: bounded retention without allowing token replay
            cur.execute("""
                CREATE TABLE IF NOT EXISTS dedup_tombstones (
                    dedup_key TEXT PRIMARY KEY,
                    intervention_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    terminal_at TEXT NOT NULL
                );
            """)

    def close(self) -> None:
        self._conn.close()

    def _row_to_intervention(self, row: Tuple[Any, ...]) -> InterventionRecord:
        return InterventionRecord(
            id=row[0],
            dedup_key=row[1],
            obligation_id=row[2],
            obligation_revision=row[3],
            owner_tag=row[4],
            workspace=row[5],
            session_id=row[6],
            generation=row[7],
            intervention_type=row[8],
            state=row[9],
            created_at=row[10],
            updated_at=row[11],
            timeout_at=row[12],
            reason=row[13],
            evidence_summary=row[14],
            outbox_message_id=row[15],
            is_principal=bool(row[16]) if len(row) > 16 else False,
        )

    def _row_to_outbox(self, row: Tuple[Any, ...]) -> OutboxMessage:
        return OutboxMessage(
            message_id=row[0],
            intervention_id=row[1],
            dedup_key=row[2],
            route=row[3],
            recipient_tag=row[4],
            payload_json=row[5],
            status=row[6],
            attempt_count=row[7],
            created_at=row[8],
            dispatched_at=row[9],
            uncertain_at=row[10],
            is_principal=bool(row[11]) if len(row) > 11 else False,
        )

    def get_intervention(self, intervention_id: str) -> Optional[InterventionRecord]:
        cur = self._conn.cursor()
        cur.execute("SELECT * FROM interventions WHERE id = ?", (intervention_id,))
        row = cur.fetchone()
        return self._row_to_intervention(row) if row else None

    def get_outbox_message(self, message_id: str) -> Optional[OutboxMessage]:
        cur = self._conn.cursor()
        cur.execute("SELECT * FROM outbox WHERE message_id = ?", (message_id,))
        row = cur.fetchone()
        return self._row_to_outbox(row) if row else None

    def record_decision(self, decision: OversightDecision) -> InterventionRecord:
        """Atomically record an oversight decision and queue its outbox message.

        Survives restarts and concurrent callers via deterministic deduplication.
        Enforces one unresolved intervention per owner binding across obligations and types.
        Honors bounded active row limits without pruning active rows.
        """
        now = self._now()
        now_str = now_iso(now)
        canon_ws = os.path.realpath(os.path.abspath(decision.workspace))

        dedup_tuple = [
            str(decision.obligation_id),
            int(decision.obligation_revision),
            str(decision.owner_tag),
            str(canon_ws),
            str(decision.session_id),
            int(decision.generation),
            str(decision.intervention_type),
        ]
        canonical_json = json.dumps(dedup_tuple, separators=(",", ":"), ensure_ascii=True)
        dedup_key = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()

        cur = self._conn.cursor()
        cur.execute("BEGIN IMMEDIATE;")
        try:
            # 1. Check dedup tombstones (already completed/pruned, replay must not recreate)
            cur.execute(
                "SELECT intervention_id, state, terminal_at FROM dedup_tombstones WHERE dedup_key = ?",
                (dedup_key,),
            )
            tomb_row = cur.fetchone()
            if tomb_row:
                cur.execute("SELECT * FROM interventions WHERE id = ?", (tomb_row[0],))
                existing_row = cur.fetchone()
                cur.execute("COMMIT;")
                if existing_row:
                    return self._row_to_intervention(existing_row)
                return InterventionRecord(
                    id=tomb_row[0],
                    dedup_key=dedup_key,
                    obligation_id=decision.obligation_id,
                    obligation_revision=decision.obligation_revision,
                    owner_tag=decision.owner_tag,
                    workspace=canon_ws,
                    session_id=decision.session_id,
                    generation=decision.generation,
                    intervention_type=decision.intervention_type,
                    state=tomb_row[1],
                    created_at=tomb_row[2],
                    updated_at=tomb_row[2],
                    timeout_at=None,
                    reason=decision.reason,
                    evidence_summary=decision.evidence_summary,
                    outbox_message_id=None,
                    is_principal=decision.is_principal,
                )

            # 2. Deterministic Deduplication: check if exact decision already exists
            cur.execute("SELECT * FROM interventions WHERE dedup_key = ?", (dedup_key,))
            existing_row = cur.fetchone()
            if existing_row:
                cur.execute("COMMIT;")
                return self._row_to_intervention(existing_row)

            # 3. Owner Reservation: exactly ONE unresolved intervention per owner binding
            cur.execute("""
                SELECT * FROM interventions
                WHERE owner_tag = ? AND workspace = ? AND session_id = ? AND generation = ?
                AND state NOT IN ('completed', 'fenced', 'cancelled')
            """, (decision.owner_tag, canon_ws, decision.session_id, decision.generation))
            active_owner_row = cur.fetchone()
            if active_owner_row:
                cur.execute("COMMIT;")
                return self._row_to_intervention(active_owner_row)

            # 4. Honest Capacity Enforcement:
            # Include all reserved states (not in completed, fenced, cancelled)
            cur.execute("""
                SELECT COUNT(*) FROM interventions
                WHERE state NOT IN ('completed', 'fenced', 'cancelled')
            """)
            active_count = cur.fetchone()[0]
            if active_count >= self.max_active:
                raise CapacityExceededError(
                    f"Active interventions capacity limit reached ({active_count} >= {self.max_active}). "
                    "Refusing new intervention to preserve existing obligations."
                )

            # Include pending, sending, and uncertain outbox
            cur.execute("""
                SELECT COUNT(*) FROM outbox
                WHERE status IN ('pending', 'sending', 'uncertain')
            """)
            pending_outbox_count = cur.fetchone()[0]
            if pending_outbox_count >= self.max_outbox:
                raise CapacityExceededError(
                    f"Pending/uncertain outbox capacity limit reached ({pending_outbox_count} >= {self.max_outbox}). "
                    "Refusing new intervention to preserve buffer integrity."
                )

            # Retained history limit includes active interventions + tombstones
            cur.execute("SELECT COUNT(*) FROM interventions")
            total_intvs = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM dedup_tombstones")
            total_tombs = cur.fetchone()[0]
            if (total_intvs + total_tombs) >= self.max_retained:
                raise CapacityExceededError(
                    f"Retained history capacity limit reached ({total_intvs + total_tombs} >= {self.max_retained}). "
                    "Refusing new intervention rather than forgetting old token and allowing replay."
                )

            # Audit events limit
            cur.execute("SELECT COUNT(*) FROM audit_events")
            total_audit = cur.fetchone()[0]
            if total_audit >= self.max_audit_events:
                raise CapacityExceededError(
                    f"Audit events capacity limit reached ({total_audit} >= {self.max_audit_events}). "
                    "Refusing new intervention to preserve audit log integrity."
                )

            # 5. Create Intervention & Outbox atomically
            intervention_id = f"intv-{uuid.uuid4()}"
            message_id = f"msg-{uuid.uuid4()}"
            timeout_at = now_iso(now + datetime.timedelta(seconds=decision.timeout_seconds))

            sanitized_reason = sanitize_text(decision.reason, MAX_REASON_LENGTH)
            sanitized_evidence = sanitize_text(decision.evidence_summary, MAX_EVIDENCE_LENGTH)

            # Check raw payload size before sanitization for honest capacity rejection
            try:
                raw_bytes = json.dumps(decision.notification_payload).encode("utf-8")
                if len(raw_bytes) > MAX_PAYLOAD_BYTES:
                    raise CapacityExceededError(
                        f"Notification payload exceeds maximum allowed size ({len(raw_bytes)} > {MAX_PAYLOAD_BYTES}). "
                        "Refusing new intervention rather than silently truncating or rewriting payload."
                    )
            except (TypeError, ValueError):
                pass

            # Ensure notification payload has no raw secrets and conforms to known safe schema
            sanitized_payload = sanitize_payload(decision.notification_payload)
            payload_json = json.dumps(sanitized_payload, ensure_ascii=True)
            payload_bytes = payload_json.encode("utf-8")
            if len(payload_bytes) > MAX_PAYLOAD_BYTES:
                raise CapacityExceededError(
                    f"Notification payload exceeds maximum allowed size ({len(payload_bytes)} > {MAX_PAYLOAD_BYTES}). "
                    "Refusing new intervention rather than silently truncating or rewriting payload."
                )

            cur.execute("""
                INSERT INTO interventions (
                    id, dedup_key, obligation_id, obligation_revision,
                    owner_tag, workspace, session_id, generation,
                    intervention_type, state, created_at, updated_at,
                    timeout_at, reason, evidence_summary, outbox_message_id, is_principal
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """, (
                intervention_id, dedup_key, decision.obligation_id, decision.obligation_revision,
                decision.owner_tag, canon_ws, decision.session_id, decision.generation,
                decision.intervention_type, STATE_OUTBOX_PENDING, now_str, now_str,
                timeout_at, sanitized_reason, sanitized_evidence, message_id,
                1 if decision.is_principal else 0
            ))

            cur.execute("""
                INSERT INTO outbox (
                    message_id, intervention_id, dedup_key, route,
                    recipient_tag, payload_json, status, attempt_count,
                    created_at, dispatched_at, uncertain_at, is_principal
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, NULL, NULL, ?);
            """, (
                message_id, intervention_id, dedup_key, decision.route,
                decision.owner_tag, payload_json, OUTBOX_PENDING, now_str,
                1 if decision.is_principal else 0
            ))

            cur.execute("""
                INSERT INTO audit_events (
                    intervention_id, event_type, from_state, to_state, detail, recorded_at
                ) VALUES (?, 'decision', NULL, ?, ?, ?);
            """, (
                intervention_id, STATE_OUTBOX_PENDING, f"Decision recorded: {sanitized_reason}", now_str
            ))

            cur.execute("COMMIT;")

            return InterventionRecord(
                id=intervention_id,
                dedup_key=dedup_key,
                obligation_id=decision.obligation_id,
                obligation_revision=decision.obligation_revision,
                owner_tag=decision.owner_tag,
                workspace=canon_ws,
                session_id=decision.session_id,
                generation=decision.generation,
                intervention_type=decision.intervention_type,
                state=STATE_OUTBOX_PENDING,
                created_at=now_str,
                updated_at=now_str,
                timeout_at=timeout_at,
                reason=sanitized_reason,
                evidence_summary=sanitized_evidence,
                outbox_message_id=message_id,
                is_principal=decision.is_principal,
            )
        except sqlite3.IntegrityError:
            try:
                cur.execute("ROLLBACK;")
            except sqlite3.OperationalError:
                pass
            # Concurrent race caught by SQL unique constraint:
            # Re-read existing active intervention or dedup key
            cur.execute("""
                SELECT * FROM interventions
                WHERE owner_tag = ? AND workspace = ? AND session_id = ? AND generation = ?
                AND state NOT IN ('completed', 'fenced', 'cancelled')
            """, (decision.owner_tag, canon_ws, decision.session_id, decision.generation))
            row = cur.fetchone()
            if row:
                return self._row_to_intervention(row)
            cur.execute("SELECT * FROM interventions WHERE dedup_key = ?", (dedup_key,))
            row = cur.fetchone()
            if row:
                return self._row_to_intervention(row)
            raise
        except Exception:
            try:
                cur.execute("ROLLBACK;")
            except sqlite3.OperationalError:
                pass
            raise

    def dispatch_outbox_message(
        self,
        message_id: str,
        transport: TransportProtocol,
        guard: Optional[GuardProtocol] = None,
    ) -> bool:
        """Dispatch a queued outbox message via transport with guard checks.

        Distinguishes pre-send failure from uncertain delivery.
        Uncertain delivery is reserved and never replayed automatically.
        """
        now = self._now()
        now_str = now_iso(now)

        # 1. Guard check is mandatory! None/missing/invalid/false guard has ZERO transport calls.
        if guard is None:
            raise GuardAuthorizationError("Guard is mandatory for dispatch; None guard is forbidden")
        if not hasattr(guard, "authorize_dispatch") or not callable(guard.authorize_dispatch):
            raise GuardAuthorizationError("Guard is invalid; authorize_dispatch method is required")

        cur = self._conn.cursor()
        cur.execute("BEGIN IMMEDIATE;")
        try:
            cur.execute("""
                SELECT o.*, i.state, i.timeout_at, i.is_principal
                FROM outbox o
                JOIN interventions i ON o.intervention_id = i.id
                WHERE o.message_id = ?
            """, (message_id,))
            row = cur.fetchone()
            if not row:
                raise ValueError(f"Outbox message {message_id} not found")

            # OutboxMessage has 12 columns in table
            msg = self._row_to_outbox(row[:12])
            intv_state = row[12]
            intv_timeout_at = row[13]
            intv_is_principal = bool(row[14]) if len(row) > 14 else msg.is_principal

            # Uncertainty reservation: never replay uncertain deliveries
            if msg.status == OUTBOX_UNCERTAIN:
                raise TransportError(
                    f"Outbox message {message_id} is in uncertain delivery state; automatic replay is forbidden"
                )
            if msg.status == OUTBOX_SENDING:
                raise TransportError(
                    f"Outbox message {message_id} is currently sending; automatic or concurrent resend is forbidden"
                )

            if msg.status != OUTBOX_PENDING:
                cur.execute("COMMIT;")
                return False

            # Lifecycle checks on intervention state:
            # Escalated, terminal (completed, cancelled, fenced), or failed cannot dispatch!
            if intv_state in (STATE_ESCALATION_NEEDED, STATE_FENCED, STATE_CANCELLED, STATE_COMPLETED, STATE_FAILED):
                cur.execute("UPDATE outbox SET status = ? WHERE message_id = ?", (OUTBOX_CANCELLED, message_id))
                cur.execute("COMMIT;")
                return False

            if intv_state != STATE_OUTBOX_PENDING:
                cur.execute("COMMIT;")
                return False

            # Timeout check on intervention
            if intv_timeout_at is not None:
                try:
                    to_dt = parse_iso_utc(intv_timeout_at)
                    if now >= to_dt:
                        # Expired! Cannot dispatch
                        cur.execute("UPDATE interventions SET state = ?, updated_at = ? WHERE id = ?", (STATE_ESCALATION_NEEDED, now_str, msg.intervention_id))
                        cur.execute("UPDATE outbox SET status = ? WHERE message_id = ?", (OUTBOX_CANCELLED, message_id))
                        cur.execute("""
                            INSERT INTO audit_events (
                                intervention_id, event_type, from_state, to_state, detail, recorded_at
                            ) VALUES (?, 'timeout_expired_dispatch_refused', ?, ?, 'Dispatch refused on expired intervention', ?);
                        """, (msg.intervention_id, STATE_OUTBOX_PENDING, STATE_ESCALATION_NEEDED, now_str))
                        cur.execute("COMMIT;")
                        return False
                except Exception:
                    pass

            # Caller Guard Authorization check
            try:
                authorized, reason = guard.authorize_dispatch(msg)
            except Exception as exc:
                raise GuardAuthorizationError(f"Guard authorize_dispatch raised error: {exc}")
            if not authorized:
                raise GuardAuthorizationError(f"Dispatch unauthorized by guard: {reason}")

            # Route Policy Check: Principal is strictly bus-only
            if intv_is_principal or msg.is_principal or msg.recipient_tag.strip().lower() == "codex-principal" or "principal" in msg.recipient_tag.strip().lower():
                if msg.route != ROUTE_BUS:
                    raise GuardAuthorizationError("Principal messages must use bus-only route")

            # Mark sending
            cur.execute("""
                UPDATE outbox SET status = ?, attempt_count = attempt_count + 1 WHERE message_id = ?
            """, (OUTBOX_SENDING, message_id))
            cur.execute("COMMIT;")
        except Exception:
            try:
                cur.execute("ROLLBACK;")
            except sqlite3.OperationalError:
                pass
            raise

        # Execute Transport Send outside of SQLite transaction
        payload_data = json.loads(msg.payload_json)
        try:
            transport.send(
                route=msg.route,
                recipient_tag=msg.recipient_tag,
                payload=payload_data,
                idempotency_key=msg.dedup_key,
            )
        except TransportSendFailedBeforeDelivery as exc:
            # Failed before transmitting bytes: ONE atomic SQLite transaction
            cur = self._conn.cursor()
            cur.execute("BEGIN IMMEDIATE;")
            try:
                cur.execute("""
                    UPDATE outbox SET status = ? WHERE message_id = ?
                """, (OUTBOX_FAILED, message_id))
                cur.execute("""
                    INSERT INTO audit_events (
                        intervention_id, event_type, from_state, to_state, detail, recorded_at
                    ) VALUES (?, 'dispatch_failed_before_send', ?, ?, ?, ?);
                """, (msg.intervention_id, STATE_OUTBOX_PENDING, STATE_OUTBOX_PENDING, str(exc), now_str))
                cur.execute("COMMIT;")
            except Exception:
                try:
                    cur.execute("ROLLBACK;")
                except Exception:
                    pass
                raise
            return False
        except (TransportUncertainError, Exception) as exc:
            # Uncertain delivery (connection timeout, dropped socket, uncertain ACK): ONE atomic SQLite transaction
            cur = self._conn.cursor()
            cur.execute("BEGIN IMMEDIATE;")
            try:
                cur.execute("""
                    UPDATE outbox SET status = ?, uncertain_at = ? WHERE message_id = ?
                """, (OUTBOX_UNCERTAIN, now_str, message_id))
                cur.execute("""
                    UPDATE interventions SET state = ?, updated_at = ? WHERE id = ?
                """, (STATE_DISPATCHED_UNCERTAIN, now_str, msg.intervention_id))
                cur.execute("""
                    INSERT INTO audit_events (
                        intervention_id, event_type, from_state, to_state, detail, recorded_at
                    ) VALUES (?, 'dispatch_uncertain', ?, ?, ?, ?);
                """, (msg.intervention_id, STATE_OUTBOX_PENDING, STATE_DISPATCHED_UNCERTAIN, str(exc), now_str))
                cur.execute("COMMIT;")
            except Exception:
                try:
                    cur.execute("ROLLBACK;")
                except Exception:
                    pass
                raise
            return False

        # Confirmed Sent: ONE atomic SQLite transaction
        cur = self._conn.cursor()
        cur.execute("BEGIN IMMEDIATE;")
        try:
            cur.execute("""
                UPDATE outbox SET status = ?, dispatched_at = ? WHERE message_id = ?
            """, (OUTBOX_SENT, now_str, message_id))
            cur.execute("""
                UPDATE interventions SET state = ?, updated_at = ? WHERE id = ?
            """, (STATE_DISPATCHED_SENT, now_str, msg.intervention_id))
            cur.execute("""
                INSERT INTO audit_events (
                    intervention_id, event_type, from_state, to_state, detail, recorded_at
                ) VALUES (?, 'dispatch_sent', ?, ?, 'Message sent confirmed', ?);
            """, (msg.intervention_id, STATE_OUTBOX_PENDING, STATE_DISPATCHED_SENT, now_str))
            cur.execute("COMMIT;")
        except Exception:
            try:
                cur.execute("ROLLBACK;")
            except Exception:
                pass
            raise
        return True

    def record_mailbox_read_ack(self, message_id: str, ack: MailboxReadAck) -> InterventionRecord:
        """Record mailbox read receipt, strictly enforcing monotonic lifecycle and session bindings."""
        now_str = now_iso(self._now())
        cur = self._conn.cursor()
        cur.execute("BEGIN IMMEDIATE;")
        try:
            cur.execute("""
                SELECT i.*, o.dispatched_at, o.status FROM interventions i
                JOIN outbox o ON o.intervention_id = i.id
                WHERE o.message_id = ?
            """, (message_id,))
            row = cur.fetchone()
            if not row:
                raise ValueError(f"No intervention found for message {message_id}")

            intv = self._row_to_intervention(row[:17])
            dispatched_at = row[17] if len(row) > 17 else None

            # Idempotent replay: already accepted identical read ACK
            if intv.state == STATE_MAILBOX_READ_ACK:
                if ack.reader_session_id == intv.session_id and ack.reader_generation == intv.generation:
                    cur.execute("COMMIT;")
                    return intv
                raise SecurityBindingError(f"Contradictory replay for mailbox read ACK on intervention {intv.id}")

            # Terminal / expired check
            if intv.state in TERMINAL_STATES or intv.state == STATE_ESCALATION_NEEDED:
                raise SecurityBindingError(f"Intervention {intv.id} is in terminal or expired state ({intv.state})")

            # Monotonic lifecycle: MUST be in STATE_DISPATCHED_SENT!
            if intv.state == STATE_OUTBOX_PENDING:
                raise SecurityBindingError(f"Cannot record mailbox read ACK before message is dispatched (state: {intv.state})")
            if intv.state != STATE_DISPATCHED_SENT:
                raise SecurityBindingError(f"Invalid state for mailbox read ACK: {intv.state}; expected {STATE_DISPATCHED_SENT}")

            # Future timestamp rejection
            try:
                read_dt = parse_iso_utc(ack.read_at)
            except Exception as exc:
                raise SecurityBindingError(f"Malformed read_at timestamp: {exc}")
            if read_dt > self._now():
                raise SecurityBindingError(f"Contradictory future timestamp in mailbox read ACK: {ack.read_at}")

            # Monotonic timestamp check against dispatch
            if dispatched_at:
                try:
                    disp_dt = parse_iso_utc(dispatched_at)
                except Exception:
                    disp_dt = None
                if disp_dt and read_dt < disp_dt:
                    raise SecurityBindingError(f"Read ACK timestamp ({ack.read_at}) earlier than dispatch ({dispatched_at})")

            # Security Binding & Generation Fence
            if ack.reader_session_id != intv.session_id or ack.reader_generation != intv.generation:
                raise SecurityBindingError(
                    f"Forged or stale reader session/generation: expected ({intv.session_id}, gen {intv.generation}), "
                    f"got ({ack.reader_session_id}, gen {ack.reader_generation})"
                )

            cur.execute("""
                UPDATE interventions SET state = ?, updated_at = ? WHERE id = ?
            """, (STATE_MAILBOX_READ_ACK, now_str, intv.id))
            cur.execute("""
                INSERT INTO audit_events (
                    intervention_id, event_type, from_state, to_state, detail, recorded_at
                ) VALUES (?, 'mailbox_read_ack', ?, ?, ?, ?);
            """, (intv.id, intv.state, STATE_MAILBOX_READ_ACK, f"Read by {ack.reader_session_id}", now_str))

            cur.execute("COMMIT;")
            return dataclasses.replace(intv, state=STATE_MAILBOX_READ_ACK, updated_at=now_str)
        except Exception:
            try:
                cur.execute("ROLLBACK;")
            except sqlite3.OperationalError:
                pass
            raise

    def record_custody_acceptance(self, intervention_id: str, custody: CustodyAcceptance) -> InterventionRecord:
        """Record explicit custody acceptance by authenticated owner, strictly enforcing lifecycle and exact fields."""
        now_str = now_iso(self._now())
        cur = self._conn.cursor()
        cur.execute("BEGIN IMMEDIATE;")
        try:
            cur.execute("SELECT * FROM interventions WHERE id = ?", (intervention_id,))
            row = cur.fetchone()
            if not row:
                raise ValueError(f"Intervention {intervention_id} not found")

            intv = self._row_to_intervention(row)

            # Idempotent replay: already accepted identical custody
            if intv.state == STATE_CUSTODY_ACCEPTED:
                if (
                    custody.obligation_id == intv.obligation_id
                    and custody.owner_tag == intv.owner_tag
                    and custody.session_id == intv.session_id
                    and custody.generation == intv.generation
                ):
                    cur.execute("COMMIT;")
                    return intv
                raise SecurityBindingError(f"Contradictory replay for custody acceptance on intervention {intv.id}")

            # Terminal / expired check
            if intv.state in TERMINAL_STATES or intv.state == STATE_ESCALATION_NEEDED:
                raise SecurityBindingError(f"Intervention {intv.id} is in terminal or expired state ({intv.state})")

            # Monotonic lifecycle: MUST be in STATE_MAILBOX_READ_ACK!
            if intv.state != STATE_MAILBOX_READ_ACK:
                raise SecurityBindingError(f"Cannot accept custody from state '{intv.state}'; expected '{STATE_MAILBOX_READ_ACK}'")

            # Future timestamp rejection
            try:
                acc_dt = parse_iso_utc(custody.accepted_at)
            except Exception as exc:
                raise SecurityBindingError(f"Malformed accepted_at timestamp: {exc}")
            if acc_dt > self._now():
                raise SecurityBindingError(f"Contradictory future timestamp in custody acceptance: {custody.accepted_at}")

            # Monotonic timestamp check against read ACK
            cur.execute("""
                SELECT recorded_at FROM audit_events
                WHERE intervention_id = ? AND event_type = 'mailbox_read_ack'
                ORDER BY id DESC LIMIT 1
            """, (intervention_id,))
            read_ack_row = cur.fetchone()
            if read_ack_row:
                try:
                    read_dt = parse_iso_utc(read_ack_row[0])
                except Exception:
                    read_dt = None
                if read_dt and acc_dt < read_dt:
                    raise SecurityBindingError(f"Custody accepted_at ({custody.accepted_at}) earlier than read ACK ({read_ack_row[0]})")

            # Exact session/generation/owner/obligation binding validation
            if custody.obligation_id != intv.obligation_id:
                raise SecurityBindingError(
                    f"Mismatched custody obligation: expected {intv.obligation_id}, got {custody.obligation_id}"
                )
            if custody.owner_tag != intv.owner_tag:
                raise SecurityBindingError(
                    f"Mismatched custody owner: expected {intv.owner_tag}, got {custody.owner_tag}"
                )
            if custody.session_id != intv.session_id:
                raise SecurityBindingError(
                    f"Mismatched custody session: expected {intv.session_id}, got {custody.session_id}"
                )
            if custody.generation != intv.generation:
                raise SecurityBindingError(
                    f"Mismatched custody generation: expected {intv.generation}, got {custody.generation}"
                )
            if custody.obligation_revision is not None and custody.obligation_revision != intv.obligation_revision:
                raise SecurityBindingError(
                    f"Mismatched custody revision: expected {intv.obligation_revision}, got {custody.obligation_revision}"
                )
            if custody.workspace is not None:
                c_ws = os.path.realpath(os.path.abspath(custody.workspace))
                if c_ws != intv.workspace:
                    raise SecurityBindingError(
                        f"Mismatched custody workspace: expected {intv.workspace}, got {c_ws}"
                    )

            cur.execute("""
                UPDATE interventions SET state = ?, updated_at = ? WHERE id = ?
            """, (STATE_CUSTODY_ACCEPTED, now_str, intv.id))
            cur.execute("""
                INSERT INTO audit_events (
                    intervention_id, event_type, from_state, to_state, detail, recorded_at
                ) VALUES (?, 'custody_accepted', ?, ?, ?, ?);
            """, (intv.id, intv.state, STATE_CUSTODY_ACCEPTED, f"Custody accepted by {custody.owner_tag}", now_str))

            cur.execute("COMMIT;")
            return dataclasses.replace(intv, state=STATE_CUSTODY_ACCEPTED, updated_at=now_str)
        except Exception:
            try:
                cur.execute("ROLLBACK;")
            except sqlite3.OperationalError:
                pass
            raise

    def record_first_action(self, intervention_id: str, action: ActionReceipt) -> InterventionRecord:
        """Record genuine first useful action/tool output receipt, strictly enforcing lifecycle and productivity."""
        now_str = now_iso(self._now())
        cur = self._conn.cursor()
        cur.execute("BEGIN IMMEDIATE;")
        try:
            cur.execute("SELECT * FROM interventions WHERE id = ?", (intervention_id,))
            row = cur.fetchone()
            if not row:
                raise ValueError(f"Intervention {intervention_id} not found")

            intv = self._row_to_intervention(row)

            # Idempotent replay: already recorded identical first action
            if intv.state == STATE_FIRST_ACTION_OBSERVED:
                if (
                    action.session_id == intv.session_id
                    and action.generation == intv.generation
                    and action.action_type in VALID_USEFUL_ACTION_TYPES
                ):
                    cur.execute("COMMIT;")
                    return intv
                raise SecurityBindingError(f"Contradictory replay for first action on intervention {intv.id}")

            # Terminal / expired check
            if intv.state in TERMINAL_STATES or intv.state == STATE_ESCALATION_NEEDED:
                raise SecurityBindingError(f"Intervention {intv.id} is in terminal or expired state ({intv.state})")

            # Monotonic lifecycle: MUST be in STATE_CUSTODY_ACCEPTED!
            if intv.state != STATE_CUSTODY_ACCEPTED:
                raise SecurityBindingError(f"Cannot record first action from state '{intv.state}'; expected '{STATE_CUSTODY_ACCEPTED}'")

            # Useful action type enforcement: heartbeat/spinner/ack forbidden!
            if action.action_type not in VALID_USEFUL_ACTION_TYPES:
                raise SecurityBindingError(
                    f"Action type '{action.action_type}' is not a valid useful action (heartbeat, spinner, and ACK actions are forbidden)"
                )

            # Future timestamp rejection
            try:
                act_dt = parse_iso_utc(action.timestamp)
            except Exception as exc:
                raise SecurityBindingError(f"Malformed timestamp in action receipt: {exc}")
            if act_dt > self._now():
                raise SecurityBindingError(f"Contradictory future timestamp in action receipt: {action.timestamp}")

            # Freshness threshold check
            age_s = (self._now() - act_dt).total_seconds()
            if age_s > self.freshness_threshold_seconds:
                raise SecurityBindingError(f"First action receipt is stale ({age_s:.1f}s > {self.freshness_threshold_seconds}s)")

            # Monotonic timestamp check against custody acceptance
            cur.execute("""
                SELECT recorded_at FROM audit_events
                WHERE intervention_id = ? AND event_type = 'custody_accepted'
                ORDER BY id DESC LIMIT 1
            """, (intervention_id,))
            cust_row = cur.fetchone()
            if cust_row:
                try:
                    cust_dt = parse_iso_utc(cust_row[0])
                except Exception:
                    cust_dt = None
                if cust_dt and act_dt < cust_dt:
                    raise SecurityBindingError(f"Action timestamp ({action.timestamp}) earlier than custody acceptance ({cust_row[0]})")

            # Validate binding
            if action.session_id != intv.session_id or action.generation != intv.generation:
                raise SecurityBindingError(
                    f"Mismatched session/generation for first action: expected ({intv.session_id}, gen {intv.generation}), "
                    f"got ({action.session_id}, gen {action.generation})"
                )
            if action.obligation_id is not None and action.obligation_id != intv.obligation_id:
                raise SecurityBindingError(
                    f"Mismatched obligation for first action: expected {intv.obligation_id}, got {action.obligation_id}"
                )
            if action.obligation_revision is not None and action.obligation_revision != intv.obligation_revision:
                raise SecurityBindingError(
                    f"Mismatched revision for first action: expected {intv.obligation_revision}, got {action.obligation_revision}"
                )
            if action.owner_tag is not None and action.owner_tag != intv.owner_tag:
                raise SecurityBindingError(
                    f"Mismatched owner for first action: expected {intv.owner_tag}, got {action.owner_tag}"
                )

            cur.execute("""
                UPDATE interventions SET state = ?, updated_at = ? WHERE id = ?
            """, (STATE_FIRST_ACTION_OBSERVED, now_str, intv.id))
            cur.execute("""
                INSERT INTO audit_events (
                    intervention_id, event_type, from_state, to_state, detail, recorded_at
                ) VALUES (?, 'first_action_observed', ?, ?, ?, ?);
            """, (intv.id, intv.state, STATE_FIRST_ACTION_OBSERVED, f"Action: {action.action_type}", now_str))

            cur.execute("COMMIT;")
            return dataclasses.replace(intv, state=STATE_FIRST_ACTION_OBSERVED, updated_at=now_str)
        except Exception:
            try:
                cur.execute("ROLLBACK;")
            except sqlite3.OperationalError:
                pass
            raise

    def record_completion_outcome(self, intervention_id: str, outcome: CompletionOutcome) -> InterventionRecord:
        """Record completed outcome for the intervention, strictly enforcing lifecycle and exact fields."""
        now_str = now_iso(self._now())
        cur = self._conn.cursor()
        cur.execute("BEGIN IMMEDIATE;")
        try:
            cur.execute("SELECT * FROM interventions WHERE id = ?", (intervention_id,))
            row = cur.fetchone()
            if not row:
                raise ValueError(f"Intervention {intervention_id} not found")

            intv = self._row_to_intervention(row)

            # Idempotent replay: already completed with matching outcome
            if intv.state == STATE_COMPLETED:
                cur.execute("""
                    SELECT detail FROM audit_events
                    WHERE intervention_id = ? AND to_state = 'completed'
                    ORDER BY id DESC LIMIT 1
                """, (intv.id,))
                comp_row = cur.fetchone()
                existing_res = None
                if comp_row and comp_row[0]:
                    try:
                        existing_res = json.loads(comp_row[0])
                    except Exception:
                        existing_res = None
                if (
                    outcome.obligation_id == intv.obligation_id
                    and outcome.owner_tag == intv.owner_tag
                    and outcome.session_id == intv.session_id
                    and outcome.generation == intv.generation
                    and (existing_res is None or outcome.result == existing_res)
                ):
                    cur.execute("COMMIT;")
                    return intv
                raise SecurityBindingError(f"Contradictory replay for completion on intervention {intv.id}")

            # Terminal / expired / fenced checks
            if intv.state in TERMINAL_STATES or intv.state == STATE_ESCALATION_NEEDED:
                raise SecurityBindingError(f"Intervention {intv.id} is in terminal or expired state ({intv.state})")

            # Monotonic lifecycle: MUST be in STATE_FIRST_ACTION_OBSERVED!
            if intv.state != STATE_FIRST_ACTION_OBSERVED:
                raise SecurityBindingError(
                    f"Cannot complete intervention from state '{intv.state}'; expected '{STATE_FIRST_ACTION_OBSERVED}' (first action must be observed before completion)"
                )

            # Future timestamp rejection
            try:
                comp_dt = parse_iso_utc(outcome.completed_at)
            except Exception as exc:
                raise SecurityBindingError(f"Malformed completed_at timestamp: {exc}")
            if comp_dt > self._now():
                raise SecurityBindingError(f"Contradictory future timestamp in completion outcome: {outcome.completed_at}")

            # Monotonic timestamp check against first action
            cur.execute("""
                SELECT recorded_at FROM audit_events
                WHERE intervention_id = ? AND event_type = 'first_action_observed'
                ORDER BY id DESC LIMIT 1
            """, (intervention_id,))
            act_row = cur.fetchone()
            if act_row:
                try:
                    act_dt = parse_iso_utc(act_row[0])
                except Exception:
                    act_dt = None
                if act_dt and comp_dt < act_dt:
                    raise SecurityBindingError(f"Completion timestamp ({outcome.completed_at}) earlier than first action ({act_row[0]})")

            # Exact field binding validation: obligation_id, owner_tag, session_id, generation
            if outcome.obligation_id != intv.obligation_id:
                raise SecurityBindingError(
                    f"Mismatched obligation_id for completion: expected {intv.obligation_id}, got {outcome.obligation_id}"
                )
            if outcome.owner_tag != intv.owner_tag:
                raise SecurityBindingError(
                    f"Mismatched owner_tag for completion: expected {intv.owner_tag}, got {outcome.owner_tag}"
                )
            if outcome.session_id != intv.session_id:
                raise SecurityBindingError(
                    f"Mismatched session_id for completion: expected {intv.session_id}, got {outcome.session_id}"
                )
            if outcome.generation != intv.generation:
                raise SecurityBindingError(
                    f"Mismatched generation for completion: expected {intv.generation}, got {outcome.generation}"
                )
            if outcome.obligation_revision is not None and outcome.obligation_revision != intv.obligation_revision:
                raise SecurityBindingError(
                    f"Mismatched revision for completion: expected {intv.obligation_revision}, got {outcome.obligation_revision}"
                )
            if outcome.workspace is not None:
                o_ws = os.path.realpath(os.path.abspath(outcome.workspace))
                if o_ws != intv.workspace:
                    raise SecurityBindingError(
                        f"Mismatched workspace for completion: expected {intv.workspace}, got {o_ws}"
                    )

            cur.execute("""
                UPDATE interventions SET state = ?, updated_at = ? WHERE id = ?
            """, (STATE_COMPLETED, now_str, intv.id))

            # Record dedup tombstone upon completion so old tokens are never replayed
            cur.execute("""
                INSERT OR IGNORE INTO dedup_tombstones (dedup_key, intervention_id, state, terminal_at)
                VALUES (?, ?, ?, ?);
            """, (intv.dedup_key, intv.id, STATE_COMPLETED, now_str))

            res_json = json.dumps(outcome.result) if outcome.result is not None else "{}"
            cur.execute("""
                INSERT INTO audit_events (
                    intervention_id, event_type, from_state, to_state, detail, recorded_at
                ) VALUES (?, 'completed', ?, ?, ?, ?);
            """, (intv.id, intv.state, STATE_COMPLETED, res_json, now_str))

            cur.execute("COMMIT;")
            return dataclasses.replace(intv, state=STATE_COMPLETED, updated_at=now_str)
        except Exception:
            try:
                cur.execute("ROLLBACK;")
            except sqlite3.OperationalError:
                pass
            raise

    def check_timeouts(self) -> List[str]:
        """Check expired deadlines and mark escalation_needed without retry or respawn."""
        now = self._now()
        now_str = now_iso(now)
        escalated_ids: List[str] = []

        cur = self._conn.cursor()
        cur.execute("BEGIN IMMEDIATE;")
        try:
            cur.execute("""
                SELECT id FROM interventions
                WHERE state NOT IN ('completed', 'cancelled', 'fenced', 'escalation_needed')
                AND timeout_at IS NOT NULL
                AND timeout_at <= ?
            """, (now_str,))
            rows = cur.fetchall()

            for (iid,) in rows:
                cur.execute("""
                    UPDATE interventions SET state = ?, updated_at = ? WHERE id = ?
                """, (STATE_ESCALATION_NEEDED, now_str, iid))
                cur.execute("""
                    UPDATE outbox SET status = ? WHERE intervention_id = ? AND status IN ('pending', 'sending')
                """, (OUTBOX_CANCELLED, iid))
                cur.execute("""
                    INSERT INTO audit_events (
                        intervention_id, event_type, from_state, to_state, detail, recorded_at
                    ) VALUES (?, 'timeout_escalation', NULL, ?, 'Intervention timeout exceeded without completion', ?);
                """, (iid, STATE_ESCALATION_NEEDED, now_str))
                escalated_ids.append(iid)

            cur.execute("COMMIT;")
            return escalated_ids
        except Exception:
            try:
                cur.execute("ROLLBACK;")
            except sqlite3.OperationalError:
                pass
            raise

    def fence_superseded_sessions(
        self,
        owner_tag: str,
        workspace: str,
        new_session_id: str,
        new_generation: int,
    ) -> int:
        """Fence old interventions and pending outbox messages when a session or generation advances."""
        now_str = now_iso(self._now())
        canon_ws = os.path.realpath(os.path.abspath(workspace))

        cur = self._conn.cursor()
        cur.execute("BEGIN IMMEDIATE;")
        try:
            # Find active interventions for this owner and workspace with older session or generation
            cur.execute("""
                SELECT id FROM interventions
                WHERE owner_tag = ?
                AND workspace = ?
                AND state NOT IN ('completed', 'cancelled', 'fenced', 'escalation_needed')
                AND (session_id != ? OR generation < ?)
            """, (owner_tag, canon_ws, new_session_id, new_generation))
            rows = cur.fetchall()

            fenced_count = 0
            for (iid,) in rows:
                cur.execute("""
                    UPDATE interventions SET state = ?, updated_at = ? WHERE id = ?
                """, (STATE_FENCED, now_str, iid))
                cur.execute("""
                    UPDATE outbox SET status = ? WHERE intervention_id = ? AND status IN ('pending', 'sending')
                """, (OUTBOX_CANCELLED, iid))
                cur.execute("""
                    INSERT INTO audit_events (
                        intervention_id, event_type, from_state, to_state, detail, recorded_at
                    ) VALUES (?, 'session_fenced', NULL, ?, ?, ?);
                """, (iid, STATE_FENCED, f"Fenced by new session {new_session_id} gen {new_generation}", now_str))
                fenced_count += 1

            cur.execute("COMMIT;")
            return fenced_count
        except Exception:
            try:
                cur.execute("ROLLBACK;")
            except sqlite3.OperationalError:
                pass
            raise

    def prune_terminal_retention(self, retention_seconds: float) -> int:
        """Prune old terminal rows to enforce bounding. Never deletes active or unresolved rows."""
        now = self._now()
        cutoff_str = now_iso(now - datetime.timedelta(seconds=retention_seconds))

        cur = self._conn.cursor()
        cur.execute("BEGIN IMMEDIATE;")
        try:
            # Strictly completed, cancelled, or fenced. NEVER escalation_needed or dispatched_uncertain!
            cur.execute("""
                SELECT id, dedup_key, state FROM interventions
                WHERE state IN ('completed', 'cancelled', 'fenced')
                AND updated_at <= ?
            """, (cutoff_str,))
            rows = cur.fetchall()
            if not rows:
                cur.execute("COMMIT;")
                return 0

            for iid, dkey, st in rows:
                cur.execute("""
                    INSERT OR IGNORE INTO dedup_tombstones (dedup_key, intervention_id, state, terminal_at)
                    VALUES (?, ?, ?, ?);
                """, (dkey, iid, st, cutoff_str))

            ids = [r[0] for r in rows]
            placeholders = ",".join("?" for _ in ids)
            cur.execute(f"DELETE FROM audit_events WHERE intervention_id IN ({placeholders});", ids)
            cur.execute(f"DELETE FROM outbox WHERE intervention_id IN ({placeholders});", ids)
            cur.execute(f"DELETE FROM interventions WHERE id IN ({placeholders});", ids)
            pruned_count = cur.rowcount
            cur.execute("COMMIT;")
            return pruned_count
        except Exception:
            try:
                cur.execute("ROLLBACK;")
            except sqlite3.OperationalError:
                pass
            raise

