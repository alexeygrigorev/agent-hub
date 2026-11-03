"""Intake ccc50cca: read-only/detached review sessions never start inside a
linked checkout that a live implementer holds locked."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_hub_host import worktree_guard as guard
from agent_hub_host.intake_worktree import launch_cwd, read_only_cwd


def _git(*args):
    return subprocess.run(["git", *args], capture_output=True, text=True, check=True)


class ReadOnlyCwd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(os.path.realpath(self.tmp.name))
        # TMPDIR may itself sit inside a linked checkout; keep git discovery local.
        ceiling = patch.dict(os.environ, {"GIT_CEILING_DIRECTORIES": str(base)})
        ceiling.start()
        self.addCleanup(ceiling.stop)
        self.main, self.tree = base / "git" / "demo", base / "git" / "demo-wt-12345678"
        _git("init", "-q", str(self.main))
        _git("-C", str(self.main), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
             "commit", "-q", "--allow-empty", "-m", "initial")
        _git("-C", str(self.main), "worktree", "add", "-q", "--detach", str(self.tree))
        (self.main / "src").mkdir()
        (self.tree / "src").mkdir()
        (self.tree / "only-here").mkdir()
        self.assertTrue(guard.ensure_locked("git", str(self.tree), "a" * 32, "intake-1"))
        self.addCleanup(lambda: guard.unlock_worktree("git", str(self.tree)))

    def test_maps_linked_checkout_and_nested_paths_to_main_checkout(self):
        self.assertEqual(read_only_cwd(self.tree), self.main)
        self.assertEqual(read_only_cwd(self.tree / "src"), self.main / "src")
        self.assertEqual(read_only_cwd(self.tree / "only-here"), self.main)
        # Already outside any linked checkout: unchanged.
        self.assertEqual(read_only_cwd(self.main / "src"), self.main / "src")
        plain = Path(self.tmp.name) / "plain"
        plain.mkdir()
        self.assertEqual(read_only_cwd(plain), plain)
        # The mapped cwd passes the start guard while the implementer's lock stays.
        reviewer = "b" * 32
        self.assertFalse(guard.protect_bound_worktree("git", str(self.tree / "src"), reviewer, "intake-1"))
        self.assertTrue(guard.protect_bound_worktree("git", str(read_only_cwd(self.tree / "src")), reviewer, "intake-1"))
        entry = guard.find_entry(guard.list_entries("git", str(self.tree)), str(self.tree))
        self.assertEqual(entry["reason"], guard.lock_reason("a" * 32, "intake-1"))

    def test_launch_cwd_applies_only_to_read_only_or_detached_plans_without_explicit_cwd(self):
        def hub(plan):
            calls = []
            def request(method, path, body=None):
                calls.append(path)
                if path.endswith("/plan"):
                    return {"plan": plan}
                return {}
            return request, calls
        for flag in ({"read_only": True}, {"detached": True}):
            request, _ = hub({"mode": "none", "task_id": "intake-1", **flag})
            self.assertEqual(launch_cwd(request, {"id": "l1", "cwd": None}, self.tree, self.tree / "src"),
                             (self.main / "src", "", None))
            # Explicit caller cwd keeps the requested cwd.
            self.assertEqual(launch_cwd(request, {"id": "l1", "cwd": "src"}, self.tree, self.tree / "src"),
                             (self.tree / "src", "", None))
        # Plain mode none keeps the requested cwd.
        request, _ = hub({"mode": "none", "task_id": "intake-1"})
        self.assertEqual(launch_cwd(request, {"id": "l1", "cwd": None}, self.tree, self.tree / "src"),
                         (self.tree / "src", "", None))


if __name__ == "__main__":
    unittest.main()
