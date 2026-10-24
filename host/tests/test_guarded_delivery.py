"""Tests for guarded delivery of original envelope and model reply verification.

Behavioral cases covered:
1. idle guard submits original once;
2. blocked busy/draft/menu no input;
3. successful correlated model reply proves delivery;
4. rc0 not-ready is not transported;
5. uncertainty/lost response/restart do not duplicate envelopes or PTY;
6. replay old inbox-only journal remains compatible;
7. confirmed terminal target can fail pending delivery honestly;
8. pending send does not prevent stop/capture processing;
9. controller restart retains honest unknown/recorded state if sender cannot qualify.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from agent_hub_host import native_sessions as ns
from agent_hub_host import session_executor as se
from agent_hub_host import session_receiver as rc
from agent_hub_host.native_sessions import (
    NativeTarget,
    SessionControlError,
    SessionUncertain,
)

REPO_TMP = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".local", "tmp"))

FAKE_SCRIPT = r"""#!/usr/bin/env python3
import json, os, sys, time, uuid

SFILE = os.environ["FAKE_SESSIONS"]
MDIR = os.environ["FAKE_MSGS"]
DELIVER_STATUS = os.environ.get("FAKE_DELIVER_STATUS", "submitted")
DELIVER_RC = int(os.environ.get("FAKE_DELIVER_RC", "0"))
DELIVER_LOG = os.environ.get("FAKE_DELIVER_LOG", "")
PTY_LOG = os.environ.get("FAKE_PTY_LOG", "")

def load():
    try:
        with open(SFILE) as f:
            return json.load(f)
    except Exception:
        return []

def save(rows):
    with open(SFILE, "w") as f:
        json.dump(rows, f)

def flag(args, *names):
    return any(a in args for a in names)

def opt(args, *names):
    for i, a in enumerate(args):
        if a in names and i + 1 < len(args):
            return args[i + 1]
    return None

def bare(args):
    return [a for a in args if not a.startswith("-")]

def main():
    args = [a for a in sys.argv[1:] if a != "--json"]
    if not args:
        sys.exit(1)
    cmd = args[0]
    if cmd == "whoami":
        sid = os.environ.get("APLEXER_SESSION_ID", "")
        for r in load():
            if str(r.get("id", "")).lower() == sid.lower() and sid:
                print(json.dumps(r))
                sys.exit(0)
        sys.stderr.write("no bound session\n")
        sys.exit(1)
    elif cmd == "list":
        print(json.dumps(load()))
    elif cmd == "start":
        ws = opt(args, "--workspace")
        tag = opt(args, "--tag")
        eng = opt(args, "--engine", "") or "shell"
        rows = load()
        rec = {"id": str(uuid.uuid4()), "tag": tag, "workspace": ws, "engine": eng,
               "profile": None, "phase": "running", "state": "running",
               "containment_empty": False,
               "created_at_ms": 1700000000000, "last_activity_ms": 1700000000000}
        rows.append(rec)
        save(rows)
        print(json.dumps({"id": rec["id"], "workspace": ws, "tag": tag}))
    elif cmd == "capture":
        ws = opt(args, "--workspace")
        tag = opt(args, "--tag")
        rows = load()
        if not any(r["tag"] == tag and r["workspace"] == ws for r in rows):
            sys.stderr.write("no session\n")
            sys.exit(1)
        sys.stdout.write("line-00 captured tail output\n")
    elif cmd == "status":
        ws = opt(args, "--workspace")
        tag = opt(args, "--tag")
        rows = load()
        hit = [r for r in rows if r["tag"] == tag and r["workspace"] == ws]
        if not hit:
            sys.stderr.write("no session\n")
            sys.exit(1)
        print(json.dumps({"phase": hit[0]["phase"], "state": hit[0]["phase"],
                          "id": hit[0]["id"], "tag": hit[0]["tag"],
                          "workspace": hit[0]["workspace"],
                          "created_at_ms": hit[0].get("created_at_ms")}))
    elif cmd == "kill":
        ws = opt(args, "--workspace")
        tag = opt(args, "--tag")
        rows = load()
        hit = [r for r in rows if r["tag"] == tag and r["workspace"] == ws]
        if not hit:
            sys.stderr.write("no session\n")
            sys.exit(1)
        hit[0]["phase"] = "exited"
        hit[0]["state"] = "exited"
        hit[0]["containment_empty"] = True
        save(rows)
        print(json.dumps({"killed": hit[0]["id"]}))
    elif cmd == "message":
        sub = args[1]
        if sub == "send":
            rest = [a for a in args[2:] if a != "--"]
            ws = opt(rest, "--workspace")
            to = opt(rest, "--to")
            data = json.loads(opt(rest, "--data") or "{}")
            tails = [a for a in rest if not a.startswith("-")]
            body = tails[-1] if tails else ""
            rows = load()
            hit = [r for r in rows if r["tag"] == to and r["workspace"] == ws]
            if not hit:
                sys.stderr.write("no session tagged\n")
                sys.exit(1)
            sid = os.environ.get("APLEXER_SESSION_ID", "")
            sender_rec = next((r for r in rows if str(r.get("id", "")).lower() == sid.lower() and sid
                               and r.get("workspace") == ws and r.get("tag")), None)
            from_obj = ({"session_id": str(sender_rec["id"]), "workspace": ws,
                         "tag": str(sender_rec["tag"]), "engine": "shell",
                         "external": False} if sender_rec else {"external": True})
            mid = str(uuid.uuid4())
            env = {"schema_version": 1, "id": mid, "workspace": ws,
                   "created_at": int(time.time()),
                   "from": from_obj,
                   "to": {"tag": to, "session_id": hit[0]["id"]},
                   "kind": "note", "data": data, "body": body, "reply_to": None,
                   "delivery": "inbox"}
            os.makedirs(MDIR, exist_ok=True)
            with open(os.path.join(MDIR, mid + ".json"), "w") as f:
                json.dump(env, f)
            print(json.dumps(env))
        elif sub == "deliver":
            ids = bare([a for a in args[2:] if a != "--workspace"])
            mid = ids[-1] if ids else None
            ws = opt(args, "--workspace")
            if DELIVER_LOG:
                with open(DELIVER_LOG, "a") as f:
                    f.write(f"deliver:{mid}\n")
            target_file = os.path.join(MDIR, (mid or "") + ".json")
            if not os.path.isfile(target_file):
                sys.stderr.write("no such message\n")
                sys.exit(1)

            status = DELIVER_STATUS
            rc = DELIVER_RC
            if status == "submitted":
                with open(target_file, "r") as f:
                    m = json.load(f)
                m["delivery"] = "pane"
                with open(target_file, "w") as f:
                    json.dump(m, f)
                if PTY_LOG:
                    with open(PTY_LOG, "a") as f:
                        f.write(f"pty_input:{mid}:{m.get('body')}\n")
                print(json.dumps({"id": mid, "status": "submitted", "detail": "delivered into target pane"}))
                sys.exit(0)
            elif status == "already-submitted":
                print(json.dumps({"id": mid, "status": "already-submitted", "detail": "envelope already submitted"}))
                sys.exit(0)
            elif status == "recipient-acked":
                print(json.dumps({"id": mid, "status": "recipient-acked", "detail": "message already cursor acked"}))
                sys.exit(0)
            elif status == "not-ready":
                print(json.dumps({"id": mid, "status": "not-ready", "detail": "recipient reported working; prompt not empty"}))
                if rc != 0:
                    sys.stderr.write("Error: recipient reported working\n")
                sys.exit(rc)
            elif status == "busy":
                sys.stderr.write("composer busy, input box not empty\n")
                sys.exit(1)
            elif status == "menu":
                sys.stderr.write("recipient in interactive menu, input blocked\n")
                sys.exit(1)
            elif status == "draft":
                sys.stderr.write("recipient draft text in prompt, delivery refused\n")
                sys.exit(1)
            elif status == "delivery-uncertain":
                print(json.dumps({"id": mid, "status": "delivery-uncertain", "detail": "attempt reservation exists"}))
                sys.stderr.write("Error: delivery uncertain\n")
                sys.exit(1)
            elif status == "malformed-no-status":
                print(json.dumps({"id": mid, "foo": "bar"}))
                sys.exit(rc)
            elif status == "non-json":
                print("plain text not json")
                sys.exit(rc)
            elif status:
                print(json.dumps({"id": mid, "status": status, "detail": f"unrecognized status {status}"}))
                sys.exit(rc)
            else:
                sys.stderr.write(f"unknown status {status}\n")
                sys.exit(1)
        elif sub == "reply":
            rest = [a for a in args[2:] if a != "--"]
            orig_id = opt(args, "--id") or (bare(rest)[0] if bare(rest) else None)
            data = json.loads(opt(rest, "--data") or "{}")
            tails = bare(rest)
            body = tails[-1] if len(tails) > 1 else ""
            orig = None
            for fn in os.listdir(MDIR):
                with open(os.path.join(MDIR, fn)) as f:
                    m = json.load(f)
                if m.get("id") == orig_id:
                    orig = m
                    break
            if orig is None:
                sys.stderr.write("no such message\n")
                sys.exit(1)
            mid = str(uuid.uuid4())
            env = {"schema_version": 1, "id": mid, "workspace": orig["workspace"],
                   "created_at": int(time.time()),
                   "from": {"session_id": orig["to"]["session_id"],
                            "workspace": orig["workspace"],
                            "tag": orig["to"]["tag"], "engine": "shell"},
                   "to": orig["from"], "kind": "reply", "data": data, "body": body,
                   "reply_to": orig_id, "delivery": "inbox"}
            with open(os.path.join(MDIR, mid + ".json"), "w") as f:
                json.dump(env, f)
            print(json.dumps(env))
        elif sub == "log":
            ws = opt(args, "--workspace")
            out = []
            for fn in sorted(os.listdir(MDIR)):
                with open(os.path.join(MDIR, fn)) as f:
                    m = json.load(f)
                if m.get("workspace") == ws:
                    out.append(m)
            print(json.dumps(out))
        elif sub == "ack":
            rest = list(args[2:])
            mids = []
            skip = False
            for a in rest:
                if skip:
                    skip = False
                    continue
                if a in ("--from", "--workspace"):
                    skip = True
                    continue
                if not a.startswith("-"):
                    mids.append(a)
            for mid in mids:
                fn = os.path.join(MDIR, mid + ".json")
                if os.path.isfile(fn):
                    os.remove(fn)
            print(json.dumps({"acked": mids}))
        else:
            sys.exit(1)
    else:
        sys.exit(1)

if __name__ == "__main__":
    main()
"""


class MockHub:
    def __init__(self):
        self.offers: list[dict] = []
        self.claims: dict[str, dict] = {}
        self.acks: dict[str, dict] = {}
        self.statuses: dict[str, dict] = {}
        self.ack_calls = 0

    def request(self, method, path, payload=None):
        if method == "GET" and path == "/api/session-commands/offers":
            return {"offers": list(self.offers)}
        if method == "POST" and (path.endswith("/status") or path.endswith("/progress")):
            cid = path.split("/")[-2]
            self.statuses[cid] = dict(payload)
            return {"ok": True, "command_id": cid}
        if method == "POST" and path.endswith("/claim"):
            cid = path.split("/")[-2]
            match = next((o for o in self.offers if o["command_id"] == cid), None)
            gen = match["generation"] if match else 1
            grant = {
                "command_id": cid,
                "claim_token": "tok-" + uuid.uuid4().hex,
                "holder": "hetzner1",
                "generation": gen,
                "expires_at_ms": int(time.time() * 1000) + 60000,
                "attempt": 1,
            }
            return grant
        if method == "POST" and path.endswith("/ack"):
            cid = path.split("/")[-2]
            self.ack_calls += 1
            self.acks[cid] = dict(payload)
            return {"command_id": cid, "state": "acked"}
        raise AssertionError((method, path))


@pytest.fixture()
def env(monkeypatch):
    base = tempfile.mkdtemp(dir=REPO_TMP, prefix="sess-guard-")
    monkeypatch.setenv("TMPDIR", REPO_TMP)
    work = os.path.join(base, "work")
    os.makedirs(work)
    msgs = os.path.join(base, "msgs")
    os.makedirs(msgs)
    journal = os.path.join(base, "journal")
    os.makedirs(journal, mode=0o700)
    sessions_file = os.path.join(base, "sessions.json")
    sender_id = str(uuid.uuid4())
    with open(sessions_file, "w") as f:
        json.dump([{
            "id": sender_id, "tag": "host-sender", "workspace": work,
            "engine": "shell", "profile": None, "phase": "running",
            "state": "running", "created_at_ms": 1700000000000,
            "last_activity_ms": 1700000000000,
        }], f)
    monkeypatch.setenv("APLEXER_SESSION_ID", sender_id)
    deliver_log = os.path.join(base, "deliver.log")
    pty_log = os.path.join(base, "pty.log")
    fake = os.path.join(base, "fake-a")
    with open(fake, "w") as f:
        f.write(FAKE_SCRIPT)
    os.chmod(fake, 0o755)

    monkeypatch.setenv("FAKE_SESSIONS", sessions_file)
    monkeypatch.setenv("FAKE_MSGS", msgs)
    monkeypatch.setenv("FAKE_DELIVER_LOG", deliver_log)
    monkeypatch.setenv("FAKE_PTY_LOG", pty_log)
    monkeypatch.setenv("FAKE_DELIVER_STATUS", "submitted")
    monkeypatch.setenv("FAKE_DELIVER_RC", "0")

    yield {
        "base": base, "work": work, "fake": fake, "journal": journal,
        "msgs": msgs, "sessions_file": sessions_file, "sender_id": sender_id,
        "deliver_log": deliver_log, "pty_log": pty_log, "monkeypatch": monkeypatch,
    }
    shutil.rmtree(base, ignore_errors=True)


def seed_target(env, tag="real-target", phase="running"):
    rows = json.load(open(env["sessions_file"]))
    rec = {
        "id": str(uuid.uuid4()), "tag": tag, "workspace": env["work"],
        "engine": "antigravity", "profile": None, "phase": phase, "state": phase,
        "containment_empty": phase == "exited",
        "created_at_ms": 1700000000000, "last_activity_ms": 1700000000000,
    }
    rows.append(rec)
    json.dump(rows, open(env["sessions_file"], "w"))
    return NativeTarget(
        session_id=rec["id"], tag=tag, workspace=env["work"],
        created_at_ms=1700000000000, engine="antigravity",
    )


def custody(machine="hetzner1"):
    return se.CallerCustody(actor="machine:" + machine, machine_id=machine,
                             credential_family="host-" + machine)


def make_claim(command_id, generation=1):
    return {
        "command_id": command_id,
        "claim_token": "tok-" + uuid.uuid4().hex,
        "holder": "hetzner1",
        "generation": generation,
        "expires_at_ms": int(time.time() * 1000) + 60000,
    }


# Case 1: idle guard submits original once
def test_idle_guard_submits_original_once(env):
    target = seed_target(env)
    cid = str(uuid.uuid4())
    claim = make_claim(cid)
    cmd = {
        "op": "send", "command_id": cid, "generation": 1,
        "target": {"session_id": target.session_id, "tag": target.tag,
                   "workspace": target.workspace, "created_at_ms": target.created_at_ms,
                   "engine": target.engine},
        "body": "wake up model", "await_ack": True,
    }
    outcome = se.handle_claimed_command(
        cmd, claim, custody=custody(), journal_dir=env["journal"],
        executable=env["fake"], request=None,
    )
    # Target was idle, so native deliver submitted the original envelope
    orig_id = outcome["original_native_message_id"]
    assert orig_id
    assert outcome["state"] == "unknown"  # awaiting recipient reply
    assert outcome["delivery_state"] == "transported"
    assert outcome["ack"]["submitted"] is False

    # Check deliver was called exactly once on the original envelope ID
    with open(env["deliver_log"]) as f:
        deliver_calls = f.read().strip().splitlines()
    assert deliver_calls == [f"deliver:{orig_id}"]

    # Check PTY received input exactly once
    with open(env["pty_log"]) as f:
        pty_inputs = f.read().strip().splitlines()
    assert len(pty_inputs) == 1
    assert orig_id in pty_inputs[0]
    assert "wake up model" in pty_inputs[0]


# Case 2: blocked busy/draft/menu no input
@pytest.mark.parametrize("status_name", ["not-ready", "busy", "menu", "draft"])
def test_blocked_busy_draft_menu_no_input(env, status_name):
    env["monkeypatch"].setenv("FAKE_DELIVER_STATUS", status_name)
    env["monkeypatch"].setenv("FAKE_DELIVER_RC", "1")
    target = seed_target(env, tag="busy-target-" + status_name)
    cid = str(uuid.uuid4())
    claim = make_claim(cid)
    cmd = {
        "op": "send", "command_id": cid, "generation": 1,
        "target": {"session_id": target.session_id, "tag": target.tag,
                   "workspace": target.workspace, "created_at_ms": target.created_at_ms,
                   "engine": target.engine},
        "body": "do not input while busy", "await_ack": True,
    }
    outcome = se.handle_claimed_command(
        cmd, claim, custody=custody(), journal_dir=env["journal"],
        executable=env["fake"], request=None,
    )
    orig_id = outcome["original_native_message_id"]
    assert orig_id
    # Blocked readiness remains held in inbox (never input to PTY)
    assert outcome["state"] == "unknown"
    assert outcome["delivery_state"] == "recorded"
    assert outcome["ack"]["submitted"] is False

    # PTY must have received NO input!
    if os.path.exists(env["pty_log"]):
        with open(env["pty_log"]) as f:
            assert f.read().strip() == ""


# Case 3: successful correlated model reply proves delivery
def test_successful_correlated_model_reply_proves_delivery(env):
    target = seed_target(env)
    cid = str(uuid.uuid4())
    hub = MockHub()
    hub.offers = [{
        "command_id": cid, "kind": "send", "generation": 1,
        "binding": {"native_id": target.session_id, "tag": target.tag,
                    "workspace": target.workspace, "native_created_at_ms": target.created_at_ms},
        "engine": target.engine, "payload": {"body": "solve problem"},
    }]
    # Cycle 1: Poll -> claim -> deliver to idle prompt -> journal pending reply
    res1 = rc.receive_once(custody=custody(), journal_dir=env["journal"],
                           executable=env["fake"], request=hub.request)
    assert res1["offers_seen"] == 1
    assert res1["processed"][0]["state"] == "unknown"
    assert res1["processed"][0]["ack_submitted"] is False
    assert hub.ack_calls == 0

    journal = json.load(open(os.path.join(env["journal"], f"cmd-{cid}.json")))
    orig_id = journal["original_native_message_id"]
    digest = ns.body_digest("solve problem")

    # Real model processes input and sends correlated reply in native message log
    reply_proc = subprocess.run(
        [env["fake"], "message", "reply", orig_id,
         "--data", json.dumps({"correlation_id": cid, "body_digest": digest}),
         "--", "solution completed"],
        capture_output=True, text=True, timeout=20,
    )
    assert reply_proc.returncode == 0
    reply = json.loads(reply_proc.stdout)
    assert reply["reply_to"] == orig_id

    # Cycle 2: Reconciliation pass detects correlated reply, proves delivery, submits ack
    hub.offers = []
    res2 = rc.receive_once(custody=custody(), journal_dir=env["journal"],
                           executable=env["fake"], request=hub.request)
    assert res2["reconciled"] and res2["reconciled"][0]["state"] == "acknowledged"
    assert res2["reconciled"][0]["ack_submitted"] is True
    assert hub.ack_calls == 1

    ack = hub.acks[cid]
    assert ack["outcome"] == "delivered"
    assert ack["proof"]["native_message_id"] == orig_id
    assert ack["proof"]["reply_to_native_id"] == reply["id"]
    assert ack["proof"]["sender_native_id"] == target.session_id


# Case 4: rc0 not-ready is not transported
def test_rc0_not_ready_is_not_transported(env):
    # Some wrappers or CLI versions might exit rc=0 while status is not-ready
    env["monkeypatch"].setenv("FAKE_DELIVER_STATUS", "not-ready")
    env["monkeypatch"].setenv("FAKE_DELIVER_RC", "0")
    target = seed_target(env)
    sent, _ = ns.send_inbox(env["fake"], target, "body text", {"correlation_id": "c4"})
    result = ns.deliver_recorded(env["fake"], target, sent.native_id)
    # Must be refused, NEVER transported!
    assert result.state == "refused"
    assert "not-ready" in result.reason or "reported working" in result.reason


# Case 5: uncertainty/lost response/restart do not duplicate envelopes or PTY
def test_uncertainty_lost_response_restart_no_duplicate_envelopes_or_pty(env):
    target = seed_target(env)
    cid = str(uuid.uuid4())
    claim = make_claim(cid)

    # First attempt: deliver returns delivery-uncertain
    env["monkeypatch"].setenv("FAKE_DELIVER_STATUS", "delivery-uncertain")
    env["monkeypatch"].setenv("FAKE_DELIVER_RC", "1")
    cmd = {
        "op": "send", "command_id": cid, "generation": 1,
        "target": {"session_id": target.session_id, "tag": target.tag,
                   "workspace": target.workspace, "created_at_ms": target.created_at_ms,
                   "engine": target.engine},
        "body": "important command", "await_ack": True,
    }
    outcome = se.handle_claimed_command(
        cmd, claim, custody=custody(), journal_dir=env["journal"],
        executable=env["fake"], request=None,
    )
    # Recorded as uncertain in journal
    assert outcome["state"] == "uncertain"
    msgs_after_1 = list(os.listdir(env["msgs"]))
    assert len(msgs_after_1) == 1  # exactly one message envelope in inbox
    orig_id = msgs_after_1[0].replace(".json", "")

    # Now deliver status becomes already-submitted (idempotent native guard)
    env["monkeypatch"].setenv("FAKE_DELIVER_STATUS", "already-submitted")
    env["monkeypatch"].setenv("FAKE_DELIVER_RC", "0")

    # Restart / replay under same claim token
    outcome2 = se.handle_claimed_command(
        cmd, claim, custody=custody(), journal_dir=env["journal"],
        executable=env["fake"], request=None,
    )
    # Envelope count must NOT increase
    msgs_after_2 = list(os.listdir(env["msgs"]))
    assert len(msgs_after_2) == 1
    assert outcome2.get("replayed") is True


# Case 6: replay old inbox-only journal remains compatible
def test_replay_old_inbox_only_journal_remains_compatible(env):
    target = seed_target(env)
    cid = str(uuid.uuid4())
    # Create an old journal from before guarded delivery (no delivery_state)
    orig_envelope_id = str(uuid.uuid4())
    old_journal = {
        "op": "send", "command_id": cid, "claim_token": "tok-old-1234567890",
        "holder": "hetzner1", "generation": 1, "attempted": True,
        "state": "unknown", "native_id": orig_envelope_id,
        "original_native_message_id": orig_envelope_id,
        "body_digest": ns.body_digest("old body text"),
        "target": {"session_id": target.session_id, "tag": target.tag,
                   "workspace": target.workspace, "created_at_ms": target.created_at_ms,
                   "engine": target.engine},
        "sender_binding": {
            "session_id": env["sender_id"], "tag": "host-sender",
            "workspace": env["work"], "created_at_ms": 1700000000000,
            "engine": "shell", "profile": None,
        },
    }
    jpath = os.path.join(env["journal"], f"cmd-{cid}.json")
    with open(jpath, "w") as f:
        json.dump(old_journal, f)

    # Replay via reconcile_pending_command without error
    rec = se.reconcile_pending_command(
        journal_dir=env["journal"], command_id=cid,
        executable=env["fake"], request=None,
    )
    # No reply yet, so it stays cleanly unknown/pending
    assert rec is None
    # Verify file was not corrupted or reset
    loaded = json.load(open(jpath))
    assert loaded["state"] == "unknown"
    assert loaded["original_native_message_id"] == orig_envelope_id


# Case 7: confirmed terminal target can fail pending delivery honestly
def test_confirmed_terminal_target_fails_pending_delivery_honestly(env):
    target = seed_target(env)
    cid = str(uuid.uuid4())
    claim = make_claim(cid)
    hub = MockHub()

    # Step 1: Send is submitted and awaits reply
    cmd = {
        "op": "send", "command_id": cid, "generation": 1,
        "target": {"session_id": target.session_id, "tag": target.tag,
                   "workspace": target.workspace, "created_at_ms": target.created_at_ms,
                   "engine": target.engine},
        "body": "message before crash", "await_ack": True,
    }
    outcome = se.handle_claimed_command(
        cmd, claim, custody=custody(), journal_dir=env["journal"],
        executable=env["fake"], request=hub.request,
    )
    assert outcome["state"] == "unknown"

    # Step 2: Target session crashes / exits (terminal phase: exited)
    rows = json.load(open(env["sessions_file"]))
    for r in rows:
        if r["id"] == target.session_id:
            r["phase"] = "exited"
            r["state"] = "exited"
            r["containment_empty"] = True
    json.dump(rows, open(env["sessions_file"], "w"))

    # Step 3: Reconcile pass detects confirmed terminal target
    reconciled = se.reconcile_pending_command(
        journal_dir=env["journal"], command_id=cid,
        executable=env["fake"], request=hub.request,
    )
    assert reconciled is not None
    # Fails pending delivery honestly with state=refused
    assert reconciled["state"] == "refused"
    assert reconciled["ack"]["submitted"] is True
    assert reconciled["ack"]["payload"]["outcome"] == "refused"
    assert "terminal" in reconciled["reason"].lower()

    # Hub received refused ack
    assert hub.acks[cid]["outcome"] == "refused"


# Case 8: pending send does not prevent stop/capture processing
def test_pending_send_does_not_prevent_stop_capture_processing(env):
    target = seed_target(env)
    cid_send = str(uuid.uuid4())
    cid_cap = str(uuid.uuid4())
    cid_stop = str(uuid.uuid4())
    hub = MockHub()
    hub.offers = [
        {
            "command_id": cid_send, "kind": "send", "generation": 1,
            "binding": {"native_id": target.session_id, "tag": target.tag,
                        "workspace": target.workspace, "native_created_at_ms": target.created_at_ms},
            "engine": target.engine, "payload": {"body": "waiting send"},
        },
        {
            "command_id": cid_cap, "kind": "capture", "generation": 1,
            "binding": {"native_id": target.session_id, "tag": target.tag,
                        "workspace": target.workspace, "native_created_at_ms": target.created_at_ms},
            "engine": target.engine, "payload": {"max_bytes": 1024},
        },
        {
            "command_id": cid_stop, "kind": "stop", "generation": 1,
            "binding": {"native_id": target.session_id, "tag": target.tag,
                        "workspace": target.workspace, "native_created_at_ms": target.created_at_ms},
            "engine": target.engine, "payload": {},
        },
    ]
    # Single receive_once cycle processes all three offers:
    res = rc.receive_once(custody=custody(), journal_dir=env["journal"],
                          executable=env["fake"], request=hub.request)
    assert res["offers_seen"] == 3
    # send was processed (state=unknown, pending reply)
    assert res["processed"][0]["command_id"] == cid_send
    assert res["processed"][0]["state"] == "unknown"

    # capture was NOT blocked and completed (state=captured)
    assert res["processed"][1]["command_id"] == cid_cap
    assert res["processed"][1]["state"] == "captured"
    assert res["processed"][1]["ack_submitted"] is True

    # stop was NOT blocked and completed (state=stopped)
    assert res["processed"][2]["command_id"] == cid_stop
    assert res["processed"][2]["state"] == "stopped"
    assert res["processed"][2]["ack_submitted"] is True


# Case 9: controller restart retains honest unknown/recorded state if sender unbound
def test_controller_restart_retains_honest_unknown_if_sender_unbound(env):
    target = seed_target(env)
    cid = str(uuid.uuid4())
    claim = make_claim(cid)
    cmd = {
        "op": "send", "command_id": cid, "generation": 1,
        "target": {"session_id": target.session_id, "tag": target.tag,
                   "workspace": target.workspace, "created_at_ms": target.created_at_ms,
                   "engine": target.engine},
        "body": "test restart unbound", "await_ack": True,
    }
    se.handle_claimed_command(
        cmd, claim, custody=custody(), journal_dir=env["journal"],
        executable=env["fake"], request=None,
    )
    jpath = os.path.join(env["journal"], f"cmd-{cid}.json")
    jdata = json.load(open(jpath))
    assert jdata["state"] == "unknown"

    # Simulate controller restart where host sender binding is unavailable / unbound
    env["monkeypatch"].delenv("APLEXER_SESSION_ID", raising=False)
    jdata.pop("sender_binding", None)
    with open(jpath, "w") as f:
        json.dump(jdata, f)

    # Reconcile should return None (retaining honest unknown state without crashing or fabricating)
    reconciled = se.reconcile_pending_command(
        journal_dir=env["journal"], command_id=cid,
        executable=env["fake"], request=None,
    )
    assert reconciled is None
    loaded = json.load(open(jpath))
    assert loaded["state"] == "unknown"


def test_deliver_recorded_rejects_unknown_status_and_malformed_json_as_uncertain(env):
    target = seed_target(env)
    mid = str(uuid.uuid4())
    # Create envelope file in msgs
    env_file = os.path.join(env["msgs"], mid + ".json")
    with open(env_file, "w") as f:
        json.dump({"id": mid, "workspace": target.workspace, "delivery": "inbox"}, f)

    # 1. Unknown status string in JSON
    env["monkeypatch"].setenv("FAKE_DELIVER_STATUS", "strange-status-xyz")
    env["monkeypatch"].setenv("FAKE_DELIVER_RC", "0")
    with pytest.raises(se.SessionUncertain) as exc1:
        se.deliver_recorded(env["fake"], target, mid)
    assert "unrecognized status" in str(exc1.value).lower() or "unknown status" in str(exc1.value).lower()

    # 2. Malformed JSON without status field
    env["monkeypatch"].setenv("FAKE_DELIVER_STATUS", "malformed-no-status")
    env["monkeypatch"].setenv("FAKE_DELIVER_RC", "0")
    with pytest.raises(se.SessionUncertain) as exc2:
        se.deliver_recorded(env["fake"], target, mid)
    assert "without valid status" in str(exc2.value).lower()

    # 3. Non-JSON output with rc 0
    env["monkeypatch"].setenv("FAKE_DELIVER_STATUS", "non-json")
    env["monkeypatch"].setenv("FAKE_DELIVER_RC", "0")
    with pytest.raises(se.SessionUncertain) as exc3:
        se.deliver_recorded(env["fake"], target, mid)
    assert "without valid json" in str(exc3.value).lower()


def test_reconcile_uncertain_send_without_positive_log_does_not_retry_deliver(env):
    target = seed_target(env)
    cid = str(uuid.uuid4())
    claim = make_claim(cid)

    # First attempt: deliver returns delivery-uncertain
    env["monkeypatch"].setenv("FAKE_DELIVER_STATUS", "delivery-uncertain")
    env["monkeypatch"].setenv("FAKE_DELIVER_RC", "1")
    cmd = {
        "op": "send", "command_id": cid, "generation": 1,
        "target": {"session_id": target.session_id, "tag": target.tag,
                   "workspace": target.workspace, "created_at_ms": target.created_at_ms,
                   "engine": target.engine},
        "body": "do not retry blindly", "await_ack": True,
    }
    outcome = se.handle_claimed_command(
        cmd, claim, custody=custody(), journal_dir=env["journal"],
        executable=env["fake"], request=None,
    )
    assert outcome["state"] == "uncertain"
    jpath = os.path.join(env["journal"], f"cmd-{cid}.json")
    jdata = json.load(open(jpath))
    assert jdata["delivery_state"] == "uncertain"

    # Count deliver invocations via DELIVER_LOG
    if os.path.isfile(env["deliver_log"]):
        with open(env["deliver_log"]) as f:
            delivers_before = len(f.readlines())
    else:
        delivers_before = 0

    # Log shows message is in inbox (no positive proof of pane delivery)
    reconciled = se.reconcile_pending_command(
        journal_dir=env["journal"], command_id=cid,
        executable=env["fake"], request=None,
    )
    # Must remain None (uncertain), NEVER retrying deliver_recorded blindly
    assert reconciled is None
    if os.path.isfile(env["deliver_log"]):
        with open(env["deliver_log"]) as f:
            delivers_after = len(f.readlines())
    else:
        delivers_after = 0
    assert delivers_after == delivers_before, "deliver_recorded must NOT be called when delivery was uncertain"


def test_target_terminal_matcher_requires_workspace_engine_and_created_at_ms(env):
    target = seed_target(env)
    cid = str(uuid.uuid4())
    claim = make_claim(cid)
    cmd = {
        "op": "send", "command_id": cid, "generation": 1,
        "target": {"session_id": target.session_id, "tag": target.tag,
                   "workspace": target.workspace, "created_at_ms": target.created_at_ms,
                   "engine": target.engine},
        "body": "match test", "await_ack": True,
    }
    se.handle_claimed_command(
        cmd, claim, custody=custody(), journal_dir=env["journal"],
        executable=env["fake"], request=None,
    )

    # Mutate catalog session to have different engine and mark it exited
    with open(env["sessions_file"], "r") as f:
        rows = json.load(f)
    for r in rows:
        if r["id"] == target.session_id:
            r["phase"] = "exited"
            r["engine"] = "different-engine"
    with open(env["sessions_file"], "w") as f:
        json.dump(rows, f)

    # Reconcile should NOT fail pending delivery because engine does not match
    reconciled = se.reconcile_pending_command(
        journal_dir=env["journal"], command_id=cid,
        executable=env["fake"], request=None,
    )
    assert reconciled["state"] == "unknown"  # Not failed as refused
    jdata = json.load(open(os.path.join(env["journal"], f"cmd-{cid}.json")))
    assert jdata["state"] == "unknown"  # Not failed

    # Now restore engine and match created_at_ms, workspace, session_id, tag
    for r in rows:
        if r["id"] == target.session_id:
            r["engine"] = target.engine
            r["created_at_ms"] = target.created_at_ms
            r["workspace"] = target.workspace
    with open(env["sessions_file"], "w") as f:
        json.dump(rows, f)

    # Now it matches all identity attributes and phase is exited -> fails honestly
    reconciled2 = se.reconcile_pending_command(
        journal_dir=env["journal"], command_id=cid,
        executable=env["fake"], request=None,
    )
    assert reconciled2 is not None
    assert reconciled2["state"] == "refused"
    assert "terminal" in reconciled2["reason"]

@pytest.mark.parametrize("source", ["offer", "catalog"])
def test_codex_exec_send_fails_fast_without_inbox_or_input(env, source):
    target = seed_target(env)
    rows = json.load(open(env["sessions_file"]))
    rows[-1]["engine"] = "codex"
    if source == "catalog":
        rows[-1]["command"] = ["managed-wrapper", "codex-test", "exec", "--json", "original goal"]
    json.dump(rows, open(env["sessions_file"], "w"))
    cid = str(uuid.uuid4())
    cmd = {"op": "send", "command_id": cid, "generation": 1,
           "target": {"session_id": target.session_id, "tag": target.tag,
                      "workspace": target.workspace, "created_at_ms": target.created_at_ms,
                      "engine": "codex"}, "body": "must never enter exec", "await_ack": True}
    if source == "offer":
        cmd["mode"] = "headless"
    hub = MockHub()
    grant = make_claim(cid)
    started = time.monotonic()
    outcome = se.handle_claimed_command(cmd, grant, custody=custody(),
        journal_dir=env["journal"], executable=env["fake"], request=hub.request)
    assert time.monotonic() - started < 60
    assert outcome["state"] == "refused"
    assert hub.acks[cid]["reason"] == "send_unsupported_headless"
    assert hub.acks[cid]["outcome"] == "refused"
    assert not os.listdir(env["msgs"])
    assert not os.path.exists(env["deliver_log"])
    assert not os.path.exists(env["pty_log"])
    # Renewed custody replays the journaled refusal; never sends anything.
    grant["expires_at_ms"] += 60000
    again = se.handle_claimed_command(cmd, grant, custody=custody(),
        journal_dir=env["journal"], executable=env["fake"], request=hub.request)
    assert again["replayed"] is True
    assert not os.listdir(env["msgs"])


def test_reconciliation_cannot_deliver_an_old_envelope_to_codex_exec(env):
    target = seed_target(env)
    rows = json.load(open(env["sessions_file"]))
    rows[-1].update(engine="codex", command=["codex", "exec", "--json", "goal"])
    json.dump(rows, open(env["sessions_file"], "w"))
    target = NativeTarget(session_id=target.session_id, tag=target.tag,
                          workspace=target.workspace, created_at_ms=target.created_at_ms,
                          engine="codex")
    with pytest.raises(SessionControlError, match="^send_unsupported_headless$"):
        ns.deliver_recorded(env["fake"], target, "old-persisted-envelope")
    assert not os.path.exists(env["deliver_log"])
    assert not os.path.exists(env["pty_log"])


def test_expired_claim_retracts_undelivered_inbox_and_fails_recipient_not_ready(env):
    env["monkeypatch"].setenv("FAKE_DELIVER_STATUS", "not-ready")
    target = seed_target(env)
    cid = str(uuid.uuid4())
    cmd = {"op": "send", "command_id": cid, "generation": 1,
           "target": {"session_id": target.session_id, "tag": target.tag,
                      "workspace": target.workspace, "created_at_ms": target.created_at_ms,
                      "engine": target.engine}, "body": "pending message", "await_ack": True}
    hub = MockHub()
    now_ms = 1700000000000
    grant = make_claim(cid)
    grant["expires_at_ms"] = now_ms + 1000

    # Initial send records into inbox; deliver guard refuses because recipient not ready
    outcome = se.handle_claimed_command(cmd, grant, custody=custody(),
        journal_dir=env["journal"], executable=env["fake"], request=hub.request,
        now_ms=now_ms)
    assert outcome["state"] == "unknown"
    # Ensure message file was created in msgs directory
    assert len(os.listdir(env["msgs"])) == 1

    # Message is still in inbox. Now reconcile after lease expiry.
    reconciled = se.reconcile_pending_command(
        journal_dir=env["journal"], command_id=cid,
        executable=env["fake"], request=hub.request, now_ms=now_ms + 2000)
    assert reconciled is not None
    assert reconciled["state"] == "failed"
    assert reconciled["reason"] == "recipient_not_ready"
    assert hub.acks[cid]["outcome"] == "failed"
    assert hub.acks[cid]["reason"] == "recipient_not_ready"
    # Undelivered message in inbox was retracted
    assert len(os.listdir(env["msgs"])) == 0
