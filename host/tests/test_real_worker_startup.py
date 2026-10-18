"""Unit tests for the real worker startup harness and process manager.

Tests verify:
- Fail-fast detection on child termination with actionable log tail
- Monotonic bounded readiness timeout
- Log pressure resilience without PIPE deadlock
- Process group isolation and guaranteed cleanup of child and grandchildren
- Bounded infrastructure-only pre-seed startup retry with structured evidence
- Utility functions (port allocation, CPU affinity, node resolution)
"""

import http.server
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request

from real_worker_harness import (
    RealWorkerHarness,
    WorkerProcessHandle,
    WorkerStartupError,
    build_fixture_worker_ts,
    build_miniflare_script,
    build_wrangler_config,
    find_available_port,
    get_available_cpus,
    read_file_tail,
    resolve_node_binary,
)


class TestRealWorkerStartupHarness(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="test-harness-")
        self.repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_get_available_cpus_and_port(self):
        cpus = get_available_cpus(2)
        self.assertIsInstance(cpus, str)
        self.assertTrue(len(cpus) > 0)
        # Port finding test
        port = find_available_port()
        self.assertIsInstance(port, int)
        self.assertGreater(port, 1024)

    def test_resolve_node_binary(self):
        node_bin = resolve_node_binary()
        self.assertTrue(os.path.exists(node_bin))
        ver = subprocess.check_output([node_bin, "-v"], text=True).strip()
        self.assertTrue(ver.startswith("v"))
        major = int(ver[1:].split(".")[0])
        self.assertGreaterEqual(major, 22)

    def test_resolve_node_binary_refusal_when_under_22(self):
        """Must strictly refuse node versions < 22 without falling back."""
        # Create a mock node binary that reports v20.20.2
        mock_bin = os.path.join(self.tmp_dir, "mock-node-v20")
        with open(mock_bin, "w") as f:
            f.write("#!/bin/sh\necho 'v20.20.2'\n")
        os.chmod(mock_bin, 0o755)

        with self.assertRaises(RuntimeError) as ctx:
            resolve_node_binary(candidates=[mock_bin], include_defaults=False)
        self.assertIn("Wrangler requires Node >= 22", str(ctx.exception))

    def test_read_file_tail_bounded(self):
        log_file = os.path.join(self.tmp_dir, "test.log")
        with open(log_file, "w") as f:
            for i in range(200):
                f.write(f"log line {i}\n")
        tail = read_file_tail(log_file, max_lines=10, max_bytes=1024)
        lines = tail.splitlines()
        self.assertEqual(len(lines), 10)
        self.assertEqual(lines[-1], "log line 199")
        self.assertEqual(lines[0], "log line 190")

    def test_startup_failure_fail_fast(self):
        """Child exiting prematurely should trigger fail-fast without waiting the full timeout."""
        harness = RealWorkerHarness(
            repo_root=self.repo_root,
            server_dir=self.tmp_dir,
            tmp_root=self.tmp_dir,
            readiness_timeout=15.0,  # Deliberately long timeout
            poll_interval=0.05,
        )
        port = find_available_port()
        # Script that prints diagnostic to stderr and exits with 42 after 0.1s
        cmd = [
            sys.executable,
            "-c",
            "import sys, time; time.sleep(0.05); sys.stderr.write('CRITICAL: V8 isolate died\\n'); sys.stderr.flush(); sys.exit(42)",
        ]
        start = time.monotonic()
        handle = harness.launch_command(cmd, port=port, log_name="fail_fast.log")
        try:
            with self.assertRaises(WorkerStartupError) as ctx:
                harness.wait_for_ready(handle, timeout=15.0)
            elapsed = time.monotonic() - start
            self.assertLess(elapsed, 3.0, f"Expected fail-fast (<3s), took {elapsed:.2f}s")
            self.assertEqual(ctx.exception.exit_code, 42)
            self.assertIn("CRITICAL: V8 isolate died", ctx.exception.log_tail)
        finally:
            handle.cleanup()

    def test_readiness_timeout_bounded(self):
        """Process that stays alive but does not serve readiness should time out boundedly."""
        harness = RealWorkerHarness(
            repo_root=self.repo_root,
            server_dir=self.tmp_dir,
            tmp_root=self.tmp_dir,
            readiness_timeout=0.4,
            poll_interval=0.05,
            http_timeout=0.1,
        )
        port = find_available_port()
        # Script that sleeps and never listens
        cmd = [sys.executable, "-c", "import time; time.sleep(10)"]
        start = time.monotonic()
        handle = harness.launch_command(cmd, port=port, log_name="timeout.log")
        try:
            with self.assertRaises(WorkerStartupError) as ctx:
                harness.wait_for_ready(handle)
            elapsed = time.monotonic() - start
            self.assertLess(elapsed, 1.5, f"Expected bounded timeout (<1.5s), took {elapsed:.2f}s")
            self.assertIsNone(ctx.exception.exit_code)
            self.assertIn("failed to become ready", str(ctx.exception))
        finally:
            handle.cleanup()
            self.assertFalse(handle.is_alive())

    def test_log_pressure_no_deadlock(self):
        """Massive log output (>256KB) must not deadlock or block the child process."""
        harness = RealWorkerHarness(
            repo_root=self.repo_root,
            server_dir=self.tmp_dir,
            tmp_root=self.tmp_dir,
            readiness_timeout=10.0,
            poll_interval=0.05,
        )
        port = find_available_port()
        # Script that writes 500KB of logs before starting an HTTP server on the port
        server_code = f"""
import sys, http.server, socketserver
# Flood stdout/stderr with > 256KB to test pipe deadlock resilience
for i in range(5000):
    sys.stdout.write(f"LOG STREAM PRESSURE TEST LINE {{i}} - " + "X" * 100 + "\\n")
sys.stdout.flush()

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, format, *args):
        pass

with socketserver.TCPServer(("127.0.0.1", {port}), Handler) as httpd:
    httpd.serve_forever()
"""
        cmd = [sys.executable, "-c", server_code]
        handle = harness.launch_command(cmd, port=port, log_name="pressure.log")
        try:
            ready = harness.wait_for_ready(handle, timeout=10.0)
            self.assertTrue(ready)
            tail = handle.tail_log(max_lines=10)
            self.assertIn("LOG STREAM PRESSURE TEST LINE", tail)
        finally:
            handle.cleanup()
            self.assertFalse(handle.is_alive())

    def test_process_group_cleanup_including_grandchildren(self):
        """Cleanup must terminate child and any spawned grandchildren (process group)."""
        harness = RealWorkerHarness(
            repo_root=self.repo_root,
            server_dir=self.tmp_dir,
            tmp_root=self.tmp_dir,
        )
        port = find_available_port()
        pid_file = os.path.join(self.tmp_dir, "grandchild.pid")
        # Script spawns a grandchild process that sleeps 30s
        parent_code = f"""
import subprocess, sys, time
proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
with open({json.dumps(pid_file)}, "w") as f:
    f.write(str(proc.pid))
time.sleep(30)
"""
        handle = harness.launch_command([sys.executable, "-c", parent_code], port=port)
        # Wait for grandchild PID to be written
        deadline = time.monotonic() + 5.0
        grandchild_pid = None
        while time.monotonic() < deadline:
            if os.path.exists(pid_file):
                with open(pid_file) as f:
                    content = f.read().strip()
                if content:
                    grandchild_pid = int(content)
                    break
            time.sleep(0.05)
        self.assertIsNotNone(grandchild_pid, "Grandchild PID file not created")

        # Verify grandchild is running
        try:
            os.kill(grandchild_pid, 0)
            grandchild_alive_initially = True
        except ProcessLookupError:
            grandchild_alive_initially = False
        self.assertTrue(grandchild_alive_initially)

        # Cleanup harness handle (which kills the entire process group)
        handle.cleanup()
        self.assertFalse(handle.is_alive())

        # Verify grandchild is also terminated
        deadline = time.monotonic() + 3.0
        grandchild_killed = False
        while time.monotonic() < deadline:
            try:
                os.kill(grandchild_pid, 0)
                time.sleep(0.05)
            except ProcessLookupError:
                grandchild_killed = True
                break
        self.assertTrue(grandchild_killed, f"Grandchild PID {grandchild_pid} was not killed by process group cleanup")

    def test_process_group_cleanup_parent_exited_grandchild_alive(self):
        """When parent process exits (poll() is not None), cleanup must still terminate living grandchildren in the owned group."""
        harness = RealWorkerHarness(
            repo_root=self.repo_root,
            server_dir=self.tmp_dir,
            tmp_root=self.tmp_dir,
        )
        port = find_available_port()
        pid_file = os.path.join(self.tmp_dir, "grandchild_parent_exited.pid")
        # Script spawns a grandchild that sleeps 30s, then parent exits 0 immediately
        parent_code = f"""
import subprocess, sys
proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
with open({json.dumps(pid_file)}, "w") as f:
    f.write(str(proc.pid))
sys.exit(0)
"""
        handle = harness.launch_command([sys.executable, "-c", parent_code], port=port)
        # Wait for parent to exit
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and handle.proc.poll() is None:
            time.sleep(0.05)
        self.assertIsNotNone(handle.proc.poll(), "Parent process did not exit")
        self.assertEqual(handle.proc.poll(), 0)

        # Read grandchild PID
        grandchild_pid = None
        while time.monotonic() < deadline:
            if os.path.exists(pid_file):
                with open(pid_file) as f:
                    content = f.read().strip()
                if content:
                    grandchild_pid = int(content)
                    break
            time.sleep(0.05)
        self.assertIsNotNone(grandchild_pid, "Grandchild PID file not created")

        # Verify grandchild is alive even though parent exited
        self.assertTrue(handle.group_is_alive())
        os.kill(grandchild_pid, 0)

        # Now call handle.cleanup() - it must detect the living group and kill the grandchild
        handle.cleanup()
        self.assertFalse(handle.group_is_alive())

        # Verify grandchild is terminated
        kill_deadline = time.monotonic() + 3.0
        grandchild_killed = False
        while time.monotonic() < kill_deadline:
            try:
                os.kill(grandchild_pid, 0)
                time.sleep(0.05)
            except ProcessLookupError:
                grandchild_killed = True
                break
        self.assertTrue(grandchild_killed, f"Grandchild PID {grandchild_pid} remained alive after cleanup")

    def test_retry_aborts_if_cleanup_cannot_be_verified(self):
        """Startup retry must not proceed if cleanup cannot be verified."""
        harness = RealWorkerHarness(
            repo_root=self.repo_root,
            server_dir=self.tmp_dir,
            tmp_root=self.tmp_dir,
            max_startup_attempts=2,
        )

        def launcher(port: int, log_name: str) -> WorkerProcessHandle:
            h = harness.launch_command([sys.executable, "-c", "import sys; sys.exit(1)"], port=port)

            def bad_cleanup(*args, **kwargs):
                raise RuntimeError("Failed to verify cleanup of worker process: group remains alive")

            h.cleanup = bad_cleanup
            return h

        with self.assertRaises(RuntimeError) as ctx:
            harness.start_with_retry(launcher)
        self.assertIn("Failed to verify cleanup", str(ctx.exception))

    def test_infrastructure_retry_bounded_success_on_attempt_2(self):
        """Infrastructure retry: attempt 1 fails at pre-seed; attempt 2 succeeds with evidence recorded."""
        harness = RealWorkerHarness(
            repo_root=self.repo_root,
            server_dir=self.tmp_dir,
            tmp_root=self.tmp_dir,
            readiness_timeout=5.0,
            poll_interval=0.05,
            max_startup_attempts=2,
        )
        attempt_counter = [0]

        def launcher(port: int, log_name: str) -> WorkerProcessHandle:
            attempt_counter[0] += 1
            if attempt_counter[0] == 1:
                # Attempt 1: exits immediately with error
                cmd = [sys.executable, "-c", "import sys; sys.stderr.write('Transient port collision\\n'); sys.exit(1)"]
            else:
                # Attempt 2: starts working HTTP server
                server_code = f"""
import http.server, socketserver
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, format, *args):
        pass
with socketserver.TCPServer(("127.0.0.1", {port}), Handler) as httpd:
    httpd.serve_forever()
"""
                cmd = [sys.executable, "-c", server_code]
            return harness.launch_command(cmd, port=port, log_name=log_name)

        handle = harness.start_with_retry(launcher)
        try:
            self.assertTrue(handle.is_alive())
            self.assertEqual(attempt_counter[0], 2)
            self.assertEqual(len(harness.retry_history), 1)
            evidence = harness.retry_history[0]
            self.assertEqual(evidence["attempt"], 1)
            self.assertEqual(evidence["exit_code"], 1)
            self.assertIn("Transient port collision", evidence["log_tail"])
        finally:
            handle.cleanup()

    def test_infrastructure_retry_exhausted_raises_with_full_evidence(self):
        """When all attempts fail, raise WorkerStartupError with retry history recorded."""
        harness = RealWorkerHarness(
            repo_root=self.repo_root,
            server_dir=self.tmp_dir,
            tmp_root=self.tmp_dir,
            readiness_timeout=0.3,
            poll_interval=0.05,
            max_startup_attempts=2,
        )

        def launcher(port: int, log_name: str) -> WorkerProcessHandle:
            cmd = [sys.executable, "-c", "import sys; sys.stderr.write('Fatal crash\\n'); sys.exit(13)"]
            return harness.launch_command(cmd, port=port, log_name=log_name)

        with self.assertRaises(WorkerStartupError) as ctx:
            harness.start_with_retry(launcher)

        self.assertEqual(ctx.exception.attempts, 2)
        self.assertEqual(ctx.exception.exit_code, 13)
        self.assertEqual(len(harness.retry_history), 2)
        self.assertEqual(harness.retry_history[0]["attempt"], 1)
        self.assertEqual(harness.retry_history[1]["attempt"], 2)

    def test_cold_actual_worker_positive(self):
        """Build real worker bundle, launch Miniflare under harness, probe /healthz, and clean up."""
        if not os.path.exists(os.path.join(self.repo_root, "src", "bus.ts")):
            raise unittest.SkipTest("Real Worker source pending")

        server_dir = tempfile.mkdtemp(dir=self.tmp_dir, prefix="sess-cold-")
        fixture_ts = build_fixture_worker_ts(self.repo_root)
        with open(os.path.join(server_dir, "fixture_worker.ts"), "w") as f:
            f.write(fixture_ts)

        cfg = build_wrangler_config(port=8787, main_ts="fixture_worker.ts")
        with open(os.path.join(server_dir, "wrangler.jsonc"), "w") as f:
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
        env["TMPDIR"] = self.tmp_dir
        cpu_str = get_available_cpus(2)

        harness = RealWorkerHarness(
            repo_root=self.repo_root,
            server_dir=server_dir,
            tmp_root=self.tmp_dir,
            readiness_timeout=35.0,
            poll_interval=0.2,
            max_startup_attempts=2,
        )

        build_dir = Path(server_dir) / "bundle"
        bundle_path = harness.build_worker(
            config_path=os.path.join(server_dir, "wrangler.jsonc"),
            out_dir=str(build_dir),
            env=env,
            cpu_str=cpu_str,
            node_bin=node_bin,
        )
        self.assertTrue(os.path.exists(bundle_path))

        def launch_worker(port: int, log_name: str) -> WorkerProcessHandle:
            mf_js = os.path.join(server_dir, f"miniflare-{port}.cjs")
            script = build_miniflare_script(self.repo_root, server_dir, str(bundle_path), port)
            with open(mf_js, "w") as f:
                f.write(script)
            cmd = ["taskset", "-c", cpu_str, node_bin, str(mf_js)]
            return harness.launch_command(cmd, port=port, env=env, log_name=log_name)

        handle = harness.start_with_retry(launch_worker)
        try:
            self.assertTrue(handle.is_alive())
            # Real HTTP request to verify worker is actively serving
            req = urllib.request.Request(handle.health_url)
            try:
                with urllib.request.urlopen(req, timeout=5) as resp:
                    status_code = resp.status
            except urllib.error.HTTPError as err:
                status_code = err.code
            self.assertIn(status_code, (200, 401))
        finally:
            handle.cleanup()
            self.assertFalse(handle.is_alive())

    def test_caller_cleanup_proc_surfaces_cleanup_failure_and_preserves_setup_exc(self):
        """Caller teardown (_cleanup_proc) surfaces handle.cleanup errors via try/finally
        without masking already-raised setup/test exceptions.
        """
        import sys
        from tests.test_session_real_worker import TestSessionRealWorker

        class FailingHandle:
            def __init__(self):
                self.cleaned_up = False

            def cleanup(self):
                self.cleaned_up = True
                raise RuntimeError("Process group 9999 still has living processes")

        class DummyCase(TestSessionRealWorker):
            handle = None
            proc = None

        # Case 1: Teardown without prior active exception surfaces cleanup error
        # and guarantees references are cleared.
        h1 = FailingHandle()
        DummyCase.handle = h1
        DummyCase.proc = None
        with self.assertRaisesRegex(RuntimeError, "still has living processes"):
            DummyCase._cleanup_proc()
        self.assertTrue(h1.cleaned_up)
        self.assertIsNone(DummyCase.handle)
        self.assertIsNone(DummyCase.proc)

        # Case 2: Teardown when an exception is already in-flight (setup failure)
        # preserves original setup exception and does not mask it, while still
        # running cleanup and clearing references in finally.
        h2 = FailingHandle()
        DummyCase.handle = h2
        DummyCase.proc = None
        caught = None
        try:
            try:
                raise ValueError("Worker startup failed in setUpClass")
            except Exception:
                DummyCase._cleanup_proc()
                raise
        except Exception as e:
            caught = e

        self.assertIsInstance(caught, ValueError)
        self.assertEqual(str(caught), "Worker startup failed in setUpClass")
        self.assertTrue(h2.cleaned_up)
        self.assertIsNone(DummyCase.handle)
        self.assertIsNone(DummyCase.proc)


if __name__ == "__main__":
    unittest.main()

