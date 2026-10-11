#!/usr/bin/env python3
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
            self.assertTrue(os.path.exists("evidence.log"))
            
            with open("evidence.log", "r") as f:
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

if __name__ == '__main__':
    unittest.main()
