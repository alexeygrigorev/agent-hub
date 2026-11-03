"""Message delivery workers must keep native capture and stop polling responsive."""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import threading
import time
import uuid
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
