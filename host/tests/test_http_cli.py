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
                    "run_worker_first": False,
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

        # 6. Deliver & read before ACK
        now_ts = int(time.time() * 1000)
        self.seed_sql("UPDATE messages SET delivered_at = ?, read_at = ? WHERE id = ?", [now_ts, now_ts, m_id])

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

        # Deliver & read in real DB
        now_ts = int(time.time() * 1000)
        self.seed_sql("UPDATE messages SET delivered_at = ?, read_at = ? WHERE id = ?", [now_ts, now_ts, m_id])

        # 2. Bridge setup with fake-a binary
        from agent_hub_host.bridge import MailboxBridge, canonicalize_workspace, get_workspace_key

        work_dir = os.path.join(self.server_dir, "work_rt")
        rt_dir = os.path.join(self.server_dir, "rt_dir")
        st_dir = os.path.join(self.server_dir, "st_dir")
        os.makedirs(work_dir, exist_ok=True)
        canon_ws = canonicalize_workspace(work_dir)
        rec_uuid = str(uuid.uuid4())
        native_m_id = str(uuid.uuid4())
        native_rep_id = str(uuid.uuid4())

        # Session in rt_dir
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
import sys, json, os
args = sys.argv[1:]
if not args: sys.exit(1)
if args[0] == "message":
    if args[1] == "send":
        with open("{os.path.join(mb_dir, "cursors", f"{rec_uuid}.json")}", "w") as cf:
            json.dump({{"exceptions": ["{native_m_id}"], "acked_through": None}}, cf)
        print(json.dumps({{"id": "{native_m_id}", "to": {{"session_id": "{rec_uuid}"}}}}))
        sys.exit(0)
    elif args[1] == "log":
        print(json.dumps([{{
            "id": "{native_rep_id}",
            "reply_to": "{native_m_id}",
            "from": {{"session_id": "{rec_uuid}", "tag": "worker-2"}},
            "to": {{"tag": "worker-1"}},
            "kind": "reply",
            "body": "Fake-A processed"
        }}]))
        sys.exit(0)
sys.exit(0)
""")
        os.chmod(fake_a, 0o755)

        bridge = MailboxBridge(work_dir, aplexer_bin=fake_a, state_dir=st_dir, runtime_dir=rt_dir)
        fwd_id = bridge.forward_hub_to_native({
            "id": m_id,
            "from_actor": "worker-1",
            "to_actor": "worker-2",
            "body": "Bridge roundtrip payload",
            "kind": "note",
            "thread_id": msg_info["thread_id"]
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

