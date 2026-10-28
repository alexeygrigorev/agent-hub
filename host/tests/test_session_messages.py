"""Host delivery of Hub session messages (docs/SESSION-MESSAGING.md §5-6).

Runs against the fake ``a`` from test_session_executor (native mailbox,
deliver guard and replies), with a fake Hub request callable.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from agent_hub_host import session_messages as sm  # noqa: E402
from test_session_executor import env, seed_session  # noqa: E402,F401

HUB_SESSION = "22222222-2222-4222-8222-222222222222"
MESSAGE_ID = "01JD3K7QXYZABCDEFGHJKMNPQR"


class FakeHub:
    def __init__(self):
        self.calls = []
        self.tokens = 0

    def __call__(self, method, path, payload):
        self.calls.append((method, path, payload))
        if path.endswith("/claim"):
            self.tokens += 1
            return {"command_id": path.split("/")[3], "claim_token": "tok-%032d" % self.tokens, "holder": "tabo",
                    "generation": 1, "expires_at_ms": int(time.time() * 1000) + 60000}
        if path == "/api/bus/send":
            return {"id": "reply-" + str(len(self.calls))}
        return {"state": "ok"}



def offer(env, rec, command_id=None, delivery="tui"):
    command_id = command_id or str(uuid.uuid4())
    return {"command_id": command_id, "session_id": HUB_SESSION, "kind": "message", "generation": 1,
            "correlation": "cmd:%s:gen:1" % command_id, "engine": None,
            "binding": {"native_id": rec["id"], "tag": "agent1", "workspace": env["work"], "generation": 1,
                        "native_created_at_ms": 1700000000000},
            "payload": {"message_id": MESSAGE_ID, "delivery": delivery, "from_actor": "agent:developer",
                        "body": "⟦hub:GHJKMNPQ⟧ from agent:developer: ping"}}


def handle(env, hub, o):
    return sm.handle_message_offer(o, journal_dir=env["journal"], executable=env["fake"], request=hub,
                                   attempt_id=str(uuid.uuid4()))


def native_log(env):
    out = subprocess.run([env["fake"], "message", "log", "--workspace", env["work"], "--json"],
                         capture_output=True, text=True, timeout=20)
    return json.loads(out.stdout)


def acks(hub):
    return [payload for method, path, payload in hub.calls if path.endswith("/ack")]


def test_tui_delivery_writes_once_and_acks_delivered_with_envelope(env):
    rec = seed_session(env)
    hub = FakeHub()
    o = offer(env, rec)
    assert handle(env, hub, o)["state"] == "delivered"
    [ack] = acks(hub)
    assert ack["outcome"] == "delivered" and ack["claim_token"] == "tok-%032d" % 1
    assert ack["native"] == {"native_id": rec["id"], "tag": "agent1", "workspace": env["work"]}
    envelopes = [e for e in native_log(env) if e.get("kind") == "hub-message"]
    assert len(envelopes) == 1 and ack["proof"] == {"native_message_id": envelopes[0]["id"]}
    # A lost ack re-offered under a new claim replays without a second write.
    assert handle(env, hub, o) == {"command_id": o["command_id"], "state": "delivered", "replayed": True}
    assert len([e for e in native_log(env) if e.get("kind") == "hub-message"]) == 1


def test_not_ready_backs_off_then_delivers_the_same_envelope(env):
    rec = seed_session(env)
    hub = FakeHub()
    o = offer(env, rec)
    env["monkeypatch"].setenv("FAKE_DELIVER_BEHAVIOR", "refused-busy")
    assert handle(env, hub, o)["state"] == "not_ready"
    assert acks(hub)[-1]["outcome"] == "refused" and acks(hub)[-1]["reason"].startswith("not_ready")
    env["monkeypatch"].delenv("FAKE_DELIVER_BEHAVIOR")
    assert handle(env, hub, o)["state"] == "delivered"
    assert acks(hub)[-1]["outcome"] == "delivered"
    assert len([e for e in native_log(env) if e.get("kind") == "hub-message"]) == 1


def test_uncertain_deliver_is_never_retyped(env):
    rec = seed_session(env)
    hub = FakeHub()
    o = offer(env, rec)
    env["monkeypatch"].setenv("FAKE_DELIVER_BEHAVIOR", "crash")
    assert handle(env, hub, o)["state"] == "uncertain"
    env["monkeypatch"].delenv("FAKE_DELIVER_BEHAVIOR")
    again = handle(env, hub, o)
    assert again["state"] == "uncertain" and again["replayed"]
    assert [a["outcome"] for a in acks(hub)] == ["uncertain", "uncertain"]


def test_inbox_delivery_skips_the_tui(env):
    rec = seed_session(env)
    hub = FakeHub()
    env["monkeypatch"].setenv("FAKE_DELIVER_BEHAVIOR", "crash")  # would fail if deliver ran
    assert handle(env, hub, offer(env, rec, delivery="inbox"))["state"] == "delivered"


def test_native_reply_is_relayed_once_as_the_hub_session(env):
    rec = seed_session(env)
    hub = FakeHub()
    handle(env, hub, offer(env, rec))
    envelope = [e for e in native_log(env) if e.get("kind") == "hub-message"][0]["id"]
    assert sm.relay_replies(journal_dir=env["journal"], executable=env["fake"], request=hub) == []
    reply_env = dict(os.environ, APLEXER_SESSION_ID=rec["id"])
    rep = subprocess.run([env["fake"], "message", "reply", envelope, "pong from Tabo", "--json"],
                         capture_output=True, text=True, timeout=20, env=reply_env)
    assert rep.returncode == 0, rep.stderr
    relayed = sm.relay_replies(journal_dir=env["journal"], executable=env["fake"], request=hub)
    assert len(relayed) == 1
    sends = [p for m, path, p in hub.calls if path == "/api/bus/send"]
    assert sends == [{"from_actor": "session:" + HUB_SESSION, "to_actor": "agent:developer", "kind": "reply",
                      "body": "pong from Tabo", "reply_to": MESSAGE_ID,
                      "idempotency_key": "native-" + json.loads(rep.stdout)["id"]}]
    assert sm.relay_replies(journal_dir=env["journal"], executable=env["fake"], request=hub) == []


def test_reply_from_another_session_is_not_relayed(env):
    rec = seed_session(env)
    other = seed_session(env, tag="intruder")
    hub = FakeHub()
    handle(env, hub, offer(env, rec))
    envelope = [e for e in native_log(env) if e.get("kind") == "hub-message"][0]["id"]
    subprocess.run([env["fake"], "message", "reply", envelope, "spoof", "--json"], capture_output=True, text=True,
                   timeout=20, env=dict(os.environ, APLEXER_SESSION_ID=other["id"]))
    assert sm.relay_replies(journal_dir=env["journal"], executable=env["fake"], request=hub) == []
