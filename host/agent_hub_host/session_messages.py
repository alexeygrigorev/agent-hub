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
import hashlib
import os
import re
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Optional

from .native_sessions import (
    NativeTarget,
    SessionControlError,
    SessionUncertain,
    capture_output,
    deliver_recorded,
    fetch_message_log,
    qualify_target,
    send_inbox,
)
from .session_executor import _journal_path, _lock_held, _read_journal, _write_journal, verify_server_claim

RELAY_WINDOW_MS = 7 * 24 * 3600 * 1000
MAX_RELAY_JOURNALS = 50

# M4 AC2 (docs/SESSION-MESSAGING.md §5.4): hook readiness (waiting|idle) on
# Windows is unverified until proven live for an engine. Until then a
# delivery=tui request to a Windows session falls back to the durable inbox
# copy with this reason — never a silent typing into an unknown state. The
# operator enables an engine after the live proof with, e.g.,
# ``AGENT_HUB_WINDOWS_TUI_ENGINES=codex,claude`` in the host environment.
WINDOWS_TUI_UNVERIFIED_REASON = "tui_unverified_platform"
WINDOWS_TUI_ENGINES_ENV = "AGENT_HUB_WINDOWS_TUI_ENGINES"


def windows_tui_verified(engine: Optional[str], platform: Optional[str] = None) -> bool:
    """Whether TUI injection is proven on this host for the target engine.

    Linux hosts run the verified path. On Windows only engines explicitly
    listed in ``AGENT_HUB_WINDOWS_TUI_ENGINES`` (comma-separated, set by the
    operator after the live hook-readiness proof) may be typed into; every
    other engine — including an unknown one — takes the inbox fallback.
    ``platform`` is injectable for unit tests (production passes none, which
    reads the real ``sys.platform``); never fake ``sys.platform`` itself —
    filesystem helpers dispatch on it.
    """
    if (sys.platform if platform is None else platform) != "win32":
        return True
    if not isinstance(engine, str) or not engine.strip():
        return False
    verified = {p.strip().lower() for p in os.environ.get(WINDOWS_TUI_ENGINES_ENV, "").split(",")
                if p.strip()}
    return engine.strip().lower() in verified


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


def _marker(message_id: str) -> str:
    return "⟦hub:%s⟧" % message_id[-8:]


def _message_journal_path(journal_dir: str, message_id: str) -> str:
    """Stable receipt key for a Hub message, independent of command retries."""
    if not isinstance(journal_dir, str) or not os.path.isabs(journal_dir):
        raise SessionControlError("journal_dir must be an absolute path")
    if (not isinstance(message_id, str) or not message_id or len(message_id) > 128
            or any(ord(ch) < 0x20 or ord(ch) == 0x7f for ch in message_id)):
        raise SessionControlError("Invalid Hub message id")
    key = hashlib.sha256(message_id.encode("utf-8")).hexdigest()
    return os.path.join(journal_dir, "msg-id-" + key + ".json")


def _rendered_body(body: Any, message_id: str) -> str:
    if not isinstance(body, str) or not body:
        raise SessionControlError("Malformed rendered message body")
    raw = body.encode("utf-8")
    if len(raw) > 2048:
        raise SessionControlError("Rendered message exceeds 2 KiB")
    if _marker(message_id) not in body:
        raise SessionControlError("Rendered message marker is missing")
    if any((ord(ch) < 0x20 and ch not in "\n\t") or 0x7f <= ord(ch) <= 0x9f for ch in body):
        raise SessionControlError("Rendered message contains a control character")
    return body


def _record_target(record: dict[str, Any]) -> NativeTarget:
    target = record.get("target")
    if not isinstance(target, dict):
        raise SessionControlError("Message journal has no native target")
    return NativeTarget(session_id=target.get("session_id"), tag=target.get("tag"),
                        workspace=target.get("workspace"), created_at_ms=target.get("created_at_ms"),
                        engine=target.get("engine")).validated()


def _same_target(record: dict[str, Any], target: NativeTarget) -> bool:
    prior = record.get("target")
    return isinstance(prior, dict) and all((
        prior.get("session_id", "").lower() == target.session_id.lower(),
        prior.get("tag") == target.tag,
        prior.get("workspace") == target.workspace,
        prior.get("created_at_ms") == target.created_at_ms,
        prior.get("engine") in (None, target.engine),
    ))


def _ack_record(request: Callable, record: dict[str, Any], target: NativeTarget,
                outcome: str, reason: Optional[str], envelope: Optional[str]) -> Any:
    offer = {"command_id": record["command_id"], "correlation": record["correlation"]}
    return _ack(request, offer, record.get("ack_claim_token") or record["claim_token"],
                target, outcome, reason, envelope)


def _finish_ack(journal_file: str, record: dict[str, Any], request: Callable, target: NativeTarget,
                outcome: str, final_state: str, reason: Optional[str], envelope: Optional[str]) -> Any:
    """Persist the exact ack intent so a lost HTTP response is replayable."""
    record["state"] = final_state + "_pending_ack"
    record["ack_outcome"] = outcome
    record["ack_reason"] = reason
    record["ack_envelope_id"] = envelope
    record["ack_claim_token"] = record["claim_token"]
    record["ack_submitted"] = False
    _write_journal(journal_file, record)
    result = _ack_record(request, record, target, outcome, reason, envelope)
    record["state"] = final_state
    record["ack_submitted"] = True
    _write_journal(journal_file, record)
    return result


def _deliver_inboxed(journal_file: str, record: dict[str, Any], executable: str,
                     request: Callable, target: NativeTarget) -> dict[str, Any]:
    """Start one guarded TUI attempt; the durable pre-effect state fences retries."""
    envelope = record.get("envelope_id")
    if not isinstance(envelope, str) or not envelope:
        raise SessionControlError("Message journal has no native envelope")
    record["state"] = "delivering"
    record["delivery_started_ms"] = _now_ms()
    _write_journal(journal_file, record)
    try:
        result = deliver_recorded(executable, target, envelope)
    except SessionUncertain as exc:
        _finish_ack(journal_file, record, request, target, "uncertain", "uncertain",
                    str(exc)[:300], envelope)
        return {"command_id": record["command_id"], "state": "uncertain"}
    except SessionControlError as exc:
        if str(exc) == "send_unsupported_headless":
            _finish_ack(journal_file, record, request, target, "delivered", "delivered",
                        "inbox_only_headless", envelope)
            return {"command_id": record["command_id"], "state": "delivered"}
        _finish_ack(journal_file, record, request, target, "refused", "refused", str(exc)[:300], envelope)
        return {"command_id": record["command_id"], "state": "refused"}

    if result.state == "refused":
        _finish_ack(journal_file, record, request, target, "not_ready", "not_ready",
                    result.reason or "native deliver guard not ready", envelope)
        return {"command_id": record["command_id"], "state": "not_ready"}
    if result.state == "transported":
        _finish_ack(journal_file, record, request, target, "delivered", "delivered", None, envelope)
        return {"command_id": record["command_id"], "state": "delivered"}
    if result.state == "recorded":
        _finish_ack(journal_file, record, request, target, "delivered", "delivered",
                    "inbox_only_deliver_unsupported", envelope)
        return {"command_id": record["command_id"], "state": "delivered"}
    _finish_ack(journal_file, record, request, target, "uncertain", "uncertain",
                "native deliver returned an unknown state", envelope)
    return {"command_id": record["command_id"], "state": "uncertain"}


def handle_message_offer(offer: dict[str, Any], *, journal_dir: str, executable: str, request: Callable,
                         attempt_id: str, sender_workspace: Optional[str] = None,
                         now_ms: Optional[int] = None) -> dict[str, Any]:
    """Claim, deliver and ack one message offer. Returns a small result row."""
    command_id = str(offer.get("command_id"))
    payload = offer.get("payload") if isinstance(offer.get("payload"), dict) else {}
    body, delivery, message_id = payload.get("body"), payload.get("delivery"), payload.get("message_id")
    if delivery not in ("tui", "inbox") or not isinstance(message_id, str):
        raise SessionControlError("Malformed message offer")
    body = _rendered_body(body, message_id)
    target = _target(offer)
    claim = request("POST", "/api/session-commands/" + command_id + "/claim",
                    {"correlation": offer.get("correlation"), "attempt_id": attempt_id})
    if not isinstance(claim, dict) or not claim.get("claim_token"):
        raise SessionUncertain("Message claim rejected")
    proved = verify_server_claim(claim, now_ms=now_ms)
    token = proved["claim_token"]
    journal_file = _message_journal_path(journal_dir, message_id)
    # Upgrade compatibility: the first M3 implementation keyed these receipts
    # by command id. Reuse an existing receipt before attempting any native write.
    legacy_file = _journal_path(journal_dir, "msg-" + command_id)
    if not os.path.exists(journal_file) and os.path.exists(legacy_file):
        journal_file = legacy_file
    with _lock_held(journal_file):
        record = _read_journal(journal_file) or {
            "command_id": command_id, "message_id": message_id, "hub_session_id": offer.get("session_id"),
            "from_actor": payload.get("from_actor"), "delivery": delivery, "created_ms": now_ms or _now_ms(),
            "target": {"session_id": target.session_id, "tag": target.tag, "workspace": target.workspace,
                       "created_at_ms": target.created_at_ms, "engine": target.engine},
            "marker": _marker(message_id), "state": None, "envelope_id": None, "relayed": []}
        if (record.get("message_id") != message_id or record.get("hub_session_id") != offer.get("session_id")
                or record.get("delivery") != delivery or not _same_target(record, target)):
            raise SessionControlError("Journal belongs to another message")
        prior_claim_token = record.get("claim_token")
        prior_state = record.get("state")
        prior_command_id = record.get("command_id")
        if isinstance(prior_state, str) and prior_state.endswith("_pending_ack"):
            final_state = prior_state[:-len("_pending_ack")]
            old_ack_token = record.get("ack_claim_token") or prior_claim_token
            if prior_command_id != command_id:
                raise SessionUncertain("Pending message ack belongs to a different command; reconcile before retry")
            if old_ack_token != token and final_state != "not_ready":
                raise SessionUncertain("Pending message ack claim changed before reconciliation")
        record["command_id"] = command_id
        record["correlation"] = offer.get("correlation")
        record["claim_token"] = token
        record["ack_submitted"] = bool(record.get("ack_submitted", False))
        _write_journal(journal_file, record)
        if isinstance(prior_state, str) and prior_state.endswith("_pending_ack"):
            final_state = prior_state[:-len("_pending_ack")]
            old_ack_token = record.get("ack_claim_token") or prior_claim_token
            if old_ack_token == token:
                _ack_record(request, record, target, record.get("ack_outcome", final_state),
                            record.get("ack_reason"), record.get("ack_envelope_id"))
                record["state"] = final_state
                record["ack_submitted"] = True
                _write_journal(journal_file, record)
                return {"command_id": command_id, "state": final_state, "replayed": True}
            # A fresh offer carries a different claim only after the Hub
            # accepted the previous not_ready ack and its backoff elapsed.
            record["state"] = final_state
            record["ack_submitted"] = True
            _write_journal(journal_file, record)
        if record.get("state") in ("uncertain", "delivering"):
            # A persisted delivering state means the prior process may have
            # typed already. It can only be resolved from a screen capture.
            if record.get("state") == "delivering":
                record["state"] = "uncertain"
                _write_journal(journal_file, record)
            _finish_ack(journal_file, record, request, target, "uncertain", "uncertain",
                        "delivery_uncertain_not_retyped", record.get("envelope_id"))
            return {"command_id": command_id, "state": "uncertain", "replayed": True}
        if record.get("state") in ("delivered", "delivered_pending_ack"):
            _finish_ack(journal_file, record, request, target, "delivered", "delivered",
                        record.get("ack_reason"), record.get("envelope_id"))
            return {"command_id": command_id, "state": "delivered", "replayed": True}
        if not record.get("envelope_id"):
            # Mark the attempt BEFORE the native write: a crash between write
            # and journal leaves state 'sending' and is reported uncertain.
            if record.get("state") == "sending":
                record["state"] = "uncertain"
                _write_journal(journal_file, record)
                _finish_ack(journal_file, record, request, target, "uncertain", "uncertain", "inbox_write_unknown", None)
                return {"command_id": command_id, "state": "uncertain"}
            record["state"] = "sending"
            _write_journal(journal_file, record)
            try:
                qualify_target(executable, target)
                sent, sender = send_inbox(executable, target, body,
                                          {"correlation_id": command_id, "hub_message_id": message_id},
                                          kind="hub-message", sender_workspace=sender_workspace)
            except SessionControlError as exc:
                record["state"] = "refused"
                _write_journal(journal_file, record)
                _finish_ack(journal_file, record, request, target, "refused", "refused", str(exc)[:300], None)
                return {"command_id": command_id, "state": "refused"}
            except SessionUncertain as exc:
                record["state"] = "uncertain"
                _write_journal(journal_file, record)
                _finish_ack(journal_file, record, request, target, "uncertain", "uncertain", str(exc)[:300], None)
                return {"command_id": command_id, "state": "uncertain"}
            record["envelope_id"] = sent.native_id
            record["sender_workspace"] = sender.workspace
            record["state"] = "inboxed"
            _write_journal(journal_file, record)
        envelope = record["envelope_id"]
        if delivery == "inbox":
            _finish_ack(journal_file, record, request, target, "delivered", "delivered", None, envelope)
            return {"command_id": command_id, "state": "delivered"}
        if not windows_tui_verified(target.engine):
            # §5.4: hook readiness is unverified on Windows; retain the
            # durable inbox copy and report why the TUI was skipped.
            _finish_ack(journal_file, record, request, target, "delivered", "delivered",
                        WINDOWS_TUI_UNVERIFIED_REASON, envelope)
            return {"command_id": command_id, "state": "delivered"}
        # Re-qualify after journaling the envelope and directly before TUI input.
        qualify_target(executable, target)
        return _deliver_inboxed(journal_file, record, executable, request, target)


def _message_journals(journal_dir: str) -> list[str]:
    try:
        return sorted(n for n in os.listdir(journal_dir) if n.startswith("msg-") and n.endswith(".json"))
    except OSError:
        return []


def _load_session_credential(record: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    target = record.get("target") if isinstance(record.get("target"), dict) else {}
    native_id = str(target.get("session_id", "")).lower()
    hub_session_id = record.get("hub_session_id")
    if not re.fullmatch(r"[0-9a-f-]{36}", native_id) or not isinstance(hub_session_id, str):
        raise SessionControlError("Session messaging identity is incomplete")
    from . import private_fs
    from .msg_cli import session_auth_dir
    path = os.path.join(str(session_auth_dir(str(target.get("workspace", "")))), native_id + ".json")
    try:
        profile = json.loads(private_fs.read_bytes(path, 16384).decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        raise SessionControlError("Session messaging credential is unavailable") from exc
    if (not isinstance(profile, dict) or profile.get("client_kind") != "agent"
            or profile.get("actor") != "session:" + hub_session_id
            or not isinstance(profile.get("access_token"), str) or not profile["access_token"]
            or not isinstance(profile.get("refresh_token"), str) or not profile["refresh_token"]
            or not isinstance(profile.get("base_url"), str) or not profile["base_url"]):
        raise SessionControlError("Session messaging credential does not match its Hub session")
    return path, profile


def _session_credential_request(profile_path: str, profile: dict[str, Any], method: str,
                                path: str, payload: Optional[dict[str, Any]]) -> Any:
    """Make a Hub request with the target session's credential and refresh it safely."""
    from . import api, config, private_fs

    base_url = profile["base_url"]
    api.check_url(base_url)

    def load() -> dict[str, Any]:
        try:
            value = json.loads(private_fs.read_bytes(profile_path, 16384).decode("utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def save(value: dict[str, Any]) -> None:
        private_fs.atomic_write(profile_path, json.dumps(value, sort_keys=True).encode("utf-8"), 16384)

    def send(token: str) -> Any:
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(base_url.rstrip("/") + path, data=data,
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + token}, method=method)
        return api.safe_http_json(req)

    # Session CLI and host relay share one credential file. Keep refresh
    # single-writer across host processes and only use a validated Hub token
    # exchange for this exact profile path.
    with private_fs.exclusive_lock(profile_path + ".refresh.lock"):
        current = load()
        if current.get("actor") != profile.get("actor") or current.get("hub_session_id") != profile.get("hub_session_id"):
            raise SessionControlError("Session messaging credential changed during relay")
        try:
            return send(current["access_token"])
        except urllib.error.HTTPError as exc:
            if exc.code != 401:
                raise
        exchange = config._TokenExchange(base_url, "/api/token/refresh", current,
                                          _load_fn=load, _save_fn=save)
        refresh = json.dumps({"grant_type": "refresh_token", "refresh_token": current["refresh_token"]}).encode("utf-8")
        req = urllib.request.Request(exchange.url, data=refresh, headers={"Content-Type": "application/json"}, method="POST")
        tokens = api.safe_http_json(req, _token_exchange=exchange)
        exchange.commit(tokens)
        rotated = load()
        return send(rotated["access_token"])


def relay_replies(*, journal_dir: str, executable: str, request: Callable,
                  now_ms: Optional[int] = None, session_request: Optional[Callable] = None) -> list[dict[str, Any]]:
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
            # Replies are authored by sessions outside this process. Read a
            # fresh mailbox snapshot so a just-arrived reply is not hidden by
            # the short shared message-log cache.
            log = fetch_message_log(executable, mailbox, fresh=True)
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
                    profile_path, profile = _load_session_credential(record)
                    reply_payload = {
                        "to_actor": record["from_actor"], "kind": "reply", "body": body,
                        "reply_to": record["message_id"], "idempotency_key": "native:" + reply_id}
                    if session_request is None:
                        sent = _session_credential_request(profile_path, profile, "POST", "/api/bus/reply", reply_payload)
                    else:
                        sent = session_request(profile, "POST", "/api/bus/reply", reply_payload)
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


UNCERTAIN_CAPTURE_INTERVAL_MS = 15000


def pending_message_reconciliations(journal_dir: str, *, now_ms: Optional[int] = None,
                                    limit: int = 32) -> list[tuple[str, dict[str, Any]]]:
    """Return durable message work that can resume without a second blind write."""
    now = now_ms or _now_ms()
    candidates = []
    pending_states = {"sending", "inboxed", "delivering", "uncertain",
                      "delivered_pending_ack", "not_ready_pending_ack",
                      "refused_pending_ack", "uncertain_pending_ack"}
    for name in _message_journals(journal_dir):
        path = os.path.join(journal_dir, name)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                record = json.load(fh)
        except (OSError, ValueError, UnicodeDecodeError):
            continue
        if (not isinstance(record, dict) or record.get("state") not in pending_states
                or not isinstance(record.get("message_id"), str)
                or not isinstance(record.get("command_id"), str)
                or not isinstance(record.get("hub_session_id"), str)
                or not isinstance(record.get("correlation"), str)
                or not isinstance(record.get("claim_token"), str)
                or not record.get("claim_token")):
            continue
        if record.get("state") == "inboxed" and record.get("delivery") != "tui":
            continue
        try:
            last_capture = int(record.get("last_capture_at_ms") or 0)
            created = int(record.get("created_ms") or 0)
        except (TypeError, ValueError, OverflowError):
            continue
        if record.get("state") == "uncertain" and now - last_capture < UNCERTAIN_CAPTURE_INTERVAL_MS:
            continue
        candidates.append((last_capture, created, path, record))
    candidates.sort(key=lambda item: (item[0], item[1], item[2]))
    return [(path, record) for _last, _created, path, record in candidates[:max(1, limit)]]


def reconcile_message_record(journal_file: str, *, executable: str, request: Callable,
                             now_ms: Optional[int] = None) -> dict[str, Any]:
    """Resume a recorded intent or resolve uncertain TUI delivery by screen marker."""
    now = now_ms or _now_ms()
    with _lock_held(journal_file):
        record = _read_journal(journal_file)
        if not record or not isinstance(record.get("message_id"), str):
            return {"state": "skipped"}
        target = _record_target(record)
        state = record.get("state")

        if isinstance(state, str) and state.endswith("_pending_ack"):
            final_state = state[:-len("_pending_ack")]
            _ack_record(request, record, target, record.get("ack_outcome", final_state),
                        record.get("ack_reason"), record.get("ack_envelope_id"))
            record["state"] = final_state
            record["ack_submitted"] = True
            _write_journal(journal_file, record)
            state = final_state
            if state != "uncertain":
                return {"command_id": record["command_id"], "state": state, "reconciled": True}

        if state == "sending":
            # The native write may have happened, but the envelope id was not
            # durably captured. Preserve at-most-once semantics.
            record["state"] = "uncertain"
            _write_journal(journal_file, record)
            _finish_ack(journal_file, record, request, target, "uncertain", "uncertain",
                        "inbox_write_unknown", None)
            return {"command_id": record["command_id"], "state": "uncertain"}

        if state == "delivering":
            # A process exited after recording the pre-deliver fence. Capture
            # may prove the marker, but this state is never re-typed.
            record["state"] = "uncertain"
            _write_journal(journal_file, record)
            if not record.get("ack_submitted"):
                _finish_ack(journal_file, record, request, target, "uncertain", "uncertain",
                            "delivery_uncertain_not_retyped", record.get("envelope_id"))
            state = "uncertain"

        if state == "inboxed" and record.get("delivery") == "tui":
            qualify_target(executable, target)
            return _deliver_inboxed(journal_file, record, executable, request, target)

        if state != "uncertain" or record.get("delivery") != "tui" or not record.get("envelope_id"):
            return {"command_id": record.get("command_id"), "state": state or "skipped"}
        if record.get("ack_submitted") is False:
            _finish_ack(journal_file, record, request, target, "uncertain", "uncertain",
                        record.get("ack_reason") or "delivery_uncertain_not_retyped", record.get("envelope_id"))

        qualify_target(executable, target)
        capture = capture_output(executable, target, limit_bytes=65536, mode="screen")
        capture_hash = hashlib.sha256(capture.text.encode("utf-8", "replace")).hexdigest()
        record["last_capture_at_ms"] = now
        record["last_capture_sha256"] = capture_hash
        if _marker(record["message_id"]) in capture.text:
            reason = "screen_marker_found capture_sha256=" + capture_hash
            _finish_ack(journal_file, record, request, target, "delivered", "delivered",
                        reason, record.get("envelope_id"))
            return {"command_id": record["command_id"], "state": "delivered", "reconciled": True,
                    "capture_sha256": capture_hash}
        _write_journal(journal_file, record)
        return {"command_id": record["command_id"], "state": "uncertain", "marker_found": False,
                "capture_sha256": capture_hash}


_last_provision = [0.0]
PROVISION_INTERVAL_S = 600.0


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


_last_intake_provision = [0.0]


def provision_intake_credentials(request: Callable, base_url: Optional[str], *,
                                 clock: Callable[[], float] = time.monotonic, force: bool = False) -> list[str]:
    """Mint and store the intake-scoped credential of each running native
    session that has an operator-verified intake link. The file lands owner-only
    at ``<workspace>/.local/hub-worker-auth/<native_id>.json`` (the session's own
    workspace, gitignored) and is found by ``hub`` through APLEXER_SESSION_ID +
    APLEXER_WORKSPACE (worker_auth.native_session_path). It never enters the
    engine environment. The server revokes it when the session stops."""
    if not force and clock() - _last_intake_provision[0] < PROVISION_INTERVAL_S:
        return []
    _last_intake_provision[0] = clock()
    if not base_url:
        return []
    from . import private_fs
    pending = (request("GET", "/api/session-intake/credentials", None) or {}).get("sessions", [])
    written = []
    for row in pending[:10]:
        sid, native = row.get("session_id"), str(row.get("native_id", "")).lower()
        workspace = row.get("workspace")
        if not isinstance(sid, str) or len(native) != 36 or not isinstance(workspace, str) or not os.path.isabs(workspace):
            continue
        minted = request("POST", "/api/sessions/" + sid + "/intake-credential", {})
        if (not isinstance(minted, dict) or not str(minted.get("access_token", "")).startswith("hubt_")
                or not str(minted.get("refresh_token", "")).startswith("hubr_")):
            continue
        profile = {"base_url": minted.get("base_url") or base_url, "client_kind": "agent",
                   "access_token": minted["access_token"], "refresh_token": minted["refresh_token"],
                   "worker_launch_id": sid, "worker_expires_at": minted.get("worker_expires_at")}
        target_dir = os.path.join(workspace, ".local", "hub-worker-auth")
        os.makedirs(target_dir, mode=0o700, exist_ok=True)
        private_fs.atomic_write(os.path.join(target_dir, native + ".json"), json.dumps(profile).encode("utf-8"), 32768)
        written.append(sid)
    return written
