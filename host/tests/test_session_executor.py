"""M12e session executor tests: real stdlib subprocess + real HTTP + real SQLite.

No live sessions are touched. All native effects go to a per-test fake ``a``
binary (argv-shaped like the genuine CLI: ``list/start/send/capture/status/kill``
plus ``message send/log``). Claim/result reporting goes through a real local HTTP
server (stdlib ``http.server`` in a thread) backed by real SQLite, asserting host
machine binding + credential on every request. Journals live on the real
filesystem under this worktree's ``.local/tmp`` (never ``/tmp``).

Covers: positive/negative/retry/host-identity/auth/limits/correlated receipts,
idempotency conflicts, tag-reuse refusal, UUID-vs-TAG confusion, workspace escape,
busy refusal without forced Enter, bounded capture, confirmed stop, uncertain
adoption without duplicate starts, and held unqualified starts.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
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
import json, os, sys, uuid
SFILE = os.environ["FAKE_SESSIONS"]
MDIR = os.environ["FAKE_MSGS"]
SEND_BEH = os.environ.get("FAKE_SEND_BEHAVIOR", "ok")
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

def main():
    args = [a for a in sys.argv[1:] if a != "--json"]
    if not args:
        sys.exit(1)
    cmd = args[0]
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
               "profile": prof, "phase": "running", "created_at_ms": 1700000000000}
        rows.append(rec); save(rows)
        print(json.dumps({"id": rec["id"], "workspace": ws, "tag": tag, "engine": eng,
                          "profile": prof, "phase": "running"}))
    elif cmd == "send":
        ws = opt(args, "--workspace"); tag = opt(args, "--tag")
        rows = load()
        hit = [r for r in rows if r["tag"] == tag and r["workspace"] == ws]
        if not hit:
            sys.stderr.write("no session\n"); sys.exit(1)
        if SEND_BEH == "refused-busy":
            sys.stderr.write("composer busy, input box not empty\n"); sys.exit(1)
        if SEND_BEH == "crash":
            sys.stderr.write("crashed after possible effect\n"); sys.exit(1)
        if SEND_BEH == "idle3":
            print(json.dumps({"delivery": {"ready": True, "working": False}})); sys.exit(3)
        print(json.dumps({"delivery": {"ready": True, "working": True}}))
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
                          "id": hit[0]["id"]}))
    elif cmd == "kill":
        ws = opt(args, "--workspace"); tag = opt(args, "--tag")
        rows = load()
        hit = [r for r in rows if r["tag"] == tag and r["workspace"] == ws]
        if not hit:
            sys.stderr.write("no session\n"); sys.exit(1)
        if KILL_BEH != "still-running":
            hit[0]["phase"] = "exited"; save(rows)
        print(json.dumps({"killed": hit[0]["id"]}))
    elif cmd == "message":
        sub = args[1]
        if sub == "send":
            rest = [a for a in args[2:] if a != "--"]
            ws = opt(rest, "--workspace"); to = opt(rest, "--to")
            kind = opt(rest, "--kind") or "note"
            data = json.loads(opt(rest, "--data") or "{}")
            body = rest[-1] if rest and not rest[-1].startswith("-") else ""
            try:
                uuid.UUID(str(to))
                sys.stderr.write("refusing UUID --to\n"); sys.exit(1)
            except ValueError:
                pass
            rows = load()
            hit = [r for r in rows if r["tag"] == to and r["workspace"] == ws]
            if not hit:
                sys.stderr.write("no session tagged %s exists\n" % to); sys.exit(1)
            mid = str(uuid.uuid4())
            env = {"id": mid, "workspace": ws, "to": {"tag": to, "session_id": hit[0]["id"]},
                   "kind": kind, "data": data, "body": body, "reply_to": None}
            os.makedirs(MDIR, exist_ok=True)
            with open(os.path.join(MDIR, mid + ".json"), "w") as f:
                json.dump(env, f)
            print(json.dumps(env))
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
    work = os.path.join(base, "work")
    os.makedirs(work)
    sessions_file = os.path.join(base, "sessions.json")
    with open(sessions_file, "w") as f:
        json.dump([], f)
    msgs = os.path.join(base, "msgs")
    os.makedirs(msgs)
    fake = os.path.join(base, "fake-a")
    with open(fake, "w") as f:
        f.write(FAKE_SCRIPT)
    os.chmod(fake, 0o755)
    journal = os.path.join(base, "journal")
    os.makedirs(journal, mode=0o700)
    start_log = os.path.join(base, "starts.log")
    for key in ("FAKE_SEND_BEHAVIOR", "FAKE_KILL_BEHAVIOR"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("FAKE_SESSIONS", sessions_file)
    monkeypatch.setenv("FAKE_MSGS", msgs)
    monkeypatch.setenv("FAKE_START_LOG", start_log)
    return {"base": base, "work": work, "fake": fake, "journal": journal,
            "msgs": msgs, "sessions_file": sessions_file, "start_log": start_log,
            "monkeypatch": monkeypatch}


def custody():
    return se.CallerCustody(actor="tester", machine_id="hetzner1", credential_family="famA")


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
    assert len(rows) == 1 and rows[0].tag == "w1sess"
    assert rows[0].engine == "go" and rows[0].phase == "running"
    assert "history_path" not in rows[0].__dict__  # projection keeps only necessary fields


def test_qualify_rejects_tag_reuse(env):
    first = seed_session(env, tag="dupe")
    rows = json.load(open(env["sessions_file"]))
    rows.append({"id": str(uuid.uuid4()), "tag": "dupe", "workspace": env["work"],
                 "engine": "go", "profile": None, "phase": "running",
                 "created_at_ms": 1700000000001})
    json.dump(rows, open(env["sessions_file"], "w"))
    target = make_target(env, "dupe", first["id"])
    with pytest.raises(SessionControlError):
        ns.qualify_target(env["fake"], target)
    # No effect attempted: both sessions still running.
    assert len(json.load(open(env["sessions_file"]))) == 2


def test_resolve_cwd_rejects_escape(env):
    ok = ns.resolve_cwd([env["work"]], os.path.join(env["work"], "sub"))
    assert ok == os.path.join(env["work"], "sub")
    with pytest.raises(SessionControlError):
        ns.resolve_cwd([env["work"]], "/etc")
    with pytest.raises(SessionControlError):
        ns.resolve_cwd([env["work"]], os.path.join(env["work"], "..", "escape"))


# --- delivery -----------------------------------------------------------------

def test_send_inbox_recorded_with_correlated_receipt(env):
    rec = seed_session(env)
    target = make_target(env, "agent1", rec["id"])
    out = ns.send_inbox(env["fake"], target, "hello agent",
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


def test_send_inbox_rejects_uuid_to_with_no_effect(env):
    rec = seed_session(env)
    evil = NativeTarget(session_id=rec["id"], tag=rec["id"], workspace=env["work"])
    with pytest.raises(SessionControlError):
        ns.send_inbox(env["fake"], evil, "leak?", {"correlation_id": "c"})
    assert os.listdir(env["msgs"]) == []


def test_send_pty_acknowledged_vs_idle_is_transported(env):
    rec = seed_session(env)
    target = make_target(env, "agent1", rec["id"])
    ok = ns.send_prompt_guarded(env["fake"], target, "do work", {"correlation_id": "k1"})
    assert ok.state == "acknowledged"
    env["monkeypatch"].setenv("FAKE_SEND_BEHAVIOR", "idle3")
    idle = ns.send_prompt_guarded(env["fake"], target, "do more", {"correlation_id": "k2"})
    assert idle.state == "transported"  # delivered-but-idle is NEVER acknowledged


def test_send_pty_busy_refused_without_forced_enter(env):
    rec = seed_session(env)
    target = make_target(env, "agent1", rec["id"])
    env["monkeypatch"].setenv("FAKE_SEND_BEHAVIOR", "refused-busy")
    out = ns.send_prompt_guarded(env["fake"], target, "interrupt?", {"correlation_id": "k"})
    assert out.state == "refused"


def test_send_pty_crash_is_uncertain(env):
    rec = seed_session(env)
    target = make_target(env, "agent1", rec["id"])
    env["monkeypatch"].setenv("FAKE_SEND_BEHAVIOR", "crash")
    with pytest.raises(SessionUncertain):
        ns.send_prompt_guarded(env["fake"], target, "risky", {"correlation_id": "k"})


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


def test_executor_pty_retry_needs_reconciliation(env):
    rec = seed_session(env)
    target = make_target(env, "agent1", rec["id"])
    env["monkeypatch"].setenv("FAKE_SEND_BEHAVIOR", "crash")
    with pytest.raises(SessionUncertain):
        se.send_prompt(env["fake"], target, "risky", custody=custody(),
                       transport="pty-guarded", idempotency_key="k9",
                       journal_dir=env["journal"])
    env["monkeypatch"].setenv("FAKE_SEND_BEHAVIOR", "ok")
    with pytest.raises(SessionUncertain):  # blind resend refused
        se.send_prompt(env["fake"], target, "risky", custody=custody(),
                       transport="pty-guarded", idempotency_key="k9",
                       journal_dir=env["journal"])
    done = se.send_prompt(env["fake"], target, "risky", custody=custody(),
                          transport="pty-guarded", idempotency_key="k9",
                          journal_dir=env["journal"], reconciled=True)
    assert done["state"] == "acknowledged"


# --- capture / stop ------------------------------------------------------------

def test_capture_bounded_truncation(env):
    rec = seed_session(env)
    target = make_target(env, "agent1", rec["id"])
    small = se.capture_session_output(env["fake"], target, custody=custody(),
                                      limit_bytes=64, mode="tail")
    assert small["truncated"] is True and small["byte_length"] == 64
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
    rows[0]["id"] = str(uuid.uuid4())  # tag now owned by someone else
    json.dump(rows, open(env["sessions_file"], "w"))
    with pytest.raises(SessionControlError):
        se.stop_controlled_session(env["fake"], target, custody=custody())
    assert json.load(open(env["sessions_file"]))[0]["phase"] == "running"  # untouched


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


def fleet_settings(env, gate_ok=True):
    def runner(argv, payload=None, cwd=None, timeout=30):
        if argv == ["admit"]:
            observed = dt.datetime.now(dt.timezone.utc).isoformat()
            if not gate_ok:
                return {"launch_allowed": False, "profile_supported": True,
                        "account_label": "hub", "observed_at": observed}
            return {"launch_allowed": True, "profile_supported": True,
                    "account_label": "hub", "observed_at": observed}
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
        if argv == ["admit"]:
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


# --- receiver command function -------------------------------------------------

def test_handle_session_command_round_trip(hub, env):
    rec = seed_session(env)
    target = {"session_id": rec["id"], "tag": "agent1", "workspace": env["work"]}
    listed = se.handle_session_command({"op": "list", "workspace": env["work"]},
                                       custody=custody(),
                                       settings={"workspaces": {"wt": env["work"]}},
                                       executable=env["fake"])
    assert listed["sessions"] and listed["sessions"][0]["tag"] == "agent1"
    sent = se.handle_session_command(
        {"op": "send", "target": target, "body": "via receiver",
         "transport": "inbox", "idempotency_key": "rx1"},
        custody=custody(), journal_dir=env["journal"], executable=env["fake"])
    assert sent["state"] == "recorded"
    cap = se.handle_session_command(
        {"op": "capture", "target": target, "limit_bytes": 4096},
        custody=custody(), executable=env["fake"])
    assert cap["byte_length"] > 0
    stopped = se.handle_session_command({"op": "stop", "target": target},
                                        custody=custody(), executable=env["fake"])
    assert stopped["state"] == "stopped"
    with pytest.raises(SessionControlError):
        se.handle_session_command({"op": "launch-missiles"}, custody=custody(),
                                  executable=env["fake"])


def test_status_probe_is_read_only(env):
    rec = seed_session(env)
    target = make_target(env, "agent1", rec["id"])
    info = se.session_status(env["fake"], target, custody=custody())
    assert info["phase"] == "running" and info["status_proven"] is True
