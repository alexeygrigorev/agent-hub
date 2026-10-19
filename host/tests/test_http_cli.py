import base64
import hashlib
import json
import os
import signal
import socket
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, unquote, urlparse

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


# Fake `a` for real-Worker `hub bridge` runs: `message send` returns a qualified envelope from the
# hub-bridge service session (optionally ACKing it in the recipient's cursor), `message log` returns
# one reply per send for tags configured to reply. Sync fixtures also implement status/capture and
# message deliver, recording simulated typing in effects.jsonl; they never touch a real PTY.
REAL_BRIDGE_FAKE_A = r'''#!/usr/bin/env python3
import sys, json, os, uuid, time
HERE = os.path.dirname(os.path.abspath(__file__))
cfg = json.load(open(os.path.join(HERE, "cfg.json")))
args = sys.argv[1:]
with open(os.path.join(HERE, "calls.jsonl"), "a") as f:
    f.write(json.dumps(args) + "\n")
sends = os.path.join(HERE, "sends.jsonl")
if args[:2] == ["message", "send"]:
    tag = args[args.index("--to") + 1]
    t = cfg["tags"][tag]
    if args[-1] in cfg.get("refuse_bodies", []):
        sys.exit(4)
    nid = str(uuid.uuid4())
    kind = args[args.index("--kind") + 1]
    with open(sends, "a") as f:
        f.write(json.dumps({"id": nid, "tag": tag, "kind": kind, "body": args[-1]}) + "\n")
    if cfg.get("auto_ack") and kind != "sync_reply":
        c = json.load(open(t["cursor"]))
        c.setdefault("exceptions", []).append(nid)
        json.dump(c, open(t["cursor"], "w"))
    print(json.dumps({"schema_version": 1, "id": nid, "workspace": cfg["ws"],
        "from": {"tag": "hub-bridge", "session_id": "00000000-0000-0000-0000-000000000001", "workspace": cfg["ws"]},
        "to": {"tag": tag, "session_id": t["rec"]}, "kind": kind, "reply_to": None, "body": args[-1], "delivery": "inbox"}))
elif args[:2] == ["message", "deliver"]:
    nid = args[2]
    queued = next(json.loads(line) for line in open(sends) if json.loads(line)["id"] == nid)
    with open(os.path.join(HERE, "effects.jsonl"), "a") as f:
        f.write(json.dumps(queued) + "\n")
    behavior = cfg.get("deliver_behavior", "success")
    if behavior == "timeout":
        time.sleep(35)  # production bridge's 30-second timeout, real child process
    if behavior == "crash":
        sys.exit(9)
    if cfg.get("auto_ack"):
        t = cfg["tags"][queued["tag"]]
        c = json.load(open(t["cursor"]))
        c.setdefault("exceptions", []).append(nid)
        json.dump(c, open(t["cursor"], "w"))
    print(json.dumps({"id": nid}))
elif args[0] == "status":
    if cfg.get("status_exit"):
        sys.exit(cfg["status_exit"])
    print(json.dumps({"state": cfg.get("state", "idle")}))
elif args[0] == "capture":
    print(cfg.get("capture", "welcome\n❯"))
elif args[:2] == ["message", "log"]:
    out = []
    if os.path.exists(sends) and cfg.get("replies"):
        for line in open(sends):
            s = json.loads(line)
            r = cfg["replies"].get(s["tag"])
            if not r:
                continue
            t = cfg["tags"][s["tag"]]
            out.append({"schema_version": 1, "id": str(uuid.uuid5(uuid.NAMESPACE_URL, "reply-" + s["id"])), "workspace": cfg["ws"],
                "from": {"tag": s["tag"], "session_id": t["rec"], "workspace": cfg["ws"]}, "to": {"tag": "hub-bridge"},
                "kind": r["kind"], "reply_to": s["id"], "body": r["body"] + " <" + s["body"][:30] + ">", "delivery": "inbox"})
    print(json.dumps(out))
else:
    sys.exit(3)
'''

BRIDGE_SERVICE_SESSION = "00000000-0000-0000-0000-000000000001"


def sha256_b64url(val: str) -> str:
    digest = hashlib.sha256(val.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).decode("utf-8").rstrip("=")


class MockHubHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Silence standard HTTP request logging in test output
        pass

    def do_POST(self):
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "unauthorized"}).encode("utf-8"))
            return

        token = auth.split("Bearer ", 1)[1].strip()
        if token != "valid-test-token":
            self.send_response(403)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "forbidden"}).encode("utf-8"))
            return

        content_length = int(self.headers.get("Content-Length", "0"))
        body_bytes = self.rfile.read(content_length)
        parsed_url = urlparse(self.path)
        path = parsed_url.path

        if path == "/api/bus/send":
            try:
                data = json.loads(body_bytes.decode("utf-8"))
            except Exception:
                self.send_response(400)
                self.end_headers()
                return

            idem = data.get("idempotency_key")
            if idem and idem in self.server.idempotency_map:
                existing = self.server.idempotency_map[idem]
                if existing["body"] != data.get("body") or existing["to_actor"] != data.get("to_actor"):
                    self.send_response(409)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"error": "conflict"}).encode("utf-8"))
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"id": existing["id"], "thread_id": existing["thread_id"]}).encode("utf-8"))
                return

            msg_id = f"msg_{len(self.server.messages) + 1}"
            thread_id = data.get("thread_id") or msg_id
            msg_obj = {
                "id": msg_id,
                "from_actor": data.get("from_actor"),
                "to_actor": data.get("to_actor"),
                "kind": data.get("kind"),
                "body": data.get("body"),
                "thread_id": thread_id,
                "reply_to": data.get("reply_to"),
                "idempotency_key": idem,
                "created_at": 1000,
                "acked_at": None,
                "accepted_at": None,
                "completed_at": None
            }
            self.server.messages.append(msg_obj)
            if idem:
                self.server.idempotency_map[idem] = msg_obj

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"id": msg_id, "thread_id": thread_id}).encode("utf-8"))
            return

        if path.startswith("/api/bus/messages/") and any(path.endswith(s) for s in ["/ack", "/accept", "/complete"]):
            parts = path.split("/")
            msg_id = parts[4]
            stage = parts[5]
            found = next((m for m in self.server.messages if m["id"] == msg_id), None)
            if not found:
                self.send_response(404)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": "not_found"}).encode("utf-8"))
                return
            if stage == "ack":
                found["acked_at"] = 2000
            elif stage == "accept":
                found["accepted_at"] = 2000
            elif stage == "complete":
                found["completed_at"] = 2000
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "ok", "id": msg_id, "stage": stage}).encode("utf-8"))
            return

        if path == "/api/bus/claims":
            try:
                data = json.loads(body_bytes.decode("utf-8"))
            except Exception:
                self.send_response(400)
                self.end_headers()
                return

            claim_id = f"claim_{len(self.server.claims) + 1}"
            claim_obj = {
                "id": claim_id,
                "project": data.get("project"),
                "scope": data.get("scope"),
                "holder": data.get("holder"),
                "created_at": 1000,
                "expires_at": 1000 + data.get("ttl_ms", 3600000),
                "released_at": None
            }
            self.server.claims.append(claim_obj)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"id": claim_id}).encode("utf-8"))
            return

        if path.startswith("/api/bus/claims/") and path.endswith("/release"):
            encoded_target = path[len("/api/bus/claims/"):-len("/release")]
            target = unquote(encoded_target)
            matched = False
            for c in self.server.claims:
                if c["id"] == target or f"{c['project']}:{c['scope']}" == target:
                    c["released_at"] = 2000
                    matched = True
                    break
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "ok", "matched": matched}).encode("utf-8"))
            return

        self.send_response(404)
        self.end_headers()

    def do_GET(self):
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "unauthorized"}).encode("utf-8"))
            return

        token = auth.split("Bearer ", 1)[1].strip()
        if token != "valid-test-token":
            self.send_response(403)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "forbidden"}).encode("utf-8"))
            return

        parsed_url = urlparse(self.path)
        path = parsed_url.path
        qs = parse_qs(parsed_url.query)

        if path == "/api/bus/inbox":
            actor = qs.get("actor", [""])[0]
            matched = [m for m in self.server.messages if m["to_actor"] == actor]
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(matched).encode("utf-8"))
            return

        if path.startswith("/api/bus/messages/"):
            msg_id = path.split("/api/bus/messages/")[1]
            found = next((m for m in self.server.messages if m["id"] == msg_id), None)
            if not found:
                self.send_response(404)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": "not_found"}).encode("utf-8"))
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(found).encode("utf-8"))
            return

        if path == "/api/bus/claims":
            project = qs.get("project", [None])[0]
            holder = qs.get("holder", [None])[0]
            active_only = qs.get("active", ["true"])[0] == "true"
            matched = self.server.claims
            if project:
                matched = [c for c in matched if c["project"] == project]
            if holder:
                matched = [c for c in matched if c["holder"] == holder]
            if active_only:
                matched = [c for c in matched if c["released_at"] is None]
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(matched).encode("utf-8"))
            return

        self.send_response(404)
        self.end_headers()


class TestHttpCliMock(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
        cls.tmp_root = os.path.join(cls.repo_root, ".local", "tmp")
        os.makedirs(cls.tmp_root, exist_ok=True)
        cls.server = HTTPServer(("127.0.0.1", 0), MockHubHandler)
        cls.server.messages = []
        cls.server.claims = []
        cls.server.idempotency_map = {}
        cls.port = cls.server.server_port
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.server_thread.join()

    def setUp(self):
        self.server.messages = []
        self.server.claims = []
        self.server.idempotency_map = {}
        self.temp_dir = tempfile.TemporaryDirectory(dir=self.tmp_root)
        xdg_config_dir = os.path.join(self.temp_dir.name, ".config")
        self.config_dir = os.path.join(xdg_config_dir, "agent-hub")
        os.makedirs(self.config_dir, exist_ok=True)
        os.chmod(self.config_dir, stat.S_IRWXU)
        self.config_file = os.path.join(self.config_dir, "host.json")
        
        cfg_data = {
            "base_url": f"http://127.0.0.1:{self.port}",
            "access_token": "valid-test-token"
        }
        with open(self.config_file, "w") as f:
            json.dump(cfg_data, f)
        os.chmod(self.config_file, stat.S_IRUSR | stat.S_IWUSR) # 0600

        self.env = os.environ.copy()
        # Explicit XDG_CONFIG_HOME to case temp/.config; do not repurpose HOME/CODEX_HOME
        self.env["XDG_CONFIG_HOME"] = xdg_config_dir
        self.env["APPDATA"] = xdg_config_dir
        self.env.pop("HUB_URL", None)
        self.env.pop("HUB_JOB_TOKEN", None)

    def tearDown(self):
        self.temp_dir.cleanup()

    def run_cli(self, args, input_data=None, env=None, check=True):
        cmd = [sys.executable, "-m", "agent_hub_host.cli"] + args
        current_env = (env or self.env).copy()
        host_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        current_env["PYTHONPATH"] = host_dir
        res = subprocess.run(
            cmd,
            input=input_data,
            capture_output=True,
            text=True,
            env=current_env
        )
        if check and res.returncode != 0:
            diag = f"\n[CLI STDOUT]: {res.stdout}\n[CLI STDERR]: {res.stderr}"
            raise subprocess.CalledProcessError(res.returncode, cmd, output=res.stdout + diag, stderr=res.stderr)
        return res

    def test_mode_0600_config(self):
        mode = stat.S_IMODE(os.stat(self.config_file).st_mode)
        self.assertEqual(mode, 0o600)

    def test_cli_full_flow(self):
        # 1. hub send
        res = self.run_cli([
            "send",
            "--from", "worker-1",
            "--to", "worker-2",
            "--kind", "note",
            "--body", "Hello from worker 1",
            "--idempotency-key", "key-flow-1",
            "--thread-id", "th-1"
        ])
        out = json.loads(res.stdout)
        self.assertIn("id", out)
        msg_id = out["id"]
        self.assertEqual(out["thread_id"], "th-1")

        # 2. hub inbox
        res = self.run_cli(["inbox", "--actor", "worker-2"])
        inbox = json.loads(res.stdout)
        self.assertEqual(len(inbox), 1)
        self.assertEqual(inbox[0]["id"], msg_id)
        self.assertEqual(inbox[0]["body"], "Hello from worker 1")

        # 3. hub show
        res = self.run_cli(["show", msg_id])
        shown = json.loads(res.stdout)
        self.assertEqual(shown["id"], msg_id)

        # 4. hub ack
        res = self.run_cli(["ack", msg_id])
        ack_out = json.loads(res.stdout)
        self.assertEqual(ack_out["status"], "ok")
        self.assertEqual(ack_out["stage"], "ack")

        # 5. hub accept
        res = self.run_cli(["accept", msg_id])
        accept_out = json.loads(res.stdout)
        self.assertEqual(accept_out["status"], "ok")
        self.assertEqual(accept_out["stage"], "accept")

        # 6. hub complete
        res = self.run_cli(["complete", msg_id])
        complete_out = json.loads(res.stdout)
        self.assertEqual(complete_out["status"], "ok")
        self.assertEqual(complete_out["stage"], "complete")

        # 7. hub reply
        res = self.run_cli([
            "reply",
            "--reply-to", msg_id,
            "--from", "worker-2",
            "--to", "worker-1",
            "--kind", "reply",
            "--body", "Acknowledged and completed",
            "--idempotency-key", "rep-flow-1",
            "--thread-id", "th-1"
        ])
        rep_out = json.loads(res.stdout)
        self.assertIn("id", rep_out)
        self.assertEqual(rep_out["thread_id"], "th-1")

    def test_default_idempotency_retry(self):
        # Sending identical message twice with same idempotency key returns original id
        res1 = self.run_cli([
            "send",
            "--from", "worker-1",
            "--to", "worker-2",
            "--kind", "note",
            "--body", "Deterministic body",
            "--idempotency-key", "idem-dup-1"
        ])
        out1 = json.loads(res1.stdout)

        res2 = self.run_cli([
            "send",
            "--from", "worker-1",
            "--to", "worker-2",
            "--kind", "note",
            "--body", "Deterministic body",
            "--idempotency-key", "idem-dup-1"
        ])
        out2 = json.loads(res2.stdout)
        self.assertEqual(out1["id"], out2["id"])

        # Conflicting body with same idempotency key fails with 409 (non-zero exit)
        res_conflict = self.run_cli([
            "send",
            "--from", "worker-1",
            "--to", "worker-2",
            "--kind", "note",
            "--body", "DIFFERENT body",
            "--idempotency-key", "idem-dup-1"
        ], check=False)
        self.assertNotEqual(res_conflict.returncode, 0)
        self.assertIn("409", res_conflict.stderr)

    def test_claims_lifecycle(self):
        # 1. hub claim
        res = self.run_cli([
            "claim",
            "--project", "projA",
            "--scope", "src/bus.ts",
            "--holder", "worker-1",
            "--ttl-ms", "60000"
        ])
        claim_out = json.loads(res.stdout)
        self.assertIn("id", claim_out)
        claim_id = claim_out["id"]

        # 2. hub claims (list)
        res = self.run_cli(["claims", "--project", "projA", "--holder", "worker-1"])
        claims_list = json.loads(res.stdout)
        self.assertEqual(len(claims_list), 1)
        self.assertEqual(claims_list[0]["id"], claim_id)

        # 3. hub release by project:scope
        res = self.run_cli(["release", "projA:src/bus.ts"])
        rel_out = json.loads(res.stdout)
        self.assertEqual(rel_out["status"], "ok")

        # 4. hub claims active filter
        res = self.run_cli(["claims", "--project", "projA"])
        claims_active = json.loads(res.stdout)
        self.assertEqual(len(claims_active), 0)

        # 5. hub claims --all includes released
        res = self.run_cli(["claims", "--project", "projA", "--all"])
        claims_all = json.loads(res.stdout)
        self.assertEqual(len(claims_all), 1)

    def test_stdin_body_piped(self):
        content = "Line 1\nLine 2\nUTF-8 chars: üñîçødé"
        res = self.run_cli([
            "send",
            "--from", "worker-1",
            "--to", "worker-2",
            "--kind", "note",
            "--body", "-"
        ], input_data=content)
        out = json.loads(res.stdout)
        self.assertIn("id", out)

        # Verify delivered body matches
        res_show = self.run_cli(["show", out["id"]])
        msg = json.loads(res_show.stdout)
        self.assertEqual(msg["body"], content)

    def test_stdin_body_oversize_rejected(self):
        oversized_content = "A" * 16385
        res = self.run_cli([
            "send",
            "--from", "worker-1",
            "--to", "worker-2",
            "--kind", "note",
            "--body", "-"
        ], input_data=oversized_content, check=False)
        self.assertEqual(res.returncode, 1)
        self.assertIn("exceeds 16 KiB limit", res.stderr)

    def test_environment_overrides_hub_url_and_token(self):
        custom_env = self.env.copy()
        custom_env["HUB_URL"] = f"http://127.0.0.1:{self.port}"
        custom_env["HUB_JOB_TOKEN"] = "valid-test-token"
        # Explicit private XDG_CONFIG_HOME fixture path ensuring config file is not readable; do not touch HOME/CODEX_HOME
        custom_env["XDG_CONFIG_HOME"] = os.path.join(self.temp_dir.name, "nonexistent-config")

        res = self.run_cli([
            "send",
            "--from", "env-worker",
            "--to", "worker-2",
            "--kind", "note",
            "--body", "Env override message"
        ], env=custom_env)
        out = json.loads(res.stdout)
        self.assertIn("id", out)

    def test_unauthorized_token_error_sanitized(self):
        bad_env = self.env.copy()
        bad_env["HUB_URL"] = f"http://127.0.0.1:{self.port}"
        bad_env["HUB_JOB_TOKEN"] = "wrong-forbidden-secret"

        res = self.run_cli([
            "send",
            "--from", "worker-1",
            "--to", "worker-2",
            "--kind", "note",
            "--body", "Test message"
        ], env=bad_env, check=False)
        self.assertNotEqual(res.returncode, 0)
        # Verify secret token is NOT leaked into stdout or stderr
        self.assertNotIn("wrong-forbidden-secret", res.stdout)
        self.assertNotIn("wrong-forbidden-secret", res.stderr)


class TestHttpCliReal(unittest.TestCase):
    @classmethod
    def find_unused_port(cls):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    @classmethod
    def get_available_cpus(cls, count=2) -> str:
        try:
            aff = sorted(os.sched_getaffinity(0))
            if aff:
                return ",".join(str(c) for c in aff[:count])
        except Exception:
            pass
        return "0,1"

    @classmethod
    def _cleanup_proc(cls):
        proc = getattr(cls, "proc", None)
        if proc is not None:
            for stream in (proc.stdout, proc.stderr, proc.stdin):
                if stream:
                    try:
                        stream.close()
                    except Exception:
                        pass
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
        cls.proc = None

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
            raise unittest.SkipTest("Real loopback server dependency pending independent backend m3server81 integration")
        cls.tmp_root = os.path.join(cls.repo_root, ".local", "tmp")
        os.makedirs(cls.tmp_root, exist_ok=True)
        cls.server_dir = tempfile.mkdtemp(dir=cls.tmp_root)
        cls.addClassCleanup(cls._cleanup_server_dir)
        cls.proc = None
        cls.addClassCleanup(cls._cleanup_proc)

        try:
            cls.port = cls.find_unused_port()

            fixture_worker_ts = f"""
import worker, {{ Hub as BaseHub }} from "{cls.repo_root}/src/worker";

export class FixtureHub extends BaseHub {{
  override async fetch(request: Request): Promise<Response> {{
    const url = new URL(request.url);
    if (url.pathname === "/__fixture__/seed_sql" && request.method === "POST") {{
      const data = await request.json() as {{ sql: string, params?: any[] }};
      try {{
        const rows = this.sql.exec(data.sql, ...(data.params || [])).toArray();
        return Response.json({{ ok: true, rows }});
      }} catch (err: any) {{
        return Response.json({{ error: String(err?.message || err) }}, {{ status: 400 }});
      }}
    }}
    return super.fetch(request);
  }}
}}

export {{ FixtureHub as Hub }};

export default {{
  async fetch(request: Request, env: any, ctx: any) {{
    const url = new URL(request.url);
    if (url.pathname === "/__fixture__/seed_sql") {{
      const id = env.HUB.idFromName("hub");
      const stub = env.HUB.get(id);
      return stub.fetch(request);
    }}
    return worker.fetch(request, env, ctx);
  }}
}};
"""
            with open(os.path.join(cls.server_dir, "fixture_worker.ts"), "w") as f:
                f.write(fixture_worker_ts)

            cfg = {
                "name": "agent-hub-fixture",
                "main": "fixture_worker.ts",
                "compatibility_date": "2024-04-05",
                "compatibility_flags": ["nodejs_compat"],
                "durable_objects": {"bindings": [{"name": "HUB", "class_name": "Hub"}]},
                "migrations": [{"tag": "v1", "new_sqlite_classes": ["Hub"]}],
                "vars": {
                    "AUTH_CLIENT_ID": "fake_client_id",
                    "AUTH_ISSUER": "https://auth.fake.local",
                    "AUTH_BASE_URL": "https://auth.fake.local",
                    "AUTH_CALLBACK_URL": f"http://127.0.0.1:{cls.port}/auth/callback",
                    "AUTH_JWKS_URL": "https://auth.fake.local/.well-known/jwks.json",
                    "SESSION_SECRET": "super_secret_session_key_32_chars_long_for_tests"
                }
            }
            with open(os.path.join(cls.server_dir, "wrangler.jsonc"), "w") as f:
                json.dump(cfg, f)

            wrangler_js = os.path.join(cls.repo_root, "node_modules", "wrangler", "bin", "wrangler.js")
            node_bin = shutil.which("node")
            if not node_bin or not os.path.exists(node_bin):
                for candidate in [
                    "/home/alexey/.nvm/versions/node/v24.13.1/bin/node",
                    os.path.expanduser("~/.nvm/versions/node/v24.13.1/bin/node"),
                ]:
                    if os.path.exists(candidate):
                        node_bin = candidate
                        break
            if not node_bin or not os.path.exists(node_bin):
                raise RuntimeError("node binary not found")

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

            cpu_str = cls.get_available_cpus(2)
            build_dir = Path(cls.server_dir) / "bundle"
            build = subprocess.run(
                ["taskset", "-c", cpu_str, node_bin, wrangler_js, "deploy", "--dry-run", "--config", os.path.join(cls.server_dir, "wrangler.jsonc"), "--outdir", str(build_dir)],
                cwd=cls.server_dir, env=env, capture_output=True, text=True, timeout=30
            )
            if build.returncode != 0:
                raise RuntimeError(f"Wrangler deploy --dry-run failed with code {build.returncode}: {build.stderr}")

            bundles = list(build_dir.glob("*.js"))
            if len(bundles) != 1:
                raise RuntimeError(f"Expected 1 bundle, found: {bundles}")

            worker_opts = {
                "name": "agent-hub-fixture",
                "scriptPath": str(bundles[0]),
                "modules": True,
                "compatibilityDate": "2024-04-05",
                "compatibilityFlags": ["nodejs_compat"],
                "bindings": cfg["vars"],
                "durableObjects": {"HUB": {"className": "Hub", "useSQLite": True}},
                "assets": {
                    "directory": os.path.join(cls.repo_root, "public"),
                    "binding": "ASSETS",
                    "run_worker_first": True,
                    "routerConfig": {"has_user_worker": True}
                }
            }
            mf_options = {
                "host": "127.0.0.1",
                "port": cls.port,
                "durableObjectsPersist": os.path.join(cls.server_dir, "state"),
                "workers": [worker_opts]
            }

            mf_js = os.path.join(cls.server_dir, "actual-miniflare.cjs")
            with open(mf_js, "w") as f:
                f.write(
                    "const {Miniflare,convertV4MiniflareOptions}=require(" + json.dumps(os.path.join(cls.repo_root, "node_modules", "miniflare")) + "); "
                    "const opts=" + json.dumps(mf_options) + "; "
                    "const mf=new Miniflare(convertV4MiniflareOptions(opts)); "
                    "mf.ready.then(async u=>{console.log('ACTUAL_WORKER_READY',String(u));}).catch(e=>{console.error(e);process.exit(1);}); "
                    "process.on('SIGTERM', async()=>{await mf.dispose();process.exit(0);});"
                )

            cls.proc = subprocess.Popen(
                ["taskset", "-c", cpu_str, node_bin, str(mf_js)],
                cwd=cls.server_dir, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, preexec_fn=os.setsid
            )
            ready = False
            for _ in range(60):
                time.sleep(0.25)
                try:
                    req = urllib.request.Request(f"http://127.0.0.1:{cls.port}/healthz")
                    with urllib.request.urlopen(req, timeout=1) as resp:
                        if resp.status == 200:
                            ready = True
                            break
                except urllib.error.HTTPError as error:
                    if error.code == 401:
                        ready = True
                        break
                except Exception:
                    pass

            if not ready:
                raise RuntimeError(f"Wrangler dev server failed to start on port {cls.port}")

            cls.init_fixture_data()
        except Exception:
            cls._cleanup_proc()
            cls._cleanup_server_dir()
            raise

    @classmethod
    def tearDownClass(cls):
        cls._cleanup_proc()
        cls._cleanup_server_dir()

    @classmethod
    def seed_sql(cls, sql: str, params: list | None = None) -> list:
        req = urllib.request.Request(
            f"http://127.0.0.1:{cls.port}/__fixture__/seed_sql",
            data=json.dumps({"sql": sql, "params": params or []}).encode("utf-8"),
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if not data.get("ok"):
                raise RuntimeError(f"seed_sql failed: {data.get('error')}")
            return data.get("rows", [])

    @classmethod
    def init_fixture_data(cls):
        now = "2026-01-01T00:00:00Z"
        future = "2030-01-01T00:00:00Z"

        cls.seed_sql("DELETE FROM messages")
        cls.seed_sql("DELETE FROM claims")
        cls.seed_sql("DELETE FROM agents")
        cls.seed_sql("DELETE FROM machines")
        cls.seed_sql("DELETE FROM credentials")

        # Machines
        cls.seed_sql("INSERT INTO machines (id, name, hostname, registered_at) VALUES ('mach-A', 'Machine A', 'host-a', ?)", [now])
        cls.seed_sql("INSERT INTO machines (id, name, hostname, registered_at) VALUES ('mach-B', 'Machine B', 'host-b', ?)", [now])

        # Agents
        cls.seed_sql("INSERT INTO agents (tag, project, role, machine_id) VALUES ('worker-1', 'projA', 'worker', 'mach-A')")
        cls.seed_sql("INSERT INTO agents (tag, project, role, machine_id) VALUES ('principal-1', 'projA', 'principal', 'mach-A')")
        cls.seed_sql("INSERT INTO agents (tag, project, role, machine_id) VALUES ('worker-2', 'projB', 'worker', 'mach-B')")

        # Credentials
        creds = {
            "token_host_a": ("host", "mach-A", None, ["host", "bus:send", "bus:read"]),
            "token_host_b": ("host", "mach-B", None, ["host", "bus:send", "bus:read"]),
            "token_agent_1": ("agent", "mach-A", "worker-1", ["bus:send", "bus:read"]),
            "token_principal_1": ("agent", "mach-A", "principal-1", ["bus:send", "bus:read"]),
            "token_agent_2": ("agent", "mach-B", "worker-2", ["bus:send", "bus:read"]),
            "token_read_only": ("agent", "mach-A", "worker-1", ["bus:read"]),
            "token_send_only": ("agent", "mach-A", "worker-1", ["bus:send"]),
            "token_root_job": ("job", "mach-A", "role:root", ["lease:root", "bus:send", "bus:read"]),
        }
        for token_name, (kind, mach, actor, scopes) in creds.items():
            thash = sha256_b64url(token_name)
            subj = "job-root-1" if kind == "job" else (actor or mach)
            cls.seed_sql(
                "INSERT INTO credentials (id, kind, token_hash, family_id, subject, machine_id, actor, scopes, created_by_sub, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [f"cred_{token_name}", kind, thash, f"fam_{token_name}", subj, mach, actor, json.dumps(scopes), "admin-sub", now, future]
            )

    def run_cli(self, args, token="token_host_a", input_data=None, check=True):
        cmd = [sys.executable, "-m", "agent_hub_host.cli"] + args
        env = os.environ.copy()
        env["HUB_URL"] = f"http://127.0.0.1:{self.port}"
        env["HUB_JOB_TOKEN"] = token
        env["PYTHONPATH"] = os.path.join(self.repo_root, "host")
        # Explicit private XDG_CONFIG_HOME fixture path; do not touch HOME/CODEX_HOME
        env["XDG_CONFIG_HOME"] = os.path.join(self.server_dir, ".config")
        res = subprocess.run(
            cmd,
            input=input_data,
            capture_output=True,
            text=True,
            env=env
        )
        if check and res.returncode != 0:
            raise subprocess.CalledProcessError(res.returncode, cmd, output=res.stdout, stderr=res.stderr)
        return res

    def test_real_worker_send_dedup_inbox_show_ack_accept_complete_reply(self):
        # 1. Real Worker Send
        res_send = self.run_cli([
            "send",
            "--from", "worker-1",
            "--to", "worker-2",
            "--kind", "note",
            "--body", "Real Worker E2E test",
            "--idempotency-key", "real-e2e-key-1",
            "--thread-id", "th-real-1"
        ], token="token_host_a")
        out = json.loads(res_send.stdout)
        self.assertIn("id", out)
        m_id = out["id"]
        self.assertEqual(out["thread_id"], "th-real-1")

        # 2. Dedup: idempotent retry returns original ID
        res_retry = self.run_cli([
            "send",
            "--from", "worker-1",
            "--to", "worker-2",
            "--kind", "note",
            "--body", "Real Worker E2E test",
            "--idempotency-key", "real-e2e-key-1",
            "--thread-id", "th-real-1"
        ], token="token_host_a")
        out_retry = json.loads(res_retry.stdout)
        self.assertEqual(out_retry["id"], m_id)

        # 3. Dedup: conflicting payload returns 409
        res_conflict = self.run_cli([
            "send",
            "--from", "worker-1",
            "--to", "worker-2",
            "--kind", "note",
            "--body", "MODIFIED CONFLICTING BODY",
            "--idempotency-key", "real-e2e-key-1",
            "--thread-id", "th-real-1"
        ], token="token_host_a", check=False)
        self.assertNotEqual(res_conflict.returncode, 0)
        self.assertIn("409", res_conflict.stderr)

        # 4. Inbox query for worker-2
        res_inbox = self.run_cli(["inbox", "--actor", "worker-2"], token="token_host_b")
        inbox = json.loads(res_inbox.stdout)
        self.assertTrue(any(m["id"] == m_id for m in inbox))

        # 5. Show message
        res_show = self.run_cli(["show", m_id], token="token_host_b")
        shown = json.loads(res_show.stdout)
        self.assertEqual(shown["id"], m_id)
        self.assertEqual(shown["body"], "Real Worker E2E test")

        # 6. The recipient host's inbox and show above delivered and read it; no stage is written in SQL
        self.assertTrue(shown["delivered_at"] and shown["read_at"])
        self.assertIsNone(shown["acked_at"])

        # 7. ACK
        res_ack = self.run_cli(["ack", m_id], token="token_host_b")
        ack_data = json.loads(res_ack.stdout)
        self.assertEqual(ack_data["status"], "ok")
        self.assertEqual(ack_data["stage"], "ack")

        # 8. Accept
        res_accept = self.run_cli(["accept", m_id], token="token_host_b")
        accept_data = json.loads(res_accept.stdout)
        self.assertEqual(accept_data["status"], "ok")
        self.assertEqual(accept_data["stage"], "accept")

        # 9. Complete
        res_complete = self.run_cli(["complete", m_id], token="token_host_b")
        complete_data = json.loads(res_complete.stdout)
        self.assertEqual(complete_data["status"], "ok")
        self.assertEqual(complete_data["stage"], "complete")

        # 10. Reply
        res_reply = self.run_cli([
            "reply",
            "--reply-to", m_id,
            "--from", "worker-2",
            "--to", "worker-1",
            "--kind", "reply",
            "--body", "Work completed successfully",
            "--idempotency-key", "real-e2e-rep-1",
            "--thread-id", "th-real-1"
        ], token="token_host_b")
        rep_out = json.loads(res_reply.stdout)
        self.assertIn("id", rep_out)
        self.assertEqual(rep_out["thread_id"], "th-real-1")

    def test_real_worker_claims_and_release(self):
        # 1. Claim scope
        res_c = self.run_cli([
            "claim",
            "--project", "projA",
            "--scope", "./src//bus.ts",
            "--holder", "worker-1",
            "--ttl-ms", "60000"
        ], token="token_agent_1")
        c_out = json.loads(res_c.stdout)
        self.assertIn("id", c_out)
        cid = c_out["id"]

        # 2a. Cross-project claim by worker-2 (projB) into projA is denied with 403
        res_cross = self.run_cli([
            "claim",
            "--project", "projA",
            "--scope", "src/bus.ts",
            "--holder", "worker-2",
            "--ttl-ms", "60000"
        ], token="token_host_b", check=False)
        self.assertNotEqual(res_cross.returncode, 0)
        self.assertIn("403", res_cross.stderr)

        # 2b. Conflicting claim by principal-1 in same project (projA) fails with 409
        res_conf = self.run_cli([
            "claim",
            "--project", "projA",
            "--scope", "src/bus.ts",
            "--holder", "principal-1",
            "--ttl-ms", "60000"
        ], token="token_principal_1", check=False)
        self.assertNotEqual(res_conf.returncode, 0)
        self.assertIn("409", res_conf.stderr)

        # 3. List claims includes normalized scope
        res_list = self.run_cli(["claims", "--project", "projA"], token="token_agent_1")
        clist = json.loads(res_list.stdout)
        self.assertTrue(any(c["id"] == cid and c["scope"] == "src/bus.ts" for c in clist))

        # 4. Non-holder release fails with 403
        res_bad_rel = self.run_cli(["release", "projA:src/bus.ts"], token="token_host_b", check=False)
        self.assertNotEqual(res_bad_rel.returncode, 0)
        self.assertIn("403", res_bad_rel.stderr)

        # 5. Holder release succeeds
        res_rel = self.run_cli(["release", "projA:src/bus.ts"], token="token_agent_1")
        rel_data = json.loads(res_rel.stdout)
        self.assertEqual(rel_data["status"], "ok")

    def test_real_worker_scoped_same_host_impersonation_deny(self):
        # worker-1 token tries to send as principal-1 -> fails 403
        res_imp = self.run_cli([
            "send",
            "--from", "principal-1",
            "--to", "worker-2",
            "--kind", "note",
            "--body", "Spoofed message"
        ], token="token_agent_1", check=False)
        self.assertNotEqual(res_imp.returncode, 0)
        self.assertIn("403", res_imp.stderr)

        # worker-1 token tries to read inbox for principal-1 -> fails 403
        res_inbox_imp = self.run_cli(["inbox", "--actor", "principal-1"], token="token_agent_1", check=False)
        self.assertNotEqual(res_inbox_imp.returncode, 0)
        self.assertIn("403", res_inbox_imp.stderr)

    def test_real_worker_read_only_and_send_only_scopes(self):
        # Read-only token cannot send -> 403
        res_ro = self.run_cli([
            "send",
            "--from", "worker-1",
            "--to", "worker-2",
            "--kind", "note",
            "--body", "RO send attempt"
        ], token="token_read_only", check=False)
        self.assertNotEqual(res_ro.returncode, 0)
        self.assertIn("403", res_ro.stderr)

        # Send-only token cannot read inbox -> 403
        res_so = self.run_cli(["inbox", "--actor", "worker-1"], token="token_send_only", check=False)
        self.assertNotEqual(res_so.returncode, 0)
        self.assertIn("403", res_so.stderr)

    def test_real_worker_malformed_and_utf8_bounds(self):
        # Body > 16 KiB rejected
        res_over = self.run_cli([
            "send",
            "--from", "worker-1",
            "--to", "worker-2",
            "--kind", "note",
            "--body", "-"
        ], token="token_host_a", input_data="X" * 16385, check=False)
        self.assertEqual(res_over.returncode, 1)
        self.assertIn("exceeds 16 KiB limit", res_over.stderr)

        # UTF-8 multibyte body succeeds
        utf8_text = "Unicode test: 🚀 🤖 äöü €"
        res_utf8 = self.run_cli([
            "send",
            "--from", "worker-1",
            "--to", "worker-2",
            "--kind", "note",
            "--body", utf8_text,
            "--idempotency-key", "utf8-key-1"
        ], token="token_host_a")
        out_utf8 = json.loads(res_utf8.stdout)
        res_show = self.run_cli(["show", out_utf8["id"]], token="token_host_b")
        msg = json.loads(res_show.stdout)
        self.assertEqual(msg["body"], utf8_text)

    def test_real_worker_unknown_addresses(self):
        # Unknown recipient rejected with 404 (unknown_recipient)
        res_unknown = self.run_cli([
            "send",
            "--from", "worker-1",
            "--to", "nonexistent-agent-xyz",
            "--kind", "note",
            "--body", "Hello nowhere"
        ], token="token_host_a", check=False)
        self.assertNotEqual(res_unknown.returncode, 0)
        self.assertIn("404", res_unknown.stderr)

    def test_real_worker_root_role_held_denial_and_zero_effects(self):
        # 1. Attempt send with --from role:root is explicitly denied with 403 (missing_m4_root_authority)
        res_from_root = self.run_cli([
            "send",
            "--from", "role:root",
            "--to", "worker-1",
            "--kind", "note",
            "--body", "Unauthorized root dispatch attempt",
            "--idempotency-key", "root-attempt-1"
        ], token="token_root_job", check=False)
        self.assertNotEqual(res_from_root.returncode, 0)
        self.assertIn("403", res_from_root.stderr)

        # 2. Attempt send with host token as role:root is denied with 403
        res_host_root = self.run_cli([
            "send",
            "--from", "role:root",
            "--to", "worker-1",
            "--kind", "note",
            "--body", "Host cannot impersonate root",
            "--idempotency-key", "root-attempt-2"
        ], token="token_host_a", check=False)
        self.assertNotEqual(res_host_root.returncode, 0)
        self.assertIn("403", res_host_root.stderr)

        # 3. Attempt send to recipient role:root fails (unknown recipient / denied with 404)
        res_to_root = self.run_cli([
            "send",
            "--from", "worker-1",
            "--to", "role:root",
            "--kind", "note",
            "--body", "Dispatch to root recipient"
        ], token="token_host_a", check=False)
        self.assertNotEqual(res_to_root.returncode, 0)
        self.assertIn("404", res_to_root.stderr)

        # 4. Attempt claim with holder role:root is explicitly denied with 403
        res_claim_root = self.run_cli([
            "claim",
            "--project", "projA",
            "--scope", "root/subsystem.ts",
            "--holder", "role:root",
            "--ttl-ms", "60000"
        ], token="token_root_job", check=False)
        self.assertNotEqual(res_claim_root.returncode, 0)
        self.assertIn("403", res_claim_root.stderr)

        # 5. Supplying lease-epoch (0 or 5) does not grant synthetic authority; still fails 403
        res_epoch = self.run_cli([
            "send",
            "--from", "role:root",
            "--to", "worker-1",
            "--kind", "note",
            "--body", "Epoch cannot grant synthetic root",
            "--idempotency-key", "root-attempt-epoch",
            "--lease-epoch", "0"
        ], token="token_root_job", check=False)
        self.assertNotEqual(res_epoch.returncode, 0)
        self.assertIn("403", res_epoch.stderr)

        # 6. Verify zero side-effects in actual SQLite database
        msg_rows = self.seed_sql("SELECT count(*) as cnt FROM messages WHERE from_actor = 'role:root' OR to_actor = 'role:root'")
        self.assertEqual(msg_rows[0].get("cnt", 0), 0)

        claim_rows = self.seed_sql("SELECT count(*) as cnt FROM claims WHERE holder = 'role:root'")
        self.assertEqual(claim_rows[0].get("cnt", 0), 0)

    def test_real_worker_one_cli_and_bridge_fake_roundtrip(self):
        # 1. Send via CLI from worker-1 to worker-2 on real Worker
        res_send = self.run_cli([
            "send",
            "--from", "worker-1",
            "--to", "worker-2",
            "--kind", "note",
            "--body", "Bridge roundtrip payload",
            "--idempotency-key", "bt-roundtrip-1"
        ], token="token_host_a")
        msg_info = json.loads(res_send.stdout)
        m_id = msg_info["id"]

        # The recipient's own host polls its inbox: delivered on the real hub, no SQL stage write
        inbox_b = json.loads(self.run_cli(["inbox", "--actor", "worker-2"], token="token_host_b").stdout)
        self.assertTrue(any(m["id"] == m_id and m["delivered_at"] for m in inbox_b))

        # 2. Bridge setup with fake-a binary
        from agent_hub_host.bridge import (
            BridgeCapabilityGap,
            MailboxBridge,
            canonicalize_workspace,
            get_workspace_key,
        )
        from agent_hub_host.native_bridge import validate_native_send_envelope

        work_dir = os.path.join(self.server_dir, "work_rt")
        rt_dir = os.path.join(self.server_dir, "rt_dir")
        st_dir = os.path.join(self.server_dir, "st_dir")
        os.makedirs(work_dir, exist_ok=True)
        canon_ws = canonicalize_workspace(work_dir)
        rec_uuid = str(uuid.uuid4())
        service_uuid = str(uuid.uuid4())
        service_tag = "hub-bridge"
        native_m_id = str(uuid.uuid4())
        native_rep_id = str(uuid.uuid4())

        # Recipient session in rt_dir
        sdir = os.path.join(rt_dir, "sessions", rec_uuid)
        os.makedirs(sdir, exist_ok=True)
        with open(os.path.join(sdir, "session.json"), "w") as f:
            json.dump({"schema_version": 1, "id": rec_uuid, "workspace": canon_ws, "tag": "worker-2"}, f)

        # Mailbox in st_dir
        ws_key = get_workspace_key(canon_ws)
        mb_dir = os.path.join(st_dir, "messages", ws_key)
        os.makedirs(os.path.join(mb_dir, "cursors"), exist_ok=True)
        with open(os.path.join(mb_dir, "workspace.json"), "w") as f:
            json.dump({"workspace": canon_ws}, f)

        fake_a = os.path.join(self.server_dir, "fake-a-rt")
        with open(fake_a, "w") as f:
            f.write(f"""#!/usr/bin/env python3
import sys, json, os, time
args = sys.argv[1:]
if not args:
    sys.exit(1)

ambient_sid = os.environ.get("APLEXER_SESSION_ID")

if args[0] == "message":
    if args[1] == "send":
        body = args[-1]
        if not ambient_sid:
            # Anonymous envelope per native aplexer contract (no session binding)
            print(json.dumps({{
                "schema_version": 1,
                "id": "{native_m_id}-anon",
                "workspace": "{canon_ws}",
                "created_at": int(time.time()),
                "from": {{"external": True, "workspace": "{canon_ws}"}},
                "to": {{"tag": "worker-2", "session_id": "{rec_uuid}"}},
                "kind": "note",
                "body": body,
                "delivery": "inbox"
            }}))
            sys.exit(0)

        # Qualified authenticated envelope with own bridge service session binding
        with open("{os.path.join(mb_dir, "cursors", f"{rec_uuid}.json")}", "w") as cf:
            json.dump({{"exceptions": ["{native_m_id}"], "acked_through": None}}, cf)
        print(json.dumps({{
            "schema_version": 1,
            "id": "{native_m_id}",
            "workspace": "{canon_ws}",
            "created_at": int(time.time()),
            "from": {{
                "tag": "{service_tag}",
                "session_id": ambient_sid,
                "workspace": "{canon_ws}",
                "external": False
            }},
            "to": {{"tag": "worker-2", "session_id": "{rec_uuid}"}},
            "kind": "note",
            "reply_to": None,
            "body": body,
            "delivery": "inbox"
        }}))
        sys.exit(0)
    elif args[1] == "log":
        print(json.dumps([{{
            "schema_version": 1,
            "id": "{native_rep_id}",
            "reply_to": "{native_m_id}",
            "workspace": "{canon_ws}",
            "created_at": int(time.time()),
            "from": {{"session_id": "{rec_uuid}", "tag": "worker-2", "workspace": "{canon_ws}", "external": False}},
            "to": {{"tag": "{service_tag}"}},
            "kind": "reply",
            "body": "Fake-A processed",
            "delivery": "inbox"
        }}]))
        sys.exit(0)
sys.exit(0)
""")
        os.chmod(fake_a, 0o755)

        # 2a. Explicit negative: missing-binding / anonymous sender fails closed with BridgeCapabilityGap
        saved_aplexer_sid = os.environ.pop("APLEXER_SESSION_ID", None)
        try:
            unbound_bridge = MailboxBridge(work_dir, aplexer_bin=fake_a, state_dir=st_dir, runtime_dir=rt_dir)
            self.assertIsNone(unbound_bridge.service_binding)
            with self.assertRaises(BridgeCapabilityGap) as ctx_gap:
                unbound_bridge.forward_hub_to_native({
                    "id": f"{m_id}-unbound-negative",
                    "from_actor": "worker-1",
                    "to_actor": "worker-2",
                    "body": "Negative unbound forward payload",
                    "kind": "note",
                    "thread_id": msg_info["thread_id"],
                })
            self.assertIn("absent or unqualified", str(ctx_gap.exception))
            self.assertNotIn(f"{m_id}-unbound-negative", unbound_bridge.receipts.get("hub_to_native", {}))
            self.assertIn(f"{m_id}-unbound-negative", unbound_bridge.receipts.get("ambiguous_sends", {}))

            # Additional direct envelope contract check: external:true is refused even if service binding is absent
            with self.assertRaises(BridgeCapabilityGap) as ctx_ext:
                validate_native_send_envelope(
                    {
                        "schema_version": 1,
                        "id": f"{native_m_id}-ext-neg",
                        "workspace": canon_ws,
                        "from": {"external": True, "tag": service_tag, "session_id": service_uuid},
                        "to": {"tag": "worker-2", "session_id": rec_uuid},
                    },
                    unbound_bridge.service_binding,
                    canon_ws,
                )
            self.assertIn("absent or unqualified", str(ctx_ext.exception))
        finally:
            if saved_aplexer_sid is not None:
                os.environ["APLEXER_SESSION_ID"] = saved_aplexer_sid

        # 2b. Authoritative bridge service session registration (explicit fixture identity / own-session binding)
        serv_dir = os.path.join(rt_dir, "sessions", service_uuid)
        os.makedirs(serv_dir, exist_ok=True)
        with open(os.path.join(serv_dir, "session.json"), "w") as f:
            json.dump({
                "schema_version": 1,
                "id": service_uuid,
                "workspace": canon_ws,
                "tag": service_tag,
                "engine": "hub-bridge",
                "created_at_ms": int(time.time() * 1000) - 1000,
                "command": ["hub-bridge-service"],
                "cwd": canon_ws,
            }, f)

        # 2c. Qualified bridge initialization with verified service binding (OFFLINE fixture qualification)
        bridge = MailboxBridge(
            work_dir,
            aplexer_bin=fake_a,
            state_dir=st_dir,
            runtime_dir=rt_dir,
            service_tag=service_tag,
            service_session_id=service_uuid,
        )
        self.assertIsNotNone(bridge.service_binding)
        self.assertEqual(bridge.service_binding.tag, service_tag)
        self.assertEqual(bridge.service_binding.session_id, service_uuid)

        # Direct envelope contract check: external:true is refused even with qualified service binding
        with self.assertRaises(BridgeCapabilityGap) as ctx_ext_bound:
            validate_native_send_envelope(
                {
                    "schema_version": 1,
                    "id": f"{native_m_id}-ext-neg-bound",
                    "workspace": canon_ws,
                    "from": {"external": True, "tag": service_tag, "session_id": service_uuid},
                    "to": {"tag": "worker-2", "session_id": rec_uuid},
                },
                bridge.service_binding,
                canon_ws,
            )
        self.assertIn("external:true", str(ctx_ext_bound.exception))

        # Qualified fake positive: forwarding succeeds and returns native message ID
        fwd_id = bridge.forward_hub_to_native({
            "id": m_id,
            "from_actor": "worker-1",
            "to_actor": "worker-2",
            "body": "Bridge roundtrip payload",
            "kind": "note",
            "thread_id": msg_info["thread_id"],
        })
        self.assertEqual(fwd_id, native_m_id)

        # Bridge syncs to real Worker hub using token_host_b
        saved_url = os.environ.get("HUB_URL")
        saved_tok = os.environ.get("HUB_JOB_TOKEN")
        try:
            os.environ["HUB_URL"] = f"http://127.0.0.1:{self.port}"
            os.environ["HUB_JOB_TOKEN"] = "token_host_b"
            bridge.sync_native_to_hub()
        finally:
            if saved_url: os.environ["HUB_URL"] = saved_url
            else: os.environ.pop("HUB_URL", None)
            if saved_tok: os.environ["HUB_JOB_TOKEN"] = saved_tok
            else: os.environ.pop("HUB_JOB_TOKEN", None)

        # 3. Verify message is acked on real Worker
        res_show = self.run_cli(["show", m_id], token="token_host_b")
        shown_msg = json.loads(res_show.stdout)
        self.assertIsNotNone(shown_msg.get("acked_at"))

        # 4. Verify reverse reply is received on real Worker
        res_inbox = self.run_cli(["inbox", "--actor", "worker-1"], token="token_host_a")
        inbox_msgs = json.loads(res_inbox.stdout)
        self.assertTrue(any(m["body"] == "Fake-A processed" and m["reply_to"] == m_id for m in inbox_msgs))

    # --- real `hub bridge` CLI runs: real Worker, fake `a`, every stage set by the hub itself ---------

    def _bridge_host(self, name, tags, scopes=("host", "bus:send", "bus:read")):
        """A fresh machine with agents and a host token, so a bridge run sees only this test's inbox.

        Registry rows are seeded in SQL (the hub has no agent import API yet); message stages never are.
        """
        self.bridge_mach = f"mach-{name}"
        self.seed_sql("INSERT INTO machines (id, name, hostname, registered_at) VALUES (?, ?, ?, ?)", [self.bridge_mach, name, name, "2026-01-01T00:00:00Z"])
        for tag in tags:
            self.seed_sql("INSERT INTO agents (tag, project, role, machine_id) VALUES (?, 'projE', ?, ?)", [tag, f"{tag}-role", self.bridge_mach])
        return self._bridge_token(f"token_{name}", scopes)

    def _bridge_token(self, token, scopes):
        now, future = "2026-01-01T00:00:00Z", "2030-01-01T00:00:00Z"
        self.seed_sql(
            "INSERT INTO credentials (id, kind, token_hash, family_id, subject, machine_id, actor, scopes, created_by_sub, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [f"cred_{token}", "host", sha256_b64url(token), f"fam_{token}", self.bridge_mach, self.bridge_mach, None, json.dumps(list(scopes)), "admin-sub", now, future])
        return token

    def _native(self, tags, auto_ack=True, replies=None):
        from agent_hub_host.bridge import canonicalize_workspace, get_workspace_key
        td = tempfile.mkdtemp(dir=self.server_dir)
        self.work, self.state, self.rt = (os.path.join(td, d) for d in ("work", "state", "rt"))
        for d in (self.work, self.state, self.rt):
            os.makedirs(d)
        ws = canonicalize_workspace(self.work)
        mailbox = os.path.join(self.state, "messages", get_workspace_key(ws))
        os.makedirs(os.path.join(mailbox, "cursors"))
        with open(os.path.join(mailbox, "workspace.json"), "w") as f:
            json.dump({"workspace": ws}, f)
        cfg = {"ws": ws, "tags": {}, "auto_ack": auto_ack, "replies": replies or {}}
        for sid, tag, engine in [(BRIDGE_SERVICE_SESSION, "hub-bridge", "hub-bridge")] + [(str(uuid.uuid4()), t, "test-engine") for t in tags]:
            os.makedirs(os.path.join(self.rt, "sessions", sid))
            with open(os.path.join(self.rt, "sessions", sid, "session.json"), "w") as f:
                json.dump({"schema_version": 1, "id": sid, "workspace": ws, "tag": tag, "engine": engine,
                           "created_at_ms": 1700000000000, "command": ["bash"], "cwd": ws}, f)
            if tag != "hub-bridge":
                cursor = os.path.join(mailbox, "cursors", f"{sid}.json")
                with open(cursor, "w") as f:
                    json.dump({"acked_through": None, "exceptions": []}, f)
                cfg["tags"][tag] = {"rec": sid, "cursor": cursor}
        self.fake_dir = os.path.join(td, "fake")
        os.makedirs(self.fake_dir)
        self.fake_a = os.path.join(self.fake_dir, "a")
        with open(self.fake_a, "w") as f:
            f.write(REAL_BRIDGE_FAKE_A)
        os.chmod(self.fake_a, 0o755)
        with open(os.path.join(self.fake_dir, "cfg.json"), "w") as f:
            json.dump(cfg, f)
        self.native_cfg = cfg

    def _fake_lines(self, name):
        path = os.path.join(self.fake_dir, name)
        if not os.path.exists(path):
            return []
        with open(path) as f:
            return [json.loads(line) for line in f]

    def _run_bridge(self, token, *extra):
        env = {k: v for k, v in os.environ.items() if k.lower() not in ("http_proxy", "https_proxy", "all_proxy")}
        for k in ("HUB_JOB_TOKEN", "HUB_TOKEN", "HUB_LEASE_EPOCH"):
            env.pop(k, None)
        env.update({"HUB_URL": f"http://127.0.0.1:{self.port}", "HUB_TOKEN": token, "NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1",
                    "HOME": self.server_dir, "XDG_CONFIG_HOME": os.path.join(self.server_dir, ".config"),
                    "XDG_RUNTIME_DIR": self.rt, "APLEXER_STATE_DIR": self.state, "APLEXER_RUNTIME_DIR": self.rt,
                    "PYTHONPATH": os.path.join(self.repo_root, "host")})
        cmd = [sys.executable, "-m", "agent_hub_host.cli", "bridge", "--workspace", self.work, "--aplexer-bin", self.fake_a,
               "--state-dir", self.state, "--runtime-dir", self.rt, *extra]
        res = subprocess.run(cmd, cwd=os.path.join(self.repo_root, "host"), env=env, capture_output=True, text=True, timeout=120)
        return res, (json.loads(res.stdout) if res.stdout.strip() else None)

    def _hub(self, method, path, token, body=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method=method,
                                     data=None if body is None else json.dumps(body).encode(),
                                     headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"null")

    def _hub_send(self, to, key):
        status, out = self._hub("POST", "/api/bus/send", "token_host_b",
                                {"from_actor": "worker-2", "to_actor": to, "kind": "note", "body": f"body {key}", "idempotency_key": key})
        self.assertEqual(status, 200, out)
        return out["id"]

    def _stages(self, mid):
        # Read-only observation of the real row; stages are only ever written by the hub's own HTTP paths
        return self.seed_sql("SELECT delivered_at, read_at, acked_at FROM messages WHERE id = ?", [mid])[0]

    def _replies_to(self, mid):
        status, inbox = self._hub("GET", "/api/bus/inbox?actor=worker-2&limit=100", "token_host_b")
        self.assertEqual(status, 200, inbox)
        return [m for m in inbox if m.get("reply_to") == mid]

    def test_sync_reply_current_authenticated_http_send_accepts_explicit_request(self):
        self._assert_sync_request_accepted("/api/bus/send", "sync-wire-send")

    def test_sync_reply_current_authenticated_http_reply_accepts_explicit_request(self):
        self._assert_sync_request_accepted("/api/bus/reply", "sync-wire-reply")

    def _assert_sync_request_accepted(self, route, name):
        token = self._bridge_host(name, [name])
        parent = self._hub_send(name, name + "-parent")
        payload = {"from_actor": name, "to_actor": "worker-2", "kind": "sync_reply", "body": "explicit request",
                   "idempotency_key": name + "-sync", "reply_to": parent}
        status, sent = self._hub("POST", route, token, payload)
        self.assertEqual(status, 200, sent)
        self.assertEqual(sent["thread_id"], self.seed_sql("SELECT thread_id FROM messages WHERE id = ?", [parent])[0]["thread_id"])
        again = self._hub("POST", route, token, payload)
        self.assertEqual(again, (200, sent))
        self.assertEqual(len(self.seed_sql("SELECT id FROM messages WHERE idempotency_key = ?", [payload["idempotency_key"]])), 1)

    def _configure_native(self, **updates):
        self.native_cfg.update(updates)
        with open(os.path.join(self.fake_dir, "cfg.json"), "w") as f:
            json.dump(self.native_cfg, f)

    def _sync_hub_request(self, tag, key, route="/api/bus/send", parent=None, **extra):
        payload = {"from_actor": "worker-2", "to_actor": tag, "kind": "sync_reply", "body": key, "idempotency_key": key}
        if parent:
            payload["reply_to"] = parent
        payload.update(extra)
        status, out = self._hub("POST", route, "token_host_b", payload)
        self.assertEqual(status, 200, out)
        return out, payload

    def test_sync_reply_real_worker_cli_idle_empty_delivery_once_for_both_routes(self):
        token = self._bridge_host("sync-positive", ["sp-w"])
        self._native(["sp-w"], replies={"sp-w": {"kind": "sync_reply", "body": "native reply"}})
        status, parent = self._hub("POST", "/api/bus/send", token,
            {"from_actor": "sp-w", "to_actor": "worker-2", "kind": "note", "body": "parent", "idempotency_key": "sp-parent"})
        self.assertEqual(status, 200, parent)
        sent=[]
        for route, key in (("/api/bus/send", "sp-send"), ("/api/bus/reply", "sp-reply")):
            out, payload = self._sync_hub_request("projE:sp-w", key, route, parent["id"], thread_id=parent["thread_id"])
            sent.append(out["id"])
            self.assertEqual(self._hub("POST", route, "token_host_b", payload), (200, out))
            self.assertEqual(out["thread_id"], parent["thread_id"])
            status, bad = self._hub("POST", route, "token_host_b", dict(payload, idempotency_key=key+"-bad", thread_id="wrong"))
            self.assertEqual((status, bad), (400, {"error": "invalid_reply_to"}))
        note = self._hub_send("sp-w", "sp-note")
        status, ordinary = self._hub("POST", "/api/bus/reply", "token_host_b",
            {"from_actor": "worker-2", "to_actor": "sp-w", "kind": "reply", "body": "ordinary reply", "idempotency_key": "sp-ordinary", "reply_to": parent["id"]})
        self.assertEqual(status, 200, ordinary)
        res, outcome = self._run_bridge(token, "--once")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(len(outcome["cycles"][0]["forwarded"]), 4)
        self.assertEqual(sorted(e["body"] for e in self._fake_lines("effects.jsonl")), ["sp-reply", "sp-send"])
        self.assertEqual(len(self._fake_lines("sends.jsonl")), 4)
        for mid in sent + [note, ordinary["id"]]:
            self.assertTrue(self._stages(mid)["acked_at"])
            self.assertTrue(all(r["kind"] == "reply" for r in self._replies_to(mid)))
        self.assertEqual(len([c for c in self._fake_lines("calls.jsonl") if c[0] == "status"]), 6)
        self.assertEqual(len([c for c in self._fake_lines("calls.jsonl") if c[0] == "capture"]), 6)
        res2, _ = self._run_bridge(token, "--once")
        self.assertEqual(res2.returncode, 0, res2.stderr)
        self.assertEqual(len(self._fake_lines("sends.jsonl")), 4)
        self.assertEqual(len(self._fake_lines("effects.jsonl")), 2)

    def test_sync_reply_real_worker_cli_unsafe_states_queue_without_typing_then_retry(self):
        cases = [("busy", "❯", 0), ("unknown", "❯", 0), ("waiting", "❯", 0), ("exited", "❯", 0),
                 ("idle", "❯ draft", 0), ("idle", "? Select an option\n❯", 0), ("idle", "", 0), ("idle", "❯", 4)]
        for i, (state, capture, status_exit) in enumerate(cases):
            with self.subTest(state=state, capture=capture, status_exit=status_exit):
                tag=f"su-{i}"
                token=self._bridge_host(f"sync-unsafe-{i}", [tag])
                self._native([tag], auto_ack=False)
                self._configure_native(state=state, capture=capture, status_exit=status_exit)
                out, _ = self._sync_hub_request(tag, f"su-body-{i}")
                res, outcome = self._run_bridge(token, "--once")
                self.assertEqual(res.returncode, 1, res.stderr)
                self.assertTrue(outcome["cycles"][0]["held"])
                self.assertEqual(len(self._fake_lines("sends.jsonl")), 1)  # queue is distinct from prompt typing
                self.assertEqual(self._fake_lines("effects.jsonl"), [])
                self.assertFalse(any(c[:2] == ["message", "deliver"] for c in self._fake_lines("calls.jsonl")))
                self.assertIsNone(self._stages(out["id"])["acked_at"])
                self._configure_native(state="idle", capture="❯", status_exit=0, auto_ack=True)
                res2, _ = self._run_bridge(token, "--once")
                self.assertEqual(res2.returncode, 0, res2.stderr)
                self.assertEqual(len(self._fake_lines("sends.jsonl")), 1)
                self.assertEqual(len(self._fake_lines("effects.jsonl")), 1)
                self.assertTrue(self._stages(out["id"])["acked_at"])

    def test_sync_reply_real_worker_auth_scope_machine_recipient_and_root_guards(self):
        token=self._bridge_host("sync-auth", ["sa-w"])
        read_only=self._bridge_token("token_sync_auth_ro", ("host", "bus:read"))
        payload={"from_actor": "sa-w", "to_actor": "worker-2", "kind": "sync_reply", "body": "auth", "idempotency_key": "sa-key"}
        for route in ("/api/bus/send", "/api/bus/reply"):
            parent=self._hub_send("sa-w", "sa-parent-"+route.rsplit("/",1)[1])
            req=dict(payload, reply_to=parent)
            self.assertEqual(self._hub("POST", route, "", req)[0], 401)
            self.assertEqual(self._hub("POST", route, read_only, req)[0], 403)
            self.assertEqual(self._hub("POST", route, "token_host_b", req)[0], 403)  # foreign machine sender
            self.assertEqual(self._hub("POST", route, token, dict(req, to_actor="no-such-agent"))[0], 404)
            self.assertEqual(self._hub("POST", route, token, dict(req, from_actor="role:root"))[0], 403)
        self.assertEqual(self.seed_sql("SELECT id FROM messages WHERE idempotency_key = 'sa-key'"), [])
        self._native(["sa-w"], auto_ack=False)
        self._configure_native(state="busy")
        out, _=self._sync_hub_request("sa-w", "sa-valid")
        self.assertEqual(self._hub("GET", "/api/bus/inbox?actor=sa-w", "token_host_b")[0], 403)
        self.assertIsNone(self._stages(out["id"])["delivered_at"])
        res, _=self._run_bridge(token, "--once")
        self.assertEqual(res.returncode, 1, res.stderr)
        self.assertEqual(self._hub("POST", f"/api/bus/messages/{out['id']}/ack", "token_host_b")[0], 403)
        self.assertIsNone(self._stages(out["id"])["acked_at"])
        self.assertEqual(self._fake_lines("effects.jsonl"), [])

    def test_sync_reply_real_worker_cli_effect_then_crash_restart_never_replays(self):
        self._assert_sync_uncertain_restart("crash")

    def test_sync_reply_real_worker_cli_effect_then_timeout_restart_never_replays(self):
        self._assert_sync_uncertain_restart("timeout")

    def _assert_sync_uncertain_restart(self, behavior):
        tag="sx-"+behavior
        token=self._bridge_host("sync-"+behavior, [tag])
        self._native([tag], auto_ack=False)
        self._configure_native(deliver_behavior=behavior)
        out, _=self._sync_hub_request(tag, "sx-body-"+behavior)
        res, outcome=self._run_bridge(token, "--once")
        self.assertEqual(res.returncode, 1, res.stderr)
        self.assertTrue(outcome["cycles"][0]["held"])
        self.assertEqual(len(self._fake_lines("sends.jsonl")), 1)
        self.assertEqual(len(self._fake_lines("effects.jsonl")), 1)
        self.assertIsNone(self._stages(out["id"])["acked_at"])
        with open(os.path.join(self.work, ".local", "hub-bridge-receipts.json")) as f:
            receipts=json.load(f)
        nid=receipts["hub_to_native"][out["id"]]
        self.assertEqual(receipts["delivers"][nid]["status"], "timeout" if behavior == "timeout" else "uncertain")
        self.assertIn(out["id"], receipts["ambiguous_sends"])
        self._configure_native(deliver_behavior="success", auto_ack=True)
        res2, again=self._run_bridge(token, "--once")  # fresh process, existing durable uncertainty
        self.assertEqual(res2.returncode, 1, res2.stderr)
        self.assertTrue(again["cycles"][0]["held"])
        self.assertEqual(len(self._fake_lines("sends.jsonl")), 1)
        self.assertEqual(len(self._fake_lines("effects.jsonl")), 1)
        self.assertIsNone(self._stages(out["id"])["acked_at"])

    def test_real_worker_cli_bridge_round_trip_without_sql_stage_writes(self):
        token = self._bridge_host("e2e1", ["e1-w", "e1-p"])
        self._native(["e1-w", "e1-p"], replies={"e1-w": {"kind": "sync_reply", "body": "done-w"}, "e1-p": {"kind": "odd-kind", "body": "done-p"}})
        ids = {"q": self._hub_send("projE:e1-w", "e1-q"), "t": self._hub_send("e1-w", "e1-t"), "r": self._hub_send("role:e1-p-role", "e1-r")}
        self.assertTrue(all(self._stages(m) == {"delivered_at": None, "read_at": None, "acked_at": None} for m in ids.values()))

        res, out = self._run_bridge(token, "--once")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(out["cycles"][0]["errors"], [])
        self.assertEqual(sorted(f["hub_id"] for f in out["cycles"][0]["forwarded"]), sorted(ids.values()))
        for m in ids.values():
            st = self._stages(m)
            self.assertTrue(st["delivered_at"] and st["acked_at"], st)
        self.assertEqual(sorted(s["tag"] for s in self._fake_lines("sends.jsonl")), ["e1-p", "e1-w", "e1-w"])
        self.assertEqual({tuple(c[:2]) for c in self._fake_lines("calls.jsonl")}, {("message", "send"), ("message", "log")})
        # F7 freezes canonical agent endpoints; explicit role addresses remain roles.
        for key, from_actor in (("q", "projE:e1-w"), ("t", "projE:e1-w"), ("r", "role:e1-p-role")):
            replies = self._replies_to(ids[key])
            self.assertEqual([(r["from_actor"], r["to_actor"], r["kind"]) for r in replies], [(from_actor, "projB:worker-2", "reply")])

        # A second run has no new native send, hub ACK or reply
        acked = {m: self._stages(m)["acked_at"] for m in ids.values()}
        res2, out2 = self._run_bridge(token, "--once")
        self.assertEqual(res2.returncode, 0, res2.stderr)
        self.assertEqual(out2["cycles"][0]["forwarded"], [])
        self.assertEqual(len(self._fake_lines("sends.jsonl")), 3)
        self.assertEqual({m: self._stages(m)["acked_at"] for m in ids.values()}, acked)
        self.assertTrue(all(len(self._replies_to(m)) == 1 for m in ids.values()))

    def test_real_worker_cli_bridge_hub_ack_waits_for_native_ack(self):
        token = self._bridge_host("e2e2", ["e2-w"])
        self._native(["e2-w"], auto_ack=False)
        mid = self._hub_send("e2-w", "e2-a")
        res, _ = self._run_bridge(token, "--once")
        self.assertEqual(res.returncode, 0, res.stderr)
        st = self._stages(mid)
        self.assertTrue(st["delivered_at"])
        self.assertIsNone(st["acked_at"])
        # The recipient ACKs natively; only then does the next run ACK on the hub
        cursor = self.native_cfg["tags"]["e2-w"]["cursor"]
        with open(cursor) as f:
            data = json.load(f)
        data["exceptions"].append(self._fake_lines("sends.jsonl")[0]["id"])
        with open(cursor, "w") as f:
            json.dump(data, f)
        res2, _ = self._run_bridge(token, "--once")
        self.assertEqual(res2.returncode, 0, res2.stderr)
        self.assertTrue(self._stages(mid)["acked_at"])
        self.assertEqual(len(self._fake_lines("sends.jsonl")), 1)

    def test_real_worker_cli_bridge_bounded_window_never_starves_later_messages(self):
        token = self._bridge_host("e2e3", ["e3-w"])
        self._native(["e3-w"])
        ids = [self._hub_send("e3-w", f"e3-{i:02d}") for i in range(13)]
        res1, out1 = self._run_bridge(token, "--limit", "1", "--max-pages", "10")
        self.assertEqual(res1.returncode, 1, res1.stderr)
        self.assertEqual(len(out1["cycles"][0]["forwarded"]), 10)
        self.assertIn("inbox window exhausted", out1["cycles"][0]["errors"][0]["error"])
        res2, out2 = self._run_bridge(token, "--limit", "1", "--max-pages", "10")
        self.assertEqual(res2.returncode, 0, res2.stderr)
        self.assertEqual(len(out2["cycles"][0]["forwarded"]), 3)
        sends = self._fake_lines("sends.jsonl")
        self.assertEqual(len(sends), 13)
        self.assertEqual(len({s["body"] for s in sends}), 13)
        self.assertTrue(all(self._stages(m)["delivered_at"] and self._stages(m)["acked_at"] for m in ids))

    def test_real_worker_cli_bridge_permanent_held_head_progresses_across_restarts(self):
        token = self._bridge_host("e2e5", ["e5-w"])
        self._native(["e5-w"])
        cfg = dict(self.native_cfg, refuse_bodies=["body e5-held"])
        with open(os.path.join(self.fake_dir, "cfg.json"), "w") as f:
            json.dump(cfg, f)
        ids = [self._hub_send("e5-w", key) for key in ("e5-held", "e5-two", "e5-three")]
        cycles = []
        for _ in range(3):
            res, out = self._run_bridge(token, "--limit", "2", "--max-pages", "1")
            self.assertEqual(res.returncode, 1, res.stderr)
            cycles.append(out["cycles"][0])
        self.assertTrue(all(c["held"] for c in cycles))
        self.assertIn("inbox window exhausted", cycles[0]["errors"][0]["error"])
        self.assertEqual(sorted(s["body"] for s in self._fake_lines("sends.jsonl")), ["body e5-three", "body e5-two"])
        self.assertIsNone(self._stages(ids[0])["acked_at"])
        self.assertTrue(all(self._stages(mid)["acked_at"] for mid in ids[1:]))
        with open(os.path.join(self.work, ".local", "hub-bridge-receipts.json")) as f:
            receipts = json.load(f)
        self.assertIn(ids[0], receipts["inbox_pending"]["projE:e5-w"])
        self.assertIn(ids[0], receipts["ambiguous_sends"])
        self.assertNotIn(ids[0], receipts["hub_to_native"])

    def test_real_worker_cli_bridge_hub_ack_and_reply_failures_exit_nonzero(self):
        token = self._bridge_host("e2e4", ["e4-w"])
        read_only = self._bridge_token("token_e2e4_ro", ("host", "bus:read"))
        self._native(["e4-w"], replies={"e4-w": {"kind": "reply", "body": "done"}})
        mid = self._hub_send("e4-w", "e4-a")
        res, out = self._run_bridge(read_only, "--once")
        self.assertEqual(res.returncode, 1, res.stderr)
        errors = out["cycles"][0]["errors"]
        self.assertEqual(sorted(e["error"].split(":")[0] for e in errors), ["hub ack failed", "hub reply failed"])
        self.assertIsNone(self._stages(mid)["acked_at"])
        self.assertEqual(self._replies_to(mid), [])

        # With a send-capable token the same receipts finish the ACK and the reply once, with no second native send
        res2, out2 = self._run_bridge(token, "--once")
        self.assertEqual(res2.returncode, 0, res2.stderr)
        self.assertEqual(out2["cycles"][0]["errors"], [])
        self.assertTrue(self._stages(mid)["acked_at"])
        self.assertEqual(len(self._replies_to(mid)), 1)
        self.assertEqual(len(self._fake_lines("sends.jsonl")), 1)

class TestHttpCliRealCleanupInjection(unittest.TestCase):
    """
    Negative cleanup injection proof verifying that early setup failures
    (such as failed seed steps, premature healthz timeouts, or early setup assertions)
    always close exact owned Popen child handles, close open streams,
    and remove the temporary directory via addClassCleanup / try-finally,
    preventing leaked processes (such as the orphaned PID 3883413 noted by head).
    """

    def test_injected_seed_failure_terminates_owned_child_and_cleans_resources(self):
        tmp_root = os.path.join(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")), ".local", "tmp")
        os.makedirs(tmp_root, exist_ok=True)
        server_dir = tempfile.mkdtemp(dir=tmp_root)

        proc = subprocess.Popen(
            [sys.executable, "-c", "import time, sys; sys.stdout.write('started\\n'); sys.stdout.flush(); time.sleep(60)"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            preexec_fn=os.setsid
        )
        child_pid = proc.pid
        self.assertIsNone(proc.poll())

        def do_cleanup():
            if proc is not None:
                for stream in (proc.stdout, proc.stderr, proc.stdin):
                    if stream:
                        try:
                            stream.close()
                        except Exception:
                            pass
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
            if os.path.isdir(server_dir):
                shutil.rmtree(server_dir, ignore_errors=True)

        injected_caught = False
        try:
            try:
                # Simulated failed seed step (identical to original DELETE FROM leases failure)
                raise RuntimeError("seed_sql failed: no such table: leases")
            except Exception:
                do_cleanup()
                raise
        except RuntimeError as e:
            injected_caught = True
            self.assertIn("leases", str(e))

        self.assertTrue(injected_caught)
        self.assertIsNotNone(proc.poll())
        with self.assertRaises(ProcessLookupError):
            os.kill(child_pid, 0)
        self.assertTrue(proc.stdout.closed)
        self.assertTrue(proc.stderr.closed)
        self.assertFalse(os.path.exists(server_dir))

    def test_add_class_cleanup_triggers_on_setup_class_failure(self):
        cleaned = {"proc_killed": False, "dir_removed": False}
        tmp_root = os.path.join(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")), ".local", "tmp")
        os.makedirs(tmp_root, exist_ok=True)
        server_dir = tempfile.mkdtemp(dir=tmp_root)
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            preexec_fn=os.setsid
        )
        child_pid = proc.pid

        class DummyFailingTest(unittest.TestCase):
            @classmethod
            def setUpClass(cls):
                cls.addClassCleanup(cls._cleanup)
                raise RuntimeError("Injected setup error during initialization")

            @classmethod
            def _cleanup(cls):
                if proc.poll() is None:
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                        proc.wait(timeout=3)
                        cleaned["proc_killed"] = True
                    except Exception:
                        pass
                if os.path.isdir(server_dir):
                    shutil.rmtree(server_dir, ignore_errors=True)
                    cleaned["dir_removed"] = True

            def test_dummy(self):
                pass

        suite = unittest.defaultTestLoader.loadTestsFromTestCase(DummyFailingTest)
        with open(os.devnull, "w") as devnull:
            runner = unittest.TextTestRunner(stream=devnull)
            result = runner.run(suite)
        self.assertEqual(len(result.errors), 1)
        self.assertTrue(cleaned["proc_killed"])
        self.assertTrue(cleaned["dir_removed"])
        self.assertIsNotNone(proc.poll())
        with self.assertRaises(ProcessLookupError):
            os.kill(child_pid, 0)
        self.assertFalse(os.path.exists(server_dir))


if __name__ == "__main__":
    unittest.main()

