"""Real-Git lock-resume regression adopted from prior 43071c69 audit."""
import json
import os
import unittest
from pathlib import Path

from agent_hub_host import fleet, intake_worktree as iw
import test_intake_worktree as fixtures

_git = fixtures._git


class ResumeLockTests(fixtures.GitSandbox):
    OFFER_ID = fixtures.FleetHookTests.OFFER_ID
    settings = fixtures.FleetHookTests.settings
    runner_for = fixtures.FleetHookTests.runner_for
    offer = fixtures.FleetHookTests.offer

    def test_resume_foreign_locked_checkout_preserves_work_and_starts_in_fresh_tree(self):
        from agent_hub_host import worktree_guard as guard
        old = self.add_worktree("repo-wt-old", commit=True)
        Path(old, "unfinished.txt").write_text("preserve me")
        reason = guard.lock_reason("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", "abcd1234")
        _git(self.repo, "worktree", "lock", "--reason", reason, old)
        path = os.path.join(self.gitdir, "repo-wt-" + self.OFFER_ID)
        http, calls = [], []

        def request(method, route, body=None):
            http.append((route, body))
            if route == iw.PLAN_PATH:
                return {"plan": {"mode": "resume", "task_id": "abcd1234",
                                 "path": old, "branch": "repo-wt-old"}}
            return {}

        d = fleet.Dispatcher(self.settings(), request, self.runner_for(calls, cwd=path))
        result = d.dispatch(self.offer(hub_session_id=self.OFFER_ID, intake_task_id="abcd1234"))
        self.assertEqual(result["state"], "running", result)
        self.assertEqual(guard.find_entry(guard.list_entries("git", old), old)["reason"], reason)
        self.assertEqual(Path(old, "unfinished.txt").read_text(), "preserve me")
        self.assertTrue(Path(old, "work.txt").exists())
        self.assertEqual(_git(path, "rev-parse", "HEAD"), _git(self.repo, "rev-parse", "origin/main"))
        self.assertTrue(guard.is_locked("git", path))
        observed = [b for r, b in http if r == iw.OBSERVE_PATH]
        self.assertEqual(observed[0]["path"], path)
        preamble = [a for a, _ in calls if a[:3] == ["aplexer", "message", "send"]][0][-1]
        self.assertIn("Previous work remains at " + old, preamble)
        # A repeated attempt resumes the same session-owned checkout safely.
        again = iw.prepare_launch_worktree(request, self.OFFER_ID, self.repo, session_id=self.OFFER_ID)
        self.assertEqual(again["path"], path)

    def test_resume_same_session_lock_reuses_tree(self):
        from agent_hub_host import worktree_guard as guard
        old = self.add_worktree("repo-wt-same")
        _git(self.repo, "worktree", "lock", "--reason", guard.lock_reason(self.OFFER_ID, "abcd1234"), old)

        def request(method, route, body=None):
            return {"plan": {"mode": "resume", "task_id": "abcd1234", "path": old,
                             "branch": "repo-wt-same"}} if route == iw.PLAN_PATH else {}

        result = iw.prepare_launch_worktree(request, self.OFFER_ID, self.repo, session_id=self.OFFER_ID)
        self.assertEqual(result["action"], "reused")
        self.assertEqual(result["path"], old)

    def test_explicit_foreign_locked_tree_refuses_with_safe_diagnostic(self):
        from agent_hub_host import worktree_guard as guard
        from agent_hub_host.launch_diagnostics import exception_diagnostic
        old = self.add_worktree("repo-wt-explicit")
        reason = "private operator lock secret=must-not-appear"
        _git(self.repo, "worktree", "lock", "--reason", reason, old)
        d = fleet.Dispatcher(self.settings(), lambda *a: {}, self.runner_for([]))
        with self.assertRaises(ValueError) as raised:
            d._lock_session_worktree(self.offer(hub_session_id=self.OFFER_ID, intake_task_id="abcd1234"), old)
        diagnostic = exception_diagnostic(raised.exception, "dispatch_headless")
        self.assertEqual(diagnostic["reason"], "launcher_worktree_lock_failed")
        self.assertIn("existing session or operator lock", diagnostic["message"])
        self.assertNotIn("must-not-appear", json.dumps(diagnostic))
        self.assertNotIn(old, json.dumps(diagnostic))
        self.assertEqual(guard.find_entry(guard.list_entries("git", old), old)["reason"], reason)


if __name__ == "__main__":
    unittest.main()
