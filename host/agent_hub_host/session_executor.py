"""M12e session executor: durable intent + existing Fleet routing contract for start.

Start path delegates to the existing :class:`agent_hub_host.fleet.Dispatcher`
(same account/profile/workspace validation, same fresh admission gate, same
claim/result reporting, same ``a start`` argv). This module adds only:

- durable per-offer intent journal (idempotency key per offer, crash-safe),
- reconcile-before-retry (uncertain starts adopt the live native session, never
  blind-retry into a duplicate),
- custody + bounds checks shared with :mod:`agent_hub_host.native_sessions`,
- send/capture/stop wrappers with idempotent intents and correlated receipts,
- a pure :func:`handle_session_command` receiver that the head can wire later.

No server routes, CLI verbs, services, or timers are installed here. The ``request``
callable is injected (fake in tests, real hub HTTP from the API worker's wiring).
Server-side endpoints remain PROPOSED (see ``.local/session/DTO-PROPOSAL.md``) until
the head confirms the agreed DTO.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from . import private_fs
from .fleet import Dispatcher
from .native_sessions import (
    MAX_BODY_BYTES,
    DeliveryResult,
    NativeTarget,
    SessionControlError,
    SessionUncertain,
    capture_output as _capture,
    list_sessions,
    send_inbox as _send_inbox,
    send_prompt_guarded as _send_pty,
    status_of,
    stop_session as _stop,
)

JOURNAL_LIMIT = 64 * 1024
LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")


@dataclass(frozen=True)
class CallerCustody:
    """Host machine binding + current credential family for every operation."""

    actor: str
    machine_id: str
    credential_family: str

    def validated(self) -> "CallerCustody":
        for name, value in (("actor", self.actor), ("machine_id", self.machine_id),
                            ("credential_family", self.credential_family)):
            if not isinstance(value, str) or not LABEL.fullmatch(value):
                raise SessionControlError(f"Invalid caller custody field: {name}")
        return self


def _journal_path(journal_dir: str, name: str) -> str:
    if not isinstance(journal_dir, str) or not Path(journal_dir).is_absolute():
        raise SessionControlError("journal_dir must be an absolute path")
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{1,128}", name):
        raise SessionControlError("Invalid journal name")
    return os.path.join(journal_dir, name + ".json")


def _read_journal(path: str) -> Optional[dict[str, Any]]:
    try:
        raw = private_fs.read_bytes(path, JOURNAL_LIMIT)
    except FileNotFoundError:
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise SessionUncertain(f"Journal corrupt; reconcile before retry: {path}") from exc
    if not isinstance(data, dict):
        raise SessionUncertain(f"Journal malformed; reconcile before retry: {path}")
    return data


def _write_journal(path: str, record: dict[str, Any]) -> None:
    raw = json.dumps(record, sort_keys=True, ensure_ascii=False).encode("utf-8")
    private_fs.atomic_write(path, raw, JOURNAL_LIMIT)


def _check_custody(custody: CallerCustody) -> CallerCustody:
    if not isinstance(custody, CallerCustody):
        raise SessionControlError("Caller custody is required")
    return custody.validated()


def _target_from_offer_result(offer: dict[str, Any], result: dict[str, Any],
                               workspace_path: str) -> Optional[NativeTarget]:
    native_id = result.get("native_id")
    tag = "hub-" + str(offer.get("id", ""))
    if not isinstance(native_id, str) or not native_id:
        return None
    try:
        return NativeTarget(session_id=native_id, tag=tag, workspace=workspace_path).validated()
    except SessionControlError:
        return None


def reconcile_native_target(executable: str, target: NativeTarget) -> dict[str, Any]:
    """Reconcile one target against the live native catalog (read-only, no effect)."""
    target = target.validated()
    rows = [s for s in list_sessions(executable, include_exited=True)
            if s.session_id.lower() == target.session_id.lower()]
    if not rows:
        return {"session_id": target.session_id, "known": False, "phase": "absent"}
    row = rows[0]
    return {
        "session_id": row.session_id,
        "known": True,
        "phase": row.phase,
        "tag": row.tag,
        "workspace": row.workspace,
        "tag_matches": row.tag == target.tag,
        "workspace_matches": row.workspace == target.workspace,
    }


def start_from_offer(
    settings: dict[str, Any],
    offer: dict[str, Any],
    *,
    custody: CallerCustody,
    runner: Callable,
    request: Callable,
    journal_dir: str,
    executable: str = "aplexer",
) -> dict[str, Any]:
    """Start one session from a Fleet offer through the existing Dispatcher contract.

    Idempotent per ``(offer id, request_key)``: a repeated call with the same key
    returns the journaled outcome (reconciled against live native state) without a
    second native start. A conflicting payload raises (409-equivalent). An
    ``uncertain`` journal is reconciled first; an adopted live session is reported
    back through the existing ``.../result`` shape, never blind-retried.
    """
    _check_custody(custody)
    if not isinstance(settings, dict) or not isinstance(offer, dict):
        raise SessionControlError("settings and offer are required")
    offer_id = offer.get("id")
    if not isinstance(offer_id, str) or not offer_id or len(offer_id) > 128:
        raise SessionControlError("Offer id is required")
    request_key = offer.get("request_key", offer_id)
    if not isinstance(request_key, str) or not request_key:
        raise SessionControlError("Request key is required")
    workspace_label = offer.get("workspace")
    workspaces = settings.get("workspaces", {})
    if not isinstance(workspace_label, str) or workspace_label not in workspaces:
        raise SessionControlError("Offer workspace is not configured locally")
    workspace_path = str(workspaces[workspace_label])

    journal_file = _journal_path(journal_dir, "offer-" + offer_id)
    prior = _read_journal(journal_file)
    if prior is not None:
        if prior.get("request_key") != request_key:
            raise SessionControlError("Idempotency key conflict for offer (no partial mutation)")
        return _reconcile_journal(
            executable, prior, offer, workspace_path, request, journal_file,
        )

    dispatcher = Dispatcher(settings, request=request, runner=runner)
    try:
        result = dispatcher.dispatch(offer)
    except SessionUncertain:
        raise
    except SessionControlError:
        raise
    except ValueError as exc:
        # Admission/route refusal: held honestly, no native effect, journaled.
        record = {"offer_id": offer_id, "request_key": request_key, "state": "held",
                  "reason": str(exc)[:300], "native_id": None}
        _write_journal(journal_file, record)
        return {"id": offer_id, **record}
    except Exception as exc:  # unexpected runner failure: uncertain, reconcile later
        record = {"offer_id": offer_id, "request_key": request_key, "state": "uncertain",
                  "reason": f"{type(exc).__name__}", "native_id": None}
        _write_journal(journal_file, record)
        raise SessionUncertain(record["reason"]) from exc

    if not isinstance(result, dict):
        raise SessionUncertain("Dispatcher returned no result; reconcile before retry")
    record = {"offer_id": offer_id, "request_key": request_key,
              "state": str(result.get("state", "uncertain")),
              "reason": str(result.get("reason", ""))[:300],
              "native_id": result.get("native_id") if isinstance(result.get("native_id"), str) else None}
    _write_journal(journal_file, record)
    return {"id": offer_id, **record}


def _reconcile_journal(
    executable: str,
    prior: dict[str, Any],
    offer: dict[str, Any],
    workspace_path: str,
    request: Callable,
    journal_file: str,
) -> dict[str, Any]:
    """Reconcile a journaled offer outcome against live native state (no blind retry)."""
    state = prior.get("state")
    native_id = prior.get("native_id")
    offer_id = prior.get("offer_id")
    if state == "uncertain" and not native_id:
        # No receipt: adopt ONLY if the offer's unique tag resolves to exactly one
        # live session in the workspace (hub-<offer> tags are unique by construction).
        try:
            rows = [s for s in list_sessions(executable, workspace=workspace_path)
                    if s.tag == "hub-" + str(offer.get("id", "")) and s.phase not in
                    ("exited", "dead", "forgotten", "pruned")]
        except SessionControlError:
            rows = []
        if len(rows) == 1:
            native_id = rows[0].session_id
            prior = {**prior, "native_id": native_id}
            _write_journal(journal_file, prior)
        else:
            return {"id": offer_id, **prior, "reconciled": "tag-ambiguous-still-uncertain"}
    if state in ("running", "uncertain") and isinstance(native_id, str) and native_id:
        try:
            target = NativeTarget(
                session_id=native_id, tag="hub-" + str(offer.get("id", "")),
                workspace=workspace_path,
            ).validated()
        except SessionControlError:
            return {"id": offer_id, **prior, "reconciled": "target-invalid"}
        seen = reconcile_native_target(executable, target)
        if seen["known"] and seen["phase"] not in ("exited", "dead", "forgotten", "pruned"):
            if state == "uncertain":
                # Adopt, do not restart: report through the existing result shape.
                try:
                    request("POST", "/api/fleet/launches/" + offer_id + "/result",
                            {"state": "running", "native_id": native_id})
                except Exception:
                    pass
                prior = {**prior, "state": "running", "adopted": True}
                _write_journal(journal_file, prior)
            return {"id": offer_id, **prior, "reconciled": "live-session-adopted"}
        if state == "uncertain" and not seen["known"]:
            return {"id": offer_id, **prior, "reconciled": "absent-uncertain-held"}
    return {"id": offer_id, **prior, "reconciled": "journal-replay-no-effect"}


def send_prompt(
    executable: str,
    target: NativeTarget,
    body: str,
    *,
    custody: CallerCustody,
    transport: str = "inbox",
    correlation_id: Optional[str] = None,
    idempotency_key: Optional[str] = None,
    journal_dir: Optional[str] = None,
    reconciled: bool = False,
    wait_ack_secs: int = 10,
) -> dict[str, Any]:
    """Idempotent guarded send with durable intent and correlated receipts.

    - ``inbox``: durable ``message send`` only → ``recorded`` at most.
    - ``pty-guarded``: ``send --enter`` verdict mapping → ``transported`` /
      ``acknowledged`` / ``refused``; ``uncertain`` raises.
    - Same ``idempotency_key`` + same body replays the journaled outcome with no new
      effect. Same key + different body raises (conflict). A retry of an
      ``attempted`` PTY intent without ``reconciled=True`` evidence raises
      ``SessionUncertain`` (no blind resend).
    """
    _check_custody(custody)
    target = target.validated() if isinstance(target, NativeTarget) else None
    if target is None:
        raise SessionControlError("Exact native target binding is required")
    if transport not in ("inbox", "pty-guarded"):
        raise SessionControlError("transport must be inbox|pty-guarded")
    if not isinstance(body, str) or len(body.encode("utf-8")) > MAX_BODY_BYTES or not body.strip():
        raise SessionControlError(f"Body must be non-empty within {MAX_BODY_BYTES} bytes")
    key = idempotency_key or str(uuid.uuid4())
    corr = correlation_id or key
    journal_file = _journal_path(journal_dir, "send-" + key) if journal_dir else None
    prior = _read_journal(journal_file) if journal_file else None
    if prior is not None:
        if prior.get("body") != body or prior.get("transport") != transport:
            raise SessionControlError("Send idempotency conflict (no partial mutation)")
        if (
            transport == "pty-guarded"
            and prior.get("attempted")
            and prior.get("state") in (None, "uncertain")
            and not reconciled
        ):
            raise SessionUncertain("PTY intent already attempted; reconcile before resend")
        if not (transport == "pty-guarded" and reconciled and prior.get("state") in (None, "uncertain")):
            return {**prior, "replayed": True}
    if journal_file:
        _write_journal(journal_file, {"key": key, "transport": transport, "body": body,
                                      "attempted": True, "correlation_id": corr,
                                      "target": {"session_id": target.session_id,
                                                 "tag": target.tag,
                                                 "workspace": target.workspace}})
    correlation = {"correlation_id": corr, "idempotency_key": key}
    if transport == "inbox":
        result: DeliveryResult = _send_inbox(executable, target, body, correlation)
    else:
        result = _send_pty(executable, target, body, correlation, wait_ack_secs=wait_ack_secs)
    record = {"key": key, "transport": transport, "body": body, "attempted": True,
              "correlation_id": corr, "state": result.state,
              "native_id": result.native_id, "reason": result.reason,
              "target": {"session_id": target.session_id, "tag": target.tag,
                         "workspace": target.workspace}}
    if journal_file:
        _write_journal(journal_file, record)
    return {**record}


def capture_session_output(
    executable: str,
    target: NativeTarget,
    *,
    custody: CallerCustody,
    limit_bytes: int = 32 * 1024,
    mode: str = "tail",
) -> dict[str, Any]:
    """Bounded capture for the exact qualified target (read-only, no upload)."""
    _check_custody(custody)
    if not isinstance(target, NativeTarget):
        raise SessionControlError("Exact native target binding is required")
    result = _capture(executable, target, limit_bytes=limit_bytes, mode=mode)
    return {"session_id": result.session_id, "tag": result.tag, "mode": result.mode,
            "text": result.text, "truncated": result.truncated,
            "byte_length": result.byte_length}


def stop_controlled_session(
    executable: str,
    target: NativeTarget,
    *,
    custody: CallerCustody,
) -> dict[str, Any]:
    """Stop only the authorized exact session; confirm exit, never silently release."""
    _check_custody(custody)
    if not isinstance(target, NativeTarget):
        raise SessionControlError("Exact native target binding is required")
    try:
        result = _stop(executable, target)
    except SessionUncertain:
        raise
    except SessionControlError:
        # Wrong target or absent UUID: refuse without effect. Absent is NOT exit
        # proof (forget/prune removes records without stopping workloads).
        raise
    return {"session_id": result.session_id, "state": result.state, "reason": result.reason}


def handle_session_command(
    command: dict[str, Any],
    *,
    custody: CallerCustody,
    settings: Optional[dict[str, Any]] = None,
    runner: Optional[Callable] = None,
    request: Optional[Callable] = None,
    journal_dir: Optional[str] = None,
    executable: str = "aplexer",
) -> dict[str, Any]:
    """Pure receiver command function for later head wiring (no service installed).

    ``command`` is ``{"op": "list"|"start"|"send"|"capture"|"stop", ...}`` with
    op-specific fields. All side effects flow through the injected ``runner`` /
    ``request`` / native ``executable``. Unknown ops raise; nothing is installed.
    """
    _check_custody(custody)
    if not isinstance(command, dict) or command.get("op") not in (
        "list", "start", "send", "capture", "stop",
    ):
        raise SessionControlError("Unknown session command op")

    def need(name: str) -> Any:
        value = command.get(name)
        if value is None:
            raise SessionControlError(f"Command field required: {name}")
        return value

    op = command["op"]
    if op == "list":
        if settings is None:
            raise SessionControlError("settings required for list projection")
        workspace = command.get("workspace")
        sessions = list_sessions(executable, workspace=workspace)
        return {"op": "list", "sessions": [
            {"session_id": s.session_id, "tag": s.tag, "workspace": s.workspace,
             "engine": s.engine, "profile": s.profile, "phase": s.phase,
             "agent_state": s.agent_state, "last_activity_ms": s.last_activity_ms}
            for s in sessions[:200]]}
    if op == "start":
        if settings is None or runner is None or request is None or journal_dir is None:
            raise SessionControlError("start requires settings/runner/request/journal_dir")
        return {"op": "start", **start_from_offer(
            settings, need("offer"), custody=custody, runner=runner,
            request=request, journal_dir=journal_dir, executable=executable)}
    if op == "send":
        if journal_dir is None:
            raise SessionControlError("send requires journal_dir")
        raw_target = need("target")
        target = NativeTarget(session_id=raw_target["session_id"], tag=raw_target["tag"],
                              workspace=raw_target["workspace"])
        return {"op": "send", **send_prompt(
            executable, target, need("body"), custody=custody,
            transport=command.get("transport", "inbox"),
            correlation_id=command.get("correlation_id"),
            idempotency_key=command.get("idempotency_key"),
            journal_dir=journal_dir,
            reconciled=command.get("reconciled", False) is True)}
    if op == "capture":
        raw_target = need("target")
        target = NativeTarget(session_id=raw_target["session_id"], tag=raw_target["tag"],
                              workspace=raw_target["workspace"])
        return {"op": "capture", **capture_session_output(
            executable, target, custody=custody,
            limit_bytes=int(command.get("limit_bytes", 32 * 1024)),
            mode=command.get("mode", "tail"))}
    raw_target = need("target")
    target = NativeTarget(session_id=raw_target["session_id"], tag=raw_target["tag"],
                          workspace=raw_target["workspace"])
    return {"op": "stop", **stop_controlled_session(executable, target, custody=custody)}


def session_status(
    executable: str,
    target: NativeTarget,
    *,
    custody: CallerCustody,
) -> dict[str, Any]:
    """Read-only status probe for the exact qualified target (reconciliation aid)."""
    _check_custody(custody)
    if not isinstance(target, NativeTarget):
        raise SessionControlError("Exact native target binding is required")
    return status_of(executable, target)
