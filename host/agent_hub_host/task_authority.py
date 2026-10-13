"""Task Authority, Provenance, and Dependency Completion Provider for Agent Hub.

docs/SPEC.md §9, §11, §12; Issue #11.

Provides:
- Standalone stdlib task authority and durable transactional SQLite ledger.
- Authenticates caller identity and token using canonical existing FileBus store API under FileLock.
- Semantic rights derive ONLY from immutable explicit configured permissions rooted outside request (default empty DENY).
- Strict exact task ownership tuple:
  (task_id, revision, project_id, canonical_workspace, owner_bus_actor,
   native_owner_tag, native_session_uuid, generation).
- Explicit read-only native binding verification component:
  mandatory workspace/tag match, non-empty fresh observed_at, explicit booleans for liveness/readiness/protection.
  (NativeSessionRecord has no semantic generation; never invent generation from PID or time).
- Current revision, owner, session, and generation protection rechecked at EVERY mutation, query, and receipt.
- Issuer-only sequential CAS revisions; default absent root cannot issue.
- Genuine owner credentials required for custody ACK before task transitions to ready (no ready from mere assignment or caller flag).
- Pinned result proposal by genuine owner credentials for exact CURRENT task tuple.
- Independent authenticated review by configured reviewer distinct from author/owner.
- Authorized acceptance provenance verifying current grant + review + pinned digest.
- Dependency completion verifier verifying DEPENDENCY's own current revision, owner, workspace,
  session, generation, issuer, protected state, pinned result, review, and acceptance record,
  plus current canonical identity presence and policy in FileBus.
- Cross-scope task isolation on read queries (no leakage of foreign records).
- Durable transactional storage with exact duplicate idempotency and changed payload conflict detection.
- Zero subprocess / native execution in provider, no background timers, threads, pollers, or daemons.
"""

from __future__ import annotations

import contextlib
import dataclasses
import datetime
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Protocol, Sequence, Set, Tuple, Union

from .oversight_adapter import (
    AuthorityResolverProtocol,
    DependencyCompletionVerifierProtocol,
    TrustedAuthorityGrant,
)


# --- Exceptions ---

class TaskAuthorityError(Exception):
    """Base exception for task authority errors."""
    pass


class AuthenticationError(TaskAuthorityError):
    """Raised when actor authentication against FileBus store fails."""
    pass


class PermissionDeniedError(TaskAuthorityError):
    """Raised when authenticated actor lacks semantic permission rooted in trust root."""
    pass


class CASConflictError(TaskAuthorityError):
    """Raised when revision, generation, or payload CAS conflict occurs."""
    pass


class FencedStateError(TaskAuthorityError):
    """Raised when operation is attempted on fenced/revoked/expired state."""
    pass


class InvalidTupleError(TaskAuthorityError):
    """Raised when task ownership tuple or payload fails validation."""
    pass


class NativeBindingError(TaskAuthorityError):
    """Raised when native binding evidence is missing, ambiguous, stale, or protected."""
    pass


class VerificationError(TaskAuthorityError):
    """Raised when review, acceptance, or pinned evidence verification fails."""
    pass


# --- Timestamp and Strict Validation Helpers ---

def parse_strict_iso_utc(ts: str) -> datetime.datetime:
    """Parse an ISO8601 string into a UTC datetime. Strictly rejects timezone-naive strings."""
    if not isinstance(ts, str):
        raise InvalidTupleError(f"Timestamp must be a string, got {type(ts).__name__}")
    clean = ts.strip()
    if not clean:
        raise InvalidTupleError("Timestamp cannot be empty")
    has_tz = clean.endswith("Z") or "+" in clean or ("-" in clean[10:] and ":" in clean[10:])
    if not has_tz:
        raise InvalidTupleError(f"Timestamp '{ts}' is timezone-naive; explicit timezone offset is required")
    if clean.endswith("Z"):
        clean = clean[:-1] + "+00:00"
    dt = datetime.datetime.fromisoformat(clean)
    if dt.tzinfo is None:
        raise InvalidTupleError(f"Timestamp '{ts}' parsed as timezone-naive")
    return dt.astimezone(datetime.timezone.utc)


def format_iso_utc(dt: datetime.datetime) -> str:
    """Format UTC datetime into ISO8601 string."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(datetime.timezone.utc).isoformat()


def _is_valid_int(val: Any) -> bool:
    """Strictly an integer, not a boolean."""
    return type(val) is int and not isinstance(val, bool)


def compute_sha256(data: Union[str, bytes, dict, list]) -> str:
    """Compute deterministic SHA-256 digest."""
    if isinstance(data, (dict, list)):
        payload = json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")
    elif isinstance(data, str):
        payload = data.encode("utf-8")
    else:
        payload = data
    return hashlib.sha256(payload).hexdigest()


# --- Policy & Trust Root ---

VALID_ROLES = frozenset({"issuer", "owner", "reviewer", "acceptor", "reader"})


@dataclasses.dataclass(frozen=True)
class TrustedActorPolicy:
    """Immutable explicit permission rule rooted outside requests."""
    actor_id: str
    role: str
    project_id: str
    workspace: str
    task_id: Optional[str] = None
    is_principal: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.actor_id, str) or not self.actor_id.strip():
            raise InvalidTupleError("actor_id must be a non-empty string")
        if self.role not in VALID_ROLES:
            raise InvalidTupleError(f"Invalid role '{self.role}'; expected one of {sorted(VALID_ROLES)}")
        if not isinstance(self.project_id, str) or not self.project_id.strip():
            raise InvalidTupleError("project_id must be a non-empty string")
        if not isinstance(self.workspace, str) or not self.workspace.strip():
            raise InvalidTupleError("workspace must be a non-empty string")


@dataclasses.dataclass(frozen=True)
class AuthorityTrustRoot:
    """Explicit trust root configuration. Default empty DENIES all semantic rights."""
    trusted_actors: Tuple[TrustedActorPolicy, ...] = ()

    def is_authorized(
        self,
        actor_id: str,
        role: str,
        project_id: str,
        workspace: str,
        task_id: Optional[str] = None,
    ) -> bool:
        if not actor_id or not role:
            return False
        for policy in self.trusted_actors:
            if policy.actor_id != actor_id:
                continue
            if policy.role != role:
                continue
            if policy.project_id != project_id:
                continue
            if policy.workspace != workspace:
                continue
            if policy.task_id is not None and task_id is not None and policy.task_id != task_id:
                continue
            return True
        return False

    def is_principal(self, actor_id: str, project_id: str) -> bool:
        for policy in self.trusted_actors:
            if policy.actor_id == actor_id and policy.project_id == project_id and policy.is_principal:
                return True
        return False


# --- Task Ownership Tuple & Records ---

@dataclasses.dataclass(frozen=True)
class TaskOwnershipTuple:
    """Exact 8-tuple uniquely identifying current task ownership and generation."""
    task_id: str
    revision: int
    project_id: str
    canonical_workspace: str
    owner_bus_actor: str
    native_owner_tag: str
    native_session_uuid: str
    generation: int

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, str) or not self.task_id.strip():
            raise InvalidTupleError("task_id must be a non-empty string")
        if not _is_valid_int(self.revision) or self.revision < 1:
            raise InvalidTupleError(f"revision must be an integer >= 1; got {self.revision!r}")
        if not isinstance(self.project_id, str) or not self.project_id.strip():
            raise InvalidTupleError("project_id must be a non-empty string")
        if not isinstance(self.canonical_workspace, str) or not self.canonical_workspace.strip():
            raise InvalidTupleError("canonical_workspace must be a non-empty string")
        if not isinstance(self.owner_bus_actor, str) or not self.owner_bus_actor.strip():
            raise InvalidTupleError("owner_bus_actor must be a non-empty string")
        if not isinstance(self.native_owner_tag, str) or not self.native_owner_tag.strip():
            raise InvalidTupleError("native_owner_tag must be a non-empty string")
        if not isinstance(self.native_session_uuid, str) or not self.native_session_uuid.strip():
            raise InvalidTupleError("native_session_uuid must be a non-empty string")
        if not _is_valid_int(self.generation) or self.generation < 0:
            raise InvalidTupleError(f"generation must be an integer >= 0; got {self.generation!r}")

    def digest(self) -> str:
        """Canonical JSON digest for exact ordered 8-tuple with version marker."""
        return compute_sha256([
            "task_ownership_tuple_v1",
            self.task_id,
            self.revision,
            self.project_id,
            self.canonical_workspace,
            self.owner_bus_actor,
            self.native_owner_tag,
            self.native_session_uuid,
            self.generation,
        ])


@dataclasses.dataclass(frozen=True)
class TaskGrantRecord:
    """Durable task grant record stored in authority ledger."""
    ownership: TaskOwnershipTuple
    issuer_actor_id: str
    issued_at: str
    status: str
    source_id: str
    source_digest: str
    has_owner_ack: bool = False
    expires_at: Optional[str] = None
    checkpoint_due: Optional[str] = None
    protected_reason: Optional[str] = None
    dependencies: Tuple[str, ...] = ()
    is_principal: bool = False
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    @property
    def task_id(self) -> str:
        return self.ownership.task_id

    @property
    def revision(self) -> int:
        return self.ownership.revision

    @property
    def project_id(self) -> str:
        return self.ownership.project_id

    @property
    def workspace(self) -> str:
        return self.ownership.canonical_workspace

    @property
    def owner_bus_actor(self) -> str:
        return self.ownership.owner_bus_actor

    @property
    def native_owner_tag(self) -> str:
        return self.ownership.native_owner_tag

    @property
    def native_session_uuid(self) -> str:
        return self.ownership.native_session_uuid

    @property
    def generation(self) -> int:
        return self.ownership.generation


@dataclasses.dataclass(frozen=True)
class CustodyAckRecord:
    task_id: str
    revision: int
    owner_actor_id: str
    idempotency_key: str
    tuple_digest: str
    acked_at: str


@dataclasses.dataclass(frozen=True)
class CompletionProposalRecord:
    task_id: str
    revision: int
    owner_actor_id: str
    result_digest: str
    evidence_digest: str
    idempotency_key: str
    proposed_at: str
    result_evidence: Dict[str, Any] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass(frozen=True)
class TaskReviewRecord:
    task_id: str
    revision: int
    reviewer_actor_id: str
    expected_result_digest: str
    verdict: str
    review_evidence_digest: str
    idempotency_key: str
    reviewed_at: str
    review_evidence: Dict[str, Any] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass(frozen=True)
class TaskAcceptanceRecord:
    task_id: str
    revision: int
    acceptor_actor_id: str
    expected_result_digest: str
    acceptance_evidence_digest: str
    idempotency_key: str
    accepted_at: str
    acceptance_evidence: Dict[str, Any] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass(frozen=True)
class CurrentGrantBoundaryResult:
    """Structured result of current authority boundary validation."""
    grant: Optional[TaskGrantRecord]
    is_valid: bool
    is_ready: bool
    effective_status: str
    effective_protected_reason: Optional[str]
    error_type: Optional[type]
    error_message: Optional[str]
    has_owner_ack: bool
    native_evidence: Optional[NativeSessionEvidence] = None


# --- Native Session Evidence Component ---

@dataclasses.dataclass(frozen=True)
class NativeSessionEvidence:
    """Read-only evidence from native session binding lookup."""
    workspace: str
    tag: str
    session_id: str
    generation: int
    is_alive: bool
    is_ready: bool = True
    is_frozen: bool = False
    is_blocked: bool = False
    is_quota_held: bool = False
    is_manual_approval: bool = False
    is_draft: bool = False
    is_menu_active: bool = False
    is_saved_goal: bool = False
    observed_at: Optional[str] = None
    is_fixture_only: bool = False

    def __post_init__(self) -> None:
        if not _is_valid_int(self.generation) or self.generation < 0:
            raise InvalidTupleError("NativeSessionEvidence generation must be an integer >= 0")
        if type(self.is_alive) is not bool:
            raise InvalidTupleError("NativeSessionEvidence is_alive must be a boolean")
        if type(self.is_ready) is not bool:
            raise InvalidTupleError("NativeSessionEvidence is_ready must be a boolean")


class NativeBindingProviderProtocol(Protocol):
    """Protocol for read-only native session binding lookup."""
    def get_binding_evidence(
        self,
        workspace: str,
        tag: str,
        now_utc: datetime.datetime,
    ) -> Optional[NativeSessionEvidence]: ...


# --- Authenticator Interface & Canonical FileBus Adapter ---

class BusAuthenticatorProtocol(Protocol):
    """Protocol for authenticating caller against canonical FileBus store."""
    def authenticate(self, identity_id: str, token: str) -> Dict[str, Any]: ...
    def is_identity_active(self, identity_id: str, expected_project_id: str) -> bool: ...


class FileBusAuthenticator:
    """Canonical store adapter using actual FileBus auth under FileLock. Fails closed if unavailable."""

    def __init__(self, bus: Optional[Any] = None, bus_store: Optional[Union[str, Path]] = None):
        self._bus = bus
        self._bus_store = Path(bus_store) if bus_store else None

    def _get_bus(self) -> Any:
        if self._bus is not None:
            return self._bus
        if self._bus_store is None:
            raise AuthenticationError("No FileBus store or instance configured")
        try:
            from coordination.bus import FileBus
            self._bus = FileBus(self._bus_store)
            return self._bus
        except Exception as e:
            raise AuthenticationError(f"Cannot initialize canonical FileBus: {e}")

    @contextlib.contextmanager
    def _lock_context(self, bus: Any):
        lock_path = getattr(bus, "_lock", None)
        if lock_path is None:
            raise AuthenticationError("FileLock unavailable: bus has no _lock attribute")
        try:
            from coordination.bus import FileLock
            lock = FileLock(lock_path)
            with lock:
                yield
        except Exception as e:
            if isinstance(e, AuthenticationError):
                raise
            raise AuthenticationError(f"FileLock unavailable: {e}")

    def authenticate(self, identity_id: str, token: str) -> Dict[str, Any]:
        if not isinstance(identity_id, str) or not identity_id.strip():
            raise AuthenticationError("identity_id must be a non-empty string")
        if not isinstance(token, str) or not token.strip():
            raise AuthenticationError("token must be a non-empty string")

        bus = self._get_bus()
        with self._lock_context(bus):
            try:
                ident = bus._auth(identity_id, token)
            except Exception as e:
                raise AuthenticationError(f"FileBus authentication failed: {e}")

            if not isinstance(ident, dict):
                raise AuthenticationError("FileBus._auth returned unexpected identity type")
            canonical_id = ident.get("identity_id")
            if canonical_id != identity_id:
                raise AuthenticationError(f"Canonical identity mismatch: '{canonical_id}' != '{identity_id}'")
            proj = ident.get("project_id")
            if not isinstance(proj, str) or not proj.strip():
                raise AuthenticationError("Canonical identity project_id missing or invalid")
            return ident

    def is_identity_active(self, identity_id: str, expected_project_id: str) -> bool:
        """Check if identity exists in FileBus identities store, token is present, and project matches (without credentials)."""
        if not isinstance(identity_id, str) or not identity_id.strip():
            return False
        if not isinstance(expected_project_id, str) or not expected_project_id.strip():
            return False
        try:
            bus = self._get_bus()
            with self._lock_context(bus):
                identities_path = getattr(bus, "_identities", None)
                tokens_path = getattr(bus, "_tokens", None)
                if identities_path and tokens_path and hasattr(bus, "_read"):
                    identities = bus._read(identities_path, {})
                    tokens = bus._read(tokens_path, {})
                else:
                    root = getattr(bus, "root", None) or self._bus_store
                    if not root:
                        return False
                    id_p = Path(root) / "identities.json"
                    tok_p = Path(root) / "tokens.json"
                    if not id_p.exists() or not tok_p.exists():
                        return False
                    identities = json.loads(id_p.read_text(encoding="utf-8"))
                    tokens = json.loads(tok_p.read_text(encoding="utf-8"))

                ident = identities.get(identity_id)
                if not isinstance(ident, dict):
                    return False
                if ident.get("identity_id") != identity_id:
                    return False
                if ident.get("project_id") != expected_project_id:
                    return False

                current_token = tokens.get(identity_id)
                if not isinstance(current_token, str) or not current_token.strip():
                    return False

                return True
        except Exception:
            return False


# --- Durable SQLite Task Authority Store ---

class TaskAuthorityStore:
    """Transactional SQLite storage for grants, custody, proposals, reviews, acceptances, and revocations."""

    def __init__(self, db_path: Union[str, Path]):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA busy_timeout = 30000;")
        conn.execute("PRAGMA foreign_keys = ON;")
        return conn

    def _init_db(self) -> None:
        with self._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE;")
            conn.execute("""
            CREATE TABLE IF NOT EXISTS task_grants (
                task_id TEXT NOT NULL,
                revision INTEGER NOT NULL,
                project_id TEXT NOT NULL,
                canonical_workspace TEXT NOT NULL,
                owner_bus_actor TEXT NOT NULL,
                native_owner_tag TEXT NOT NULL,
                native_session_uuid TEXT NOT NULL,
                generation INTEGER NOT NULL,
                issuer_actor_id TEXT NOT NULL,
                issued_at TEXT NOT NULL,
                status TEXT NOT NULL,
                source_id TEXT NOT NULL,
                source_digest TEXT NOT NULL,
                has_owner_ack INTEGER NOT NULL DEFAULT 0,
                expires_at TEXT,
                checkpoint_due TEXT,
                protected_reason TEXT,
                dependencies_json TEXT NOT NULL DEFAULT '[]',
                is_principal INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (task_id, revision)
            );
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_task_grants_task_rev ON task_grants(task_id, revision DESC);")
            conn.execute("""
            CREATE TABLE IF NOT EXISTS custody_acks (
                task_id TEXT NOT NULL,
                revision INTEGER NOT NULL,
                owner_actor_id TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                tuple_digest TEXT NOT NULL,
                acked_at TEXT NOT NULL,
                PRIMARY KEY (task_id, revision),
                UNIQUE (idempotency_key)
            );
            """)
            conn.execute("""
            CREATE TABLE IF NOT EXISTS completion_proposals (
                task_id TEXT NOT NULL,
                revision INTEGER NOT NULL,
                owner_actor_id TEXT NOT NULL,
                result_digest TEXT NOT NULL,
                evidence_digest TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                proposed_at TEXT NOT NULL,
                result_evidence_json TEXT NOT NULL,
                PRIMARY KEY (task_id, revision),
                UNIQUE (idempotency_key)
            );
            """)
            conn.execute("""
            CREATE TABLE IF NOT EXISTS task_reviews (
                task_id TEXT NOT NULL,
                revision INTEGER NOT NULL,
                reviewer_actor_id TEXT NOT NULL,
                expected_result_digest TEXT NOT NULL,
                verdict TEXT NOT NULL,
                review_evidence_digest TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                reviewed_at TEXT NOT NULL,
                review_evidence_json TEXT NOT NULL,
                PRIMARY KEY (task_id, revision),
                UNIQUE (idempotency_key)
            );
            """)
            conn.execute("""
            CREATE TABLE IF NOT EXISTS task_acceptances (
                task_id TEXT NOT NULL,
                revision INTEGER NOT NULL,
                acceptor_actor_id TEXT NOT NULL,
                expected_result_digest TEXT NOT NULL,
                acceptance_evidence_digest TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                accepted_at TEXT NOT NULL,
                acceptance_evidence_json TEXT NOT NULL,
                PRIMARY KEY (task_id, revision),
                UNIQUE (idempotency_key)
            );
            """)
            conn.execute("""
            CREATE TABLE IF NOT EXISTS task_revocations (
                task_id TEXT NOT NULL,
                revision INTEGER NOT NULL,
                issuer_actor_id TEXT NOT NULL,
                reason TEXT NOT NULL,
                revoked_at TEXT NOT NULL,
                PRIMARY KEY (task_id, revision)
            );
            """)
            conn.execute("COMMIT;")

    def get_latest_grant(self, task_id: str) -> Optional[TaskGrantRecord]:
        with self._get_connection() as conn:
            cur = conn.execute(
                "SELECT * FROM task_grants WHERE task_id = ? ORDER BY revision DESC LIMIT 1;",
                (task_id,),
            )
            row = cur.fetchone()
            return self._row_to_grant(row) if row else None

    def get_grant_at_revision(self, task_id: str, revision: int) -> Optional[TaskGrantRecord]:
        with self._get_connection() as conn:
            cur = conn.execute(
                "SELECT * FROM task_grants WHERE task_id = ? AND revision = ?;",
                (task_id, revision),
            )
            row = cur.fetchone()
            return self._row_to_grant(row) if row else None

    def _row_to_grant(self, row: sqlite3.Row) -> TaskGrantRecord:
        deps = tuple(json.loads(row["dependencies_json"]))
        ownership = TaskOwnershipTuple(
            task_id=row["task_id"],
            revision=row["revision"],
            project_id=row["project_id"],
            canonical_workspace=row["canonical_workspace"],
            owner_bus_actor=row["owner_bus_actor"],
            native_owner_tag=row["native_owner_tag"],
            native_session_uuid=row["native_session_uuid"],
            generation=row["generation"],
        )
        return TaskGrantRecord(
            ownership=ownership,
            issuer_actor_id=row["issuer_actor_id"],
            issued_at=row["issued_at"],
            status=row["status"],
            source_id=row["source_id"],
            source_digest=row["source_digest"],
            has_owner_ack=bool(row["has_owner_ack"]),
            expires_at=row["expires_at"],
            checkpoint_due=row["checkpoint_due"],
            protected_reason=row["protected_reason"],
            dependencies=deps,
            is_principal=bool(row["is_principal"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def get_custody_ack(self, task_id: str, revision: int) -> Optional[CustodyAckRecord]:
        with self._get_connection() as conn:
            cur = conn.execute("SELECT * FROM custody_acks WHERE task_id = ? AND revision = ?;", (task_id, revision))
            row = cur.fetchone()
            if not row:
                return None
            return CustodyAckRecord(
                task_id=row["task_id"], revision=row["revision"], owner_actor_id=row["owner_actor_id"],
                idempotency_key=row["idempotency_key"], tuple_digest=row["tuple_digest"], acked_at=row["acked_at"]
            )

    def get_proposal(self, task_id: str, revision: int) -> Optional[CompletionProposalRecord]:
        with self._get_connection() as conn:
            cur = conn.execute("SELECT * FROM completion_proposals WHERE task_id = ? AND revision = ?;", (task_id, revision))
            row = cur.fetchone()
            if not row:
                return None
            return CompletionProposalRecord(
                task_id=row["task_id"], revision=row["revision"], owner_actor_id=row["owner_actor_id"],
                result_digest=row["result_digest"], evidence_digest=row["evidence_digest"],
                idempotency_key=row["idempotency_key"], proposed_at=row["proposed_at"],
                result_evidence=json.loads(row["result_evidence_json"]),
            )

    def get_review(self, task_id: str, revision: int) -> Optional[TaskReviewRecord]:
        with self._get_connection() as conn:
            cur = conn.execute("SELECT * FROM task_reviews WHERE task_id = ? AND revision = ?;", (task_id, revision))
            row = cur.fetchone()
            if not row:
                return None
            return TaskReviewRecord(
                task_id=row["task_id"], revision=row["revision"], reviewer_actor_id=row["reviewer_actor_id"],
                expected_result_digest=row["expected_result_digest"], verdict=row["verdict"],
                review_evidence_digest=row["review_evidence_digest"], idempotency_key=row["idempotency_key"],
                reviewed_at=row["reviewed_at"], review_evidence=json.loads(row["review_evidence_json"]),
            )

    def get_acceptance(self, task_id: str, revision: int) -> Optional[TaskAcceptanceRecord]:
        with self._get_connection() as conn:
            cur = conn.execute("SELECT * FROM task_acceptances WHERE task_id = ? AND revision = ?;", (task_id, revision))
            row = cur.fetchone()
            if not row:
                return None
            return TaskAcceptanceRecord(
                task_id=row["task_id"], revision=row["revision"], acceptor_actor_id=row["acceptor_actor_id"],
                expected_result_digest=row["expected_result_digest"], acceptance_evidence_digest=row["acceptance_evidence_digest"],
                idempotency_key=row["idempotency_key"], accepted_at=row["accepted_at"],
                acceptance_evidence=json.loads(row["acceptance_evidence_json"]),
            )


# --- Task Authority Provider ---

class TaskAuthorityProvider:
    """Standalone stdlib task authority provider.

    Enforces exact tuple ownership, FileBus authentication under lock, immutable trust root authorization,
    read-only native binding verification, owner custody ACKs, pinned proposals, independent reviews,
    authorized acceptances, and dependency verification.
    """

    def __init__(
        self,
        store: TaskAuthorityStore,
        authenticator: BusAuthenticatorProtocol,
        trust_root: AuthorityTrustRoot,
        native_binding_provider: Optional[NativeBindingProviderProtocol] = None,
        clock: Optional[Callable[[], datetime.datetime]] = None,
    ):
        self.store = store
        self.authenticator = authenticator
        self.trust_root = trust_root
        self.native_binding_provider = native_binding_provider
        self._clock = clock or (lambda: datetime.datetime.now(datetime.timezone.utc))

    def _now(self) -> datetime.datetime:
        dt = self._clock()
        if dt.tzinfo is None:
            raise InvalidTupleError("Provider clock returned timezone-naive datetime")
        return dt.astimezone(datetime.timezone.utc)

    # --- Verification & Binding Helpers ---

    def _verify_caller(
        self,
        actor_id: str,
        token: str,
        role: str,
        project_id: str,
        workspace: str,
        task_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Authenticate caller identity/token and verify explicit immutable permission."""
        ident = self.authenticator.authenticate(actor_id, token)
        if ident.get("project_id") != project_id:
            raise PermissionDeniedError(
                f"Project scope mismatch: caller '{actor_id}' project '{ident.get('project_id')}' != requested '{project_id}'"
            )
        if not self.trust_root.is_authorized(actor_id, role, project_id, workspace, task_id):
            raise PermissionDeniedError(
                f"Permission denied: actor '{actor_id}' is not authorized for role '{role}' on {project_id}:{workspace}:{task_id}"
            )
        return ident

    def _check_native_binding(
        self,
        workspace: str,
        native_owner_tag: str,
        expected_session_uuid: str,
        expected_generation: int,
        now: datetime.datetime,
    ) -> NativeSessionEvidence:
        """Check read-only native binding evidence. Fails closed on any ambiguity or protection."""
        if self.native_binding_provider is None:
            raise NativeBindingError("Native binding provider is not configured")

        evidence = self.native_binding_provider.get_binding_evidence(
            workspace=workspace,
            tag=native_owner_tag,
            now_utc=now,
        )
        if evidence is None:
            raise NativeBindingError(
                f"Native session binding missing for tag '{native_owner_tag}' in workspace '{workspace}'"
            )

        if evidence.workspace != workspace:
            raise NativeBindingError(
                f"Native evidence workspace mismatch: expected '{workspace}', got '{evidence.workspace}'"
            )
        if evidence.tag != native_owner_tag:
            raise NativeBindingError(
                f"Native evidence tag mismatch: expected '{native_owner_tag}', got '{evidence.tag}'"
            )

        if evidence.session_id != expected_session_uuid:
            raise NativeBindingError(
                f"Native session UUID mismatch: expected '{expected_session_uuid}', got '{evidence.session_id}'"
            )
        if evidence.generation != expected_generation:
            raise NativeBindingError(
                f"Native generation mismatch: expected {expected_generation}, got {evidence.generation}"
            )

        if type(evidence.is_alive) is not bool or evidence.is_alive is not True:
            raise NativeBindingError(f"Native session '{expected_session_uuid}' is not alive")
        if type(evidence.is_ready) is not bool or evidence.is_ready is not True:
            raise NativeBindingError(f"Native session '{expected_session_uuid}' is not ready")

        # Strict boolean checks for all protected states
        for flag_name in (
            "is_frozen", "is_blocked", "is_quota_held", "is_manual_approval",
            "is_draft", "is_menu_active", "is_saved_goal"
        ):
            val = getattr(evidence, flag_name, None)
            if type(val) is not bool or val is not False:
                raise NativeBindingError(f"Native session protected state active: {flag_name}={val}")

        # Mandatory observed_at, freshness, and future check
        if not isinstance(evidence.observed_at, str) or not evidence.observed_at.strip():
            raise NativeBindingError("Native binding observation timestamp is mandatory")
        obs_dt = parse_strict_iso_utc(evidence.observed_at)
        diff = (now - obs_dt).total_seconds()
        if diff < -5.0:
            raise NativeBindingError(f"Native binding observation timestamp is in the future: {evidence.observed_at}")
        if diff > 300.0:
            raise NativeBindingError(f"Native binding observation timestamp is stale: {evidence.observed_at}")

        return evidence

    def _validate_current_grant_boundary(
        self,
        task_id: str,
        *,
        now: datetime.datetime,
        expected_revision: Optional[int] = None,
        expected_workspace: Optional[str] = None,
        expected_tuple: Optional[TaskOwnershipTuple] = None,
        conn: Optional[sqlite3.Connection] = None,
        require_custody_ack: bool = False,
        raise_on_error: bool = False,
    ) -> CurrentGrantBoundaryResult:
        """Shared current-grant boundary validation reused by every trusted resolver,
        revalidator, query, mutation, and dependency receipt.

        Enforces:
        - Latest current revision only (rejects historic / mismatched revisions).
        - Exact requested tuple and workspace against current.
        - Revocation and strict UTC expiration fencing.
        - Canonical issuer AND owner active identity & token presence in FileBus under FileLock.
        - Explicit semantic task/workspace policy rooted outside request (default empty deny).
        - Mandatory fresh native exact workspace/tag/session/gen with explicit known protection booleans
          (no optional native skip).
        - Genuine custody record owner and tuple digest binding.
        - Zero DB side effects on any negative.
        """
        def _fail(
            err_cls: type,
            msg: str,
            status: str = "blocked",
            grant: Optional[TaskGrantRecord] = None,
        ) -> CurrentGrantBoundaryResult:
            if raise_on_error:
                raise err_cls(msg)
            return CurrentGrantBoundaryResult(
                grant=grant,
                is_valid=False,
                is_ready=False,
                effective_status=status,
                effective_protected_reason=msg,
                error_type=err_cls,
                error_message=msg,
                has_owner_ack=False,
                native_evidence=None,
            )

        if now.tzinfo is None:
            return _fail(InvalidTupleError, "Current timestamp must be timezone-aware UTC")

        # 1. Fetch latest grant
        if conn is not None:
            cur = conn.execute("SELECT * FROM task_grants WHERE task_id = ? ORDER BY revision DESC LIMIT 1;", (task_id,))
            row = cur.fetchone()
            latest_grant = self.store._row_to_grant(row) if row else None
        else:
            latest_grant = self.store.get_latest_grant(task_id)

        if latest_grant is None:
            return _fail(FencedStateError, f"Task '{task_id}' does not exist in task authority store")

        # 2. Sequential revision CAS and historic rejection
        if expected_revision is not None and latest_grant.revision != expected_revision:
            return _fail(
                CASConflictError,
                f"Fenced: task '{task_id}' current rev is {latest_grant.revision}, expected {expected_revision}",
                grant=latest_grant,
            )

        # 4. Workspace check
        if expected_workspace is not None and latest_grant.workspace != expected_workspace:
            return _fail(
                PermissionDeniedError,
                f"Workspace mismatch: task '{task_id}' workspace is '{latest_grant.workspace}', expected '{expected_workspace}'",
                grant=latest_grant,
            )

        # 5. Revocation check
        if latest_grant.status == "revoked":
            return _fail(
                FencedStateError,
                f"Task '{task_id}' is revoked: {latest_grant.protected_reason or 'issuer revoked'}",
                status="revoked",
                grant=latest_grant,
            )

        # 6. Expiry check
        if latest_grant.expires_at is not None:
            try:
                exp_dt = parse_strict_iso_utc(latest_grant.expires_at)
            except Exception as e:
                return _fail(InvalidTupleError, f"Task '{task_id}' has invalid expires_at: {e}", grant=latest_grant)
            if exp_dt <= now:
                return _fail(FencedStateError, f"Task '{task_id}' is expired ({latest_grant.expires_at})", grant=latest_grant)

        # 7. Issuer active identity and token presence in FileBus under FileLock
        if not self.authenticator.is_identity_active(latest_grant.issuer_actor_id, latest_grant.project_id):
            return _fail(
                PermissionDeniedError,
                f"Issuer '{latest_grant.issuer_actor_id}' is inactive, token removed, or project mismatch in FileBus",
                grant=latest_grant,
            )

        # 8. Issuer authorization in trust root
        if not self.trust_root.is_authorized(
            latest_grant.issuer_actor_id, "issuer", latest_grant.project_id, latest_grant.workspace, latest_grant.task_id
        ):
            return _fail(
                PermissionDeniedError,
                f"Issuer '{latest_grant.issuer_actor_id}' is not authorized for role 'issuer' in trust root",
                grant=latest_grant,
            )

        # 9. Owner active identity and token presence in FileBus under FileLock
        if not self.authenticator.is_identity_active(latest_grant.owner_bus_actor, latest_grant.project_id):
            return _fail(
                PermissionDeniedError,
                f"Owner '{latest_grant.owner_bus_actor}' is inactive, token removed, or project mismatch in FileBus",
                grant=latest_grant,
            )

        # 10. Owner authorization in trust root
        if not self.trust_root.is_authorized(
            latest_grant.owner_bus_actor, "owner", latest_grant.project_id, latest_grant.workspace, latest_grant.task_id
        ):
            return _fail(
                PermissionDeniedError,
                f"Owner '{latest_grant.owner_bus_actor}' is not authorized for role 'owner' in trust root",
                grant=latest_grant,
            )

        # 11. Mandatory native binding check (No optional native skip)
        target_ws = expected_tuple.canonical_workspace if expected_tuple is not None else (expected_workspace if expected_workspace is not None else latest_grant.workspace)
        target_tag = expected_tuple.native_owner_tag if expected_tuple is not None else latest_grant.native_owner_tag
        target_session = expected_tuple.native_session_uuid if expected_tuple is not None else latest_grant.native_session_uuid
        target_gen = expected_tuple.generation if expected_tuple is not None else latest_grant.generation
        try:
            evidence = self._check_native_binding(
                workspace=target_ws,
                native_owner_tag=target_tag,
                expected_session_uuid=target_session,
                expected_generation=target_gen,
                now=now,
            )
        except NativeBindingError as e:
            return _fail(NativeBindingError, str(e), grant=latest_grant)

        # 12. Tuple check against latest grant
        if expected_tuple is not None and latest_grant.ownership != expected_tuple:
            return _fail(
                CASConflictError,
                f"Ownership tuple mismatch for task '{task_id}'",
                grant=latest_grant,
            )

        # 12. Genuine Custody ACK and tuple digest verification
        if conn is not None:
            cur_ack = conn.execute("SELECT * FROM custody_acks WHERE task_id = ? AND revision = ?;", (task_id, latest_grant.revision))
            row_ack = cur_ack.fetchone()
            ack = CustodyAckRecord(
                task_id=row_ack["task_id"], revision=row_ack["revision"], owner_actor_id=row_ack["owner_actor_id"],
                idempotency_key=row_ack["idempotency_key"], tuple_digest=row_ack["tuple_digest"], acked_at=row_ack["acked_at"]
            ) if row_ack else None
        else:
            ack = self.store.get_custody_ack(task_id, latest_grant.revision)

        has_owner_ack = False
        if ack is not None:
            if ack.owner_actor_id != latest_grant.owner_bus_actor or ack.tuple_digest != latest_grant.ownership.digest():
                return _fail(
                    CASConflictError,
                    f"Custody ACK tuple mismatch for task '{task_id}' rev {latest_grant.revision}",
                    grant=latest_grant,
                )
            has_owner_ack = True

        if require_custody_ack and not has_owner_ack:
            return _fail(
                FencedStateError,
                f"Task '{task_id}' has not received owner custody ACK",
                status="assigned",
                grant=latest_grant,
            )

        # 13. Determine effective status & readiness
        if not has_owner_ack:
            effective_status = "assigned"
            effective_protected_reason = latest_grant.protected_reason or "awaiting_owner_custody_ack"
            is_ready = False
        else:
            effective_status = latest_grant.status
            effective_protected_reason = latest_grant.protected_reason
            is_ready = (effective_status in ("ready", "in_progress") and effective_protected_reason is None)

        return CurrentGrantBoundaryResult(
            grant=latest_grant,
            is_valid=True,
            is_ready=is_ready,
            effective_status=effective_status,
            effective_protected_reason=effective_protected_reason,
            error_type=None,
            error_message=None,
            has_owner_ack=has_owner_ack,
            native_evidence=evidence,
        )

    def _lock_and_validate_current_grant(
        self,
        conn: sqlite3.Connection,
        task_id: str,
        revision: int,
        now: datetime.datetime,
    ) -> TaskGrantRecord:
        """Lock and validate current active grant record inside transaction."""
        res = self._validate_current_grant_boundary(
            task_id=task_id,
            now=now,
            expected_revision=revision,
            conn=conn,
            require_custody_ack=False,
            raise_on_error=True,
        )
        assert res.grant is not None
        return res.grant

    # --- Task Grant Issuance & Revisions ---

    def issue_task_grant(
        self,
        *,
        issuer_actor_id: str,
        issuer_token: str,
        task_id: str,
        revision: int,
        project_id: str,
        workspace: str,
        owner_bus_actor: str,
        native_owner_tag: str,
        native_session_uuid: str,
        generation: int,
        source_id: str,
        source_digest: str,
        expires_at: Optional[str] = None,
        checkpoint_due: Optional[str] = None,
        protected_reason: Optional[str] = None,
        dependencies: Sequence[str] = (),
        is_principal: bool = False,
        now_utc: Optional[datetime.datetime] = None,
    ) -> TaskGrantRecord:
        now = now_utc or self._now()
        now_str = format_iso_utc(now)

        self._verify_caller(
            actor_id=issuer_actor_id, token=issuer_token, role="issuer",
            project_id=project_id, workspace=workspace, task_id=task_id,
        )

        if not self.authenticator.is_identity_active(owner_bus_actor, project_id):
            raise PermissionDeniedError(
                f"Owner '{owner_bus_actor}' is inactive, token removed, or project mismatch in FileBus"
            )
        if not self.trust_root.is_authorized(owner_bus_actor, "owner", project_id, workspace, task_id):
            raise PermissionDeniedError(
                f"Permission denied: owner '{owner_bus_actor}' is not authorized for role 'owner' on {project_id}:{workspace}:{task_id}"
            )

        tuple_obj = TaskOwnershipTuple(
            task_id=task_id, revision=revision, project_id=project_id,
            canonical_workspace=workspace, owner_bus_actor=owner_bus_actor,
            native_owner_tag=native_owner_tag, native_session_uuid=native_session_uuid,
            generation=generation,
        )

        if not isinstance(source_id, str) or not source_id.strip():
            raise InvalidTupleError("source_id must be a non-empty string")
        if not isinstance(source_digest, str) or not source_digest.strip():
            raise InvalidTupleError("source_digest must be a non-empty string")

        if expires_at is not None:
            exp_dt = parse_strict_iso_utc(expires_at)
            if exp_dt <= now:
                raise InvalidTupleError(f"expires_at must be in the future (got {expires_at})")

        if checkpoint_due is not None:
            parse_strict_iso_utc(checkpoint_due)

        clean_deps = []
        for d in dependencies:
            if not isinstance(d, str) or not d.strip():
                raise InvalidTupleError(f"Dependency item must be a non-empty string; got {d!r}")
            clean_deps.append(d.strip())
        deps_tuple = tuple(clean_deps)

        self._check_native_binding(
            workspace=workspace,
            native_owner_tag=native_owner_tag,
            expected_session_uuid=native_session_uuid,
            expected_generation=generation,
            now=now,
        )

        with self.store._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE;")
            cur = conn.execute("SELECT * FROM task_grants WHERE task_id = ? ORDER BY revision DESC LIMIT 1;", (task_id,))
            latest = cur.fetchone()
            if latest is None:
                if revision != 1:
                    conn.execute("ROLLBACK;")
                    raise CASConflictError(f"Initial task revision must be 1; got {revision}")
                policy_is_principal = self.trust_root.is_principal(owner_bus_actor, project_id)
                if is_principal != policy_is_principal:
                    conn.execute("ROLLBACK;")
                    raise PermissionDeniedError(
                        f"Explicit policy principal flag cannot be changed via payload for actor '{owner_bus_actor}'"
                    )
            else:
                current_rev = latest["revision"]
                if revision == current_rev:
                    existing = self.store._row_to_grant(latest)
                    if (
                        existing.ownership == tuple_obj and existing.issuer_actor_id == issuer_actor_id
                        and existing.source_id == source_id and existing.source_digest == source_digest
                        and existing.dependencies == deps_tuple and existing.protected_reason == protected_reason
                        and existing.expires_at == expires_at and existing.checkpoint_due == checkpoint_due
                        and existing.is_principal == is_principal
                    ):
                        if latest["status"] == "revoked":
                            conn.execute("ROLLBACK;")
                            raise FencedStateError(f"Task '{task_id}' is revoked; further revisions forbidden")
                        policy_is_principal = self.trust_root.is_principal(owner_bus_actor, project_id)
                        if is_principal != policy_is_principal:
                            conn.execute("ROLLBACK;")
                            raise PermissionDeniedError(
                                f"Explicit policy principal flag cannot be changed via payload for actor '{owner_bus_actor}'"
                            )
                        conn.execute("ROLLBACK;")
                        return existing
                    conn.execute("ROLLBACK;")
                    raise CASConflictError(f"Changed payload conflict on task '{task_id}' revision {revision}")
                if revision != current_rev + 1:
                    conn.execute("ROLLBACK;")
                    raise CASConflictError(
                        f"Sequential CAS violation: task '{task_id}' current rev is {current_rev}, expected {current_rev + 1}; got {revision}"
                    )
                if latest["status"] == "revoked":
                    conn.execute("ROLLBACK;")
                    raise FencedStateError(f"Task '{task_id}' is revoked; further revisions forbidden")
                policy_is_principal = self.trust_root.is_principal(owner_bus_actor, project_id)
                if is_principal != policy_is_principal:
                    conn.execute("ROLLBACK;")
                    raise PermissionDeniedError(
                        f"Explicit policy principal flag cannot be changed via payload for actor '{owner_bus_actor}'"
                    )

            initial_status = "assigned"
            has_owner_ack = 0

            conn.execute(
                """
                INSERT INTO task_grants (
                    task_id, revision, project_id, canonical_workspace, owner_bus_actor,
                    native_owner_tag, native_session_uuid, generation, issuer_actor_id,
                    issued_at, status, source_id, source_digest, has_owner_ack,
                    expires_at, checkpoint_due, protected_reason, dependencies_json,
                    is_principal, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    task_id, revision, project_id, workspace, owner_bus_actor,
                    native_owner_tag, native_session_uuid, generation, issuer_actor_id,
                    now_str, initial_status, source_id, source_digest, has_owner_ack,
                    expires_at, checkpoint_due, protected_reason, json.dumps(list(deps_tuple)),
                    1 if is_principal else 0, now_str, now_str,
                ),
            )
            conn.execute("COMMIT;")

        return TaskGrantRecord(
            ownership=tuple_obj, issuer_actor_id=issuer_actor_id, issued_at=now_str,
            status=initial_status, source_id=source_id, source_digest=source_digest,
            has_owner_ack=False, expires_at=expires_at, checkpoint_due=checkpoint_due,
            protected_reason=protected_reason, dependencies=deps_tuple, is_principal=is_principal,
            created_at=now_str, updated_at=now_str,
        )

    # --- Owner Custody ACK ---

    def record_custody_ack(
        self,
        *,
        owner_actor_id: str,
        owner_token: str,
        task_id: str,
        revision: int,
        project_id: str,
        workspace: str,
        native_owner_tag: str,
        native_session_uuid: str,
        generation: int,
        idempotency_key: str,
        now_utc: Optional[datetime.datetime] = None,
    ) -> CustodyAckRecord:
        now = now_utc or self._now()
        now_str = format_iso_utc(now)

        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise InvalidTupleError("idempotency_key must be a non-empty string")

        self._verify_caller(
            actor_id=owner_actor_id, token=owner_token, role="owner",
            project_id=project_id, workspace=workspace, task_id=task_id,
        )

        tuple_obj = TaskOwnershipTuple(
            task_id=task_id, revision=revision, project_id=project_id,
            canonical_workspace=workspace, owner_bus_actor=owner_actor_id,
            native_owner_tag=native_owner_tag, native_session_uuid=native_session_uuid,
            generation=generation,
        )
        tuple_digest = tuple_obj.digest()

        with self.store._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE;")
            try:
                self._validate_current_grant_boundary(
                    task_id=task_id,
                    now=now,
                    expected_revision=revision,
                    expected_workspace=workspace,
                    expected_tuple=tuple_obj,
                    conn=conn,
                    require_custody_ack=False,
                    raise_on_error=True,
                )

                cur_ack = conn.execute("SELECT * FROM custody_acks WHERE task_id = ? AND revision = ?;", (task_id, revision))
                existing_ack = cur_ack.fetchone()
                if existing_ack is not None:
                    if (
                        existing_ack["owner_actor_id"] == owner_actor_id
                        and existing_ack["tuple_digest"] == tuple_digest
                        and existing_ack["idempotency_key"] == idempotency_key
                    ):
                        conn.execute("ROLLBACK;")
                        return CustodyAckRecord(
                            task_id=task_id, revision=revision, owner_actor_id=owner_actor_id,
                            idempotency_key=idempotency_key, tuple_digest=tuple_digest, acked_at=existing_ack["acked_at"],
                        )
                    conn.execute("ROLLBACK;")
                    raise CASConflictError(f"Custody ACK conflict for task '{task_id}' rev {revision}")

                conn.execute(
                    """
                    INSERT INTO custody_acks (task_id, revision, owner_actor_id, idempotency_key, tuple_digest, acked_at)
                    VALUES (?, ?, ?, ?, ?, ?);
                    """,
                    (task_id, revision, owner_actor_id, idempotency_key, tuple_digest, now_str),
                )
                conn.execute(
                    "UPDATE task_grants SET has_owner_ack = 1, status = 'ready', updated_at = ? WHERE task_id = ? AND revision = ?;",
                    (now_str, task_id, revision),
                )
                conn.execute("COMMIT;")
            except Exception:
                try:
                    conn.execute("ROLLBACK;")
                except sqlite3.OperationalError:
                    pass
                raise

        return CustodyAckRecord(
            task_id=task_id, revision=revision, owner_actor_id=owner_actor_id,
            idempotency_key=idempotency_key, tuple_digest=tuple_digest, acked_at=now_str,
        )

    # --- Revocation ---

    def revoke_task_grant(
        self,
        *,
        issuer_actor_id: str,
        issuer_token: str,
        task_id: str,
        revision: int,
        project_id: str,
        workspace: str,
        reason: str,
        now_utc: Optional[datetime.datetime] = None,
    ) -> None:
        now = now_utc or self._now()
        now_str = format_iso_utc(now)

        if not isinstance(reason, str) or not reason.strip():
            raise InvalidTupleError("Revocation reason must be a non-empty string")

        self._verify_caller(
            actor_id=issuer_actor_id, token=issuer_token, role="issuer",
            project_id=project_id, workspace=workspace, task_id=task_id,
        )

        with self.store._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE;")
            try:
                self._validate_current_grant_boundary(
                    task_id=task_id,
                    now=now,
                    expected_revision=revision,
                    expected_workspace=workspace,
                    conn=conn,
                    require_custody_ack=False,
                    raise_on_error=True,
                )

                conn.execute(
                    "INSERT OR REPLACE INTO task_revocations (task_id, revision, issuer_actor_id, reason, revoked_at) VALUES (?, ?, ?, ?, ?);",
                    (task_id, revision, issuer_actor_id, reason, now_str),
                )
                conn.execute(
                    "UPDATE task_grants SET status = 'revoked', protected_reason = ?, updated_at = ? WHERE task_id = ? AND revision = ?;",
                    (reason, now_str, task_id, revision),
                )
                conn.execute("COMMIT;")
            except Exception:
                try:
                    conn.execute("ROLLBACK;")
                except sqlite3.OperationalError:
                    pass
                raise

    # --- Completion Proposal ---

    def propose_task_completion(
        self,
        *,
        owner_actor_id: str,
        owner_token: str,
        task_id: str,
        revision: int,
        project_id: str,
        workspace: str,
        native_owner_tag: str,
        native_session_uuid: str,
        generation: int,
        result_digest: str,
        result_evidence: Dict[str, Any],
        idempotency_key: str,
        now_utc: Optional[datetime.datetime] = None,
    ) -> CompletionProposalRecord:
        now = now_utc or self._now()
        now_str = format_iso_utc(now)

        if not isinstance(result_digest, str) or len(result_digest) < 16:
            raise InvalidTupleError(f"result_digest must be a valid hex digest; got {result_digest!r}")
        if not isinstance(result_evidence, dict) or not result_evidence:
            raise InvalidTupleError("result_evidence must be a non-empty dict containing bound evidence")
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise InvalidTupleError("idempotency_key must be a non-empty string")

        evidence_digest = compute_sha256(result_evidence)

        self._verify_caller(
            actor_id=owner_actor_id, token=owner_token, role="owner",
            project_id=project_id, workspace=workspace, task_id=task_id,
        )

        tuple_obj = TaskOwnershipTuple(
            task_id=task_id, revision=revision, project_id=project_id,
            canonical_workspace=workspace, owner_bus_actor=owner_actor_id,
            native_owner_tag=native_owner_tag, native_session_uuid=native_session_uuid,
            generation=generation,
        )

        with self.store._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE;")
            try:
                self._validate_current_grant_boundary(
                    task_id=task_id,
                    now=now,
                    expected_revision=revision,
                    expected_workspace=workspace,
                    expected_tuple=tuple_obj,
                    conn=conn,
                    require_custody_ack=True,
                    raise_on_error=True,
                )

                cur_p = conn.execute("SELECT * FROM completion_proposals WHERE task_id = ? AND revision = ?;", (task_id, revision))
                existing_p = cur_p.fetchone()
                if existing_p is not None:
                    if (
                        existing_p["owner_actor_id"] == owner_actor_id
                        and existing_p["result_digest"] == result_digest
                        and existing_p["evidence_digest"] == evidence_digest
                        and existing_p["idempotency_key"] == idempotency_key
                    ):
                        conn.execute("ROLLBACK;")
                        return CompletionProposalRecord(
                            task_id=task_id, revision=revision, owner_actor_id=owner_actor_id,
                            result_digest=result_digest, evidence_digest=evidence_digest,
                            idempotency_key=idempotency_key, proposed_at=existing_p["proposed_at"],
                            result_evidence=json.loads(existing_p["result_evidence_json"]),
                        )
                    conn.execute("ROLLBACK;")
                    raise CASConflictError(f"Proposal payload conflict for task '{task_id}' rev {revision}")

                conn.execute(
                    """
                    INSERT INTO completion_proposals (
                        task_id, revision, owner_actor_id, result_digest,
                        evidence_digest, idempotency_key, proposed_at, result_evidence_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    (task_id, revision, owner_actor_id, result_digest, evidence_digest, idempotency_key, now_str, json.dumps(result_evidence)),
                )
                conn.execute(
                    "UPDATE task_grants SET status = 'completed_awaiting_review', updated_at = ? WHERE task_id = ? AND revision = ?;",
                    (now_str, task_id, revision),
                )
                conn.execute("COMMIT;")
            except Exception:
                try:
                    conn.execute("ROLLBACK;")
                except sqlite3.OperationalError:
                    pass
                raise

        return CompletionProposalRecord(
            task_id=task_id, revision=revision, owner_actor_id=owner_actor_id,
            result_digest=result_digest, evidence_digest=evidence_digest,
            idempotency_key=idempotency_key, proposed_at=now_str, result_evidence=result_evidence,
        )

    # --- Provenance Validation Helper ---

    def _validate_provenance(
        self,
        *,
        grant: TaskGrantRecord,
        conn: Optional[sqlite3.Connection] = None,
        expected_result_digest: Optional[str] = None,
        require_proposal: bool = True,
        require_review: bool = False,
        require_acceptance: bool = False,
        acceptor_actor_id: Optional[str] = None,
        raise_on_error: bool = True,
    ) -> Tuple[bool, str, Optional[CompletionProposalRecord], Optional[TaskReviewRecord], Optional[TaskAcceptanceRecord]]:
        """Validate provenance of proposal, review, and acceptance records.
        Recomputes evidence digests against stored JSON and verifies actor policies and result pins.
        Used across review, acceptance, and dependency verification.
        """
        def _err(exc_cls: type[TaskAuthorityError], msg: str):
            if raise_on_error:
                raise exc_cls(msg)
            return False, msg, None, None, None

        proposal_rec: Optional[CompletionProposalRecord] = None
        review_rec: Optional[TaskReviewRecord] = None
        acceptance_rec: Optional[TaskAcceptanceRecord] = None

        if require_proposal:
            if conn is not None:
                cur_p = conn.execute("SELECT * FROM completion_proposals WHERE task_id = ? AND revision = ?;", (grant.task_id, grant.revision))
                row_p = cur_p.fetchone()
                if row_p is None:
                    return _err(FencedStateError if not require_review else VerificationError, f"No completion proposal found for task '{grant.task_id}' rev {grant.revision}")
                try:
                    p_ev = json.loads(row_p["result_evidence_json"])
                except Exception as e:
                    return _err(VerificationError, f"Corrupted proposal evidence JSON: {e}")
                proposal_rec = CompletionProposalRecord(
                    task_id=row_p["task_id"], revision=row_p["revision"], owner_actor_id=row_p["owner_actor_id"],
                    result_digest=row_p["result_digest"], evidence_digest=row_p["evidence_digest"],
                    idempotency_key=row_p["idempotency_key"], proposed_at=row_p["proposed_at"],
                    result_evidence=p_ev,
                )
            else:
                proposal_rec = self.store.get_proposal(grant.task_id, grant.revision)
                if proposal_rec is None:
                    return _err(VerificationError, f"Dependency '{grant.task_id}' lacks completion proposal")

            if proposal_rec.owner_actor_id != grant.owner_bus_actor:
                return _err(VerificationError, f"Dependency '{grant.task_id}' proposal owner mismatch: {proposal_rec.owner_actor_id} != {grant.owner_bus_actor}")

            if expected_result_digest is not None and proposal_rec.result_digest != expected_result_digest:
                return _err(VerificationError, f"Pinned artifact tamper/conflict: proposal digest '{proposal_rec.result_digest}' != expected '{expected_result_digest}'")

            computed_p_ev_hash = compute_sha256(proposal_rec.result_evidence)
            if proposal_rec.evidence_digest != computed_p_ev_hash:
                return _err(VerificationError, f"Dependency '{grant.task_id}' proposal evidence digest mismatch")

        if require_review:
            if conn is not None:
                cur_r = conn.execute("SELECT * FROM task_reviews WHERE task_id = ? AND revision = ?;", (grant.task_id, grant.revision))
                row_r = cur_r.fetchone()
                if row_r is None:
                    return _err(VerificationError, f"Acceptance denied: task '{grant.task_id}' rev {grant.revision} lacks review record")
                try:
                    r_ev = json.loads(row_r["review_evidence_json"])
                except Exception as e:
                    return _err(VerificationError, f"Corrupted review evidence JSON: {e}")
                review_rec = TaskReviewRecord(
                    task_id=row_r["task_id"], revision=row_r["revision"], reviewer_actor_id=row_r["reviewer_actor_id"],
                    expected_result_digest=row_r["expected_result_digest"], verdict=row_r["verdict"],
                    review_evidence_digest=row_r["review_evidence_digest"], idempotency_key=row_r["idempotency_key"],
                    reviewed_at=row_r["reviewed_at"], review_evidence=r_ev,
                )
            else:
                review_rec = self.store.get_review(grant.task_id, grant.revision)
                if review_rec is None:
                    return _err(VerificationError, f"Dependency '{grant.task_id}' lacks independent review")

            if review_rec.verdict != "approved":
                return _err(VerificationError, f"Dependency '{grant.task_id}' review verdict is '{review_rec.verdict}', not 'approved'")

            if review_rec.reviewer_actor_id == grant.owner_bus_actor:
                return _err(PermissionDeniedError, f"Dependency '{grant.task_id}' review is invalid (self-review)")

            if proposal_rec is not None and review_rec.expected_result_digest != proposal_rec.result_digest:
                return _err(VerificationError, f"Dependency '{grant.task_id}' review digest mismatch with proposal")

            if expected_result_digest is not None and review_rec.expected_result_digest != expected_result_digest:
                return _err(VerificationError, "Acceptance denied: review digest mismatch with acceptance digest")

            computed_r_ev_hash = compute_sha256(review_rec.review_evidence)
            if review_rec.review_evidence_digest != computed_r_ev_hash:
                return _err(VerificationError, f"Dependency '{grant.task_id}' review evidence digest mismatch")

            if not self.authenticator.is_identity_active(review_rec.reviewer_actor_id, grant.project_id):
                return _err(PermissionDeniedError, f"Dependency '{grant.task_id}' has revoked or missing canonical reviewer identity: '{review_rec.reviewer_actor_id}'")

            if not self.trust_root.is_authorized(review_rec.reviewer_actor_id, "reviewer", grant.project_id, grant.workspace, grant.task_id):
                return _err(PermissionDeniedError, f"Dependency '{grant.task_id}' has unauthorized current reviewer policy: '{review_rec.reviewer_actor_id}'")

            if acceptor_actor_id is not None:
                if acceptor_actor_id == grant.owner_bus_actor:
                    return _err(PermissionDeniedError, f"Self-acceptance forbidden: acceptor '{acceptor_actor_id}' is the owner of task '{grant.task_id}'")
                if acceptor_actor_id == review_rec.reviewer_actor_id:
                    return _err(PermissionDeniedError, f"Distinct acceptor required: acceptor '{acceptor_actor_id}' cannot be the reviewer of task '{grant.task_id}'")

        if require_acceptance:
            if conn is not None:
                cur_a = conn.execute("SELECT * FROM task_acceptances WHERE task_id = ? AND revision = ?;", (grant.task_id, grant.revision))
                row_a = cur_a.fetchone()
                if row_a is None:
                    return _err(VerificationError, f"Dependency '{grant.task_id}' lacks authorized acceptance record")
                try:
                    a_ev = json.loads(row_a["acceptance_evidence_json"])
                except Exception as e:
                    return _err(VerificationError, f"Corrupted acceptance evidence JSON: {e}")
                acceptance_rec = TaskAcceptanceRecord(
                    task_id=row_a["task_id"], revision=row_a["revision"], acceptor_actor_id=row_a["acceptor_actor_id"],
                    expected_result_digest=row_a["expected_result_digest"], acceptance_evidence_digest=row_a["acceptance_evidence_digest"],
                    idempotency_key=row_a["idempotency_key"], accepted_at=row_a["accepted_at"],
                    acceptance_evidence=a_ev,
                )
            else:
                acceptance_rec = self.store.get_acceptance(grant.task_id, grant.revision)
                if acceptance_rec is None:
                    return _err(VerificationError, f"Dependency '{grant.task_id}' lacks authorized acceptance record")

            if proposal_rec is not None and acceptance_rec.expected_result_digest != proposal_rec.result_digest:
                return _err(VerificationError, f"Dependency '{grant.task_id}' acceptance digest mismatch with proposal")

            computed_a_ev_hash = compute_sha256(acceptance_rec.acceptance_evidence)
            if acceptance_rec.acceptance_evidence_digest != computed_a_ev_hash:
                return _err(VerificationError, f"Dependency '{grant.task_id}' acceptance evidence digest mismatch")

            if not self.authenticator.is_identity_active(acceptance_rec.acceptor_actor_id, grant.project_id):
                return _err(PermissionDeniedError, f"Dependency '{grant.task_id}' has revoked or missing canonical acceptor identity: '{acceptance_rec.acceptor_actor_id}'")

            if not self.trust_root.is_authorized(acceptance_rec.acceptor_actor_id, "acceptor", grant.project_id, grant.workspace, grant.task_id):
                return _err(PermissionDeniedError, f"Dependency '{grant.task_id}' has unauthorized current acceptor policy: '{acceptance_rec.acceptor_actor_id}'")

        return True, "ok", proposal_rec, review_rec, acceptance_rec

    # --- Independent Review ---

    def record_task_review(
        self,
        *,
        reviewer_actor_id: str,
        reviewer_token: str,
        task_id: str,
        revision: int,
        project_id: str,
        workspace: str,
        expected_result_digest: str,
        verdict: str,
        review_evidence: Dict[str, Any],
        idempotency_key: str,
        now_utc: Optional[datetime.datetime] = None,
    ) -> TaskReviewRecord:
        now = now_utc or self._now()
        now_str = format_iso_utc(now)

        if verdict not in ("approved", "rejected"):
            raise InvalidTupleError(f"Verdict must be 'approved' or 'rejected'; got {verdict!r}")
        if not isinstance(expected_result_digest, str) or not expected_result_digest:
            raise InvalidTupleError("expected_result_digest must be a non-empty string")
        if not isinstance(review_evidence, dict) or not review_evidence:
            raise InvalidTupleError("review_evidence must be a non-empty dict containing test/negative verification")
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise InvalidTupleError("idempotency_key must be a non-empty string")

        evidence_digest = compute_sha256(review_evidence)

        self._verify_caller(
            actor_id=reviewer_actor_id, token=reviewer_token, role="reviewer",
            project_id=project_id, workspace=workspace, task_id=task_id,
        )

        with self.store._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE;")
            try:
                boundary = self._validate_current_grant_boundary(
                    task_id=task_id,
                    now=now,
                    expected_revision=revision,
                    expected_workspace=workspace,
                    conn=conn,
                    require_custody_ack=True,
                    raise_on_error=True,
                )
                current_grant = boundary.grant
                assert current_grant is not None

                if reviewer_actor_id == current_grant.owner_bus_actor:
                    conn.execute("ROLLBACK;")
                    raise PermissionDeniedError(
                        f"Self-review forbidden: reviewer '{reviewer_actor_id}' is the author/owner of task '{task_id}'"
                    )

                self._validate_provenance(
                    grant=current_grant,
                    conn=conn,
                    expected_result_digest=expected_result_digest,
                    require_proposal=True,
                    require_review=False,
                    require_acceptance=False,
                    raise_on_error=True,
                )

                cur_r = conn.execute("SELECT * FROM task_reviews WHERE task_id = ? AND revision = ?;", (task_id, revision))
                existing_r = cur_r.fetchone()
                if existing_r is not None:
                    if (
                        existing_r["reviewer_actor_id"] == reviewer_actor_id
                        and existing_r["expected_result_digest"] == expected_result_digest
                        and existing_r["verdict"] == verdict
                        and existing_r["review_evidence_digest"] == evidence_digest
                        and existing_r["idempotency_key"] == idempotency_key
                    ):
                        try:
                            ex_ev = json.loads(existing_r["review_evidence_json"])
                        except Exception as e:
                            conn.execute("ROLLBACK;")
                            raise VerificationError(f"Corrupted review evidence JSON: {e}")
                        if existing_r["review_evidence_digest"] != compute_sha256(ex_ev):
                            conn.execute("ROLLBACK;")
                            raise VerificationError(
                                f"Review evidence digest mismatch for task '{task_id}' rev {revision}"
                            )
                        conn.execute("ROLLBACK;")
                        return TaskReviewRecord(
                            task_id=task_id, revision=revision, reviewer_actor_id=reviewer_actor_id,
                            expected_result_digest=expected_result_digest, verdict=verdict,
                            review_evidence_digest=evidence_digest, idempotency_key=idempotency_key,
                            reviewed_at=existing_r["reviewed_at"], review_evidence=ex_ev,
                        )
                    conn.execute("ROLLBACK;")
                    raise CASConflictError(f"Review conflict for task '{task_id}' rev {revision}")

                conn.execute(
                    """
                    INSERT INTO task_reviews (
                        task_id, revision, reviewer_actor_id, expected_result_digest,
                        verdict, review_evidence_digest, idempotency_key, reviewed_at, review_evidence_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    (
                        task_id, revision, reviewer_actor_id, expected_result_digest,
                        verdict, evidence_digest, idempotency_key, now_str, json.dumps(review_evidence),
                    ),
                )

                new_status = "review_approved" if verdict == "approved" else "review_rejected"
                conn.execute(
                    "UPDATE task_grants SET status = ?, updated_at = ? WHERE task_id = ? AND revision = ?;",
                    (new_status, now_str, task_id, revision),
                )
                conn.execute("COMMIT;")
            except Exception:
                try:
                    conn.execute("ROLLBACK;")
                except sqlite3.OperationalError:
                    pass
                raise

        return TaskReviewRecord(
            task_id=task_id, revision=revision, reviewer_actor_id=reviewer_actor_id,
            expected_result_digest=expected_result_digest, verdict=verdict,
            review_evidence_digest=evidence_digest, idempotency_key=idempotency_key,
            reviewed_at=now_str, review_evidence=review_evidence,
        )

    # --- Authorized Acceptance ---

    def record_task_acceptance(
        self,
        *,
        acceptor_actor_id: str,
        acceptor_token: str,
        task_id: str,
        revision: int,
        project_id: str,
        workspace: str,
        expected_result_digest: str,
        acceptance_evidence: Dict[str, Any],
        idempotency_key: str,
        now_utc: Optional[datetime.datetime] = None,
    ) -> TaskAcceptanceRecord:
        now = now_utc or self._now()
        now_str = format_iso_utc(now)

        if not isinstance(expected_result_digest, str) or not expected_result_digest:
            raise InvalidTupleError("expected_result_digest must be a non-empty string")
        if not isinstance(acceptance_evidence, dict) or not acceptance_evidence:
            raise InvalidTupleError("acceptance_evidence must be a non-empty dict")
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise InvalidTupleError("idempotency_key must be a non-empty string")

        evidence_digest = compute_sha256(acceptance_evidence)

        self._verify_caller(
            actor_id=acceptor_actor_id, token=acceptor_token, role="acceptor",
            project_id=project_id, workspace=workspace, task_id=task_id,
        )

        with self.store._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE;")
            try:
                boundary = self._validate_current_grant_boundary(
                    task_id=task_id,
                    now=now,
                    expected_revision=revision,
                    expected_workspace=workspace,
                    conn=conn,
                    require_custody_ack=True,
                    raise_on_error=True,
                )
                current_grant = boundary.grant
                assert current_grant is not None

                self._validate_provenance(
                    grant=current_grant,
                    conn=conn,
                    expected_result_digest=expected_result_digest,
                    require_proposal=True,
                    require_review=True,
                    require_acceptance=False,
                    acceptor_actor_id=acceptor_actor_id,
                    raise_on_error=True,
                )

                cur_a = conn.execute("SELECT * FROM task_acceptances WHERE task_id = ? AND revision = ?;", (task_id, revision))
                existing_a = cur_a.fetchone()
                if existing_a is not None:
                    if (
                        existing_a["acceptor_actor_id"] == acceptor_actor_id
                        and existing_a["expected_result_digest"] == expected_result_digest
                        and existing_a["acceptance_evidence_digest"] == evidence_digest
                        and existing_a["idempotency_key"] == idempotency_key
                    ):
                        try:
                            ex_a_ev = json.loads(existing_a["acceptance_evidence_json"])
                        except Exception as e:
                            conn.execute("ROLLBACK;")
                            raise VerificationError(f"Corrupted acceptance evidence JSON: {e}")
                        if existing_a["acceptance_evidence_digest"] != compute_sha256(ex_a_ev):
                            conn.execute("ROLLBACK;")
                            raise VerificationError(
                                f"Acceptance evidence digest mismatch for task '{task_id}' rev {revision}"
                            )
                        conn.execute("ROLLBACK;")
                        return TaskAcceptanceRecord(
                            task_id=task_id, revision=revision, acceptor_actor_id=acceptor_actor_id,
                            expected_result_digest=expected_result_digest, acceptance_evidence_digest=evidence_digest,
                            idempotency_key=idempotency_key, accepted_at=existing_a["accepted_at"],
                            acceptance_evidence=ex_a_ev,
                        )
                    conn.execute("ROLLBACK;")
                    raise CASConflictError(f"Acceptance conflict for task '{task_id}' rev {revision}")

                conn.execute(
                    """
                    INSERT INTO task_acceptances (
                        task_id, revision, acceptor_actor_id, expected_result_digest,
                        acceptance_evidence_digest, idempotency_key, accepted_at, acceptance_evidence_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    (task_id, revision, acceptor_actor_id, expected_result_digest, evidence_digest, idempotency_key, now_str, json.dumps(acceptance_evidence)),
                )
                conn.execute(
                    "UPDATE task_grants SET status = 'accepted', updated_at = ? WHERE task_id = ? AND revision = ?;",
                    (now_str, task_id, revision),
                )
                conn.execute("COMMIT;")
            except Exception:
                try:
                    conn.execute("ROLLBACK;")
                except sqlite3.OperationalError:
                    pass
                raise

        return TaskAcceptanceRecord(
            task_id=task_id, revision=revision, acceptor_actor_id=acceptor_actor_id,
            expected_result_digest=expected_result_digest, acceptance_evidence_digest=evidence_digest,
            idempotency_key=idempotency_key, accepted_at=now_str, acceptance_evidence=acceptance_evidence,
        )

    # --- Explicit Read Scope Query ---

    def get_task_grant(
        self,
        task_id: str,
        *,
        caller_actor_id: str,
        caller_token: str,
        project_id: str,
        workspace: str,
    ) -> Optional[TaskGrantRecord]:
        """Authenticated read query. Requires explicit reader permission and cross-scope protection.
        Reuses shared boundary validation: cannot leak raw ready authority after failures.
        """
        self._verify_caller(
            actor_id=caller_actor_id, token=caller_token, role="reader",
            project_id=project_id, workspace=workspace, task_id=task_id,
        )
        latest_grant = self.store.get_latest_grant(task_id)
        if latest_grant is None:
            return None
        if latest_grant.project_id != project_id or latest_grant.workspace != workspace:
            return None

        now = self._now()
        res = self._validate_current_grant_boundary(
            task_id=task_id,
            now=now,
            expected_workspace=workspace,
            raise_on_error=False,
        )

        if not res.is_valid:
            # unverified/expired/revoked/missing authority cannot leak raw ready authority
            return dataclasses.replace(
                latest_grant,
                status=res.effective_status,
                protected_reason=res.effective_protected_reason,
                has_owner_ack=res.has_owner_ack,
            )

        return dataclasses.replace(
            latest_grant,
            status=res.effective_status,
            protected_reason=res.effective_protected_reason,
            has_owner_ack=res.has_owner_ack,
        )

    # --- DependencyCompletionVerifierProtocol Implementation ---

    def verify_dependency_completion(
        self,
        dependency_task_id: str,
        expected_workspace: str,
        now_utc: datetime.datetime,
        dependency_evidence: Optional[Dict[str, Any]] = None,
    ) -> Tuple[bool, str]:
        """Verify DEPENDENCY's own current revision, owner, workspace, session, generation,
        issuer, protected state, pinned result, independent review, acceptance record,
        and current canonical identity presence and policy in FileBus.
        Never trusts caller metadata or plain completed set.
        """
        res = self._validate_current_grant_boundary(
            task_id=dependency_task_id,
            now=now_utc,
            expected_workspace=expected_workspace,
            require_custody_ack=True,
            raise_on_error=False,
        )
        if not res.is_valid or res.grant is None:
            return False, f"Dependency '{dependency_task_id}' authority boundary check failed: {res.error_message}"

        grant = res.grant
        if grant.protected_reason is not None:
            return False, f"Dependency '{dependency_task_id}' is protected: {grant.protected_reason}"

        ok, reason, proposal, review, acceptance = self._validate_provenance(
            grant=grant,
            conn=None,
            expected_result_digest=None,
            require_proposal=True,
            require_review=True,
            require_acceptance=True,
            raise_on_error=False,
        )
        if not ok:
            return False, reason

        return True, (
            f"Dependency '{dependency_task_id}' verified: rev={grant.revision}, owner={grant.native_owner_tag}, "
            f"digest={proposal.result_digest[:16]}..., accepted_by={acceptance.acceptor_actor_id}"
        )

    # --- AuthorityResolverProtocol Implementation ---

    def resolve_authority(
        self,
        raw_task_data: Any,
        now_utc: datetime.datetime,
    ) -> Optional[TrustedAuthorityGrant]:
        """Resolve raw task data into a verified TrustedAuthorityGrant against durable authority store.
        Reuses shared boundary validation. Rejects historic revisions, unverified tokens,
        missing native evidence, and unauthorized identities.
        """
        task_id: Optional[str] = None
        requested_revision: Optional[int] = None

        if isinstance(raw_task_data, str):
            task_id = raw_task_data.strip()
        elif isinstance(raw_task_data, dict):
            task_id = str(raw_task_data.get("task_id") or raw_task_data.get("id") or "").strip()
            rev = raw_task_data.get("revision")
            if rev is not None:
                if not _is_valid_int(rev) or rev < 1:
                    return None
                requested_revision = rev
        elif hasattr(raw_task_data, "task_id"):
            task_id = str(getattr(raw_task_data, "task_id")).strip()
        elif hasattr(raw_task_data, "id"):
            task_id = str(getattr(raw_task_data, "id")).strip()

        if not task_id:
            return None

        res = self._validate_current_grant_boundary(
            task_id=task_id,
            now=now_utc,
            expected_revision=requested_revision,
            raise_on_error=False,
        )

        if res.grant is None:
            return None

        # Historic revision requested: resolver cannot pick requested historic record
        if requested_revision is not None and res.grant.revision != requested_revision:
            return None

        # Revoked task: return None
        if res.effective_status == "revoked":
            return None

        # If unverified / expired / native binding failed: return blocked clearly notready
        if not res.is_valid:
            return TrustedAuthorityGrant(
                task_id=res.grant.task_id,
                revision=res.grant.revision,
                owner_tag=res.grant.native_owner_tag,
                workspace=res.grant.workspace,
                session_id=res.grant.native_session_uuid,
                generation=res.grant.generation,
                source_id=res.grant.source_id,
                source_digest=res.grant.source_digest,
                issued_at=res.grant.issued_at,
                status="blocked",
                expires_at=res.grant.expires_at,
                checkpoint_due=res.grant.checkpoint_due,
                completed_result=None,
                is_principal=res.grant.is_principal,
                protected_reason=res.effective_protected_reason or "Authority unverified or protected",
                dependencies=res.grant.dependencies,
                has_owner_ack=res.has_owner_ack,
                raw_metadata={"issuer_actor_id": res.grant.issuer_actor_id},
            )

        completed_result = None
        proposal = self.store.get_proposal(res.grant.task_id, res.grant.revision)
        if proposal is not None:
            completed_result = {
                "result_digest": proposal.result_digest,
                "evidence_digest": proposal.evidence_digest,
                "proposed_at": proposal.proposed_at,
            }

        return TrustedAuthorityGrant(
            task_id=res.grant.task_id,
            revision=res.grant.revision,
            owner_tag=res.grant.native_owner_tag,
            workspace=res.grant.workspace,
            session_id=res.grant.native_session_uuid,
            generation=res.grant.generation,
            source_id=res.grant.source_id,
            source_digest=res.grant.source_digest,
            issued_at=res.grant.issued_at,
            status=res.effective_status,
            expires_at=res.grant.expires_at,
            checkpoint_due=res.grant.checkpoint_due,
            completed_result=completed_result,
            is_principal=res.grant.is_principal,
            protected_reason=res.effective_protected_reason,
            dependencies=res.grant.dependencies,
            has_owner_ack=res.has_owner_ack,
            raw_metadata={"issuer_actor_id": res.grant.issuer_actor_id},
        )

    def revalidate_authority(
        self,
        task_id: str,
        revision: int,
        workspace: str,
        now_utc: datetime.datetime,
    ) -> Optional[TrustedAuthorityGrant]:
        """Revalidate current authority grant at exact revision and workspace.
        When revision differs from latest, returns None.
        When authority is unverified, expired, revoked, or missing, returns None.
        """
        if not _is_valid_int(revision) or revision < 1:
            return None
        if not isinstance(workspace, str) or not workspace.strip():
            return None

        res = self._validate_current_grant_boundary(
            task_id=task_id,
            now=now_utc,
            expected_revision=revision,
            expected_workspace=workspace,
            raise_on_error=False,
        )

        if not res.is_valid or res.grant is None:
            return None

        if res.effective_status != "ready" or not res.has_owner_ack:
            return None

        completed_result = None
        proposal = self.store.get_proposal(res.grant.task_id, res.grant.revision)
        if proposal is not None:
            completed_result = {
                "result_digest": proposal.result_digest,
                "evidence_digest": proposal.evidence_digest,
                "proposed_at": proposal.proposed_at,
            }

        return TrustedAuthorityGrant(
            task_id=res.grant.task_id,
            revision=res.grant.revision,
            owner_tag=res.grant.native_owner_tag,
            workspace=res.grant.workspace,
            session_id=res.grant.native_session_uuid,
            generation=res.grant.generation,
            source_id=res.grant.source_id,
            source_digest=res.grant.source_digest,
            issued_at=res.grant.issued_at,
            status=res.effective_status,
            expires_at=res.grant.expires_at,
            checkpoint_due=res.grant.checkpoint_due,
            completed_result=completed_result,
            is_principal=res.grant.is_principal,
            protected_reason=res.effective_protected_reason,
            dependencies=res.grant.dependencies,
            has_owner_ack=res.has_owner_ack,
            raw_metadata={"issuer_actor_id": res.grant.issuer_actor_id},
        )
