"""M12e bounded host receiver: offers -> claim -> handle_claimed_command -> ack.

Connects the public hub session queue to native execution using ONLY the
existing host credential/config (no daemon/timer/service/global config, no
second Fleet consumer). One bounded cycle (``--once``) or a bounded number of
offers; each offer maps EXACTLY to the shared host function:

  server offer kind/payload -> command op/body/NativeTarget
    send    {body}              -> op send, body, await_ack True
    capture {max_bytes, mode}   -> op capture, limit_bytes, mode tail|screen
    stop    {}                  -> op stop

``start`` is REJECTED here: Fleet already handles start once (existing
Dispatcher consumer); this receiver never polls Fleet offers or invokes start.

Claim discipline (per command, under local flock BEFORE claim/effect):
a stable ``attempt_id`` UUID is journaled per command (``recv-<id>.json``) and
reused for every claim retry, so a lost claim reply replays the SAME attester
and token but never executes again. A different attempt on the same host gets
409 already_claimed; an expired claim becomes uncertain with NO blind retry,
new attempt, or new start. Lost GET/claim/ack responses reconcile via
re-discovery (offers) and command listing — never duplicate effects.

Ack.native semantics (CORRECTED): ack.native.native_id is ALWAYS the TARGET
NATIVE SESSION UUID (binding match), never an envelope id. Envelope ids travel
in proof (native_message_id = original delivered envelope,
reply_to_native_id = reply envelope) and in the journal. See
docs/SESSION-CONTROL.md and the exported server validator
(``validateSendProof``).
"""

from __future__ import annotations

import os
import re
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

from . import private_fs
from .native_sessions import (
    NativeTarget,
    SessionControlError,
    SessionUncertain,
    qualify_sender_binding,
)
from .session_executor import (
    CallerCustody,
    handle_claimed_command,
    reconcile_pending_command,
    reconcile_stop_command,
    resubmit_pending_ack,
)

RECV_JOURNAL_LIMIT = 64 * 1024
UUID_RE = re.compile(r"^[a-f0-9-]{36}$", re.IGNORECASE)
CORRELATION_RE = re.compile(r"^cmd:([a-f0-9-]{36}):gen:(\d+)$", re.IGNORECASE)


def _journal_path(journal_dir: str, command_id: str) -> str:
    if not isinstance(journal_dir, str) or not Path(journal_dir).is_absolute():
        raise SessionControlError("journal_dir must be an absolute path")
    if not isinstance(command_id, str) or not UUID_RE.match(command_id):
        raise SessionControlError("Invalid command id for receiver journal")
    return os.path.join(journal_dir, "recv-" + command_id + ".json")


def _ensure_journal_dir(journal_dir: str) -> str:
    path = Path(journal_dir)
    if not path.is_absolute():
        raise SessionControlError("journal_dir must be an absolute path")
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return str(path)


def get_or_create_attempt(journal_dir: str, command_id: str, correlation: str) -> str:
    """Stable attempt_id per command, journaled under flock BEFORE any claim.

    Same attester reuses the SAME attempt_id across lost-reply retries (server
    replays the token); a different attempt never authorizes another execution.
    """
    import json

    _ensure_journal_dir(journal_dir)
    path = _journal_path(journal_dir, command_id)
    lock_path = path + ".lock"
    try:
        with private_fs.exclusive_lock(lock_path):
            try:
                raw = private_fs.read_bytes(path, RECV_JOURNAL_LIMIT)
                data = json.loads(raw.decode("utf-8"))
                if isinstance(data, dict) and isinstance(data.get("attempt_id"), str) \
                        and UUID_RE.match(data["attempt_id"]):
                    return str(data["attempt_id"])
            except FileNotFoundError:
                pass
            except (ValueError, UnicodeDecodeError, OSError):
                # Corrupt receiver journal: fail closed, reconcile manually.
                raise SessionControlError("Receiver journal corrupt; reconcile before retry: " + path)
            attempt_id = str(uuid.uuid4())
            raw = json.dumps({"command_id": command_id, "correlation": correlation,
                              "attempt_id": attempt_id},
                             sort_keys=True).encode("utf-8")
            private_fs.atomic_write(path, raw, RECV_JOURNAL_LIMIT)
            return attempt_id
    except BlockingIOError as exc:
        raise SessionUncertain("Receiver journal locked by a peer; reconcile, do not duplicate") from exc


def offer_to_command(offer: dict[str, Any]) -> dict[str, Any]:
    """Map one server offer EXACTLY to the shared host function input.

    Raises SessionControlError (skip, no claim) when the offer is unqualified:
    unknown kind, start (Fleet-owned), missing binding/incarnation for
    send/capture, or malformed payload. Callers leave unclaimed offers for
    re-discovery after the transport TTL — never blind-execute.

    The server-owned ``cleanup_tree`` offer flag (True only when explicitly
    True) is carried as visibility (``cleanup_tree_offered``); it NEVER
    authorizes a tree effect. Only the claim's re-evaluated flag authorizes.
    """
    if not isinstance(offer, dict):
        raise SessionControlError("Offer must be an object")
    kind = offer.get("kind")
    command_id = offer.get("command_id")
    correlation = offer.get("correlation", "")
    if not isinstance(command_id, str) or not UUID_RE.match(command_id):
        raise SessionControlError("Offer command_id is required")
    if kind == "start":
        raise SessionControlError("held: start is Fleet-owned; receiver takes send|capture|stop")
    if kind not in ("send", "capture", "stop"):
        raise SessionControlError("Unknown session command kind: %r" % (kind,))
    generation = offer.get("generation")
    if type(generation) is not int or generation < 0:
        # Fall back to parsing the canonical correlation cmd:<id>:gen:<N>.
        match = CORRELATION_RE.match(str(correlation or ""))
        if not match:
            raise SessionControlError("Offer generation is required")
        generation = int(match.group(2))
    binding = offer.get("binding")
    payload = offer.get("payload") if isinstance(offer.get("payload"), dict) else {}
    if kind in ("send", "capture"):
        if not isinstance(binding, dict):
            raise SessionControlError("Exact native target binding is required (unbound; await binding)")
        for key in ("native_id", "tag", "workspace"):
            if not isinstance(binding.get(key), str) or not binding[key]:
                raise SessionControlError("Exact native target binding is required")
        created = binding.get("native_created_at_ms")
        if type(created) is not int or created <= 0:
            raise SessionControlError("Native incarnation (native_created_at_ms) is required; refusing")
        target = {"session_id": binding["native_id"], "tag": binding["tag"],
                  "workspace": binding["workspace"], "created_at_ms": created}
        if isinstance(offer.get("engine"), str) and offer["engine"]:
            # Expected session engine from the hub offer: a live SHELL row
            # never qualifies as a requested model engine (masquerade refuses).
            target["engine"] = offer["engine"]
        # Validate early (UUID shape, absolute workspace) before any claim.
        NativeTarget(session_id=target["session_id"], tag=target["tag"],
                     workspace=target["workspace"],
                     created_at_ms=target["created_at_ms"],
                     engine=target.get("engine")).validated()
        command: dict[str, Any] = {"op": kind, "command_id": command_id,
                                   "generation": generation, "target": target}
        if kind == "send":
            body = payload.get("body")
            if not isinstance(body, str) or not body.strip():
                raise SessionControlError("Send body is required")
            command["body"] = body
            command["await_ack"] = True
        else:
            limit = payload.get("max_bytes", 32 * 1024)
            if type(limit) is not int or not 1 <= limit <= 65536:
                raise SessionControlError("Capture bound must be 1..65536")
            # Exact projection from the stored server command (default tail
            # for legacy rows); never guessed. Top-level offer mode mirrors
            # the stored payload mode; payload mode is canonical.
            raw_mode = payload.get("mode", offer.get("mode", "tail"))
            if isinstance(payload.get("screen"), bool) and "mode" not in payload and "mode" not in offer:
                raw_mode = "screen" if payload.get("screen") is True else "tail"
            if raw_mode not in ("tail", "screen"):
                raise SessionControlError("Capture mode must be tail|screen")
            command["limit_bytes"] = limit
            command["mode"] = raw_mode
        return command
    # stop: binding null (offered, pre-bind) means no native target exists.
    # The receiver still claims to fence the intent, then acks refused with no
    # native effect (nothing to exit). Bound stops carry the exact target.
    # Server-owned cleanup visibility only (never authority; the claim decides).
    offered_cleanup = offer.get("cleanup_tree") is True
    if isinstance(binding, dict) and binding.get("native_id"):
        created = binding.get("native_created_at_ms")
        target = {"session_id": binding["native_id"], "tag": binding["tag"],
                  "workspace": binding["workspace"],
                  "created_at_ms": created if type(created) is int else None}
        if isinstance(offer.get("engine"), str) and offer["engine"]:
            target["engine"] = offer["engine"]
        NativeTarget(session_id=target["session_id"], tag=target["tag"],
                     workspace=target["workspace"],
                     created_at_ms=target["created_at_ms"],
                     engine=target.get("engine")).validated()
        return {"op": "stop", "command_id": command_id,
                "generation": generation, "target": target,
                "cleanup_tree_offered": offered_cleanup}
    return {"op": "stop", "command_id": command_id,
            "generation": generation, "target": None,
            "cleanup_tree_offered": offered_cleanup}


def claim_to_host_claim(server_claim: dict[str, Any], command_id: str) -> dict[str, Any]:
    """Translate the server claim DTO to the host verifier shape.

    Server canonical: {command_id, claim_token, holder, generation,
    expires_at (ISO, canonical) and expires_at_ms (int, host clock), attempt,
    cleanup_tree (server-owned bool, re-evaluated eligibility)}. Host verifier accepts
    either expiry form and normalizes to expires_at_ms. The cleanup flag is
    passed through verbatim (True only when explicitly True); it is the ONLY
    tree authorization and is never inferred from keys/paths/JSON owner proof.
    """
    if not isinstance(server_claim, dict):
        raise SessionControlError("Server claim is required")
    return {"command_id": server_claim.get("command_id", command_id),
            "claim_token": server_claim.get("claim_token"),
            "holder": server_claim.get("holder"),
            "generation": server_claim.get("generation"),
            "expires_at_ms": server_claim.get("expires_at_ms"),
            "expires_at": server_claim.get("expires_at"),
            "cleanup_tree": True if server_claim.get("cleanup_tree") is True else False}


def receive_once(
    *,
    custody: CallerCustody,
    journal_dir: str,
    executable: str = "aplexer",
    request: Callable,
    now_ms: Optional[int] = None,
    limit: int = 20,
    sender_workspace: Optional[str] = None,
    cleanup_roots: Optional[list] = None,
) -> dict[str, Any]:
    """Run ONE bounded receiver cycle: poll -> claim -> execute -> ack.

    ``request`` is the injected hub HTTP callable ``(method, path, payload)``,
    e.g. ``session_control.request`` with the existing host credential/config
    (fake in tests). ``sender_workspace`` pins the authorized native sender
    workspace for replyable sends (service deployment); when absent, the
    receiver's own qualified whoami workspace is the sender mailbox. Returns
    ``{"offers_seen", "processed", "skipped", "results"}``; per-command
    results carry the shared function outcome plus the ack submission flag.
    No daemon, no timer, no second Fleet consumer.

    ``cleanup_roots`` is the HOST-config trusted worktree allowlist (never
    caller-supplied paths): confinement only, never target authority. A
    containment-confirmed stop additionally removes the session's own worktree
    ONLY when the server claim for that exact command carries explicit
    ``cleanup_tree is True`` (re-evaluated owner/incarnation/M15 eligibility
    at claim). Ordinary stops NEVER run the tree even with roots configured.
    ``None`` (default) leaves stop acks tree-free.
    """
    if not isinstance(custody, CallerCustody):
        raise SessionControlError("Caller custody is required")
    custody.validated()
    if not callable(request):
        raise SessionControlError("Hub request callable is required")
    if type(limit) is not int or not 1 <= limit <= 20:
        raise SessionControlError("Receiver limit must be 1..20")
    if sender_workspace is not None and (
            not isinstance(sender_workspace, str) or not os.path.isabs(sender_workspace)):
        raise SessionControlError("sender_workspace must be an absolute path")
    _ensure_journal_dir(journal_dir)

    try:
        listing = request("GET", "/api/session-commands/offers", None)
    except Exception as exc:
        raise SessionUncertain("Offers poll failed; reconcile before retry: %s" % type(exc).__name__) from exc
    offers = listing.get("offers", []) if isinstance(listing, dict) else []
    if not isinstance(offers, list):
        raise SessionControlError("Offers listing is malformed")
    offers = offers[:limit]

    processed: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for offer in offers:
        command_id = offer.get("command_id") if isinstance(offer, dict) else None
        correlation = offer.get("correlation", "") if isinstance(offer, dict) else ""
        try:
            command = offer_to_command(offer)
        except SessionControlError as exc:
            skipped.append({"command_id": command_id, "reason": str(exc)[:300]})
            continue
        except SessionUncertain as exc:
            skipped.append({"command_id": command_id, "reason": str(exc)[:300]})
            continue
        # Stop with no native target: fence via claim, then ack refused.
        if command.get("target") is None:
            try:
                attempt_id = get_or_create_attempt(journal_dir, str(command_id), str(correlation or ""))
                try:
                    server_claim = request("POST", "/api/session-commands/" + str(command_id) + "/claim",
                                           {"correlation": correlation, "attempt_id": attempt_id})
                except Exception as exc:
                    skipped.append({"command_id": command_id, "reason": "claim_unresolved_%s_reconcile" % type(exc).__name__})
                    continue
                token = server_claim.get("claim_token") if isinstance(server_claim, dict) else None
                try:
                    request("POST", "/api/session-commands/" + str(command_id) + "/ack",
                            {"correlation": correlation, "outcome": "refused",
                             "native": {"native_id": "00000000-0000-0000-0000-000000000000",
                                        "tag": "unbound", "workspace": "/"},
                             "reason": "unbound stop: no native target; nothing to exit",
                             "claim_token": token})
                    processed.append({"command_id": command_id, "op": "stop",
                                      "state": "refused", "reason": "unbound_no_target"})
                except Exception as exc:
                    skipped.append({"command_id": command_id,
                                    "reason": "ack_unresolved_%s_reconcile" % type(exc).__name__})
            except (SessionControlError, SessionUncertain) as exc:
                skipped.append({"command_id": command_id, "reason": str(exc)[:300]})
            continue
        # Stable attempt BEFORE claim (lost-reply safe).
        try:
            attempt_id = get_or_create_attempt(journal_dir, str(command_id), str(correlation or ""))
        except (SessionControlError, SessionUncertain) as exc:
            skipped.append({"command_id": command_id, "reason": str(exc)[:300]})
            continue
        # Genuine sender qualification BEFORE the claim/effect: a host process
        # without a bound native sender identity would emit an anonymous
        # envelope recipients cannot reply to, so send offers are held
        # honestly (no claim, no journal, no effect) instead. The authorized
        # sender workspace is independent of the target workspace.
        if command.get("op") == "send":
            try:
                qualify_sender_binding(executable, sender_workspace)
            except (SessionControlError, SessionUncertain) as exc:
                skipped.append({"command_id": command_id,
                                "reason": "native_sender_unbound_reconcile"})
                continue
        try:
            server_claim = request("POST", "/api/session-commands/" + str(command_id) + "/claim",
                                   {"correlation": correlation, "attempt_id": attempt_id})
        except Exception as exc:
            # Lost claim reply: SAME attempt_id reconciles via replay on the
            # next cycle (server replays the token to the same attester).
            skipped.append({"command_id": command_id,
                            "reason": "claim_unresolved_%s_same_attempt_%s" % (type(exc).__name__, attempt_id)})
            continue
        if not isinstance(server_claim, dict) or not server_claim.get("claim_token"):
            skipped.append({"command_id": command_id, "reason": "claim_rejected_reconcile_via_commands"})
            continue
        host_claim = claim_to_host_claim(server_claim, str(command_id))
        # Generation agreement: the granted claim must match the offered command.
        if host_claim.get("generation") != command.get("generation"):
            skipped.append({"command_id": command_id, "reason": "claim_generation_mismatch_reconcile"})
            continue
        try:
            outcome = handle_claimed_command(command, host_claim, custody=custody,
                                             journal_dir=journal_dir, executable=executable,
                                             request=request, now_ms=now_ms,
                                             sender_workspace=sender_workspace,
                                             cleanup_roots=cleanup_roots)
        except SessionUncertain as exc:
            skipped.append({"command_id": command_id, "reason": "uncertain_%s" % str(exc)[:200]})
            continue
        except SessionControlError as exc:
            skipped.append({"command_id": command_id, "reason": str(exc)[:300]})
            continue
        processed.append({"command_id": command_id, "op": command.get("op"),
                          "state": outcome.get("state"),
                          "ack_submitted": bool((outcome.get("ack") or {}).get("submitted", False)),
                          "tree": ((outcome.get("ack") or {}).get("payload") or {}).get("tree"),
                          "replayed": bool(outcome.get("replayed", False))})
    # Bounded FAIR reconciliation pass: sends journaled as unknown (reply not
    # yet observed, server command still claimed under its lease) are
    # re-checked WITHOUT any second native send; a delayed recipient reply
    # observed now is acked under the SAME claim token. Completed effects
    # whose ack response was lost are re-submitted identically (never
    # re-offered by the server, so the journal is the discovery path).
    # Completed (ack_submitted) and terminal-verdict journals are filtered
    # BEFORE the per-cycle cap so they can never starve pending work behind
    # lexicographically smaller filenames; they are retained on disk as the
    # durable record (never deleted to hide history).
    reconciled: list[dict[str, Any]] = []
    try:
        pending = _pending_reconcile_names(journal_dir, limit=20)
    except OSError:
        pending = []
    for name in pending:
        command_id = name[len("cmd-"):-len(".json")]
        if any(r.get("command_id") == command_id for r in processed):
            continue
        try:
            updated = reconcile_pending_command(
                journal_dir=journal_dir, command_id=command_id,
                executable=executable, request=request, now_ms=now_ms)
            if updated is None:
                updated = reconcile_stop_command(
                    journal_dir=journal_dir, command_id=command_id,
                    executable=executable, request=request, now_ms=now_ms)
            if updated is None:
                updated = resubmit_pending_ack(
                    journal_dir=journal_dir, command_id=command_id,
                    request=request)
        except (SessionControlError, SessionUncertain, OSError, ValueError):
            continue
        if updated is not None:
            reconciled.append({"command_id": command_id, "op": updated.get("op"),
                               "state": updated.get("state"),
                               "ack_submitted": bool((updated.get("ack") or {}).get("submitted", False)),
                               "reconciled": True})
    return {"offers_seen": len(offers), "processed": processed, "skipped": skipped,
            "reconciled": reconciled}


def _pending_reconcile_names(journal_dir: str, limit: int = 20) -> list[str]:
    """Fair pending scan: completed/terminal journals never consume the cap.

    A journal needs attention when it is an unreconciled send (state unknown
    with a journaled original envelope), an uncertain stop with a persisted
    containment oracle (read-only SAME-claim reconcile, never a second kill),
    or an unsubmitted proven ack for a completed effect (ack_payload present,
    not submitted, no terminal server verdict). Everything else — submitted,
    terminal, or malformed — is skipped but retained. Names sort for
    determinism; the cap applies to pending journals only, so 20 completed
    files can never starve a pending one, and multiple pages/cycles converge
    without busy retry.
    """
    import json as _json

    try:
        names = sorted(
            name for name in os.listdir(journal_dir)
            if name.startswith("cmd-") and name.endswith(".json"))
    except OSError:
        return []
    pending: list[str] = []
    for name in names:
        path = os.path.join(journal_dir, name)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                record = _json.load(fh)
        except (OSError, ValueError, UnicodeDecodeError):
            continue  # corrupt journal: fail closed, surface via manual reconcile
        if not isinstance(record, dict):
            continue
        if record.get("state") in ("unknown", "uncertain") and record.get("original_native_message_id"):
            pending.append(name)
        elif (record.get("op") == "stop" and record.get("state") == "uncertain"
                and isinstance(record.get("stop_oracle"), dict)
                and record["stop_oracle"].get("session_id")
                and not record.get("ack_submitted")
                and not str(record.get("ack_error") or "").startswith("server_terminal_")):
            pending.append(name)
        elif (isinstance(record.get("ack_payload"), dict)
                and not record.get("ack_submitted")
                and not str(record.get("ack_error") or "").startswith("server_terminal_")):
            pending.append(name)
        if len(pending) >= limit:
            break
    return pending
