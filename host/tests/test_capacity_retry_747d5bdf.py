"""Capacity-hold retry + clean-exit guard (intakes 747d5bdf, 9432731e).

A capacity-gate refusal for a heavy step is a HOLD, not a code failure:
the session waits and retries with backoff (or Hub re-places it) instead
of exiting. A command's own exit 3 is a FAIL and is never retried. A
session never exits with an uncommitted or unrecorded fix without a Hub
note and an automatically queued follow-up, and a clean guard result never
authorizes pushing to main.

Uses private lock roots, a fake sleep clock and synthetic repos only; never
generates host load, never touches the shared heavy-test directory, never
needs network or docker.
"""
from __future__ import annotations

import fcntl
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"
RETRY = SCRIPTS / "capped-test-retry.sh"
CAPPED = SCRIPTS / "capped-test-run.sh"
GUARD = SCRIPTS / "require-clean-exit.sh"
MARKER_ENV = "AGENT_HUB_HEAVY_TEST_REFUSAL_FILE"


def run(argv, env=None, cwd=None, timeout=60):
    return subprocess.run(argv, env=env, cwd=cwd, capture_output=True,
                          text=True, timeout=timeout)


def scratch():
    parent = REPO / ".local" / "tmp"
    parent.mkdir(parents=True, exist_ok=True)
    return tempfile.TemporaryDirectory(dir=parent)


def write_exec(path: Path, body: str) -> Path:
    path.write_text(body)
    path.chmod(0o755)
    return path


class HeavyEnv:
    """Private heavy-test root, fake sleep clock and call counters."""

    def __init__(self, root: Path):
        self.root = root
        self.slots = root / "slots"
        self.slots.mkdir()
        self.sleeps = root / "sleeps"
        self.fake_sleep = write_exec(
            root / "fake-sleep", f"#!/usr/bin/env bash\necho \"$1\" >> {self.sleeps}\n")

    def env(self, **extra):
        env = dict(os.environ, AGENT_HUB_HEAVY_TEST_DIR=str(self.slots),
                   AGENT_HUB_HEAVY_TEST_SLOTS="1", AGENT_HUB_HEAVY_TEST_WAIT_S="0",
                   CAPPED_TEST_RETRY_SLEEP=str(self.fake_sleep), TMPDIR=str(self.root))
        env.pop(MARKER_ENV, None)
        env.update(extra)
        return env

    @contextmanager
    def occupied(self):
        """Hold the only slot so admission genuinely refuses (wait 0)."""
        with (self.slots / "slot-0.lock").open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield

    def recorded_sleeps(self):
        if not self.sleeps.exists():
            return []
        return [int(x) for x in self.sleeps.read_text().split()]


def counting_command(root: Path, name: str, body: str = "exit 0") -> tuple[Path, Path]:
    calls = root / f"{name}.calls"
    script = write_exec(root / f"{name}.sh",
                        f"#!/usr/bin/env bash\necho x >> {calls}\n{body}\n")
    return script, calls


def count(calls: Path) -> int:
    return len(calls.read_text().split()) if calls.exists() else 0


class AdmissionRefusalChannelTests(unittest.TestCase):
    """The real admission gate signals refusal through its own marker."""

    def test_genuine_refusal_writes_marker(self):
        with scratch() as temp:
            heavy = HeavyEnv(Path(temp))
            marker = Path(temp) / "refused"
            command, calls = counting_command(Path(temp), "cmd")
            with heavy.occupied():
                result = run([str(CAPPED), "--", str(command)],
                             env=heavy.env(**{MARKER_ENV: str(marker)}))
            self.assertEqual(result.returncode, 3, result.stderr)
            self.assertIn("refused", marker.read_text())
            self.assertEqual(count(calls), 0)

    def test_command_own_exit_three_leaves_no_marker_and_hides_channel(self):
        with scratch() as temp:
            heavy = HeavyEnv(Path(temp))
            marker = Path(temp) / "refused"
            seen = Path(temp) / "seen"
            command, _ = counting_command(
                Path(temp), "cmd", f"echo \"${{{MARKER_ENV}:-unset}}\" > {seen}\nexit 3")
            result = run([str(CAPPED), "--", str(command)],
                         env=heavy.env(**{MARKER_ENV: str(marker)}))
            self.assertEqual(result.returncode, 3, result.stderr)
            self.assertFalse(marker.exists())
            self.assertEqual(seen.read_text().strip(), "unset")


class RetryWrapperTests(unittest.TestCase):
    """Real retry wrapper over the real admission gate (private slots)."""

    def test_real_exit_three_is_fail_exit_one_without_retry(self):
        with scratch() as temp:
            heavy = HeavyEnv(Path(temp))
            command, calls = counting_command(Path(temp), "internalerror", "exit 3")
            result = run(["bash", str(RETRY), "--attempts", "5", "--", str(command)],
                         env=heavy.env())
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn("FAIL", result.stderr)
            self.assertNotIn("capacity hold", result.stderr)
            self.assertEqual(count(calls), 1)
            self.assertEqual(heavy.recorded_sleeps(), [])

    def test_genuine_refusal_is_hold_retried_then_exit_three(self):
        with scratch() as temp:
            heavy = HeavyEnv(Path(temp))
            command, calls = counting_command(Path(temp), "cmd")
            with heavy.occupied():
                result = run(["bash", str(RETRY), "--attempts", "3", "--base-delay", "7",
                              "--", str(command)], env=heavy.env())
            self.assertEqual(result.returncode, 3, result.stderr)
            self.assertIn("HOLD", result.stderr)
            self.assertIn("followup", result.stderr.lower())
            self.assertEqual(result.stderr.count("capacity hold ("), 2)
            self.assertEqual(count(calls), 0)
            self.assertEqual(heavy.recorded_sleeps(), [7, 14])

    def test_refusal_then_admission_succeeds(self):
        with scratch() as temp:
            heavy = HeavyEnv(Path(temp))
            command, calls = counting_command(Path(temp), "cmd")
            # The fake clock frees the slot on the second sleep.
            holder = subprocess.Popen(
                ["python3", "-c",
                 "import fcntl,sys,time,pathlib\n"
                 "f=open(sys.argv[1],'a+'); fcntl.flock(f, fcntl.LOCK_EX)\n"
                 "print('held', flush=True)\n"
                 "r=pathlib.Path(sys.argv[2])\n"
                 "while not r.exists(): time.sleep(0.02)\n"
                 "f.close(); pathlib.Path(sys.argv[3]).touch()\n",
                 str(heavy.slots / "slot-0.lock"), str(Path(temp) / "release"),
                 str(Path(temp) / "released")],
                stdout=subprocess.PIPE, text=True)
            try:
                self.assertEqual(holder.stdout.readline().strip(), "held")
                write_exec(heavy.fake_sleep,
                           "#!/usr/bin/env bash\n"
                           f"echo \"$1\" >> {heavy.sleeps}\n"
                           f"if [ \"$(wc -l < {heavy.sleeps})\" -ge 2 ]; then\n"
                           f"  touch {Path(temp) / 'release'}\n"
                           f"  while [ ! -e {Path(temp) / 'released'} ]; do sleep 0.02; done\n"
                           "fi\n")
                result = run(["bash", str(RETRY), "--attempts", "5", "--base-delay", "1",
                              "--", str(command)], env=heavy.env())
            finally:
                (Path(temp) / "release").touch()
                holder.wait(timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(count(calls), 1)
            self.assertEqual(heavy.recorded_sleeps(), [1, 2])

    def test_backoff_schedule_doubles_and_caps_with_fake_clock(self):
        with scratch() as temp:
            heavy = HeavyEnv(Path(temp))
            command, _ = counting_command(Path(temp), "cmd")
            with heavy.occupied():
                result = run(["bash", str(RETRY), "--attempts", "6", "--base-delay", "30",
                              "--max-delay", "100", "--", str(command)], env=heavy.env())
            self.assertEqual(result.returncode, 3, result.stderr)
            self.assertEqual(heavy.recorded_sleeps(), [30, 60, 100, 100, 100])
            # A base delay above the cap is capped too.
            heavy.sleeps.unlink()
            with heavy.occupied():
                result = run(["bash", str(RETRY), "--attempts", "2", "--base-delay", "900",
                              "--max-delay", "120", "--", str(command)], env=heavy.env())
            self.assertEqual(result.returncode, 3, result.stderr)
            self.assertEqual(heavy.recorded_sleeps(), [120])

    def test_other_codes_pass_through_without_retry(self):
        with scratch() as temp:
            heavy = HeavyEnv(Path(temp))
            command, calls = counting_command(Path(temp), "cmd", "exit 17")
            result = run(["bash", str(RETRY), "--", str(command)], env=heavy.env())
            self.assertEqual(result.returncode, 17, result.stderr)
            self.assertEqual(count(calls), 1)

    def test_options_without_value_print_usage(self):
        for option in ("--attempts", "--base-delay", "--max-delay"):
            with self.subTest(option=option):
                result = run(["bash", str(RETRY), option])
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("Usage:", result.stderr)
                self.assertNotIn("unbound variable", result.stderr)

    def test_full_runner_is_run_directly_not_under_an_outer_slot(self):
        """Documented shape: capped-test-retry.sh -- scripts/test-full-local.sh --priority final."""
        with scratch() as temp:
            heavy = HeavyEnv(Path(temp))
            tree = Path(temp) / "repo" / "scripts"
            tree.mkdir(parents=True)
            for name in ("capped-test-retry.sh", "heavy-phase-status.sh"):
                shutil.copyfile(SCRIPTS / name, tree / name)
            outer = Path(temp) / "outer-wrapper-called"
            write_exec(tree / "capped-test-run.sh", f"#!/usr/bin/env bash\ntouch {outer}\nexit 99\n")
            runs = Path(temp) / "runs"
            # First run reports a pure hold through the caller's marker.
            write_exec(tree / "test-full-local.sh",
                       "#!/usr/bin/env bash\n"
                       f"echo \"$*|${{AGENT_HUB_HEAVY_TEST_HELD:-}}\" >> {runs}\n"
                       f"if [ \"$(wc -l < {runs})\" -eq 1 ]; then\n"
                       f"  echo 'capped-test-run refused: test' > \"${MARKER_ENV}\"; exit 3\n"
                       "fi\nexit 0\n")
            env = heavy.env()
            env.pop("AGENT_HUB_HEAVY_TEST_HELD", None)
            result = run(["bash", "scripts/capped-test-retry.sh", "--base-delay", "5", "--",
                          "scripts/test-full-local.sh", "--priority", "final"],
                         env=env, cwd=tree.parent)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(outer.exists())
            self.assertEqual(runs.read_text().splitlines(),
                             ["--priority final|", "--priority final|"])
            self.assertEqual(heavy.recorded_sleeps(), [5])


GIT_ENV = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_AUTHOR_NAME="t",
               GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")


def git(repo: Path, *args: str) -> str:
    result = run(["git", "-c", "commit.gpgsign=false", *args], cwd=repo, env=GIT_ENV)
    if result.returncode != 0:
        raise AssertionError(f"git {args}: {result.stderr}")
    return result.stdout.strip()


class CleanExitGuardTests(unittest.TestCase):
    def setUp(self):
        self._temp = scratch()
        self.root = Path(self._temp.name)
        self.addCleanup(self._temp.cleanup)

    def make_repo(self, origin: bool = True, push: bool = True) -> Path:
        repo = self.root / "repo"
        repo.mkdir()
        git(repo, "init", "-q", "-b", "main")
        (repo / "file.txt").write_text("one\n")
        git(repo, "add", ".")
        git(repo, "commit", "-qm", "init")
        if origin:
            git(self.root, "init", "-q", "--bare", str(self.root / "origin.git"))
            git(repo, "remote", "add", "origin", str(self.root / "origin.git"))
            if push:
                git(repo, "push", "-q", "origin", "HEAD:main")
                git(repo, "fetch", "-q", "origin")
        return repo

    def commit_on_agent_branch(self, repo: Path, branch: str = "ff68-agent") -> str:
        git(repo, "checkout", "-q", "-b", branch)
        (repo / "fix.txt").write_text("reviewed fix, not on main\n")
        git(repo, "add", ".")
        git(repo, "commit", "-qm", "fix: agent branch work")
        return git(repo, "rev-parse", "HEAD")

    def hub_stub(self, payload=None, rc: int = 0) -> Path:
        body = json.dumps(payload or {})
        return write_exec(self.root / "hub",
                          f"#!/usr/bin/env bash\ncat <<'EOF'\n{body}\nEOF\nexit {rc}\n")

    def guard(self, repo: Path, *args: str, hub: Path | None = None):
        env = dict(GIT_ENV, HUB_CLI=str(hub or self.hub_stub()))
        return run(["bash", str(GUARD), "--repo", str(repo), *args], env=env)

    def test_clean_tree_matching_origin_exits_zero_and_denies_push_authority(self):
        repo = self.make_repo()
        result = self.guard(repo)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("clean", result.stdout.lower())
        self.assertIn("NOT authorize pushing to main", result.stdout)

    def test_missing_origin_main_fails_closed(self):
        for origin in (False, True):
            with self.subTest(origin=origin):
                shutil.rmtree(self.root / "repo", ignore_errors=True)
                shutil.rmtree(self.root / "origin.git", ignore_errors=True)
                repo = self.make_repo(origin=origin, push=False)
                result = self.guard(repo)
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn("UNKNOWN", result.stderr)
                self.assertIn("followup", result.stderr)

    def test_stale_origin_main_is_refetched(self):
        repo = self.make_repo()
        base = git(repo, "rev-parse", "HEAD")
        (repo / "fix.txt").write_text("pushed, then rewound upstream\n")
        git(repo, "add", ".")
        git(repo, "commit", "-qm", "fix")
        git(repo, "push", "-q", "origin", "HEAD:main")
        git(repo, "fetch", "-q", "origin")
        # Upstream main moves back; the local origin/main ref is now stale
        # and would wrongly claim HEAD is on main without a fetch.
        other = self.root / "other"
        git(self.root, "clone", "-q", str(self.root / "origin.git"), str(other))
        git(other, "push", "-q", "--force", "origin", f"{base}:refs/heads/main")
        self.assertEqual(git(repo, "rev-parse", "refs/remotes/origin/main"),
                         git(repo, "rev-parse", "HEAD"))
        result = self.guard(repo)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("commits ahead of origin/main", result.stderr)

    def test_agent_branch_commit_recorded_in_hub_is_clean(self):
        repo = self.make_repo()
        sha = self.commit_on_agent_branch(repo)
        for kind, reference in (("commit", sha), ("commit", sha[:8].upper()), ("report", sha)):
            with self.subTest(kind=kind, reference=reference):
                hub = self.hub_stub({"task": {"history": [
                    {"kind": "note", "text": sha},
                    {"kind": kind, "reference": reference}]}})
                result = self.guard(repo, "--intake", "deadbeef", hub=hub)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("agent branch ff68-agent", result.stdout)
                self.assertIn("NOT authorize pushing to main", result.stdout)

    def test_agent_branch_commit_not_recorded_holds(self):
        repo = self.make_repo()
        sha = self.commit_on_agent_branch(repo)
        # A note mentioning the sha is not a commit record.
        hub = self.hub_stub({"task": {"history": [{"kind": "note", "text": sha}]}})
        result = self.guard(repo, "--intake", "deadbeef", hub=hub)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn('"kind":"report","reference":"<full sha>"', result.stderr)
        self.assertIn("blocker", result.stderr)
        # Without --intake nothing can be verified.
        result = self.guard(repo)
        self.assertEqual(result.returncode, 1, result.stdout)

    def test_recorded_commit_on_main_branch_still_holds(self):
        repo = self.make_repo()
        (repo / "fix.txt").write_text("committed on main, unpushed\n")
        git(repo, "add", ".")
        git(repo, "commit", "-qm", "fix on main")
        sha = git(repo, "rev-parse", "HEAD")
        hub = self.hub_stub({"history": [{"kind": "commit", "reference": sha}]})
        result = self.guard(repo, "--intake", "deadbeef", hub=hub)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("NOT authorize pushing to main", result.stderr)

    def test_unreadable_hub_fails_closed(self):
        repo = self.make_repo()
        self.commit_on_agent_branch(repo)
        result = self.guard(repo, "--intake", "deadbeef", hub=self.hub_stub(rc=1))
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("UNKNOWN", result.stderr)

    def test_dirty_tree_refuses_with_note_and_followup_guidance(self):
        repo = self.make_repo()
        (repo / "file.txt").write_text("modified but uncommitted\n")
        result = self.guard(repo, "--intake", "deadbeef-1234")
        self.assertEqual(result.returncode, 1)
        self.assertIn("HOLD", result.stderr)
        self.assertIn("hub intake append deadbeef-1234", result.stderr)
        self.assertIn("followup", result.stderr)

    def test_options_without_value_print_usage(self):
        for option in ("--intake", "--repo"):
            with self.subTest(option=option):
                result = run(["bash", str(GUARD), option])
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("Usage:", result.stderr)
                self.assertNotIn("unbound variable", result.stderr)


class SessionRulesTests(unittest.TestCase):
    def test_preamble_carries_capacity_retry_and_clean_exit(self):
        from agent_hub_host.session_rules import (
            CAPACITY_RETRY_RULE, CLEAN_EXIT_RULE, SESSION_PREAMBLE,
            compose_prompt)
        self.assertIn("scripts/capped-test-retry.sh -- scripts/test-full-local.sh --priority final",
                      CAPACITY_RETRY_RULE)
        self.assertIn("exit 3", CAPACITY_RETRY_RULE)
        self.assertIn("HOLD", CAPACITY_RETRY_RULE)
        self.assertIn("never retried", CAPACITY_RETRY_RULE)
        self.assertIn("require-clean-exit.sh", CLEAN_EXIT_RULE)
        self.assertIn("followup", CLEAN_EXIT_RULE)
        self.assertIn("`report` event", CLEAN_EXIT_RULE)
        self.assertIn("`blocker`", CLEAN_EXIT_RULE)
        self.assertIn("does NOT authorize pushing to main", CLEAN_EXIT_RULE)
        self.assertIn("orchestrator-main", CLEAN_EXIT_RULE)
        for rule in (CAPACITY_RETRY_RULE, CLEAN_EXIT_RULE):
            self.assertIn(rule, SESSION_PREAMBLE)
            self.assertIn(rule, compose_prompt("brief"))

    def test_documented_retry_commands_match_the_rule(self):
        doc = (REPO / "docs" / "HETZNER-OVERLOAD-0689CE9C.md").read_text()
        self.assertIn("-- scripts/test-full-local.sh --priority final", doc)
        runner = (SCRIPTS / "test-full-local.sh").read_text()
        self.assertIn("scripts/capped-test-retry.sh -- scripts/test-full-local.sh --priority final",
                      runner)


if __name__ == "__main__":
    unittest.main()
