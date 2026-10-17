"""M12e host native session adapter: list / inbox delivery / bounded capture / confirmed stop.

Genuine native protocols only (installed ``a``/``aplexer`` CLI, argv, no shell).
Read-only with respect to all other hub source: this module wraps the native CLI and
reuses :mod:`agent_hub_host.native_bridge` workspace helpers. It never starts sessions
(start is owned by :mod:`agent_hub_host.session_executor` through the existing Fleet
``Dispatcher`` contract), never touches live sessions except the exact qualified target,
and never claims more than the native evidence proves:

- ``recorded``: durable inbox envelope persisted (``message send``). NOT delivery proof.
- ``transported``: an existing envelope submitted through the native deliver guard
  (``message deliver`` inspects a fresh empty prompt itself). NOT processing proof.
- ``acknowledged``: ONLY a correlated native recipient reply proves processing (see
  :func:`verify_ack_via_reply`). It is never derived from a ``send --enter`` working
  verdict, transcript activity, phase flags, or inbox persistence.
- ``unknown``: the ACK proof transport is unqualified or no reply envelope matched;
  reported honestly instead of a false confirmed.
- ``refused``/``held``: readiness/composer gate rejected the attempt; no effect attempted.
- ``uncertain``: the effect may have happened; reconcile from ``list``/``status``/``log``
  before any retry. Never blind-retry.

R2 correction (head-ordered): the predecessor mapped ``send --enter --json``
``working:true`` to ``acknowledged``. That verdict is NOT target-read/process proof,
so the raw ``send --enter`` path is RETIRED here (no ``--pane``, no ``--no-ack``,
no ``--enter`` anywhere in this module). Delivery is durable inbox only, with the
native ``message deliver`` guard available as an explicit opt-in. Inbox persistence
is never rendered as transported or acknowledged.

Forbidden transports: ``send --enter``, ``--pane`` injection, ``--no-ack``,
``--from`` identity override, busy-composer input, stale-idle bypass.
``message send --to`` takes a TAG, never a UUID.
"""

from __future__ import annotations

import json
import os
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .native_bridge import (
    NativeServiceBinding,
    canonicalize_workspace,
)


def _validate_own_send_envelope(envelope: Any, sender_binding: NativeServiceBinding,
                                target: NativeTarget) -> None:
    """Validate a persisted send envelope against the qualified own sender.

    Cross-workspace contract (R2-F5, live probe 2026-10-17): the envelope is
    addressed to the TARGET mailbox, but its ``from`` is the SENDER's own
    identity in the SENDER mailbox — the two workspaces are independent, and
    the shared single-workspace validator in ``native_bridge`` cannot express
    this (that module is outside session-control scope, so the rule lives
    here). Requirements: ``from`` present, non-anonymous, same session/tag as
    the binding qualified BEFORE the effect, filed from the sender mailbox;
    the envelope itself addressed to the target mailbox (or, tolerantly, our
    own mailbox). Any violation is an unproven effect — ``SessionUncertain``
    (reconcile), never silent acceptance and never a fabricated identity.
    """
    if not isinstance(envelope, dict):
        raise SessionUncertain("native send returned no envelope object; effect unproven")
    sender_ws = canonicalize_workspace(sender_binding.workspace)
    target_ws = target.workspace
    from_obj = envelope.get("from")
    if not isinstance(from_obj, dict) or from_obj.get("external") is True:
        raise SessionUncertain(
            "native send persisted an unreplyable sender envelope; effect recorded "
            "but processing can never be proven — reconcile")
    if not from_obj.get("tag") or not from_obj.get("session_id"):
        raise SessionUncertain("native send envelope from lacks tag/session_id; effect unproven")
    if from_obj.get("tag") != sender_binding.tag or \
            str(from_obj.get("session_id")).lower() != str(sender_binding.session_id).lower():
        raise SessionUncertain("native send envelope sender mismatch; effect unproven")
    from_ws = from_obj.get("workspace")
    if not isinstance(from_ws, str) or not from_ws or canonicalize_workspace(from_ws) != sender_ws:
        raise SessionUncertain("native send envelope not filed from the sender mailbox; effect unproven")
    env_ws = envelope.get("workspace")
    if isinstance(env_ws, str) and env_ws and \
            canonicalize_workspace(env_ws) != target_ws and canonicalize_workspace(env_ws) != sender_ws:
        raise SessionUncertain("native send envelope filed outside both mailboxes; effect unproven")

MAX_BODY_BYTES = 16 * 1024  # bus limit, reused for prompt bodies
MAX_CAPTURE_BYTES = 32 * 1024  # default bounded tail
MAX_CAPTURE_LIMIT = 128 * 1024  # absolute capture ceiling for one call
MAX_NATIVE_OUTPUT = 1024 * 1024  # native CLI stdout bound
NATIVE_TIMEOUT = 60.0  # single native call ceiling (send --enter may wait for readiness)
QUICK_TIMEOUT = 30.0

TERMINAL_PHASES = {"exited", "dead", "forgotten", "pruned"}


class SessionControlError(Exception):
    """Refused or invalid session-control operation (no native effect attempted)."""


class SessionUncertain(Exception):
    """Native effect may have happened; reconcile before retrying.

    Carries the partial evidence observed so far in ``evidence``.
    """


@dataclass(frozen=True)
class NativeTarget:
    """Exact native session binding required before/after every operation.

    ``created_at_ms`` is the native generation provenance (from ``a list`` /
    ``a status``). When provided, :func:`qualify_target` requires the live catalog
    row to carry the same generation (tag reuse or incarnation drift refuses).
    ``engine`` is the expected session engine (from the hub offer/binding);
    when provided, :func:`qualify_target` requires the live catalog row to
    report the same engine — a real SHELL session never qualifies as a
    requested codex/antigravity model (masquerade refuses with no effect).
    """

    session_id: str
    tag: str
    workspace: str
    created_at_ms: Optional[int] = None
    engine: Optional[str] = None

    def validated(self) -> "NativeTarget":
        try:
            norm = str(uuid.UUID(str(self.session_id)))
        except (ValueError, TypeError) as exc:
            raise SessionControlError(f"Invalid native session UUID: {self.session_id!r}") from exc
        if not isinstance(self.tag, str) or not self.tag or self.tag.startswith("-"):
            raise SessionControlError(f"Invalid native tag: {self.tag!r}")
        if not isinstance(self.workspace, str) or not Path(self.workspace).is_absolute():
            raise SessionControlError("Workspace must be an absolute path")
        canonical = canonicalize_workspace(self.workspace)
        if canonical != self.workspace:
            raise SessionControlError("Workspace must already be canonical (no symlinks)")
        if self.created_at_ms is not None and type(self.created_at_ms) is not int:
            raise SessionControlError("Native generation (created_at_ms) must be an int when given")
        if self.engine is not None and (not isinstance(self.engine, str) or not self.engine or len(self.engine) > 64):
            raise SessionControlError("Expected engine must be a non-empty label when given")
        return NativeTarget(session_id=norm, tag=self.tag, workspace=canonical,
                            created_at_ms=self.created_at_ms, engine=self.engine)


@dataclass(frozen=True)
class NativeSession:
    """One projected row from ``a list`` (only necessary fields; no env/secrets)."""

    session_id: str
    tag: str
    workspace: str
    engine: Optional[str] = None
    profile: Optional[str] = None
    phase: str = "unknown"
    agent_state: Optional[str] = None
    last_activity_ms: Optional[int] = None
    created_at_ms: Optional[int] = None
    cwd: Optional[str] = None
    containment_empty: Optional[bool] = None
    workload_cgroup: Optional[str] = None
    worker_pid: Optional[int] = None
    workload_pid: Optional[int] = None
    processes: Optional[int] = None
    agent_state_at_ms: Optional[int] = None


@dataclass(frozen=True)
class DeliveryResult:
    state: str  # recorded|transported|refused|uncertain (never acknowledged here)
    native_id: Optional[str] = None
    reason: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AckResult:
    """Correlated recipient-ACK proof (or its honest absence).

    ``acknowledged`` only when a reply envelope was found in the retrieved native
    log whose ``reply_to`` equals the original envelope id, whose ``from``
    session/tag/workspace matches the intended native generation, and whose body
    echoes the exact correlation/digest. Anything else is ``unknown`` — never a
    false confirmed.
    """

    state: str  # acknowledged|unknown
    reply_envelope_id: Optional[str] = None
    reason: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CaptureResult:
    session_id: str
    tag: str
    mode: str  # tail|screen
    text: str
    truncated: bool
    byte_length: int
    # Privacy is explicitly UNKNOWN: terminal output can contain secrets and a
    # redaction regex alone cannot prove safety. This projection is private,
    # no-store, bounded UTF-8 for the exact opt-in controlled session only.
    # Callers must hold explicit capture authorization; never upload raw archives.
    privacy_unknown: bool = True


@dataclass(frozen=True)
class StopResult:
    state: str  # stopped|uncertain|refused
    session_id: str
    reason: str = ""


def _looks_like_uuid(value: str) -> bool:
    try:
        uuid.UUID(str(value))
        return True
    except (ValueError, TypeError):
        return False


def check_body_bound(body: str, limit: int = MAX_BODY_BYTES) -> str:
    if not isinstance(body, str) or not body.strip():
        raise SessionControlError("Body must be non-empty text")
    if len(body.encode("utf-8")) > limit:
        raise SessionControlError(f"Body exceeds {limit} byte bound")
    return body


def body_digest(body: str) -> str:
    """Stable hex digest of the exact intended body for correlation echo checks."""
    import hashlib

    if not isinstance(body, str):
        raise SessionControlError("Body must be text for digesting")
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def resolve_cwd(workspace_roots: list[str], cwd: str) -> str:
    """Resolve requested cwd strictly inside operator-configured workspace roots.

    Rejects path traversal and escaping symlinks (realpath comparison). Raises
    SessionControlError without any native effect. This validates a caller's
    requested path for a qualified start (session/Fleet/host adapter now
    carries cwd end to end); it never claims an already-launched session moved.
    """
    if not isinstance(cwd, str) or not cwd:
        raise SessionControlError("cwd is required")
    canon_roots: list[str] = []
    for root in workspace_roots:
        if not isinstance(root, str) or not Path(root).is_absolute():
            raise SessionControlError("Workspace roots must be absolute paths")
        canon_roots.append(canonicalize_workspace(root))
    candidate = canonicalize_workspace(os.path.abspath(cwd))
    for root in canon_roots:
        if candidate == root or candidate.startswith(root + os.sep):
            return candidate
    raise SessionControlError(f"cwd escapes configured workspace roots: {cwd!r}")


def run_native(
    executable: str,
    args: list[str],
    timeout: float = QUICK_TIMEOUT,
    cwd: Optional[str] = None,
) -> subprocess.CompletedProcess:
    """Run one native CLI call: argv only (no shell), bounded output, no secrets in errors."""
    if not isinstance(executable, str) or not executable:
        raise SessionControlError("Native executable is required")
    if not isinstance(args, list) or any(not isinstance(a, str) or not a for a in args):
        raise SessionControlError("Native argv must be a non-empty string list")
    if any(a.startswith("--from") for a in args):
        raise SessionControlError("Sender identity override (--from) is forbidden")
    if "--pane" in args or "--no-ack" in args or "--enter" in args:
        raise SessionControlError("--pane/--no-ack/--enter transports are forbidden")
    try:
        proc = subprocess.run(
            [executable, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            cwd=cwd,
        )
    except subprocess.TimeoutExpired as exc:
        raise SessionUncertain(
            "Native call timed out; effect unknown, reconcile before retry",
            # evidence intentionally minimal: no stdout/stderr exfiltration
        ) from exc
    except OSError as exc:
        raise SessionControlError(f"Native executable failed: {type(exc).__name__}") from exc
    for stream in (proc.stdout, proc.stderr):
        if len(stream.encode("utf-8", "replace")) > MAX_NATIVE_OUTPUT:
            raise SessionUncertain("Native response exceeds bound; effect unknown")
    return proc


def _parse_json_output(proc: subprocess.CompletedProcess, what: str) -> Any:
    try:
        return json.loads(proc.stdout)
    except (ValueError, TypeError) as exc:
        raise SessionUncertain(f"Native {what} returned non-JSON output; effect unknown") from exc


def project_session(raw: Any) -> NativeSession:
    """Project only necessary list fields; unknown shapes stay unknown, never guessed."""
    if not isinstance(raw, dict):
        raise SessionControlError("Native session row is not an object")
    sid = raw.get("id")
    tag = raw.get("tag")
    ws = raw.get("workspace")
    if not isinstance(sid, str) or not sid or not isinstance(tag, str) or not tag:
        raise SessionControlError("Native session row missing id/tag")
    if not isinstance(ws, str) or not ws:
        raise SessionControlError("Native session row missing workspace")
    last_ms = raw.get("last_activity_ms")
    created_ms = raw.get("created_at_ms")
    worker_pid = raw.get("worker_pid")
    workload_pid = raw.get("workload_pid")
    processes = raw.get("processes")
    containment = raw.get("containment_empty")
    cgroup = raw.get("containment_cgroup") if isinstance(raw.get("containment_cgroup"), str) and raw.get("containment_cgroup") else raw.get("workload_cgroup")
    if isinstance(cgroup, str) and cgroup.startswith("/user.slice/"):
        cgroup = "/sys/fs/cgroup" + cgroup
    cwd = raw.get("cwd")
    # Installed aplexer calls its hook metadata reported_state. Keep the
    # older agent_state spelling, without inferring state from process phase.
    agent_state = raw.get("agent_state")
    state_at = raw.get("agent_state_at_ms")
    if agent_state is None:
        agent_state = raw.get("reported_state")
        state_at = raw.get("reported_state_at_ms")
    return NativeSession(
        session_id=sid,
        tag=tag,
        workspace=ws,
        engine=raw.get("engine") if isinstance(raw.get("engine"), str) else None,
        profile=raw.get("profile") if isinstance(raw.get("profile"), str) else None,
        phase=str(raw.get("phase") or raw.get("state") or "unknown"),
        agent_state=agent_state if isinstance(agent_state, str) else None,
        agent_state_at_ms=state_at if type(state_at) is int and state_at >= 0 else None,
        last_activity_ms=last_ms if type(last_ms) is int else None,
        created_at_ms=created_ms if type(created_ms) is int else None,
        cwd=cwd if isinstance(cwd, str) and cwd else None,
        containment_empty=containment if type(containment) is bool else None,
        workload_cgroup=cgroup if isinstance(cgroup, str) and cgroup else None,
        worker_pid=worker_pid if type(worker_pid) is int else None,
        workload_pid=workload_pid if type(workload_pid) is int else None,
        processes=processes if type(processes) is int else None,
    )


def list_sessions(
    executable: str = "aplexer",
    workspace: Optional[str] = None,
    include_exited: bool = False,
) -> list[NativeSession]:
    """List native sessions, optionally filtered to one canonical workspace (read-only)."""
    args = ["list", "--json"]
    if include_exited:
        args.append("--all")
    proc = run_native(executable, args)
    if proc.returncode != 0:
        raise SessionControlError("Native list failed")
    rows = _parse_json_output(proc, "list")
    if not isinstance(rows, list):
        raise SessionControlError("Native list output is not an array")
    sessions = [project_session(r) for r in rows[:500]]
    if workspace is not None:
        canonical = canonicalize_workspace(workspace)
        sessions = [s for s in sessions if canonicalize_workspace(s.workspace) == canonical]
    return sessions


def qualify_target(
    executable: str,
    target: NativeTarget,
    live_only: bool = True,
) -> NativeSession:
    """Re-qualify tag -> (UUID, workspace, generation) immediately before an effect.

    Requires exactly one catalog row matching workspace+tag AND the expected UUID.
    Tag reuse (same tag, different UUID) or workspace drift refuses with no effect.
    When the target carries ``created_at_ms``, the row must carry the same native
    generation (a hub counter alone is not provenance); a stale generation refuses.
    Never infers idleness/readiness from phase flags: phase is liveness only.
    """
    target = target.validated()
    sessions = list_sessions(executable, workspace=target.workspace, include_exited=True)
    same_tag = [s for s in sessions if s.tag == target.tag]
    if len(same_tag) != 1:
        raise SessionControlError(
            f"Tag {target.tag!r} is ambiguous or absent in workspace "
            f"(rows: {len(same_tag)}); refusing to address by tag"
        )
    row = same_tag[0]
    if row.session_id.lower() != target.session_id.lower():
        raise SessionControlError(
            "Tag now resolves to a different native session (tag reuse); refusing"
        )
    if target.created_at_ms is not None and row.created_at_ms != target.created_at_ms:
        raise SessionControlError(
            "Native generation mismatch (stale incarnation or tag reuse); refusing"
        )
    if target.engine is not None and row.engine != target.engine:
        # A real SHELL (or any other) native session never qualifies as a
        # requested model engine: exact match or no effect. Unknown catalog
        # engine (None) is NOT a match for a qualified expectation.
        raise SessionControlError(
            "Native engine mismatch (reported %r, expected %r); refusing"
            % (row.engine, target.engine)
        )
    if live_only and row.phase in TERMINAL_PHASES:
        raise SessionControlError(f"Target session is terminal (phase={row.phase}); refusing")
    return row


def qualify_sender_binding(
    executable: str,
    authorized_workspace: Optional[str] = None,
) -> NativeServiceBinding:
    """Qualify the calling process's genuine native sender identity BEFORE any send.

    Runs read-only ``whoami --json`` (argv only, no shell, no identity
    override) and requires a real bound session (UUID id + non-empty tag).
    The observed own workspace is the sender mailbox the installed ``a``
    routes cross-workspace replies to — it is qualified INDEPENDENTLY of any
    target workspace (the old same-workspace restriction is removed: the
    installed CLI delivers cross-workspace sends and files replies in the
    author's mailbox). When ``authorized_workspace`` is given (service
    deployment pin), the observed workspace must equal it; otherwise the
    observed own workspace is the authorized sender workspace. An unbound
    process or an anonymous/foreign envelope sender means native recipients
    could never ``message reply`` (installed Rust ``reply_target`` rejects
    anonymous originals), so the attempt is refused here with
    ``SessionControlError`` (honest capability gap, no native effect) instead
    of emitting an unreplyable envelope. The returned binding is the observed
    own identity, used to validate the actual send envelope and to read the
    correct reply mailbox below — sender fields are never fabricated.
    """
    if authorized_workspace is not None and (
            not isinstance(authorized_workspace, str) or not Path(authorized_workspace).is_absolute()):
        raise SessionControlError("Authorized sender workspace must be an absolute path")
    try:
        proc = run_native(executable, ["whoami", "--json"], timeout=QUICK_TIMEOUT)
    except (SessionControlError, SessionUncertain) as exc:
        raise SessionControlError(
            "native_sender_unbound: this host process holds no qualified native sender "
            "identity (whoami unavailable); recipients could not reply") from exc
    if proc.returncode != 0:
        raise SessionControlError(
            "native_sender_unbound: this host process holds no qualified native sender "
            "identity (whoami refused); recipients could not reply")
    try:
        identity = json.loads(proc.stdout)
    except (ValueError, TypeError) as exc:
        raise SessionControlError(
            "native_sender_unbound: unqualified native sender identity output") from exc
    if not isinstance(identity, dict):
        raise SessionControlError("native_sender_unbound: unqualified native sender identity output")
    sender_id = identity.get("id", identity.get("session_id"))
    sender_tag = identity.get("tag")
    sender_ws = identity.get("workspace")
    try:
        norm_id = str(uuid.UUID(str(sender_id))) if sender_id else ""
    except (ValueError, TypeError, AttributeError):
        norm_id = ""
    if not norm_id or not isinstance(sender_tag, str) or not sender_tag \
            or not isinstance(sender_ws, str) or not sender_ws:
        raise SessionControlError(
            "native_sender_unbound: anonymous or incomplete native sender identity; "
            "recipients could not reply")
    canonical = canonicalize_workspace(sender_ws)
    if authorized_workspace is not None and canonicalize_workspace(authorized_workspace) != canonical:
        raise SessionControlError(
            "native_sender_unbound: own native session lives in %r, not the authorized "
            "sender workspace %r; refusing" % (sender_ws, authorized_workspace))
    try:
        created = identity.get("created_at_ms")
        created_ms = int(created) if type(created) is int else 0
    except (TypeError, ValueError):
        created_ms = 0
    return NativeServiceBinding(
        session_id=norm_id, tag=sender_tag, workspace=canonical,
        engine=str(identity.get("engine", "hub-host")),
        profile=identity.get("profile") if isinstance(identity.get("profile"), str) else None,
        created_at_ms=created_ms,
    )


def send_inbox(
    executable: str,
    target: NativeTarget,
    body: str,
    correlation: dict[str, str],
    kind: str = "note",
    sender_workspace: Optional[str] = None,
) -> tuple[DeliveryResult, NativeServiceBinding]:
    """Durable inbox delivery via ``message send --to TAG`` (TAG, never UUID).

    State ``recorded`` at most: inbox persistence alone is NOT proof the target
    read or processed the message. Returns the delivery record AND the genuine
    sender binding observed before the effect (the sender mailbox the installed
    CLI routes cross-workspace replies to — callers must read replies there,
    not in the target workspace).
    """
    target = target.validated()
    check_body_bound(body)
    if _looks_like_uuid(target.tag):
        raise SessionControlError("Refusing UUID-shaped --to value (TAG required, never UUID)")
    if not isinstance(correlation, dict) or not correlation.get("correlation_id"):
        raise SessionControlError("Correlation id is required for every send")
    if not isinstance(kind, str) or not kind or len(kind) > 64:
        raise SessionControlError("Invalid message kind")
    # Genuine sender qualification BEFORE the effect: an unbound sender would
    # emit an anonymous envelope recipients cannot reply to, so refuse
    # honestly with no effect. The sender workspace is the author's own
    # (optionally pinned); it is independent of the target workspace.
    sender_binding = qualify_sender_binding(executable, sender_workspace)
    qualify_target(executable, target)
    data = json.dumps({"hub_session_control": correlation}, ensure_ascii=False)
    proc = run_native(
        executable,
        ["message", "send", "--workspace", target.workspace, "--to", target.tag,
         "--kind", kind, "--data", data, "--json", "--", body],
        timeout=NATIVE_TIMEOUT,
    )
    if proc.returncode != 0:
        stderr = proc.stderr.strip().splitlines()
        hint = stderr[-1][:300] if stderr else "native send failed"
        if "no session tagged" in proc.stderr:
            raise SessionControlError(f"Inbox refused: {hint}")
        raise SessionUncertain(f"Inbox send uncertain: {hint}")
    envelope = _parse_json_output(proc, "message send")
    if not isinstance(envelope, dict) or not envelope.get("id"):
        raise SessionUncertain("Inbox send returned no envelope id; effect unknown")
    # The envelope MUST carry our own qualified sender identity: recipients
    # can only reply to a non-anonymous sender tag (installed reply_target).
    # Cross-workspace aware (see _validate_own_send_envelope): the sender
    # mailbox is independent of the target mailbox.
    _validate_own_send_envelope(envelope, sender_binding, target)
    # Sender provenance is the caller's own session; recipients reply per the native
    # reply_target contract. No processing claim is made here by construction.
    try:
        qualify_target(executable, target, live_only=False)
    except SessionControlError:
        pass  # post-send binding drift is reported, not hidden; envelope id stands
    return (DeliveryResult(
        state="recorded",
        native_id=str(envelope.get("id")),
        reason="durable inbox persisted; target processing NOT proven",
        evidence={"kind": kind, "correlation_id": correlation.get("correlation_id")},
    ), sender_binding)


def deliver_recorded(
    executable: str,
    target: NativeTarget,
    envelope_id: str,
) -> DeliveryResult:
    """Submit one existing inbox envelope via the native deliver guard.

    ``message deliver <id>`` submits the ORIGINAL envelope (delivery never creates
    another one) after the native side inspects a fresh, empty prompt itself. This
    is the supported transport beyond durable inbox. Success maps to ``transported``
    — never ``acknowledged`` (only :func:`verify_ack_via_reply` proves processing).
    No composer-idle claim is made from phase/working flags; the readiness inspection
    belongs to the native guard, not to this caller.
    """
    target = target.validated()
    if not isinstance(envelope_id, str) or not envelope_id or len(envelope_id) > 128:
        raise SessionControlError("Envelope id is required for deliver")
    qualify_target(executable, target)
    proc = run_native(
        executable,
        ["message", "deliver", "--workspace", target.workspace, "--json", envelope_id],
        timeout=NATIVE_TIMEOUT,
    )
    outcome: Optional[dict[str, Any]] = None
    if proc.stdout.strip():
        try:
            parsed = json.loads(proc.stdout)
            if isinstance(parsed, dict):
                outcome = parsed
        except (ValueError, TypeError):
            outcome = None

    if outcome is not None:
        status = outcome.get("status")
        detail = outcome.get("detail")
        if isinstance(status, str):
            status_lower = status.lower().strip()
            if status_lower in ("submitted", "already-submitted", "recipient-acked"):
                # Note: recipient-acked indicates previous native cursor ack,
                # but correlated exact-recipient reply remains the ONLY proof of processing.
                return DeliveryResult(
                    state="transported",
                    native_id=envelope_id,
                    reason=detail or f"native deliver guard reports {status_lower}; processing NOT proven",
                    evidence={"status": status_lower, "detail": detail},
                )
            if status_lower in ("not-ready", "busy", "draft", "menu", "unknown"):
                # rc0 not-ready is not transported: readiness gate refused.
                return DeliveryResult(
                    state="refused",
                    native_id=envelope_id,
                    reason=detail or f"native deliver guard reports {status_lower}",
                    evidence={"status": status_lower, "detail": detail},
                )
            if status_lower in ("delivery-uncertain", "uncertain"):
                raise SessionUncertain(detail or "Deliver outcome uncertain; reconcile before retry")
            raise SessionUncertain(detail or f"Native deliver returned unknown status: {status!r}")
        raise SessionUncertain("Native deliver returned JSON without valid status; outcome uncertain")

    if proc.returncode == 0:
        raise SessionUncertain("Native deliver exited 0 without valid JSON output; outcome uncertain")

    lowered = proc.stderr.lower()
    if any(s in lowered for s in ("no session tagged", "not recorded", "no such message",
                                  "only the original sender or recipient may deliver",
                                  "delivery requires")):
        raise SessionControlError((proc.stderr.strip().splitlines() or ["deliver guard refused"])[-1][:300])

    if any(s in lowered for s in ("busy", "not empty", "menu", "draft", "unknown", "not ready", "reported working")):
        return DeliveryResult(
            state="refused",
            native_id=envelope_id,
            reason=(proc.stderr.strip().splitlines() or ["deliver guard refused"])[-1][:300],
            evidence={"status": "not-ready"},
        )
    if any(s in lowered for s in ("unrecognized subcommand", "not supported", "error: unrecognized")) or (not proc.stdout and not proc.stderr):
        return DeliveryResult(
            state="recorded",
            native_id=envelope_id,
            reason="native deliver guard unsupported; envelope recorded in inbox",
            evidence={"status": "unsupported"},
        )
    raise SessionUncertain("Deliver outcome unknown; reconcile before retry")


def reconcile_delivery_from_log(
    executable: str,
    target_workspace: str,
    envelope_id: str,
) -> Optional[str]:
    """Retrieve message log for target workspace to inspect the delivery mode of an envelope.

    Returns:
    - 'transported' if the envelope's delivery is 'pane'
    - 'recorded' if the envelope exists and delivery is 'inbox'
    - None if the envelope is not found or log unavailable
    """
    try:
        log = fetch_message_log(executable, target_workspace)
        for env in log:
            if env.get("id") == envelope_id:
                if env.get("delivery") == "pane":
                    return "transported"
                return "recorded"
    except (SessionControlError, SessionUncertain):
        return None
    return None


MAX_LOG_ROWS = 200  # bounded native log retrieval per reconciliation


def fetch_message_log(
    executable: str,
    workspace: str,
    limit: int = MAX_LOG_ROWS,
) -> list[dict[str, Any]]:
    """Retrieve the actual native message log for the exact workspace (bounded, read-only).

    Returns raw envelope dicts (newest last per native order), truncated to ``limit``
    rows. Callers must verify envelope fields themselves; nothing here is proof.
    """
    if not isinstance(workspace, str) or not Path(workspace).is_absolute():
        raise SessionControlError("Workspace must be an absolute path")
    canonical = canonicalize_workspace(workspace)
    if type(limit) is not int or not 1 <= limit <= MAX_LOG_ROWS:
        raise SessionControlError(f"Log bound must be 1..{MAX_LOG_ROWS}")
    proc = run_native(
        executable,
        ["message", "log", "--workspace", canonical, "--json"],
        timeout=QUICK_TIMEOUT,
    )
    if proc.returncode != 0:
        raise SessionUncertain("Native message log unavailable; reconciliation deferred")
    rows = _parse_json_output(proc, "message log")
    if not isinstance(rows, list):
        raise SessionUncertain("Native message log is not an array; reconciliation deferred")
    return [r for r in rows if isinstance(r, dict)][-limit:]


def verify_ack_via_reply(
    executable: str,
    target: NativeTarget,
    sender: NativeServiceBinding,
    original_envelope_id: str,
    correlation: dict[str, str],
) -> AckResult:
    """Verify a correlated recipient reply in the actual native message log.

    ``acknowledged`` requires ALL of: the reply envelope exists in the retrieved
    log (bounded, SENDER mailbox); its ``reply_to`` equals the ORIGINAL NATIVE
    message envelope id (never a hub ``cmd:`` string); its ``from`` session/tag/
    workspace matches the intended target generation (observed catalog
    ``created_at_ms``); and its body/data echoes the exact correlation id and
    body digest. Otherwise the result is ``unknown`` — an honest report, never
    false confirmed. When the proof transport itself is unqualified (log
    unavailable, unknown envelope shape), the result is likewise ``unknown``
    with the reason recorded.

    The mailbox read is the SENDER's (author's) workspace: the installed CLI
    routes cross-workspace replies to the original author's mailbox, not the
    target's. ``sender`` is the genuine binding qualified before the send
    (never a copy, never a ``--from`` override). The observed live catalog
    incarnation from :func:`qualify_target` is the provenance witness; an
    absent envelope ``created_at_ms`` (the real installed DTO carries none in
    ``from``) never falls back to an expected target value.
    """
    target = target.validated()
    if not isinstance(sender, NativeServiceBinding) or not sender.workspace:
        raise SessionControlError("Genuine sender binding is required for ACK proof")
    if not isinstance(original_envelope_id, str) or not original_envelope_id:
        raise SessionControlError("Original native envelope id is required for ACK proof")
    if not isinstance(correlation, dict) or not correlation.get("correlation_id"):
        raise SessionControlError("Correlation id is required for ACK proof")
    expected_digest = correlation.get("body_digest", "")
    # Exact target requalification: the tag must still resolve to this UUID +
    # incarnation (read-only list). Tag reuse or incarnation drift means a
    # reply from the tag is no longer proof from THIS target. The returned
    # live catalog row is the observed provenance witness.
    try:
        observed = qualify_target(executable, target, live_only=False)
    except SessionControlError as exc:
        return AckResult(state="unknown", reason=f"target requalification failed: {exc}")
    try:
        log = fetch_message_log(executable, sender.workspace)
    except SessionUncertain as exc:
        return AckResult(state="unknown", reason=f"proof transport unqualified: {exc}")
    except SessionControlError as exc:
        return AckResult(state="unknown", reason=f"proof transport unqualified: {exc}")
    for env in log:
        if env.get("reply_to") != original_envelope_id:
            continue
        author = env.get("from")
        if not isinstance(author, dict):
            continue
        if (str(author.get("session_id", "")).lower() != target.session_id.lower()
                or author.get("tag") != target.tag
                or author.get("workspace") != target.workspace):
            continue  # foreign or retargeted reply: not our proof
        # Real reply provenance: when the reply carries its author's native
        # creation incarnation, it MUST match the OBSERVED live catalog
        # incarnation. An actual creation value is never overwritten with an
        # expected one; an absent value uses the observed row (real installed
        # envelopes carry no created_at_ms in ``from``).
        author_created = author.get("created_at_ms")
        if type(author_created) is int:
            if observed.created_at_ms is not None and author_created != observed.created_at_ms:
                continue  # stale incarnation or tag reuse: not our proof
        payload = env.get("data")
        body_text = env.get("body") if isinstance(env.get("body"), str) else ""
        echoed_corr = ""
        echoed_digest = ""
        if isinstance(payload, dict):
            echoed_corr = str(payload.get("correlation_id", ""))
            echoed_digest = str(payload.get("body_digest", ""))
        if echoed_corr:
            # Structured echo path (installed reply --data contract).
            if echoed_corr != correlation["correlation_id"]:
                continue
            if expected_digest and echoed_digest != expected_digest:
                continue
        else:
            # Body-text fallback path (standard `message reply <id> "<text>"`
            # without --data): the body must echo the exact correlation id and,
            # when a digest is expected, the exact body digest. A bare id echo
            # with no digest is NOT proof.
            if correlation["correlation_id"] not in body_text:
                continue
            if expected_digest and expected_digest not in body_text:
                continue
        # Server proof needs a 64-hex reply digest computed from the OBSERVED
        # reply envelope (host-verified provenance, not crypto target auth).
        # Defined as sha256 hex of the reply body text (UTF-8); when the body
        # is not text, of the canonical JSON of its data payload. The hub checks
        # shape/binding/uniqueness only — the host performed the log retrieval
        # and correlation verification above.
        import hashlib
        if isinstance(env.get("body"), str) and env.get("body"):
            digest_src = env["body"].encode("utf-8")
        else:
            try:
                digest_src = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
            except (TypeError, ValueError):
                digest_src = str(env.get("id", "")).encode("utf-8")
        reply_digest = hashlib.sha256(digest_src).hexdigest()
        return AckResult(
            state="acknowledged",
            reply_envelope_id=str(env.get("id")),
            reason="correlated native reply verified in message log",
            evidence={"correlation_id": correlation["correlation_id"],
                      "original_envelope_id": original_envelope_id,
                      "reply_digest": reply_digest,
                      "sender_native_id": str(author.get("session_id", "")),
                      "sender_tag": str(author.get("tag", "")),
                      "sender_workspace": str(author.get("workspace", "")),
                      "sender_created_at_ms": author_created if type(author_created) is int else observed.created_at_ms,
                      "reply_body": body_text[:512]},
        )
    return AckResult(
        state="unknown",
        reason="no correlated reply envelope in retrieved native log",
    )


def capture_output(
    executable: str,
    target: NativeTarget,
    limit_bytes: int = MAX_CAPTURE_BYTES,
    mode: str = "tail",
) -> CaptureResult:
    """Bounded capture: ``capture --bytes N`` tail or ``--screen --plain`` snapshot."""
    target = target.validated()
    if type(limit_bytes) is not int or not 1 <= limit_bytes <= MAX_CAPTURE_LIMIT:
        raise SessionControlError(f"Capture bound must be 1..{MAX_CAPTURE_LIMIT}")
    if mode not in ("tail", "screen"):
        raise SessionControlError("Capture mode must be tail|screen")
    qualify_target(executable, target, live_only=False)
    if mode == "screen":
        args = ["capture", "--workspace", target.workspace, "--tag", target.tag,
                "--screen", "--plain"]
    else:
        args = ["capture", "--workspace", target.workspace, "--tag", target.tag,
                "--bytes", str(limit_bytes)]
    proc = run_native(executable, args, timeout=QUICK_TIMEOUT)
    if proc.returncode != 0:
        raise SessionControlError("Native capture failed")
    text = proc.stdout
    raw = text.encode("utf-8", "replace")
    truncated = len(raw) > limit_bytes
    if truncated:
        text = raw[:limit_bytes].decode("utf-8", "replace")
    return CaptureResult(
        session_id=target.session_id,
        tag=target.tag,
        mode=mode,
        text=text,
        truncated=truncated,
        byte_length=len(text.encode("utf-8")),
    )


def status_of(
    executable: str,
    target: NativeTarget,
) -> dict[str, Any]:
    """Read-only native status for the exact qualified target (no effect)."""
    target = target.validated()
    row = qualify_target(executable, target, live_only=False)
    proc = run_native(
        executable,
        ["status", "--workspace", target.workspace, "--tag", target.tag, "--json"],
        timeout=QUICK_TIMEOUT,
    )
    if proc.returncode != 0:
        return {"session_id": row.session_id, "phase": row.phase, "status_proven": False}
    try:
        detail = json.loads(proc.stdout)
    except ValueError:
        return {"session_id": row.session_id, "phase": row.phase, "status_proven": False}
    if not isinstance(detail, dict):
        return {"session_id": row.session_id, "phase": row.phase, "status_proven": False}
    phase = str(detail.get("phase") or detail.get("state") or row.phase)
    return {
        "session_id": row.session_id,
        "phase": phase,
        "exit_info": detail.get("exit_info"),
        "status_proven": True,
    }


def _pid_dead(pid: Any) -> Optional[bool]:
    """Read-only liveness probe for one PID. True = dead, False = alive,
    None = unknown (no permission or unparsable). Never signals."""
    if type(pid) is not int or pid <= 0:
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    except (OSError, ValueError, OverflowError):
        return None
    return False


def _workload_containment_gone(cgroup: Optional[str]) -> Optional[bool]:
    """Read-only check that a previously observed workload cgroup path is gone.

    True = path absent (contained work reaped), False = still present,
    None = no usable path observed (unsupported). Only absolute paths under
    /sys/fs/cgroup are consulted; anything else is unsupported, never probed.
    """
    if not isinstance(cgroup, str) or not cgroup:
        return None
    norm = os.path.normpath(cgroup)
    if not norm.startswith("/sys/fs/cgroup/"):
        return None
    try:
        if not os.path.exists(norm):
            return True
        procs = os.path.join(norm, "cgroup.procs")
        if os.path.exists(procs):
            with open(procs, "r", encoding="utf-8", errors="replace") as f:
                return f.read().strip() == ""
        return False
    except (OSError, ValueError):
        return None


def _confirm_workload_exit(before: NativeSession) -> tuple[bool, str]:
    """Confirm the owned workload is actually gone (stop oracle).

    A missing catalog entry or a lone ``kill`` exit 0 is NOT confirmation.
    Stopped requires at least one positive containment/process proof from the
    observed row: the workload cgroup path reaped, every observed PID dead,
    or the catalog itself reporting containment empty. Anything else is
    unproven (unknown/unsupported oracle stays uncertain/held, never false
    stopped).
    """
    cgroup_gone = _workload_containment_gone(before.workload_cgroup)
    if cgroup_gone is False:
        return False, "workload cgroup still present after kill"
    if cgroup_gone is True:
        return True, "workload cgroup reaped after kill"
    pids = [p for p in (before.worker_pid, before.workload_pid) if type(p) is int]
    if pids:
        states = [_pid_dead(p) for p in pids]
        if any(s is False for s in states):
            return False, "observed workload PID still alive after kill"
        if all(s is True for s in states):
            return True, "observed workload PIDs dead after kill"
    if before.containment_empty is True:
        return True, "catalog reports containment empty after kill"
    return False, "no containment/process proof available; exit unproven"


STOP_ORACLE_VERSION = 1


def _oracle_from_row(row: NativeSession) -> dict[str, Any]:
    """Project the owned pre-stop containment oracle (bounded, private, no secrets).

    Captures the exact bound identity (UUID/tag/workspace/generation/engine)
    plus the owned workload liveness witnesses (PIDs, cgroup, containment
    flag, phases). No argv, no output, no tokens. Callers persist this BEFORE
    the kill result so a later read-only reconcile can confirm exit against
    the SAME claim without a second kill.
    """
    return {
        "version": STOP_ORACLE_VERSION,
        "session_id": row.session_id,
        "tag": row.tag,
        "workspace": row.workspace,
        "created_at_ms": row.created_at_ms,
        "engine": row.engine,
        "phase": row.phase,
        "worker_pid": row.worker_pid if type(row.worker_pid) is int else None,
        "workload_pid": row.workload_pid if type(row.workload_pid) is int else None,
        "processes": row.processes if type(row.processes) is int else None,
        "containment_empty": row.containment_empty if type(row.containment_empty) is bool else None,
        "workload_cgroup": row.workload_cgroup if isinstance(row.workload_cgroup, str) else None,
        "last_activity_ms": row.last_activity_ms if type(row.last_activity_ms) is int else None,
    }


def _row_from_oracle(oracle: dict[str, Any]) -> NativeSession:
    """Rebuild the observed row from a persisted oracle (for read-only confirm)."""
    return NativeSession(
        session_id=str(oracle.get("session_id", "")),
        tag=str(oracle.get("tag", "")),
        workspace=str(oracle.get("workspace", "")),
        engine=oracle.get("engine") if isinstance(oracle.get("engine"), str) else None,
        phase=str(oracle.get("phase", "unknown")),
        worker_pid=oracle.get("worker_pid") if type(oracle.get("worker_pid")) is int else None,
        workload_pid=oracle.get("workload_pid") if type(oracle.get("workload_pid")) is int else None,
        processes=oracle.get("processes") if type(oracle.get("processes")) is int else None,
        containment_empty=oracle.get("containment_empty") if type(oracle.get("containment_empty")) is bool else None,
        workload_cgroup=oracle.get("workload_cgroup") if isinstance(oracle.get("workload_cgroup"), str) else None,
        last_activity_ms=oracle.get("last_activity_ms") if type(oracle.get("last_activity_ms")) is int else None,
        created_at_ms=oracle.get("created_at_ms") if type(oracle.get("created_at_ms")) is int else None,
    )


def _uncertain_with_oracle(message: str, oracle: dict[str, Any],
                           kill: dict[str, Any]) -> SessionUncertain:
    """Attach the qualified pre-stop oracle + kill attempt/result (no secrets)."""
    exc = SessionUncertain(message)
    try:
        setattr(exc, "oracle", dict(oracle))
        setattr(exc, "kill", dict(kill))
    except Exception:
        pass
    return exc


def reconcile_stop_oracle(
    executable: str,
    oracle: dict[str, Any],
    kill: dict[str, Any],
) -> tuple[bool, str]:
    """Read-only reconcile of SAME stop claim against its qualified oracle.

    Never kills, never mutates: lists the catalog, binds the SAME UUID/tag/
    workspace/generation, guards PID-reuse/foreign/session-family (a live row
    with the same UUID but a different tag/workspace/generation is NOT our
    proof; a live PID is NOT exit), and confirms exit ONLY via the persisted
    oracle's containment witnesses (reaped cgroup, dead PIDs, catalog empty).
    An absent catalog record alone is NEVER exit proof.
    """
    if not isinstance(oracle, dict) or not oracle.get("session_id"):
        return False, "oracle unqualified; reconcile manually"
    try:
        sid = str(oracle["session_id"])
        tag = str(oracle["tag"])
        ws = str(oracle["workspace"])
        gen = oracle.get("created_at_ms")
    except (KeyError, TypeError, ValueError):
        return False, "oracle unqualified; reconcile manually"
    try:
        rows = [s for s in list_sessions(executable, include_exited=True)
                if s.session_id.lower() == sid.lower()
                and s.tag == tag
                and canonicalize_workspace(s.workspace) == ws]
    except (SessionControlError, SessionUncertain, OSError):
        # Catalog unreadable: cannot prove exit read-only; stay uncertain.
        return False, "catalog unreadable; exit unproven"
    if rows:
        row = rows[0]
        # SAME-claim guards: generation drift or engine change means the live
        # row is a different incarnation/family — not our exit proof.
        if type(gen) is int and row.created_at_ms != gen:
            return False, "live row generation differs; not our exit (no re-kill)"
        # A live non-terminal row is still running: capacity NOT released.
        # The catalog row is authoritative while it lives — never claim
        # stopped from PIDs/cgroup alone here (PID reuse / reaping race).
        # Only a terminal phase (below) or an absent record with a proven
        # oracle may confirm.
        if row.phase not in TERMINAL_PHASES:
            return False, "target still %s after kill; capacity NOT released (no re-kill)" % row.phase
        # Terminal row: confirm via CURRENT witnesses, falling back to the
        # persisted oracle's witnesses (same claim, same PIDs/cgroup).
        confirmed, why = _confirm_workload_exit(row)
        if confirmed:
            return True, "terminal phase observed: %s (%s)" % (row.phase, why)
        try:
            before = _row_from_oracle(oracle)
        except Exception:
            return False, "oracle unparsable; exit unproven"
        confirmed_o, why_o = _confirm_workload_exit(before)
        if confirmed_o:
            return True, "terminal row + persisted oracle confirm exit (%s)" % why_o
        return False, "target terminal (%s) but %s; reconcile (no re-kill)" % (row.phase, why)
    # Absent record: NEVER exit proof alone. Confirm ONLY via the persisted
    # oracle's live witnesses (cgroup reaped / PIDs dead / containment empty
    # observed BEFORE the kill, re-checked live now by _confirm_workload_exit).
    try:
        before = _row_from_oracle(oracle)
    except Exception:
        return False, "oracle unparsable; exit unproven"
    confirmed, why = _confirm_workload_exit(before)
    if confirmed:
        return True, "native record cleaned up after kill (%s)" % why
    return False, "native record gone after kill but %s; reconcile (no re-kill)" % why


def stop_session(
    executable: str,
    target: NativeTarget,
    signal: str = "HUP",
    grace_ms: int = 2000,
) -> StopResult:
    """Stop ONLY the authorized exact session and confirm its native exit.

    Binds UUID+workspace+tag (+engine when expected) before the effect; after
    ``kill``, the target UUID must be absent from the catalog (records cleaned
    up per native contract) or in a terminal phase, AND the owned workload
    must be proven gone (reaped cgroup, dead PIDs, or catalog containment
    empty). A still-running UUID — or a vanished record with no containment
    proof — is ``uncertain``, never silently released.

    Every uncertain path carries the qualified pre-stop oracle
    (``exc.oracle``) + kill attempt/result (``exc.kill``) so a later
    read-only :func:`reconcile_stop_oracle` can confirm the SAME claim
    without a second kill. Absent catalog alone is never exit proof.
    """
    target = target.validated()
    if signal not in ("HUP", "TERM"):
        raise SessionControlError("Signal must be HUP|TERM")
    if type(grace_ms) is not int or not 0 <= grace_ms <= 30000:
        raise SessionControlError("grace_ms must be 0..30000")
    before = [s for s in list_sessions(executable, include_exited=True)
              if s.session_id.lower() == target.session_id.lower()
              and s.tag == target.tag
              and canonicalize_workspace(s.workspace) == target.workspace]
    if not before:
        from .retired_native import verify_native_retirement
        is_retired, tombstone, reason = verify_native_retirement(executable, target)
        if is_retired and isinstance(tombstone, dict):
            return StopResult(
                state="stopped",
                session_id=target.session_id,
                reason="already confirmed retired: finished tombstone at %s (%s)" % (
                    tombstone.get("finished_at_ms"), reason
                ),
            )
        raise SessionControlError("Target not in native catalog (tag reuse or absent; %s); refusing" % reason)
    if target.created_at_ms is not None and before[0].created_at_ms != target.created_at_ms:
        raise SessionControlError("Native generation mismatch before kill; refusing")
    if target.engine is not None and before[0].engine != target.engine:
        raise SessionControlError(
            "Native engine mismatch (reported %r, expected %r); refusing"
            % (before[0].engine, target.engine)
        )
    generation = before[0].created_at_ms
    oracle = _oracle_from_row(before[0])
    attempt = {"signal": signal, "grace_ms": grace_ms}
    if before[0].phase in TERMINAL_PHASES:
        confirmed, why = _confirm_workload_exit(before[0])
        if confirmed:
            return StopResult(state="stopped", session_id=target.session_id,
                              reason="already terminal before kill: %s (%s)" % (before[0].phase, why))
        raise _uncertain_with_oracle(
            "Target terminal but workload exit unproven (%s); reconcile" % why,
            oracle, {**attempt, "killed": False, "result": "no_kill_terminal_unproven"})
    proc = run_native(
        executable,
        ["kill", "--workspace", target.workspace, "--tag", target.tag,
         "--signal", signal, "--grace-ms", str(grace_ms), "--json"],
        timeout=QUICK_TIMEOUT + grace_ms / 1000.0 + 10,
    )
    if proc.returncode != 0:
        # No exit proof: the session may be gone because it was forgotten/pruned
        # while running (forget claims no stop). Reconcile, never assume stopped.
        raise _uncertain_with_oracle(
            "Kill reported failure; target exit unproven — reconcile",
            oracle, {**attempt, "killed": False, "result": "kill_failed"})
    remaining = [s for s in list_sessions(executable, include_exited=True)
                   if s.session_id.lower() == target.session_id.lower()
                   and s.tag == target.tag
                   and canonicalize_workspace(s.workspace) == target.workspace]
    if not remaining:
        # Kill reported success AND the exact bound UUID/tag/workspace record is
        # gone. Absence WITHOUT our kill proof (forget/prune) is never exit
        # proof — that case refuses above, before any effect. Even with our
        # kill, absence alone is not confirmation: prove the workload gone.
        confirmed, why = _confirm_workload_exit(before[0])
        if confirmed:
            return StopResult(state="stopped", session_id=target.session_id,
                              reason="native record cleaned up after kill (%s)" % why)
        raise _uncertain_with_oracle(
            "Native record gone after kill but %s; reconcile" % why,
            oracle, {**attempt, "killed": True, "result": "record_absent_unproven"})
    if generation is not None and remaining[0].created_at_ms != generation:
        raise _uncertain_with_oracle(
            "Native generation changed across kill; exit unproven",
            oracle, {**attempt, "killed": True, "result": "generation_changed"})
    if remaining[0].phase in TERMINAL_PHASES:
        confirmed, why = _confirm_workload_exit(remaining[0])
        if confirmed:
            return StopResult(state="stopped", session_id=target.session_id,
                              reason="terminal phase observed: %s (%s)" % (remaining[0].phase, why))
        raise _uncertain_with_oracle(
            "Target terminal (%s) but %s; reconcile" % (remaining[0].phase, why),
            oracle, {**attempt, "killed": True, "result": "terminal_unproven"})
    raise _uncertain_with_oracle(
        "Target still %s after kill; capacity NOT released" % remaining[0].phase,
        oracle, {**attempt, "killed": True, "result": "still_running"})
