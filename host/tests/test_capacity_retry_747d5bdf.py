"""Capacity-hold retry + clean-exit guard (intake 747d5bdf).

A capacity-gate refusal for a heavy step is a HOLD, not a code failure:
the session waits and retries with backoff (or Hub re-places it) instead
of exiting. A session never exits with an uncommitted fix without a Hub
note and an automatically queued follow-up.

Uses private lock roots and synthetic repos only; never generates host
load, never touches the shared heavy-test directory, never needs network
or docker.
"""
from __future__ import annotations

import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
RETRY = REPO / "scripts" / "capped-test-retry.sh"
GUARD = REPO / "scripts" / "require-clean-exit.sh"


def run(argv, env=None, cwd=None, timeout=60):
    return subprocess.run(argv, env=env, cwd=cwd, capture_output=True,
                          text=True, timeout=timeout)


class RetryWrapperTests(unittest.TestCase):
    def copy_retry_with_stub(self, temp: Path, stub_body: str) -> Path:
        """Copy the real retry script beside a stub admission wrapper.

        The retry script resolves its wrapper as <own-dir>/capped-test-run.sh,
        so the copy exercises the real backoff logic hermetically.
        """
        import shutil
        import stat as statmod
        tree = Path(temp) / "scripts"
        tree.mkdir()
        (tree / "capped-test-run.sh").write_text(stub_body)
        (tree / "capped-test-run.sh").chmod(0o755)
        shutil.copyfile(RETRY, tree / "capped-test-retry.sh")
        (tree / "capped-test-retry.sh").chmod(0o755)
        return tree / "capped-test-retry.sh"

    def test_retry_succeeds_after_transient_refusals(self):
        parent = REPO / ".local" / "tmp"
        parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=parent) as temp:
            counter = Path(temp) / "attempts"
            counter.write_text("0")
            probe = Path(temp) / "probe.sh"
            probe.write_text(
                "#!/usr/bin/env bash\n"
                f"n=$(cat {counter})\n"
                f"echo $((n+1)) > {counter}\n"
                "if [ \"$n\" -lt 2 ]; then exit 3; fi\n"
                "exit 0\n")
            probe.chmod(probe.stat().st_mode | stat.S_IEXEC)
            retry = self.copy_retry_with_stub(
                temp, f"#!/usr/bin/env bash\nexec {probe} \"$@\"\n")
            result = run(["bash", str(retry), "--attempts", "5",
                          "--base-delay", "0", "--", "true"])
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(counter.read_text().strip(), "3")
            self.assertIn("capacity hold", result.stderr.lower())

    def test_non_hold_codes_pass_through_without_retry(self):
        parent = REPO / ".local" / "tmp"
        parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=parent) as temp:
            calls = Path(temp) / "calls"
            calls.write_text("0")
            probe = Path(temp) / "probe.sh"
            probe.write_text(
                "#!/usr/bin/env bash\n"
                f"echo $(( $(cat {calls}) + 1 )) > {calls}\n"
                "exit 17\n")
            probe.chmod(probe.stat().st_mode | stat.S_IEXEC)
            retry = self.copy_retry_with_stub(
                temp, f"#!/usr/bin/env bash\nexec {probe} \"$@\"\n")
            result = run(["bash", str(retry), "--attempts", "5",
                          "--base-delay", "0", "--", "true"])
            self.assertEqual(result.returncode, 17)
            self.assertEqual(calls.read_text().strip(), "1")

    def test_exhausted_attempts_exit_three_with_hold_diagnostic(self):
        parent = REPO / ".local" / "tmp"
        parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=parent) as temp:
            retry = self.copy_retry_with_stub(
                temp, "#!/usr/bin/env bash\nexit 3\n")
            result = run(["bash", str(retry), "--attempts", "2",
                          "--base-delay", "0", "--", "true"])
            self.assertEqual(result.returncode, 3)
            self.assertIn("HOLD", result.stderr)
            self.assertIn("followup", result.stderr.lower())


class CleanExitGuardTests(unittest.TestCase):
    def make_repo(self, root: Path) -> Path:
        repo = root / "repo"
        repo.mkdir()
        env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1",
                   GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                   GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
        run(["git", "init", "-q"], cwd=repo, env=env)
        run(["git", "config", "user.email", "t@t"], cwd=repo, env=env)
        run(["git", "config", "user.name", "t"], cwd=repo, env=env)
        (repo / "file.txt").write_text("one\n")
        run(["git", "add", "."], cwd=repo, env=env)
        run(["git", "commit", "-qm", "init"], cwd=repo, env=env)
        return repo

    def test_clean_tree_exits_zero(self):
        parent = REPO / ".local" / "tmp"
        parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=parent) as temp:
            repo = self.make_repo(Path(temp))
            result = run(["bash", str(GUARD), "--repo", str(repo)])
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("clean", result.stdout.lower())

    def test_dirty_tree_refuses_with_note_and_followup_guidance(self):
        parent = REPO / ".local" / "tmp"
        parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=parent) as temp:
            repo = self.make_repo(Path(temp))
            (repo / "file.txt").write_text("modified but uncommitted\n")
            result = run(["bash", str(GUARD), "--intake", "deadbeef-1234",
                          "--repo", str(repo)])
            self.assertEqual(result.returncode, 1)
            self.assertIn("HOLD", result.stderr)
            self.assertIn("hub intake append", result.stderr)
            self.assertIn("followup", result.stderr)

    def test_unpushed_commit_refuses_with_followup_guidance(self):
        parent = REPO / ".local" / "tmp"
        parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=parent) as temp:
            root = Path(temp)
            env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1",
                       GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                       GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
            run(["git", "init", "-q", "--bare", str(root / "origin.git")],
                cwd=root, env=env)
            repo = self.make_repo(root)
            run(["git", "remote", "add", "origin", str(root / "origin.git")],
                cwd=repo, env=env)
            run(["git", "push", "-q", "origin", "HEAD:main"], cwd=repo, env=env)
            run(["git", "fetch", "-q", "origin"], cwd=repo, env=env)
            run(["git", "branch", "--set-upstream-to=origin/main"],
                cwd=repo, env=env)
            (repo / "fix.txt").write_text("reviewed fix, not yet pushed\n")
            run(["git", "add", "."], cwd=repo, env=env)
            run(["git", "commit", "-qm", "fix: unpushed reviewed fix"],
                cwd=repo, env=env)
            result = run(["bash", str(GUARD), "--repo", str(repo)])
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn("unpushed", result.stderr.lower())
            self.assertIn("followup", result.stderr.lower())


class SessionRulesTests(unittest.TestCase):
    def test_preamble_carries_capacity_retry_and_clean_exit(self):
        from agent_hub_host.session_rules import (
            CAPACITY_RETRY_RULE, CLEAN_EXIT_RULE, SESSION_PREAMBLE,
            compose_prompt)
        self.assertIn("capped-test-retry.sh", CAPACITY_RETRY_RULE)
        self.assertIn("exit 3", CAPACITY_RETRY_RULE)
        self.assertIn("HOLD", CAPACITY_RETRY_RULE)
        self.assertIn("require-clean-exit.sh", CLEAN_EXIT_RULE)
        self.assertIn("followup", CLEAN_EXIT_RULE)
        self.assertIn(CAPACITY_RETRY_RULE, SESSION_PREAMBLE)
        self.assertIn(CLEAN_EXIT_RULE, SESSION_PREAMBLE)
        self.assertIn(CAPACITY_RETRY_RULE, compose_prompt("brief"))
        self.assertIn(CLEAN_EXIT_RULE, compose_prompt("brief"))

    def test_full_runner_distinguishes_hold_from_failure(self):
        text = (REPO / "scripts" / "test-full-local.sh").read_text()
        self.assertIn("hold_or_fail", text)
        self.assertIn("HOLD: ", text)
        self.assertIn("capped-test-retry.sh", text)
        self.assertIn("require-clean-exit.sh", text)
        # Pure holds exit 3 so a retry wrapper can tell them apart.
        self.assertIn("exit 3", text)


if __name__ == "__main__":
    unittest.main()
