"""Exercise the real affected runner without suites, credentials or admission."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
EARLY_SELECTORS = (
    "APLEXER_TAG", "APLEXER_WORKSPACE", "HUB_WORKER_CONFIG_FILE",
    "HUB_CONFIG_FILE", "HUB2_CONFIG",
)
CHILD_SELECTORS = EARLY_SELECTORS + (
    "APLEXER_TEST_SELECTOR", "HUB_TEST_SELECTOR", "HUB2_TEST_SELECTOR",
    "AGENT_HUB_TEST_SELECTOR", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
    "XDG_STATE_HOME", "XDG_RUNTIME_DIR", "GH_CONFIG_DIR",
)
FIELDS = CHILD_SELECTORS + (
    "ISOLATION_SENTINEL", "HOME", "TMPDIR", "AGENT_HUB_HEAVY_TEST_HELD",
    "AFFECTED_SENTINEL", "AFFECTED_PYTEST", "AFFECTED_HOME",
)
# Only this fixed list of synthetic values is recorded; never dump the environment.
RECORD = (
    "import json, os\nfrom pathlib import Path\n"
    f"fields = {FIELDS!r}\n"
    "def record(phase):\n"
    "    Path(os.environ['ISOLATION_RECORDS'], phase + '.json').write_text(\n"
    "        json.dumps({key: os.environ.get(key) for key in fields}))\n"
)


class AffectedRunnerIsolationTests(unittest.TestCase):
    def setUp(self):
        parent = ROOT / ".local" / "tmp"
        parent.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="runner-isolation-", dir=parent)
        self.addCleanup(self.temp.cleanup)
        self.checkout = Path(self.temp.name)
        for name in ("scripts/ci", "bin", "records", "scratch", "parent-home"):
            (self.checkout / name).mkdir(parents=True)
        shutil.copyfile(ROOT / "scripts/test-affected-local.sh",
                        self.checkout / "scripts/test-affected-local.sh")
        self.write("scripts/ci/affected_tests.py", RECORD +
                   "record('planner')\nprint(json.dumps(dict(broad=[], vitest=[], "
                   "pytest=['synthetic_test.py'], workflows=0, typecheck=0, build=0)))\n")
        self.write("scripts/local-dependencies.py", "# No dependency installation.\n")
        self.write("scripts/ci/scope_guard.py", "# Synthetic checkout has no source changes.\n")
        self.write("bin/git", "#!/bin/sh\n"
                   "case \"$*\" in\n"
                   "'cat-file -e d8740aaca3944a295680177a40148bb00accea78^{commit}'|"
                   "'merge-base --is-ancestor d8740aaca3944a295680177a40148bb00accea78 HEAD') exit 0;;\n"
                   "*) exit 99;;\nesac\n", executable=True)
        self.write("scripts/capped-test-run.sh", "#!/bin/bash\n"
                   "[ \"$1\" = -- ] || exit 99\nshift\n"
                   "export AGENT_HUB_HEAVY_TEST_HELD=1\nexec \"$@\"\n", executable=True)
        self.write("bin/uv", f"#!{sys.executable}\n" + RECORD +
                   "import sys\n"
                   "if sys.argv[1:] == ['sync', '--frozen']:\n    pass\n"
                   "elif sys.argv[1:8] == ['run', '--frozen', '--with', 'pytest', 'python', '-m', 'pytest']:\n"
                   "    record('python-child')\n"
                   "else:\n    sys.exit(99)\n", executable=True)
        # Start from an allowlist, so no real worker context or credential path
        # can reach even the synthetic planner or fake tools.
        self.env = {
            "PATH": str(self.checkout / "bin") + os.pathsep + os.defpath,
            "HOME": str(self.checkout / "parent-home"),
            "TMPDIR": str(self.checkout / "scratch"),
            "PYTHONUSERBASE": str(self.checkout / "userbase"),
            "DTC_AUTH_HOME": str(self.checkout / "unused-auth"),
            "UV_CACHE_DIR": str(self.checkout / "unused-cache"),
            "ISOLATION_RECORDS": str(self.checkout / "records"),
            "ISOLATION_SENTINEL": "unrelated-kept",
            "AFFECTED_SENTINEL": "affected-kept",
            "AGENT_HUB_HEAVY_TEST_DIR": str(self.checkout / "unused-admission"),
        }
        self.env.update({key: "synthetic-selector" for key in CHILD_SELECTORS})

    def write(self, name, body, *, executable=False):
        path = self.checkout / name
        path.write_text(body)
        if executable:
            path.chmod(0o700)

    def run_runner(self, *args):
        parent_before = dict(os.environ)
        supplied_before = self.env.copy()
        result = subprocess.run(
            ["bash", "scripts/test-affected-local.sh", *args], cwd=self.checkout,
            env=self.env, capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(dict(os.environ), parent_before)
        self.assertEqual(self.env, supplied_before)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def read(self, phase):
        return json.loads((self.checkout / "records" / (phase + ".json")).read_text())

    def assert_selectors_absent(self, record, selectors):
        for key in selectors:
            with self.subTest(selector=key):
                self.assertIsNone(record[key])
        self.assertEqual(record["ISOLATION_SENTINEL"], "unrelated-kept")
        self.assertEqual(record["AFFECTED_SENTINEL"], "affected-kept")

    def test_dry_run_clears_context_before_planning(self):
        self.run_runner("--dry-run")
        planner = self.read("planner")
        self.assert_selectors_absent(planner, EARLY_SELECTORS)
        self.assertEqual(planner["HOME"], self.env["HOME"])
        self.assertEqual(planner["TMPDIR"], self.env["TMPDIR"])
        self.assertFalse((self.checkout / "records/python-child.json").exists())

    def test_python_child_scrubs_worker_context_and_keeps_admission(self):
        self.run_runner()
        self.assert_selectors_absent(self.read("planner"), EARLY_SELECTORS)
        child = self.read("python-child")
        self.assert_selectors_absent(child, CHILD_SELECTORS)
        self.assertEqual(child["AGENT_HUB_HEAVY_TEST_HELD"], "1")
        self.assertEqual(child["AFFECTED_PYTEST"], "synthetic_test.py")
        self.assertEqual(child["HOME"], child["AFFECTED_HOME"])
        run_tmp = Path(child["TMPDIR"])
        self.assertEqual(run_tmp.parent, self.checkout / "scratch")
        self.assertEqual(Path(child["HOME"]), run_tmp / "home")
        self.assertNotEqual(child["HOME"], self.env["HOME"])
        self.assertFalse(run_tmp.exists(), "runner must clean up its own temporary HOME")


if __name__ == "__main__":
    unittest.main()
