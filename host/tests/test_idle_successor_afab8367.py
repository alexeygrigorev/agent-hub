"""Intake afab8367: an idle-work successor of a LIVE predecessor starts in its
own fresh sibling worktree instead of the held one.

Reproduction: idle-work keeps the predecessor running until the replacement
itself is running, but the host worktree lock (_lock_session_worktree) only
moves to a proven-terminal owner. A coupled replacement planned into the same
worktree is therefore refused as dispatch_worktree_refused. A successor start
(Hub plans mode "create" with a successor-unique suffix) creates a fresh
sibling from fresh origin/main, locks only that sibling, and leaves the live
owner's lock untouched.
"""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_hub_host import fleet as fleet_mod
from agent_hub_host import worktree_guard as guard
from agent_hub_host.dispatch_diagnostics import classify
from agent_hub_host.intake_worktree import launch_cwd

INTAKE = "afab8367-95a2-41e2-8ef1-58bc618d5fbb"
OWNER = "6b31d971-f96a-46b9-9b63-2646563233df"
SUCCESSOR = "7384605b-774d-4ba6-9860-293953f826a8"


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


class IdleSuccessorWorktree(unittest.TestCase):
    def setUp(self):
        base = os.environ.get("TMPDIR") or None
        self._tmp = tempfile.TemporaryDirectory(prefix="idle-successor-", dir=base)
        self.addCleanup(self._tmp.cleanup)
        self.home = os.path.realpath(self._tmp.name)
        self.assertNotIn("/prod", self.home)
        gitconfig = os.path.join(self.home, ".gitconfig")
        Path(gitconfig).write_text("[user]\n\tname = Test\n\temail = test@example.invalid\n"
                                   "[init]\n\tdefaultBranch = main\n")
        env = {"HOME": self.home, "GIT_CONFIG_GLOBAL": gitconfig, "GIT_CONFIG_NOSYSTEM": "1",
               "GIT_CEILING_DIRECTORIES": self.home,
               "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
               "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.invalid",
               "HUB_WORKTREE_IDENTITY_DIR": os.path.join(self.home, "identities")}
        self._env = patch.dict(os.environ, env)
        self._env.start()
        self.addCleanup(self._env.stop)
        gitdir = os.path.join(self.home, "git")
        os.makedirs(gitdir)
        origin = os.path.join(self.home, "remote", "origin.git")
        os.makedirs(origin)
        _git(origin, "init", "--bare", "-b", "main")
        seed = os.path.join(self.home, "remote", "seed")
        _git(os.path.dirname(seed), "clone", "-q", origin, seed)
        Path(seed, "README").write_text("seed\n")
        _git(seed, "add", "README")
        _git(seed, "commit", "-qm", "seed")
        _git(seed, "push", "-q", "origin", "HEAD:main")
        self.repo = os.path.join(gitdir, "repo")
        _git(gitdir, "clone", "-q", origin, self.repo)
        # The live predecessor runs in its own worktree of the same repository.
        self.tree = os.path.join(gitdir, "repo-wt-pred")
        _git(self.repo, "worktree", "add", "-q", "-b", "pred-branch", self.tree, "origin/main")
        self.main_head = _git(self.repo, "rev-parse", "origin/main").strip()

        def request(method, path, body=None):
            if path.startswith("/api/fleet/worktree-owner/"):
                # The owner session is live (non-terminal) on the same intake.
                return {"session_id": path.rsplit("/", 1)[-1], "terminal": False, "intake_id": INTAKE}
            if path == "/api/worktree-host/plan":
                return {"plan": self.successor_plan}
            if path == "/api/worktree-host/observe":
                return {}
            return {}
        cfg = {"version": 1, "workspaces": {"agent-hub": self.repo}, "subscriptions": []}
        self.dispatcher = fleet_mod.Dispatcher(cfg, request, lambda *a: {})
        self.request = request
        # The live owner session holds the intake worktree lock.
        self.assertTrue(self.dispatcher._lock_session_worktree(
            {"id": "c" * 32, "hub_session_id": OWNER, "intake_task_id": INTAKE}, self.tree))
        self.assertTrue(guard.is_locked("git", self.tree))
        self.successor_plan = {"mode": "create", "task_id": INTAKE, "suffix": "wt-r-7384605b"}

    def sibling(self):
        return os.path.join(self.home, "git", "repo-wt-pred-" + self.successor_plan["suffix"])

    def owner_lock_reason(self):
        entry = guard.find_entry(guard.list_entries("git", self.tree), self.tree)
        return entry["reason"]

    def test_coupled_same_worktree_start_is_dispatch_worktree_refused(self):
        # The pre-fix shape: a replacement into the live-held tree refuses.
        offer = {"id": "d" * 32, "hub_session_id": SUCCESSOR, "intake_task_id": INTAKE}
        with self.assertRaises(ValueError) as caught:
            self.dispatcher._lock_session_worktree(offer, self.tree)
        self.assertEqual(classify(caught.exception)["reason"], "dispatch_worktree_refused")
        self.assertEqual(self.owner_lock_reason(), guard.lock_reason(OWNER, INTAKE))

    def test_successor_start_runs_in_fresh_sibling_without_touching_owner_lock(self):
        offer = {"id": "d" * 32, "hub_session_id": SUCCESSOR, "intake_task_id": INTAKE}
        # The successor names no cwd, so the host asks Hub for the worktree plan.
        target, note, wt = launch_cwd(self.request, offer, Path(self.tree), Path(self.tree))
        self.assertEqual(target, Path(self.sibling()))
        self.assertTrue(os.path.isdir(self.sibling()))
        self.assertIn("Hub prepared the dedicated worktree", note)
        self.assertEqual(wt["branch"], "repo-wt-pred-" + self.successor_plan["suffix"])
        # The sibling is a fresh checkout of origin/main, never the held state.
        self.assertEqual(_git(self.sibling(), "rev-parse", "HEAD").strip(), self.main_head)
        # No dispatch_worktree_refused: the fresh sibling locks cleanly.
        self.assertTrue(self.dispatcher._lock_session_worktree(offer, target))
        self.assertEqual(guard.find_entry(guard.list_entries("git", str(target)), str(target))["reason"],
                         guard.lock_reason(SUCCESSOR, INTAKE))
        # No lock stolen: the live owner's lock is exactly as it was.
        self.assertEqual(self.owner_lock_reason(), guard.lock_reason(OWNER, INTAKE))

    def test_successor_from_workspace_root_creates_sibling_next_to_workspace(self):
        offer = {"id": "d" * 32, "hub_session_id": SUCCESSOR, "intake_task_id": INTAKE}
        target, _, _ = launch_cwd(self.request, offer, Path(self.repo), Path(self.repo))
        self.assertEqual(target, Path(os.path.join(self.home, "git", "repo-" + self.successor_plan["suffix"])))
        self.assertTrue(self.dispatcher._lock_session_worktree(offer, target))
        self.assertEqual(self.owner_lock_reason(), guard.lock_reason(OWNER, INTAKE))

    def test_failed_plan_fails_closed_into_honest_refusal(self):
        # An old Hub (mode none) keeps the coupled target: the start refuses
        # honestly instead of sharing the live-held worktree.
        self.successor_plan = {"mode": "none", "task_id": INTAKE, "detached": False}
        offer = {"id": "d" * 32, "hub_session_id": SUCCESSOR, "intake_task_id": INTAKE}
        target, note, wt = launch_cwd(self.request, offer, Path(self.tree), Path(self.tree))
        self.assertEqual((target, note, wt), (Path(self.tree), "", None))
        with self.assertRaises(ValueError) as caught:
            self.dispatcher._lock_session_worktree(offer, target)
        self.assertEqual(classify(caught.exception)["reason"], "dispatch_worktree_refused")
        self.assertEqual(self.owner_lock_reason(), guard.lock_reason(OWNER, INTAKE))


if __name__ == "__main__":
    unittest.main()
