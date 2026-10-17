"""M12e session executor: durable intent + existing Fleet routing contract for start.

Start path delegates to the existing :class:`agent_hub_host.fleet.Dispatcher`
(same account/profile/workspace validation, same fresh admission gate, same
claim/result reporting, same ``a start`` argv). This module adds only:

- durable per-command intent journal with per-journal exclusive locks
  (``private_fs.exclusive_lock``; concurrent processes serialize per intent,
  contention reports ``uncertain`` instead of duplicating effects),
- reconcile-before-retry (uncertain starts adopt the live native session, never
  blind-retry into a duplicate),
- custody + bounds checks shared with :mod:`agent_hub_host.native_sessions`,
- inbox-only send / bounded capture / confirmed stop with idempotent intents,
- :func:`handle_claimed_command`: the shared server-claimed receiver the head
  wires to the agreed endpoints (claim token + holder + generation + expiry,
  journal attempt durable BEFORE effect, ack via injected ``request``).

R2 corrections (head-ordered): raw ``send --enter`` is retired (its working
verdict is not ACK proof); ``transport="pty-guarded"`` now holds honestly.
``start_from_offer`` stays a pure compat helper for the EXISTING Fleet consumer
only — this module never polls Fleet offers and the claimed receiver rejects
``start`` (Fleet already handles start once). No server routes, CLI verbs,
services, or timers are installed here. The ``request`` callable is injected
(fake in tests, real hub HTTP from the API worker's wiring). Server-side claim/
ack field names follow the head-chosen interface (see ``docs/SESSION-HOST.md``
and ``.local/session/HOST-R2-INTERFACE.md``).
"""

from __future__ import annotations

import json
import os
import re
import time
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
    body_digest,
    capture_output as _capture,
    deliver_recorded,
    list_sessions,
    send_inbox as _send_inbox,
    status_of,
    stop_session as _stop,
    verify_ack_via_reply,
)

JOURNAL_LIMIT = 64 * 1024
LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
# Canonical bus actor (src/bus.ts parseAddress): `tag` or `project:tag`, one colon
# max, segments non-empty without whitespace/controls, 128 chars max. Custody text
# grants no privilege; the server enforces family/incarnation/revocation.
ACTOR = re.compile(r"[^\s:\x00-\x1f\x7f]{1,64}(?::[^\s:\x00-\x1f\x7f]{1,63})?\Z")


@dataclass(frozen=True)
class CallerCustody:
    """Host machine binding + current credential family for every operation.

    ``actor`` follows the existing canonical actor semantics (``tag`` or
    project-qualified ``project:tag``); ``machine_id``/``credential_family`` stay
    strict labels. Family, registry incarnation, rebinding and revocation are
    enforced server-side and passed bound to the host — never guessed here.
    """

    actor: str
    machine_id: str
    credential_family: str

    def validated(self) -> "CallerCustody":
        if not isinstance(self.actor, str) or len(self.actor) > 128 or not ACTOR.fullmatch(self.actor):
            raise SessionControlError("Invalid caller custody field: actor")
        for name, value in (("machine_id", self.machine_id),
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


def _locked(path: str):
    """Per-intent exclusive lock (scoped journal lock, never a second global lock).

    Serializes concurrent processes holding the same journal file. Non-blocking:
    contention raises ``SessionUncertain`` (a peer owns this intent; reconcile,
    never duplicate the effect). The kernel releases the lock on process death.
    """
    return private_fs.exclusive_lock(path + ".lock")


def _lock_held(path: str):
    try:
        return _locked(path)
    except BlockingIOError as exc:
        raise SessionUncertain(
            "Intent locked by a concurrent process; reconcile, do not duplicate"
        ) from exc
    except OSError as exc:
        raise SessionUncertain(
            "Intent lock unavailable; reconcile, do not duplicate"
        ) from exc


def _check_custody(custody: CallerCustody) -> CallerCustody:
    if not isinstance(custody, CallerCustody):
        raise SessionControlError("Caller custody is required")
    return custody.validated()


def verify_server_claim(claim: dict[str, Any], *, now_ms: Optional[int] = None) -> dict[str, Any]:
    """Verify a server-issued single-attempt claim BEFORE any native effect.

    Required fields (head-chosen interface, server canonical): ``command_id``,
    ``claim_token`` (unpredictable, >= 16 chars), ``holder``, ``generation``,
    and expiry as EITHER ``expires_at_ms`` (int ms, host clock) OR
    ``expires_at`` (ISO-8601 string, server canonical). The server returns both
    (see src/session_control.ts claim route); the host accepts either and
    normalizes to ``expires_at_ms``. An expired or malformed claim refuses with
    NO effect. A repeated claim token never authorizes another execution — the
    journal binds the token per key. Expiry uses the caller's clock only to
    fail closed; the server is the authority on grant and revocation.
    """
    if not isinstance(claim, dict):
        raise SessionControlError("Server claim is required before any effect")
    command_id = claim.get("command_id")
    token = claim.get("claim_token")
    holder = claim.get("holder")
    generation = claim.get("generation")
    expires_at = claim.get("expires_at_ms")
    if expires_at is None and isinstance(claim.get("expires_at"), str):
        try:
            from datetime import datetime, timezone
            text = claim["expires_at"]
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            expires_at = int(parsed.timestamp() * 1000)
        except (ValueError, OverflowError):
            expires_at = None
    if not isinstance(command_id, str) or not command_id or len(command_id) > 128:
        raise SessionControlError("Claim command_id is required")
    if not isinstance(token, str) or len(token) < 16 or len(token) > 256:
        raise SessionControlError("Claim token is missing or too weak; refusing")
    if not isinstance(holder, str) or not holder or len(holder) > 128:
        raise SessionControlError("Claim holder is required")
    if type(generation) is not int or generation < 0:
        raise SessionControlError("Claim generation is required")
    if type(expires_at) is not int or expires_at <= 0:
        raise SessionControlError("Claim expiry is required")
    now = now_ms if type(now_ms) is int else int(time.time() * 1000)
    if now >= expires_at:
        raise SessionControlError("Claim expired; no effect, request a fresh attempt")
    return {"command_id": command_id, "claim_token": token, "holder": holder,
            "generation": generation, "expires_at_ms": expires_at}


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

    Pure compat helper for the EXISTING Fleet consumer path only: the caller passes
    one already-authorized offer (server-side reservation flow). This function never
    polls Fleet offers and the claimed-command receiver rejects ``start`` — Fleet
    already handles start once, and no second consumer is added here.

    Idempotent per ``(offer id, request_key)``: a repeated call with the same key
    returns the journaled outcome (reconciled against live native state) without a
    second native start. A conflicting payload raises (409-equivalent). An
    ``uncertain`` journal is reconciled first; an adopted live session is reported
    back through the existing ``.../result`` shape, never blind-retried. The
    read-modify-write holds the per-offer exclusive lock across processes.
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
    with _lock_held(journal_file):
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
    """Idempotent inbox send with durable intent and correlated receipts.

    - ``inbox``: durable ``message send`` only → ``recorded`` at most. Inbox
      persistence is never rendered as transported or acknowledged.
    - ``pty-guarded``: RETIRED (R2 head correction — the ``send --enter``
      working verdict is not target-read/process proof). Requesting it raises a
      held refusal with no native effect; callers use ``inbox``.
    - Same ``idempotency_key`` + same body replays the journaled outcome with no
      new effect (the original native envelope id is retained). Same key +
      different body raises (conflict). A retry of an ``attempted``-uncertain
      intent without ``reconciled=True`` raises ``SessionUncertain`` (no blind
      resend). The read-modify-write holds the per-key exclusive lock.
    """
    _check_custody(custody)
    target = target.validated() if isinstance(target, NativeTarget) else None
    if target is None:
        raise SessionControlError("Exact native target binding is required")
    if transport not in ("inbox", "pty-guarded"):
        raise SessionControlError("transport must be inbox|pty-guarded")
    if transport == "pty-guarded":
        raise SessionControlError(
            "held: raw PTY send retired — working verdict is not ACK proof; use inbox"
        )
    if not isinstance(body, str) or len(body.encode("utf-8")) > MAX_BODY_BYTES or not body.strip():
        raise SessionControlError(f"Body must be non-empty within {MAX_BODY_BYTES} bytes")
    key = idempotency_key or str(uuid.uuid4())
    corr = correlation_id or key
    digest = body_digest(body)
    journal_file = _journal_path(journal_dir, "send-" + key) if journal_dir else None

    def _do() -> dict[str, Any]:
        prior = _read_journal(journal_file) if journal_file else None
        if prior is not None:
            if prior.get("body_digest") != digest or prior.get("transport") != transport:
                raise SessionControlError("Send idempotency conflict (no partial mutation)")
            if prior.get("attempted") and prior.get("state") in (None, "uncertain") and not reconciled:
                raise SessionUncertain("Send intent already attempted; reconcile before resend")
            if not (reconciled and prior.get("state") in (None, "uncertain")):
                return {**prior, "replayed": True}
        if journal_file:
            _write_journal(journal_file, {
                "op": "send", "key": key, "transport": transport,
                "body_digest": digest, "attempted": True, "correlation_id": corr,
                "caller_actor": custody.actor, "caller_family": custody.credential_family,
                "target": {"session_id": target.session_id, "tag": target.tag,
                           "workspace": target.workspace,
                           "created_at_ms": target.created_at_ms}})
        correlation = {"correlation_id": corr, "idempotency_key": key, "body_digest": digest}
        result: DeliveryResult = _send_inbox(executable, target, body, correlation)
        record = {"op": "send", "key": key, "transport": transport,
                  "body_digest": digest, "attempted": True,
                  "correlation_id": corr, "state": result.state,
                  "native_id": result.native_id, "reason": result.reason,
                  "caller_actor": custody.actor, "caller_family": custody.credential_family,
                  "target": {"session_id": target.session_id, "tag": target.tag,
                             "workspace": target.workspace,
                             "created_at_ms": target.created_at_ms}}
        if journal_file:
            _write_journal(journal_file, record)
        return {**record}

    if journal_file:
        with _lock_held(journal_file):
            return _do()
    return _do()


def capture_session_output(
    executable: str,
    target: NativeTarget,
    *,
    custody: CallerCustody,
    limit_bytes: int = 32 * 1024,
    mode: str = "tail",
) -> dict[str, Any]:
    """Bounded capture for the exact qualified opt-in controlled session.

    Read-only native effect, private no-store bounded UTF-8 projection returned
    to the caller (never uploaded anywhere here). Privacy stays explicitly
    unknown — terminal output can contain secrets and no regex proves safety —
    and truncation is explicit. Capture without declared projection/authorization
    must be rejected by the caller; this function performs no authorization
    itself and logs no output in errors.
    """
    _check_custody(custody)
    if not isinstance(target, NativeTarget):
        raise SessionControlError("Exact native target binding is required")
    result = _capture(executable, target, limit_bytes=limit_bytes, mode=mode)
    return {"session_id": result.session_id, "tag": result.tag, "mode": result.mode,
            "text": result.text, "truncated": result.truncated,
            "byte_length": result.byte_length, "privacy_unknown": True}


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


def handle_claimed_command(
    command: dict[str, Any],
    claim: dict[str, Any],
    *,
    custody: CallerCustody,
    journal_dir: str,
    executable: str = "aplexer",
    request: Optional[Callable] = None,
    now_ms: Optional[int] = None,
) -> dict[str, Any]:
    """Shared server-claimed receiver (exact function the API worker wires).

    ``command`` is ``{"op": "send"|"capture"|"stop", "command_id": ..., ...}``
    with op-specific fields; ``claim`` is the server-issued single attempt
    (``command_id``, ``claim_token``, ``holder``, ``generation``, ``expires_at_ms``;
    see :func:`verify_server_claim`). ``request`` is the injected hub HTTP
    callable used for the ``.../ack`` report (fake in tests); when ``None``, the
    ack payload is returned for the API worker's wiring to submit.

    Flow per attempt, all under the per-command exclusive lock:

    1. Verify the claim (token strength, holder, generation, expiry) — BEFORE
       any native effect. Expired/malformed claims refuse with no effect.
    2. Read the journal. Same key + different payload digests reject (conflict).
       A journaled claim token that differs from the incoming token never
       authorizes another execution: a completed journal replays, an
       attempted-uncertain journal stays uncertain until reconciled.
    3. Journal the attempt (canonical intent + caller family + target
       incarnation + operation + key + claim attempt) BEFORE the effect.
    4. Run the native effect for the exact qualified target only.
    5. Journal the outcome and report ``.../ack`` with the claim token
       (``outcome`` is ``delivered`` only with correlated reply proof — else the
       honest ``recorded``/``transported``/``refused``/``uncertain`` state;
       ``acknowledged`` lives in the reply-proof field, never as a bare claim).

    ``start`` is explicitly REJECTED here: Fleet already handles start once
    (see :func:`start_from_offer`); this receiver never reconsumes Fleet offers
    or invokes start. ``GET .../offers`` is discovery/transport only and never
    reaches this function without a granted claim.

    Exact result DTO: ``{"op", "command_id", "state", "native_id"?,
    "reply_envelope_id"?, "reason", "ack": {...submitted or to-submit...}}``.
    """
    _check_custody(custody)
    if not isinstance(command, dict):
        raise SessionControlError("Command is required")
    op = command.get("op")
    if op == "start":
        raise SessionControlError("held: start is Fleet-owned; claimed receiver takes send|capture|stop")
    if op not in ("send", "capture", "stop"):
        raise SessionControlError("Unknown session command op")
    proved = verify_server_claim(claim, now_ms=now_ms)
    command_id = command.get("command_id")
    if not isinstance(command_id, str) or command_id != proved["command_id"]:
        raise SessionControlError("Command id must match the granted claim")
    if proved["generation"] != int(command.get("generation", proved["generation"]) or 0) and \
            "generation" in command and command.get("generation") != proved["generation"]:
        raise SessionControlError("Command generation must match the granted claim")
    raw_target = command.get("target")
    if not isinstance(raw_target, dict):
        raise SessionControlError("Exact native target binding is required")
    try:
        target = NativeTarget(
            session_id=raw_target["session_id"], tag=raw_target["tag"],
            workspace=raw_target["workspace"],
            created_at_ms=raw_target.get("created_at_ms")).validated()
    except (KeyError, TypeError) as exc:
        raise SessionControlError("Exact native target binding is required") from exc

    journal_file = _journal_path(journal_dir, "cmd-" + command_id)
    with _lock_held(journal_file):
        prior = _read_journal(journal_file)
        if prior is not None:
            if prior.get("claim_token") != proved["claim_token"]:
                # A repeated or foreign token never authorizes another execution.
                if prior.get("attempted") and prior.get("state") in (None, "uncertain"):
                    raise SessionUncertain(
                        "Attempt already recorded under another claim; reconcile, no new effect")
                return {**prior, "replayed": True}
            if prior.get("attempted") and prior.get("state") in (None, "uncertain"):
                raise SessionUncertain(
                    "Claim already attempted with unknown outcome; reconcile, no new effect")
            return {**prior, "replayed": True}

        attempt = {"op": op, "command_id": command_id, "attempted": True,
                   "state": None, "claim_token": proved["claim_token"],
                   "holder": proved["holder"], "generation": proved["generation"],
                   "caller_actor": custody.actor, "caller_family": custody.credential_family,
                   "target": {"session_id": target.session_id, "tag": target.tag,
                              "workspace": target.workspace,
                              "created_at_ms": target.created_at_ms}}
        if op == "send":
            body = command.get("body")
            if not isinstance(body, str):
                raise SessionControlError("Send body is required")
            attempt["body_digest"] = body_digest(body)
        _write_journal(journal_file, attempt)

        try:
            outcome = _run_claimed_effect(
                executable, op, command, target, custody=custody,
                journal_dir=journal_dir)
        except SessionUncertain as exc:
            raise SessionUncertain(str(exc) or "Effect uncertain; reconcile") from exc

        record = {**attempt, "state": outcome.get("state"),
                  "native_id": outcome.get("native_id"),
                  "original_native_message_id": outcome.get("original_native_message_id"),
                  "reply_envelope_id": outcome.get("reply_envelope_id"),
                  "reason": str(outcome.get("reason", ""))[:300]}
        _write_journal(journal_file, record)

        # CORRECTED ack.native semantics: ack.native.native_id is ALWAYS the
        # TARGET NATIVE SESSION UUID (binding match), never an envelope id.
        # Envelope ids travel in proof (native_message_id = original delivered
        # envelope, reply_to_native_id = reply envelope) and in the journal.
        ack_payload: dict[str, Any] = {
            "correlation": "cmd:%s:gen:%d" % (command_id, proved["generation"]),
            "outcome": outcome.get("ack_outcome", "uncertain"),
            "native": {"native_id": target.session_id,
                       "tag": target.tag, "workspace": target.workspace},
            "reason": str(outcome.get("reason", ""))[:512],
        }
        if op == "send" and outcome.get("ack_outcome") == "delivered" and outcome.get("reply_envelope_id"):
            evidence = outcome.get("proof_evidence") or {}
            ack_payload["proof"] = {
                "native_message_id": outcome.get("original_native_message_id"),
                "reply_to_native_id": outcome.get("reply_envelope_id"),
                "reply_digest": evidence.get("reply_digest", "0" * 64),
                "sender_native_id": target.session_id,
                "sender_tag": target.tag,
                "sender_workspace": target.workspace,
                "sender_generation": proved["generation"],
                "sender_created_at_ms": target.created_at_ms,
            }
        if op == "capture" and outcome.get("ack_outcome") == "delivered":
            text = str(outcome.get("capture_text", ""))
            ack_payload["capture"] = text[:65536]
        if op == "stop":
            ack_payload["confirmed_exit"] = bool(outcome.get("confirmed_exit", False))
        ack_result: dict[str, Any] = {"submitted": False, "payload": ack_payload}
        if request is not None:
            try:
                request("POST", "/api/session-commands/" + command_id + "/ack",
                        {**ack_payload, "claim_token": proved["claim_token"]})
                ack_result = {"submitted": True, "payload": ack_payload}
            except Exception as exc:
                ack_result = {"submitted": False, "payload": ack_payload,
                              "error": type(exc).__name__}
        return {"op": op, **record, "ack": ack_result}


def _run_claimed_effect(
    executable: str,
    op: str,
    command: dict[str, Any],
    target: NativeTarget,
    *,
    custody: CallerCustody,
    journal_dir: str,
) -> dict[str, Any]:
    """Run one claimed native effect; returns the outcome fields for ack mapping.

    Ack semantics (CORRECTED — ack.native is the TARGET native session UUID,
    never an envelope id): ``native_id`` below is the ORIGINAL envelope id for
    send (separate from the target), kept in the journal as
    ``original_native_message_id``. The ack ``native`` triple is always the
    exact bound target; proof carries the envelope ids separately.
    """
    if op == "send":
        body = command["body"]
        key = "cmdsend-" + str(command["command_id"])
        sent = send_prompt(executable, target, body, custody=custody,
                           transport="inbox", correlation_id=str(command["command_id"]),
                           idempotency_key=key, journal_dir=journal_dir)
        # Inbox send proves recording only. A correlated reply, when the caller
        # asks for it AND qualified reply proof is available, is checked in the
        # actual native log; otherwise the honest state stays recorded/unknown.
        reply_id: Optional[str] = None
        if command.get("await_ack") is True:
            proof = verify_ack_via_reply(
                executable, target, str(sent.get("native_id") or ""),
                {"correlation_id": str(command["command_id"]),
                 "body_digest": str(sent.get("body_digest") or "")})
            if proof.state == "acknowledged":
                reply_id = proof.reply_envelope_id
                evidence = proof.evidence or {}
                return {"state": "acknowledged", "ack_outcome": "delivered",
                        "native_id": sent.get("native_id"),
                        "original_native_message_id": sent.get("native_id"),
                        "reply_envelope_id": reply_id, "reason": proof.reason,
                        "proof_evidence": evidence}
            return {"state": "unknown", "ack_outcome": "uncertain",
                    "native_id": sent.get("native_id"),
                    "original_native_message_id": sent.get("native_id"),
                    "reply_envelope_id": None, "reason": proof.reason}
        return {"state": sent.get("state", "recorded"), "ack_outcome": "uncertain",
                "native_id": sent.get("native_id"),
                "original_native_message_id": sent.get("native_id"),
                "reply_envelope_id": None, "reason": sent.get("reason", "")}
    if op == "capture":
        limit = command.get("limit_bytes", 32 * 1024)
        mode = command.get("mode", "tail")
        if type(limit) is not int:
            raise SessionControlError("Capture bound must be an int")
        cap = capture_session_output(executable, target, custody=custody,
                                     limit_bytes=limit, mode=mode)
        return {"state": "captured", "ack_outcome": "delivered",
                "native_id": None, "reply_envelope_id": None,
                "capture_text": cap["text"], "capture_truncated": cap["truncated"],
                "reason": "bounded private projection; privacy unknown, truncated=%s"
                          % cap["truncated"]}
    stopped = stop_controlled_session(executable, target, custody=custody)
    confirmed = stopped["state"] == "stopped"
    return {"state": stopped["state"], "ack_outcome": "delivered" if confirmed else "uncertain",
            "native_id": None, "reply_envelope_id": None, "reason": stopped["reason"],
            "confirmed_exit": confirmed}


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
    """Compat command entry kept for local tests; mutating ops need a server claim.

    Read-only ``list``/``status`` work as before. ``send``/``capture``/``stop``
    require ``command["claim"]`` (server-issued single attempt) and delegate to
    :func:`handle_claimed_command`. ``start`` stays Fleet-owned via
    :func:`start_from_offer` and is rejected here. Nothing is installed anywhere.
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
        raise SessionControlError("held: start is Fleet-owned; use start_from_offer via Fleet")
    if journal_dir is None:
        raise SessionControlError("journal_dir is required")
    claim = command.get("claim")
    if not isinstance(claim, dict):
        raise SessionControlError("Server claim is required for send|capture|stop")
    if "command_id" not in command or not isinstance(command.get("command_id"), str):
        raise SessionControlError("Command id is required")
    return handle_claimed_command(command, claim, custody=custody,
                                  journal_dir=journal_dir, executable=executable,
                                  request=request)


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
