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
409 already_claimed; expired custody is re-offered with the SAME token and
attempt identity for bounded journal reconciliation, never a new effect. Lost GET/claim/ack responses reconcile via
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
import threading
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

from . import private_fs, session_messages
from .native_sessions import (
    NativeTarget,
    SessionControlError,
    SessionUncertain,
    list_sessions,
    qualify_sender_binding,
    stop_session,
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
MESSAGE_DELIVERY_LIMIT = 4


class MessageDeliveryWorkers:
    """Daemon workers for slow message delivery, isolated from command polling.

    Session messages have one in-flight worker per Hub session and four per
    host. Capture and stop command execution stays on the receiver thread.
    """
    def __init__(self, limit: int = MESSAGE_DELIVERY_LIMIT):
        if type(limit) is not int or not 1 <= limit <= MESSAGE_DELIVERY_LIMIT:
            raise ValueError("Message worker limit must be 1..4")
        self.limit = limit
        self._condition = threading.Condition()
        self._active_commands: dict[str, str] = {}
        self._active_sessions: set[str] = set()
        self._completed: list[dict[str, Any]] = []

    def submit(self, session_id: str, command_id: str, work: Callable[[], Any]) -> bool:
        if not isinstance(session_id, str) or not session_id or not isinstance(command_id, str) or not command_id:
            return False
        with self._condition:
            if (len(self._active_commands) >= self.limit or session_id in self._active_sessions
                    or command_id in self._active_commands):
                return False
            self._active_commands[command_id] = session_id
            self._active_sessions.add(session_id)

        def run():
            result: dict[str, Any] = {"command_id": command_id, "session_id": session_id}
            try:
                value = work()
                if isinstance(value, dict):
                    result["state"] = value.get("state")
                    result["replayed"] = bool(value.get("replayed", False))
                else:
                    result["state"] = "completed"
            except Exception as exc:
                result["state"] = "error"
                result["error"] = type(exc).__name__
            finally:
                with self._condition:
                    self._active_commands.pop(command_id, None)
                    self._active_sessions.discard(session_id)
                    self._completed.append(result)
                    if len(self._completed) > 256:
                        del self._completed[:-256]
                    self._condition.notify_all()

        thread = threading.Thread(target=run, name="session-message-delivery", daemon=True)
        try:
            thread.start()
        except Exception:
            with self._condition:
                self._active_commands.pop(command_id, None)
                self._active_sessions.discard(session_id)
                self._condition.notify_all()
            return False
        return True

    def wait_idle(self, timeout: Optional[float] = None) -> bool:
        """Test/operator seam; host command polling never waits on this."""
        import time
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._condition:
            while self._active_commands:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True

    def drain_completed(self) -> list[dict[str, Any]]:
        with self._condition:
            rows, self._completed = self._completed, []
            return rows

    @property
    def active_count(self) -> int:
        with self._condition:
            return len(self._active_commands)


_MESSAGE_DELIVERY_WORKERS = MessageDeliveryWorkers()


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
            command["mode"] = offer.get("mode")
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
        return {"op": "stop", "command_id": command_id, "session_id": offer.get("session_id"),
                "generation": generation, "target": target,
                "cleanup_tree_offered": offered_cleanup}
    return {"op": "stop", "command_id": command_id, "session_id": offer.get("session_id"),
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
            "cleanup_tree": True if server_claim.get("cleanup_tree") is True else False,
            "linked_intake_open": True if server_claim.get("linked_intake_open") is True else (
                False if server_claim.get("linked_intake_open") is False else None)}


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
    message_workers: Optional[MessageDeliveryWorkers] = None,
    command_kind: Optional[str] = None,
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
    if command_kind not in (None, "stop", "non_stop"):
        raise SessionControlError("command_kind must be stop|non_stop")
    if sender_workspace is not None and (
            not isinstance(sender_workspace, str) or not os.path.isabs(sender_workspace)):
        raise SessionControlError("sender_workspace must be an absolute path")
    _ensure_journal_dir(journal_dir)

    workers = message_workers or _MESSAGE_DELIVERY_WORKERS
    # Reconcile crash-left message intents before polling. These workers share
    # the same per-session and host-wide bounds as fresh delivery.
    try:
        pending_messages = [] if command_kind == "stop" else session_messages.pending_message_reconciliations(
            journal_dir, now_ms=now_ms, limit=32)
    except OSError:
        pending_messages = []
    for path, record in pending_messages:
        session_id, command_id = record.get("hub_session_id"), record.get("command_id")
        if isinstance(session_id, str) and isinstance(command_id, str):
            workers.submit(session_id, command_id, lambda path=path: session_messages.reconcile_message_record(
                path, executable=executable, request=request, now_ms=now_ms))

    offers_path = "/api/session-commands/offers"
    if command_kind is not None:
        offers_path += "?kind=" + command_kind
    try:
        listing = request("GET", offers_path, None)
    except Exception as exc:
        raise SessionUncertain("Offers poll failed; reconcile before retry: %s" % type(exc).__name__) from exc
    offers = listing.get("offers", []) if isinstance(listing, dict) else []
    if not isinstance(offers, list):
        raise SessionControlError("Offers listing is malformed")
    if command_kind == "stop":
        offers = [offer for offer in offers if isinstance(offer, dict) and offer.get("kind") == "stop"]
    elif command_kind == "non_stop":
        offers = [offer for offer in offers if not isinstance(offer, dict) or offer.get("kind") != "stop"]
    offers = offers[:limit]

    processed: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for offer in offers:
        command_id = offer.get("command_id") if isinstance(offer, dict) else None
        correlation = offer.get("correlation", "") if isinstance(offer, dict) else ""
        if isinstance(offer, dict) and offer.get("kind") == "message":
            # Message delivery can block in a native TUI guard. Run it outside
            # this poll thread so subsequent capture/stop offers stay live.
            try:
                attempt_id = get_or_create_attempt(journal_dir, str(command_id), str(correlation or ""))
                target_session = offer.get("session_id")
                if not isinstance(target_session, str) or not target_session:
                    raise SessionControlError("Message offer has no Hub session id")
                submitted = workers.submit(target_session, str(command_id), lambda offer=offer, attempt_id=attempt_id:
                    session_messages.handle_message_offer(
                        offer, journal_dir=journal_dir, executable=executable, request=request,
                        attempt_id=attempt_id, sender_workspace=sender_workspace, now_ms=now_ms))
                if submitted:
                    processed.append({"op": "message", "command_id": command_id, "state": "scheduled"})
                else:
                    skipped.append({"command_id": command_id, "reason": "message_worker_busy"})
            except (SessionControlError, SessionUncertain) as exc:
                skipped.append({"command_id": command_id, "reason": str(exc)[:300]})
            except Exception as exc:
                skipped.append({"command_id": command_id, "reason": "message_%s" % type(exc).__name__})
            continue
        try:
            command = offer_to_command(offer)
        except SessionControlError as exc:
            skipped.append({"command_id": command_id, "reason": str(exc)[:300]})
            continue
        except SessionUncertain as exc:
            skipped.append({"command_id": command_id, "reason": str(exc)[:300]})
            continue
        # Stop with no bound native target: a correlated launch tag can help
        # discover the exact native identity, but an empty catalog result is
        # not containment proof. Never ack an unbound stop unless an exact
        # native target was found and stop_session verified it empty.
        if command.get("target") is None:
            try:
                launch_id = offer.get("fleet_launch_id") if isinstance(offer, dict) else None
                if not isinstance(launch_id, str) or not launch_id:
                    skipped.append({"command_id": command_id,
                                    "reason": "unbound_stop_missing_launch_identity"})
                    continue
                expected_tags = {f"hub-{launch_id}", f"task-hub-{launch_id}"}
                try:
                    rows = [row for row in list_sessions(executable, include_exited=True)
                            if row.tag in expected_tags]
                except Exception as exc:
                    skipped.append({"command_id": command_id,
                                    "reason": "unbound_stop_discovery_%s_reconcile" % type(exc).__name__})
                    continue
                if len(rows) != 1:
                    skipped.append({"command_id": command_id,
                                    "reason": "unbound_stop_native_identity_missing_or_ambiguous"})
                    continue
                row = rows[0]
                target = NativeTarget(
                    session_id=row.session_id,
                    tag=row.tag,
                    workspace=row.workspace,
                    created_at_ms=row.created_at_ms,
                    engine=row.engine,
                )
                try:
                    attempt_id = get_or_create_attempt(journal_dir, str(command_id), str(correlation or ""))
                    server_claim = request("POST", "/api/session-commands/" + str(command_id) + "/claim",
                                           {"correlation": correlation, "attempt_id": attempt_id})
                except Exception as exc:
                    skipped.append({"command_id": command_id,
                                    "reason": "unbound_stop_claim_unresolved_%s_reconcile" % type(exc).__name__})
                    continue
                token = server_claim.get("claim_token") if isinstance(server_claim, dict) else None
                if not isinstance(token, str) or not token:
                    skipped.append({"command_id": command_id, "reason": "claim_missing_token_reconcile"})
                    continue
                stopped = stop_session(executable, target)
                if getattr(stopped, "state", None) != "stopped":
                    skipped.append({"command_id": command_id,
                                    "reason": "unbound_stop_exit_unverified_reconcile"})
                    continue
                native_payload = {"native_id": target.session_id, "tag": target.tag,
                                  "workspace": target.workspace}
                try:
                    ack = request("POST", "/api/session-commands/" + str(command_id) + "/ack",
                                  {"correlation": correlation, "outcome": "delivered",
                                   "confirmed_exit": True,
                                   "native": native_payload,
                                   "reason": "unbound stop: " + str(getattr(stopped, "reason", "containment verified"))[:180],
                                   "claim_token": token})
                    if not isinstance(ack, dict) or ack.get("command_id") != command_id or ack.get("state") != "acked":
                        skipped.append({"command_id": command_id, "reason": "ack_unverified_reconcile"})
                        continue
                    processed.append({"command_id": command_id, "op": "stop",
                                      "state": "stopped", "reason": "unbound_target_stopped_and_verified"})
                except Exception as exc:
                    skipped.append({"command_id": command_id,
                                    "reason": "ack_unresolved_%s_reconcile" % type(exc).__name__})
            except (SessionControlError, SessionUncertain) as exc:
                skipped.append({"command_id": command_id, "reason": str(exc)[:300]})
            except Exception as exc:
                skipped.append({"command_id": command_id,
                                "reason": "unbound_stop_%s_reconcile" % type(exc).__name__})
            continue
        # Stable attempt BEFORE claim (lost-reply safe).
        try:
            attempt_id = get_or_create_attempt(journal_dir, str(command_id), str(correlation or ""))
        except (SessionControlError, SessionUncertain) as exc:
            skipped.append({"command_id": command_id, "reason": str(exc)[:300]})
            continue
        # A process without a bound sender identity cannot create a replyable
        # native envelope. Hold it before claiming; this is not a command
        # delivery failure and should not consume a server-side fence.
        if command.get("op") == "send":
            try:
                qualify_sender_binding(executable, sender_workspace)
            except (SessionControlError, SessionUncertain):
                skipped.append({"command_id": command_id,
                                "reason": "native_sender_unbound_reconcile"})
                continue
        try:
            server_claim = request("POST", "/api/session-commands/" + str(command_id) + "/claim",
                                   {"correlation": correlation, "attempt_id": attempt_id})
        except Exception as exc:
            # Lost claim reply: SAME attempt_id reconciles via replay on the
            # next cycle (server replays the token to the same attester).
            status = getattr(exc, "status", None)
            code = getattr(exc, "code", None)
            if type(status) is int and 400 <= status < 500 and isinstance(code, str) \
                    and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code):
                skipped.append({"command_id": command_id, "attempt_id": attempt_id,
                                "reason": "claim_refused:%s" % code})
            else:
                skipped.append({"command_id": command_id, "attempt_id": attempt_id,
                                "reason": "claim_unresolved_%s_same_attempt" % type(exc).__name__})
            continue
        if not isinstance(server_claim, dict) or not server_claim.get("claim_token"):
            code = server_claim.get("error") if isinstance(server_claim, dict) else None
            if not isinstance(code, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code):
                code = "unknown"
            skipped.append({"command_id": command_id, "attempt_id": attempt_id,
                            "reason": "claim_refused:%s" % code})
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
        processed.append({"command_id": command_id, "attempt_id": attempt_id,
                          "op": command.get("op"),
                          "state": outcome.get("state"),
                          **({"reason": str(outcome["reason"])[:300]}
                             if isinstance(outcome.get("reason"), str) and outcome.get("reason") else {}),
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
        pending = _pending_reconcile_names(journal_dir, limit=20, command_kind=command_kind)
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
    try:
        relayed = [] if command_kind == "stop" else session_messages.relay_replies(
            journal_dir=journal_dir, executable=executable, request=request, now_ms=now_ms)
    except Exception:
        relayed = []
    return {"offers_seen": len(offers), "processed": processed, "skipped": skipped,
            "reconciled": reconciled, "relayed": relayed,
            "message_workers": [] if command_kind == "stop" else workers.drain_completed()}


def _is_terminal_ack_error(err: Any) -> bool:
    # Only definitive server verdicts close a journal; 401/408/429 and
    # transport errors stay pending for the next cycle.
    s = str(err or "")
    return s.startswith("server_terminal_") or any(
        s.startswith("Session HTTP request failed with status %d" % code)
        for code in (400, 404, 409))


def _pending_reconcile_names(journal_dir: str, limit: int = 20,
                             command_kind: Optional[str] = None) -> list[str]:
    """Fair pending scan: completed/terminal journals never consume the cap.

    A journal needs attention when it is an unreconciled send (state unknown
    with a journaled original envelope), an uncertain stop with a persisted
    containment oracle (read-only SAME-claim reconcile, never a second kill),
    or an unsubmitted proven ack for a completed effect (ack_payload present,
    not submitted, no terminal server verdict). Everything else — submitted,
    terminal, or malformed — is skipped but retained. Active/recent claims sort
    ahead of older expired journals (tie-broken by name for determinism) so
    historical journals can never starve a fresh pending command.
    """
    import json as _json

    try:
        names = sorted(
            name for name in os.listdir(journal_dir)
            if name.startswith("cmd-") and name.endswith(".json"))
    except OSError:
        return []
    candidates: list[tuple[int, str]] = []
    for name in names:
        path = os.path.join(journal_dir, name)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                record = _json.load(fh)
        except (OSError, ValueError, UnicodeDecodeError):
            continue  # corrupt journal: fail closed, surface via manual reconcile
        if not isinstance(record, dict):
            continue
        if command_kind == "stop" and record.get("op") != "stop":
            continue
        if command_kind == "non_stop" and record.get("op") == "stop":
            continue
        ack_err = record.get("ack_error")
        if record.get("ack_submitted") or _is_terminal_ack_error(ack_err):
            continue
        expires = record.get("expires_at_ms")
        sort_ts = -expires if type(expires) is int else 0
        if record.get("state") in ("unknown", "uncertain") and record.get("original_native_message_id"):
            candidates.append((sort_ts, name))
        elif (record.get("op") == "stop" and record.get("state") == "uncertain"
                and isinstance(record.get("stop_oracle"), dict)
                and record["stop_oracle"].get("session_id")):
            candidates.append((sort_ts, name))
        elif isinstance(record.get("ack_payload"), dict):
            candidates.append((sort_ts, name))
    candidates.sort()
    return [name for _, name in candidates[:limit]]
