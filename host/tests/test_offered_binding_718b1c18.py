"""Intake 718b1c18: a native admitted under a reused same-intake lock binds.

Session f521a611 stayed offered with binding null for hours. Dispatch
admitted launch a8ccb7c5 under the predecessor's same-intake worktree lock
(fresh proof that the owning Hub session was terminal). Binding then refused
the same lock because it never received that proof, so the live native was
never bound.
"""
import contextlib
import io
import json
import os
import pathlib
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from agent_hub_host import worktree_guard as guard
from host.tests.test_worktree_guard import _add_worktree, _make_repo, _run

INTAKE = "intake-718b1c18"
PREVIOUS = "a" * 8 + "-aaaa-4aaa-8aaa-" + "a" * 12
SUCCESSOR = "b" * 8 + "-bbbb-4bbb-8bbb-" + "b" * 12
LAUNCH = "c" * 8 + "-cccc-4ccc-8ccc-" + "c" * 12
NATIVE = "d" * 8 + "-dddd-4ddd-8ddd-" + "d" * 12


class SameIntakeLockBindingTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self._env = patch.dict(os.environ, {"HUB_WORKTREE_IDENTITY_DIR": self.tmp + "/identity"})
        self._env.start()
        self.origin = _make_repo(self.tmp)
        self.tree = _add_worktree(self.origin, self.tmp)
        self.reason = guard.lock_reason(PREVIOUS, INTAKE)
        self.assertTrue(guard.lock_worktree("git", self.tree, self.reason))
        self.assertTrue(guard.remember_repository(LAUNCH, self.tree))
        self.row = {"id": NATIVE, "tag": "hub-" + LAUNCH, "workspace": self.tree, "cwd": self.tree,
                    "engine": "codex", "profile": None, "state": "running", "created_at_ms": 1793283719586}
        self.session = {"id": SUCCESSOR, "fleet_launch_id": LAUNCH, "intake_task_id": INTAKE,
                        "engine": "codex", "native_id": None}

    def tearDown(self):
        guard.unlock_worktree("git", self.tree)
        self._env.stop()
        self._tmp.cleanup()

    def lock_entry(self):
        return guard.find_entry(guard.list_entries("git", self.tree), self.tree)

    def test_binding_accepts_the_owner_proof_dispatch_used(self):
        # Predecessor Hub session terminal, its native still live: the old
        # reuse rule refuses, exactly as in production.
        facts = {"sessions": [{"session_id": PREVIOUS, "intake_task_id": INTAKE, "terminal": True,
                               "native_terminal": False},
                              {"session_id": SUCCESSOR, "intake_task_id": INTAKE, "terminal": False}]}
        self.assertFalse(guard.protect_adoption(self.row, self.session, request=lambda *a: facts))
        self.assertTrue(guard.protect_adoption(self.row, self.session, request=lambda *a: facts,
                                               owner_terminal=lambda owner: owner == PREVIOUS))
        # The predecessor's lock is preserved, never replaced or released.
        self.assertEqual(self.lock_entry()["reason"], self.reason)
        self.assertNotEqual(_run(["git", "-C", self.origin, "worktree", "remove", self.tree], self.tmp).returncode, 0)

    def test_binding_still_refuses_live_owner_and_foreign_intake(self):
        refuse = lambda *a: {"sessions": []}
        self.assertFalse(guard.protect_adoption(self.row, self.session, request=refuse,
                                                owner_terminal=lambda owner: False))
        other = dict(self.session, intake_task_id="another-intake")
        self.assertFalse(guard.protect_adoption(self.row, other, request=refuse,
                                                owner_terminal=lambda owner: True))
        self.assertEqual(self.lock_entry()["reason"], self.reason)

    def dispatcher(self, requests, owner_terminal):
        from agent_hub_host import fleet
        from host.tests.test_fleet import settings

        def request(method, path, body=None):
            requests.append((method, path, body))
            if path == "/api/sessions/host-bindings":
                return {"sessions": [dict(self.session)]}
            if path == "/api/fleet/worktree-owner/" + PREVIOUS:
                return {"session_id": PREVIOUS, "terminal": owner_terminal, "intake_id": INTAKE}
            if path == "/api/sessions/worktree-guards":
                return {"sessions": []}
            return {}

        directory = pathlib.Path(self.tmp) / "journal"
        directory.mkdir(exist_ok=True)
        with patch("agent_hub_host.provisioning.managed_subscriptions", return_value=[]):
            dispatcher = fleet.Dispatcher(settings(directory), request, lambda *args: [dict(self.row)],
                                          journal_dir=str(directory))
        dispatcher._session_progress = SimpleNamespace(sample=lambda *args: {})
        return dispatcher

    def report(self, dispatcher):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            dispatcher.report_native_bindings()
        events = [json.loads(line) for line in out.getvalue().splitlines() if line.startswith("{")]
        return next(e for e in events if e.get("event") == "bindings_report_phases"), events

    def test_host_cycle_binds_live_native_under_reused_lock(self):
        requests = []
        phases, _ = self.report(self.dispatcher(requests, owner_terminal=True))
        posts = [(path, body) for method, path, body in requests if path.endswith("/binding")]
        self.assertEqual(posts, [("/api/sessions/" + SUCCESSOR + "/binding",
                                  {"native": {"native_id": NATIVE, "tag": "hub-" + LAUNCH,
                                              "workspace": self.tree, "native_created_at_ms": 1793283719586}})])
        self.assertEqual(phases["outcomes"], {"bound": 1})
        self.assertEqual(self.lock_entry()["reason"], self.reason)

    def test_refused_binding_is_reported_not_silent(self):
        requests = []
        phases, events = self.report(self.dispatcher(requests, owner_terminal=False))
        self.assertFalse(any(path.endswith("/binding") for _, path, _ in requests))
        self.assertEqual(phases["outcomes"], {"worktree_lock_refused": 1})
        self.assertIn({"event": "bindings_skipped", "skipped": [
            {"session": SUCCESSOR[:8], "reason": "worktree_lock_refused"}]}, events)


if __name__ == "__main__":
    unittest.main()
