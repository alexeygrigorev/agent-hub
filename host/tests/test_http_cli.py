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
        if sys.platform != 'win32':
            self.assertEqual(stat.S_IMODE(os.stat(self.config_dir).st_mode), 0o700)

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

