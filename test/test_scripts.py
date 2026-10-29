#!/usr/bin/env python3
import json
import unittest
import subprocess
import os
import sys
import importlib.util
import tempfile
import shutil
import textwrap

class TestScripts(unittest.TestCase):
    def setUp(self):
        self.original_cwd = os.getcwd()
        # Find path relative to current or absolute
        self.deploy_script = os.path.abspath(os.path.join("scripts", "deploy.sh"))
        self.usage_script = os.path.abspath(os.path.join("scripts", "check-usage.py"))
        
        spec = importlib.util.spec_from_file_location("check_usage_mod", self.usage_script)
        self.check_usage_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.check_usage_mod)

        # All temp dirs under .local/tmp
        self.local_tmp = os.path.abspath(os.path.join(".local", "tmp"))
        os.makedirs(self.local_tmp, exist_ok=True)

    def test_deploy_no_approval_fails(self):
        result = subprocess.run([self.deploy_script], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("requires explicit root and Alexey approval", result.stderr + result.stdout)

    def test_deploy_unknown_args_fails(self):
        result = subprocess.run([self.deploy_script, "--some-unknown-arg"], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Unknown argument", result.stderr + result.stdout)

    def test_deploy_missing_config_fails(self):
        # Run from an empty arbitrary directory
        with tempfile.TemporaryDirectory(dir=self.local_tmp) as tmpdir:
            result = subprocess.run([self.deploy_script, "--dry-run"], cwd=tmpdir, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("wrangler config not found", result.stderr + result.stdout)

    def test_deploy_dry_run_actual_execution(self):
        # We need a fake npx that actually simulates wrangler for --dry-run
        # But NEVER for real --i-have-approval
        with tempfile.TemporaryDirectory(dir=self.local_tmp) as tmpdir:
            # Setup a fake project
            os.chdir(tmpdir)
            with open("wrangler.jsonc", "w") as f:
                f.write("{}")
            os.makedirs("node_modules", exist_ok=True)
            
            # Create a fake npx
            fake_npx = os.path.join(tmpdir, "npx")
            with open(fake_npx, "w") as f:
                f.write(textwrap.dedent("""\
                #!/usr/bin/env bash
                if [ "$1" = "--no-install" ] && [ "$2" = "wrangler" ] && [ "$3" = "deploy" ]; then
                    if [ "$4" = "--dry-run" ]; then
                        echo "Fake wrangler dry-run success"
                        mkdir -p .local/buildout
                        exit 0
                    else
                        echo "FATAL: fake npx received deployment without --dry-run" >&2
                        exit 1
                    fi
                fi
                echo "Unknown npx command: $@" >&2
                exit 1
                """))
            os.chmod(fake_npx, 0o755)

            # Prepend fake npx to PATH
            env = os.environ.copy()
            env["PATH"] = f"{tmpdir}:{env['PATH']}"

            result = subprocess.run([self.deploy_script, "--dry-run"], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0)
            self.assertIn("Fake wrangler dry-run success", result.stdout)
            self.assertTrue(os.path.exists(".local/evidence.log"))
            self.assertTrue(os.path.exists(".local/buildout"))
            self.assertFalse(os.path.exists("evidence.log"))
            
            # Check permissions
            stat = os.stat(".local/evidence.log")
            self.assertEqual(stat.st_mode & 0o777, 0o600)
            
            with open(".local/evidence.log", "r") as f:
                evidence = f.read()
                self.assertIn("Fake wrangler dry-run success", evidence)

            os.chdir(self.original_cwd)

    def test_deploy_executable_failure(self):
        # Test what happens when npx is missing
        with tempfile.TemporaryDirectory(dir=self.local_tmp) as tmpdir:
            os.chdir(tmpdir)
            with open("wrangler.jsonc", "w") as f:
                f.write("{}")

            env = os.environ.copy()
            # Provide a minimal PATH so bash can be found but not npx
            # Assuming npx is not in /bin or /usr/bin but usually in /usr/local/bin or node_modules/.bin
            env["PATH"] = "/bin:/usr/bin"
            
            result = subprocess.run([self.deploy_script, "--dry-run"], env=env, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("npx not found", result.stderr + result.stdout)
            
            os.chdir(self.original_cwd)

    def test_deploy_actual_deployment_never_fake_npx(self):
        # Ensure that if --i-have-approval is passed, fake npx blows up
        # This protects against real deploy script silently succeeding with a mock
        with tempfile.TemporaryDirectory(dir=self.local_tmp) as tmpdir:
            os.chdir(tmpdir)
            with open("wrangler.jsonc", "w") as f:
                f.write("{}")

            fake_npx = os.path.join(tmpdir, "npx")
            with open(fake_npx, "w") as f:
                f.write(textwrap.dedent("""\
                #!/usr/bin/env bash
                echo "FATAL: Mock invoked for real deployment" >&2
                exit 1
                """))
            os.chmod(fake_npx, 0o755)
            
            env = os.environ.copy()
            env["PATH"] = f"{tmpdir}:{env['PATH']}"

            result = subprocess.run([self.deploy_script, "--i-have-approval"], env=env, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("FATAL: Mock invoked for real deployment", result.stderr)

            os.chdir(self.original_cwd)

    def test_check_usage_py(self):
        result = subprocess.run([self.usage_script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertIn("Agent Hub Usage Estimator", result.stdout)

    def test_check_usage_missing_key(self):
        estimates = self.check_usage_mod.ESTIMATES.copy()
        del estimates["worker_requests_per_day"]
        with self.assertRaises(SystemExit) as e:
            self.check_usage_mod.check_usage(estimates)
        self.assertEqual(e.exception.code, 1)

    def test_check_usage_negative(self):
        estimates = self.check_usage_mod.ESTIMATES.copy()
        estimates["worker_requests_per_day"] = -10
        with self.assertRaises(SystemExit) as e:
            self.check_usage_mod.check_usage(estimates)
        self.assertEqual(e.exception.code, 1)

    def test_check_usage_nonfinite(self):
        estimates = self.check_usage_mod.ESTIMATES.copy()
        estimates["worker_requests_per_day"] = float('inf')
        with self.assertRaises(SystemExit) as e:
            self.check_usage_mod.check_usage(estimates)
        self.assertEqual(e.exception.code, 1)

    def test_check_usage_threshold_alert(self):
        estimates = self.check_usage_mod.ESTIMATES.copy()
        # 100,000 is the limit. 70,000 triggers alert. We have 1.5x conservative multiplier.
        # So estimate >= (70000 / 1.5) = 46666.67 triggers alert.
        estimates["worker_requests_per_day"] = 50000 
        with self.assertRaises(SystemExit) as e:
            self.check_usage_mod.check_usage(estimates)
        self.assertEqual(e.exception.code, 1)

    def test_check_usage_exact_boundary(self):
        estimates = self.check_usage_mod.ESTIMATES.copy()
        limit = self.check_usage_mod.LIMITS["worker_requests_per_day"] # 100_000
        # Exactly 70% threshold (limit * 0.7) / 1.5
        exact_threshold = (limit * self.check_usage_mod.ALERT_THRESHOLD) / 1.5
        estimates["worker_requests_per_day"] = exact_threshold
        with self.assertRaises(SystemExit) as e:
            self.check_usage_mod.check_usage(estimates)
        self.assertEqual(e.exception.code, 1)

    def test_check_usage_below_boundary(self):
        estimates = self.check_usage_mod.ESTIMATES.copy()
        limit = self.check_usage_mod.LIMITS["worker_requests_per_day"] # 100_000
        # Just below 70% threshold
        below_threshold = ((limit * self.check_usage_mod.ALERT_THRESHOLD) / 1.5) - 0.01
        estimates["worker_requests_per_day"] = below_threshold
        # Should not raise exception
        result = self.check_usage_mod.check_usage(estimates)
        self.assertEqual(result, 0)
        
    def tearDown(self):
        os.chdir(self.original_cwd)


class TestCappedTestRun(unittest.TestCase):
    """Intake 0689ce9c AC2: host-wide cap on concurrent heavy test runs."""

    def setUp(self):
        self.original_cwd = os.getcwd()
        self.capped = os.path.abspath(os.path.join("scripts", "capped-test-run.sh"))
        self.local_tmp = os.path.abspath(os.path.join(".local", "tmp"))
        os.makedirs(self.local_tmp, exist_ok=True)

    def run_capped(self, *args, env_extra=None):
        env = dict(os.environ)
        if env_extra:
            env.update(env_extra)
        return subprocess.run([self.capped, *args], capture_output=True,
                              text=True, timeout=120, env=env)

    def test_rejects_invalid_args_and_missing_command(self):
        with tempfile.TemporaryDirectory(dir=self.local_tmp) as tmpdir:
            env = {"AGENT_HUB_HEAVY_TEST_DIR": os.path.join(tmpdir, "heavy")}
            self.assertEqual(self.run_capped("--slots", "0", "--", "true", env_extra=env).returncode, 2)
            self.assertEqual(self.run_capped("--wait", "soon", "--", "true", env_extra=env).returncode, 2)
            self.assertEqual(self.run_capped("--slots", "1", "--wait", "5", env_extra=env).returncode, 2)

    def test_passes_command_through_with_its_exit_code(self):
        with tempfile.TemporaryDirectory(dir=self.local_tmp) as tmpdir:
            env = {"AGENT_HUB_HEAVY_TEST_DIR": os.path.join(tmpdir, "heavy")}
            ok = self.run_capped("--slots", "2", "--wait", "30", "--",
                                 "bash", "-c", "echo CAPPED-OK", env_extra=env)
            self.assertEqual(ok.returncode, 0)
            self.assertIn("CAPPED-OK", ok.stdout)
            failing = self.run_capped("--", "bash", "-c", "exit 7", env_extra=env)
            self.assertEqual(failing.returncode, 7)

    def test_single_slot_serializes_parallel_runs(self):
        import time
        with tempfile.TemporaryDirectory(dir=self.local_tmp) as tmpdir:
            env = {"AGENT_HUB_HEAVY_TEST_DIR": os.path.join(tmpdir, "heavy")}
            stamp = os.path.join(tmpdir, "order")
            script = ("echo start-$(date +%%s%%N) >> '%s'; sleep 3; echo end-$(date +%%s%%N) >> '%s'"
                      % (stamp, stamp))
            procs = [subprocess.Popen([self.capped, "--slots", "1", "--wait", "60", "--",
                                       "bash", "-c", script],
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                      env={**os.environ, **env})
                     for _ in range(2)]
            deadline = time.monotonic() + 90
            try:
                for proc in procs:
                    remaining = deadline - time.monotonic()
                    self.assertGreater(remaining, 0, "capped runs did not finish")
                    proc.communicate(timeout=remaining)
                self.assertEqual([p.returncode for p in procs], [0, 0])
            finally:
                for proc in procs:
                    if proc.poll() is None:
                        proc.kill()
                        proc.communicate(timeout=10)
            with open(stamp) as handle:
                lines = [line.strip() for line in handle.read().splitlines()]
            self.assertEqual(len(lines), 4)
            # Serialized: first end precedes second start; parallel would interleave starts.
            self.assertEqual([line.split("-")[0] for line in lines],
                             ["start", "end", "start", "end"])

    def test_descendant_keeps_slot_after_parent_exits(self):
        import time
        with tempfile.TemporaryDirectory(dir=self.local_tmp) as tmpdir:
            env = {**os.environ, "AGENT_HUB_HEAVY_TEST_DIR": os.path.join(tmpdir, "heavy")}
            child_pid = os.path.join(tmpdir, "child")
            parent = subprocess.Popen(
                [self.capped, "--slots", "1", "--", "bash", "-c",
                 'sleep 2 & echo $! > "$1"; exit 0', "bash", child_pid],
                env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                self.assertEqual(parent.wait(timeout=15), 0)
                held = self.run_capped("--slots", "1", "--wait", "0", "--", "true", env_extra=env)
                self.assertEqual(held.returncode, 3)
                time.sleep(2.1)
                free = self.run_capped("--slots", "1", "--wait", "0", "--", "true", env_extra=env)
                self.assertEqual(free.returncode, 0)
            finally:
                if parent.poll() is None:
                    parent.kill()
                    parent.wait(timeout=10)

    def test_two_slots_allow_two_holders_and_hold_third(self):
        with tempfile.TemporaryDirectory(dir=self.local_tmp) as tmpdir:
            env = {**os.environ, "AGENT_HUB_HEAVY_TEST_DIR": os.path.join(tmpdir, "heavy")}
            holders = [subprocess.Popen([self.capped, "--slots", "2", "--", "bash", "-c",
                         'echo ready; read -r line'], env=env, stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                       for _ in range(2)]
            try:
                for holder in holders:
                    self.assertEqual(holder.stdout.readline().strip(), "ready")
                held = self.run_capped("--slots", "2", "--wait", "0", "--", "true", env_extra=env)
                self.assertEqual(held.returncode, 3)
            finally:
                for holder in holders:
                    holder.communicate("done\n", timeout=10)
            free = self.run_capped("--slots", "2", "--wait", "0", "--", "true", env_extra=env)
            self.assertEqual(free.returncode, 0)

    def test_command_is_marked_as_holding_a_slot(self):
        # 736ba282 AC6h: auto-acquiring runners nested under the wrapper must
        # not request a second slot (deadlock once every slot is held).
        with tempfile.TemporaryDirectory(dir=self.local_tmp) as tmpdir:
            env = {"AGENT_HUB_HEAVY_TEST_DIR": os.path.join(tmpdir, "heavy")}
            marked = self.run_capped("--", "bash", "-c", 'echo "held=$AGENT_HUB_HEAVY_TEST_HELD"',
                                     env_extra=env)
            self.assertEqual(marked.returncode, 0, marked.stderr)
            self.assertIn("held=1", marked.stdout)

    def test_waiter_blocks_without_polling_and_runs_when_slot_frees(self):
        with tempfile.TemporaryDirectory(dir=self.local_tmp) as tmpdir:
            env = {**os.environ, "AGENT_HUB_HEAVY_TEST_DIR": os.path.join(tmpdir, "heavy")}
            holder = subprocess.Popen([self.capped, "--slots", "1", "--", "bash", "-c",
                                       'echo ready; read -r line'], env=env, stdin=subprocess.PIPE,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            waiter = None
            try:
                self.assertEqual(holder.stdout.readline().strip(), "ready")
                waiter = subprocess.Popen([self.capped, "--slots", "1", "--wait", "60", "--",
                                           "bash", "-c", "echo WAITER-RAN"], env=env,
                                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                notice = waiter.stderr.readline()
                self.assertIn("heavy-test slots busy", notice)
                # A blocked waiter sits in flock, not in a sleep loop.
                import time
                time.sleep(1.0)
                children = subprocess.run(["pgrep", "-P", str(waiter.pid), "-x", "sleep"],
                                          capture_output=True, text=True).stdout.split()
                self.assertEqual(children, [])
                holder.communicate("done\n", timeout=10)
                out, _ = waiter.communicate(timeout=20)
                self.assertEqual(waiter.returncode, 0)
                self.assertIn("WAITER-RAN", out)
            finally:
                for proc in (holder, waiter):
                    if proc is not None and proc.poll() is None:
                        proc.kill()
                        proc.wait(timeout=10)

    def test_vitest_global_setup_skips_held_ci_windows_and_opt_out(self):
        script = ("import('./test/heavy-slot.global-setup.mjs').then(m => console.log(JSON.stringify(["
                  "m.heavySlotWanted({}, 'linux'),"
                  "m.heavySlotWanted({AGENT_HUB_HEAVY_TEST_HELD: '1'}, 'linux'),"
                  "m.heavySlotWanted({CI: 'true'}, 'linux'),"
                  "m.heavySlotWanted({AGENT_HUB_HEAVY_TEST_AUTO: '0'}, 'linux'),"
                  "m.heavySlotWanted({}, 'win32')])))")
        result = subprocess.run(["node", "--input-type=module", "-e", script],
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), [True, False, False, False, False])

    def test_crash_releases_slot_without_stale_files_or_deletion(self):
        with tempfile.TemporaryDirectory(dir=self.local_tmp) as tmpdir:
            env = {"AGENT_HUB_HEAVY_TEST_DIR": os.path.join(tmpdir, "heavy")}
            crashed = self.run_capped("--slots", "1", "--", "bash", "-c", "kill -KILL $$", env_extra=env)
            self.assertEqual(crashed.returncode, 137)
            recovered = self.run_capped("--slots", "1", "--wait", "0", "--", "true", env_extra=env)
            self.assertEqual(recovered.returncode, 0, recovered.stderr)

    def test_full_slot_times_out_and_different_home_cannot_bypass_it(self):
        import time
        with tempfile.TemporaryDirectory(dir=self.local_tmp) as tmpdir:
            env = dict(os.environ, AGENT_HUB_HEAVY_TEST_DIR=os.path.join(tmpdir, "heavy"))
            ready = os.path.join(tmpdir, "ready")
            release = os.path.join(tmpdir, "release")
            program = ("from pathlib import Path; import os,sys,time; os.close(9); "
                       "Path(sys.argv[1]).touch(); "
                       "exec('while not Path(sys.argv[2]).exists(): time.sleep(.02)')")
            child = subprocess.Popen([self.capped, "--slots", "1", "--", sys.executable,
                                      "-c", program, ready, release], env=env)
            try:
                deadline = time.monotonic() + 10
                while not os.path.exists(ready):
                    self.assertIsNone(child.poll())
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(.02)
                held = self.run_capped("--slots", "1", "--wait", "0", "--", "true",
                                       env_extra={**env, "HOME": os.path.join(tmpdir, "isolated-home")})
                self.assertEqual(held.returncode, 3, held.stderr)
                self.assertIsNone(child.poll(), "Contender must leave holder running")
                child.terminate()  # Interrupt only this fixture's wrapper.
                still_held = self.run_capped("--slots", "1", "--wait", "0", "--", "true", env_extra=env)
                self.assertEqual(still_held.returncode, 3, still_held.stderr)
                self.assertIsNone(child.poll(), "Interrupted wrapper must retain the child's slot")
            finally:
                with open(release, "w") as marker:
                    marker.write("release")
                child.wait(timeout=10)
            self.assertEqual(child.returncode, 143)
            self.assertEqual(self.run_capped("--slots", "1", "--wait", "0", "--", "true",
                                            env_extra=env).returncode, 0)

    def tearDown(self):
        os.chdir(self.original_cwd)

if __name__ == '__main__':
    unittest.main()
