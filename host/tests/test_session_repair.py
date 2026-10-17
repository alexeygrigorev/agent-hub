"""M12e session repair regressions (F1/F2/F4/F6): owned, bounded, faithful fixtures.

Each test reproduces a confirmed review finding on the seed contract and
asserts the repaired behavior. The fake `a` binary models ONLY the installed
native contracts (bound-sender resolution per workspace, anonymous envelopes
for unbound callers, verbatim Rust anonymous-reply rejection, reply_to
threading, whoami/list/log read-only surface): sender fields always come out
of the fake binary's own session records, never from product-side fabrication.
No live sessions, no network hub (a stub models the real server DTOs and
claimed-state/409 semantics from src/session_control.ts).
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

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest

from agent_hub_host import native_sessions as ns_mod
from agent_hub_host import session_receiver as rc
from agent_hub_host import session_executor as se
from agent_hub_host import session_control as sc
from agent_hub_host.native_sessions import NativeTarget, SessionControlError, SessionUncertain

REPO_TMP = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".local", "tmp"))

REPAIR_FAKE_A = r"""#!/usr/bin/env python3
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
def bare(args): return [a for a in args if not a.startswith("-")]
def main():
    args = [a for a in sys.argv[1:] if a != "--json"]
    if not args: sys.exit(1)
    cmd = args[0]
    if cmd == "whoami":
        sid = os.environ.get("APLEXER_SESSION_ID", "")
        for r in load():
            if str(r.get("id", "")).lower() == sid.lower() and sid:
                print(json.dumps(r))
                sys.exit(0)
        sys.stderr.write("no bound session\n")
        sys.exit(1)
    if cmd == "list":
        print(json.dumps(load()))
    elif cmd == "start":
        ws = opt(args, "--workspace"); tag = opt(args, "--tag")
        rows = load()
        rec = {"id": str(uuid.uuid4()), "tag": tag, "workspace": ws, "engine": "shell",
               "profile": None, "phase": "running", "state": "running",
               # Real native DTO fidelity (see executor FAKE_SCRIPT note):
               # containment_empty is the stop-oracle proof field.
               "containment_empty": False,
               "created_at_ms": 1700000000000, "last_activity_ms": 1700000000000}
        rows.append(rec); save(rows)
        print(json.dumps({"id": rec["id"], "workspace": ws, "tag": tag}))
    elif cmd == "status":
        ws = opt(args, "--workspace"); tag = opt(args, "--tag")
        rows = load()
        hit = [r for r in rows if r["tag"] == tag and r["workspace"] == ws]
        if not hit: sys.stderr.write("no session\n"); sys.exit(1)
        print(json.dumps({"phase": hit[0]["phase"], "state": hit[0]["phase"],
                          "id": hit[0]["id"], "tag": hit[0]["tag"],
                          "workspace": hit[0]["workspace"],
                          "created_at_ms": hit[0].get("created_at_ms")}))
    elif cmd == "capture":
        ws = opt(args, "--workspace"); tag = opt(args, "--tag")
        rows = load()
        if not any(r["tag"] == tag and r["workspace"] == ws for r in rows):
            sys.stderr.write("no session\n"); sys.exit(1)
        sys.stdout.write("repair capture line\n")
    elif cmd == "kill":
        ws = opt(args, "--workspace"); tag = opt(args, "--tag")
        rows = load()
        hit = [r for r in rows if r["tag"] == tag and r["workspace"] == ws]
        if not hit: sys.stderr.write("no session\n"); sys.exit(1)
        hit[0]["phase"] = "exited"; hit[0]["state"] = "exited"
        # Reaped-cgroup proof for the stop oracle (see start-note above).
        hit[0]["containment_empty"] = True; save(rows)
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
            # Genuine sender = the calling process's bound session in ANY
            # workspace (installed cross-workspace sends succeed for bound
            # senders; live probe 2026-10-17). The sender mailbox (from's
            # workspace) is independent of the target mailbox (ws).
            sid = os.environ.get("APLEXER_SESSION_ID", "")
            sender_rec = next((r for r in rows if str(r.get("id", "")).lower() == sid.lower() and sid
                               and r.get("tag") and r.get("workspace")), None)
            # REAL installed DTO: envelope "from" carries ONLY
            # engine/session_id/tag/workspace (+external flag here for the
            # anonymous case). created_at_ms NEVER appears in "from" (live
            # probe 2026-10-17); provenance comes from the live catalog row.
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
            orig_id = opt(args, "--id") or (bare(rest)[0] if bare(rest) else None)
            data = json.loads(opt(rest, "--data") or "{}")
            tails = bare(rest)
            body = tails[-1] if len(tails) > 1 else ""
            orig = None
            if os.path.isdir(MDIR):
                for fn in os.listdir(MDIR):
                    with open(os.path.join(MDIR, fn)) as f: m = json.load(f)
                    if m.get("id") == orig_id: orig = m; break
            if orig is None: sys.stderr.write("no such message\n"); sys.exit(1)
            if not (orig.get("from") or {}).get("tag"):
                sys.stderr.write("Error: original message %s was sent anonymously (no sender tag)\n" % orig_id)
                sys.exit(1)
            # Installed routing (live probe 2026-10-17): a reply is filed in
            # the ORIGINAL AUTHOR's mailbox, not the replier's workspace.
            ws = (orig.get("from") or {}).get("workspace") or orig["workspace"]
            rows = load()
            sid = os.environ.get("APLEXER_SESSION_ID", "")
            replier = next((r for r in rows if str(r.get("id", "")).lower() == sid.lower() and sid
                            and r.get("tag") and r.get("workspace")), None)
            if replier is None:
                sys.stderr.write("Error: reply requires calling aplexer session\n"); sys.exit(1)
            mid = str(uuid.uuid4())
            env = {"schema_version": 1, "id": mid, "workspace": ws,
                   "created_at": int(time.time()),
                   # REAL installed DTO (see note above): no created_at_ms in
                   # "from"; stale-incarnation tests mutate the envelope explicitly.
                   "from": {"session_id": str(replier["id"]),
                            "workspace": str(replier["workspace"]),
                            "tag": str(replier["tag"]), "engine": "shell",
                            "external": False},
                   "to": {"tag": (orig.get("from") or {}).get("tag"),
                          "session_id": (orig.get("from") or {}).get("session_id")},
                   "kind": "reply", "data": data, "body": body,
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


class StubHub:
    """Minimal server DTO/state model of src/session_control.ts claim/ack.

    Tracks per-command claim tokens and ack state; raises ValueError with an
    HTTP status marker (like the production client) for terminal verdicts.
    Modes: ack_mode 'ok' | 'raise_once' (lost response before commit) |
    'commit_then_raise' (lost response after commit).
    """

    def __init__(self):
        self.offers: list[dict] = []
        self.claims: dict[str, dict] = {}
        self.acks: dict[str, dict] = {}
        self.ack_calls = 0
        self.ack_mode = "ok"
        self.claim_calls = 0

    def err(self, status, name):
        return ValueError("Session HTTP request failed with status %d (%s)" % (status, name))

    def request(self, method, path, payload):
        if method == "GET" and path == "/api/session-commands/offers":
            return {"offers": list(self.offers)}
        if method == "POST" and path.endswith("/claim"):
            cid = path.split("/")[-2]
            match = [o for o in self.offers if o["command_id"] == cid]
            if not match:
                raise self.err(404, "not_found")
            self.claim_calls += 1
            if cid in self.claims:
                if payload.get("attempt_id") == self.claims[cid]["attempt_id"]:
                    return dict(self.claims[cid]["grant"])
                raise self.err(409, "already_claimed")
            grant = {"command_id": cid, "claim_token": "tok-" + uuid.uuid4().hex,
                     "holder": "hetzner1", "generation": match[0]["generation"],
                     "expires_at": "2030-01-01T00:00:00.000Z",
                     "expires_at_ms": int(time.time() * 1000) + 60000, "attempt": 1}
            self.claims[cid] = {"attempt_id": payload.get("attempt_id"), "grant": grant,
                                "correlation": match[0]["correlation"]}
            return dict(grant)
        if method == "POST" and path.endswith("/ack"):
            cid = path.split("/")[-2]
            self.ack_calls += 1
            if cid not in self.claims:
                raise self.err(409, "claim_required")
            if payload.get("claim_token") != self.claims[cid]["grant"]["claim_token"]:
                raise self.err(409, "claim_conflict")
            if self.ack_mode == "raise_once":
                self.ack_mode = "ok"
                raise self.err(502, "lost_response_before_commit")
            if self.ack_mode == "commit_then_raise":
                self.ack_mode = "ok"
                self.acks[cid] = dict(payload)
                raise self.err(502, "lost_response_after_commit")
            self.acks[cid] = dict(payload)
            return {"command_id": cid, "state": "acked"}
        raise AssertionError((method, path))


@pytest.fixture()
def env(tmp_path_factory, monkeypatch):
    base = tempfile.mkdtemp(dir=REPO_TMP, prefix="sess-repair-")
    monkeypatch.setenv("TMPDIR", REPO_TMP)
    work = os.path.join(base, "work")
    os.makedirs(work)
    sessions_file = os.path.join(base, "sessions.json")
    sender_id = str(uuid.uuid4())
    with open(sessions_file, "w") as f:
        json.dump([{"id": sender_id, "tag": "host-sender", "workspace": work,
                    "engine": "shell", "profile": None, "phase": "running",
                    "state": "running", "created_at_ms": 1700000000000,
                    "last_activity_ms": 1700000000000}], f)
    monkeypatch.setenv("APLEXER_SESSION_ID", sender_id)
    msgs = os.path.join(base, "msgs")
    os.makedirs(msgs)
    fake = os.path.join(base, "fake-a")
    with open(fake, "w") as f:
        f.write(REPAIR_FAKE_A)
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


def send_offer(command_id, target, generation=1, body="hello"):
    return {"command_id": command_id, "session_id": "sess-1", "kind": "send",
            "generation": generation, "session_state": "running",
            "fleet_launch_id": "launch-1",
            "binding": {"native_id": target.session_id, "tag": target.tag,
                        "workspace": target.workspace, "generation": generation,
                        "native_created_at_ms": target.created_at_ms},
            "native_tag": target.tag, "workspace": "demo", "cwd": None,
            "payload": {"body": body},
            "correlation": "cmd:%s:gen:%d" % (command_id, generation)}


def as_target(env, target):
    """Environment running as the recipient session (for its reply)."""
    merged = dict(os.environ)
    rows = json.load(open(env["sessions_file"]))
    rec = next(r for r in rows if r.get("tag") == target.tag)
    merged["APLEXER_SESSION_ID"] = rec["id"]
    return merged


# --- F1: delayed recipient reply reconciles without a second send ----------

def test_f1_delayed_reply_reconciles_across_cycles_without_second_send(env):
    target = seed_target(env)
    cid = str(uuid.uuid4())
    hub = StubHub()
    hub.offers = [send_offer(cid, target)]
    first = rc.receive_once(custody=custody(), journal_dir=env["journal"],
                            executable=env["fake"], request=hub.request, limit=20)
    assert first["offers_seen"] == 1
    assert first["processed"] and first["processed"][0]["state"] == "unknown"
    assert first["processed"][0]["ack_submitted"] is False
    assert hub.ack_calls == 0  # no premature uncertain POST on the 0ms check
    assert len(os.listdir(env["msgs"])) == 1
    sent_id = json.load(open(os.path.join(env["journal"], "cmd-" + cid + ".json")))["original_native_message_id"]
    # The recipient replies asynchronously, AFTER the first cycle.
    digest = hashlib.sha256(b"hello").hexdigest()
    rep = subprocess.run(
        [env["fake"], "message", "reply", sent_id,
         "--data", json.dumps({"correlation_id": cid, "body_digest": digest}),
         "--", "reply %s" % cid],
        capture_output=True, text=True, timeout=20, env=as_target(env, target))
    assert rep.returncode == 0, rep.stderr
    reply = json.loads(rep.stdout)
    assert reply["reply_to"] == sent_id
    # Second cycle: read-only reconciliation, same claim token, no second send.
    hub.offers = []
    second = rc.receive_once(custody=custody(), journal_dir=env["journal"],
                             executable=env["fake"], request=hub.request, limit=20)
    assert len(os.listdir(env["msgs"])) == 2  # one send envelope + one reply only
    assert hub.ack_calls == 1
    assert second["reconciled"] and second["reconciled"][0]["state"] == "acknowledged"
    assert second["reconciled"][0]["ack_submitted"] is True
    acked = hub.acks[cid]
    assert acked["outcome"] == "delivered"
    assert acked["correlation"] == "cmd:%s:gen:1" % cid
    assert acked["native"]["native_id"] == target.session_id
    assert acked["proof"]["native_message_id"] == sent_id
    assert acked["proof"]["reply_to_native_id"] == reply["id"]
    assert acked["proof"]["sender_native_id"] == target.session_id
    assert acked["proof"]["sender_created_at_ms"] == 1700000000000


def test_f1_restart_reconciles_from_durable_journal(env):
    target = seed_target(env)
    cid = str(uuid.uuid4())
    hub = StubHub()
    hub.offers = [send_offer(cid, target)]
    rc.receive_once(custody=custody(), journal_dir=env["journal"],
                    executable=env["fake"], request=hub.request, limit=20)
    journal = json.load(open(os.path.join(env["journal"], "cmd-" + cid + ".json")))
    assert journal["state"] == "unknown" and not journal.get("ack_submitted")
    sent_id = journal["original_native_message_id"]
    digest = hashlib.sha256(b"hello").hexdigest()
    rep = subprocess.run(
        [env["fake"], "message", "reply", sent_id,
         "--data", json.dumps({"correlation_id": cid, "body_digest": digest}),
         "--", "reply %s" % cid],
        capture_output=True, text=True, timeout=20, env=as_target(env, target))
    assert rep.returncode == 0, rep.stderr
    # Simulated process restart: no in-memory state is consulted; the journal
    # plus the native log are sufficient for the identical repair path.
    hub.offers = []
    updated = se.reconcile_pending_command(
        journal_dir=env["journal"], command_id=cid,
        executable=env["fake"], request=hub.request)
    assert updated is not None and updated["state"] == "acknowledged"
    assert hub.ack_calls == 1 and hub.acks[cid]["outcome"] == "delivered"


def test_f1_silent_recipient_never_posts_uncertain(env):
    target = seed_target(env)
    cid = str(uuid.uuid4())
    hub = StubHub()
    hub.offers = [send_offer(cid, target)]
    rc.receive_once(custody=custody(), journal_dir=env["journal"],
                    executable=env["fake"], request=hub.request, limit=20)
    hub.offers = []
    rc.receive_once(custody=custody(), journal_dir=env["journal"],
                    executable=env["fake"], request=hub.request, limit=20)
    assert hub.ack_calls == 0
    assert len(os.listdir(env["msgs"])) == 1  # exactly one native effect
    journal = json.load(open(os.path.join(env["journal"], "cmd-" + cid + ".json")))
    assert journal["state"] == "unknown" and not journal.get("ack_submitted")


def test_f1_reconcile_posts_after_claim_deadline_without_new_effect(env):
    target = seed_target(env)
    cid = str(uuid.uuid4())
    hub = StubHub()
    hub.offers = [send_offer(cid, target)]
    rc.receive_once(custody=custody(), journal_dir=env["journal"],
                    executable=env["fake"], request=hub.request, limit=20)
    journal_path = os.path.join(env["journal"], "cmd-" + cid + ".json")
    journal = json.load(open(journal_path))
    sent_id = journal["original_native_message_id"]
    # The execution lease deadline passes with no reply (the server would
    # sweep the command to uncertain): the host journal keeps no client-side
    # deadline of its own — reconciliation stays available, never a new send.
    journal["expires_at_ms"] = int(time.time() * 1000) - 60000
    json.dump(journal, open(journal_path, "w"))
    digest = hashlib.sha256(b"hello").hexdigest()
    rep = subprocess.run(
        [env["fake"], "message", "reply", sent_id,
         "--data", json.dumps({"correlation_id": cid, "body_digest": digest}),
         "--", "late reply %s" % cid],
        capture_output=True, text=True, timeout=20, env=as_target(env, target))
    assert rep.returncode == 0, rep.stderr
    hub.offers = []
    second = rc.receive_once(custody=custody(), journal_dir=env["journal"],
                             executable=env["fake"], request=hub.request, limit=20)
    assert hub.ack_calls == 1 and hub.acks[cid]["outcome"] == "delivered"
    assert second["reconciled"] and second["reconciled"][0]["state"] == "acknowledged"
    assert len(os.listdir(env["msgs"])) == 2  # one send + one reply, no resend


# --- F2: genuine sender qualification ----------------------------------------

def test_f2_unbound_host_process_holds_send_without_claim_or_effect(env):
    target = seed_target(env)
    cid = str(uuid.uuid4())
    hub = StubHub()
    hub.offers = [send_offer(cid, target)]
    env["monkeypatch"].delenv("APLEXER_SESSION_ID")
    result = rc.receive_once(custody=custody(), journal_dir=env["journal"],
                             executable=env["fake"], request=hub.request, limit=20)
    assert result["processed"] == []
    assert any("native_sender_unbound" in s.get("reason", "") for s in result["skipped"])
    assert hub.claim_calls == 0  # held BEFORE the claim: no fence consumed
    assert hub.ack_calls == 0
    assert os.listdir(env["msgs"]) == []
    assert os.listdir(env["journal"]) == [] or all(
        not n.startswith("cmd-") for n in os.listdir(env["journal"]))


def test_f2_bound_send_carries_genuine_replyable_sender(env):
    target = seed_target(env)
    out, _sender = ns_mod.send_inbox(
        env["fake"], target, "bound hello", {"correlation_id": "f2-c1"})
    assert out.state == "recorded" and out.native_id
    envelope = json.load(open(os.path.join(env["msgs"], out.native_id + ".json")))
    assert envelope["from"]["tag"] == "host-sender"
    assert envelope["from"]["session_id"] == env["sender_id"]
    assert "external" not in envelope["from"] or envelope["from"].get("external") is False
    # The recipient CAN reply (installed contract), threading the envelope.
    rep = subprocess.run(
        [env["fake"], "message", "reply", out.native_id, "--", "got it"],
        capture_output=True, text=True, timeout=20, env=as_target(env, target))
    assert rep.returncode == 0, rep.stderr
    assert json.loads(rep.stdout)["reply_to"] == out.native_id


def test_f2_anonymous_envelope_is_unreplyable(env):
    target = seed_target(env)
    env["monkeypatch"].delenv("APLEXER_SESSION_ID")
    proc = subprocess.run(
        [env["fake"], "message", "send", "--workspace", env["work"], "--to", "agent1",
         "--kind", "note", "--data", "{}", "--json", "--", "anon hello"],
        capture_output=True, text=True, timeout=20)
    assert proc.returncode == 0
    envelope = json.loads(proc.stdout)
    assert envelope["from"] == {"external": True}
    # Installed Rust reply_target rejection, verbatim.
    rep = subprocess.run(
        [env["fake"], "message", "reply", envelope["id"], "--", "try reply"],
        capture_output=True, text=True, timeout=20, env=as_target(env, target))
    assert rep.returncode == 1
    assert "was sent anonymously (no sender tag)" in rep.stderr
    # And the product refuses to emit such envelopes in the first place.
    with pytest.raises(SessionControlError, match="native_sender_unbound"):
        ns_mod.send_inbox(
            env["fake"], target, "nope", {"correlation_id": "f2-c2"})


def test_f2_cross_workspace_send_succeeds_with_sender_mailbox_ack(env):
    # R2-F5 live-protocol correction: a bound sender in ANOTHER workspace is
    # NOT unbound. Cross-workspace sends succeed; the sender mailbox (the
    # author's own workspace) is where replies land and where ACK proof is
    # read — never the target workspace.
    other = os.path.join(env["base"], "other-ws")
    os.makedirs(other)
    rows = json.load(open(env["sessions_file"]))
    foreign_id = str(uuid.uuid4())
    rows.append({"id": foreign_id, "tag": "elsewhere", "workspace": other,
                 "engine": "shell", "phase": "running", "state": "running",
                 "created_at_ms": 1700000000000, "last_activity_ms": 1700000000000})
    json.dump(rows, open(env["sessions_file"], "w"))
    env["monkeypatch"].setenv("APLEXER_SESSION_ID", foreign_id)
    target = seed_target(env)  # target lives in env["work"], sender in other
    assert target.workspace != other
    out, sender = ns_mod.send_inbox(
        env["fake"], target, "cross hello", {"correlation_id": "f2-c3"})
    assert out.state == "recorded" and out.native_id
    assert sender.workspace == other  # genuine sender binding, not target ws
    envelope = json.load(open(os.path.join(env["msgs"], out.native_id + ".json")))
    assert envelope["workspace"] == target.workspace  # filed for recipient
    assert envelope["from"]["workspace"] == other  # ...authored from sender box
    assert envelope["from"]["session_id"] == foreign_id
    assert "created_at_ms" not in envelope["from"]  # real DTO fidelity
    # The recipient replies; the reply lands in the SENDER mailbox (other).
    reply_env = dict(os.environ)
    reply_env["APLEXER_SESSION_ID"] = target.session_id
    digest = ns_mod.body_digest("cross hello")
    rep = subprocess.run(
        [env["fake"], "message", "reply", out.native_id,
         "--data", json.dumps({"correlation_id": "f2-c3", "body_digest": digest}),
         "--", "got cross"],
        capture_output=True, text=True, timeout=20, env=reply_env)
    assert rep.returncode == 0, rep.stderr
    reply = json.loads(rep.stdout)
    assert reply["workspace"] == other  # sender mailbox, not target ws
    assert reply["from"]["workspace"] == target.workspace
    # ACK proof reads the sender mailbox and the observed live incarnation.
    proven = ns_mod.verify_ack_via_reply(
        env["fake"], target, sender, out.native_id,
        {"correlation_id": "f2-c3", "body_digest": digest})
    assert proven.state == "acknowledged" and proven.reply_envelope_id
    assert proven.evidence["sender_created_at_ms"] == 1700000000000


# --- F4: durable effect with lost ack ----------------------------------------

def capture_offer(command_id, target, generation=1, max_bytes=512):
    return {"command_id": command_id, "session_id": "sess-1", "kind": "capture",
            "generation": generation, "session_state": "running",
            "fleet_launch_id": "launch-1",
            "binding": {"native_id": target.session_id, "tag": target.tag,
                        "workspace": target.workspace, "generation": generation,
                        "native_created_at_ms": target.created_at_ms},
            "native_tag": target.tag, "workspace": "demo", "cwd": None,
            "payload": {"max_bytes": max_bytes},
            "correlation": "cmd:%s:gen:%d" % (command_id, generation)}


def test_f4_lost_ack_before_commit_resubmits_identically_without_new_effect(env):
    target = seed_target(env)
    cid = str(uuid.uuid4())
    hub = StubHub()
    hub.offers = [capture_offer(cid, target)]
    hub.ack_mode = "raise_once"
    first = rc.receive_once(custody=custody(), journal_dir=env["journal"],
                            executable=env["fake"], request=hub.request, limit=20)
    assert first["processed"] and first["processed"][0]["state"] == "captured"
    assert first["processed"][0]["ack_submitted"] is False
    assert hub.ack_calls == 1 and cid not in hub.acks  # lost before commit
    journal = json.load(open(os.path.join(env["journal"], "cmd-" + cid + ".json")))
    assert journal["ack_submitted"] is False and isinstance(journal.get("ack_payload"), dict)
    # The server no longer offers the claimed command; the journal is the
    # discovery path for the identical re-submit (no new native effect).
    hub.offers = []
    second = rc.receive_once(custody=custody(), journal_dir=env["journal"],
                             executable=env["fake"], request=hub.request, limit=20)
    assert hub.ack_calls == 2 and cid in hub.acks
    assert second["reconciled"] and second["reconciled"][0]["ack_submitted"] is True
    journal = json.load(open(os.path.join(env["journal"], "cmd-" + cid + ".json")))
    assert journal["ack_submitted"] is True


def test_f4_lost_ack_after_commit_is_idempotent_without_new_effect(env):
    target = seed_target(env)
    cid = str(uuid.uuid4())
    hub = StubHub()
    hub.offers = [capture_offer(cid, target)]
    hub.ack_mode = "commit_then_raise"
    rc.receive_once(custody=custody(), journal_dir=env["journal"],
                    executable=env["fake"], request=hub.request, limit=20)
    assert hub.ack_calls == 1 and cid in hub.acks  # server committed
    journal = json.load(open(os.path.join(env["journal"], "cmd-" + cid + ".json")))
    assert journal["ack_submitted"] is False  # host never saw the response
    hub.offers = []
    second = rc.receive_once(custody=custody(), journal_dir=env["journal"],
                             executable=env["fake"], request=hub.request, limit=20)
    assert hub.ack_calls == 2  # identical proven ack re-submitted once
    assert second["reconciled"] and second["reconciled"][0]["ack_submitted"] is True


def test_f4_terminal_server_verdict_closes_journal_without_new_effect(env):
    target = seed_target(env)
    cid = str(uuid.uuid4())
    hub = StubHub()
    hub.offers = [capture_offer(cid, target)]

    calls = {"n": 0}
    real_request = hub.request

    def flaky(method, path, payload):
        if method == "POST" and path.endswith("/ack"):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ValueError("Session HTTP request failed with status 502 (lost)")
            raise ValueError("Session HTTP request failed with status 409 (claim_expired)")
        return real_request(method, path, payload)

    rc.receive_once(custody=custody(), journal_dir=env["journal"],
                    executable=env["fake"], request=flaky, limit=20)
    journal = json.load(open(os.path.join(env["journal"], "cmd-" + cid + ".json")))
    assert journal["ack_submitted"] is False
    second = rc.receive_once(custody=custody(), journal_dir=env["journal"],
                             executable=env["fake"], request=flaky, limit=20)
    assert calls["n"] == 2  # exactly one re-submit, no effect repetition
    journal = json.load(open(os.path.join(env["journal"], "cmd-" + cid + ".json")))
    assert journal["state"] == "uncertain"  # honest terminal, no new effect


# --- F6: pagination, body fallback, sender incarnation ----------------------

def test_f6_wait_for_command_finds_command_past_first_page(monkeypatch):
    pages = [
        {"commands": [{"id": "cmd-%03d" % i, "state": "acked"} for i in range(100)],
         "next_cursor": "cursor-page-2"},
        {"commands": [{"id": "cmd-101", "state": "acked"}],
         "next_cursor": None},
    ]
    calls = {"n": 0}

    def fake_request(method, path):
        assert method == "GET"
        calls["n"] += 1
        if "cursor=" in path:
            return pages[1]
        return pages[0]

    monkeypatch.setattr(sc, "request", fake_request)
    monkeypatch.setattr(sc.time, "sleep", lambda s: None)
    found = sc.wait_for_command("sess-1", "cmd-101", timeout=30, interval=1)
    assert found["id"] == "cmd-101"
    assert calls["n"] == 2


def test_f6_wait_for_command_missing_after_last_page_still_fails(monkeypatch):
    monkeypatch.setattr(sc, "request",
                        lambda m, p: {"commands": [{"id": "x", "state": "acked"}],
                                      "next_cursor": None})
    monkeypatch.setattr(sc.time, "sleep", lambda s: None)
    import argparse as _ap
    with pytest.raises(ValueError, match="reconcile before retrying"):
        sc.wait_for_command("sess-1", "absent", timeout=1, interval=1)


def test_f6_list_state_filters_across_pages(monkeypatch):
    import argparse as _ap
    pages = [
        {"sessions": [{"id": "a", "state": "offered"}, {"id": "b", "state": "offered"}],
         "next_cursor": "c2"},
        {"sessions": [{"id": "c", "state": "running"}, {"id": "d", "state": "offered"}],
         "next_cursor": None},
    ]

    def fake_request(method, path):
        return pages[1] if "cursor=" in path else pages[0]

    monkeypatch.setattr(sc, "request", fake_request)
    root = _ap.ArgumentParser()
    sub = root.add_subparsers(dest="cmd", required=True)
    sc.add_commands(sub)
    args = root.parse_args(["sessions", "list", "--state", "running", "--limit", "50"])
    result = sc.execute(args)
    assert [s["id"] for s in result["sessions"]] == ["c"]


def test_f6_body_fallback_reply_is_proven_without_data(env):
    target = seed_target(env)
    ns = ns_mod
    body = "prompt with body echo"
    sent, sender = ns.send_inbox(env["fake"], target, body, {"correlation_id": "f6-c1"})
    assert ns.verify_ack_via_reply(
        env["fake"], target, sender, sent.native_id,
        {"correlation_id": "f6-c1", "body_digest": ns.body_digest(body)}).state == "unknown"
    # Standard reply WITHOUT --data: the body echoes correlation id + digest.
    digest = ns.body_digest(body)
    rep = subprocess.run(
        [env["fake"], "message", "reply", sent.native_id, "--",
         "done f6-c1 %s" % digest],
        capture_output=True, text=True, timeout=20, env=as_target(env, target))
    assert rep.returncode == 0, rep.stderr
    proven = ns.verify_ack_via_reply(
        env["fake"], target, sender, sent.native_id,
        {"correlation_id": "f6-c1", "body_digest": digest})
    assert proven.state == "acknowledged" and proven.reply_envelope_id
    # The actual sender incarnation is reported, not overwritten.
    assert proven.evidence["sender_created_at_ms"] == 1700000000000


def test_f6_stale_sender_incarnation_is_rejected(env):
    target = seed_target(env)
    ns = ns_mod
    body = "incarnation check"
    sent, sender = ns.send_inbox(env["fake"], target, body, {"correlation_id": "f6-c2"})
    digest = ns.body_digest(body)
    rep = subprocess.run(
        [env["fake"], "message", "reply", sent.native_id,
         "--data", json.dumps({"correlation_id": "f6-c2", "body_digest": digest}),
         "--", "reply f6-c2"],
        capture_output=True, text=True, timeout=20, env=as_target(env, target))
    assert rep.returncode == 0, rep.stderr
    reply_id = json.loads(rep.stdout)["id"]
    # Positive control first: the genuine reply proves processing.
    assert ns.verify_ack_via_reply(
        env["fake"], target, sender, sent.native_id,
        {"correlation_id": "f6-c2", "body_digest": digest}).state == "acknowledged"
    # A reply carrying a DIFFERENT actual sender incarnation (same session
    # id/tag/workspace, rotated creation) is not proof from THIS binding:
    # the real value is checked, never overwritten with the expectation.
    reply_path = os.path.join(env["msgs"], reply_id + ".json")
    envelope = json.load(open(reply_path))
    envelope["from"]["created_at_ms"] = 1800000000000
    json.dump(envelope, open(reply_path, "w"))
    assert ns.verify_ack_via_reply(
        env["fake"], target, sender, sent.native_id,
        {"correlation_id": "f6-c2", "body_digest": digest}).state == "unknown"


def test_f6_tag_reuse_requalification_blocks_foreign_proof(env):
    target = seed_target(env)
    ns = ns_mod
    body = "tag reuse check"
    sent, sender = ns.send_inbox(env["fake"], target, body, {"correlation_id": "f6-c3"})
    digest = ns.body_digest(body)
    rep = subprocess.run(
        [env["fake"], "message", "reply", sent.native_id,
         "--data", json.dumps({"correlation_id": "f6-c3", "body_digest": digest}),
         "--", "reply f6-c3"],
        capture_output=True, text=True, timeout=20, env=as_target(env, target))
    assert rep.returncode == 0, rep.stderr
    # The tag now resolves to a DIFFERENT live session: exact requalification
    # refuses to attribute the old reply to the new binding.
    rows = json.load(open(env["sessions_file"]))
    rec = next(r for r in rows if r.get("tag") == "agent1")
    rec["id"] = str(uuid.uuid4())
    json.dump(rows, open(env["sessions_file"], "w"))
    drifted = NativeTarget(session_id=rec["id"], tag="agent1",
                           workspace=env["work"], created_at_ms=1700000000000)
    assert ns.verify_ack_via_reply(
        env["fake"], drifted, sender, sent.native_id,
        {"correlation_id": "f6-c3", "body_digest": digest}).state == "unknown"
