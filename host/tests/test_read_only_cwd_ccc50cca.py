"""Regression tests for intake ccc50cca & 36c4cf38:
- read-only review launch_cwd mapping (including nested subdirectories)
- exclusive successor worktree ownership across ensure_locked, reuse_stopped_intake_lock,
  protect_successor_worktree, and Dispatcher._lock_session_worktree (Review-2 P1).
"""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from agent_hub_host import intake_worktree as iw
from agent_hub_host import worktree_guard as guard
from agent_hub_host.fleet import Dispatcher


def _git(args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _make_repo_with_worktree(tmp: str, repo_name: str = "demo", suffix: str = "wt-ccc50cca"):
    git_root = Path(tmp) / "git"
    git_root.mkdir(parents=True, exist_ok=True)
    main_repo = git_root / repo_name
    main_repo.mkdir()
    _git(["init", "-q"], str(main_repo))
    _git(["config", "user.email", "t@example.invalid"], str(main_repo))
    _git(["config", "user.name", "Test"], str(main_repo))
    (main_repo / "README.md").write_text("hi\n", encoding="utf-8")
    _git(["add", "README.md"], str(main_repo))
    _git(["commit", "-qm", "init"], str(main_repo))
    wt = git_root / f"{repo_name}-{suffix}"
    _git(["worktree", "add", "-q", "-b", f"branch-{suffix}", str(wt)], str(main_repo))
    return main_repo, wt


class TestReadOnlyReviewCwdMapping(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {"HUB_WORKTREE_IDENTITY_DIR": self.tmp.name + "/identities"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.main_repo, self.wt = _make_repo_with_worktree(self.tmp.name)

    def test_read_only_plan_maps_worktree_root_and_nested_subdir_to_main_checkout(self):
        nested = self.wt / "packages" / "sub"
        nested.mkdir(parents=True)
        impl_session = "a" * 32
        intake_id = "ccc50cca-0d72-4eff-83bd-8c6919c9ff26"
        self.assertTrue(guard.protect_bound_worktree("git", str(self.wt), impl_session, intake_id))
        self.assertTrue(guard.is_locked("git", str(self.wt)))

        def request(method, path, body=None):
            if path == iw.PLAN_PATH:
                return {"plan": {"mode": "none", "task_id": intake_id, "read_only": True}}
            raise AssertionError(f"unexpected request: {method} {path}")

        for start_target in (self.wt, nested):
            offer = {
                "id": "b" * 32,
                "hub_session_id": "c" * 32,
                "intake_task_id": intake_id,
                "workspace": str(start_target),
                "cwd": None,
            }
            resolved_cwd, note, wt_info = iw.launch_cwd(request, offer, str(start_target), start_target)
            self.assertEqual(Path(resolved_cwd), self.main_repo)
            self.assertEqual(note, "")
            self.assertIsNone(wt_info)
            dispatcher = Dispatcher({}, request)
            self.assertTrue(dispatcher._lock_session_worktree(offer, Path(resolved_cwd)))

        entry = guard.find_entry(guard.list_entries("git", str(self.wt)), str(self.wt))
        self.assertEqual(entry["reason"], guard.lock_reason(impl_session, intake_id))

    def test_explicit_cwd_is_preserved_even_for_read_only_plan(self):
        intake_id = "ccc50cca-0d72-4eff-83bd-8c6919c9ff26"
        def request(method, path, body=None):
            if path == iw.PLAN_PATH:
                return {"plan": {"mode": "none", "task_id": intake_id, "read_only": True}}
            raise AssertionError(f"unexpected request: {method} {path}")

        offer = {
            "id": "b" * 32,
            "hub_session_id": "c" * 32,
            "intake_task_id": intake_id,
            "workspace": str(self.main_repo),
            "cwd": str(self.wt),
        }
        resolved_cwd, note, wt_info = iw.launch_cwd(request, offer, str(self.main_repo), self.wt)
        self.assertEqual(Path(resolved_cwd), self.wt)
        self.assertEqual(note, "")
        self.assertIsNone(wt_info)


class TestExclusiveSuccessorWorktreeOwnership(unittest.TestCase):
    """Review-2 P1 + 36c4cf38 host-dispatch regression coverage."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {"HUB_WORKTREE_IDENTITY_DIR": self.tmp.name + "/identities"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.main_repo, self.wt = _make_repo_with_worktree(self.tmp.name)
        self.intake_id = "60cff605-3e19-4c95-9cf9-000000000001"
        self.owner_a = "a" * 32
        self.launch_a = "1" * 32
        self.succ_b = "b" * 32
        self.launch_b = "2" * 32
        self.succ_c = "c" * 32
        self.launch_c = "3" * 32

        # Predecessor A locked the worktree and recorded its repository identity.
        self.assertTrue(guard.remember_repository(self.launch_a, str(self.wt)))
        self.assertTrue(guard.protect_bound_worktree("git", str(self.wt), self.owner_a, self.intake_id))

    def _make_hub_state(self, sessions: dict):
        """Simulate Hub's /api/fleet/worktree-owner/:id and /api/sessions/worktree-guards."""
        def request(method, path, body=None):
            if method == "GET" and path.startswith("/api/fleet/worktree-owner/"):
                rest = path[len("/api/fleet/worktree-owner/"):]
                owner_id, _, query = rest.partition("?")
                requester = None
                if query.startswith("requester="):
                    requester = query[len("requester="):]
                row = sessions.get(owner_id)
                if not row:
                    raise OSError("404 not_found")
                intake = row.get("intake_task_id")
                req_ok = True
                if requester is not None:
                    req_row = sessions.get(requester)
                    if (not req_row or requester == owner_id
                            or req_row.get("intake_task_id") != intake
                            or req_row.get("terminal") is not False):
                        req_ok = False
                other_contenders = [
                    s for sid, s in sessions.items()
                    if sid != owner_id and (requester is None or sid != requester)
                    and s.get("intake_task_id") == intake
                    and s.get("terminal") is not True
                    and (s.get("task_kind") != "review" or bool(s.get("requested_cwd")))
                ]
                terminal = (
                    row.get("terminal") is True
                    and row.get("native_terminal") is True
                    and req_ok
                    and len(other_contenders) == 0
                )
                return {"session_id": owner_id, "intake_id": intake, "terminal": terminal}
            if method == "GET" and path == "/api/sessions/worktree-guards":
                return {"sessions": list(sessions.values())}
            raise AssertionError(f"unexpected request: {method} {path}")
        return request

    def test_valid_single_successor_and_sequential_second_successor_refused_until_first_exits(self):
        sessions = {
            self.owner_a: {
                "session_id": self.owner_a,
                "intake_task_id": self.intake_id,
                "terminal": True,
                "native_terminal": True,
                "tag": "task-hub-" + self.launch_a,
            },
            self.succ_b: {
                "session_id": self.succ_b,
                "intake_task_id": self.intake_id,
                "terminal": False,
                "native_terminal": False,
                "tag": "task-hub-" + self.launch_b,
            },
        }
        dispatcher = Dispatcher({}, self._make_hub_state(sessions))
        offer_b = {"id": self.launch_b, "hub_session_id": self.succ_b, "intake_task_id": self.intake_id}
        self.assertTrue(dispatcher._lock_session_worktree(offer_b, self.wt))

        # Now sequential successor C arrives while B is still running.
        sessions[self.succ_c] = {
            "session_id": self.succ_c,
            "intake_task_id": self.intake_id,
            "terminal": False,
            "native_terminal": False,
            "tag": "task-hub-" + self.launch_c,
        }
        offer_c = {"id": self.launch_c, "hub_session_id": self.succ_c, "intake_task_id": self.intake_id}
        with self.assertRaisesRegex(ValueError, "worktree_lock_failed"):
            dispatcher._lock_session_worktree(offer_c, self.wt)

        # Even if worktree-owner were bypassed or mocked to return True for A,
        # the host's fenced active_owner (B) refuses C across all 3 functions while B is live.
        self.assertFalse(guard.ensure_locked(
            "git", str(self.wt), self.succ_c, self.intake_id,
            owner_terminal=lambda oid: oid == self.owner_a,
        ))
        self.assertFalse(guard.reuse_stopped_intake_lock(
            "git", str(self.wt), self.succ_c, self.intake_id, self._make_hub_state(sessions),
        ))
        self.assertFalse(guard.protect_successor_worktree(
            self._make_hub_state(sessions), "git", str(self.wt), self.succ_c, self.intake_id,
        ))

        # Once B also becomes positively terminal, C is admitted cleanly.
        sessions[self.succ_b]["terminal"] = True
        sessions[self.succ_b]["native_terminal"] = True
        self.assertTrue(dispatcher._lock_session_worktree(offer_c, self.wt))

    def test_concurrent_successors_refuse_both_including_fallback_paths(self):
        # Both B and C are offered concurrently on the same host before either acquires the lock.
        sessions = {
            self.owner_a: {
                "session_id": self.owner_a,
                "intake_task_id": self.intake_id,
                "terminal": True,
                "native_terminal": True,
                "tag": "task-hub-" + self.launch_a,
            },
            self.succ_b: {
                "session_id": self.succ_b,
                "intake_task_id": self.intake_id,
                "terminal": False,
                "native_terminal": False,
                "tag": "task-hub-" + self.launch_b,
            },
            self.succ_c: {
                "session_id": self.succ_c,
                "intake_task_id": self.intake_id,
                "terminal": False,
                "native_terminal": False,
                "tag": "task-hub-" + self.launch_c,
            },
        }
        dispatcher = Dispatcher({}, self._make_hub_state(sessions))
        offer_b = {"id": self.launch_b, "hub_session_id": self.succ_b, "intake_task_id": self.intake_id}
        offer_c = {"id": self.launch_c, "hub_session_id": self.succ_c, "intake_task_id": self.intake_id}

        # Neither B nor C can bypass the contender refusal via reuse_stopped_intake_lock or protect_successor_worktree.
        with self.assertRaisesRegex(ValueError, "worktree_lock_failed"):
            dispatcher._lock_session_worktree(offer_b, self.wt)
        with self.assertRaisesRegex(ValueError, "worktree_lock_failed"):
            dispatcher._lock_session_worktree(offer_c, self.wt)

    def test_explicit_cwd_review_session_cannot_overlap_ordinary_successor_in_either_order(self):
        # Order 1: Ordinary B starts first, then explicit-cwd review C tries to enter self.wt.
        sessions = {
            self.owner_a: {
                "session_id": self.owner_a,
                "intake_task_id": self.intake_id,
                "terminal": True,
                "native_terminal": True,
                "tag": "task-hub-" + self.launch_a,
            },
            self.succ_b: {
                "session_id": self.succ_b,
                "intake_task_id": self.intake_id,
                "terminal": False,
                "native_terminal": False,
                "tag": "task-hub-" + self.launch_b,
            },
        }
        dispatcher = Dispatcher({}, self._make_hub_state(sessions))
        offer_b = {"id": self.launch_b, "hub_session_id": self.succ_b, "intake_task_id": self.intake_id}
        self.assertTrue(dispatcher._lock_session_worktree(offer_b, self.wt))

        sessions[self.succ_c] = {
            "session_id": self.succ_c,
            "intake_task_id": self.intake_id,
            "task_kind": "review",
            "requested_cwd": str(self.wt),
            "terminal": False,
            "native_terminal": False,
            "tag": "task-hub-" + self.launch_c,
        }
        offer_c = {"id": self.launch_c, "hub_session_id": self.succ_c, "intake_task_id": self.intake_id, "cwd": str(self.wt)}
        with self.assertRaisesRegex(ValueError, "worktree_lock_failed"):
            dispatcher._lock_session_worktree(offer_c, self.wt)

        # Order 2: B is terminal, explicit-cwd review C starts first and is admitted;
        # then ordinary successor D is refused while C is live.
        sessions[self.succ_b]["terminal"] = True
        sessions[self.succ_b]["native_terminal"] = True
        self.assertTrue(dispatcher._lock_session_worktree(offer_c, self.wt))

        succ_d = "d" * 32
        launch_d = "4" * 32
        sessions[succ_d] = {
            "session_id": succ_d,
            "intake_task_id": self.intake_id,
            "terminal": False,
            "native_terminal": False,
            "tag": "task-hub-" + launch_d,
        }
        offer_d = {"id": launch_d, "hub_session_id": succ_d, "intake_task_id": self.intake_id}
        with self.assertRaisesRegex(ValueError, "worktree_lock_failed"):
            dispatcher._lock_session_worktree(offer_d, self.wt)

    def test_live_or_unknown_owner_refuses_all_paths(self):
        for owner_state in (
            {"terminal": False, "native_terminal": True},
            {"terminal": True, "native_terminal": False},
            None,
        ):
            with self.subTest(owner_state=owner_state):
                sessions = {
                    self.succ_b: {
                        "session_id": self.succ_b,
                        "intake_task_id": self.intake_id,
                        "terminal": False,
                        "native_terminal": False,
                        "tag": "task-hub-" + self.launch_b,
                    },
                }
                if owner_state is not None:
                    sessions[self.owner_a] = {
                        "session_id": self.owner_a,
                        "intake_task_id": self.intake_id,
                        "tag": "task-hub-" + self.launch_a,
                        **owner_state,
                    }
                dispatcher = Dispatcher({}, self._make_hub_state(sessions))
                offer_b = {"id": self.launch_b, "hub_session_id": self.succ_b, "intake_task_id": self.intake_id}
                with self.assertRaisesRegex(ValueError, "worktree_lock_failed"):
                    dispatcher._lock_session_worktree(offer_b, self.wt)


if __name__ == "__main__":
    unittest.main()
