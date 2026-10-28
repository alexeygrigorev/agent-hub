"""Intake 4b161918 host side: path guard, status, remove, ensure, command poll, fleet hook.

Every repository here is synthetic and lives under a disposable HOME inside
the test TMPDIR (``<repo>/.local/tmp`` under the full runner). Git global and
system config are isolated so no real repository or setting is touched.
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_hub_host import fleet, intake_worktree as iw


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


class GitSandbox(unittest.TestCase):
    """Disposable HOME with ~/git/repo cloned from a bare origin."""

    def setUp(self):
        base = os.environ.get("TMPDIR") or None
        self._tmp = tempfile.TemporaryDirectory(prefix="intake-wt-", dir=base)
        self.home = os.path.realpath(self._tmp.name)
        self.assertNotIn("/prod", self.home)
        gitconfig = os.path.join(self.home, ".gitconfig")
        Path(gitconfig).write_text("[user]\n\tname = Test\n\temail = test@example.invalid\n"
                                   "[init]\n\tdefaultBranch = main\n[advice]\n\tdetachedHead = false\n")
        # The ceiling stops git discovery at the disposable HOME, so a
        # directory here never resolves to the enclosing real checkout.
        env = {"HOME": self.home, "GIT_CONFIG_GLOBAL": gitconfig, "GIT_CONFIG_NOSYSTEM": "1",
               "GIT_CEILING_DIRECTORIES": self.home,
               "XDG_CONFIG_HOME": os.path.join(self.home, ".config"),
               "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
               "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.invalid"}
        self._env = patch.dict(os.environ, env)
        self._env.start()
        self.gitdir = os.path.join(self.home, "git")
        os.makedirs(self.gitdir)
        self.origin = os.path.join(self.home, "remote", "origin.git")
        os.makedirs(self.origin)
        _git(self.origin, "init", "--bare", "-b", "main")
        seed = os.path.join(self.home, "remote", "seed")
        _git(os.path.dirname(seed), "clone", "-q", self.origin, seed)
        Path(seed, "README").write_text("seed\n")
        _git(seed, "add", "README")
        _git(seed, "commit", "-qm", "seed")
        _git(seed, "push", "-q", "origin", "HEAD:main")
        self.repo = os.path.join(self.gitdir, "repo")
        _git(self.gitdir, "clone", "-q", self.origin, self.repo)

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def add_worktree(self, name, commit=False):
        path = os.path.join(self.gitdir, name)
        _git(self.repo, "worktree", "add", "-q", "-b", name, path, "origin/main")
        if commit:
            Path(path, "work.txt").write_text(name + "\n")
            _git(path, "add", "work.txt")
            _git(path, "commit", "-qm", "work " + name)
        return path


class PathGuardTests(unittest.TestCase):
    home = "/home/someone"

    def test_accepts_direct_child_with_wt(self):
        self.assertEqual(iw.path_guard("/home/someone/git/x-wt-y", self.home), "/home/someone/git/x-wt-y")

    def test_rejections(self):
        cases = {
            "/home/someone/other/x-wt-y": "directly under",
            "/home/someone/git/x/x-wt-y": "directly under",
            "/home/someone/git/plain": "-wt-",
            "/home/someone/git/prod-wt-y": "/prod",
            "/home/someone/git/x-wt-y/prod": "/prod",
            "git/x-wt-y": "absolute",
            "/home/someone/git/../git/x-wt-y": "normalized",
            "/home/someone/git/./x-wt-y": "normalized",
            "": "required",
            None: "required",
        }
        for path, reason in cases.items():
            with self.subTest(path=path):
                with self.assertRaisesRegex(iw.WorktreeGuardError, reason):
                    iw.path_guard(path, self.home)

    def test_rejects_symlink(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR") or None) as tmp:
            home = os.path.realpath(tmp)
            os.makedirs(os.path.join(home, "git", "real-wt-a"))
            os.symlink(os.path.join(home, "git", "real-wt-a"), os.path.join(home, "git", "link-wt-a"))
            self.assertTrue(iw.path_guard(os.path.join(home, "git", "real-wt-a"), home))
            with self.assertRaisesRegex(iw.WorktreeGuardError, "symlink"):
                iw.path_guard(os.path.join(home, "git", "link-wt-a"), home)
            # A symlinked ~/git itself is refused too.
            os.makedirs(os.path.join(home, "h2"))
            os.symlink(os.path.join(home, "git"), os.path.join(home, "h2", "git"))
            with self.assertRaisesRegex(iw.WorktreeGuardError, "symlink"):
                iw.path_guard(os.path.join(home, "h2", "git", "real-wt-a"), os.path.join(home, "h2"))

    def test_default_home_from_environment(self):
        with patch.dict(os.environ, {"HOME": "/home/other"}):
            self.assertEqual(iw.path_guard("/home/other/git/a-wt-b"), "/home/other/git/a-wt-b")
            with self.assertRaises(iw.WorktreeGuardError):
                iw.path_guard("/home/someone/git/a-wt-b")


class StatusTests(GitSandbox):
    def test_clean_merged(self):
        path = self.add_worktree("repo-wt-clean")
        status = iw.worktree_status(path)
        self.assertEqual(set(status), set(iw.STATUS_KEYS))
        self.assertEqual({k: status[k] for k in ("present", "dirty", "dirty_count", "unpushed", "unmerged", "merged", "branch", "error")},
                         {"present": True, "dirty": False, "dirty_count": 0, "unpushed": 0, "unmerged": 0,
                          "merged": True, "branch": "repo-wt-clean", "error": None})
        self.assertEqual(status["head"], _git(path, "rev-parse", "HEAD").strip())
        self.assertIsInstance(status["checked_at"], int)

    def test_dirty_counts_untracked_and_modified(self):
        path = self.add_worktree("repo-wt-dirty")
        Path(path, "new.txt").write_text("x")
        Path(path, "README").write_text("changed")
        status = iw.worktree_status(path)
        self.assertTrue(status["dirty"])
        self.assertEqual(status["dirty_count"], 2)

    def test_unmerged_and_unpushed(self):
        path = self.add_worktree("repo-wt-ahead", commit=True)
        status = iw.worktree_status(path)
        self.assertEqual((status["unmerged"], status["merged"], status["unpushed"]), (1, False, 1))
        _git(path, "push", "-q", "origin", "repo-wt-ahead")
        status = iw.worktree_status(path)
        self.assertEqual((status["unmerged"], status["merged"], status["unpushed"]), (1, False, 0))

    def test_absent_uses_main_repo_branch(self):
        path = self.add_worktree("repo-wt-gone")
        _git(self.repo, "worktree", "remove", path)
        status = iw.worktree_status(path, branch="repo-wt-gone")
        self.assertEqual((status["present"], status["merged"], status["unmerged"]), (False, True, 0))
        ahead = self.add_worktree("repo-wt-gone2", commit=True)
        _git(self.repo, "worktree", "remove", ahead)
        status = iw.worktree_status(ahead, branch="repo-wt-gone2")
        self.assertEqual((status["present"], status["merged"], status["unmerged"]), (False, False, 1))
        unknown = iw.worktree_status(ahead)
        self.assertEqual((unknown["present"], unknown["merged"]), (False, None))
        missing = iw.worktree_status(os.path.join(self.gitdir, "nope-wt-x"), branch="nope-wt-x")
        self.assertEqual((missing["present"], missing["merged"]), (False, None))
        self.assertIsNotNone(missing["error"])

    def test_fetch_failure_leaves_merge_unknown(self):
        path = self.add_worktree("repo-wt-offline")
        _git(self.repo, "remote", "set-url", "origin", os.path.join(self.home, "missing.git"))
        status = iw.worktree_status(path)
        self.assertIsNone(status["merged"])
        self.assertIsNone(status["unmerged"])
        self.assertIn("fetch", status["error"])
        self.assertTrue(status["present"])


class RemoveTests(GitSandbox):
    def remove(self, path, branch=None, **kw):
        kw.setdefault("live_check", lambda p: [])
        return iw.remove_worktree(path, branch, home=self.home, **kw)

    def test_success_removes_prunes_and_deletes_branch(self):
        path = self.add_worktree("repo-wt-done")
        seen = []

        def runner(argv, cwd, timeout=iw.GIT_TIMEOUT):
            seen.append((argv[1:], cwd))
            return iw.default_runner(argv, cwd, timeout)
        result = self.remove(path, "repo-wt-done", runner=runner)
        self.assertEqual(result["outcome"], "ok", result)
        self.assertTrue(result["removed"])
        self.assertTrue(result["branch_deleted"])
        self.assertIsNone(result["reason"])
        self.assertFalse(os.path.exists(path))
        self.assertEqual((result["status"]["present"], result["status"]["merged"]), (False, True))
        self.assertNotIn(path, _git(self.repo, "worktree", "list", "--porcelain"))
        self.assertEqual(_git(self.repo, "branch", "--list", "repo-wt-done"), "")
        args = [a for a, _ in seen]
        self.assertIn((["worktree", "remove", path], self.repo), seen)
        self.assertIn(["worktree", "prune"], args)
        self.assertIn(["branch", "-d", "repo-wt-done"], args)
        self.assertFalse(any("--force" in a or "-D" in a or "-f" in a for a in args))

    def test_squash_equivalent_removes_but_reports_branch_kept(self):
        path = self.add_worktree("repo-wt-squash", commit=True)
        # Land an equivalent commit on main (cherry-pick) so cherry has no '+'.
        main = os.path.join(self.home, "remote", "seed")
        _git(main, "fetch", "-q", self.repo, "repo-wt-squash")
        _git(main, "cherry-pick", "-x", "FETCH_HEAD")
        _git(main, "push", "-q", "origin", "HEAD:main")
        _git(path, "push", "-q", "origin", "repo-wt-squash")
        result = self.remove(path)
        self.assertEqual(result["outcome"], "ok", result)
        self.assertFalse(result["branch_deleted"])
        self.assertIn("branch -d failed", result["reason"])
        self.assertIn("repo-wt-squash", _git(self.repo, "branch", "--list", "repo-wt-squash"))

    def test_refusals(self):
        dirty = self.add_worktree("repo-wt-dirty")
        Path(dirty, "scratch").write_text("x")
        unpushed = self.add_worktree("repo-wt-unpushed", commit=True)
        pushed = self.add_worktree("repo-wt-pushed", commit=True)
        _git(pushed, "push", "-q", "origin", "repo-wt-pushed")
        locked = self.add_worktree("repo-wt-locked")
        _git(self.repo, "worktree", "lock", locked)
        detached = os.path.join(self.gitdir, "repo-wt-detached")
        _git(self.repo, "worktree", "add", "-q", "--detach", detached, "origin/main")
        plain = os.path.join(self.gitdir, "repo-wt-plain")
        os.makedirs(plain)
        cases = [
            (dirty, "dirty: 1 uncommitted changes"),
            (unpushed, "unpushed commits 1"),
            (pushed, "unmerged commits 1"),
            (locked, "worktree locked"),
            (detached, "detached HEAD"),
            (os.path.join(self.gitdir, "repo-wt-missing"), "not present"),
            (plain, "not a registered git worktree"),
            (os.path.join(self.home, "elsewhere-wt-x"), "path guard: worktree path must sit directly under"),
            (self.repo, "path guard: worktree basename must contain -wt-"),
        ]
        for path, reason in cases:
            with self.subTest(reason=reason):
                result = self.remove(path)
                self.assertEqual(result["outcome"], "refused", result)
                self.assertTrue(result["reason"].startswith(reason), result["reason"])
                self.assertFalse(result["removed"])
        for path in (dirty, unpushed, pushed, locked, detached, plain):
            self.assertTrue(os.path.isdir(path))
        clean = self.add_worktree("repo-wt-mismatch")
        result = self.remove(clean, "other-branch")
        self.assertEqual(result["reason"], "branch mismatch: worktree is on repo-wt-mismatch")

    def test_merge_unknown_refused(self):
        path = self.add_worktree("repo-wt-offline")
        _git(self.repo, "remote", "set-url", "origin", os.path.join(self.home, "missing.git"))
        result = self.remove(path)
        self.assertEqual(result["outcome"], "refused")
        self.assertTrue(result["reason"].startswith("merge state unknown: git fetch failed"), result["reason"])
        self.assertTrue(os.path.isdir(path))

    def test_live_process_refused(self):
        path = self.add_worktree("repo-wt-live")
        proc = subprocess.Popen(["sleep", "30"], cwd=os.path.join(path))
        try:
            result = iw.remove_worktree(path, home=self.home)
            self.assertEqual(result["outcome"], "refused", result)
            self.assertEqual(result["reason"], f"live process has cwd inside worktree (pid {proc.pid})")
            self.assertTrue(os.path.isdir(path))
        finally:
            proc.kill()
            proc.wait()
        self.assertEqual(iw.live_processes_in(path), [])
        result = self.remove(path, live_check=lambda p: iw.live_processes_in(p, proc_root=os.path.join(self.home, "noproc")))
        self.assertEqual((result["outcome"], result["reason"]), ("refused", "cannot verify live processes"))
        # Our own cwd inside the tree is excluded (the host process itself).
        old = os.getcwd()
        os.chdir(path)
        try:
            self.assertEqual(iw.live_processes_in(path), [])
        finally:
            os.chdir(old)


class EnsureTests(GitSandbox):
    def test_create_reuse_and_recreate(self):
        plan = {"mode": "create", "task_id": "abcd1234-0000", "suffix": "wt-abcd1234"}
        out = iw.ensure_intake_worktree(self.repo, plan, home=self.home)
        path = os.path.join(self.gitdir, "repo-wt-abcd1234")
        self.assertEqual(out, {"action": "created", "path": path, "branch": "repo-wt-abcd1234", "error": None})
        self.assertEqual(_git(path, "rev-parse", "HEAD"), _git(self.repo, "rev-parse", "origin/main"))
        self.assertEqual(_git(path, "branch", "--show-current").strip(), "repo-wt-abcd1234")
        self.assertEqual(iw.ensure_intake_worktree(self.repo, plan, home=self.home)["action"], "reused")
        resume = {"mode": "resume", "task_id": "abcd1234", "worktree_id": "w1", "path": path, "branch": "repo-wt-abcd1234"}
        self.assertEqual(iw.ensure_intake_worktree(self.repo, resume, home=self.home)["action"], "reused")
        _git(self.repo, "worktree", "remove", path)
        out = iw.ensure_intake_worktree(self.repo, resume, home=self.home)
        self.assertEqual(out["action"], "recreated", out)
        self.assertTrue(os.path.isdir(path))
        # Directory vanished behind git's back: stale registration is pruned, then recreated.
        shutil.rmtree(path)
        out = iw.ensure_intake_worktree(self.repo, resume, home=self.home)
        self.assertEqual(out["action"], "recreated", out)
        self.assertIsNone(iw.ensure_intake_worktree(self.repo, {"mode": "none"}, home=self.home))
        self.assertIsNone(iw.ensure_intake_worktree(self.repo, None, home=self.home))

    def test_failures_never_delete(self):
        notrepo = os.path.join(self.gitdir, "notrepo")
        os.makedirs(notrepo)
        plan = {"mode": "create", "task_id": "abcd1234", "suffix": "wt-abcd1234"}
        out = iw.ensure_intake_worktree(notrepo, plan, home=self.home)
        self.assertEqual((out["action"], out["error"]), ("failed", "workspace is not a git repository"))
        nested = os.path.join(self.home, "elsewhere", "repo")
        out = iw.ensure_intake_worktree(nested, plan, home=self.home)
        self.assertEqual(out["action"], "failed")
        self.assertIn("path guard", out["error"])
        occupied = os.path.join(self.gitdir, "repo-wt-abcd1234")
        os.makedirs(occupied)
        Path(occupied, "keep").write_text("x")
        out = iw.ensure_intake_worktree(self.repo, plan, home=self.home)
        self.assertEqual(out["action"], "failed")
        self.assertTrue(Path(occupied, "keep").exists())
        _git(self.repo, "remote", "remove", "origin")
        out = iw.ensure_intake_worktree(self.repo, dict(plan, suffix="wt-ffff0000"), home=self.home)
        self.assertEqual((out["action"], out["error"]), ("failed", "no origin remote"))
        self.assertEqual(iw.ensure_intake_worktree(self.repo, {"mode": "create", "suffix": "../x"}, home=self.home)["action"], "failed")


class PollTests(GitSandbox):
    def test_runs_commands_and_posts_results(self):
        clean = self.add_worktree("repo-wt-c1")
        dirty = self.add_worktree("repo-wt-c2")
        Path(dirty, "x").write_text("x")
        commands = [
            {"id": "cmd1", "kind": "status", "worktree_id": "w1", "task_id": "t", "path": clean, "branch": "repo-wt-c1"},
            {"id": "cmd2", "kind": "remove", "worktree_id": "w2", "task_id": "t", "path": dirty, "branch": "repo-wt-c2"},
            {"id": "cmd3", "kind": "remove", "worktree_id": "w1", "task_id": "t", "path": clean, "branch": "repo-wt-c1"},
            {"id": "cmd4", "kind": "explode", "path": clean},
            {"id": "../bad", "kind": "status", "path": clean},
            {"id": "cmd5", "kind": "status", "path": "/etc/passwd"},
        ] + [{"id": f"extra{i}", "kind": "status", "path": clean} for i in range(10)]
        posted = []

        def request(method, path, body=None):
            if method == "GET":
                self.assertEqual(path, "/api/worktree-host/commands")
                return {"commands": commands}
            posted.append((path, body))
            return {"ok": True}
        with patch.object(iw, "live_processes_in", lambda p: []):
            out = iw.poll_commands_once(request, home=self.home)
        # At most 10 commands per cycle: 9 valid ones handled, 1 invalid id.
        self.assertEqual(out, {"supported": True, "handled": 9, "errors": 1})
        results = dict(posted)
        self.assertEqual(len(posted), 9)
        first = results["/api/worktree-host/commands/cmd1/result"]
        self.assertEqual(first["outcome"], "ok")
        self.assertTrue(first["status"]["merged"])
        self.assertEqual(results["/api/worktree-host/commands/cmd2/result"]["reason"], "dirty: 1 uncommitted changes")
        removed = results["/api/worktree-host/commands/cmd3/result"]
        self.assertEqual((removed["outcome"], removed["removed"], removed["branch_deleted"]), ("ok", True, True))
        self.assertFalse(os.path.exists(clean))
        self.assertEqual(results["/api/worktree-host/commands/cmd4/result"]["outcome"], "error")
        self.assertEqual(results["/api/worktree-host/commands/cmd5/result"]["outcome"], "refused")

    def test_old_hub_and_command_exceptions(self):
        def old(method, path, body=None):
            raise ValueError("HTTP request failed with status 404")
        self.assertEqual(iw.poll_commands_once(old), {"supported": False, "handled": 0, "errors": 0})

        def broken(method, path, body=None):
            raise ValueError("HTTP request failed with status 500")
        with self.assertRaises(ValueError):
            iw.poll_commands_once(broken)
        posted = []
        path = self.add_worktree("repo-wt-boom")

        def request(method, p, body=None):
            if method == "GET":
                return {"commands": [{"id": "c1", "kind": "remove", "path": path}]}
            posted.append(body)

        def runner(argv, cwd, timeout=None):
            raise RuntimeError("boom")
        with patch.object(iw, "remove_worktree", side_effect=RuntimeError("boom")):
            iw.poll_commands_once(request, runner=runner, home=self.home)
        self.assertEqual(posted[0]["outcome"], "error")
        self.assertIn("RuntimeError: boom", posted[0]["reason"])


class SessionHostHookTests(unittest.TestCase):
    def test_step_swallows_errors_and_old_hub(self):
        import contextlib
        import io
        from types import SimpleNamespace
        from agent_hub_host import session_host
        seen = []

        def old(method, path, body=None):
            seen.append(path)
            raise ValueError("HTTP request failed with status 404")

        def broken(method, path, body=None):
            raise ConnectionError("down")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            session_host.worktree_commands_step_once(SimpleNamespace(request=old, settings={}))
            session_host.worktree_commands_step_once(SimpleNamespace(request=broken, settings={}))
        self.assertEqual(seen, ["/api/worktree-host/commands"])
        lines = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual(lines, [{"event": "worktree_commands_error", "type": "ConnectionError"}])


class FleetHookTests(GitSandbox):
    OFFER_ID = "12345678-1234-1234-1234-123456789abc"

    def settings(self):
        return {"version": 1, "workspaces": {"demo": self.repo}, "subscriptions": [
            {"service": "codex", "account_label": "shared-codex", "plan": "pro", "engine": "codex",
             "profile": None, "admission_argv": ["maintained-gate"]}]}

    def offer(self, **kw):
        return {"id": self.OFFER_ID, "engine": "codex", "profile": None, "account": "shared-codex",
                "mode": "interactive", "workspace": "demo", "prompt": "", **kw}

    def runner_for(self, calls, cwd=None):
        def runner(argv, **kw):
            calls.append((argv, kw))
            if argv[0] == "maintained-gate":
                return {"launch_allowed": True, "profile_supported": True, "account_label": "shared-codex",
                        "observed_at": fleet.utcnow()}
            receipt = {"id": "native-session", "workspace": self.repo, "tag": "hub-" + self.OFFER_ID,
                       "engine": "codex", "profile": None, "phase": "running"}
            if cwd:
                receipt["cwd"] = cwd
            return receipt
        return runner

    def test_plan_create_sets_cwd_and_note(self):
        path = os.path.join(self.gitdir, "repo-wt-abcd1234")
        http, calls = [], []

        def request(method, p, body=None):
            http.append((method, p, body))
            if p == "/api/worktree-host/plan":
                return {"plan": {"mode": "create", "task_id": "abcd1234-5678", "suffix": "wt-abcd1234"}}
            return {}
        d = fleet.Dispatcher(self.settings(), request, self.runner_for(calls, cwd=path))
        # AC1 lock identity: worktree launches carry the canonical Hub session
        # and intake ids, exactly as /api/fleet/offers enrichment provides.
        result = d.dispatch(self.offer(hub_session_id=self.OFFER_ID, intake_task_id="abcd1234-5678"))
        self.assertEqual(result["state"], "running", result)
        start = [a for a, _ in calls if a[:2] == ["aplexer", "start"]][0]
        self.assertEqual(start[start.index("--cwd") + 1], path)
        self.assertEqual(start[start.index("--workspace") + 1], self.repo)
        preamble = [a for a, _ in calls if a[:3] == ["aplexer", "message", "send"]][0][-1]
        self.assertTrue(preamble.startswith(
            f"Hub prepared the dedicated worktree for intake abcd1234: {path} (branch repo-wt-abcd1234, "
            "from origin/main). Work and commit there; do not create another worktree.\n"))
        paths = [p for _, p, _ in http]
        self.assertEqual(paths[0], "/api/worktree-host/plan")
        self.assertEqual(http[0][2], {"launch_id": self.OFFER_ID})
        observe = [b for _, p, b in http if p == "/api/worktree-host/observe"]
        self.assertEqual(observe, [{"launch_id": self.OFFER_ID, "action": "created", "path": path,
                                    "branch": "repo-wt-abcd1234"}])
        self.assertTrue(os.path.isdir(path))

    def test_old_hub_404_fails_open(self):
        http, calls = [], []

        def request(method, p, body=None):
            http.append((method, p, body))
            if p.startswith("/api/worktree-host/"):
                raise ValueError("HTTP request failed with status 404")
            return {}
        d = fleet.Dispatcher(self.settings(), request, self.runner_for(calls))
        self.assertEqual(d.dispatch(self.offer())["state"], "running")
        start = [a for a, _ in calls if a[:2] == ["aplexer", "start"]][0]
        self.assertNotIn("--cwd", start)
        self.assertEqual([p for _, p, _ in http if p.startswith("/api/worktree-host/")], ["/api/worktree-host/plan"])

    def test_git_failure_fails_open_and_observes(self):
        _git(self.repo, "remote", "remove", "origin")
        http, calls = [], []

        def request(method, p, body=None):
            http.append((method, p, body))
            if p == "/api/worktree-host/plan":
                return {"plan": {"mode": "create", "task_id": "abcd1234", "suffix": "wt-abcd1234"}}
            return {}
        d = fleet.Dispatcher(self.settings(), request, self.runner_for(calls))
        self.assertEqual(d.dispatch(self.offer())["state"], "running")
        start = [a for a, _ in calls if a[:2] == ["aplexer", "start"]][0]
        self.assertNotIn("--cwd", start)
        observe = [b for _, p, b in http if p == "/api/worktree-host/observe"]
        self.assertEqual(observe[0]["action"], "failed")
        self.assertEqual(observe[0]["error"], "no origin remote")

    def test_explicit_cwd_reports_plan_but_never_overrides_cwd(self):
        os.makedirs(os.path.join(self.repo, "sub"))
        http, calls = [], []

        def request(method, path, body=None):
            http.append(path)
            if path == "/api/worktree-host/plan":
                # Even a (misbehaving) resume plan never replaces an explicit cwd.
                return {"plan": {"mode": "create", "task_id": "abcd1234", "suffix": "wt-abcd1234"}}
            return {}
        d = fleet.Dispatcher(self.settings(), request, self.runner_for(calls, cwd=os.path.join(self.repo, "sub")))
        self.assertEqual(d.dispatch(self.offer(cwd="sub"))["state"], "running")
        self.assertIn("/api/worktree-host/plan", http)
        start = [a for a, _ in calls if a[:2] == ["aplexer", "start"]][0]
        self.assertEqual(start[start.index("--cwd") + 1], os.path.join(self.repo, "sub"))

    def test_headless_task_units_uses_worktree_cwd_and_tmpdir(self):
        cfg = self.settings()
        launcher_root = os.path.join(self.home, "launcher")
        os.makedirs(launcher_root)
        cfg["launcher"] = {"argv": ["python3", "-m", "launcher"], "root": launcher_root,
                           "config_dir": os.path.join(launcher_root, "config")}
        cfg["subscriptions"][0].update(launcher_provider="codex", launcher_model="gpt-6-sol")
        path = os.path.join(self.gitdir, "repo-wt-abcd1234")
        calls = []

        def runner(argv, **kw):
            calls.append(argv)
            if argv[0] == "maintained-gate":
                return {"launch_allowed": True, "profile_supported": True, "account_label": "shared-codex",
                        "observed_at": fleet.utcnow()}
            if "plan" in argv:
                return {"eligible": [{"provider": "codex", "model": "gpt-6-sol", "task_fit": 1.0}]}
            if "run" in argv:
                return {"receipt": {"unit_name": "headless-unit", "exit_code": 0}}
            return {}

        def request(method, p, body=None):
            if p == "/api/worktree-host/plan":
                return {"plan": {"mode": "create", "task_id": "abcd1234", "suffix": "wt-abcd1234"}}
            if p.endswith("/result"):
                return {"id": p.split("/")[-2], "state": body.get("state")}
            return {}
        d = fleet.Dispatcher(cfg, request, runner)
        d.dispatch(self.offer(mode="headless", prompt="Do the thing",
                              hub_session_id=self.OFFER_ID, intake_task_id="abcd1234"))
        run = [a for a in calls if "run" in a][-1]
        self.assertEqual(run[run.index("--cwd") + 1], path)
        self.assertEqual(run[run.index("--tmpdir") + 1], os.path.join(path, ".local", "tmp"))
        self.assertTrue(os.path.isdir(os.path.join(path, ".local", "tmp")))
        submit = [a for a in calls if "submit" in a][-1]
        payload = json.loads(submit[submit.index("--payload") + 1])
        self.assertEqual(payload["cwd"], path)
        self.assertTrue(payload["goal"].startswith("Hub prepared the dedicated worktree for intake abcd1234: "))

    def test_headless_aplexer_uses_worktree_cwd_and_matches_receipt_workspace(self):
        cfg = self.settings()
        launcher_root = os.path.join(self.home, "launcher")
        os.makedirs(launcher_root)
        cfg["launcher"] = {"argv": ["python3", "-m", "launcher"], "root": launcher_root,
                           "config_dir": os.path.join(launcher_root, "config")}
        cfg["subscriptions"][0].update(
            service="zcodex",
            engine="zcodex",
            account_label="shared-zai",
            launcher_provider="zai",
            launcher_model="glm-5.3-flash",
        )
        path = os.path.join(self.gitdir, "repo-wt-abcd1234")
        native_uuid = "55f09886-da2a-408a-bf10-b389947b4fba"
        calls = []

        def runner(argv, **kw):
            calls.append(argv)
            if argv[0] == "maintained-gate":
                return {"launch_allowed": True, "profile_supported": True, "account_label": "shared-zai",
                        "observed_at": fleet.utcnow()}
            if "plan" in argv:
                return {"eligible": [{"provider": "zai", "model": "glm-5.3-flash", "task_fit": 1.0}]}
            if "run" in argv:
                return {"receipt": {
                    "session_id": native_uuid,
                    "state": "running",
                    "first_action_verified": True,
                    "engine": "zcodex",
                    "workspace": path,
                    "tag": "task-hub-" + self.OFFER_ID,
                }}
            return {}

        def request(method, p, body=None):
            if p == "/api/worktree-host/plan":
                return {"plan": {"mode": "create", "task_id": "abcd1234", "suffix": "wt-abcd1234"}}
            if p.endswith("/result"):
                return {"id": p.split("/")[-2], "state": body.get("state")}
            return {}
        d = fleet.Dispatcher(cfg, request, runner)
        res = d.dispatch(self.offer(mode="headless", engine="zcodex", account="shared-zai", prompt="Do the thing",
                                    hub_session_id=self.OFFER_ID, intake_task_id="abcd1234"))
        self.assertEqual(res["state"], "running", res)
        run = [a for a in calls if "run" in a][-1]
        self.assertEqual(run[run.index("--backend") + 1], "aplexer")
        self.assertEqual(run[run.index("--cwd") + 1], path)
        self.assertEqual(run[run.index("--tmpdir") + 1], os.path.join(path, ".local", "tmp"))
        submit = [a for a in calls if "submit" in a][-1]
        payload = json.loads(submit[submit.index("--payload") + 1])
        self.assertEqual(payload["cwd"], path)
        self.assertIn(os.path.join(path, ".local", "hub-results", self.OFFER_ID + ".json"), payload["goal"])


if __name__ == "__main__":
    unittest.main()
