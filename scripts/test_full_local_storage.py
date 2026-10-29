"""Exercise the real full-suite shell with small command stubs, without installs."""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest


RUNNER = Path(__file__).with_name("test-full-local.sh")
CAPPED_RUNNER = Path(__file__).with_name("capped-test-run.sh")
STUB = r'''
import json, os, pathlib, socket, sys, time
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
if name == "python3" and args and pathlib.Path(args[0]).name == "heavy-test-admission.py":
    os.execv(sys.executable, [sys.executable] + args)
record = pathlib.Path(os.environ["STORAGE_RECORD"])
profile = pathlib.Path(os.environ.get("HUB_CONFIG_FILE") or
                       pathlib.Path(os.environ.get("XDG_CONFIG_HOME", pathlib.Path.home() / ".config")) / "agent-hub/host.json")
with record.open("a") as out:
    out.write(json.dumps({"tool": name, "args": args, "tmp": os.environ.get("TMPDIR"),
                          "home": os.environ.get("HOME"), "cache": os.environ.get("UV_CACHE_DIR"),
                          "cap_dir": os.environ.get("AGENT_HUB_HEAVY_TEST_DIR"),
                          "pools": [os.environ.get(k) for k in ("UV_THREADPOOL_SIZE", "GOMAXPROCS", "NODE_OPTIONS")],
                          # AGENT_HUB_HEAVY_TEST_HELD is the wrapper's non-secret slot marker; the
                          # child must keep it so pytest never takes a second heavy-test slot.
                          "held": os.environ.get("AGENT_HUB_HEAVY_TEST_HELD"),
                          "priority": os.environ.get("AGENT_HUB_HEAVY_TEST_PRIORITY"),
                          "selectors": sorted(k for k in os.environ if (k.startswith(("APLEXER_", "HUB_", "AGENT_HUB_"))
                            or k in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_RUNTIME_DIR", "GH_CONFIG_DIR"))
                            and k != "AGENT_HUB_HEAVY_TEST_HELD"),
                          "profile": str(profile),
                          "hub_env": sorted(k for k in os.environ if k.startswith(("HUB_", "AGENT_HUB_"))
                                            and k != "AGENT_HUB_HEAVY_TEST_HELD")}) + "\n")
if name == "uv" and args[:1] == ["run"] and os.environ.get("CHECK_PROFILE_ISOLATION"):
    profile.parent.mkdir(parents=True, exist_ok=True)
    profile.write_text("written by isolated Python fixture")
if name == "python3" and args[:1] == ["-c"]:
    print("/fixture/user-base")
if name == "npx" and os.environ.get("FAIL_VITEST"):
    sys.exit(7)
if name == "node":
    # Model Chromium's different cwd when opening its shared-memory path.
    os.chdir(record.parent)
    tmp = pathlib.Path(os.environ["TMPDIR"])
    assert tmp.is_absolute(), "Browser TMPDIR must be absolute"
    (tmp / "browser-shared-memory").write_text("owned by this run")
if name == "uv" and args[:1] == ["run"] and os.environ.get("CHECK_SOCKET"):
    with socket.socket(socket.AF_UNIX) as server:
        server.bind(str(pathlib.Path(os.environ["TMPDIR"]) / "fixture.sock"))
if name == "uv" and args[:1] == ["run"] and os.environ.get("CHECK_AUTH_ISOLATION"):
    sys.path.insert(0, os.environ["STORAGE_HOST_SOURCE"])
    from agent_hub_host import config
    config.save_config({"access_token": "synthetic-test-only"})
    assert (pathlib.Path(os.environ["HOME"]) / ".config/agent-hub/host.json").is_file()
if name == "uv" and args[:1] == ["sync"]:
    if os.environ.get("FAIL_SYNC"):
        sys.exit(9)
    if os.environ.get("PAUSE_SYNC"):
        pathlib.Path(str(record) + ".ready").write_text("ready")
        release = pathlib.Path(str(record) + ".release")
        deadline = time.monotonic() + 15
        while not release.exists():
            if time.monotonic() > deadline:
                sys.exit(99)
            time.sleep(.02)
'''


class FullLocalStorageTests(unittest.TestCase):
    def setUp(self):
        base = Path(os.environ.get("TMPDIR", RUNNER.parent.parent / ".local/tmp")).resolve()
        base.mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(prefix="runner-storage-", dir=base))
        self.repo = self.root / "repo"
        scripts = self.repo / "scripts"
        scripts.mkdir(parents=True)
        shutil.copyfile(RUNNER, scripts / RUNNER.name)
        # Intake 0689ce9c: the runner gates heavy phases through the
        # host-wide cap wrapper; fixtures carry the real wrapper so the
        # shell under test resolves the same path it uses in production.
        shutil.copyfile(CAPPED_RUNNER, scripts / CAPPED_RUNNER.name)
        shutil.copyfile(CAPPED_RUNNER.with_name("heavy-test-admission.py"), scripts / "heavy-test-admission.py")
        # copyfile drops modes; the runner execs the wrapper directly.
        (scripts / CAPPED_RUNNER.name).chmod(0o755)
        (scripts / "local-dependencies.py").write_text("# Command stub handles this fixture.\n")
        self.bin = self.root / "bin"
        self.bin.mkdir()
        for name in ("python3", "npm", "npx", "uv", "node"):
            command = self.bin / name
            command.write_text(f"#!{sys.executable}\n" + STUB)
            command.chmod(0o755)
        self.temp = self.root / "shared-temp"
        self.temp.mkdir()
        self.sentinel = self.temp / "preexisting-sibling"
        self.sentinel.write_text("preserve")
        self.children = []

    def tearDown(self):
        # Processes are resources; fixtures remain for the caller's retention.
        for child in self.children:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGKILL)
                child.communicate(timeout=5)

    def start(self, name, runner_args=(), capped=False, **overrides):
        record = self.root / f"{name}.jsonl"
        env = dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}",
                   TMPDIR=str(self.temp), HOME=str(self.root / "original-home"),
                   UV_CACHE_DIR=str(self.root / "shared-uv-cache"), STORAGE_RECORD=str(record),
                   # These commands are stubs, not real heavy suites. Keep
                   # shell-storage fixtures out of the real host semaphore.
                   AGENT_HUB_HEAVY_TEST_DIR=str(self.root / "fixture-heavy-slots"),
                   AGENT_HUB_HEAVY_TEST_SLOTS="2")
        for key in ("SKIP_PHONE_UI", "FAIL_SYNC", "FAIL_VITEST", "PAUSE_SYNC", "CHECK_SOCKET", "CHECK_AUTH_ISOLATION",
                    "CHECK_PROFILE_ISOLATION", "UV_THREADPOOL_SIZE", "GOMAXPROCS", "NODE_OPTIONS",
                    "AGENT_HUB_HEAVY_TEST_HELD"):
            env.pop(key, None)
        env.update(AGENT_HUB_HEAVY_TEST_PRIORITY="normal")
        env.update(overrides)
        command = ["bash", str(self.repo / "scripts" / RUNNER.name), *runner_args]
        if capped:
            command = [str(self.repo / "scripts" / CAPPED_RUNNER.name),
                       "--priority", "final", "--", *command]
        child = subprocess.Popen(command,
                                 cwd=self.repo, env=env, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True, start_new_session=True)
        self.children.append(child)
        return child, record

    def finish(self, child):
        output, _ = child.communicate(timeout=15)
        return child.returncode, output

    def records(self, record):
        return [json.loads(line) for line in record.read_text().splitlines()]

    def wait_ready(self, child, record):
        ready = Path(str(record) + ".ready")
        deadline = time.monotonic() + 10
        while not ready.exists():
            self.assertIsNone(child.poll(), "Runner exited before sync barrier")
            self.assertLess(time.monotonic(), deadline, "Sync barrier timeout")
            time.sleep(.02)
        return Path(next(row["tmp"] for row in self.records(record) if row["tool"] == "uv"))

    def assert_preserved(self):
        self.assertEqual(self.sentinel.read_text(), "preserve")

    def test_outer_cap_is_refused_before_any_phase_or_temp_creation(self):
        child, record = self.start("outer-cap", capped=True,
                                  AGENT_HUB_HEAVY_TEST_SLOTS="1",
                                  AGENT_HUB_HEAVY_TEST_WAIT_S="1")
        code, output = self.finish(child)
        self.assertEqual(code, 2, output)
        self.assertIn("do not wrap", output)
        self.assertIn("test-full-local.sh --priority final", output)
        self.assertFalse(record.exists(), "no test phase should have started")
        self.assertEqual(list(self.temp.iterdir()), [self.sentinel])
        # The rejected runner releases the outer wrapper's only slot.
        child, record = self.start("direct-final", runner_args=("--priority", "final"),
                                  AGENT_HUB_HEAVY_TEST_SLOTS="1",
                                  AGENT_HUB_HEAVY_TEST_WAIT_S="1")
        code, output = self.finish(child)
        self.assertEqual(code, 0, output)
        vitest = next(row for row in self.records(record) if row["tool"] == "npx")
        self.assertEqual(vitest["priority"], "final")
        self.assert_preserved()

    @unittest.skipUnless(os.name == "posix", "POSIX advisory locks required")
    def test_stubbed_runner_is_isolated_from_occupied_ambient_heavy_slots(self):
        import fcntl
        from contextlib import ExitStack
        from unittest.mock import patch
        ambient = self.root / "occupied-ambient-slots"
        ambient.mkdir()
        with ExitStack() as stack:
            for number in range(2):
                lock = stack.enter_context((ambient / f"slot-{number}.lock").open("w"))
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.dict(os.environ, {"AGENT_HUB_HEAVY_TEST_DIR": str(ambient),
                                        "AGENT_HUB_HEAVY_TEST_WAIT_S": "1"}):
                child, record = self.start("ambient-cap-isolation")
                code, output = self.finish(child)
            self.assertEqual(code, 0, output)
            vitest = next(row for row in self.records(record) if row["tool"] == "npx")
            self.assertEqual(vitest["cap_dir"], str(self.root / "fixture-heavy-slots"))
            self.assert_preserved()

    def test_python_discovery_does_not_touch_ambient_profiles(self):
        profile = self.root / "ambient-profile.json"
        profile.write_text("preserve ambient profile")
        child, record = self.start("profile-isolation", CHECK_PROFILE_ISOLATION="1",
                                  HUB_CONFIG_FILE=str(profile), HUB_URL="https://fixture.invalid",
                                  HUB_TOKEN="fixture-only", AGENT_HUB_MANAGED_DIR=str(self.root / "managed"),
                                  XDG_CONFIG_HOME=str(self.root / "ambient-config"))
        code, output = self.finish(child)
        self.assertEqual(code, 0, output)
        self.assertEqual(profile.read_text(), "preserve ambient profile")
        python = next(row for row in self.records(record)
                      if row["tool"] == "uv" and row["args"][0] == "run")
        self.assertEqual(Path(python["profile"]), Path(python["home"]) / ".config/agent-hub/host.json")
        self.assertEqual(python["hub_env"], [])
        self.assertEqual(python["held"], "1", "pytest child keeps the wrapper's slot marker")
        self.assert_preserved()

    def test_early_sync_failure_removes_only_new_run_directory(self):
        child, record = self.start("sync-fail", FAIL_SYNC="1")
        code, output = self.finish(child)
        self.assertEqual(code, 1, output)
        rows = self.records(record)
        run = Path(next(row["tmp"] for row in rows if row["tool"] == "uv"))
        self.assertEqual(run.parent, self.temp)
        self.assertFalse(run.exists())
        self.assertFalse(any(row["tool"] == "uv" and row["args"][0] == "run" for row in rows))
        self.assert_preserved()

    def test_vitest_bounds_background_pools_without_changing_discovery_or_other_steps(self):
        for overrides, expected in (
            ({}, ["1", "1", "--v8-pool-size=1"]),
            ({"NODE_OPTIONS": "--trace-warnings"}, ["1", "1", "--trace-warnings --v8-pool-size=1"]),
            ({"UV_THREADPOOL_SIZE": "2", "GOMAXPROCS": "3", "NODE_OPTIONS": "--v8-pool-size=2 --trace-warnings"},
             ["2", "3", "--v8-pool-size=2 --trace-warnings"]),
            ({"NODE_OPTIONS": "--v8-pool-size 2"}, ["1", "1", "--v8-pool-size 2"]),
        ):
            with self.subTest(overrides=overrides):
                child, record = self.start(f"pools-{len(self.children)}", **overrides)
                code, output = self.finish(child)
                self.assertEqual(code, 0, output)
                rows = self.records(record)
                vitest = next(row for row in rows if row["tool"] == "npx")
                self.assertEqual(vitest["pools"], expected)
                self.assertEqual(vitest["args"], ["--no-install", "vitest", "run", "--configLoader", "runner",
                    "--no-file-parallelism", "--maxWorkers", "1", "--disableConsoleIntercept"])
                inherited = [overrides.get(k) for k in ("UV_THREADPOOL_SIZE", "GOMAXPROCS", "NODE_OPTIONS")]
                self.assertTrue(all(row["pools"] == inherited for row in rows if row["tool"] != "npx"))
                self.assertFalse(Path(vitest["tmp"]).exists())
                self.assert_preserved()

    def test_default_relative_tmpdir_preserves_existing_scratch(self):
        parent = self.repo / ".local/tmp"
        parent.mkdir(parents=True)
        marker = parent / "preexisting-sibling"
        marker.write_text("preserve")
        child, record = self.start("relative-default", TMPDIR="", CHECK_SOCKET="1")
        code, output = self.finish(child)
        self.assertEqual(code, 0, output)
        python = next(row for row in self.records(record)
                      if row["tool"] == "uv" and row["args"][0] == "run")
        relative = Path(python["tmp"])
        self.assertFalse(relative.is_absolute())
        self.assertEqual(relative.parent, Path(".local/tmp"))
        self.assertEqual(Path(python["home"]), self.repo / relative / "home")
        # An absolute socket path here exceeds Linux's 108-byte sun_path.
        self.assertGreater(len(str(self.repo / relative / "fixture.sock").encode()), 108)
        browsers = [row for row in self.records(record) if row["tool"] == "node"]
        self.assertEqual(len(browsers), 2)
        self.assertEqual(browsers[0]["args"], ["--test", "scripts/test-browser-local.test.mjs"])
        self.assertEqual(browsers[1]["args"], ["scripts/test-browser-local.mjs"])
        self.assertTrue(all(Path(row["tmp"]) == self.repo / relative for row in browsers))
        self.assertFalse((self.repo / relative).exists())
        self.assertEqual(marker.read_text(), "preserve")

    def test_vitest_failure_is_aggregated_and_python_home_is_cleaned(self):
        child, record = self.start("vitest-fail", FAIL_VITEST="1")
        code, output = self.finish(child)
        self.assertEqual(code, 1, output)
        self.assertIn("Vitest discovery suite exited with code 7", output)
        rows = self.records(record)
        vitest = next(row for row in rows if row["tool"] == "npx")
        self.assertIn("--no-install", vitest["args"])
        python = next(row for row in rows if row["tool"] == "uv" and row["args"][0] == "run")
        run = Path(python["tmp"])
        self.assertEqual(Path(python["home"]), run / "home")
        self.assertEqual(python["cache"], str(self.root / "shared-uv-cache"))
        self.assertEqual(python["args"][python["args"].index("--basetemp") + 1], str(run / "pytest"))
        self.assertIn("tmp_path_retention_count=0", python["args"])
        self.assertIn("tmp_path_retention_policy=none", python["args"])
        self.assertTrue(any(row["tool"] == "node" for row in rows), "Later suites must still execute")
        self.assertTrue(all(Path(row["tmp"]) == run for row in rows if row["tool"] == "node"))
        self.assertFalse(run.exists())
        self.assert_preserved()

    def test_python_config_writes_cannot_reach_ambient_native_or_explicit_profiles(self):
        ambient = self.root / "ambient-workspace"
        profile = ambient / ".local/hub-worker-auth/11111111-1111-4111-8111-111111111111.json"
        profile.parent.mkdir(parents=True, mode=0o700)
        profile.write_text('{"access_token":"ambient-synthetic-sentinel"}')
        profile.chmod(0o600)
        original = profile.read_bytes()
        for explicit in (False, True):
            with self.subTest(explicit=explicit):
                child, record = self.start(f"auth-{explicit}", CHECK_AUTH_ISOLATION="1",
                    STORAGE_HOST_SOURCE=str(RUNNER.parent.parent / "host"),
                    APLEXER_TAG="hub-11111111-1111-4111-8111-111111111111",
                    APLEXER_WORKSPACE=str(ambient),
                    APLEXER_STATE_DIR=str(ambient / "state"),
                    APLEXER_RUNTIME_DIR=str(ambient / "runtime"),
                    APLEXER_CONFIG=str(ambient / "config.toml"),
                    APLEXER_SESSION_ID="synthetic-session",
                    HUB_WORKTREE_IDENTITY_DIR=str(ambient / "identity"),
                    AGENT_HUB_MANAGED_DIR=str(ambient / "managed"),
                    AGENT_HUB_OPENCODE_AUTH='{"synthetic":"only"}',
                    APLEXER_FUTURE_SELECTOR="synthetic", HUB_FUTURE_SELECTOR="synthetic",
                    AGENT_HUB_FUTURE_SELECTOR="synthetic", GH_CONFIG_DIR=str(ambient / "github"),
                    HUB_CONFIG_FILE=str(profile) if explicit else "",
                    HUB_WORKER_CONFIG_FILE=str(profile) if explicit else "",
                    HUB_URL="https://fixture.invalid", HUB_BASE_URL="https://fixture.invalid",
                    HUB_TOKEN="synthetic-token", HUB_JOB_TOKEN="synthetic-token",
                    XDG_CONFIG_HOME=str(ambient / "config"),
                    XDG_DATA_HOME=str(ambient / "data"), XDG_STATE_HOME=str(ambient / "state"),
                    XDG_RUNTIME_DIR=str(ambient / "runtime"))
                code, output = self.finish(child)
                self.assertEqual(code, 0, output)
                python = next(row for row in self.records(record)
                              if row["tool"] == "uv" and row["args"][0] == "run")
                self.assertEqual(python["selectors"], [])
                self.assertEqual(profile.read_bytes(), original)
                self.assertFalse(Path(python["home"]).exists())

    def test_term_cleans_run_directory_after_active_command_finishes(self):
        child, record = self.start("terminated", PAUSE_SYNC="1")
        run = self.wait_ready(child, record)
        self.assertTrue(run.is_dir())
        child.send_signal(signal.SIGTERM)
        Path(str(record) + ".release").write_text("release")
        code, output = self.finish(child)
        self.assertEqual(code, 143, output)
        self.assertFalse(run.exists())
        self.assert_preserved()

    def test_concurrent_runs_do_not_remove_each_others_temporary_files(self):
        first, first_record = self.start("first", PAUSE_SYNC="1")
        second, second_record = self.start("second", PAUSE_SYNC="1")
        first_run = self.wait_ready(first, first_record)
        second_run = self.wait_ready(second, second_record)
        self.assertNotEqual(first_run, second_run)
        marker = second_run / "active-sibling-run"
        marker.write_text("still running")
        Path(str(first_record) + ".release").write_text("release")
        code, output = self.finish(first)
        self.assertEqual(code, 0, output)
        self.assertFalse(first_run.exists())
        self.assertEqual(marker.read_text(), "still running")
        Path(str(second_record) + ".release").write_text("release")
        code, output = self.finish(second)
        self.assertEqual(code, 0, output)
        self.assertFalse(second_run.exists())
        self.assert_preserved()


if __name__ == "__main__":
    unittest.main()
