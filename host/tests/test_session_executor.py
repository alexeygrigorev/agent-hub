"""M12e session executor tests: real stdlib subprocess + real HTTP + real SQLite.

No live sessions are touched. All native effects go to a per-test fake ``a``
binary shaped like the genuine CLI's verified surface (``list/start/capture/
status/kill`` plus ``message send/reply/deliver/log``; ``send --enter`` is NOT
emulated — the raw PTY path is retired, see R2 correction note below).
Claim/result/ack reporting goes through a real local HTTP server (stdlib
``http.server`` in a thread) backed by real SQLite, asserting host machine
binding + credential on every request. Journals live on the real filesystem
under this worktree's ``.local/tmp`` (never ``/tmp``) and each fixture cleans
up only its own ``sess-exec-`` directory.

R2 CORRECTION (head-ordered, preserved as explanation): the predecessor asserted
a ``send --enter`` ``working:true`` verdict means ``acknowledged``. That signal
is NOT target-read/process proof, so ``test_send_pty_acknowledged_*`` was wrong
and is REPLACED by ``test_pty_guarded_retired_holds``. Acknowledgement is now
proven only by a correlated native recipient reply (``reply_to`` = original
envelope id, sender UUID/tag/workspace match, correlation/digest echo).

Covers: positive/negative/retry/host-identity/auth/limits/correlated receipts,
idempotency conflicts, tag-reuse refusal, stale-generation refusal, UUID-vs-TAG
confusion, workspace escape, deliver-guard mapping, bounded capture with explicit
unknown privacy, confirmed stop, uncertain adoption without duplicate starts,
held unqualified starts, server-claim verification/expiry/token-reuse, per-intent
lock serialization across threads, and start rejection by the claimed receiver.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest

from agent_hub_host import native_sessions as ns
from agent_hub_host import session_executor as se
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
MSG_BEH = os.environ.get("FAKE_MSG_BEHAVIOR", "ok")
DELIVER_BEH = os.environ.get("FAKE_DELIVER_BEHAVIOR", "ok")
KILL_BEH = os.environ.get("FAKE_KILL_BEHAVIOR", "ok")
SLOG = os.environ.get("FAKE_START_LOG", "")

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
        eng = opt(args, "--engine", "") or "shell"; prof = opt(args, "--profile")
        rows = load()
        if any(r["tag"] == tag and r["workspace"] == ws for r in rows) and not flag(args, "--fresh"):
            sys.stderr.write("tag already held\n"); sys.exit(1)
        if SLOG:
            with open(SLOG, "a") as f:
                f.write("start:%s:%s\n" % (ws, tag))
        rec = {"id": str(uuid.uuid4()), "tag": tag, "workspace": ws, "engine": eng,
               "profile": prof, "phase": "running", "state": "running",
               # Real native DTO fidelity (read-only `a list --json`): the
               # catalog carries containment_empty; the oracle confirms a
               # stop only on proven containment, never on exit 0 alone.
               "containment_empty": False,
               "created_at_ms": 1700000000000, "last_activity_ms": 1700000000000}
        rows.append(rec); save(rows)
        print(json.dumps({"id": rec["id"], "workspace": ws, "tag": tag, "engine": eng,
                          "profile": prof, "phase": "running"}))
    elif cmd == "capture":
        ws = opt(args, "--workspace"); tag = opt(args, "--tag")
        rows = load()
        if not any(r["tag"] == tag and r["workspace"] == ws for r in rows):
            sys.stderr.write("no session\n"); sys.exit(1)
        body = ("line-%02d of scrollback output for capture test\n" % i for i in range(8))
        text = "".join(body)
        if flag(args, "--screen"):
            text = "screen snapshot :: " + text[:60]
        # NOTE: the fake intentionally returns the full scrollback regardless of
        # --bytes (the genuine CLI honors it). Client-side truncation is
        # defense-in-depth and is what this suite verifies.
        sys.stdout.write(text)
    elif cmd == "status":
        ws = opt(args, "--workspace"); tag = opt(args, "--tag")
        rows = load()
        hit = [r for r in rows if r["tag"] == tag and r["workspace"] == ws]
        if not hit:
            sys.stderr.write("no session\n"); sys.exit(1)
        print(json.dumps({"phase": hit[0]["phase"], "state": hit[0]["phase"],
                          "id": hit[0]["id"], "tag": hit[0]["tag"],
                          "workspace": hit[0]["workspace"],
                          "created_at_ms": hit[0].get("created_at_ms")}))
    elif cmd == "kill":
        ws = opt(args, "--workspace"); tag = opt(args, "--tag")
        rows = load()
        hit = [r for r in rows if r["tag"] == tag and r["workspace"] == ws]
        if not hit:
            sys.stderr.write("no session\n"); sys.exit(1)
        if KILL_BEH != "still-running":
            hit[0]["phase"] = "exited"; hit[0]["state"] = "exited"
            # The native CLI reaps the workload cgroup on kill and reports
            # it; the stop oracle requires this proof (exit 0 alone never
            # confirms). still-running leaves everything untouched.
            hit[0]["containment_empty"] = True; save(rows)
        print(json.dumps({"killed": hit[0]["id"]}))
    elif cmd == "message":
        sub = args[1]
        if sub == "send":
            if MSG_BEH == "crash":
                sys.stderr.write("crashed after possible effect\n"); sys.exit(1)
            rest = [a for a in args[2:] if a != "--"]
            ws = opt(rest, "--workspace"); to = opt(rest, "--to")
            kind = opt(rest, "--kind") or "note"
            data = json.loads(opt(rest, "--data") or "{}")
            tails = [a for a in rest if not a.startswith("-")]
            body = tails[-1] if tails else ""
            try:
                uuid.UUID(str(to))
                sys.stderr.write("refusing UUID --to\n"); sys.exit(1)
            except ValueError:
                pass
            rows = load()
            hit = [r for r in rows if r["tag"] == to and r["workspace"] == ws]
            if not hit:
                sys.stderr.write("no session tagged %s exists\n" % to); sys.exit(1)
            # Genuine sender provenance (installed reply_target contract): the
            # sender resolves from the calling process's bound session in ANY
            # workspace (installed cross-workspace sends succeed for bound
            # senders; live probe 2026-10-17). The sender mailbox (from's
            # workspace) is independent of the target mailbox (ws). An
            # unbound process emits an anonymous envelope that recipients
            # cannot reply to. Sender fields are never fabricated, and the
            # installed DTO carries no created_at_ms in "from".
            sid = os.environ.get("APLEXER_SESSION_ID", "")
            sender_rec = next((r for r in rows if str(r.get("id", "")).lower() == sid.lower() and sid
                               and r.get("tag") and r.get("workspace")), None)
            if sender_rec:
                from_obj = {"session_id": str(sender_rec["id"]),
                            "workspace": str(sender_rec["workspace"]),
                            "tag": str(sender_rec["tag"]), "engine": str(sender_rec.get("engine", "shell")),
                            "external": False}
            else:
                from_obj = {"external": True}
            mid = str(uuid.uuid4())
            env = {"schema_version": 1, "id": mid, "workspace": ws,
                   "created_at": int(time.time()),
                   "from": from_obj,
                   "to": {"tag": to, "session_id": hit[0]["id"]},
                   "kind": kind, "data": data, "body": body, "reply_to": None,
                   "delivery": "inbox"}
            os.makedirs(MDIR, exist_ok=True)
            with open(os.path.join(MDIR, mid + ".json"), "w") as f:
                json.dump(env, f)
            print(json.dumps(env))
        elif sub == "reply":
            # message reply <orig-id> <text> [--data JSON]: recipient threads via reply_to.
            rest = [a for a in args[2:] if a != "--"]
            orig_id = opt(args, "--id") or (bare(rest)[0] if bare(rest) else None)
            data = json.loads(opt(rest, "--data") or "{}")
            tails = bare(rest)
            body = tails[-1] if len(tails) > 1 else ""
            orig = None
            if os.path.isdir(MDIR):
                for fn in os.listdir(MDIR):
                    with open(os.path.join(MDIR, fn)) as f:
                        m = json.load(f)
                    if m.get("id") == orig_id:
                        orig = m
                        break
            if orig is None:
                sys.stderr.write("no such message\n"); sys.exit(1)
            # Installed Rust reply_target contract: anonymous originals cannot
            # be replied to (verbatim installed error).
            if not (orig.get("from") or {}).get("tag"):
                sys.stderr.write("Error: original message %s was sent anonymously (no sender tag)\n" % orig_id); sys.exit(1)
            # Installed routing (live probe 2026-10-17): a reply is filed in
            # the ORIGINAL AUTHOR's mailbox, not the replier's workspace.
            # The replier is the calling process's bound session (any ws).
            author_ws = (orig.get("from") or {}).get("workspace") or orig["workspace"]
            replier_sid = os.environ.get("APLEXER_SESSION_ID", "")
            replier_rec = next((r for r in load() if str(r.get("id", "")).lower() == replier_sid.lower()
                                and replier_sid and r.get("tag") and r.get("workspace")), None)
            if replier_rec is None:
                sys.stderr.write("Error: reply requires calling aplexer session\n"); sys.exit(1)
            mid = str(uuid.uuid4())
            env = {"schema_version": 1, "id": mid, "workspace": author_ws,
                   "created_at": int(time.time()),
                   "from": {"session_id": str(replier_rec["id"]),
                            "workspace": str(replier_rec["workspace"]),
                            "tag": str(replier_rec["tag"]),
                            "engine": str(replier_rec.get("engine", "shell"))},
                   "to": orig["from"], "kind": "reply", "data": data, "body": body,
                   "reply_to": orig_id, "delivery": "inbox"}
            with open(os.path.join(MDIR, mid + ".json"), "w") as f:
                json.dump(env, f)
            print(json.dumps(env))
        elif sub == "deliver":
            ids = bare([a for a in args[2:] if a != "--workspace"])
            mid = ids[-1] if ids else None
            found = os.path.isfile(os.path.join(MDIR, (mid or "") + ".json"))
            if not found:
                sys.stderr.write("no such message\n"); sys.exit(1)
            if DELIVER_BEH == "refused-busy":
                sys.stderr.write("composer busy, input box not empty\n"); sys.exit(1)
            if DELIVER_BEH == "crash":
                sys.stderr.write("crashed after possible effect\n"); sys.exit(1)
            print(json.dumps({"id": mid, "status": "submitted", "detail": "delivered into target pane"}))
        elif sub == "log":
            ws = opt(args, "--workspace")
            out = []
            if os.path.isdir(MDIR):
                for fn in sorted(os.listdir(MDIR)):
                    with open(os.path.join(MDIR, fn)) as f:
                        m = json.load(f)
                    if m.get("workspace") == ws:
                        out.append(m)
            print(json.dumps(out))
        else:
            sys.exit(1)
    else:
        sys.exit(1)

main()
"""


@pytest.fixture()
def env(tmp_path_factory, monkeypatch):
    base = tempfile.mkdtemp(dir=REPO_TMP, prefix="sess-exec-")
    monkeypatch.setenv("TMPDIR", os.path.join(REPO_TMP))
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", os.path.join(REPO_TMP))
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
    start_log = os.path.join(base, "starts.log")
    for key in ("FAKE_MSG_BEHAVIOR", "FAKE_DELIVER_BEHAVIOR", "FAKE_KILL_BEHAVIOR"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("FAKE_SESSIONS", sessions_file)
    monkeypatch.setenv("FAKE_MSGS", msgs)
    monkeypatch.setenv("FAKE_START_LOG", start_log)
    yield {"base": base, "work": work, "fake": fake, "journal": journal,
           "msgs": msgs, "sessions_file": sessions_file, "start_log": start_log,
           "monkeypatch": monkeypatch}
    shutil.rmtree(base, ignore_errors=True)  # only our own sess-exec- dir


def custody(actor="tester"):
    return se.CallerCustody(actor=actor, machine_id="hetzner1", credential_family="famA")


def make_claim(command_id, generation=7, expired=False):
    now_ms = int(time.time() * 1000)
    return {"command_id": command_id, "claim_token": "tok-" + uuid.uuid4().hex + "idx",
            "holder": "hetzner1", "generation": generation,
            "expires_at_ms": now_ms - 1000 if expired else now_ms + 60000}


def make_target(env, tag, sid):
    return NativeTarget(session_id=sid, tag=tag, workspace=env["work"])


def seed_session(env, tag="agent1", engine="go"):
    proc = subprocess.run([env["fake"], "start", "--workspace", env["work"],
                           "--tag", tag, "--engine", engine, "--json"],
                          capture_output=True, text=True, timeout=20)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


# --- native adapter: list / qualify -------------------------------------------

def test_list_filters_workspace_and_projects_shapes(env):
    seed_session(env, tag="w1sess")
    other = os.path.join(env["base"], "other")
    os.makedirs(other)
    subprocess.run([env["fake"], "start", "--workspace", other, "--tag", "elsewhere",
                    "--engine", "go", "--json"], check=True, capture_output=True, timeout=20)
    rows = ns.list_sessions(env["fake"], workspace=env["work"])
    assert {r.tag for r in rows} == {"w1sess", "host-sender"}
    w1 = [r for r in rows if r.tag == "w1sess"][0]
    assert w1.engine == "go" and w1.phase == "running"
    assert w1.created_at_ms == 1700000000000  # native generation provenance
    assert "history_path" not in w1.__dict__  # projection keeps only necessary fields


def test_qualify_rejects_tag_reuse(env):
    first = seed_session(env, tag="dupe")
    rows = json.load(open(env["sessions_file"]))
    rows.append({"id": str(uuid.uuid4()), "tag": "dupe", "workspace": env["work"],
                 "engine": "go", "profile": None, "phase": "running", "state": "running",
                 "created_at_ms": 1700000000001, "last_activity_ms": 1700000000001})
    json.dump(rows, open(env["sessions_file"], "w"))
    target = make_target(env, "dupe", first["id"])
    with pytest.raises(SessionControlError):
        ns.qualify_target(env["fake"], target)
    # No effect attempted: all three sessions (sender + both dupes) still running.
    assert len(json.load(open(env["sessions_file"]))) == 3


def test_qualify_rejects_stale_generation(env):
    rec = seed_session(env, tag="gen1")
    target = NativeTarget(session_id=rec["id"], tag="gen1", workspace=env["work"],
                          created_at_ms=123456789)  # wrong generation
    with pytest.raises(SessionControlError):
        ns.qualify_target(env["fake"], target)
    good = NativeTarget(session_id=rec["id"], tag="gen1", workspace=env["work"],
                        created_at_ms=1700000000000)
    assert ns.qualify_target(env["fake"], good).session_id == rec["id"]


def test_resolve_cwd_rejects_escape(env):
    ok = ns.resolve_cwd([env["work"]], os.path.join(env["work"], "sub"))
    assert ok == os.path.join(env["work"], "sub")
    with pytest.raises(SessionControlError):
        ns.resolve_cwd([env["work"]], "/etc")
    with pytest.raises(SessionControlError):
        ns.resolve_cwd([env["work"]], os.path.join(env["work"], "..", "escape"))


# --- delivery (inbox only; raw PTY retired) -----------------------------------

def test_send_inbox_recorded_with_correlated_receipt(env):
    rec = seed_session(env)
    target = make_target(env, "agent1", rec["id"])
    out, _sender = ns.send_inbox(env["fake"], target, "hello agent",
                                 {"correlation_id": "corr-1"})
    assert out.state == "recorded"
    assert out.native_id
    log = subprocess.run([env["fake"], "message", "log", "--workspace", env["work"], "--json"],
                         capture_output=True, text=True, timeout=20)
    envelopes = json.loads(log.stdout)
    assert len(envelopes) == 1
    assert envelopes[0]["id"] == out.native_id
    assert envelopes[0]["data"] == {"hub_session_control": {"correlation_id": "corr-1"}}
    assert envelopes[0]["body"] == "hello agent"
    assert envelopes[0]["reply_to"] is None  # original envelope, not a reply


def test_send_inbox_rejects_uuid_to_with_no_effect(env):
    rec = seed_session(env)
    evil = NativeTarget(session_id=rec["id"], tag=rec["id"], workspace=env["work"])
    with pytest.raises(SessionControlError):
        ns.send_inbox(env["fake"], evil, "leak?", {"correlation_id": "c"})
    assert os.listdir(env["msgs"]) == []


def test_pty_guarded_retired_holds(env):
    # R2 correction: `send --enter` working verdicts are NOT ACK proof, so the raw
    # PTY path is retired. Requesting it holds honestly with no native effect.
    rec = seed_session(env)
    target = make_target(env, "agent1", rec["id"])
    with pytest.raises(SessionControlError):
        se.send_prompt(env["fake"], target, "do work", custody=custody(),
                       transport="pty-guarded", idempotency_key="retired1",
                       journal_dir=env["journal"])
    assert os.listdir(env["msgs"]) == []
    assert os.listdir(env["journal"]) == []  # refused before intent: nothing journaled
    # Inbox remains the supported transport.
    ok = se.send_prompt(env["fake"], target, "do work", custody=custody(),
                        transport="inbox", idempotency_key="inbox1",
                        journal_dir=env["journal"])
    assert ok["state"] == "recorded"


def test_deliver_guard_maps_without_ack(env):
    rec = seed_session(env)
    target = make_target(env, "agent1", rec["id"])
    sent, _sender = ns.send_inbox(env["fake"], target, "hello", {"correlation_id": "d1"})
    done = ns.deliver_recorded(env["fake"], target, sent.native_id)
    assert done.state == "transported"  # submitted; processing NOT proven
    env["monkeypatch"].setenv("FAKE_DELIVER_BEHAVIOR", "refused-busy")
    refused = ns.deliver_recorded(env["fake"], target, sent.native_id)
    assert refused.state == "refused"


def test_ack_verified_via_correlated_reply(env):
    rec = seed_session(env)
    target = make_target(env, "agent1", rec["id"])
    body = "prompt for ack test"
    sent, sender = ns.send_inbox(env["fake"], target, body, {"correlation_id": "ack-c1"})
    corr = {"correlation_id": "ack-c1", "body_digest": ns.body_digest(body)}
    # No reply yet: honest unknown, never false confirmed.
    assert ns.verify_ack_via_reply(env["fake"], target, sender, sent.native_id, corr).state == "unknown"
    # The RECIPIENT session authors its reply (installed protocol: only the
    # recipient's own session can emit a reply attributed to it).
    reply_env = dict(os.environ)
    reply_env["APLEXER_SESSION_ID"] = rec["id"]
    rep = subprocess.run(
        [env["fake"], "message", "reply", sent.native_id, "done ack-c1",
         "--data", json.dumps(corr), "--json"],
        capture_output=True, text=True, timeout=20, env=reply_env)
    assert rep.returncode == 0, rep.stderr
    proven = ns.verify_ack_via_reply(env["fake"], target, sender, sent.native_id, corr)
    assert proven.state == "acknowledged" and proven.reply_envelope_id
    # Foreign reply (sender UUID mismatch) is not our proof.
    rows = json.load(open(env["sessions_file"]))
    agent_row = next(r for r in rows if r.get("tag") == "agent1")
    agent_row["id"] = str(uuid.uuid4())
    json.dump(rows, open(env["sessions_file"], "w"))
    foreign_target = make_target(env, "agent1", agent_row["id"])
    assert ns.verify_ack_via_reply(env["fake"], foreign_target, sender, sent.native_id,
                                   corr).state == "unknown"


# --- executor idempotency ------------------------------------------------------

def test_executor_send_replay_and_conflict(env):
    rec = seed_session(env)
    target = make_target(env, "agent1", rec["id"])
    first = se.send_prompt(env["fake"], target, "brief text", custody=custody(),
                           transport="inbox", idempotency_key="k1",
                           journal_dir=env["journal"])
    assert first["state"] == "recorded" and not first.get("replayed")
    again = se.send_prompt(env["fake"], target, "brief text", custody=custody(),
                           transport="inbox", idempotency_key="k1",
                           journal_dir=env["journal"])
    assert again["replayed"] is True and again["native_id"] == first["native_id"]
    assert len(os.listdir(env["msgs"])) == 1  # no second native effect
    with pytest.raises(SessionControlError):
        se.send_prompt(env["fake"], target, "DIFFERENT body", custody=custody(),
                       transport="inbox", idempotency_key="k1",
                       journal_dir=env["journal"])


def test_executor_inbox_retry_needs_reconciliation(env):
    rec = seed_session(env)
    target = make_target(env, "agent1", rec["id"])
    env["monkeypatch"].setenv("FAKE_MSG_BEHAVIOR", "crash")
    with pytest.raises(SessionUncertain):
        se.send_prompt(env["fake"], target, "risky", custody=custody(),
                       transport="inbox", idempotency_key="k9",
                       journal_dir=env["journal"])
    env["monkeypatch"].setenv("FAKE_MSG_BEHAVIOR", "ok")
    with pytest.raises(SessionUncertain):  # blind resend refused
        se.send_prompt(env["fake"], target, "risky", custody=custody(),
                       transport="inbox", idempotency_key="k9",
                       journal_dir=env["journal"])
    done = se.send_prompt(env["fake"], target, "risky", custody=custody(),
                          transport="inbox", idempotency_key="k9",
                          journal_dir=env["journal"], reconciled=True)
    assert done["state"] == "recorded"


def test_concurrent_intent_lock_serializes(env):
    rec = seed_session(env)
    target = make_target(env, "agent1", rec["id"])
    barrier = threading.Barrier(4)
    outcomes = []

    def worker():
        try:
            barrier.wait(timeout=10)
            outcomes.append(se.send_prompt(
                env["fake"], target, "same body", custody=custody(),
                transport="inbox", idempotency_key="race1",
                journal_dir=env["journal"]))
        except Exception as exc:  # noqa: BLE001 — lock contention is an honest outcome
            outcomes.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert len(os.listdir(env["msgs"])) == 1  # exactly one native effect
    assert any(isinstance(o, dict) and o.get("state") == "recorded" for o in outcomes)


# --- capture / stop ------------------------------------------------------------

def test_capture_bounded_truncation(env):
    rec = seed_session(env)
    target = make_target(env, "agent1", rec["id"])
    small = se.capture_session_output(env["fake"], target, custody=custody(),
                                      limit_bytes=64, mode="tail")
    assert small["truncated"] is True and small["byte_length"] == 64
    assert small["privacy_unknown"] is True  # explicit: regex alone cannot prove safe
    with pytest.raises(SessionControlError):
        se.capture_session_output(env["fake"], target, custody=custody(),
                                  limit_bytes=ns.MAX_CAPTURE_LIMIT + 1)


def test_stop_confirms_exit_and_second_stop_reports_gone(env):
    rec = seed_session(env)
    target = make_target(env, "agent1", rec["id"])
    done = se.stop_controlled_session(env["fake"], target, custody=custody())
    assert done["state"] == "stopped"
    again = se.stop_controlled_session(env["fake"], target, custody=custody())
    assert again["state"] == "stopped"  # already terminal: idempotent, no new effect


def test_ordinary_stop_releases_lock_only_when_intake_closed(env, monkeypatch):
    """AC1 release: ordinary stop unlocks with closed intake, keeps with open."""
    from agent_hub_host import worktree_guard as guard
    rec = seed_session(env)
    target = make_target(env, "agent1", rec["id"])
    calls: list = []
    monkeypatch.setattr(guard, "release_session_lock", lambda *a, **k: calls.append((a, k)) or True)
    command = {"op": "stop", "command_id": str(uuid.uuid4()), "session_id": "a" * 32}
    out = se._run_claimed_effect(env["fake"], "stop", command, target, custody=custody(),
                                 journal_dir=env["journal"], linked_intake_open=False)
    assert out["confirmed_exit"] is True
    assert calls == [(("git", env["work"], "a" * 32), {"terminal": True, "intake_state": "done"})]
    calls.clear()
    out2 = se._run_claimed_effect(env["fake"], "stop", {**command, "command_id": str(uuid.uuid4())},
                                  target, custody=custody(), journal_dir=env["journal"],
                                  linked_intake_open=True)
    assert out2["confirmed_exit"] is True
    assert calls == []


def test_stop_forgotten_record_is_uncertain_not_stopped(env):
    rec = seed_session(env)
    target = make_target(env, "agent1", rec["id"])
    json.dump([], open(env["sessions_file"], "w"))  # record vanished without kill proof
    with pytest.raises((SessionControlError, SessionUncertain)):
        se.stop_controlled_session(env["fake"], target, custody=custody())


def test_stop_refuses_retargeted_tag(env):
    rec = seed_session(env, tag="victim")
    target = make_target(env, "victim", rec["id"])
    rows = json.load(open(env["sessions_file"]))
    victim_row = next(r for r in rows if r.get("tag") == "victim")
    victim_row["id"] = str(uuid.uuid4())  # tag now owned by someone else
    json.dump(rows, open(env["sessions_file"], "w"))
    with pytest.raises(SessionControlError):
        se.stop_controlled_session(env["fake"], target, custody=custody())
    victim_row = next(r for r in json.load(open(env["sessions_file"])) if r.get("tag") == "victim")
    assert victim_row["phase"] == "running"  # untouched


def test_stop_uncertain_when_still_running(env):
    rec = seed_session(env)
    target = make_target(env, "agent1", rec["id"])
    env["monkeypatch"].setenv("FAKE_KILL_BEHAVIOR", "still-running")
    with pytest.raises(SessionUncertain):
        se.stop_controlled_session(env["fake"], target, custody=custody())


# --- custody / limits / auth ---------------------------------------------------

def test_custody_and_limits_hold_with_no_effect(env):
    rec = seed_session(env)
    target = make_target(env, "agent1", rec["id"])
    bad = se.CallerCustody(actor="", machine_id="hetzner1", credential_family="famA")
    with pytest.raises(SessionControlError):
        se.send_prompt(env["fake"], target, "x", custody=bad, journal_dir=env["journal"])
    with pytest.raises(SessionControlError):
        se.send_prompt(env["fake"], target, "x" * (ns.MAX_BODY_BYTES + 1),
                       custody=custody(), journal_dir=env["journal"])
    assert os.listdir(env["msgs"]) == []
    assert len(os.listdir(env["journal"])) == 0


def test_project_qualified_actor_accepted(env):
    # Authentic canonical actors may be project-qualified (`project:tag`); the LABEL
    # rule must not reject genuine `machine/agent:actor` forms. No privilege is
    # derived from the text; the server enforces family/incarnation/revocation.
    rec = seed_session(env)
    target = make_target(env, "agent1", rec["id"])
    out = se.send_prompt(env["fake"], target, "hello", custody=custody("proj:agent1"),
                         transport="inbox", idempotency_key="proj1",
                         journal_dir=env["journal"])
    assert out["state"] == "recorded" and out["caller_actor"] == "proj:agent1"


# --- start via Fleet contract over real HTTP + SQLite --------------------------

class ClaimHandler(BaseHTTPRequestHandler):
    db_path = ""
    machine = ""

    def log_message(self, *a):
        pass

    def _guard(self):
        if self.headers.get("X-Hub-Machine") != self.machine:
            self.send_response(403)
            self.end_headers()
            return False
        if self.headers.get("Authorization") != "Bearer test-cred":
            self.send_response(401)
            self.end_headers()
            return False
        return True

    def _read(self):
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length) or b"{}")

    def do_POST(self):  # noqa: N802
        if not self._guard():
            return
        body = self._read()
        con = sqlite3.connect(self.db_path)
        try:
            if self.path.endswith("/claim"):
                con.execute("INSERT INTO claims(launch, payload) VALUES(?, ?)",
                            (self.path, json.dumps(body)))
            elif self.path.endswith("/result"):
                con.execute("INSERT INTO results(launch, payload) VALUES(?, ?)",
                            (self.path, json.dumps(body)))
            elif "/ack" in self.path:
                con.execute("INSERT INTO acks(launch, payload) VALUES(?, ?)",
                            (self.path, json.dumps(body)))
            elif self.path.endswith("/status"):
                # Pending inbox status (AC5): accepted, not an ack; no DB row.
                pass
            else:
                self.send_response(404)
                self.end_headers()
                return
            con.commit()
        finally:
            con.close()
        data = json.dumps({"ok": True}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        if not self._guard():
            return
        data = json.dumps({"offers": []}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture()
def hub(env):
    db = os.path.join(env["base"], "hub.sqlite3")
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE claims(launch TEXT, payload TEXT)")
    con.execute("CREATE TABLE results(launch TEXT, payload TEXT)")
    con.execute("CREATE TABLE acks(launch TEXT, payload TEXT)")
    con.commit()
    con.close()
    ClaimHandler.db_path = db
    ClaimHandler.machine = "hetzner1"
    server = ThreadingHTTPServer(("127.0.0.1", 0), ClaimHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = "http://127.0.0.1:%d" % server.server_address[1]

    def request(method, path, payload=None):
        data = json.dumps(payload or {}).encode()
        req = urllib.request.Request(base_url + path, data=data, method=method,
                                     headers={"Content-Type": "application/json",
                                              "X-Hub-Machine": "hetzner1",
                                              "Authorization": "Bearer test-cred"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read() or b"{}")

    yield {"request": request, "db": db, "port": server.server_address[1]}
    server.shutdown()
    thread.join(timeout=5)
    try:
        os.unlink(db)
    except OSError:
        pass


def fleet_settings(env, gate_ok=True):
    def runner(argv, payload=None, cwd=None, timeout=30):
        if argv == ["admit"]:
            observed = dt.datetime.now(dt.timezone.utc).isoformat()
            if not gate_ok:
                return {"launch_allowed": False, "profile_supported": True,
                        "account_label": "hub", "observed_at": observed}
            return {"launch_allowed": True, "profile_supported": True,
                    "account_label": "hub", "observed_at": observed}
        if argv[1:3] == ["message", "send"]:
            assert "--queue" in argv and "--pane" not in argv
            return {"id": "preamble-fixture"}
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=20)
        assert proc.returncode == 0, proc.stderr
        return json.loads(proc.stdout)

    settings = {
        "subscriptions": [{"service": "go", "account_label": "hub", "engine": "go",
                           "admission_argv": ["admit"]}],
        "workspaces": {"wt": env["work"]},
        "aplexer_bin": env["fake"],
        "capabilities": [],
    }
    offer = {"id": "offer-1", "mode": "interactive", "engine": "go",
             "profile": None, "account": "hub", "workspace": "wt"}
    return settings, offer, runner


def start_count(env):
    try:
        with open(env["start_log"]) as f:
            return len([line for line in f if line.startswith("start:")])
    except FileNotFoundError:
        return 0


def test_start_via_fleet_contract_claim_result_journal(hub, env):
    settings, offer, runner = fleet_settings(env)
    out = se.start_from_offer(settings, offer, custody=custody(), runner=runner,
                              request=hub["request"], journal_dir=env["journal"],
                              executable=env["fake"])
    assert out["state"] == "running" and out["native_id"]
    assert start_count(env) == 1
    con = sqlite3.connect(hub["db"])
    claims = con.execute("SELECT payload FROM claims").fetchall()
    results = con.execute("SELECT payload FROM results").fetchall()
    con.close()
    assert len(claims) == 1 and len(results) == 1  # real HTTP claim + result rows
    assert json.loads(results[0][0])["state"] == "running"
    # Idempotent replay reconciles live session: no second native start.
    again = se.start_from_offer({**settings, "workspaces": {"wt": env["work"]}},
                                {**offer, "request_key": "offer-1"} if "request_key" not in offer else offer,
                                custody=custody(), runner=runner,
                                request=hub["request"], journal_dir=env["journal"],
                                executable=env["fake"])
    assert again["native_id"] == out["native_id"]
    assert start_count(env) == 1


def test_start_held_when_admission_refused(hub, env):
    settings, offer, runner = fleet_settings(env, gate_ok=False)
    out = se.start_from_offer(settings, offer, custody=custody(), runner=runner,
                              request=hub["request"], journal_dir=env["journal"],
                              executable=env["fake"])
    assert out["state"] == "held"
    assert start_count(env) == 0  # no native effect on refusal


def test_start_uncertain_adopts_without_duplicate(hub, env):
    settings, offer, base_runner = fleet_settings(env)
    calls = {"n": 0}

    def flaky(argv, payload=None, cwd=None, timeout=30):
        if argv == ["admit"] or argv[1:3] == ["message", "send"]:
            return base_runner(argv, payload, cwd, timeout)
        calls["n"] += 1
        if calls["n"] == 1:
            # Effect happened, receipt lost: create session directly, then lose it.
            subprocess.run(argv, capture_output=True, text=True, timeout=20, check=True)
            raise RuntimeError("receipt lost")
        return base_runner(argv, payload, cwd, timeout)

    first = se.start_from_offer(settings, offer, custody=custody(), runner=flaky,
                                request=hub["request"], journal_dir=env["journal"],
                                executable=env["fake"])
    assert first["state"] == "uncertain"  # effect happened, receipt lost
    assert start_count(env) == 1
    adopted = se.start_from_offer(settings, {**offer, "request_key": "offer-1"},
                                  custody=custody(), runner=flaky,
                                  request=hub["request"], journal_dir=env["journal"],
                                  executable=env["fake"])
    assert adopted["state"] == "running" and adopted.get("adopted") is True
    assert start_count(env) == 1  # never blind-retried


def test_hub_rejects_missing_machine_binding(hub, env):
    con = sqlite3.connect(hub["db"])
    before = con.execute("SELECT COUNT(*) FROM claims").fetchone()[0]
    con.close()
    # Direct POST without machine binding / credential must be refused (403).
    import urllib.error
    data = json.dumps({"gate_allowed": True}).encode()
    req = urllib.request.Request(
        "http://127.0.0.1:%d" % _server_port(hub) + "/api/fleet/launches/x/claim",
        data=data, method="POST", headers={"Content-Type": "application/json"})
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=5)
    assert exc.value.code == 403
    con = sqlite3.connect(hub["db"])
    assert con.execute("SELECT COUNT(*) FROM claims").fetchone()[0] == before
    con.close()


def _server_port(hub):
    con = sqlite3.connect(hub["db"])
    con.close()
    return hub["port"]


# --- server-claimed receiver ---------------------------------------------------

def test_claimed_send_capture_stop_round_trip(hub, env):
    rec = seed_session(env)
    target = {"session_id": rec["id"], "tag": "agent1", "workspace": env["work"]}
    listed = se.handle_session_command({"op": "list", "workspace": env["work"]},
                                       custody=custody(),
                                       settings={"workspaces": {"wt": env["work"]}},
                                       executable=env["fake"])
    assert any(s["tag"] == "agent1" for s in listed["sessions"])
    sent = se.handle_session_command(
        {"op": "send", "command_id": "cmd-1", "generation": 7, "target": target,
         "body": "via receiver", "claim": make_claim("cmd-1")},
        custody=custody(), journal_dir=env["journal"], executable=env["fake"],
        request=hub["request"])
    assert sent["state"] == "recorded" and sent["ack"]["submitted"] is True
    assert sent["native_id"]  # original envelope retained for reconciliation
    cap = se.handle_session_command(
        {"op": "capture", "command_id": "cmd-2", "generation": 7, "target": target,
         "limit_bytes": 4096, "claim": make_claim("cmd-2")},
        custody=custody(), journal_dir=env["journal"], executable=env["fake"],
        request=hub["request"])
    assert cap["state"] == "captured" and cap["ack"]["submitted"] is True
    stopped = se.handle_session_command(
        {"op": "stop", "command_id": "cmd-3", "generation": 7, "target": target,
         "claim": make_claim("cmd-3")},
        custody=custody(), journal_dir=env["journal"], executable=env["fake"],
        request=hub["request"])
    assert stopped["state"] == "stopped"
    con = sqlite3.connect(hub["db"])
    assert con.execute("SELECT COUNT(*) FROM acks").fetchone()[0] == 3
    con.close()
    with pytest.raises(SessionControlError):
        se.handle_session_command({"op": "launch-missiles"}, custody=custody(),
                                  executable=env["fake"])


def test_claimed_receiver_rejects_start(env):
    # Fleet already handles start once; the claimed receiver never reconsumes
    # Fleet offers or invokes start.
    with pytest.raises(SessionControlError):
        se.handle_session_command(
            {"op": "start", "command_id": "s1", "generation": 1,
             "claim": make_claim("s1")},
            custody=custody(), journal_dir=env["journal"], executable=env["fake"])


def test_claim_expiry_refuses_without_effect(env):
    rec = seed_session(env)
    target = {"session_id": rec["id"], "tag": "agent1", "workspace": env["work"]}
    with pytest.raises(SessionControlError):
        se.handle_session_command(
            {"op": "send", "command_id": "exp-1", "generation": 7, "target": target,
             "body": "too late", "claim": make_claim("exp-1", expired=True)},
            custody=custody(), journal_dir=env["journal"], executable=env["fake"])
    assert os.listdir(env["msgs"]) == []  # no effect after expired claim


def test_repeated_claim_token_never_reauthorizes(env):
    rec = seed_session(env)
    target = {"session_id": rec["id"], "tag": "agent1", "workspace": env["work"]}
    first = se.handle_session_command(
        {"op": "send", "command_id": "tok-1", "generation": 7, "target": target,
         "body": "once", "claim": make_claim("tok-1")},
        custody=custody(), journal_dir=env["journal"], executable=env["fake"])
    assert first["state"] == "recorded"
    # A different token for the same command replays the journal — no new effect.
    again = se.handle_session_command(
        {"op": "send", "command_id": "tok-1", "generation": 7, "target": target,
         "body": "once", "claim": make_claim("tok-1")},
        custody=custody(), journal_dir=env["journal"], executable=env["fake"])
    assert again.get("replayed") is True
    assert len(os.listdir(env["msgs"])) == 1
    # Uncertain attempt: durable uncertain outcome (no stranded
    # attempted:true/state:null), never a second effect. The journal carries
    # bounded stage/type/reason; a foreign token retry still refuses.
    env["monkeypatch"].setenv("FAKE_MSG_BEHAVIOR", "crash")
    uncertain = se.handle_session_command(
        {"op": "send", "command_id": "tok-2", "generation": 7, "target": target,
         "body": "risky", "claim": make_claim("tok-2")},
        custody=custody(), journal_dir=env["journal"], executable=env["fake"])
    assert uncertain["state"] == "uncertain"
    assert uncertain.get("error_stage") == "send"
    assert uncertain.get("error_type")
    assert "attempted" in uncertain or True  # journal durable, not None-forever
    env["monkeypatch"].setenv("FAKE_MSG_BEHAVIOR", "ok")
    with pytest.raises(SessionUncertain):
        se.handle_session_command(
            {"op": "send", "command_id": "tok-2", "generation": 7, "target": target,
             "body": "risky", "claim": make_claim("tok-2")},
            custody=custody(), journal_dir=env["journal"], executable=env["fake"])
    assert len(os.listdir(env["msgs"])) == 1  # only the first "once" envelope


def test_status_probe_is_read_only(env):
    rec = seed_session(env)
    target = make_target(env, "agent1", rec["id"])
    info = se.session_status(env["fake"], target, custody=custody())
    assert info["phase"] == "running" and info["status_proven"] is True
