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
STUB = r'''
import json, os, pathlib, socket, sys, time
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
record = pathlib.Path(os.environ["STORAGE_RECORD"])
with record.open("a") as out:
    out.write(json.dumps({"tool": name, "args": args, "tmp": os.environ.get("TMPDIR"),
                          "home": os.environ.get("HOME"), "cache": os.environ.get("UV_CACHE_DIR")}) + "\n")
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

    def start(self, name, **overrides):
        record = self.root / f"{name}.jsonl"
        env = dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}",
                   TMPDIR=str(self.temp), HOME=str(self.root / "original-home"),
                   UV_CACHE_DIR=str(self.root / "shared-uv-cache"), STORAGE_RECORD=str(record))
        for key in ("SKIP_PHONE_UI", "FAIL_SYNC", "FAIL_VITEST", "PAUSE_SYNC", "CHECK_SOCKET"):
            env.pop(key, None)
        env.update(overrides)
        child = subprocess.Popen(["bash", str(self.repo / "scripts" / RUNNER.name)],
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
        self.assertEqual(len(browsers), 5)
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
