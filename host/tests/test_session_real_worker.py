"""Real-Worker composed session loop (ACTUAL Worker/SQLite server boundary).

Spins the ACTUAL hub Worker (same harness pattern as
host/tests/test_http_cli.py TestHttpCliReal: real src/worker bundle via
wrangler dry-run + miniflare + /__fixture__/seed_sql) with fleet placement
seeded (machine + quota snapshot + fleet account/route), then drives the FULL
loop with the PUBLIC Python CLI + REAL receiver + fake native fixture:

  CLI sessions start -> host binding POST -> CLI sessions send ->
  receiver.receive_once (claim -> native inbox send -> auto-reply proof ->
  ack delivered) -> CLI sessions capture -> receive_once ->
  CLI sessions stop -> receive_once -> CLI show stopped.

The fake `a` emulates ONLY the verified native contract
(list/message send/log/reply/capture/status/kill, argv only) plus a LABELED
fixture auto-reply on send (instant correlated reply for source evidence;
production targets reply asynchronously). No --enter/--pane/--from, no live
sessions, no production credentials, no extra services.

This is the server-agreement proof: claim (holder/generation/both expiries),
offers (generation + native_created_at_ms), and ack (native target UUID +
separate envelope proof) are validated by the REAL server, not a Python mock.
Stub-hub unit cases live in test_session_control_loop.py and are labeled as
such; a green mock alone never proves the contract.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from http.server import BaseHTTPRequestHandler  # noqa: F401 (harness parity)

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from real_worker_harness import (
    RealWorkerHarness,
    WorkerProcessHandle,
    WorkerStartupError,
    build_fixture_worker_ts,
    build_miniflare_script,
    build_wrangler_config,
    find_available_port,
    get_available_cpus,
    resolve_node_binary,
)

REPO_TMP = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".local", "tmp"))

# Fake `a`: verified native surface only, + LABELED fixture auto-reply.
# On `message send` the fixture instantly writes a correlated reply envelope
# from the TARGET (reply_to = new envelope id, sender = target session/tag/
# workspace, data echoes correlation_id + body_digest). This stands in for an
# async production target reply so the receiver's immediate proof check can
# observe delivery in a bounded test; it is fixture behavior, not a contract
# claim about production timing.
REAL_SESSION_FAKE_A = r"""#!/usr/bin/env python3
import json, os, sys, time, uuid
HERE = os.path.dirname(os.path.abspath(__file__))
SFILE = os.path.join(HERE, "sessions.json")
MDIR = os.path.join(HERE, "msgs")
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
        ws = opt(args, "--workspace"); tag = opt(args, "--tag"); eng = opt(args, "--engine") or "shell"
        rows = load()
        rec = {"id": str(uuid.uuid4()), "tag": tag, "workspace": ws, "engine": eng,
               "profile": None, "phase": "running", "state": "running",
               # Real native DTO fidelity: containment_empty is the
               # stop-oracle proof field (see executor FAKE_SCRIPT note).
               "containment_empty": False,
               "created_at_ms": 1700000000000, "last_activity_ms": 1700000000000}
        rows.append(rec); save(rows)
        print(json.dumps({"id": rec["id"], "workspace": ws, "tag": tag}))
    elif cmd == "capture":
        ws = opt(args, "--workspace"); tag = opt(args, "--tag")
        rows = load()
        if not any(r["tag"] == tag and r["workspace"] == ws for r in rows):
            sys.stderr.write("no session\n"); sys.exit(1)
        sys.stdout.write("tail line one\ntail line two\n")
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
            # LABELED fixture auto-reply: instant correlated target reply.
            inner = data.get("hub_session_control", {}) if isinstance(data, dict) else {}
            cid = inner.get("correlation_id", "")
            digest = inner.get("body_digest", "")
            rid = str(uuid.uuid4())
            reply = {"schema_version": 1, "id": rid, "workspace": ws,
                     "created_at": int(time.time()),
                     "from": {"session_id": hit[0]["id"], "workspace": ws,
                              "tag": to, "engine": "shell"},
                     "to": env["from"], "kind": "reply",
                     "data": {"correlation_id": cid, "body_digest": digest},
                     "body": "ack %s" % cid, "reply_to": mid, "delivery": "inbox"}
            with open(os.path.join(MDIR, rid + ".json"), "w") as f: json.dump(reply, f)
            print(json.dumps(env))
        elif sub == "log":
            ws = opt(args, "--workspace")
            out = []
            if os.path.isdir(MDIR):
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


def sha256_b64url(val: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(val.encode()).digest()).decode().rstrip("=")


class TestSessionRealWorker(unittest.TestCase):
    @classmethod
    def find_unused_port(cls):
        return find_available_port()

    @classmethod
    def get_available_cpus(cls, count=2) -> str:
        return get_available_cpus(count)

    @classmethod
    def _cleanup_proc(cls):
        handle = getattr(cls, "handle", None)
        proc = getattr(cls, "proc", None)
        cleanup_exc = None
        try:
            if handle is not None:
                try:
                    handle.cleanup()
                except Exception as e:
                    cleanup_exc = e
            elif proc is not None:
                if proc.poll() is None:
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                        proc.wait(timeout=3)
                    except Exception:
                        try:
                            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                            proc.wait(timeout=2)
                        except Exception:
                            pass
        finally:
            if proc is not None:
                for stream in (proc.stdout, proc.stderr, proc.stdin):
                    if stream:
                        try:
                            stream.close()
                        except Exception:
                            pass
            cls.handle = None
            cls.proc = None

        if cleanup_exc is not None and sys.exc_info()[0] is None:
            raise cleanup_exc

    @classmethod
    def _cleanup_server_dir(cls):
        server_dir = getattr(cls, "server_dir", None)
        if server_dir and os.path.isdir(server_dir):
            try:
                shutil.rmtree(server_dir, ignore_errors=True)
            except Exception:
                pass
        cls.server_dir = None

    @classmethod
    def setUpClass(cls):
        cls.repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
        if not os.path.exists(os.path.join(cls.repo_root, "src", "bus.ts")):
            raise unittest.SkipTest("Real Worker source pending")
        cls.tmp_root = os.path.join(cls.repo_root, ".local", "tmp")
        os.makedirs(cls.tmp_root, exist_ok=True)
        cls.server_dir = tempfile.mkdtemp(dir=cls.tmp_root, prefix="sess-real-")
        cls.addClassCleanup(cls._cleanup_server_dir)
        cls.handle = None
        cls.proc = None
        cls.addClassCleanup(cls._cleanup_proc)
        try:
            fixture_worker_ts = build_fixture_worker_ts(cls.repo_root)
            with open(os.path.join(cls.server_dir, "fixture_worker.ts"), "w") as f:
                f.write(fixture_worker_ts)

            cfg = build_wrangler_config(port=8787, main_ts="fixture_worker.ts")
            with open(os.path.join(cls.server_dir, "wrangler.jsonc"), "w") as f:
                json.dump(cfg, f)

            node_bin = resolve_node_binary()
            node_dir = str(Path(node_bin).parent)
            env = os.environ.copy()
            current_path = env.get("PATH", "")
            if node_dir not in current_path.split(os.pathsep):
                env["PATH"] = f"{node_dir}:{current_path}"
            env["TOKIO_WORKER_THREADS"] = "1"
            env["RAYON_NUM_THREADS"] = "1"
            env["UV_THREADPOOL_SIZE"] = "1"
            env["NODE_OPTIONS"] = "--max-old-space-size=384"
            env["MINIFLARE_WORKERD_V8_FLAGS"] = "--single-threaded --single-threaded-gc"
            env["TMPDIR"] = str(cls.tmp_root)
            cpu_str = get_available_cpus(2)

            cls.harness = RealWorkerHarness(
                repo_root=cls.repo_root,
                server_dir=cls.server_dir,
                tmp_root=cls.tmp_root,
                readiness_timeout=35.0,
                poll_interval=0.2,
                http_timeout=1.0,
                max_startup_attempts=2,
            )

            build_dir = Path(cls.server_dir) / "bundle"
            bundle_path = cls.harness.build_worker(
                config_path=os.path.join(cls.server_dir, "wrangler.jsonc"),
                out_dir=str(build_dir),
                env=env,
                cpu_str=cpu_str,
                node_bin=node_bin,
            )

            def launch_worker(port: int, log_name: str) -> WorkerProcessHandle:
                mf_js = os.path.join(cls.server_dir, f"miniflare-{port}.cjs")
                script = build_miniflare_script(cls.repo_root, cls.server_dir, str(bundle_path), port)
                with open(mf_js, "w") as f:
                    f.write(script)
                cmd = ["taskset", "-c", cpu_str, node_bin, str(mf_js)]
                return cls.harness.launch_command(cmd, port=port, env=env, log_name=log_name)

            cls.handle = cls.harness.start_with_retry(launch_worker)
            cls.proc = cls.handle.proc
            cls.port = cls.handle.port

            cls.init_fixture_data()
            # Native fixture dir (fake-a + journal), per-test tmp under server dir.
            cls.native_dir = os.path.join(cls.server_dir, "native")
            os.makedirs(cls.native_dir, exist_ok=True)
            # Genuine host sender session for the fixture workspace (F2): the
            # receiver's sends resolve to this bound identity (via
            # APLEXER_SESSION_ID below), never an anonymous envelope.
            cls.sender_id = str(uuid.uuid4())
            with open(os.path.join(cls.native_dir, "sessions.json"), "w") as f:
                json.dump([{"id": cls.sender_id, "tag": "host-sender",
                            "workspace": cls.native_dir, "engine": "shell",
                            "profile": None, "phase": "running", "state": "running",
                            "created_at_ms": 1700000000000,
                            "last_activity_ms": 1700000000000}], f)
            os.makedirs(os.path.join(cls.native_dir, "msgs"), exist_ok=True)
            fake = os.path.join(cls.native_dir, "fake-a")
            with open(fake, "w") as f:
                f.write(REAL_SESSION_FAKE_A)
            os.chmod(fake, 0o755)
            cls.fake_bin = fake
            cls.journal_dir = os.path.join(cls.native_dir, "journal")
            os.makedirs(cls.journal_dir, mode=0o700, exist_ok=True)
        except Exception:
            cls._cleanup_proc()
            cls._cleanup_server_dir()
            raise

    @classmethod
    def seed_sql(cls, sql: str, params=None):
        req = urllib.request.Request(
            f"http://127.0.0.1:{cls.port}/__fixture__/seed_sql",
            data=json.dumps({"sql": sql, "params": params or []}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            if not data.get("ok"):
                raise RuntimeError(f"seed_sql failed: {data.get('error')}")
            return data.get("rows", [])

    @classmethod
    def init_fixture_data(cls):
        # Fleet placement enforces FRESHNESS (checkin 180s, quota 1200s), so
        # seed with the CURRENT clock, not a fixed fixture date.
        import datetime as _dt
        _now = _dt.datetime.now(_dt.timezone.utc)
        now = _now.isoformat().replace("+00:00", "Z")
        future = (_now + _dt.timedelta(days=30)).isoformat().replace("+00:00", "Z")
        now_ms = int(_now.timestamp() * 1000)
        caps = json.dumps({"mem_avail_mb": 32000, "disk_free_gb": 80, "load": [1],
                           "gate": {"launch_allowed": True, "at": now},
                           "engines": ["codex"], "fleet_modes": ["headless"],
                           "workspaces": ["demo"], "cpu_count": 8})
        hb = json.dumps({"mem_avail_mb": 32000, "disk_free_gb": 80, "load": [1],
                         "gate": {"launch_allowed": True, "at": now}})
        cls.seed_sql("INSERT INTO machines (id, name, hostname, registered_at, last_checkin_at, last_heartbeat, drained, capabilities) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                     ["hetzner", "hetzner", "hetzner", now, now, hb, 0, caps])
        routes = json.dumps({"codex": {"status": "ok", "observed_at": now,
                                       "details": {"limit_reached": False},
                                       "windows": {"weekly": {"present": True, "percent_remaining": 80, "reset_at": future}}}})
        cls.seed_sql("INSERT INTO host_quota_snapshots (host_id, schema_version, captured_at, captured_at_ms, received_at, received_at_ms, status, error, routes_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     ["hetzner", "1.0", now, now_ms, now, now_ms, "ok", None, routes])
        cls.seed_sql("INSERT INTO fleet_accounts (label, provider, max_concurrency, reserve_percent, enabled) VALUES (?, ?, ?, ?, ?)",
                     ["sess-codex", "codex", 4, 15, 1])
        cls.seed_sql("INSERT INTO fleet_routes (machine_id, engine, account, profile) VALUES (?, ?, ?, ?)",
                     ["hetzner", "codex", "sess-codex", None])
        # Registry actor for the agent credential (credentialActor requires a
        # matching agents row bound to the credential machine).
        cls.seed_sql("INSERT INTO agents (tag, project, role, machine_id) VALUES (?, ?, ?, ?)",
                     ["assistant", "demo", "worker", "hetzner"])
        creds = {
            "sess-agent-token": ("agent", "hetzner", "demo:assistant", ["fleet:launch", "bus:send"]),
            "sess-host-token": ("host", "hetzner", None, ["host"]),
        }
        for token, (kind, mach, actor, scopes) in creds.items():
            cls.seed_sql(
                "INSERT INTO credentials (id, kind, token_hash, family_id, subject, machine_id, actor, scopes, created_by_sub, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [f"cred_{token}", kind, sha256_b64url(token), f"fam_{token}",
                 actor or mach, mach, actor, json.dumps(scopes), "test", now, future],
            )
        cls.seed_sql("UPDATE machines SET credential_id = ? WHERE id = ?", ["cred_sess-host-token", "hetzner"])

    def run_cli(self, args, token, check=True):
        cmd = [sys.executable, "-m", "agent_hub_host.cli"] + args
        env = os.environ.copy()
        env["HUB_URL"] = f"http://127.0.0.1:{self.port}"
        env["HUB_JOB_TOKEN"] = token
        env["PYTHONPATH"] = os.path.join(self.repo_root, "host")
        env["XDG_CONFIG_HOME"] = os.path.join(self.server_dir, ".config")
        res = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=60)
        if check and res.returncode != 0:
            raise RuntimeError(f"CLI failed rc={res.returncode} stdout={res.stdout!r} stderr={res.stderr!r}")
        return res

    def hub_post(self, path, payload, token):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + token},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            try:
                body = json.loads(exc.read().decode())
            except Exception:
                body = {"error": "http_%d" % exc.code}
            return exc.code, body

    def receive_bounded(self, custody, request_mod):
        """One bounded receiver cycle as the bound fixture sender (F2)."""
        from agent_hub_host.session_executor import CallerCustody  # noqa: F401
        from agent_hub_host import session_receiver as rc
        old_url, old_tok = os.environ.get("HUB_URL"), os.environ.get("HUB_JOB_TOKEN")
        old_sid = os.environ.get("APLEXER_SESSION_ID")
        os.environ["HUB_URL"] = f"http://127.0.0.1:{self.port}"
        os.environ["HUB_JOB_TOKEN"] = "sess-host-token"
        os.environ["APLEXER_SESSION_ID"] = self.sender_id
        try:
            return rc.receive_once(custody=custody, journal_dir=self.journal_dir,
                                   executable=self.fake_bin, request=request_mod,
                                   limit=20)
        finally:
            if old_url is None:
                os.environ.pop("HUB_URL", None)
            else:
                os.environ["HUB_URL"] = old_url
            if old_tok is None:
                os.environ.pop("HUB_JOB_TOKEN", None)
            else:
                os.environ["HUB_JOB_TOKEN"] = old_tok
            if old_sid is None:
                os.environ.pop("APLEXER_SESSION_ID", None)
            else:
                os.environ["APLEXER_SESSION_ID"] = old_sid

    def test_real_worker_session_loop_via_cli_and_receiver(self):
        # 1. Public CLI start (real fleet placement, no mock).
        brief_file = os.path.join(self.native_dir, "goal.txt")
        with open(brief_file, "w") as f:
            f.write("real loop goal")
        start = self.run_cli(["sessions", "start", "--workspace", "demo",
                              "--engine", "codex", "--reason", "codex required for real loop test",
                              "--machine", "hetzner",
                              "--brief-file", brief_file,
                              "--key", "real-loop-1"], token="sess-agent-token")
        created = json.loads(start.stdout)
        self.assertEqual(created["state"], "offered")
        self.assertTrue(created.get("fleet_launch_id"))
        sid = created["id"]
        launch_id = created["fleet_launch_id"]
        # 2. Native target + host binding POST (real /binding route). The
        # native tag is the canonical Fleet consumer tag for the assigned
        # launch, and the launch is advanced to the modeled consumer report
        # (starting->running with its native receipt) that F5 reconciles.
        native = subprocess.run([self.fake_bin, "start", "--workspace", self.native_dir,
                                 "--tag", "hub-" + launch_id, "--engine", "codex", "--json"],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(native.returncode, 0, native.stderr)
        target = json.loads(native.stdout)
        self.seed_sql("UPDATE fleet_launches SET state = 'running', native_id = ? WHERE id = ?",
                      [target["id"], launch_id])
        # The existing Fleet sessions report carries the canonical native
        # identity over the REAL route (machine + UUID + tag + workspace),
        # which F5 binding reconciles before accepting the tag/workspace.
        import datetime as _dt
        _now = _dt.datetime.now(_dt.timezone.utc).isoformat().replace("+00:00", "Z")
        sessions_put = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/fleet/sessions",
            data=json.dumps({"sessions": [{
                "native_id": target["id"], "tag": "hub-" + launch_id,
                "engine": "codex", "phase": "running", "agent_state": None,
                "workspace": self.native_dir, "profile": None,
                "account": "sess-codex", "last_activity_at": _now}]}).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer sess-host-token"},
            method="PUT")
        with urllib.request.urlopen(sessions_put, timeout=10) as resp:
            self.assertEqual(resp.status, 200)
        code, bound = self.hub_post(f"/api/sessions/{sid}/binding",
                                    {"native": {"native_id": target["id"], "tag": "hub-" + launch_id,
                                                "workspace": self.native_dir,
                                                "native_created_at_ms": 1700000000000}},
                                    "sess-host-token")
        self.assertEqual(code, 200, bound)
        self.assertEqual(bound["state"], "running")
        # 3. CLI send, then REAL receiver (claim -> native send with fixture
        # auto-reply -> proof -> ack delivered) against the real server.
        body_file = os.path.join(self.native_dir, "prompt.txt")
        with open(body_file, "w") as f:
            f.write("hello real target")
        sent = self.run_cli(["sessions", "send", sid, "--body-file", body_file, "--key", "m-real-1"],
                            token="sess-agent-token")
        self.assertTrue(json.loads(sent.stdout).get("command_id"))
        from agent_hub_host.session_executor import CallerCustody
        from agent_hub_host import session_control as sc
        custody = CallerCustody(actor="machine:hetzner", machine_id="hetzner",
                                credential_family="host-hetzner").validated()
        result = self.receive_bounded(custody, sc.request)
        self.assertGreaterEqual(result["offers_seen"], 1)
        processed = {r["command_id"]: r for r in result["processed"]}
        self.assertTrue(processed, result)
        # 4. Server agrees: the send command is acked with target-UUID proof.
        cmds = json.loads(self.run_cli(["sessions", "commands", sid],
                                       token="sess-agent-token").stdout)
        send_rows = [c for c in cmds["commands"] if c["kind"] == "send"]
        self.assertTrue(send_rows)
        self.assertEqual(send_rows[0]["state"], "acked")
        ack = json.loads(send_rows[0]["ack_json"]) if isinstance(send_rows[0].get("ack_json"), str) else send_rows[0].get("ack")
        self.assertEqual(ack["native"]["native_id"], target["id"])
        self.assertIn("proof", ack)
        self.assertEqual(ack["proof"]["sender_native_id"], target["id"])
        # 5. Capture + stop complete the loop through the same path.
        from agent_hub_host import session_control as sc2
        self.run_cli(["sessions", "capture", sid, "--no-wait", "--max-bytes", "512", "--key", "c-real-1"],
                     token="sess-agent-token")
        self.receive_bounded(custody, sc2.request)
        self.run_cli(["sessions", "stop", sid, "--key", "t-real-1"], token="sess-agent-token")
        self.receive_bounded(custody, sc2.request)
        view = json.loads(self.run_cli(["sessions", "show", sid], token="sess-agent-token").stdout)
        self.assertEqual(view["state"], "stopped")


if __name__ == "__main__":
    unittest.main()
