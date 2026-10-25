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
from .native_bridge import NativeServiceBinding
from .native_sessions import (
    MAX_BODY_BYTES,
    TERMINAL_PHASES,
    DeliveryResult,
    NativeTarget,
    SessionControlError,
    SessionUncertain,
    body_digest,
    canonicalize_workspace,
    capture_output as _capture,
    deliver_recorded,
    list_sessions,
    qualify_target,
    reconcile_delivery_from_log,
    send_inbox as _send_inbox,
    status_of,
    stop_session as _stop,
    verify_ack_via_reply,
)


def _sender_binding_from_record(value: Any) -> Optional[NativeServiceBinding]:
    """Rebuild the journaled genuine sender binding (never fabricated).

    Returns None when the journal predates sender capture or is malformed —
    verification then stays honestly unknown rather than guessing a mailbox.
    """
    if not isinstance(value, dict):
        return None
    try:
        session_id = str(value["session_id"])
        tag = str(value["tag"])
        workspace = str(value["workspace"])
        created = value.get("created_at_ms")
        engine = value.get("engine")
        profile = value.get("profile")
    except (KeyError, TypeError, ValueError):
        return None
    if not session_id or not tag or not workspace:
        return None
    import uuid as _uuid
    try:
        _uuid.UUID(session_id)
    except (ValueError, TypeError, AttributeError):
        return None
    return NativeServiceBinding(
        session_id=session_id, tag=tag, workspace=workspace,
        engine=str(engine) if isinstance(engine, str) and engine else "hub-host",
        profile=str(profile) if isinstance(profile, str) else None,
        created_at_ms=created if type(created) is int else 0,
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

    ``cleanup_tree`` (optional server-owned boolean, default False) is the
    ONLY tree authorization: true means the server re-evaluated
    owner/incarnation/M15 eligibility at claim time for this exact command.
    The host never infers it from key prefixes, paths, or JSON owner proof.
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
    # Server-owned tree authorization only: explicit True from the claim.
    # Anything else (missing/False/non-bool, key prefixes, paths, JSON owner
    # strings) is never authorization.
    cleanup_authorized = claim.get("cleanup_tree") is True
    return {"command_id": command_id, "claim_token": token, "holder": holder,
            "generation": generation, "expires_at_ms": expires_at,
            "cleanup_tree_authorized": cleanup_authorized}


def _request_status(exc: BaseException) -> Optional[int]:
    """Best-effort HTTP status extraction from request-callable failures.

    The production hub client raises ``ValueError('... status NNN')``; stub
    callables in tests raise similar errors. A 409 means the server refused
    this attempt while any other failure is transport loss with the outcome
    still unknown. Terminality additionally needs :func:`_request_code`.
    """
    import re as _re

    match = _re.search(r"status (\d{3})", str(exc))
    if match:
        try:
            return int(match.group(1))
        except ValueError:
            return None
    status = getattr(exc, "status", None)
    return status if type(status) is int else None


def _request_code(exc: BaseException) -> Optional[str]:
    """Best-effort server error-code extraction (``... status 409 (code)``).

    The production client appends the server ``error`` field in parentheses;
    test stubs follow the same convention. Returns None when unavailable.
    """
    import re as _re

    match = _re.search(r"status \d{3} \(([A-Za-z0-9_-]{1,64})\)", str(exc))
    if match:
        return match.group(1)
    code = getattr(exc, "server_code", None)
    return code if isinstance(code, str) and code else None


# Server 409 codes that can never heal for the journaled attempt: the claim
# fence moved on (rival holder/token), the proof was consumed elsewhere or is
# bound to a superseded incarnation, or the row already settled. claim_expired
# is NOT terminal for an unknown-state send: the offers sweep (or the fixed
# lateReplyAck path) may still accept the proof, and a later reply produces a
# fresh proof. Unknown/absent codes are retryable for unknown-state journals
# (one POST per distinct observed reply; see below) and terminal for
# completed-effect resubmits (never busy-retry a finished effect).
TERMINAL_409_CODES = frozenset({
    "claim_conflict", "already_claimed", "correlation_mismatch",
    "proof_replayed", "proof_sender_mismatch", "proof_generation_stale",
    "proof_reply_mismatch", "invalid_proof_shape",
    "binding_mismatch", "command_uncertain", "claim_required",
    "proof_required", "native_proof_unavailable",
    "confirmed_exit_required",
})

# Completed-effect outcomes restored when a resubmitted ack finally lands
# (the journal must converge to the executed fact, not linger uncertain).
_COMPLETED_BY_OUTCOME = {
    "delivered": {"send": "acknowledged", "capture": "captured", "stop": "stopped"},
}


def _post_inbox_status(
    *,
    request: Optional[Callable],
    command_id: str,
    claim_token: str,
    reason: str,
) -> bool:
    """Best-effort POST of pending inbox status (AC5).

    The server projects ``waiting in inbox for recipient`` + host reason
    instead of ``claimed`` only after the host reports it under the live claim
    fence. Failures are ignored by callers: the command stays claimed under
    its lease and the next reconcile cycle retries. Returns True on 2xx.
    """
    if request is None:
        return False
    if not isinstance(command_id, str) or not command_id:
        return False
    if not isinstance(claim_token, str) or not claim_token:
        return False
    bounded_reason = str(reason or "aplexer inbox recorded; recipient has not acknowledged")[:512]
    try:
        request(
            "POST",
            "/api/session-commands/" + command_id + "/status",
            {
                "claim_token": claim_token,
                "status": "waiting in inbox for recipient",
                "reason": bounded_reason,
            },
        )
        return True
    except Exception:
        return False


def _server_correlation(command_id: str, generation: int) -> str:
    return "cmd:%s:gen:%d" % (command_id, generation)


def _delivered_ack_payload(
    *,
    command_id: str,
    generation: int,
    target: "NativeTarget",
    original_native_message_id: str,
    reply_envelope_id: str,
    evidence: dict[str, Any],
    reason: str,
) -> dict[str, Any]:
    sender_created = evidence.get("sender_created_at_ms")
    if type(sender_created) is not int:
        # No observed incarnation anywhere (reply carries none AND the live
        # catalog row has none): provenance is unprovable, so no proof is
        # minted. Never fabricate the expected binding value as a witness.
        raise SessionControlError(
            "Native incarnation unobserved; reply proof unavailable, reconcile")
    return {
        "correlation": _server_correlation(command_id, generation),
        "outcome": "delivered",
        "native": {"native_id": target.session_id,
                   "tag": target.tag, "workspace": target.workspace},
        "reason": str(reason or "")[:512],
        "proof": {
            "native_message_id": original_native_message_id,
            "reply_to_native_id": reply_envelope_id,
            "reply_digest": evidence.get("reply_digest", "0" * 64),
            "sender_native_id": target.session_id,
            "sender_tag": target.tag,
            "sender_workspace": target.workspace,
            "sender_generation": generation,
            "sender_created_at_ms": sender_created,
        },
    }


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
    sender_workspace: Optional[str] = None,
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
        result, sender_binding = _send_inbox(executable, target, body, correlation,
                                             sender_workspace=sender_workspace)
        sender_record = {"session_id": sender_binding.session_id, "tag": sender_binding.tag,
                         "workspace": sender_binding.workspace, "engine": sender_binding.engine,
                         "profile": sender_binding.profile,
                         "created_at_ms": sender_binding.created_at_ms}
        record = {"op": "send", "key": key, "transport": transport,
                  "body_digest": digest, "attempted": True,
                  "correlation_id": corr, "state": result.state,
                  "native_id": result.native_id, "reason": result.reason,
                  "caller_actor": custody.actor, "caller_family": custody.credential_family,
                  "sender_binding": sender_record,
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
    if mode not in ("tail", "screen"):
        raise SessionControlError("Capture mode must be tail|screen")
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


def _reconcile_unknown_locked(
    journal_file: str,
    prior: dict[str, Any],
    command_id: str,
    *,
    executable: str,
    request: Optional[Callable],
    now_ms: Optional[int] = None,
) -> Optional[dict[str, Any]]:
    """Read-only reconciliation of one pending send (caller holds the journal lock).

    Re-runs ``verify_ack_via_reply`` against the journaled
    ``original_native_message_id`` WITHOUT any second native send. When the
    delayed recipient reply is now observed, its delivered proof is submitted
    under the SAME claim token and the journal advances to acknowledged. A
    terminal server verdict (HTTP 409: expired/stale/uncertain/mismatch)
    closes the journal as uncertain; transport loss leaves it pending.
    Returns the updated record, or None when still unknown.
    """
    from .native_sessions import NativeTarget as _NativeTarget
    from .native_sessions import verify_ack_via_reply as _verify

    raw_target = prior.get("target") or {}
    try:
        target = _NativeTarget(
            session_id=raw_target["session_id"], tag=raw_target["tag"],
            workspace=raw_target["workspace"],
            created_at_ms=raw_target.get("created_at_ms"),
            engine=raw_target.get("engine")).validated()
    except (KeyError, TypeError, SessionControlError):
        return None
    # Reconciliation reads the JOURNALED genuine sender mailbox (the binding
    # observed before the send). Without it the reply is honestly unverifiable;
    # the sender workspace is never re-derived from the target.
    sender_binding = _sender_binding_from_record(prior.get("sender_binding"))
    if sender_binding is None:
        return None
    original_id = str(prior.get("original_native_message_id") or "")
    body_digest = prior.get("body_digest") or ""
    proof = _verify(
        executable, target, sender_binding,
        original_id,
        {"correlation_id": command_id, "body_digest": body_digest})
    if proof.state == "acknowledged" and proof.reply_envelope_id:
        evidence = proof.evidence or {}
        payload = _delivered_ack_payload(
            command_id=command_id, generation=int(prior.get("generation", 0)),
            target=target,
            original_native_message_id=original_id,
            reply_envelope_id=str(proof.reply_envelope_id),
            evidence=evidence, reason=proof.reason)
        updated = {**prior, "state": "acknowledged",
                   "delivery_state": "transported",
                   "reply_envelope_id": str(proof.reply_envelope_id),
                   "reason": str(proof.reason)[:300],
                   "ack_payload": payload}
        if request is None:
            updated["ack_submitted"] = False
            _write_journal(journal_file, updated)
            return {"op": prior.get("op"), **updated,
                    "ack": {"submitted": False, "payload": payload}}
        # One POST per distinct observed reply: a 409 rejection already recorded
        # for this exact reply envelope is not re-POSTed (no busy retry); a NEW
        # reply envelope re-arms the attempt. Transport loss leaves the journal
        # pending for the next cycle.
        if prior.get("rejected_reply_envelope_id") == proof.reply_envelope_id:
            _write_journal(journal_file, {**prior, "ack_submitted": False})
            return None
        try:
            request("POST", "/api/session-commands/" + command_id + "/ack",
                    {**payload, "claim_token": prior.get("claim_token")})
        except Exception as exc:
            if _request_status(exc) == 409:
                code = _request_code(exc) or "unknown"
                if code in TERMINAL_409_CODES:
                    # Terminal for this attempt: close as uncertain with the
                    # durable human-obligation fact (never fake completed, never
                    # silently dropped). No retry: resubmit skips terminal
                    # journals and reconcile only owns unknown-state journals.
                    closed = {**updated, "state": "uncertain",
                              "ack_submitted": False,
                              "ack_error": "server_terminal_%s" % code}
                    _write_journal(journal_file, closed)
                    return {"op": prior.get("op"), **closed,
                            "ack": {"submitted": False, "payload": payload,
                                    "terminal": "uncertain"}}
                pending = {**prior, "ack_submitted": False,
                           "ack_error": "server_retryable_%s" % code,
                           "rejected_reply_envelope_id": str(proof.reply_envelope_id)}
                _write_journal(journal_file, pending)
                return None
            return None
        updated["ack_submitted"] = True
        updated.pop("ack_error", None)
        updated.pop("rejected_reply_envelope_id", None)
        _write_journal(journal_file, updated)
        return {"op": prior.get("op"), **updated,
                "ack": {"submitted": True, "payload": payload, "reconciled": True}}

    # No correlated reply yet: check if target session has become confirmed terminal.
    # Confirmed terminal target can fail pending delivery honestly (never hang forever).
    try:
        target_rows = list_sessions(executable, workspace=target.workspace, include_exited=True)
    except Exception:
        target_rows = []
    matched_target = [
        s for s in target_rows
        if s.session_id.lower() == target.session_id.lower()
        and s.tag == target.tag
        and canonicalize_workspace(s.workspace) == canonicalize_workspace(target.workspace)
        and (target.engine is None or s.engine == target.engine)
        and (target.created_at_ms is None or s.created_at_ms == target.created_at_ms)
    ]
    if matched_target and matched_target[0].phase in TERMINAL_PHASES:
        reason = f"Target session is terminal (phase={matched_target[0].phase}); pending delivery failed"
        payload = {
            "correlation": _server_correlation(command_id, int(prior.get("generation", 0))),
            "outcome": "refused",
            "native": {"native_id": target.session_id, "tag": target.tag, "workspace": target.workspace},
            "reason": reason[:512],
        }
        updated = {**prior, "state": "refused", "reason": reason[:300],
                   "ack_payload": payload, "ack_submitted": False, "terminal_target": True}
        updated.pop("ack_pending", None)
        if request is not None:
            try:
                request("POST", "/api/session-commands/" + command_id + "/ack",
                        {**payload, "claim_token": prior.get("claim_token")})
                updated["ack_submitted"] = True
            except Exception as exc:
                if _request_status(exc) == 409:
                    code = _request_code(exc) or "unknown"
                    if code in TERMINAL_409_CODES:
                        updated["state"] = "uncertain"
                        updated["ack_error"] = f"server_terminal_{code}"
                else:
                    updated["ack_error"] = type(exc).__name__[:64]
        _write_journal(journal_file, updated)
        return {"op": prior.get("op"), **updated, "ack": {"submitted": updated["ack_submitted"], "payload": payload}}

    now = now_ms if type(now_ms) is int else int(time.time() * 1000)
    expires_at_ms = prior.get("expires_at_ms")
    if type(expires_at_ms) is int and now >= expires_at_ms:
        log_delivery = reconcile_delivery_from_log(executable, target.workspace, original_id)
        if log_delivery == "recorded":
            # Keep the server projection honest while the lease burns (AC5):
            # refresh pending status so expireClaim sees the inbox reason.
            _post_inbox_status(
                request=request,
                command_id=command_id,
                claim_token=str(prior.get("claim_token") or ""),
                reason=str(prior.get("reason", ""))[:512] or "aplexer inbox recorded; recipient has not acknowledged",
            )
            from .native_sessions import fetch_message_log, retract_inbox_envelope
            # Controller `message ack` advances a consumer cursor; it does NOT
            # cancel the original sender envelope. Its boolean is never proof
            # on its own — positive proof is log fetch success + absence.
            retract_ok = False
            try:
                retract_ok = bool(retract_inbox_envelope(executable, target, original_id))
            except Exception:
                retract_ok = False
            log_ok = False
            removed = False
            try:
                rows = fetch_message_log(executable, target.workspace)
                log_ok = True
                removed = not any(isinstance(r, dict) and r.get("id") == original_id for r in rows)
            except Exception:
                log_ok = False
                removed = False
            if retract_ok and log_ok and removed:
                reason = "recipient_not_ready"
                payload = {
                    "correlation": _server_correlation(command_id, int(prior.get("generation", 0))),
                    "outcome": "failed",
                    "native": {"native_id": target.session_id, "tag": target.tag, "workspace": target.workspace},
                    "reason": reason,
                }
                updated = {**prior, "state": "failed", "reason": reason,
                           "ack_payload": payload, "ack_submitted": False}
            else:
                reason = (
                    "recipient_not_ready_uncertain: inbox envelope still undelivered; "
                    "removal unproven (retract_ok=%s log_ok=%s removed=%s); no duplicate sent"
                    % (retract_ok, log_ok, removed)
                )[:512]
                payload = {
                    "correlation": _server_correlation(command_id, int(prior.get("generation", 0))),
                    "outcome": "uncertain",
                    "native": {"native_id": target.session_id, "tag": target.tag, "workspace": target.workspace},
                    "reason": reason,
                }
                updated = {**prior, "state": "uncertain", "reason": reason[:300],
                           "ack_payload": payload, "ack_submitted": False}
            updated.pop("ack_pending", None)
            if request is not None:
                try:
                    request("POST", "/api/session-commands/" + command_id + "/ack",
                            {**payload, "claim_token": prior.get("claim_token")})
                    updated["ack_submitted"] = True
                except Exception as exc:
                    updated["ack_error"] = str(exc)[:120]
            _write_journal(journal_file, updated)
            return {"op": prior.get("op"), **updated, "ack": {"submitted": updated["ack_submitted"], "payload": payload}, "expired": True}

    # Reconcile exact original envelope from native message log
    log_delivery = reconcile_delivery_from_log(executable, target.workspace, original_id)
    if log_delivery in ("transported", "pane"):
        prior["delivery_state"] = "transported"
        prior["state"] = "unknown"
        _write_journal(journal_file, prior)
        recheck_proof = _verify(
            executable, target, sender_binding, original_id,
            {"correlation_id": command_id, "body_digest": body_digest})
        if recheck_proof.state == "acknowledged" and recheck_proof.reply_envelope_id:
            evidence = recheck_proof.evidence or {}
            payload = _delivered_ack_payload(
                command_id=command_id, generation=int(prior.get("generation", 0)),
                target=target,
                original_native_message_id=original_id,
                reply_envelope_id=str(recheck_proof.reply_envelope_id),
                evidence=evidence, reason=recheck_proof.reason)
            updated = {**prior, "state": "acknowledged",
                       "delivery_state": "transported",
                       "reply_envelope_id": str(recheck_proof.reply_envelope_id),
                       "reason": str(recheck_proof.reason)[:300],
                       "ack_payload": payload}
            if request is not None and prior.get("rejected_reply_envelope_id") != recheck_proof.reply_envelope_id:
                try:
                    request("POST", "/api/session-commands/" + command_id + "/ack",
                            {**payload, "claim_token": prior.get("claim_token")})
                    updated["ack_submitted"] = True
                except Exception:
                    pass
            _write_journal(journal_file, updated)
            return {"op": prior.get("op"), **updated,
                    "ack": {"submitted": updated.get("ack_submitted", False), "payload": payload, "reconciled": True}}
        return {"op": prior.get("op"), **prior, "replayed": True}

    # If prior delivery was uncertain: missing/unchanged inbox or unavailable log is NOT proof
    # an uncertain PTY effect never happened. Keep uncertain until positive native submission
    # or reply evidence; NEVER retry deliver blindly.
    if prior.get("delivery_state") == "uncertain":
        return None

    # For recorded or refused messages where message is confirmed in inbox (log_delivery == "recorded"),
    # we may safely attempt deliver_recorded now.
    if prior.get("delivery_state") in ("recorded", "refused") and log_delivery == "recorded":
        try:
            del_res = deliver_recorded(executable, target, original_id)
            if del_res.state == "transported":
                prior["delivery_state"] = "transported"
                prior["state"] = "unknown"
                _write_journal(journal_file, prior)
                recheck_proof = _verify(
                    executable, target, sender_binding, original_id,
                    {"correlation_id": command_id, "body_digest": body_digest})
                if recheck_proof.state == "acknowledged" and recheck_proof.reply_envelope_id:
                    evidence = recheck_proof.evidence or {}
                    payload = _delivered_ack_payload(
                        command_id=command_id, generation=int(prior.get("generation", 0)),
                        target=target,
                        original_native_message_id=original_id,
                        reply_envelope_id=str(recheck_proof.reply_envelope_id),
                        evidence=evidence, reason=recheck_proof.reason)
                    updated = {**prior, "state": "acknowledged",
                               "delivery_state": "transported",
                               "reply_envelope_id": str(recheck_proof.reply_envelope_id),
                               "reason": str(recheck_proof.reason)[:300],
                               "ack_payload": payload}
                    if request is not None and prior.get("rejected_reply_envelope_id") != recheck_proof.reply_envelope_id:
                        try:
                            request("POST", "/api/session-commands/" + command_id + "/ack",
                                    {**payload, "claim_token": prior.get("claim_token")})
                            updated["ack_submitted"] = True
                        except Exception:
                            pass
                    _write_journal(journal_file, updated)
                    return {"op": prior.get("op"), **updated,
                            "ack": {"submitted": updated.get("ack_submitted", False), "payload": payload, "reconciled": True}}
                return {"op": prior.get("op"), **prior, "replayed": True}
            elif del_res.state == "refused":
                prior["reason"] = del_res.reason[:300]
                _write_journal(journal_file, prior)
        except SessionUncertain as exc:
            prior["delivery_state"] = "uncertain"
            prior["reason"] = str(exc)[:300]
            _write_journal(journal_file, prior)
        except SessionControlError:
            pass
        # Still inbox-waiting: refresh pending status so the server keeps
        # projecting "waiting in inbox for recipient" (AC5) until the lease
        # burns. Best-effort; failures leave the journal pending.
        try:
            still = reconcile_delivery_from_log(executable, target.workspace, original_id)
        except Exception:
            still = None
        if still == "recorded":
            _post_inbox_status(
                request=request,
                command_id=command_id,
                claim_token=str(prior.get("claim_token") or ""),
                reason=str(prior.get("reason", ""))[:512] or "aplexer inbox recorded; recipient has not acknowledged",
            )

    return None


def _resubmit_ack_locked(
    journal_file: str,
    prior: dict[str, Any],
    command_id: str,
    claim_token: str,
    *,
    request: Callable,
) -> Optional[dict[str, Any]]:
    """Re-submit an identical proven ack (caller holds the journal lock).

    The native effect is never repeated: the journaled ``ack_payload`` is
    POSTed verbatim under the same claim token. Success marks the journal
    submitted AND restores the completed local state (acknowledged/captured/
    stopped) so server and host converge; any HTTP 409 closes a completed
    effect as uncertain with the durable verdict (never retried, never
    marked delivered without a server ACK); transport loss leaves it pending
    for the next cycle.
    """
    payload = prior.get("ack_payload")
    if not isinstance(payload, dict):
        return None
    if not isinstance(prior.get("claim_token"), str) or not prior.get("claim_token"):
        return None  # no authorized attempt token: never submit without it
    try:
        request("POST", "/api/session-commands/" + command_id + "/ack",
                {**payload, "claim_token": claim_token})
    except Exception as exc:
        if _request_status(exc) == 409:
            code = _request_code(exc) or "unknown"
            updated = {**prior, "state": "uncertain", "ack_submitted": False,
                       "ack_error": "server_terminal_%s" % code}
            _write_journal(journal_file, updated)
            return {**updated, "replayed": True,
                    "ack": {"submitted": False, "payload": payload,
                            "terminal": "uncertain"}}
        return None
    op = prior.get("op")
    outcome = payload.get("outcome") if isinstance(payload, dict) else None
    restored = _COMPLETED_BY_OUTCOME.get(outcome, {}).get(op, prior.get("state"))
    updated = {**prior, "state": restored, "ack_submitted": True}
    updated.pop("ack_error", None)
    _write_journal(journal_file, updated)
    return {**updated, "replayed": True,
            "ack": {"submitted": True, "payload": payload, "resubmitted": True}}


def reconcile_pending_command(
    *,
    journal_dir: str,
    command_id: str,
    executable: str = "aplexer",
    request: Optional[Callable] = None,
    now_ms: Optional[int] = None,
) -> Optional[dict[str, Any]]:
    """Bounded read-only reconciliation of one pending send (acquires the journal lock).

    Used by the receiver's post-cycle reconcile pass and by external
    wiring: re-runs ``verify_ack_via_reply`` against the journaled original
    envelope WITHOUT any second native send, submitting the delivered proof
    under the same claim token once observed. Returns the updated record, or
    None when there is nothing (or nothing yet) to reconcile.
    """
    journal_file = _journal_path(journal_dir, "cmd-" + command_id)
    try:
        with _lock_held(journal_file):
            prior = _read_journal(journal_file)
            if prior is None:
                return None
            if prior.get("state") not in ("unknown", "uncertain") or not prior.get("original_native_message_id"):
                return None
            return _reconcile_unknown_locked(
                journal_file, prior, command_id, executable=executable,
                request=request, now_ms=now_ms)
    except (SessionControlError, SessionUncertain, OSError):
        return None


def resubmit_pending_ack(
    *,
    journal_dir: str,
    command_id: str,
    request: Callable,
) -> Optional[dict[str, Any]]:
    """Re-submit one journaled but unsubmitted ack (acquires the journal lock).

    Used by the receiver's post-cycle pass so a lost ack response is repaired
    even when the server no longer offers the command (claimed/acked rows are
    never re-offered). Never repeats the native effect.
    """
    journal_file = _journal_path(journal_dir, "cmd-" + command_id)
    try:
        with _lock_held(journal_file):
            prior = _read_journal(journal_file)
            if prior is None:
                return None
            if prior.get("ack_submitted") or not isinstance(prior.get("ack_payload"), dict):
                return None
            if prior.get("state") == "unknown" or prior.get("ack_pending"):
                return None  # reconciliation owns this journal, not ack replay
            if str(prior.get("ack_error") or "").startswith("server_terminal_"):
                return None  # terminal verdict stays closed: no infinite 409 retry
            return _resubmit_ack_locked(
                journal_file, prior, command_id, str(prior.get("claim_token") or ""),
                request=request)
    except (SessionControlError, SessionUncertain, OSError):
        return None


def reconcile_stop_command(
    *,
    journal_dir: str,
    command_id: str,
    executable: str = "aplexer",
    request: Optional[Callable] = None,
    now_ms: Optional[int] = None,
) -> Optional[dict[str, Any]]:
    """Read-only reconcile of one uncertain stop against its persisted oracle.

    Uses the journaled ``stop_oracle`` + ``stop_kill`` (persisted BEFORE the
    kill result was known) and calls
    :func:`native_sessions.reconcile_stop_oracle` — list + PID/cgroup probes
    only, never a second kill. SAME-claim guards (UUID/tag/workspace/
    generation) live in the oracle check; a foreign/live row stays uncertain;
    an absent catalog alone never proves exit. On confirmed exit the journal
    advances to stopped and a corrected ack is submitted under the SAME claim
    token; otherwise the journal stays uncertain with no mutation.
    """
    from .native_sessions import reconcile_stop_oracle as _reconcile_oracle

    journal_file = _journal_path(journal_dir, "cmd-" + command_id)
    try:
        with _lock_held(journal_file):
            prior = _read_journal(journal_file)
            if prior is None:
                return None
            if prior.get("op") != "stop" or prior.get("state") != "uncertain":
                return None
            oracle = prior.get("stop_oracle")
            kill = prior.get("stop_kill") or {}
            if not isinstance(oracle, dict) or not oracle.get("session_id"):
                return None
            # SAME-claim binding: the oracle must match the journaled target.
            tgt = prior.get("target") or {}
            try:
                if (str(oracle.get("session_id", "")).lower()
                        != str(tgt.get("session_id", "")).lower()
                        or oracle.get("tag") != tgt.get("tag")
                        or oracle.get("workspace") != tgt.get("workspace")):
                    return None
            except (AttributeError, TypeError):
                return None

            now = now_ms if type(now_ms) is int else int(time.time() * 1000)
            expires_at_ms = prior.get("expires_at_ms")
            # If lease has expired: honestly uncertain (no promotion, no second kill).
            if type(expires_at_ms) is int and now >= expires_at_ms:
                if prior.get("ack_pending"):
                    closed = {**prior, "state": "uncertain", "ack_pending": None,
                              "reason": ("claim_lease_expired_uncertain: lease expired at %s" % expires_at_ms)[:300]}
                    payload = prior.get("ack_payload") or {
                        "correlation": _server_correlation(command_id, int(prior.get("generation", 0))),
                        "outcome": "uncertain",
                        "native": {"native_id": str(tgt.get("session_id", "")),
                                   "tag": str(tgt.get("tag", "")),
                                   "workspace": str(tgt.get("workspace", ""))},
                        "reason": closed["reason"],
                        "confirmed_exit": False,
                    }
                    ack_result = {"submitted": False, "payload": payload}
                    if request is not None:
                        try:
                            request("POST", "/api/session-commands/" + command_id + "/ack",
                                    {**payload, "claim_token": prior.get("claim_token")})
                            ack_result = {"submitted": True, "payload": payload}
                        except Exception as req_exc:
                            ack_result = {"submitted": False, "payload": payload,
                                          "error": type(req_exc).__name__[:64]}
                    closed["ack_payload"] = payload
                    closed["ack_submitted"] = bool(ack_result.get("submitted", False))
                    if ack_result.get("error"):
                        closed["ack_error"] = str(ack_result["error"])[:120]
                    _write_journal(journal_file, closed)
                    return {"op": "stop", **closed, "ack": ack_result, "expired": True}
                return None

            confirmed, why = _reconcile_oracle(executable, oracle, kill if isinstance(kill, dict) else {})
            if not confirmed:
                return None
            updated = {**prior, "state": "stopped",
                       "reason": str(why)[:300],
                       "confirmed_exit": True,
                       "reconciled_via_oracle": True,
                       "ack_pending": None}
            ack_payload: dict[str, Any] = {
                "correlation": _server_correlation(
                    command_id, int(prior.get("generation", 0))),
                "outcome": "delivered",
                "native": {"native_id": str(tgt.get("session_id", "")),
                           "tag": str(tgt.get("tag", "")),
                           "workspace": str(tgt.get("workspace", ""))},
                "reason": str(why)[:512],
                "confirmed_exit": True,
            }
            # Preserve any STOP-bundled tree outcome already journaled (kept
            # on evidence failure never flips to removed here).
            tree = prior.get("tree")
            if isinstance(tree, dict) and tree.get("state") in ("removed", "kept", "uncertain"):
                ack_payload["tree"] = {"state": tree["state"],
                                       "reason": str(tree.get("reason", ""))[:300]}
            # Preserve evidence reference (never raw text in the ack).
            if isinstance(prior.get("cleanup_evidence_path"), str):
                updated["cleanup_evidence_path"] = prior["cleanup_evidence_path"]
            ack_result: dict[str, Any] = {"submitted": False, "payload": ack_payload}
            if request is not None:
                try:
                    request("POST", "/api/session-commands/" + command_id + "/ack",
                            {**ack_payload, "claim_token": prior.get("claim_token")})
                    ack_result = {"submitted": True, "payload": ack_payload}
                except Exception as exc:
                    status = _request_status(exc)
                    code = _request_code(exc) or "unknown"
                    if status == 409 and code == "claim_expired":
                        closed = {**prior, "state": "uncertain", "ack_pending": None,
                                  "ack_submitted": False,
                                  "ack_error": "server_terminal_claim_expired",
                                  "reason": "claim_expired_on_server"}
                        _write_journal(journal_file, closed)
                        return {"op": "stop", **closed,
                                "ack": {"submitted": False, "payload": ack_payload,
                                        "terminal": "uncertain"}}
                    ack_result = {"submitted": False, "payload": ack_payload,
                                  "error": type(exc).__name__[:64]}
            updated = {**updated, "ack_payload": ack_payload,
                       "ack_submitted": bool(ack_result.get("submitted", False))}
            if ack_result.get("error"):
                updated["ack_error"] = str(ack_result["error"])[:120]
            _write_journal(journal_file, updated)
            return {"op": "stop", **updated,
                    "ack": ack_result, "reconciled": True}
    except (SessionControlError, SessionUncertain, OSError, ValueError):
        return None


def handle_claimed_command(
    command: dict[str, Any],
    claim: dict[str, Any],
    *,
    custody: CallerCustody,
    journal_dir: str,
    executable: str = "aplexer",
    request: Optional[Callable] = None,
    now_ms: Optional[int] = None,
    sender_workspace: Optional[str] = None,
    cleanup_roots: Optional[list] = None,
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

    M15d STOP-bundled tree: when ``cleanup_roots`` (HOST-config allowlist) is
    provided AND the server claim carries explicit ``cleanup_tree is True``
    (re-evaluated owner/incarnation/M15 eligibility at claim), a
    containment-confirmed stop additionally removes the session's
    own worktree under the same claim (canonical binding workspace +
    discovered branch); the tree outcome rides the stop ack as data and never
    gates the stop verdict. ``None`` roots, or a claim without the explicit
    flag (ordinary stops), leaves stop acks tree-free. Roots are confinement,
    never target authority.

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
            created_at_ms=raw_target.get("created_at_ms"),
            engine=raw_target.get("engine")).validated()
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
                if prior.get("original_native_message_id"):
                    reconciled = _reconcile_unknown_locked(
                        journal_file, prior, command_id, executable=executable,
                        request=request, now_ms=now_ms)
                    if reconciled is not None:
                        return reconciled
                    return {**prior, "replayed": True}
                raise SessionUncertain(
                    "Claim already attempted with unknown outcome; reconcile, no new effect")
            if prior.get("state") == "unknown" and prior.get("original_native_message_id"):
                # Same attempt, reply pending: read-only reconciliation only
                # (never a second native send). Either the delayed reply is
                # now observed and its delivered proof is submitted, or the
                # journal replays unchanged.
                reconciled = _reconcile_unknown_locked(
                    journal_file, prior, command_id, executable=executable,
                    request=request, now_ms=now_ms)
                if reconciled is not None:
                    return reconciled
                return {**prior, "replayed": True}
            if not prior.get("ack_submitted") and isinstance(prior.get("ack_payload"), dict) \
                    and request is not None:
                # Same attempt, effect complete, ack response lost: re-submit
                # the IDENTICAL proven ack without repeating the native effect.
                # A terminal server verdict stays closed: never re-POST a
                # rejected ack (no infinite 409 loop), just replay the fact.
                if str(prior.get("ack_error") or "").startswith("server_terminal_"):
                    return {**prior, "replayed": True}
                resubmitted = _resubmit_ack_locked(
                    journal_file, prior, command_id, proved["claim_token"],
                    request=request)
                if resubmitted is not None:
                    return resubmitted
            return {**prior, "replayed": True}

        attempt = {"op": op, "command_id": command_id, "attempted": True,
                   "state": None, "claim_token": proved["claim_token"],
                   "holder": proved["holder"], "generation": proved["generation"],
                   "cleanup_tree_authorized": proved.get("cleanup_tree_authorized") is True,
                   "caller_actor": custody.actor, "caller_family": custody.credential_family,
                   "target": {"session_id": target.session_id, "tag": target.tag,
                              "workspace": target.workspace,
                              "created_at_ms": target.created_at_ms,
                              "engine": target.engine}}
        if op == "send":
            body = command.get("body")
            if not isinstance(body, str):
                raise SessionControlError("Send body is required")
            attempt["body_digest"] = body_digest(body)
        _write_journal(journal_file, attempt)

        def _durable_failure(state: str, exc: BaseException) -> dict[str, Any]:
            # Durable bounded private diagnostic (no stdout/capture/secrets):
            # only the operation stage + exception type + bounded reason.
            # Refused = pre-effect/no-mutation (replayable); uncertain =
            # attempted mutation with unknown outcome (reconcile, no blind
            # retry). Both ack so the server command never strands with a
            # bare attempted:true/state:null journal.
            err_type = type(exc).__name__[:64] or "Error"
            reason = (str(exc) or ("Effect %s; reconcile" % state))[:300]
            failure: dict[str, Any] = {
                **attempt, "state": state,
                "native_id": None,
                "reason": reason,
                "error_stage": op[:32],
                "error_type": err_type,
                "expires_at_ms": proved["expires_at_ms"],
                "server_correlation": _server_correlation(command_id, proved["generation"]),
            }
            # Preserve any pre-stop evidence already durably stored before
            # the failure (e.g. flagged-cleanup capture succeeded but the
            # stop itself went uncertain): the file stays, the journal
            # references it, the tree is never deleted on this path.
            try:
                meta_path = _journal_path(journal_dir, "cleanup-evidence-" + command_id)
                if os.path.isfile(meta_path):
                    failure["cleanup_evidence_path"] = meta_path
            except (SessionControlError, OSError, ValueError):
                pass
            # Persist the qualified pre-stop containment oracle + kill
            # attempt/result when the native stop carried them (uncertain
            # after kill). Bounded private fields only (PIDs/cgroup/phase,
            # no stdout/secrets). Enables read-only SAME-claim reconcile
            # without a second kill; absent catalog alone never proves exit.
            try:
                oracle = getattr(exc, "oracle", None)
                kill = getattr(exc, "kill", None)
                if isinstance(oracle, dict) and oracle.get("session_id"):
                    bounded_oracle = {
                        k: oracle.get(k) for k in (
                            "version", "session_id", "tag", "workspace",
                            "created_at_ms", "engine", "phase",
                            "worker_pid", "workload_pid", "processes",
                            "containment_empty", "workload_cgroup",
                            "last_activity_ms")
                    }
                    # Validate the oracle binds the SAME claim target before
                    # persisting (UUID/tag/workspace must match this attempt).
                    if (str(bounded_oracle.get("session_id", "")).lower()
                            == target.session_id.lower()
                            and bounded_oracle.get("tag") == target.tag
                            and bounded_oracle.get("workspace") == target.workspace):
                        failure["stop_oracle"] = bounded_oracle
                        if isinstance(kill, dict):
                            failure["stop_kill"] = {
                                "signal": str(kill.get("signal", ""))[:16],
                                "grace_ms": kill.get("grace_ms")
                                if type(kill.get("grace_ms")) is int else None,
                                "killed": bool(kill.get("killed", False)),
                                "result": str(kill.get("result", ""))[:64],
                            }
            except Exception:
                pass
            ack_payload: dict[str, Any] = {
                "correlation": _server_correlation(command_id, proved["generation"]),
                "outcome": "refused" if state == "refused" else "uncertain",
                "native": {"native_id": target.session_id,
                           "tag": target.tag, "workspace": target.workspace},
                "reason": reason[:512],
            }
            if op == "stop":
                ack_payload["confirmed_exit"] = False
            if op == "stop" and state == "uncertain" and failure.get("stop_oracle"):
                # Oracle-backed uncertain stop: keep original claim pending for
                # bounded read-only reconciliation. Do NOT send a premature
                # terminal uncertain ACK to the server, which would lock the server
                # command and prevent promotion to delivered upon oracle confirmation.
                failure["ack_submitted"] = False
                failure["ack_pending"] = "awaiting_stop_oracle_reconcile"
                failure["ack_payload"] = ack_payload
                _write_journal(journal_file, failure)
                return {"op": op, **failure,
                        "ack": {"submitted": False, "pending": "awaiting_stop_oracle_reconcile",
                                "payload": ack_payload}}
            if op == "send":
                orig_mid = getattr(exc, "original_native_message_id", None)
                if orig_mid:
                    failure["original_native_message_id"] = str(orig_mid)
                    failure["delivery_state"] = getattr(exc, "delivery_state", "uncertain")
                    if getattr(exc, "sender_binding", None):
                        failure["sender_binding"] = exc.sender_binding
            if op == "send" and state == "uncertain" and failure.get("original_native_message_id"):
                # Uncertain delivery of original envelope: keep original claim pending for
                # bounded read-only reconciliation. Do NOT send a premature terminal ACK.
                failure["ack_submitted"] = False
                failure["ack_pending"] = "awaiting_delivery_reconcile"
                failure["ack_payload"] = ack_payload
                _write_journal(journal_file, failure)
                return {"op": op, **failure,
                        "ack": {"submitted": False, "pending": "awaiting_delivery_reconcile",
                                "payload": ack_payload}}
            ack_result: dict[str, Any] = {"submitted": False, "payload": ack_payload}
            if request is not None:
                try:
                    request("POST", "/api/session-commands/" + command_id + "/ack",
                            {**ack_payload, "claim_token": proved["claim_token"]})
                    ack_result = {"submitted": True, "payload": ack_payload}
                except Exception as req_exc:
                    ack_result = {"submitted": False, "payload": ack_payload,
                                  "error": type(req_exc).__name__[:64]}
            failure = {**failure, "ack_payload": ack_payload,
                       "ack_submitted": bool(ack_result.get("submitted", False))}
            if ack_result.get("error"):
                failure["ack_error"] = str(ack_result["error"])[:120]
            _write_journal(journal_file, failure)
            return {"op": op, **failure, "ack": ack_result}

        try:
            outcome = _run_claimed_effect(
                executable, op, command, target, custody=custody,
                journal_dir=journal_dir, sender_workspace=sender_workspace,
                cleanup_roots=cleanup_roots,
                cleanup_tree_authorized=proved.get("cleanup_tree_authorized") is True)
        except SessionControlError as exc:
            # Read-only or pre-effect refusal (no mutation): durable refused.
            return _durable_failure("refused", exc)
        except SessionUncertain as exc:
            return _durable_failure("uncertain", exc)
        except Exception as exc:  # never strand attempted:true/state:null
            return _durable_failure("uncertain", exc)

        record = {**attempt, "state": outcome.get("state"),
                  "native_id": outcome.get("native_id"),
                  "original_native_message_id": outcome.get("original_native_message_id"),
                  "delivery_state": outcome.get("delivery_state"),
                  "reply_envelope_id": outcome.get("reply_envelope_id"),
                  "reason": str(outcome.get("reason", ""))[:300],
                  "expires_at_ms": proved["expires_at_ms"],
                  "server_correlation": _server_correlation(command_id, proved["generation"])}
        if isinstance(outcome.get("cleanup_evidence_path"), str) and outcome["cleanup_evidence_path"]:
            # Local durable evidence path only (never in the Hub ack): a lost
            # ack re-submit replays the identical ack without re-running the
            # effect, so the same evidence file serves every retry.
            record["cleanup_evidence_path"] = outcome["cleanup_evidence_path"]
        if isinstance(outcome.get("sender_binding"), dict):
            # Journal the genuine sender binding observed before the effect so
            # read-only reconciliation reads the correct sender mailbox without
            # re-deriving identity from the target.
            record["sender_binding"] = outcome["sender_binding"]

        # A send whose recipient reply has not arrived yet is journaled as
        # pending WITHOUT any server ack: the command stays claimed under its
        # lease while bounded read-only reconciliation watches for the delayed
        # reply. Finalizing terminal `uncertain` on the 0ms check would lock
        # the server command and strand the later reply, so no terminal ack
        # POST happens here by construction. The pending inbox status IS
        # posted (AC5) so `hub sessions commands` shows "waiting in inbox
        # for recipient" + reason instead of `claimed`.
        if outcome.get("state") == "unknown":
            record["ack_submitted"] = False
            record["ack_pending"] = "awaiting_recipient_reply"
            _write_journal(journal_file, record)
            status_posted = _post_inbox_status(
                request=request,
                command_id=command_id,
                claim_token=str(proved["claim_token"]),
                reason=str(outcome.get("reason", ""))[:512] or "aplexer inbox recorded; recipient has not acknowledged",
            )
            return {"op": op, **record,
                    "ack": {"submitted": False, "pending": "awaiting_recipient_reply",
                            "status_posted": bool(status_posted)}}

        # CORRECTED ack.native semantics: ack.native.native_id is ALWAYS the
        # TARGET NATIVE SESSION UUID (binding match), never an envelope id.
        # Envelope ids travel in proof (native_message_id = original delivered
        # envelope, reply_to_native_id = reply envelope) and in the journal.
        ack_payload: dict[str, Any] = {
            "correlation": _server_correlation(command_id, proved["generation"]),
            "outcome": outcome.get("ack_outcome", "uncertain"),
            "native": {"native_id": target.session_id,
                       "tag": target.tag, "workspace": target.workspace},
            "reason": str(outcome.get("reason", ""))[:512],
        }
        if op == "send" and outcome.get("ack_outcome") == "delivered" and outcome.get("reply_envelope_id"):
            evidence = outcome.get("proof_evidence") or {}
            ack_payload["proof"] = _delivered_ack_payload(
                command_id=command_id, generation=proved["generation"], target=target,
                original_native_message_id=str(outcome.get("original_native_message_id")),
                reply_envelope_id=str(outcome.get("reply_envelope_id")),
                evidence=evidence, reason=str(outcome.get("reason", "")),
            )["proof"]
        if op == "capture" and outcome.get("ack_outcome") == "delivered":
            text = str(outcome.get("capture_text", ""))
            ack_payload["capture"] = text[:65536]
        if op == "stop":
            ack_payload["confirmed_exit"] = bool(outcome.get("confirmed_exit", False))
            # STOP-bundled owning-host tree outcome (server shape-validates,
            # persists in ack_json, and rechecks the owner at durable ack).
            tree = outcome.get("tree")
            if isinstance(tree, dict) and tree.get("state") in ("removed", "kept", "uncertain"):
                ack_payload["tree"] = {"state": tree["state"],
                                       "reason": str(tree.get("reason", ""))[:300]}
        # The ack payload and its submission state persist atomically with the
        # outcome: a lost ack response re-submits this identical payload under
        # the same claim token instead of repeating the native effect.
        ack_result: dict[str, Any] = {"submitted": False, "payload": ack_payload}
        if request is not None:
            try:
                request("POST", "/api/session-commands/" + command_id + "/ack",
                        {**ack_payload, "claim_token": proved["claim_token"]})
                ack_result = {"submitted": True, "payload": ack_payload}
            except Exception as exc:
                ack_result = {"submitted": False, "payload": ack_payload,
                              "error": type(exc).__name__}
        record = {**record, "ack_payload": ack_payload,
                  "ack_submitted": bool(ack_result.get("submitted", False))}
        if ack_result.get("error"):
            record["ack_error"] = str(ack_result["error"])[:120]
        _write_journal(journal_file, record)
        return {"op": op, **record, "ack": ack_result}


CLEANUP_EVIDENCE_LIMIT_BYTES = 64 * 1024


def _evidence_raw_path(journal_dir: str, command_id: str) -> str:
    """Private raw payload path for cleanup evidence (validated, no JSON)."""
    if not isinstance(journal_dir, str) or not Path(journal_dir).is_absolute():
        raise SessionControlError("journal_dir must be an absolute path")
    if not isinstance(command_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{1,128}", command_id):
        raise SessionControlError("Invalid command id for cleanup evidence")
    return os.path.join(journal_dir, "cleanup-evidence-" + command_id + ".txt")


def _preserve_cleanup_evidence(
    executable: str,
    target: NativeTarget,
    *,
    custody: CallerCustody,
    journal_dir: str,
    command_id: str,
) -> str:
    """Save a bounded pre-stop capture of the cleanup target (flagged cleanup ONLY).

    Runs a bounded tail capture (64 KiB) against the exact qualified target
    BEFORE any native stop, and persists it as TWO private 0600 fsync'd files
    OUTSIDE the cleanup roots beside the receiver journals (never under a
    cleanup root):

    - ``cleanup-evidence-<command_id>.txt``: exact raw UTF-8 payload bytes
      (<= 64 KiB, no JSON escaping, so full 64 KiB + ANSI expansion never
      overflows);
    - ``cleanup-evidence-<command_id>.json``: small bounded metadata
      (op/command/native/hash/byte_length/truncated/raw_file, < 2 KiB).

    The raw text never leaves the host (never in the Hub ack, never in logs):
    callers report only kept/removed with a generic reason. Returns the
    metadata path. Raises SessionControlError/SessionUncertain when the
    capture or either write fails — callers then keep (never delete) the
    tree rather than claim preservation.
    """
    import hashlib as _hashlib

    _check_custody(custody)
    if not isinstance(command_id, str) or not command_id:
        raise SessionControlError("Command id is required for cleanup evidence")
    retired_source: Optional[str] = None
    native_finished_at_ms: Optional[int] = None
    retained_capture: Optional[dict] = None
    try:
        cap = capture_session_output(executable, target, custody=custody,
                                     limit_bytes=CLEANUP_EVIDENCE_LIMIT_BYTES, mode="tail")
        text = cap.get("text", "")
        if not isinstance(text, str):
            raise SessionControlError("Cleanup evidence capture returned no text; refusing")
        raw = text.encode("utf-8", "replace")
        truncated = bool(cap.get("truncated", False))
    except SessionControlError as cap_exc:
        from .retired_native import verify_native_retirement, capture_retired_history
        is_retired, tombstone, reason = verify_native_retirement(executable, target)
        if not is_retired or not tombstone:
            raise SessionControlError(
                "Cleanup evidence capture failed and native not retired: %s; %s" % (cap_exc, reason)
            ) from cap_exc
        raw_retired, source_path, mode, hist_reason = capture_retired_history(
            target, limit_bytes=CLEANUP_EVIDENCE_LIMIT_BYTES
        )
        native_finished_at_ms = tombstone.get("finished_at_ms")
        if raw_retired is None or len(raw_retired) == 0:
            from .capture_evidence import read_retained_capture
            try:
                raw, retained_capture = read_retained_capture(
                    journal_dir, target, custody.machine_id, native_finished_at_ms)
            except SessionControlError as exc:
                raise SessionControlError("authentic retired history unavailable; listed-only") from exc
            truncated = bool(retained_capture.get("truncated"))
            retired_source = "commanded-capture"
        else:
            raw = raw_retired
            truncated = len(raw) >= CLEANUP_EVIDENCE_LIMIT_BYTES
            retired_source = os.path.basename(source_path) if source_path else "retired"

    # Exact byte budget on the raw UTF-8 payload (not on JSON-escaped text):
    # the adapter already truncates to limit_bytes, but stubs/tests may return
    # more, and multi-byte/ANSI content expands under JSON. Enforce here.
    if len(raw) > CLEANUP_EVIDENCE_LIMIT_BYTES:
        raw = raw[:CLEANUP_EVIDENCE_LIMIT_BYTES]
        # Keep valid UTF-8 after byte truncation (dangling trail -> replace).
        text = raw.decode("utf-8", "replace")
        raw = text.encode("utf-8")
        # Re-enforce after re-encode (replacement char is 3 bytes).
        if len(raw) > CLEANUP_EVIDENCE_LIMIT_BYTES:
            raw = raw[:CLEANUP_EVIDENCE_LIMIT_BYTES]
        truncated = True
    byte_length = len(raw)
    digest = _hashlib.sha256(raw).hexdigest()
    meta_path = _journal_path(journal_dir, "cleanup-evidence-" + command_id)
    raw_path = _evidence_raw_path(journal_dir, command_id)
    # Fail closed when the evidence file would land inside a cleanup root is
    # enforced by construction: journal_dir is never under a cleanup root
    # (receiver invariant; see session_cleanup module docstring).
    try:
        private_fs.atomic_write(raw_path, raw, CLEANUP_EVIDENCE_LIMIT_BYTES)
    except (ValueError, OSError, PermissionError) as exc:
        raise SessionControlError(
            "Cleanup evidence store failed; refusing") from exc
    record = {"op": "cleanup-evidence", "command_id": command_id,
              "native_id": target.session_id, "tag": target.tag,
              "workspace": target.workspace,
              "byte_length": byte_length, "truncated": truncated,
              "sha256": digest,
              "raw_file": os.path.basename(raw_path),
              "privacy_unknown": True,
              "evidence_observed_at_ms": int(time.time() * 1000)}
    if native_finished_at_ms is not None:
        record["native_finished_at_ms"] = native_finished_at_ms
    if retired_source is not None:
        record["retired_source"] = retired_source
    if retained_capture is not None:
        record["capture_command_id"] = retained_capture["command_id"]
        record["capture_observed_at_ms"] = retained_capture["captured_at_ms"]
        record["capture_mode"] = retained_capture["mode"]
    try:
        _write_journal(meta_path, record)
    except (ValueError, OSError, PermissionError) as exc:
        # Metadata must be durable too; a half-stored payload keeps the tree.
        # Remove the raw payload best-effort (never under a cleanup root) so
        # a later retry starts clean, then refuse.
        try:
            os.unlink(raw_path)
        except OSError:
            pass
        raise SessionControlError(
            "Cleanup evidence metadata store failed; refusing") from exc
    return meta_path


def _run_cleanup_tree_after_stop(
    executable: str,
    target: NativeTarget,
    *,
    custody: CallerCustody,
    journal_dir: str,
    cleanup_roots: Any,
    command_id: str,
) -> dict[str, Any]:
    """STOP-bundled owning-host tree step (same claim, same attempt, no new fence).

    The caller guarantees the stop is already containment-confirmed in this
    process. The tree is the canonical binding workspace (server offer, never
    a caller path); the branch is discovered; roots come from HOST config.
    Returns the ack-vocabulary outcome ``{state: removed|kept|uncertain,
    reason}`` — the tree never raises past this point, so a kept tree cannot
    fail the already-confirmed stop. A lost ack response re-submits the
    identical ack (with this tree outcome) via the existing resubmit path;
    a crash between the stop effect and this step leaves the command for
    operator reconciliation under existing uncertain semantics (the effect is
    never blind-repeated under the consumed claim).
    """
    from .session_cleanup import remove_owned_worktree
    from .retired_native import verify_native_retirement

    # Live-use inventory excludes terminal native phases: the just-stopped
    # target's own exited row still references its workspace, and must not
    # self-block its own tree. Unknown phases fail closed (treated live).
    try:
        rows = list_sessions(executable, include_exited=True)
        live_paths = [p for row in rows
                      if getattr(row, "phase", "") not in ("exited", "dead", "forgotten", "pruned")
                      for p in (row.workspace, row.cwd)
                      if isinstance(p, str) and p]
    except Exception:
        return {"state": "uncertain", "reason": "live_inventory_unavailable: native list failed; listed-only"}

    # Target exit / retirement verification before tree:
    target_in_rows = [r for r in rows if r.session_id.lower() == target.session_id.lower()]
    if target_in_rows:
        target_phase = getattr(target_in_rows[0], "phase", "")
        if target_phase not in ("exited", "dead", "forgotten", "pruned"):
            return {"state": "kept", "reason": f"target still active in catalog before tree (phase={target_phase}); listed-only"}
    else:
        # Target absent from catalog: native retirement proof required again before tree
        is_retired, tombstone, reason = verify_native_retirement(executable, target)
        if not is_retired:
            return {"state": "kept", "reason": f"native retirement re-verification failed before tree ({reason}); listed-only"}
    try:
        result = remove_owned_worktree(
            custody=custody, journal_dir=journal_dir, machine_id=custody.machine_id,
            native_id=target.session_id, canonical_workspace=target.workspace,
            cleanup_roots=cleanup_roots, claim_holder=custody.machine_id,
            stop_confirmed=True, live_paths=live_paths, command_id=command_id)
    except SessionControlError as exc:
        return {"state": "uncertain", "reason": str(exc)[:300]}
    except SessionUncertain as exc:
        return {"state": "uncertain", "reason": str(exc)[:300]}
    state = result.get("state")
    if state == "tree_removed":
        return {"state": "removed", "reason": str(result.get("reason", ""))[:300]}
    if state == "refused":
        return {"state": "kept", "reason": str(result.get("reason", ""))[:300]}
    return {"state": "uncertain", "reason": str(result.get("reason", ""))[:300]}


def _run_claimed_effect(
    executable: str,
    op: str,
    command: dict[str, Any],
    target: NativeTarget,
    *,
    custody: CallerCustody,
    journal_dir: str,
    sender_workspace: Optional[str] = None,
    cleanup_roots: Optional[list] = None,
    cleanup_tree_authorized: bool = False,
) -> dict[str, Any]:
    """Run one claimed native effect; returns the outcome fields for ack mapping.

    Ack semantics (CORRECTED — ack.native is the TARGET native session UUID,
    never an envelope id): ``native_id`` below is the ORIGINAL envelope id for
    send (separate from the target), kept in the journal as
    ``original_native_message_id``. The ack ``native`` triple is always the
    exact bound target; proof carries the envelope ids separately.

    ``cleanup_roots`` (HOST-config allowlist, None disables) plus explicit
    ``cleanup_tree_authorized is True`` (server claim flag, re-evaluated
    eligibility) arms the M15d STOP-bundled tree step: after a
    containment-confirmed stop, the session's own worktree is removed via
    :func:`_run_cleanup_tree_after_stop` under the same claim. Ordinary stops
    (flag False/missing) NEVER run the tree even with roots configured.
    """
    if op == "send":
        observed = qualify_target(executable, target)
        if observed.headless_exec or (command.get("mode") == "headless"
                                      and observed.engine in ("codex", "zcodex")):
            raise SessionControlError("send_unsupported_headless")
        body = command["body"]
        key = "cmdsend-" + str(command["command_id"])
        transport_mode = command.get("transport", "guarded")
        sent = send_prompt(executable, target, body, custody=custody,
                           transport="inbox", correlation_id=str(command["command_id"]),
                           idempotency_key=key, journal_dir=journal_dir,
                           sender_workspace=sender_workspace)
        sender_binding = _sender_binding_from_record(sent.get("sender_binding"))
        original_id = str(sent.get("native_id") or "")

        # Default real Hub send uses safe guarded deliver of its original persisted envelope
        # when supported/ready (await_ack=True or transport="guarded").
        want_deliver = command.get("transport") == "guarded" or (
            command.get("await_ack") is True and command.get("transport") != "inbox"
        )
        if want_deliver:
            try:
                delivered = deliver_recorded(executable, target, original_id)
            except SessionUncertain as exc:
                exc.original_native_message_id = original_id
                exc.sender_binding = sent.get("sender_binding")
                exc.delivery_state = "uncertain"
                raise
        else:
            delivered = DeliveryResult(
                state="recorded",
                native_id=original_id,
                reason=sent.get("reason", "inbox recorded; transport=inbox requested"),
            )

        reply_id: Optional[str] = None
        if command.get("await_ack") is True:
            if sender_binding is None:
                return {"state": "unknown", "ack_outcome": "uncertain",
                        "native_id": original_id,
                        "original_native_message_id": original_id,
                        "delivery_state": delivered.state,
                        "reply_envelope_id": None,
                        "reason": "genuine sender binding unavailable; reply unverifiable"}
            if delivered.state == "transported":
                proof = verify_ack_via_reply(
                    executable, target, sender_binding, original_id,
                    {"correlation_id": str(command["command_id"]),
                     "body_digest": str(sent.get("body_digest") or "")})
                if proof.state == "acknowledged":
                    reply_id = proof.reply_envelope_id
                    evidence = proof.evidence or {}
                    return {"state": "acknowledged", "ack_outcome": "delivered",
                            "native_id": original_id,
                            "original_native_message_id": original_id,
                            "delivery_state": "transported",
                            "reply_envelope_id": reply_id, "reason": proof.reason,
                            "proof_evidence": evidence,
                            "sender_binding": sent.get("sender_binding")}
                return {"state": "unknown", "ack_outcome": "uncertain",
                        "native_id": original_id,
                        "original_native_message_id": original_id,
                        "delivery_state": "transported",
                        "reply_envelope_id": None, "reason": proof.reason or delivered.reason,
                        "sender_binding": sent.get("sender_binding")}
            # Target was blocked busy/draft/menu or unsupported: remains held in inbox, never input to PTY
            return {"state": "unknown", "ack_outcome": "uncertain",
                    "native_id": original_id,
                    "original_native_message_id": original_id,
                    "delivery_state": "recorded",
                    "reply_envelope_id": None,
                    "reason": delivered.reason or "target not ready; message held in inbox",
                    "sender_binding": sent.get("sender_binding")}

        return {"state": delivered.state if transport_mode != "inbox" else sent.get("state", "recorded"),
                "ack_outcome": "uncertain",
                "native_id": original_id,
                "original_native_message_id": original_id,
                "delivery_state": delivered.state,
                "reply_envelope_id": None,
                "reason": delivered.reason or sent.get("reason", ""),
                "sender_binding": sent.get("sender_binding")}
    if op == "capture":
        limit = command.get("limit_bytes", 32 * 1024)
        mode = command.get("mode", "tail")
        if type(limit) is not int or not 1 <= limit <= 65536:
            raise SessionControlError("Capture bound must be 1..65536")
        if mode not in ("tail", "screen"):
            raise SessionControlError("Capture mode must be tail|screen")
        # Exact mode to the existing native adapter (capture --screen --plain
        # vs tail); bounded UTF-8 return, private raw semantics retained.
        cap = capture_session_output(executable, target, custody=custody,
                                     limit_bytes=limit, mode=mode)
        # Only explicit authenticated captures populate this bounded archive.
        # Legacy unbound-incarnation captures remain readable, never archive proof.
        if type(target.created_at_ms) is int and target.created_at_ms > 0:
            from .capture_evidence import retain_capture
            retain_capture(journal_dir, target, custody.machine_id,
                           str(command.get("command_id", "")), {**cap, "mode": mode})
        return {"state": "captured", "ack_outcome": "delivered",
                "native_id": None, "reply_envelope_id": None,
                "capture_text": cap["text"], "capture_truncated": cap["truncated"],
                "reason": "bounded private projection; privacy unknown, truncated=%s"
                          % cap["truncated"]}
    want_tree = cleanup_tree_authorized is True and cleanup_roots is not None
    evidence_path: Optional[str] = None
    evidence_error: Optional[str] = None
    if want_tree:
        # Flagged cleanup ONLY: preserve a bounded pre-stop capture to the
        # private journal BEFORE the native stop. Ordinary stops never
        # capture here. If capture/fsync/meta fails, refuse the WHOLE effect
        # before stop/tree, returning kept tree and confirmed_exit=False.
        try:
            evidence_path = _preserve_cleanup_evidence(
                executable, target, custody=custody, journal_dir=journal_dir,
                command_id=str(command["command_id"]))
        except (SessionControlError, SessionUncertain) as exc:
            evidence_error = str(exc)[:200]
        except Exception as exc:
            evidence_error = ("evidence_capture_%s" % type(exc).__name__)[:200]
        if evidence_error is not None or evidence_path is None:
            err_msg = evidence_error or "unknown"
            refused_reason = ("cleanup_preservation_failed_refused: %s" % err_msg)[:300]
            tree_reason = err_msg if err_msg.endswith("; listed-only") else f"{err_msg}; listed-only"
            return {
                "state": "refused",
                "ack_outcome": "refused",
                "native_id": None,
                "reply_envelope_id": None,
                "reason": refused_reason,
                "confirmed_exit": False,
                "tree": {
                    "state": "kept",
                    "reason": ("evidence_capture_failed: %s" % tree_reason)[:300],
                },
            }
    stopped = stop_controlled_session(executable, target, custody=custody)
    confirmed = stopped["state"] == "stopped"
    outcome: dict[str, Any] = {
        "state": stopped["state"], "ack_outcome": "delivered" if confirmed else "uncertain",
        "native_id": None, "reply_envelope_id": None, "reason": stopped["reason"],
        "confirmed_exit": confirmed}
    if evidence_path is not None:
        outcome["cleanup_evidence_path"] = evidence_path
    # M15d owning-host tree (STOP-bundled, same claim, no second controller):
    # after a containment-confirmed stop, remove the session's own dedicated
    # worktree derived ONLY from the canonical binding workspace + HOST-config
    # roots + live native identity + discovered registered branch, and ONLY
    # when the server claim explicitly authorized it (cleanup_tree is True)
    # AND pre-stop evidence was durably preserved. Ordinary stops NEVER run
    # the tree even with roots configured: roots are confinement, not target
    # authority. The tree never gates the stop verdict: kept/uncertain trees
    # ride the stop ack as data while the stop itself still reports its
    # confirmed outcome. Raw evidence text never enters the ack.
    if confirmed and want_tree:
        outcome["tree"] = _run_cleanup_tree_after_stop(
            executable, target, custody=custody, journal_dir=journal_dir,
            cleanup_roots=cleanup_roots, command_id=str(command["command_id"]))
    return outcome


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
