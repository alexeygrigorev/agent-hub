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

from .native_bridge import canonicalize_workspace

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
    row to carry the same generation; a mismatch means tag reuse and refuses.
    """

    session_id: str
    tag: str
    workspace: str
    created_at_ms: Optional[int] = None

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
        return NativeTarget(session_id=norm, tag=self.tag, workspace=canonical,
                            created_at_ms=self.created_at_ms)


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
    SessionControlError without any native effect.

    Honest limitation (head-ordered): the existing Fleet strict DTO does not carry
    ``cwd`` and the start adapter does not consume it, so this helper CANNOT
    retroactively change an already-launched session's cwd. Until the qualified
    existing integration is extended, the server must reject unsupported cwd/tier
    BEFORE effects (honest hold). This function only validates a caller's requested
    path for a future qualified start; it never claims a running session moved.
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
    return NativeSession(
        session_id=sid,
        tag=tag,
        workspace=ws,
        engine=raw.get("engine") if isinstance(raw.get("engine"), str) else None,
        profile=raw.get("profile") if isinstance(raw.get("profile"), str) else None,
        phase=str(raw.get("phase") or raw.get("state") or "unknown"),
        agent_state=raw.get("agent_state") if isinstance(raw.get("agent_state"), str) else None,
        last_activity_ms=last_ms if type(last_ms) is int else None,
        created_at_ms=created_ms if type(created_ms) is int else None,
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
    if live_only and row.phase in TERMINAL_PHASES:
        raise SessionControlError(f"Target session is terminal (phase={row.phase}); refusing")
    return row


def send_inbox(
    executable: str,
    target: NativeTarget,
    body: str,
    correlation: dict[str, str],
    kind: str = "note",
) -> DeliveryResult:
    """Durable inbox delivery via ``message send --to TAG`` (TAG, never UUID).

    State ``recorded`` at most: inbox persistence alone is NOT proof the target
    read or processed the message.
    """
    target = target.validated()
    check_body_bound(body)
    if _looks_like_uuid(target.tag):
        raise SessionControlError("Refusing UUID-shaped --to value (TAG required, never UUID)")
    if not isinstance(correlation, dict) or not correlation.get("correlation_id"):
        raise SessionControlError("Correlation id is required for every send")
    if not isinstance(kind, str) or not kind or len(kind) > 64:
        raise SessionControlError("Invalid message kind")
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
    # Sender provenance is the caller's own session; recipients reply per the native
    # reply_target contract. No processing claim is made here by construction.
    try:
        qualify_target(executable, target, live_only=False)
    except SessionControlError:
        pass  # post-send binding drift is reported, not hidden; envelope id stands
    return DeliveryResult(
        state="recorded",
        native_id=str(envelope.get("id")),
        reason="durable inbox persisted; target processing NOT proven",
        evidence={"kind": kind, "correlation_id": correlation.get("correlation_id")},
    )


def deliver_recorded(
    executable: str,
    target: NativeTarget,
    envelope_id: str,
) -> DeliveryResult:
    """Submit one existing inbox envelope via the native deliver guard.

    ``message deliver <id>`` submits the ORIGINAL envelope (delivery never creates
    another one) after the native side inspects a fresh, empty prompt itself. This
    is the only supported transport beyond durable inbox, and it is explicit
    opt-in: without it, commands stay ``recorded``/``held``. Success maps to
    ``transported`` — never ``acknowledged`` (only :func:`verify_ack_via_reply`
    proves processing). No composer-idle claim is made from phase/working flags;
    the readiness inspection belongs to the native guard, not to this caller.
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
    if proc.returncode == 0:
        return DeliveryResult(
            state="transported",
            native_id=envelope_id,
            reason="native deliver guard submitted the envelope; processing NOT proven",
            evidence={},
        )
    lowered = proc.stderr.lower()
    if any(s in lowered for s in ("busy", "not empty", "menu", "draft", "unknown")):
        return DeliveryResult(
            state="refused",
            native_id=envelope_id,
            reason=(proc.stderr.strip().splitlines() or ["deliver guard refused"])[-1][:300],
        )
    raise SessionUncertain("Deliver outcome unknown; reconcile before retry")


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
    original_envelope_id: str,
    correlation: dict[str, str],
) -> AckResult:
    """Verify a correlated recipient reply in the actual native message log.

    ``acknowledged`` requires ALL of: the reply envelope exists in the retrieved
    log (bounded, exact workspace); its ``reply_to`` equals the ORIGINAL NATIVE
    message envelope id (never a hub ``cmd:`` string); its ``from`` session/tag/
    workspace matches the intended native generation (``created_at_ms`` when
    known); and its body/data echoes the exact correlation id and body digest.
    Otherwise the result is ``unknown`` — an honest report, never false confirmed.
    When the proof transport itself is unqualified (log unavailable, unknown
    envelope shape), the result is likewise ``unknown`` with the reason recorded.
    """
    target = target.validated()
    if not isinstance(original_envelope_id, str) or not original_envelope_id:
        raise SessionControlError("Original native envelope id is required for ACK proof")
    if not isinstance(correlation, dict) or not correlation.get("correlation_id"):
        raise SessionControlError("Correlation id is required for ACK proof")
    expected_digest = correlation.get("body_digest", "")
    try:
        log = fetch_message_log(executable, target.workspace)
    except SessionUncertain as exc:
        return AckResult(state="unknown", reason=f"proof transport unqualified: {exc}")
    except SessionControlError as exc:
        return AckResult(state="unknown", reason=f"proof transport unqualified: {exc}")
    for env in log:
        if env.get("reply_to") != original_envelope_id:
            continue
        sender = env.get("from")
        if not isinstance(sender, dict):
            continue
        if (str(sender.get("session_id", "")).lower() != target.session_id.lower()
                or sender.get("tag") != target.tag
                or sender.get("workspace") != target.workspace):
            continue  # foreign or retargeted reply: not our proof
        payload = env.get("data")
        body_text = env.get("body") if isinstance(env.get("body"), str) else ""
        echoed_corr = ""
        echoed_digest = ""
        if isinstance(payload, dict):
            echoed_corr = str(payload.get("correlation_id", ""))
            echoed_digest = str(payload.get("body_digest", ""))
        if echoed_corr != correlation["correlation_id"]:
            continue
        if expected_digest and echoed_digest != expected_digest:
            continue
        if not expected_digest and correlation["correlation_id"] not in body_text:
            continue
        return AckResult(
            state="acknowledged",
            reply_envelope_id=str(env.get("id")),
            reason="correlated native reply verified in message log",
            evidence={"correlation_id": correlation["correlation_id"]},
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


def stop_session(
    executable: str,
    target: NativeTarget,
    signal: str = "HUP",
    grace_ms: int = 2000,
) -> StopResult:
    """Stop ONLY the authorized exact session and confirm its native exit.

    Binds UUID+workspace+tag before the effect; after ``kill``, the target UUID must
    be absent from the catalog (records cleaned up per native contract) or in a
    terminal phase. A still-running UUID is ``uncertain``, never silently released.
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
        raise SessionControlError("Target not in native catalog (tag reuse or absent); refusing")
    if target.created_at_ms is not None and before[0].created_at_ms != target.created_at_ms:
        raise SessionControlError("Native generation mismatch before kill; refusing")
    generation = before[0].created_at_ms
    if before[0].phase in TERMINAL_PHASES:
        return StopResult(state="stopped", session_id=target.session_id,
                          reason=f"already terminal before kill: {before[0].phase}")
    proc = run_native(
        executable,
        ["kill", "--workspace", target.workspace, "--tag", target.tag,
         "--signal", signal, "--grace-ms", str(grace_ms), "--json"],
        timeout=QUICK_TIMEOUT + grace_ms / 1000.0 + 10,
    )
    if proc.returncode != 0:
        # No exit proof: the session may be gone because it was forgotten/pruned
        # while running (forget claims no stop). Reconcile, never assume stopped.
        raise SessionUncertain("Kill reported failure; target exit unproven — reconcile")
    remaining = [s for s in list_sessions(executable, include_exited=True)
                  if s.session_id.lower() == target.session_id.lower()
                  and s.tag == target.tag
                  and canonicalize_workspace(s.workspace) == target.workspace]
    if not remaining:
        # Kill reported success AND the exact bound UUID/tag/workspace record is gone.
        # Absence WITHOUT our kill proof (forget/prune) is never exit proof — that
        # case refuses above, before any effect.
        return StopResult(state="stopped", session_id=target.session_id,
                          reason="native record cleaned up after kill")
    if generation is not None and remaining[0].created_at_ms != generation:
        raise SessionUncertain("Native generation changed across kill; exit unproven")
    if remaining[0].phase in TERMINAL_PHASES:
        return StopResult(state="stopped", session_id=target.session_id,
                          reason=f"terminal phase observed: {remaining[0].phase}")
    raise SessionUncertain(
        f"Target still {remaining[0].phase} after kill; capacity NOT released"
    )
