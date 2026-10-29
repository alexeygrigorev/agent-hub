"""Host delivery of Hub session messages (docs/SESSION-MESSAGING.md §5-6).

The Hub offers each message to a bound session as a session command of kind
``message``. Per command this module, under the server claim:

1. writes the rendered text ONCE into the session's native mailbox
   (``aplexer message send``; the envelope id is journaled before anything
   else can repeat it), then
2. for ``delivery=tui`` asks the native deliver guard to submit that same
   envelope at a ready prompt (``aplexer message deliver``). Not ready acks
   ``not_ready`` (the Hub backs off); an unknown outcome acks ``uncertain``
   and is never typed again.

Replies: the controller authored the envelope, so a session's
``a message reply <id> "text"`` lands in the controller's mailbox. Each new
reply to a delivered envelope is relayed once to the Hub as
``session:<hub-session-id>`` replying to the original message. Linux and
Windows run the same code; aplexer owns the PTY/ConPTY specifics.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Callable, Optional

from .native_sessions import (
    NativeTarget,
    SessionControlError,
    SessionUncertain,
    deliver_recorded,
    fetch_message_log,
    send_inbox,
)
from .session_executor import _journal_path, _lock_held, _read_journal, _write_journal, verify_server_claim

RELAY_WINDOW_MS = 7 * 24 * 3600 * 1000
MAX_RELAY_JOURNALS = 50


def _now_ms() -> int:
    return int(time.time() * 1000)


def _target(offer: dict[str, Any]) -> NativeTarget:
    binding = offer.get("binding")
    if not isinstance(binding, dict):
        raise SessionControlError("Message target is not bound yet")
    created = binding.get("native_created_at_ms")
    if type(created) is not int or created <= 0:
        raise SessionControlError("Native incarnation (native_created_at_ms) is required; refusing")
    return NativeTarget(session_id=binding.get("native_id"), tag=binding.get("tag"),
                        workspace=binding.get("workspace"), created_at_ms=created,
                        engine=offer.get("engine") or None).validated()


def _ack(request: Callable, offer: dict[str, Any], token: str, target: NativeTarget, outcome: str,
         reason: Optional[str], envelope: Optional[str]) -> Any:
    payload: dict[str, Any] = {
        "correlation": offer.get("correlation"), "outcome": outcome, "claim_token": token,
        "native": {"native_id": target.session_id, "tag": target.tag, "workspace": target.workspace},
    }
    if reason:
        payload["reason"] = reason[:512]
    if envelope:
        payload["proof"] = {"native_message_id": envelope}
    return request("POST", "/api/session-commands/" + str(offer["command_id"]) + "/ack", payload)


def handle_message_offer(offer: dict[str, Any], *, journal_dir: str, executable: str, request: Callable,
                         attempt_id: str, sender_workspace: Optional[str] = None,
                         now_ms: Optional[int] = None) -> dict[str, Any]:
    """Claim, deliver and ack one message offer. Returns a small result row."""
    command_id = str(offer.get("command_id"))
    payload = offer.get("payload") if isinstance(offer.get("payload"), dict) else {}
    body, delivery, message_id = payload.get("body"), payload.get("delivery"), payload.get("message_id")
    if not isinstance(body, str) or not body or delivery not in ("tui", "inbox") or not isinstance(message_id, str):
        raise SessionControlError("Malformed message offer")
    target = _target(offer)
    claim = request("POST", "/api/session-commands/" + command_id + "/claim",
                    {"correlation": offer.get("correlation"), "attempt_id": attempt_id})
    if not isinstance(claim, dict) or not claim.get("claim_token"):
        raise SessionUncertain("Message claim rejected")
    proved = verify_server_claim(claim, now_ms=now_ms)
    token = proved["claim_token"]
    journal_file = _journal_path(journal_dir, "msg-" + command_id)
    with _lock_held(journal_file):
        record = _read_journal(journal_file) or {
            "command_id": command_id, "message_id": message_id, "hub_session_id": offer.get("session_id"),
            "from_actor": payload.get("from_actor"), "delivery": delivery, "created_ms": now_ms or _now_ms(),
            "target": {"session_id": target.session_id, "tag": target.tag, "workspace": target.workspace,
                       "created_at_ms": target.created_at_ms},
            "state": None, "envelope_id": None, "relayed": []}
        if record.get("message_id") != message_id:
            raise SessionControlError("Journal belongs to another message")
        if record.get("state") == "uncertain":
            # Never typed twice: the earlier attempt may have reached the TUI.
            _ack(request, offer, token, target, "uncertain", "delivery_uncertain_not_retyped", record.get("envelope_id"))
            return {"command_id": command_id, "state": "uncertain", "replayed": True}
        if record.get("state") == "delivered":
            _ack(request, offer, token, target, "delivered", None, record.get("envelope_id"))
            return {"command_id": command_id, "state": "delivered", "replayed": True}
        if not record.get("envelope_id"):
            # Mark the attempt BEFORE the native write: a crash between write
            # and journal leaves state 'sending' and is reported uncertain.
            if record.get("state") == "sending":
                record["state"] = "uncertain"
                _write_journal(journal_file, record)
                _ack(request, offer, token, target, "uncertain", "inbox_write_unknown", None)
                return {"command_id": command_id, "state": "uncertain"}
            record["state"] = "sending"
            _write_journal(journal_file, record)
            try:
                sent, sender = send_inbox(executable, target, body,
                                          {"correlation_id": command_id, "hub_message_id": message_id},
                                          kind="hub-message", sender_workspace=sender_workspace)
            except SessionControlError as exc:
                record["state"] = "refused"
                _write_journal(journal_file, record)
                _ack(request, offer, token, target, "refused", str(exc)[:300], None)
                return {"command_id": command_id, "state": "refused"}
            except SessionUncertain as exc:
                record["state"] = "uncertain"
                _write_journal(journal_file, record)
                _ack(request, offer, token, target, "uncertain", str(exc)[:300], None)
                return {"command_id": command_id, "state": "uncertain"}
            record["envelope_id"] = sent.native_id
            record["sender_workspace"] = sender.workspace
            record["state"] = "inboxed"
            _write_journal(journal_file, record)
        envelope = record["envelope_id"]
        if delivery == "inbox":
            outcome, reason = "delivered", None
        else:
            try:
                result = deliver_recorded(executable, target, envelope)
            except SessionUncertain as exc:
                record["state"] = "uncertain"
                _write_journal(journal_file, record)
                _ack(request, offer, token, target, "uncertain", str(exc)[:300], envelope)
                return {"command_id": command_id, "state": "uncertain"}
            except SessionControlError as exc:
                if str(exc) == "send_unsupported_headless":
                    # No input channel: the native inbox copy is the delivery.
                    result = None
                else:
                    record["state"] = "refused"
                    _write_journal(journal_file, record)
                    _ack(request, offer, token, target, "refused", str(exc)[:300], envelope)
                    return {"command_id": command_id, "state": "refused"}
            if result is None:
                outcome, reason = "delivered", "inbox_only_headless"
            elif result.state == "transported":
                outcome, reason = "delivered", None
            elif result.state == "refused":
                record["state"] = "not_ready"
                _write_journal(journal_file, record)
                _ack(request, offer, token, target, "refused", "not_ready: " + (result.reason or "prompt not ready"), envelope)
                return {"command_id": command_id, "state": "not_ready"}
            else:
                outcome, reason = "delivered", "inbox_only_deliver_unsupported"
        record["state"] = "delivered"
        _write_journal(journal_file, record)
        _ack(request, offer, token, target, outcome, reason, envelope)
        return {"command_id": command_id, "state": "delivered"}


def _message_journals(journal_dir: str) -> list[str]:
    try:
        return sorted(n for n in os.listdir(journal_dir) if n.startswith("msg-") and n.endswith(".json"))
    except OSError:
        return []


def relay_replies(*, journal_dir: str, executable: str, request: Callable,
                  now_ms: Optional[int] = None) -> list[dict[str, Any]]:
    """Relay new native replies to delivered Hub messages, each exactly once
    (Hub idempotency on the reply envelope id backs up the local record)."""
    now = now_ms or _now_ms()
    by_mailbox: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for name in _message_journals(journal_dir)[-MAX_RELAY_JOURNALS * 4:]:
        path = os.path.join(journal_dir, name)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                record = json.load(fh)
        except (OSError, ValueError, UnicodeDecodeError):
            continue
        if (not isinstance(record, dict) or record.get("state") != "delivered" or not record.get("envelope_id")
                or not record.get("sender_workspace") or not record.get("hub_session_id")
                or not record.get("from_actor") or now - int(record.get("created_ms") or 0) > RELAY_WINDOW_MS):
            continue
        by_mailbox.setdefault(record["sender_workspace"], []).append((path, record))
    relayed: list[dict[str, Any]] = []
    for mailbox, records in by_mailbox.items():
        try:
            log = fetch_message_log(executable, mailbox)
        except (SessionControlError, SessionUncertain):
            continue
        for path, record in records[-MAX_RELAY_JOURNALS:]:
            for env in log:
                author = env.get("from") if isinstance(env.get("from"), dict) else {}
                reply_id = env.get("id")
                if (env.get("reply_to") != record["envelope_id"] or not isinstance(reply_id, str)
                        or str(author.get("session_id", "")).lower() != str(record["target"]["session_id"]).lower()
                        or reply_id in record.get("relayed", []) or not isinstance(env.get("body"), str)):
                    continue
                try:
                    # The bus limits UTF-8 bytes; drop a partial trailing code point.
                    body = env["body"].encode("utf-8", "replace")[:16384].decode("utf-8", "ignore")
                    sent = request("POST", "/api/bus/send", {
                        "from_actor": "session:" + record["hub_session_id"], "to_actor": record["from_actor"],
                        "kind": "reply", "body": body, "reply_to": record["message_id"],
                        "idempotency_key": "native-" + reply_id})
                except Exception as exc:
                    relayed.append({"message_id": record["message_id"], "error": type(exc).__name__})
                    continue
                with _lock_held(path):
                    fresh = _read_journal(path) or record
                    fresh.setdefault("relayed", []).append(reply_id)
                    _write_journal(path, fresh)
                    record = fresh
                relayed.append({"message_id": record["message_id"], "reply_id": (sent or {}).get("id")})
    return relayed


_last_provision = [0.0]
PROVISION_INTERVAL_S = 120.0


def provision_credentials(request: Callable, base_url: Optional[str], *, auth_dir=None,
                          clock: Callable[[], float] = time.monotonic, force: bool = False) -> list[str]:
    """Mint and store each running session's own messaging credential
    (docs/SESSION-MESSAGING.md §8). The file lands owner-only in the session's
    own workspace at ``<workspace>/.local/hub-session-auth/<native_id>.json``
    — the location ``hub msg`` re-finds via APLEXER_SESSION_ID +
    APLEXER_WORKSPACE — and never enters the engine environment or a
    repository (``.local`` is gitignored)."""
    if not force and clock() - _last_provision[0] < PROVISION_INTERVAL_S:
        return []
    _last_provision[0] = clock()
    if not base_url:
        return []
    from .msg_cli import session_auth_dir
    from . import private_fs
    pending = (request("GET", "/api/session-messaging/credentials", None) or {}).get("sessions", [])
    written = []
    for row in pending[:10]:
        sid, native = row.get("session_id"), str(row.get("native_id", "")).lower()
        workspace = row.get("workspace")
        if not isinstance(sid, str) or len(native) != 36 or not isinstance(workspace, str) or not workspace:
            continue
        minted = request("POST", "/api/sessions/" + sid + "/messaging-credential", {})
        if not isinstance(minted, dict) or not minted.get("access_token"):
            continue
        target_dir = auth_dir or session_auth_dir(workspace)
        os.makedirs(target_dir, mode=0o700, exist_ok=True)
        record = {"base_url": base_url, "client_kind": "agent", "actor": minted.get("actor"),
                  "access_token": minted["access_token"], "refresh_token": minted.get("refresh_token"),
                  "hub_session_id": sid}
        private_fs.atomic_write(os.path.join(str(target_dir), native + ".json"),
                                json.dumps(record).encode("utf-8"), 16384)
        written.append(sid)
    return written
