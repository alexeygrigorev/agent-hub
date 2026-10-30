"""Intake 79458f2d: triage/verifier starts never lock the linked intake worktree.

Reproduction: the hourly triage reviewer is linked to an intake whose
registered worktree is git-locked by a live owner session. Planned into that
worktree, the host refuses the start as dispatch_worktree_refused. A detached
start (Hub plans mode "none") runs in the workspace root and leaves the
owner's lock untouched.
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_hub_host import fleet as fleet_mod
from agent_hub_host import worktree_guard as guard
from agent_hub_host.dispatch_diagnostics import classify
from agent_hub_host.intake_worktree import launch_cwd
from host.tests.test_worktree_guard import _add_worktree, _make_repo

INTAKE = "d8d509d2-33b8-4ac0-b819-f840dfd54719"
OWNER = "a" * 32
TRIAGE = "c" * 32


class DetachedTriageStart(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.origin = _make_repo(self.tmp)
        self.tree = _add_worktree(self.origin, self.tmp)
        env = patch.dict(os.environ, {"HUB_WORKTREE_IDENTITY_DIR": self.tmp + "/identities"})
        env.start()
        self.addCleanup(env.stop)
        self.plan = None

        def request(method, path, body=None):
            if path.startswith("/api/fleet/worktree-owner/"):
                # The owner session is live (non-terminal) on the same intake.
                return {"session_id": path.rsplit("/", 1)[-1], "terminal": False, "intake_id": INTAKE}
            if path == "/api/worktree-host/plan":
                return self.plan
            return {}
        cfg = {"version": 1, "workspaces": {"agent-hub": self.origin}, "subscriptions": []}
        self.dispatcher = fleet_mod.Dispatcher(cfg, request, lambda *a: {})
        self.request = request
        # The live owner session holds the intake worktree lock.
        self.assertTrue(self.dispatcher._lock_session_worktree(
            {"id": "d" * 32, "hub_session_id": OWNER, "intake_task_id": INTAKE}, self.tree))
        self.assertTrue(guard.is_locked("git", self.tree))
        self.offer = {"id": "b" * 32, "hub_session_id": TRIAGE, "intake_task_id": INTAKE}

    def test_coupled_start_into_locked_intake_worktree_is_dispatch_worktree_refused(self):
        with self.assertRaises(ValueError) as caught:
            self.dispatcher._lock_session_worktree(self.offer, self.tree)
        self.assertEqual(classify(caught.exception)["reason"], "dispatch_worktree_refused")

    def test_detached_start_runs_in_workspace_root_and_keeps_owner_lock(self):
        self.plan = {"mode": "none", "task_id": INTAKE, "detached": True}
        target, note, wt = launch_cwd(self.request, self.offer, Path(self.origin), Path(self.origin))
        self.assertEqual((target, note, wt), (Path(self.origin), "", None))
        self.assertTrue(self.dispatcher._lock_session_worktree(self.offer, target))
        entry = guard.find_entry(guard.list_entries("git", self.origin), self.tree)
        self.assertEqual(guard.parse_lock_reason(entry["reason"]), (OWNER, INTAKE))


if __name__ == "__main__":
    unittest.main()
