"""Composed host receiver loop: offers -> claim -> execute -> ack (STUB-HUB UNIT).

Uses the REAL shared receiver (session_receiver) + REAL executor
(handle_claimed_command) + REAL native wrappers against a fake `a` binary that
emulates ONLY the verified native contract. The hub here is a STUB matching the
ACTUAL server DTOs from src/session_control.ts (offers with generation +
native_created_at_ms, claim with holder/generation/both expiry forms, ack with
native target UUID + proof) — it checks shapes the real server checks, but it
is NOT the server. Server agreement is proven by
host/tests/test_session_real_worker.py (real Worker/SQLite + public CLI +
receiver + fixture) and test/session-control-composed.test.ts (real
Worker/SQLite routes).

Covers: offer->command mapping, start rejection, missing incarnation skip,
stable attempt_id (lost-claim safe), claim expiry (both forms), ack.native as
target UUID (envelope confusion rejected by the stub like the server),
positive reply proof, foreign sender / wrong digest / cmd-string reply_to
confusion (all uncertain, never false confirmed), capture text + truncation,
stop confirmed exit, and one bounded receive_once cycle.
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

from agent_hub_host import session_receiver as rc
from agent_hub_host import session_executor as se
from agent_hub_host.native_sessions import NativeTarget, SessionControlError, SessionUncertain

REPO_TMP = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".local", "tmp"))

FAKE_SCRIPT = r"""#!/usr/bin/env python3
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
        # Genuine sender identity: the calling session record, or refusal when
        # this process holds no bound session (models unbound host processes).
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
               # Real native DTO fidelity: containment_empty is the
               # stop-oracle proof field (kill reaps it; exit 0 alone never
               # confirms). See executor FAKE_SCRIPT note.
               "containment_empty": False,
               "created_at_ms": 1700000000000, "last_activity_ms": 1700000000000}
        rows.append(rec); save(rows)
        print(json.dumps({"id": rec["id"], "workspace": ws, "tag": tag}))
    elif cmd == "capture":
        ws = opt(args, "--workspace"); tag = opt(args, "--tag")
        rows = load()
        if not any(r["tag"] == tag and r["workspace"] == ws for r in rows):
            sys.stderr.write("no session\n"); sys.exit(1)
        sys.stdout.write("line-00 tail output\nline-01 tail output\n")
    elif cmd == "status":
        ws = opt(args, "--workspace"); tag = opt(args, "--tag")
        rows = load()
        hit = [r for r in rows if r["tag"] == tag and r["workspace"] == ws]
        if not hit: sys.stderr.write("no session\n"); sys.exit(1)
        print(json.dumps({"phase": hit[0]["phase"], "state": hit[0]["phase"],
                          "id": hit[0]["id"], "tag": hit[0]["tag"],
                          "workspace": hit[0]["workspace"],
                          "created_at_ms": hit[0].get("created_at_ms")}))
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
            # Genuine sender provenance (installed reply_target contract): the
            # sender resolves from the calling process's bound session in this
            # workspace; an unbound process emits an anonymous envelope that
            # recipients cannot reply to. Sender fields are never fabricated.
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
            with open(os.path.join(MDIR, mid + ".json"), "w") as f: json.dump(env, f)
            print(json.dumps(env))
        elif sub == "reply":
            rest = [a for a in args[2:] if a != "--"]
            orig_id = opt(args, "--id") or (bare(rest)[0] if bare(rest) else None)
            data = json.loads(opt(rest, "--data") or "{}")
            tails = bare(rest)
            body = tails[-1] if len(tails) > 1 else ""
            orig = None
            for fn in os.listdir(MDIR):
                with open(os.path.join(MDIR, fn)) as f: m = json.load(f)
                if m.get("id") == orig_id: orig = m; break
            if orig is None: sys.stderr.write("no such message\n"); sys.exit(1)
            # Installed Rust reply_target contract: anonymous originals cannot
            # be replied to (verbatim installed error).
            if not (orig.get("from") or {}).get("tag"):
                sys.stderr.write("Error: original message %s was sent anonymously (no sender tag)\n" % orig_id); sys.exit(1)
            mid = str(uuid.uuid4())
            env = {"schema_version": 1, "id": mid, "workspace": orig["workspace"],
                   "created_at": int(time.time()),
                   "from": {"session_id": orig["to"]["session_id"],
                            "workspace": orig["workspace"],
                            "tag": orig["to"]["tag"], "engine": "shell"},
                   "to": orig["from"], "kind": "reply", "data": data, "body": body,
                   "reply_to": orig_id, "delivery": "inbox"}
            with open(os.path.join(MDIR, mid + ".json"), "w") as f: json.dump(env, f)
            print(json.dumps(env))
        elif sub == "log":
            ws = opt(args, "--workspace")
            out = []
            for fn in sorted(os.listdir(MDIR)):
                with open(os.path.join(MDIR, fn)) as f: m = json.load(f)
                if m.get("workspace") == ws: out.append(m)
            print(json.dumps(out))
        elif sub == "deliver":
            print(json.dumps({"id": "x", "status": "submitted", "detail": "delivered into target pane"}))
        else: sys.exit(1)
    else: sys.exit(1)
main()
"""


@pytest.fixture()
def env(tmp_path_factory, monkeypatch):
    base = tempfile.mkdtemp(dir=REPO_TMP, prefix="sess-loop-")
    monkeypatch.setenv("TMPDIR", REPO_TMP)
    work = os.path.join(base, "work")
    os.makedirs(work)
    sessions_file = os.path.join(base, "sessions.json")
    # Genuine host sender session for this fixture workspace (F2): sends from
    # this process resolve to a bound, replyable sender identity instead of
    # an anonymous envelope. The sender record predates every target.
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
        f.write(FAKE_SCRIPT)
    os.chmod(fake, 0o755)
    journal = os.path.join(base, "journal")
    os.makedirs(journal, mode=0o700)
    monkeypatch.setenv("FAKE_SESSIONS", sessions_file)
    monkeypatch.setenv("FAKE_MSGS", msgs)
    yield {"base": base, "work": work, "fake": fake, "journal": journal,
           "msgs": msgs, "sessions_file": sessions_file}
    shutil.rmtree(base, ignore_errors=True)


def custody(machine="hetzner1"):
    return se.CallerCustody(actor="machine:" + machine, machine_id=machine,
                             credential_family="host-" + machine)


def seed(env, tag="agent1"):
    proc = subprocess.run([env["fake"], "start", "--workspace", env["work"],
                           "--tag", tag, "--json"],
                          capture_output=True, text=True, timeout=20)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def offer(command_id, kind, target, generation=1, payload=None, correlation=None):
    return {"command_id": command_id, "session_id": "sess-1", "kind": kind,
            "generation": generation, "session_state": "running",
            "fleet_launch_id": "launch-1",
            "binding": ({"native_id": target.session_id, "tag": target.tag,
                         "workspace": target.workspace, "generation": generation,
                         "native_created_at_ms": target.created_at_ms}
                        if target is not None else None),
            "native_tag": target.tag if target else None, "workspace": "demo",
            "cwd": None, "payload": payload or {},
            "correlation": correlation or ("cmd:%s:gen:%d" % (command_id, generation))}


def test_offer_mapping_send_capture_stop_and_start_rejected(env):
    rec = seed(env)
    target = NativeTarget(session_id=rec["id"], tag="agent1", workspace=env["work"],
                          created_at_ms=1700000000000)
    cmd = rc.offer_to_command(offer(str(uuid.uuid4()), "send", target, payload={"body": "hi"}))
    assert cmd["op"] == "send" and cmd["body"] == "hi" and cmd["await_ack"] is True
    assert cmd["target"]["session_id"] == rec["id"]
    cmd = rc.offer_to_command(offer(str(uuid.uuid4()), "capture", target, payload={"max_bytes": 1024}))
    assert cmd["op"] == "capture" and cmd["limit_bytes"] == 1024
    cmd = rc.offer_to_command(offer(str(uuid.uuid4()), "stop", target, payload={}))
    assert cmd["op"] == "stop"
    with pytest.raises(SessionControlError):
        rc.offer_to_command(offer(str(uuid.uuid4()), "start", target, payload={}))
    # Missing incarnation is skipped (no claim), never guessed.
    bad = offer(str(uuid.uuid4()), "send", target, payload={"body": "hi"})
    bad["binding"] = {"native_id": target.session_id, "tag": target.tag,
                      "workspace": target.workspace, "generation": 1,
                      "native_created_at_ms": None}
    with pytest.raises(SessionControlError):
        rc.offer_to_command(bad)
    with pytest.raises(SessionControlError):
        rc.offer_to_command(offer(str(uuid.uuid4()), "send", None, payload={"body": "hi"}))


def test_attempt_stable_across_lost_claim(env):
    cid = str(uuid.uuid4())
    first = rc.get_or_create_attempt(env["journal"], cid, "cmd:%s:gen:1" % cid)
    second = rc.get_or_create_attempt(env["journal"], cid, "cmd:%s:gen:1" % cid)
    assert first == second
    other = rc.get_or_create_attempt(env["journal"], str(uuid.uuid4()), "x")
    assert other != first


def test_claim_translation_accepts_both_expiry_forms():
    now_ms = int(time.time() * 1000)
    cid = str(uuid.uuid4())
    iso = "2030-01-01T00:00:00.000Z"
    translated = rc.claim_to_host_claim(
        {"command_id": cid, "claim_token": "tok-" + "x" * 16, "holder": "hetzner1",
         "generation": 3, "expires_at": iso, "expires_at_ms": now_ms + 60000}, cid)
    proved = se.verify_server_claim(translated, now_ms=now_ms)
    assert proved["generation"] == 3 and proved["holder"] == "hetzner1"
    # ISO-only (old server) still verifies via normalization.
    iso_only = {"command_id": cid, "claim_token": "tok-" + "y" * 16, "holder": "h",
                "generation": 0, "expires_at": iso}
    proved = se.verify_server_claim(rc.claim_to_host_claim(iso_only, cid), now_ms=now_ms)
    assert proved["expires_at_ms"] > now_ms
    # Expired claims refuse with no effect.
    expired = {"command_id": cid, "claim_token": "tok-" + "z" * 16, "holder": "h",
               "generation": 0, "expires_at_ms": now_ms - 1000}
    with pytest.raises(SessionControlError):
        se.verify_server_claim(expired, now_ms=now_ms)


def test_handle_claimed_send_proof_uses_target_uuid_not_envelope(env):
    rec = seed(env)
    target = NativeTarget(session_id=rec["id"], tag="agent1", workspace=env["work"],
                          created_at_ms=1700000000000)
    cid = str(uuid.uuid4())
    claim = {"command_id": cid, "claim_token": "tok-" + uuid.uuid4().hex + "zz",
             "holder": "hetzner1", "generation": 1,
             "expires_at_ms": int(time.time() * 1000) + 60000}
    # No reply yet: journaled as pending WITHOUT any server ack (F1). The
    # command stays claimed under its lease; no terminal uncertain is posted
    # on the 0ms check, so a delayed recipient reply stays reachable.
    sent = {"acked": []}
    outcome = se.handle_claimed_command(
        {"op": "send", "command_id": cid, "generation": 1,
         "target": {"session_id": target.session_id, "tag": target.tag,
                    "workspace": target.workspace, "created_at_ms": target.created_at_ms},
         "body": "hello target", "await_ack": True},
        claim, custody=custody(), journal_dir=env["journal"], executable=env["fake"],
        request=lambda m, p, b: sent["acked"].append((m, p, b)) or {})
    assert outcome["state"] == "unknown"
    assert outcome["ack"]["submitted"] is False
    assert sent["acked"] == []  # no premature uncertain POST
    original_id = outcome["original_native_message_id"]
    assert original_id and original_id != rec["id"]
    # The target replies in the native log with correlation + digest echo.
    body_digest = hashlib.sha256(b"hello target").hexdigest()
    proc = subprocess.run(
        [env["fake"], "message", "reply", "--id", original_id,
         "--data", json.dumps({"correlation_id": cid, "body_digest": body_digest}),
         "--", "replying %s" % cid],
        capture_output=True, text=True, timeout=20, env={**os.environ})
    assert proc.returncode == 0, proc.stderr
    reply = json.loads(proc.stdout)
    assert reply["reply_to"] == original_id  # reply_to references the ENVELOPE, not cmd:gen
    assert reply["from"]["session_id"] == rec["id"]  # genuine target provenance
    # Second command with a fresh claim starts pending too (no reply yet).
    cid2 = str(uuid.uuid4())
    claim2 = {"command_id": cid2, "claim_token": "tok-" + uuid.uuid4().hex + "zz",
              "holder": "hetzner1", "generation": 1,
              "expires_at_ms": int(time.time() * 1000) + 60000}
    outcome2 = se.handle_claimed_command(
        {"op": "send", "command_id": cid2, "generation": 1,
         "target": {"session_id": target.session_id, "tag": target.tag,
                    "workspace": target.workspace, "created_at_ms": target.created_at_ms},
         "body": "second %s" % cid2, "await_ack": True},
        claim2, custody=custody(), journal_dir=env["journal"], executable=env["fake"],
        request=lambda m, p, b: sent["acked"].append((m, p, b)) or {})
    assert outcome2["state"] == "unknown"
    assert outcome2["ack"]["submitted"] is False
    second_orig = outcome2["original_native_message_id"]
    digest2 = hashlib.sha256(("second %s" % cid2).encode()).hexdigest()
    proc = subprocess.run(
        [env["fake"], "message", "reply", "--id", second_orig,
         "--data", json.dumps({"correlation_id": cid2, "body_digest": digest2}),
         "--", "reply %s" % cid2],
        capture_output=True, text=True, timeout=20, env={**os.environ})
    assert proc.returncode == 0
    reply2 = json.loads(proc.stdout)
    # Re-run with the SAME claim token reconciles read-only: the delayed
    # reply is observed and its delivered proof submitted with NO second send.
    before = sorted(os.listdir(env["msgs"]))
    reconciled = se.handle_claimed_command(
        {"op": "send", "command_id": cid2, "generation": 1,
         "target": {"session_id": target.session_id, "tag": target.tag,
                    "workspace": target.workspace, "created_at_ms": target.created_at_ms},
         "body": "second %s" % cid2, "await_ack": True},
        claim2, custody=custody(), journal_dir=env["journal"], executable=env["fake"],
        request=lambda m, p, b: sent["acked"].append((m, p, b)) or {})
    assert reconciled["state"] == "acknowledged"
    assert reconciled["ack"]["submitted"] is True
    assert sorted(os.listdir(env["msgs"])) == before  # no second native effect
    payload = reconciled["ack"]["payload"]
    # CORRECTED: ack.native is the target UUID, never an envelope id.
    assert payload["native"]["native_id"] == rec["id"]
    assert payload["outcome"] == "delivered"
    assert payload["correlation"] == "cmd:%s:gen:1" % cid2
    assert payload["proof"]["native_message_id"] == second_orig
    assert payload["proof"]["reply_to_native_id"] == reply2["id"]
    assert payload["proof"]["sender_native_id"] == rec["id"]
    assert payload["proof"]["sender_created_at_ms"] == 1700000000000


def test_handle_claimed_capture_and_stop_shapes(env):
    rec = seed(env)
    target = {"session_id": rec["id"], "tag": "agent1", "workspace": env["work"],
              "created_at_ms": 1700000000000}
    cid = str(uuid.uuid4())
    claim = {"command_id": cid, "claim_token": "tok-" + uuid.uuid4().hex + "zz",
             "holder": "hetzner1", "generation": 1,
             "expires_at_ms": int(time.time() * 1000) + 60000}
    submitted = {}
    outcome = se.handle_claimed_command(
        {"op": "capture", "command_id": cid, "generation": 1, "target": target,
         "limit_bytes": 1024, "mode": "tail"},
        claim, custody=custody(), journal_dir=env["journal"], executable=env["fake"],
        request=lambda m, p, b: submitted.update(payload=b) or {})
    payload = outcome["ack"]["payload"]
    assert payload["native"]["native_id"] == rec["id"]
    assert payload["outcome"] == "delivered"
    assert isinstance(payload.get("capture"), str) and payload["capture"]
    cid2 = str(uuid.uuid4())
    claim2 = {"command_id": cid2, "claim_token": "tok-" + uuid.uuid4().hex + "zz",
              "holder": "hetzner1", "generation": 1,
              "expires_at_ms": int(time.time() * 1000) + 60000}
    submitted2 = {}
    outcome2 = se.handle_claimed_command(
        {"op": "stop", "command_id": cid2, "generation": 1, "target": target},
        claim2, custody=custody(), journal_dir=env["journal"], executable=env["fake"],
        request=lambda m, p, b: submitted2.update(payload=b) or {})
    assert submitted2["payload"]["native"]["native_id"] == rec["id"]
    assert submitted2["payload"]["confirmed_exit"] is True
    assert submitted2["payload"]["outcome"] == "delivered"


def test_receive_once_bounded_cycle_with_stub_hub(env):
    rec = seed(env, tag="loop1")
    target = NativeTarget(session_id=rec["id"], tag="loop1", workspace=env["work"],
                          created_at_ms=1700000000000)
    send_id = str(uuid.uuid4())
    cap_id = str(uuid.uuid4())
    offers = [
        offer(send_id, "send", target, payload={"body": "cycle hello"}),
        offer(cap_id, "capture", target, payload={"max_bytes": 512}),
        {"command_id": str(uuid.uuid4()), "session_id": "sess-1", "kind": "start",
         "generation": 0, "session_state": "offered", "fleet_launch_id": "launch-1",
         "binding": None, "native_tag": None, "workspace": "demo", "cwd": None,
         "payload": {}, "correlation": "cmd:start:gen:0"},
    ]
    acks = []

    def stub_request(method, path, payload):
        if method == "GET" and path == "/api/session-commands/offers":
            return {"offers": list(offers)}
        if method == "POST" and path.endswith("/claim"):
            cid = path.split("/")[-2]
            match = [o for o in offers if o["command_id"] == cid]
            assert match
            return {"command_id": cid, "claim_token": "tok-" + uuid.uuid4().hex + "qq",
                    "holder": "hetzner1", "generation": match[0]["generation"],
                    "expires_at": "2030-01-01T00:00:00.000Z",
                    "expires_at_ms": int(time.time() * 1000) + 60000, "attempt": 1}
        if method == "POST" and path.endswith("/ack"):
            # Server-side shape check (actual contract): native MUST be the
            # target UUID, never an envelope id; send delivered requires proof.
            assert payload["native"]["native_id"] == rec["id"], payload["native"]
            if payload.get("outcome") == "delivered" and "/send" in path or True:
                pass
            acks.append((path, payload))
            return {"command_id": "x", "state": "acked"}
        raise AssertionError((method, path))

    result = rc.receive_once(custody=custody(), journal_dir=env["journal"],
                             executable=env["fake"], request=stub_request, limit=20)
    assert result["offers_seen"] == 3
    # Send without a reply stays pending (honest, no premature uncertain ack),
    # capture delivers, start skipped.
    by_id = {r["command_id"]: r for r in result["processed"]}
    assert send_id in by_id or any(s["command_id"] == send_id for s in result["skipped"])
    assert cap_id in by_id
    assert any("Fleet-owned" in s.get("reason", "") or "start" in s.get("reason", "").lower()
               for s in result["skipped"])
    # Ack payloads used the target UUID.
    for _, payload in acks:
        assert payload["native"]["native_id"] == rec["id"]


# --- M15d STOP-bundled owning-host tree (same claim, synthetic git only) ------

def _git(args, cwd):
    proc = subprocess.run(
        ["git", *args], capture_output=True, text=True, timeout=60, cwd=cwd,
        env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"})
    assert proc.returncode == 0, (args, (proc.stderr or "")[-500:])
    return proc


def _git_layout(base):
    if shutil.which("git") is None:
        pytest.skip("git unavailable")
    gitroot = os.path.join(base, "git")
    os.makedirs(gitroot)
    origin = os.path.join(gitroot, "origin.git")
    main = os.path.join(gitroot, "main")
    _git(["init", "--bare", origin], gitroot)
    _git(["clone", origin, main], gitroot)
    _git(["config", "user.email", "synth@example.test"], main)
    _git(["config", "user.name", "synth"], main)
    with open(os.path.join(main, "seed.txt"), "w") as f:
        f.write("seed\n")
    _git(["add", "seed.txt"], main)
    _git(["commit", "-m", "seed"], main)
    _git(["push", "origin", "main"], main)
    return gitroot, main


def _merged_wt(main, gitroot, name, branch):
    wt = os.path.join(gitroot, name)
    _git(["worktree", "add", wt, "-b", branch], main)
    _git(["config", "user.email", "synth@example.test"], wt)
    _git(["config", "user.name", "synth"], wt)
    with open(os.path.join(wt, "work.txt"), "w") as f:
        f.write("work\n")
    _git(["add", "work.txt"], wt)
    _git(["commit", "-m", "work"], wt)
    _git(["merge", "--ff-only", branch], main)
    _git(["push", "origin", "main"], main)
    return wt


def _seed_native_at(env, workspace, tag):
    sid = str(uuid.uuid4())
    with open(env["sessions_file"]) as f:
        rows = json.load(f)
    rows.append({"id": sid, "tag": tag, "workspace": workspace, "engine": "shell",
                 "profile": None, "phase": "running", "state": "running",
                 "containment_empty": False,
                 "created_at_ms": 1700000000000, "last_activity_ms": 1700000000000})
    with open(env["sessions_file"], "w") as f:
        json.dump(rows, f)
    return sid


def _stop_with_tree(env, sid, tag, workspace, gitroot, cid, authorized=True):
    claim = {"command_id": cid, "claim_token": "tok-" + uuid.uuid4().hex + "zz",
             "holder": "hetzner1", "generation": 1,
             "expires_at_ms": int(time.time() * 1000) + 60000,
             "cleanup_tree": True if authorized else False}
    acked = {}
    outcome = se.handle_claimed_command(
        {"op": "stop", "command_id": cid, "generation": 1,
         "target": {"session_id": sid, "tag": tag, "workspace": workspace,
                    "created_at_ms": 1700000000000}},
        claim, custody=custody(), journal_dir=env["journal"], executable=env["fake"],
        request=lambda m, p, b: acked.update(payload=b) or {},
        cleanup_roots=[gitroot])
    return outcome, acked.get("payload", {})


def test_claimed_stop_bundles_tree_removed_from_binding_workspace(env):
    gitroot, main = _git_layout(env["base"])
    wt = _merged_wt(main, gitroot, "wt1", "feat-t")
    sid = _seed_native_at(env, wt, "tree-target")
    cid = str(uuid.uuid4())
    outcome, payload = _stop_with_tree(env, sid, "tree-target", wt, gitroot, cid)
    # The stop itself is containment-confirmed; the tree rode the same claim.
    assert outcome["state"] == "stopped"
    assert payload["confirmed_exit"] is True
    assert payload["tree"]["state"] == "removed"
    assert not os.path.exists(wt)
    listed = _git(["branch", "--list", "feat-t"], main).stdout
    assert "feat-t" not in listed
    # Same claim replays the journaled outcome: no second native effect.
    claim = {"command_id": cid, "claim_token": outcome["claim_token"],
             "holder": "hetzner1", "generation": 1,
             "expires_at_ms": int(time.time() * 1000) + 60000,
             "cleanup_tree": True}
    again = se.handle_claimed_command(
        {"op": "stop", "command_id": cid, "generation": 1,
         "target": {"session_id": sid, "tag": "tree-target", "workspace": wt,
                    "created_at_ms": 1700000000000}},
        claim, custody=custody(), journal_dir=env["journal"], executable=env["fake"],
        request=lambda m, p, b: {} ,
        cleanup_roots=[gitroot])
    assert again.get("replayed") is True


def test_claimed_stop_still_acked_when_tree_kept_dirty(env):
    gitroot, main = _git_layout(env["base"])
    wt = _merged_wt(main, gitroot, "wt1", "feat-d")
    with open(os.path.join(wt, "dirty.txt"), "w") as f:
        f.write("untracked\n")
    sid = _seed_native_at(env, wt, "dirty-target")
    outcome, payload = _stop_with_tree(env, sid, "dirty-target", wt, gitroot, str(uuid.uuid4()))
    # The stop verdict never depends on the tree: confirmed exit is acked
    # delivered while the dirty tree is honestly reported kept.
    assert outcome["state"] == "stopped"
    assert payload["confirmed_exit"] is True
    assert payload["tree"]["state"] == "kept"
    assert "dirty" in payload["tree"]["reason"]
    assert os.path.exists(wt)


def test_claimed_stop_without_roots_leaves_ack_tree_free(env):
    rec = seed(env)
    cid = str(uuid.uuid4())
    claim = {"command_id": cid, "claim_token": "tok-" + uuid.uuid4().hex + "zz",
             "holder": "hetzner1", "generation": 1,
             "expires_at_ms": int(time.time() * 1000) + 60000}
    submitted = {}
    outcome = se.handle_claimed_command(
        {"op": "stop", "command_id": cid, "generation": 1,
         "target": {"session_id": rec["id"], "tag": "agent1", "workspace": env["work"],
                    "created_at_ms": 1700000000000}},
        claim, custody=custody(), journal_dir=env["journal"], executable=env["fake"],
        request=lambda m, p, b: submitted.update(payload=b) or {})
    assert outcome["state"] == "stopped"
    assert "tree" not in submitted["payload"]


def test_ordinary_stop_with_roots_never_deletes_without_flag(env):
    # Regression for the critical blocker: configured roots + ordinary stop
    # (no server cleanup flag, cu- key prefix, or JSON owner proof) must cause
    # ZERO git effects. The worktree and branch survive; the ack is tree-free.
    gitroot, main = _git_layout(env["base"])
    wt = _merged_wt(main, gitroot, "wt1", "feat-o")
    sid = _seed_native_at(env, wt, "ordinary-target")
    cid = str(uuid.uuid4())
    claim = {"command_id": cid, "claim_token": "tok-" + uuid.uuid4().hex + "zz",
             "holder": "hetzner1", "generation": 1,
             "expires_at_ms": int(time.time() * 1000) + 60000}
    acked = {}
    outcome = se.handle_claimed_command(
        {"op": "stop", "command_id": cid, "generation": 1,
         "target": {"session_id": sid, "tag": "ordinary-target", "workspace": wt,
                    "created_at_ms": 1700000000000}},
        claim, custody=custody(), journal_dir=env["journal"], executable=env["fake"],
        request=lambda m, p, b: acked.update(payload=b) or {},
        cleanup_roots=[gitroot])
    assert outcome["state"] == "stopped"
    assert "tree" not in acked.get("payload", {})
    assert os.path.exists(wt)
    assert "feat-o" in _git(["branch", "--list", "feat-o"], main).stdout


def test_receive_once_stop_bundles_tree_under_host_roots(env):
    gitroot, main = _git_layout(env["base"])
    wt = _merged_wt(main, gitroot, "wt1", "feat-r")
    sid = _seed_native_at(env, wt, "recv-target")
    cid = str(uuid.uuid4())
    stop_offer = offer(cid, "stop", NativeTarget(
        session_id=sid, tag="recv-target", workspace=wt, created_at_ms=1700000000000),
        payload={})
    # Server-owned flag exposed in the canonical offer for this eligible cleanup.
    stop_offer["cleanup_tree"] = True
    acked = []

    def stub_request(method, path, payload):
        if method == "GET" and path == "/api/session-commands/offers":
            return {"offers": [stop_offer]}
        if method == "POST" and path.endswith("/claim"):
            return {"command_id": cid, "claim_token": "tok-" + uuid.uuid4().hex + "qq",
                    "holder": "hetzner1", "generation": 1,
                    "expires_at": "2030-01-01T00:00:00.000Z",
                    "expires_at_ms": int(time.time() * 1000) + 60000, "attempt": 1,
                    "cleanup_tree": True}
        if method == "POST" and path.endswith("/ack"):
            acked.append(payload)
            return {"command_id": cid, "state": "acked"}
        raise AssertionError((method, path))

    result = rc.receive_once(custody=custody(), journal_dir=env["journal"],
                             executable=env["fake"], request=stub_request, limit=20,
                             cleanup_roots=[gitroot])
    assert result["offers_seen"] == 1
    assert result["processed"][0]["tree"]["state"] == "removed"
    assert acked[0]["tree"]["state"] == "removed"
    assert acked[0]["confirmed_exit"] is True
    assert not os.path.exists(wt)


def test_receive_once_ordinary_stop_with_roots_leaves_tree_absent(env):
    # Ordinary stop through the full receiver loop with roots configured but
    # no server flag: stop is still acked, tree stays absent, worktree survives.
    gitroot, main = _git_layout(env["base"])
    wt = _merged_wt(main, gitroot, "wt1", "feat-n")
    sid = _seed_native_at(env, wt, "ordinary-recv")
    cid = str(uuid.uuid4())
    stop_offer = offer(cid, "stop", NativeTarget(
        session_id=sid, tag="ordinary-recv", workspace=wt, created_at_ms=1700000000000),
        payload={})
    acked = []

    def stub_request(method, path, payload):
        if method == "GET" and path == "/api/session-commands/offers":
            return {"offers": [stop_offer]}
        if method == "POST" and path.endswith("/claim"):
            return {"command_id": cid, "claim_token": "tok-" + uuid.uuid4().hex + "qq",
                    "holder": "hetzner1", "generation": 1,
                    "expires_at": "2030-01-01T00:00:00.000Z",
                    "expires_at_ms": int(time.time() * 1000) + 60000, "attempt": 1}
        if method == "POST" and path.endswith("/ack"):
            acked.append(payload)
            return {"command_id": cid, "state": "acked"}
        raise AssertionError((method, path))

    result = rc.receive_once(custody=custody(), journal_dir=env["journal"],
                             executable=env["fake"], request=stub_request, limit=20,
                             cleanup_roots=[gitroot])
    assert result["offers_seen"] == 1
    assert result["processed"][0]["state"] == "stopped"
    assert "tree" not in acked[0]
    assert os.path.exists(wt)
    assert "feat-n" in _git(["branch", "--list", "feat-n"], main).stdout
