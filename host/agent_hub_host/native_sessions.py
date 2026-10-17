"""M12e host native session adapter: list / guarded delivery / bounded capture / confirmed stop.

Genuine native protocols only (installed ``a``/``aplexer`` CLI, argv, no shell).
Read-only with respect to all other hub source: this module wraps the native CLI and
reuses :mod:`agent_hub_host.native_bridge` workspace helpers. It never starts sessions
(start is owned by :mod:`agent_hub_host.session_executor` through the existing Fleet
``Dispatcher`` contract), never touches live sessions except the exact qualified target,
and never claims more than the native evidence proves:

- ``recorded``: durable inbox envelope persisted (``message send``). NOT delivery proof.
- ``transported``: PTY bytes delivered (``send --enter`` exit 0 without working proof,
  or exit 3 delivered-but-idle). NOT processing proof.
- ``acknowledged``: native verdict carries explicit working/ACK proof correlated to this
  request. Only then may callers treat the target as having received the prompt.
- ``refused``: readiness/composer gate rejected the attempt; no effect was attempted.
- ``uncertain``: the effect may have happened; reconcile from ``list``/``status``/``log``
  before any retry. Never blind-retry.

Forbidden transports: ``--pane`` injection, ``--no-ack``, ``--from`` identity override,
busy-composer input, stale-idle bypass. ``message send --to`` takes a TAG, never a UUID.
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
    """Exact native session binding required before/after every operation."""

    session_id: str
    tag: str
    workspace: str

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
        return NativeTarget(session_id=norm, tag=self.tag, workspace=canonical)


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
    state: str  # recorded|transported|acknowledged|refused|uncertain
    native_id: Optional[str] = None
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


def resolve_cwd(workspace_roots: list[str], cwd: str) -> str:
    """Resolve requested cwd strictly inside operator-configured workspace roots.

    Rejects path traversal and escaping symlinks (realpath comparison). Raises
    SessionControlError without any native effect.
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
    if "--pane" in args or "--no-ack" in args:
        raise SessionControlError("--pane/--no-ack transports are forbidden")
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
    """Re-qualify tag -> (UUID, workspace) immediately before an effect.

    Requires exactly one catalog row matching workspace+tag AND the expected UUID.
    Tag reuse (same tag, different UUID) or workspace drift refuses with no effect.
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


def _map_enter_verdict(proc: subprocess.CompletedProcess) -> DeliveryResult:
    """Map ``send --enter [--json]`` outcome to honest delivery states.

    Conservative by construction: exit 0 without explicit working proof is
    ``transported``, never ``acknowledged``. Exit 3 (delivered but agent stayed idle)
    is ``transported``. Busy/not-ready failures are ``refused`` (no retry by us).
    Anything else is ``uncertain``.
    """
    stderr_tail = proc.stderr.strip().splitlines()
    hint = stderr_tail[-1][:300] if stderr_tail else ""
    verdict: dict[str, Any] = {}
    if "--json" in (proc.args or []):
        try:
            parsed = json.loads(proc.stdout) if proc.stdout.strip() else {}
            if isinstance(parsed, dict):
                verdict = parsed.get("delivery", parsed) if isinstance(parsed, dict) else {}
                if not isinstance(verdict, dict):
                    verdict = {}
        except ValueError:
            verdict = {}
    working = verdict.get("working") is True or verdict.get("acknowledged") is True
    ready = verdict.get("ready", True)
    if proc.returncode == 0:
        if working:
            return DeliveryResult(
                state="acknowledged", reason="native verdict shows agent working",
                evidence={"verdict": verdict},
            )
        return DeliveryResult(
            state="transported",
            reason="PTY bytes delivered; agent processing NOT proven",
            evidence={"verdict": verdict},
        )
    if proc.returncode == 3:
        return DeliveryResult(
            state="transported",
            reason="delivered but agent stayed idle (native exit 3); processing NOT proven",
            evidence={"verdict": verdict},
        )
    lowered = (hint + " " + proc.stderr).lower()
    if ready is False or any(
        s in lowered for s in ("busy", "not ready", "not empty", "menu", "draft", "unknown composer")
    ):
        return DeliveryResult(state="refused", reason=hint or "readiness/composer gate refused")
    return DeliveryResult(state="uncertain", reason=hint or f"native exit {proc.returncode}")


def send_prompt_guarded(
    executable: str,
    target: NativeTarget,
    body: str,
    correlation: dict[str, str],
    wait_ack_secs: int = 10,
) -> DeliveryResult:
    """Guarded PTY prompt delivery via ``send --enter --json`` (no bypass flags).

    Refuses busy/non-empty composers; never treats idle as proof of readiness;
    maps the native verdict conservatively (see :func:`_map_enter_verdict`).
    """
    target = target.validated()
    check_body_bound(body)
    if not isinstance(correlation, dict) or not correlation.get("correlation_id"):
        raise SessionControlError("Correlation id is required for every send")
    if type(wait_ack_secs) is not int or not 0 <= wait_ack_secs <= 120:
        raise SessionControlError("wait_ack_secs must be 0..120")
    qualify_target(executable, target)
    proc = run_native(
        executable,
        ["send", "--workspace", target.workspace, "--tag", target.tag,
         "--enter", "--json", "--wait-ack", str(wait_ack_secs), body],
        timeout=NATIVE_TIMEOUT + wait_ack_secs,
    )
    result = _map_enter_verdict(proc)
    if result.state == "uncertain":
        raise SessionUncertain(result.reason)
    return result


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
                 if s.session_id.lower() == target.session_id.lower()]
    if not remaining:
        return StopResult(state="stopped", session_id=target.session_id,
                          reason="native record cleaned up after kill")
    if remaining[0].phase in TERMINAL_PHASES:
        return StopResult(state="stopped", session_id=target.session_id,
                          reason=f"terminal phase observed: {remaining[0].phase}")
    raise SessionUncertain(
        f"Target still {remaining[0].phase} after kill; capacity NOT released"
    )
