"""M12e R3 host regressions: fair pending scan, terminal-409 silence, engine
masquerade refusal, unproven-stop oracle. Self-contained fake `a` binary
shaped like the verified installed surface (same-ws + cross-ws sends filed
for the recipient with the sender's own `from`; replies filed in the
original author's mailbox; rows carry the real `containment_empty` proof
field observed read-only on the installed CLI)."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import uuid

import pytest

import sys
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

FAKE_R3 = r"""#!/usr/bin/env python3
import json, os, sys, time, uuid
SFILE = os.environ["FAKE_SESSIONS"]
MDIR = os.environ["FAKE_MSGS"]
def load():
    try:
        with open(SFILE) as f: return json.load(f)
    except Exception: return []
def save(rows):
    with open(SFILE, "w") as f: json.dump(rows, f)
def opt(args, *names):
    for i, a in enumerate(args):
        if a in names and i + 1 < len(args): return args[i + 1]
    return None
def main():
    args = [a for a in sys.argv[1:] if a != "--json"]
    if not args: sys.exit(1)
    cmd = args[0]
    if cmd == "whoami":
        sid = os.environ.get("APLEXER_SESSION_ID", "")
        for r in load():
            if str(r.get("id", "")).lower() == sid.lower() and sid:
                print(json.dumps(r)); sys.exit(0)
        sys.stderr.write("no bound session\n"); sys.exit(1)
    if cmd == "list":
        print(json.dumps(load()))
    elif cmd == "start":
        ws = opt(args, "--workspace"); tag = opt(args, "--tag")
        rows = load()
        rec = {"id": str(uuid.uuid4()), "tag": tag, "workspace": ws, "engine": "shell",
               "profile": None, "phase": "running", "state": "running",
               "containment_empty": False,
               "created_at_ms": 1700000000000, "last_activity_ms": 1700000000000}
        rows.append(rec); save(rows)
        print(json.dumps({"id": rec["id"], "workspace": ws, "tag": tag}))
    elif cmd == "capture":
        ws = opt(args, "--workspace"); tag = opt(args, "--tag")
        rows = load()
        if not any(r["tag"] == tag and r["workspace"] == ws for r in rows):
            sys.stderr.write("no session\n"); sys.exit(1)
        sys.stdout.write("r3 capture line\n")
    elif cmd == "kill":
        ws = opt(args, "--workspace"); tag = opt(args, "--tag")
        rows = load()
        hit = [r for r in rows if r["tag"] == tag and r["workspace"] == ws]
        if not hit: sys.stderr.write("no session\n"); sys.exit(1)
        hit[0]["phase"] = "exited"; hit[0]["state"] = "exited"
        if os.environ.get("FAKE_STOP_NO_PROOF") != "1":
            hit[0]["containment_empty"] = True
        save(rows)
        print(json.dumps({"killed": hit[0]["id"]}))
    elif cmd == "message":
        sub = args[1]
        if sub == "send":
            rest = [a for a in args[2:] if a != "--"]
            ws = opt(rest, "--workspace"); to = opt(rest, "--to")
            data = json.loads(opt(rest, "--data") or "{}")
            tails = [a for a in rest if not a.startswith("-")]
            body = tails[-1] if tails else ""
            rows = load()
            hit = [r for r in rows if r["tag"] == to and r["workspace"] == ws]
            if not hit: sys.stderr.write("no session tagged\n"); sys.exit(1)
            sid = os.environ.get("APLEXER_SESSION_ID", "")
            sender_rec = next((r for r in rows if str(r.get("id", "")).lower() == sid.lower() and sid
                               and r.get("tag") and r.get("workspace")), None)
            from_obj = ({"session_id": str(sender_rec["id"]),
                         "workspace": str(sender_rec["workspace"]),
                         "tag": str(sender_rec["tag"]), "engine": "shell",
                         "external": False} if sender_rec else {"external": True})
            mid = str(uuid.uuid4())
            env = {"schema_version": 1, "id": mid, "workspace": ws,
                   "created_at": int(time.time()), "from": from_obj,
                   "to": {"tag": to, "session_id": hit[0]["id"]},
                   "kind": "note", "data": data, "body": body, "reply_to": None,
                   "delivery": "inbox"}
            os.makedirs(MDIR, exist_ok=True)
            with open(os.path.join(MDIR, mid + ".json"), "w") as f: json.dump(env, f)
            print(json.dumps(env))
        elif sub == "reply":
            rest = [a for a in args[2:] if a != "--"]
            orig_id = opt(args, "--id") or next((a for a in rest if not a.startswith("-")), None)
            data = json.loads(opt(rest, "--data") or "{}")
            tails = [a for a in rest if not a.startswith("-")]
            body = tails[-1] if len(tails) > 1 else ""
            orig = None
            if os.path.isdir(MDIR):
                for fn in sorted(os.listdir(MDIR)):
                    with open(os.path.join(MDIR, fn)) as f: m = json.load(f)
                    if m.get("id") == orig_id: orig = m; break
            if orig is None: sys.stderr.write("no such message\n"); sys.exit(1)
            if not (orig.get("from") or {}).get("tag"):
                sys.stderr.write("Error: original message sent anonymously\n"); sys.exit(1)
            ws = (orig.get("from") or {}).get("workspace") or orig["workspace"]
            sid = os.environ.get("APLEXER_SESSION_ID", "")
            replier = next((r for r in load() if str(r.get("id", "")).lower() == sid.lower() and sid
                            and r.get("tag") and r.get("workspace")), None)
            if replier is None:
                sys.stderr.write("Error: reply requires calling aplexer session\n"); sys.exit(1)
            mid = str(uuid.uuid4())
            env = {"schema_version": 1, "id": mid, "workspace": ws,
                   "created_at": int(time.time()),
                   "from": {"session_id": str(replier["id"]),
                            "workspace": str(replier["workspace"]),
                            "tag": str(replier["tag"]), "engine": "shell",
                            "external": False},
                   "to": orig["from"], "kind": "reply", "data": data, "body": body,
                   "reply_to": orig_id, "delivery": "inbox"}
            with open(os.path.join(MDIR, mid + ".json"), "w") as f: json.dump(env, f)
            print(json.dumps(env))
        elif sub == "log":
            ws = opt(args, "--workspace")
            out = []
            if os.path.isdir(MDIR):
                for fn in sorted(os.listdir(MDIR)):
                    with open(os.path.join(MDIR, fn)) as f: m = json.load(f)
                    if m.get("workspace") == ws: out.append(m)
            print(json.dumps(out))
        else: sys.exit(1)
    else: sys.exit(1)
main()
"""


@pytest.fixture()
def env(tmp_path_factory, monkeypatch):
    base = tempfile.mkdtemp(dir=REPO_TMP, prefix="sess-r3-")
    monkeypatch.setenv("TMPDIR", REPO_TMP)
    work = os.path.join(base, "work")
    os.makedirs(work)
    sessions_file = os.path.join(base, "sessions.json")
    sender_id = str(uuid.uuid4())
    with open(sessions_file, "w") as f:
        json.dump([{"id": sender_id, "tag": "host-sender", "workspace": work,
                    "engine": "shell", "profile": None, "phase": "running",
                    "state": "running", "containment_empty": False,
                    "created_at_ms": 1700000000000,
                    "last_activity_ms": 1700000000000}], f)
    monkeypatch.setenv("APLEXER_SESSION_ID", sender_id)
    msgs = os.path.join(base, "msgs")
    os.makedirs(msgs)
    fake = os.path.join(base, "fake-a")
    with open(fake, "w") as f:
        f.write(FAKE_R3)
    os.chmod(fake, 0o755)
    journal = os.path.join(base, "journal")
    os.makedirs(journal, mode=0o700)
    monkeypatch.setenv("FAKE_SESSIONS", sessions_file)
    monkeypatch.setenv("FAKE_MSGS", msgs)
    yield {"base": base, "work": work, "fake": fake, "journal": journal,
           "msgs": msgs, "sessions_file": sessions_file,
           "sender_id": sender_id, "monkeypatch": monkeypatch}
    shutil.rmtree(base, ignore_errors=True)


def custody(machine="hetzner1"):
    return se.CallerCustody(actor="machine:" + machine, machine_id=machine,
                            credential_family="host-" + machine)


def seed_target(env, tag="agent1"):
    proc = subprocess.run([env["fake"], "start", "--workspace", env["work"],
                           "--tag", tag, "--json"],
                          capture_output=True, text=True, timeout=20)
    assert proc.returncode == 0, proc.stderr
    rec = json.loads(proc.stdout)
    return NativeTarget(session_id=rec["id"], tag=tag, workspace=env["work"],
                        created_at_ms=1700000000000)


class StubHub:
    """Minimal claimed-flow double: poll offers, grant claims, accept acks."""

    def __init__(self):
        self.offers = []
        self.ack_calls = 0
        self.acks = {}

    def request(self, method, path, payload=None):
        if method == "GET" and path == "/api/session-commands/offers":
            return {"offers": list(self.offers)}
        if method == "POST" and path.endswith("/claim"):
            cid = path.split("/")[-2]
            return {"command_id": cid,
                    "claim_token": "tok-" + cid.replace("-", ""),
                    "holder": "hetzner1", "generation": 1,
                    "expires_at_ms": 9999999999999, "attempt": 1}
        if method == "POST" and path.endswith("/ack"):
            self.ack_calls += 1
            cid = path.split("/")[-2]
            self.acks[cid] = dict(payload or {})
            return {"command_id": cid, "state": "acked"}
        raise AssertionError((method, path))


def send_offer(command_id, target, body="hello"):
    return {"command_id": command_id, "session_id": "sess-1", "kind": "send",
            "generation": 1, "session_state": "running",
            "fleet_launch_id": "launch-1",
            "binding": {"native_id": target.session_id, "tag": target.tag,
                        "workspace": target.workspace, "generation": 1,
                        "native_created_at_ms": target.created_at_ms},
            "native_tag": target.tag, "workspace": "demo", "cwd": None,
            "payload": {"body": body},
            "correlation": "cmd:%s:gen:1" % command_id}


def reply_as(env, target_tag, envelope_id, correlation_id, body_digest, text):
    reply_env = dict(os.environ)
    rows = json.load(open(env["sessions_file"]))
    reply_env["APLEXER_SESSION_ID"] = next(
        r["id"] for r in rows if r.get("tag") == target_tag)
    rep = subprocess.run(
        [env["fake"], "message", "reply", envelope_id,
         "--data", json.dumps({"correlation_id": correlation_id,
                               "body_digest": body_digest}),
         "--", text],
        capture_output=True, text=True, timeout=20, env=reply_env)
    assert rep.returncode == 0, rep.stderr
    return json.loads(rep.stdout)


def test_r3_fair_scan_visits_pending_behind_21_completed(env):
    """H2 starvation regression: 21 completed journals with smaller names
    must not hide one pending unknown send from the bounded reconcile pass."""
    target = seed_target(env)
    cid = str(uuid.uuid4())
    hub = StubHub()
    hub.offers = [send_offer(cid, target, body="fair hello")]
    first = rc.receive_once(custody=custody(), journal_dir=env["journal"],
                            executable=env["fake"], request=hub.request, limit=20)
    assert first["processed"] and first["processed"][0]["state"] == "unknown"
    assert hub.ack_calls == 0  # no premature ack before the reply
    # Bury the pending journal behind 21 completed files that sort earlier:
    # all-zero names sort before any real uuid name.
    names = [n for n in os.listdir(env["journal"])
             if n.startswith("cmd-") and n.endswith(".json")]
    assert len(names) == 1
    for i in range(21):
        with open(os.path.join(env["journal"], "cmd-%036d.json" % i), "w") as f:
            json.dump({"state": "acknowledged", "ack_submitted": True,
                       "ack_payload": {"outcome": "delivered"}}, f)
    assert len([n for n in os.listdir(env["journal"])
                if n.startswith("cmd-") and n.endswith(".json")]) == 22
    journal = json.load(open(os.path.join(env["journal"], names[0])))
    sent_id = journal["original_native_message_id"]
    digest = hashlib.sha256(b"fair hello").hexdigest()
    reply_as(env, "agent1", sent_id, cid, digest, "fair reply")
    # No offers now; the reconcile pass alone must find and ack the buried
    # pending journal under the SAME claim token (no second native send).
    hub.offers = []
    out = rc.receive_once(custody=custody(), journal_dir=env["journal"],
                          executable=env["fake"], request=hub.request, limit=20)
    assert hub.ack_calls == 1
    assert out["reconciled"], "buried pending journal was starved"
    assert out["reconciled"][0]["state"] == "acknowledged"
    assert out["reconciled"][0]["ack_submitted"] is True
    assert len(os.listdir(env["msgs"])) == 2  # send + reply only


def test_r3_terminal_409_never_reposted_on_later_cycles(env):
    """H3 infinite-retry regression: a terminal 409 verdict closes the
    journal; cycles 2 and 3 must not re-POST the rejected ack."""
    target = seed_target(env)
    cid = str(uuid.uuid4())
    offer = {"command_id": cid, "session_id": "sess-1", "kind": "capture",
             "generation": 1, "session_state": "running",
             "fleet_launch_id": "launch-1",
             "binding": {"native_id": target.session_id, "tag": target.tag,
                        "workspace": target.workspace, "generation": 1,
                        "native_created_at_ms": target.created_at_ms},
             "native_tag": target.tag, "workspace": "demo", "cwd": None,
             "payload": {"max_bytes": 512},
             "correlation": "cmd:%s:gen:1" % cid}
    offer = {"command_id": cid, "session_id": "sess-1", "kind": "capture",
             "generation": 1, "session_state": "running",
             "fleet_launch_id": "launch-1",
             "binding": {"native_id": target.session_id, "tag": target.tag,
                        "workspace": target.workspace, "generation": 1,
                        "native_created_at_ms": target.created_at_ms},
             "native_tag": target.tag, "workspace": "demo", "cwd": None,
             "payload": {"max_bytes": 512},
             "correlation": "cmd:%s:gen:1" % cid}
    posts = {"n": 0, "offers": [offer]}

    def flaky(method, path, payload):
        if method == "GET" and path == "/api/session-commands/offers":
            return {"offers": posts["offers"]}
        if method == "POST" and path.endswith("/claim"):
            return {"command_id": cid,
                    "claim_token": "tok-r3-" + cid.replace("-", ""),
                    "holder": "hetzner1", "generation": 1,
                    "expires_at_ms": 9999999999999, "attempt": 1}
        if method == "POST" and path.endswith("/ack"):
            posts["n"] += 1
            raise ValueError("Session HTTP request failed with status 409 (proof_replayed)")
        raise AssertionError((method, path))

    # Cycle 1: effect completes, first ack is terminally refused. The
    # completed fact stays visible; the refusal is recorded raw.
    rc.receive_once(custody=custody(), journal_dir=env["journal"],
                    executable=env["fake"], request=flaky, limit=20)
    assert posts["n"] == 1
    journal = json.load(open(os.path.join(env["journal"], "cmd-" + cid + ".json")))
    assert journal["state"] == "captured"
    posts["offers"] = []
    # Cycle 2: one identical resubmit under the same claim token; the
    # terminal verdict closes the journal as uncertain (human obligation,
    # never fake completed). Cycle 3: terminal journals are never
    # re-POSTed — the infinite-retry loop stays dead.
    rc.receive_once(custody=custody(), journal_dir=env["journal"],
                    executable=env["fake"], request=flaky, limit=20)
    assert posts["n"] == 2
    journal = json.load(open(os.path.join(env["journal"], "cmd-" + cid + ".json")))
    assert journal["state"] == "uncertain"
    assert journal["ack_error"] == "server_terminal_proof_replayed"
    rc.receive_once(custody=custody(), journal_dir=env["journal"],
                    executable=env["fake"], request=flaky, limit=20)
    assert posts["n"] == 2, "terminal ack was re-POSTed"


def test_r3_engine_masquerade_refused(env):
    """F5 engine binding: a live SHELL row never qualifies as an expected
    model engine; an unqualified expectation holds honestly."""
    target = seed_target(env)
    evil = NativeTarget(session_id=target.session_id, tag=target.tag,
                        workspace=target.workspace,
                        created_at_ms=target.created_at_ms, engine="codex")
    with pytest.raises(SessionControlError, match="engine mismatch"):
        ns.qualify_target(env["fake"], evil)
    # Unknown catalog engine is not a match for a qualified expectation.
    rows = json.load(open(env["sessions_file"]))
    rows[0].pop("engine", None)
    json.dump(rows, open(env["sessions_file"], "w"))
    with pytest.raises(SessionControlError, match="engine mismatch"):
        ns.qualify_target(env["fake"], evil)


def test_r3_terminal_session_without_proof_stays_uncertain(env, monkeypatch):
    """Stop oracle: an already-terminal record with no containment/process
    proof still needs native shutdown verification; a native response with no
    containment proof remains uncertain, never false stopped."""
    monkeypatch.setenv("FAKE_STOP_NO_PROOF", "1")
    target = seed_target(env)
    rows = json.load(open(env["sessions_file"]))
    rec = next(r for r in rows if r.get("tag") == "agent1")
    rec["phase"] = "exited"
    rec.pop("containment_empty", None)
    json.dump(rows, open(env["sessions_file"], "w"))
    with pytest.raises(SessionUncertain, match="unproven"):
        ns.stop_session(env["fake"], target)
