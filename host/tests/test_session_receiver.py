"""Message delivery workers must keep native capture and stop polling responsive."""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import threading
import time
import uuid
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from agent_hub_host import session_executor as se
from agent_hub_host import session_receiver as rc


HUB_SESSION = "22222222-2222-4222-8222-222222222222"


@pytest.fixture
def journal_dir():
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".local", "tmp"))
    os.makedirs(root, exist_ok=True)
    path = tempfile.mkdtemp(prefix="session-receiver-", dir=root)
    os.chmod(path, 0o700)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _custody():
    return se.CallerCustody(actor="machine:hetzner1", machine_id="hetzner1",
                            credential_family="host-hetzner1")


def _offer(kind: str) -> dict:
    command_id = str(uuid.uuid4())
    return {"command_id": command_id, "session_id": HUB_SESSION, "kind": kind,
            "generation": 1, "correlation": "cmd:%s:gen:1" % command_id,
            "payload": {"message_id": "01JD3K7QXYZABCDEFGHJKMNPQR"} if kind == "message" else {}}


def test_blocked_message_delivery_does_not_delay_capture_or_stop(journal_dir):
    offers = [_offer("message"), _offer("capture"), _offer("stop")]
    entered = threading.Event()
    release = threading.Event()
    completed_native_ops = []
    workers = rc.MessageDeliveryWorkers()

    def blocked_delivery(*args, **kwargs):
        entered.set()
        assert release.wait(3), "test did not release blocked delivery"
        return {"state": "delivered"}

    def request(method, path, payload):
        if method == "GET" and path == "/api/session-commands/offers":
            return {"offers": offers}
        if method == "POST" and path.endswith("/claim"):
            return {"command_id": path.split("/")[3], "claim_token": "tok-" + uuid.uuid4().hex,
                    "holder": "hetzner1", "generation": 1,
                    "expires_at_ms": int(time.time() * 1000) + 60000}
        raise AssertionError((method, path))

    def command_for(offer):
        return {"op": offer["kind"], "command_id": offer["command_id"],
                "session_id": offer["session_id"], "generation": 1, "target": {"id": "native"}}

    def execute(command, *args, **kwargs):
        completed_native_ops.append(command["op"])
        return {"state": "acked", "ack": {"submitted": True}}

    try:
        with patch.object(rc.session_messages, "handle_message_offer", side_effect=blocked_delivery), \
             patch.object(rc.session_messages, "pending_message_reconciliations", return_value=[]), \
             patch.object(rc.session_messages, "relay_replies", return_value=[]), \
             patch.object(rc, "offer_to_command", side_effect=command_for), \
             patch.object(rc, "handle_claimed_command", side_effect=execute), \
             patch.object(rc, "_pending_reconcile_names", return_value=[]):
            result = rc.receive_once(custody=_custody(), journal_dir=journal_dir,
                                     request=request, message_workers=workers)
            assert entered.wait(1), "delivery worker did not start"
            assert completed_native_ops == ["capture", "stop"]
            assert result["processed"][0]["op"] == "message"
            assert [row["op"] for row in result["processed"][1:]] == ["capture", "stop"]
            assert all(row["state"] == "acked" for row in result["processed"][1:])
            assert not workers.submit(HUB_SESSION, str(uuid.uuid4()), lambda: None)
    finally:
        release.set()
        assert workers.wait_idle(2)


def test_stop_only_receiver_never_claims_or_reconciles_ordinary_commands(journal_dir):
    offers = [_offer("message"), _offer("capture"), _offer("stop")]
    operations = []

    def request(method, path, payload):
        if method == "GET" and path == "/api/session-commands/offers?kind=stop":
            return {"offers": offers}
        if method == "POST" and path.endswith("/claim"):
            return {"command_id": path.split("/")[3], "claim_token": "stop-token",
                    "holder": "hetzner1", "generation": 1,
                    "expires_at_ms": int(time.time() * 1000) + 60000}
        raise AssertionError((method, path))

    def command_for(offer):
        return {"op": offer["kind"], "command_id": offer["command_id"],
                "session_id": offer["session_id"], "generation": 1, "target": {"id": "native"}}

    def execute(command, *args, **kwargs):
        operations.append(command["op"])
        return {"state": "acked", "ack": {"submitted": True}}

    with patch.object(rc.session_messages, "pending_message_reconciliations", side_effect=AssertionError("stop loop touched messages")), \
         patch.object(rc.session_messages, "relay_replies", side_effect=AssertionError("stop loop relayed messages")), \
         patch.object(rc, "offer_to_command", side_effect=command_for), \
         patch.object(rc, "handle_claimed_command", side_effect=execute), \
         patch.object(rc, "_pending_reconcile_names", return_value=[]):
        result = rc.receive_once(custody=_custody(), journal_dir=journal_dir,
                                 request=request, command_kind="stop")

    assert operations == ["stop"]
    assert result["offers_seen"] == 1
    assert [row["op"] for row in result["processed"]] == ["stop"]


def test_non_stop_receiver_uses_filtered_offer_poll_and_skips_stops(journal_dir):
    offers = [_offer("stop"), _offer("capture")]
    operations = []

    def request(method, path, payload):
        if method == "GET" and path == "/api/session-commands/offers?kind=non_stop":
            return {"offers": offers}
        if method == "POST" and path.endswith("/claim"):
            return {"command_id": path.split("/")[3], "claim_token": "capture-token",
                    "holder": "hetzner1", "generation": 1,
                    "expires_at_ms": int(time.time() * 1000) + 60000}
        raise AssertionError((method, path))

    def command_for(offer):
        return {"op": offer["kind"], "command_id": offer["command_id"],
                "session_id": offer["session_id"], "generation": 1, "target": {"id": "native"}}

    def execute(command, *args, **kwargs):
        operations.append(command["op"])
        return {"state": "acked", "ack": {"submitted": True}}

    with patch.object(rc.session_messages, "pending_message_reconciliations", return_value=[]), \
         patch.object(rc.session_messages, "relay_replies", return_value=[]), \
         patch.object(rc, "offer_to_command", side_effect=command_for), \
         patch.object(rc, "handle_claimed_command", side_effect=execute), \
         patch.object(rc, "_pending_reconcile_names", return_value=[]):
        result = rc.receive_once(custody=_custody(), journal_dir=journal_dir,
                                 request=request, command_kind="non_stop")

    assert operations == ["capture"]
    assert result["offers_seen"] == 1
    assert [row["op"] for row in result["processed"]] == ["capture"]


@pytest.mark.parametrize("ack_state,processed", [("acked", True), ("recorded", False)])
def test_unbound_stop_requires_exact_native_stop_result_and_ack(journal_dir, ack_state, processed):
    offer = _offer("stop")
    offer.update({"fleet_launch_id": "launch-17", "session_state": "uncertain"})
    native = SimpleNamespace(session_id="33333333-3333-4333-8333-333333333333",
                             tag="hub-launch-17", workspace="/repo", created_at_ms=123456,
                             engine="codex")
    target_seen = []
    ack_payloads = []

    def request(method, path, payload):
        if method == "GET" and path == "/api/session-commands/offers":
            return {"offers": [offer]}
        if method == "POST" and path.endswith("/claim"):
            return {"command_id": offer["command_id"], "claim_token": "stop-token"}
        if method == "POST" and path.endswith("/ack"):
            ack_payloads.append(payload)
            return {"command_id": offer["command_id"], "state": ack_state}
        raise AssertionError((method, path))

    def stop(_executable, target):
        target_seen.append(target)
        return SimpleNamespace(state="stopped", reason="workload cgroup reaped")

    with patch.object(rc, "list_sessions", return_value=[native]) as listing, \
         patch.object(rc, "stop_session", side_effect=stop), \
         patch.object(rc, "offer_to_command", return_value={"op": "stop", "target": None}), \
         patch.object(rc.session_messages, "pending_message_reconciliations", return_value=[]), \
         patch.object(rc.session_messages, "relay_replies", return_value=[]), \
         patch.object(rc, "_pending_reconcile_names", return_value=[]):
        result = rc.receive_once(custody=_custody(), journal_dir=journal_dir,
                                 request=request, limit=20)

    listing.assert_called_once_with("aplexer", include_exited=True)
    assert len(target_seen) == 1
    assert target_seen[0].session_id == native.session_id
    assert len(ack_payloads) == 1
    assert ack_payloads[0]["confirmed_exit"] is True
    assert ack_payloads[0]["native"] == {
        "native_id": native.session_id, "tag": native.tag, "workspace": native.workspace,
    }
    assert (len(result["processed"]) == 1) is processed
    if processed:
        assert result["processed"][0]["state"] == "stopped"
    else:
        assert result["skipped"] == [{"command_id": offer["command_id"], "reason": "ack_unverified_reconcile"}]


def test_unbound_stop_with_no_native_identity_does_not_claim_or_ack(journal_dir):
    offer = _offer("stop")
    offer.update({"fleet_launch_id": "launch-18", "session_state": "uncertain"})
    operations = []

    def request(method, path, payload):
        operations.append((method, path))
        if method == "GET" and path == "/api/session-commands/offers":
            return {"offers": [offer]}
        raise AssertionError((method, path))

    with patch.object(rc, "list_sessions", return_value=[]), \
         patch.object(rc, "offer_to_command", return_value={"op": "stop", "target": None}), \
         patch.object(rc.session_messages, "pending_message_reconciliations", return_value=[]), \
         patch.object(rc.session_messages, "relay_replies", return_value=[]), \
         patch.object(rc, "_pending_reconcile_names", return_value=[]):
        result = rc.receive_once(custody=_custody(), journal_dir=journal_dir,
                                 request=request, limit=20)

    assert operations == [("GET", "/api/session-commands/offers")]
    assert result["processed"] == []
    assert result["skipped"] == [{
        "command_id": offer["command_id"],
        "reason": "unbound_stop_native_identity_missing_or_ambiguous",
    }]


def test_message_worker_pool_is_capped_at_four_and_one_per_session():
    workers = rc.MessageDeliveryWorkers()
    release = threading.Event()
    started = [threading.Event() for _ in range(4)]

    def blocked(index):
        def run():
            started[index].set()
            assert release.wait(3)
        return run

    try:
        for i in range(4):
            assert workers.submit("session-%d" % i, "command-%d" % i, blocked(i))
        assert all(event.wait(1) for event in started)
        assert not workers.submit("session-0", "another-command", lambda: None)
        assert not workers.submit("session-4", "command-4", lambda: None)
    finally:
        release.set()
        assert workers.wait_idle(2)
