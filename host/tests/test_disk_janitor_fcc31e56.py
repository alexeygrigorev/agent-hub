"""Safety rules of the disk floor janitor (intake fcc31e56).

Every test builds disposable synthetic git repos inside a private TemporaryDirectory
(under the runner's TMPDIR); no real worktree, session or Hub is touched.
"""

import json
import os
import subprocess
import tarfile
import tempfile
import time
import unittest

from agent_hub_host import disk_janitor, disk_monitor
from agent_hub_host.disk_janitor import GIB, DiskJanitor, load_config, protected_reason

GIT_ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, env=GIT_ENV, check=True, capture_output=True, text=True).stdout


def age(path, hours):
    """Backdate a tree's top level and its worktree gitdir so it looks idle."""
    stamp = time.time() - hours * 3600
    targets = [path]
    for root, dirs, files in os.walk(path):
        targets += [os.path.join(root, n) for n in dirs + files]
    dotgit = os.path.join(path, ".git")
    if os.path.isfile(dotgit):
        with open(dotgit) as fh:
            gitdir = fh.read().split("gitdir:", 1)[1].strip()
        for root, dirs, files in os.walk(gitdir):
            targets += [os.path.join(root, n) for n in dirs + files]
    for target in targets:
        os.utime(target, (stamp, stamp), follow_symlinks=False)


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="janitor-")
        self.root = os.path.realpath(self.tmp.name)
        self.origin = os.path.join(self.root, "origin.git")
        self.ws = os.path.join(self.root, "ws")
        os.makedirs(self.ws)
        git(self.root, "init", "-q", "--bare", "-b", "main", self.origin)
        self.main = os.path.join(self.ws, "repo")
        git(self.root, "clone", "-q", self.origin, self.main)
        with open(os.path.join(self.main, ".gitignore"), "w") as fh:
            fh.write("node_modules/\n.local/\n")
        with open(os.path.join(self.main, "a.txt"), "w") as fh:
            fh.write("a\n")
        git(self.main, "add", ".")
        git(self.main, "commit", "-qm", "init")
        git(self.main, "push", "-q", "origin", "HEAD:main")
        self.free = 50 * GIB
        self.open_paths = []
        self.sessions = []
        self.facts = []
        self.hub_ok = True
        self.archive = os.path.join(self.root, "netdisk", "archive")
        os.makedirs(self.archive)

    def tearDown(self):
        self.tmp.cleanup()

    def worktree(self, name, *, idle_hours=5.0, commit=False, push=False):
        path = os.path.join(self.ws, name)
        git(self.main, "worktree", "add", "-q", "-b", name, path, "origin/main")
        if commit:
            with open(os.path.join(path, name + ".txt"), "w") as fh:
                fh.write(name)
            git(path, "add", ".")
            git(path, "commit", "-qm", name)
            if push:
                git(path, "push", "-q", "origin", f"HEAD:refs/heads/{name}")
        age(path, idle_hours)
        return path

    def config(self, **overrides):
        raw = {"workspace_roots": [self.ws], "mount": self.root, "floor_gb": 100, "target_gb": 200,
               "archive_dir": self.archive, "grace_hours": 2, "orphan_idle_hours": 24, "archive_idle_hours": 72}
        raw.update(overrides)
        return raw

    def janitor(self, **overrides):
        def hub(method, path):
            if not self.hub_ok:
                raise OSError("hub down")
            return {"sessions": self.facts}
        return DiskJanitor(config=self.config(**overrides), state_dir=os.path.join(self.root, "state"),
                           hub_request=hub, sessions_fn=lambda: self.sessions,
                           open_paths_fn=lambda: list(self.open_paths), free_fn=lambda m: self.free,
                           ismount_fn=lambda p: p == os.path.join(self.root, "netdisk"),
                           dev_fn=lambda p: 2 if "netdisk" in p else 1)

    def terminal_session(self, path, **extra):
        self.sessions.append({"id": "s-" + os.path.basename(path), "phase": "exited", "workspace": path, "cwd": path,
                              "last_activity_ms": (time.time() - 5 * 3600) * 1000, **extra})

    def removed_paths(self, result):
        return {r["path"] for r in result["removed"]}

    def kept(self, result, reason):
        return set(result["kept"].get(reason, []))


class TestWorkspaceSafety(Fixture):
    def test_terminal_clean_pushed_worktree_is_removed_after_grace(self):
        path = self.worktree("done-1")
        self.terminal_session(path)
        result = self.janitor().run_once()
        self.assertIn(path, self.removed_paths(result))
        self.assertFalse(os.path.exists(path))
        self.assertTrue(os.path.isdir(self.main), "main checkout is never removed")
        self.assertNotIn("done-1", git(self.main, "branch", "--list", "done-1"))

    def test_grace_period_keeps_recent_terminal_worktree(self):
        path = self.worktree("recent", idle_hours=0.5)
        self.terminal_session(path, last_activity_ms=time.time() * 1000)
        result = self.janitor().run_once()
        self.assertIn(path, self.kept(result, "grace_period"))
        self.assertTrue(os.path.isdir(path))

    def test_live_aplexer_session_keeps_worktree(self):
        path = self.worktree("live")
        self.sessions.append({"id": "x", "phase": "running", "workspace": self.main, "cwd": path})
        result = self.janitor().run_once()
        self.assertIn(path, self.kept(result, "live_session"))
        self.assertTrue(os.path.isdir(path))

    def test_done_stopped_and_failed_sessions_are_terminal(self):
        paths = [self.worktree("phase-" + phase, idle_hours=100) for phase in ("done", "stopped", "failed")]
        for phase, path in zip(("done", "stopped", "failed"), paths):
            self.terminal_session(path, phase=phase, last_activity_ms=(time.time() - 100 * 3600) * 1000)
        result = self.janitor().run_once()
        self.assertEqual(self.removed_paths(result), set(paths))

    def test_process_inside_keeps_worktree(self):
        path = self.worktree("busy")
        self.terminal_session(path)
        self.open_paths = [os.path.join(path, "sub", "file.log")]
        result = self.janitor().run_once()
        self.assertIn(path, self.kept(result, "process_inside"))
        self.assertTrue(os.path.isdir(path))

    def test_live_hub_session_and_intake_without_done_keep_worktree(self):
        live = self.worktree("hub-live")
        intake = self.worktree("hub-intake", idle_hours=100)
        unknown = self.worktree("hub-intake-unknown", idle_hours=100)
        self.facts = [
            {"workspace": live, "terminal": False, "native_terminal": True, "linked_intake_open": False, "protected": False},
            {"workspace": intake, "terminal": True, "native_terminal": True, "linked_intake_open": True, "protected": False},
            {"workspace": unknown, "terminal": True, "native_terminal": True, "linked_intake_open": None, "protected": False},
        ]
        self.terminal_session(live)
        # Maximum pressure and a long idle tree still do not override a missing DONE.
        self.free = 1 * GIB
        result = self.janitor().run_once()
        self.assertIn(live, self.kept(result, "live_hub_session"))
        self.assertIn(intake, self.kept(result, "intake_not_done"))
        self.assertIn(unknown, self.kept(result, "intake_not_done"))
        self.assertTrue(all(os.path.isdir(p) for p in (live, intake, unknown)))
        self.facts[1]["linked_intake_open"] = False
        result = self.janitor().run_once()
        self.assertIn(intake, self.removed_paths(result))

    def test_orphan_named_for_an_unlinked_intake_is_kept(self):
        path = self.worktree("agent-hub-wt-6b624e65", idle_hours=100)
        self.free = 1 * GIB
        result = self.janitor().run_once()
        self.assertIn(path, self.kept(result, "intake_unknown"))
        self.assertTrue(os.path.isdir(path))

    def test_intake_named_tree_with_only_a_terminal_aplexer_session_is_kept(self):
        # Review finding 2: a dead aplexer session says nothing about intake DONE.
        path = self.worktree("agent-hub-wt-7791a823", idle_hours=100)
        self.terminal_session(path, last_activity_ms=(time.time() - 100 * 3600) * 1000)
        self.free = 1 * GIB
        result = self.janitor().run_once()
        self.assertIn(path, self.kept(result, "intake_unknown"))
        self.assertTrue(os.path.isdir(path))

    def test_dirt_written_after_classification_is_never_removed(self):
        # Review finding 1: a file appearing between the check and the effect.
        path = self.worktree("late-dirt")
        self.terminal_session(path)
        real = disk_janitor.tree_bytes

        def write_then_measure(target, **kw):
            if target == path:
                with open(os.path.join(path, "late.txt"), "w") as fh:
                    fh.write("written mid-pass")
            return real(target, **kw)

        disk_janitor.tree_bytes = write_then_measure
        try:
            result = self.janitor().run_once()
        finally:
            disk_janitor.tree_bytes = real
        self.assertIn(path, self.kept(result, "uncommitted_changes"))
        with open(os.path.join(path, "late.txt")) as fh:
            self.assertEqual(fh.read(), "written mid-pass")

    def test_new_live_session_is_rechecked_before_worktree_removal(self):
        path = self.worktree("late-live")
        self.terminal_session(path)
        terminal = list(self.sessions)
        live = [{**terminal[0], "phase": "running"}]
        snapshots = [terminal, live]
        janitor = self.janitor()
        janitor.sessions_fn = lambda: snapshots.pop(0)
        result = janitor.run_once()
        self.assertIn(path, self.kept(result, "live_session"))
        self.assertTrue(os.path.isdir(path))

    def test_git_refusal_is_never_overridden_with_force(self):
        path = self.worktree("refused")
        with open(os.path.join(path, "untracked.txt"), "w") as fh:
            fh.write("keep me")
        self.assertFalse(self.janitor().remove_worktree(path, "refused"))
        self.assertTrue(os.path.isfile(os.path.join(path, "untracked.txt")))

    def test_no_durable_receipt_means_no_removal(self):
        # Review finding 4.
        path = self.worktree("no-receipt")
        self.terminal_session(path)
        janitor = self.janitor()
        janitor.state_dir = None
        result = janitor.run_once()
        self.assertIn(path, self.kept(result, "receipt_unwritable"))
        self.assertTrue(os.path.isdir(path))

    def test_unreadable_hub_or_aplexer_skips_whole_workspace_pass(self):
        path = self.worktree("unknown")
        self.terminal_session(path)
        self.hub_ok = False
        result = self.janitor().run_once()
        self.assertIn("hub_unreadable", result["errors"])
        self.assertTrue(os.path.isdir(path))
        self.hub_ok = True
        janitor = self.janitor()
        janitor.sessions_fn = lambda: None
        result = janitor.run_once()
        self.assertIn("aplexer_unreadable", result["errors"])
        self.assertTrue(os.path.isdir(path))

    def test_manual_lock_is_kept_and_hub_lock_needs_terminal_session(self):
        manual = self.worktree("locked")
        git(self.main, "worktree", "lock", "--reason", "Alexey keeps this", manual)
        sid = "d6bd8212-aa04-4639-b33c-f163899b30df"
        hub = self.worktree("hub-locked", idle_hours=30)
        git(self.main, "worktree", "lock", "--reason", f"hub-session {sid} intake none", hub)
        age(hub, 30)
        unknown = self.worktree("hub-locked-unknown", idle_hours=30)
        git(self.main, "worktree", "lock", "--reason", "hub-session 1a1433ea-dad6-4c4d-a693-5a2a13d8d355 intake none", unknown)
        age(unknown, 30)
        self.facts = [{"session_id": sid, "workspace": None, "terminal": False, "native_terminal": False,
                       "linked_intake_open": False, "protected": True}]
        result = self.janitor().run_once()
        self.assertIn(manual, self.kept(result, "git_locked"))
        self.assertIn(hub, self.kept(result, "live_hub_session"))
        self.assertIn(unknown, self.kept(result, "git_locked_unknown_session"))
        self.facts[0].update(terminal=True, native_terminal=True)
        result = self.janitor().run_once()
        self.assertIn(hub, self.removed_paths(result))
        self.assertTrue(os.path.isdir(manual) and os.path.isdir(unknown))
        self.assertFalse(os.path.exists(hub))

    def test_failed_removal_restores_hub_lock(self):
        sid = "d6bd8212-aa04-4639-b33c-f163899b30df"
        path = self.worktree("relock", idle_hours=30)
        reason = f"hub-session {sid} intake none"
        git(self.main, "worktree", "lock", "--reason", reason, path)
        janitor = self.janitor()
        real = disk_janitor._git
        disk_janitor._git = lambda argv, cwd, timeout=60: (subprocess.CompletedProcess(argv, 1, "", "no") if argv[:2] == ["worktree", "remove"] else real(argv, cwd, timeout))
        try:
            self.assertFalse(janitor.remove_worktree(path, None, unlock=True))
        finally:
            disk_janitor._git = real
        gitdir = disk_janitor.worktree_gitdir(path)
        with open(os.path.join(gitdir, "locked")) as fh:
            self.assertEqual(fh.read().strip(), reason)

    def _old_terminal(self, path):
        age(path, 100)
        self.terminal_session(path, last_activity_ms=(time.time() - 100 * 3600) * 1000)

    def test_unpushed_commits_are_bundled_verified_and_source_is_kept(self):
        path = self.worktree("unpushed", idle_hours=100, commit=True)
        self._old_terminal(path)
        self.free = 1 * GIB
        janitor = self.janitor()
        result = janitor.run_once()
        self.assertIn(path, self.kept(result, "unpushed_archive_kept"))
        self.assertTrue(os.path.isfile(os.path.join(path, "unpushed.txt")))
        self.assertTrue(os.path.isdir(path), "the original worktree is never removed")
        self.assertEqual(len(result["archived"]), 1, result)
        archive = result["archived"][0]["archive"]
        bundle = os.path.join(archive, "commits.bundle")
        self.assertTrue(os.path.isfile(bundle))
        self.assertTrue(os.path.isfile(os.path.join(archive, "meta.json")))
        self.assertEqual(git(path, "bundle", "list-heads", bundle).split()[0], git(path, "rev-parse", "HEAD").strip())

        again = janitor.run_once()
        self.assertEqual(again["archived"][0]["archive"], archive)
        self.assertTrue(again["archived"][0]["reused"])
        self.assertTrue(os.path.isdir(path))

    def test_unpushed_archive_requires_distinct_mounted_network_storage(self):
        path = self.worktree("unpushable", idle_hours=100, commit=True)
        self._old_terminal(path)
        janitor = self.janitor()
        janitor.dev_fn = lambda _p: 1
        result = janitor.run_once()
        self.assertIn(path, self.kept(result, "archive_archive_same_filesystem"))
        self.assertTrue(os.path.isdir(path))
        self.assertEqual(result["archived"], [])

    def test_unpushed_history_is_bundled_while_uncommitted_worktree_stays(self):
        path = self.worktree("unpushed-dirty", idle_hours=100, commit=True)
        with open(os.path.join(path, "local.txt"), "w") as fh:
            fh.write("not committed; preserve in the source worktree")
        self._old_terminal(path)
        result = self.janitor().run_once()
        self.assertIn(path, self.kept(result, "unpushed_archive_kept"))
        self.assertTrue(os.path.isdir(path))
        self.assertTrue(os.path.isfile(os.path.join(path, "local.txt")))
        self.assertTrue(os.path.isfile(os.path.join(result["archived"][0]["archive"], "commits.bundle")))

    def test_clean_pushed_feature_branch_can_be_removed_before_main_merge(self):
        path = self.worktree("unmerged", idle_hours=100, commit=True, push=True)
        git(path, "fetch", "-q", "origin")
        self._old_terminal(path)
        self.free = 1 * GIB
        result = self.janitor().run_once()
        self.assertIn(path, self.removed_paths(result))
        self.assertFalse(os.path.isdir(path))
        self.assertEqual(result["removed"][0]["reason"], "terminal_clean_pushed")

    def test_uncommitted_or_untracked_changes_are_never_removed(self):
        dirty = self.worktree("dirty", idle_hours=100)
        with open(os.path.join(dirty, "a.txt"), "a") as fh:
            fh.write("dirty\n")
        self._old_terminal(dirty)
        untracked = self.worktree("untracked", idle_hours=100)
        with open(os.path.join(untracked, "new.txt"), "w") as fh:
            fh.write("new\n")
        self._old_terminal(untracked)
        self.free = 1 * GIB
        result = self.janitor().run_once()
        self.assertIn(dirty, self.kept(result, "uncommitted_changes"))
        self.assertIn(untracked, self.kept(result, "uncommitted_changes"))
        with open(os.path.join(dirty, "a.txt")) as fh:
            self.assertEqual(fh.read(), "a\ndirty\n")
        self.assertTrue(os.path.isfile(os.path.join(untracked, "new.txt")))

    def test_merged_tree_with_private_data_is_archived_verified_and_kept(self):
        path = self.worktree("evidence", idle_hours=100)
        os.makedirs(os.path.join(path, ".local", "evidence"))
        with open(os.path.join(path, ".local", "evidence", "proof.json"), "w") as fh:
            fh.write("{}")
        self._old_terminal(path)
        result = self.janitor(archive_dir=None).run_once()
        self.assertIn(path, self.kept(result, "archive_no_archive_dir"))
        janitor = self.janitor()
        janitor.dev_fn = lambda p: 1
        result = janitor.run_once()
        self.assertIn(path, self.kept(result, "archive_archive_same_filesystem"))
        self.assertTrue(os.path.isfile(os.path.join(path, ".local", "evidence", "proof.json")))
        result = self.janitor().run_once()
        self.assertEqual(len(result["archived"]), 1, result)
        dest = result["archived"][0]["archive"]
        self.assertTrue(dest.startswith(self.archive))
        self.assertFalse(os.path.exists(path))
        with tarfile.open(os.path.join(dest, "files.tar.gz")) as tar:
            self.assertIn(".local/evidence/proof.json", set(tar.getnames()))
        self.assertTrue(os.path.isfile(os.path.join(dest, "meta.json")), "the archive is kept")

    def test_private_data_changed_after_archive_is_kept_in_source(self):
        path = self.worktree("evidence-race", idle_hours=100)
        evidence = os.path.join(path, ".local", "proof.txt")
        os.makedirs(os.path.dirname(evidence))
        with open(evidence, "w") as fh:
            fh.write("before")
        self._old_terminal(path)
        self.free = 1 * GIB
        janitor = self.janitor()
        archive = janitor.archive_worktree

        def archive_then_mutate(cfg, source, decision):
            result = archive(cfg, source, decision)
            with open(evidence, "w") as fh:
                fh.write("new data must not be lost")
            return result

        janitor.archive_worktree = archive_then_mutate
        result = janitor.run_once()
        self.assertIn(path, self.kept(result, "private_data_changed"))
        self.assertTrue(os.path.isdir(path))
        with open(evidence) as fh:
            self.assertEqual(fh.read(), "new data must not be lost")

    def test_every_removal_has_a_receipt(self):
        path = self.worktree("receipt")
        self.terminal_session(path)
        result = self.janitor().run_once()
        self.assertIn(path, self.removed_paths(result))
        with open(os.path.join(self.root, "state", disk_janitor.RECEIPTS_FILENAME)) as fh:
            receipts = [json.loads(line) for line in fh]
        self.assertEqual([(r["path"], r["outcome"]) for r in receipts], [(path, "removing"), (path, "removed")])
        self.assertEqual(receipts[0]["reason"], "terminal_clean_merged")
        self.assertEqual(receipts[0]["head"], receipts[0]["base"])
        self.assertIn("at", receipts[0])

    def test_regenerable_ignored_output_does_not_need_archive(self):
        path = self.worktree("built")
        os.makedirs(os.path.join(path, "node_modules", "pkg"))
        os.makedirs(os.path.join(path, ".local", "tmp", "run1"))
        age(path, 5)
        self.terminal_session(path)
        result = self.janitor().run_once()
        self.assertIn(path, self.removed_paths(result))
        self.assertEqual(result["archived"], [])

    def test_backup_or_export_inside_unsaved_tree_is_never_deleted(self):
        path = self.worktree("with-db", idle_hours=100)
        os.makedirs(os.path.join(path, ".local", "db-backups"))
        with open(os.path.join(path, ".local", "db-backups", "hub.sqlite"), "w") as fh:
            fh.write("x")
        age(path, 100)
        self.terminal_session(path, last_activity_ms=(time.time() - 100 * 3600) * 1000)
        result = self.janitor().run_once()
        self.assertIn(path, self.kept(result, "holds_protected_data"))
        self.assertTrue(os.path.isfile(os.path.join(path, ".local", "db-backups", "hub.sqlite")))
        self.assertEqual(result["archived"], [])

    def test_protected_worktree_names_are_never_touched(self):
        for name in ("prod", "db-backups-wt", "rds-export"):
            path = self.worktree(name)
            self.terminal_session(path)
        result = self.janitor().run_once()
        for name in ("prod", "db-backups-wt", "rds-export"):
            self.assertTrue(os.path.isdir(os.path.join(self.ws, name)))
        self.assertEqual(result["removed"], [])

    def test_orphan_worktree_needs_pressure_and_long_idle(self):
        path = self.worktree("orphan", idle_hours=5)
        result = self.janitor().run_once()
        self.assertIn(path, self.kept(result, "grace_period"))
        age(path, 30)
        self.free = 300 * GIB
        result = self.janitor().run_once()
        self.assertIn(path, self.kept(result, "orphan_no_pressure"))
        self.free = 50 * GIB
        result = self.janitor().run_once()
        self.assertIn(path, self.removed_paths(result))

    def test_dry_run_changes_nothing(self):
        path = self.worktree("dry")
        self.terminal_session(path)
        result = self.janitor(dry_run=True).run_once()
        self.assertIn(path, self.removed_paths(result))
        self.assertTrue(os.path.isdir(path))

    def test_protected_session_tree_is_held_and_unsaved_work_kept(self):
        path = self.worktree("human")
        self.terminal_session(path)
        self.facts = [{"workspace": path, "terminal": True, "native_terminal": True, "linked_intake_open": False, "protected": True}]
        result = self.janitor().run_once()
        self.assertIn(path, self.kept(result, "held_grace_period"))
        wip = self.worktree("human-wip", idle_hours=100, commit=True)
        self.facts.append({"workspace": wip, "terminal": True, "native_terminal": True, "linked_intake_open": False, "protected": True})
        age(path, 30)
        self.sessions = []
        result = self.janitor().run_once()
        self.assertIn(path, self.removed_paths(result))
        self.assertIn(wip, self.kept(result, "unpushed_archive_kept"))
        self.assertTrue(os.path.isdir(wip))


class TestProtectedPaths(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config({"protected_paths": ["/srv/keep"]})

    def test_builtin_and_configured_paths(self):
        home = os.path.expanduser("~")
        for path in (os.path.join(home, "prod"), os.path.join(home, "prod", "db"), os.path.join(home, "backups", "x"),
                     os.path.join(home, "storagebox", "a"), "/srv/keep/file", home, "/"):
            self.assertIsNotNone(protected_reason(path, self.cfg), path)

    def test_ancestors_and_names(self):
        self.assertEqual(protected_reason("/srv", self.cfg), "protected_ancestor")
        self.assertEqual(protected_reason("/tmp/rds-export", self.cfg), "protected_name")
        self.assertEqual(protected_reason("/tmp/x/native-backup-interrupted.db", self.cfg), "protected_name")
        self.assertIsNone(protected_reason("/tmp/hubtest-a", self.cfg))
        self.assertIsNone(protected_reason("/tmp/cache-entry", self.cfg))

    def test_config_cannot_drop_builtins_and_rejects_bad_values(self):
        cfg = load_config({"protected_paths": []})
        self.assertIsNotNone(protected_reason(os.path.join(os.path.expanduser("~"), "prod"), cfg))
        for bad in ({"floor_gb": 300, "target_gb": 200}, {"workspace_roots": ["relative"]}, {"bogus": 1},
                    {"cache_rules": [{"path": "/x", "min_age_hours": 0}]},
                    {"cache_rules": [{"path": "/x", "min_age_hours": float("nan")}]},
                    {"floor_gb": float("inf")}, {"interval_s": 5}):
            with self.assertRaises(disk_janitor.ConfigError):
                load_config(bad)
        self.assertIsNone(load_config({"enabled": False}))


class TestAgeRules(Fixture):
    def test_cache_and_scratch_entries_reaped_by_age_only_when_unused(self):
        cache = os.path.join(self.root, "engine-home", ".cache", "uv")
        for name in ("old", "new", "busy"):
            os.makedirs(os.path.join(cache, name))
            with open(os.path.join(cache, name, "blob"), "w") as fh:
                fh.write("x" * 100)
        # Code inside a regenerable cache may be named like a protected word.
        with open(os.path.join(cache, "old", "export.py"), "w") as fh:
            fh.write("x")
        backup_dir = os.path.join(cache, "db-backup")
        os.makedirs(backup_dir)
        backup = os.path.join(backup_dir, "native-backup-interrupted.db")
        with open(backup, "w") as fh:
            fh.write("keep")
        age(os.path.join(cache, "old"), 100)
        age(backup_dir, 100)
        age(os.path.join(cache, "busy"), 100)
        scratch_old = os.path.join(self.root, "hubtest-a")
        scratch_new = os.path.join(self.root, "hubtest-b")
        os.makedirs(scratch_old)
        os.makedirs(scratch_new)
        age(scratch_old, 10)
        self.open_paths = [os.path.join(cache, "busy", "blob")]
        result = self.janitor(workspace_roots=[], cache_rules=[{"path": cache, "min_age_hours": 48}],
                              scratch_rules=[{"glob": os.path.join(self.root, "hubtest-*"), "min_age_hours": 6}]).run_once()
        removed = self.removed_paths(result)
        self.assertEqual(removed, {os.path.join(cache, "old"), scratch_old})
        self.assertTrue(os.path.isdir(os.path.join(cache, "new")))
        self.assertTrue(os.path.isdir(os.path.join(cache, "busy")))
        self.assertTrue(os.path.isfile(backup))
        self.assertTrue(os.path.isdir(scratch_new))

    def test_broken_git_marker_is_never_removed_as_cache(self):
        cache = os.path.join(self.root, "engine-home", ".cache")
        broken = os.path.join(cache, "broken-checkout")
        os.makedirs(broken)
        with open(os.path.join(broken, ".git"), "w") as fh:
            fh.write("gitdir: /missing/worktrees/removed\n")
        age(broken, 100)
        result = self.janitor(workspace_roots=[], cache_rules=[{"path": cache, "min_age_hours": 24}]).run_once()
        self.assertTrue(os.path.isdir(broken))
        self.assertIn(broken, self.kept(result, "git_checkout"))

    def test_refused_cache_removal_has_a_closing_receipt(self):
        cache = os.path.join(self.root, "engine-home", ".cache")
        old = os.path.join(cache, "old")
        os.makedirs(old)
        with open(os.path.join(old, "payload"), "w") as fh:
            fh.write("keep")
        age(old, 100)
        janitor = self.janitor(workspace_roots=[], cache_rules=[{"path": cache, "min_age_hours": 24}])
        real = disk_janitor.shutil.rmtree

        def refuse(path, *args, **kwargs):
            if path == old:
                raise OSError("refused")
            return real(path, *args, **kwargs)

        disk_janitor.shutil.rmtree = refuse
        try:
            result = janitor.run_once()
        finally:
            disk_janitor.shutil.rmtree = real
        self.assertIn(old, self.kept(result, "remove_failed"))
        with open(os.path.join(self.root, "state", disk_janitor.RECEIPTS_FILENAME)) as fh:
            receipts = [json.loads(line) for line in fh]
        self.assertEqual([r["outcome"] for r in receipts], ["removing", "kept_remove_refused"])

    def test_scratch_glob_never_deletes_protected_or_git_checkouts(self):
        dump = os.path.join(self.root, "oc-backup-dir")
        os.makedirs(dump)
        age(dump, 100)
        test_log = os.path.join(self.root, "hubtest-old.log")
        with open(test_log, "w") as fh:
            fh.write("retain logs; scratch rules only remove test-home directories")
        age(test_log, 100)
        # Like /tmp/oc5254: an innocently named scratch dir holding a DB backup.
        holder = os.path.join(self.root, "oc5254")
        os.makedirs(holder)
        with open(os.path.join(holder, "native-backup-interrupted.db"), "w") as fh:
            fh.write("x")
        age(holder, 100)
        age(self.main, 100)
        result = self.janitor(workspace_roots=[], scratch_rules=[
            {"glob": os.path.join(self.root, "oc-*"), "min_age_hours": 1},
            {"glob": os.path.join(self.ws, "*"), "min_age_hours": 1}]).run_once()
        self.assertTrue(os.path.isdir(dump))
        self.assertTrue(os.path.isfile(test_log))
        self.assertTrue(os.path.isfile(os.path.join(holder, "native-backup-interrupted.db")))
        self.assertTrue(os.path.isdir(self.main))
        self.assertEqual(result["removed"], [])


class TestFloorReport(Fixture):
    def test_state_and_report_round_trip(self):
        janitor = self.janitor(workspace_roots=[])
        self.assertIsNone(janitor.floor_report("/elsewhere"))
        pending = janitor.floor_report(self.root)
        self.assertIsNone(pending["cleanup_at"])
        janitor.run_once()
        report = janitor.floor_report(self.root)
        self.assertEqual(report["floor_bytes"], 100 * GIB)
        self.assertEqual(report["free_after_cleanup_bytes"], 50 * GIB)
        self.assertIsNotNone(report["cleanup_at"])
        with open(os.path.join(self.root, "state", disk_janitor.STATE_FILENAME)) as fh:
            state = json.load(fh)
        self.assertTrue(state["below_floor_after_cleanup"])

    def test_due_uses_pressure_interval_and_single_inflight(self):
        clock = [1000.0]
        janitor = self.janitor(workspace_roots=[], interval_s=900, pressure_interval_s=120)
        janitor.monotonic_fn = lambda: clock[0]
        self.assertTrue(janitor.maybe_start())
        janitor._thread.join(10)
        clock[0] += 130
        self.assertTrue(janitor.maybe_start(), "below the floor the short interval applies")
        janitor._thread.join(10)
        self.free = 150 * GIB
        clock[0] += 130
        self.assertFalse(janitor.maybe_start())

    def test_disk_monitor_attaches_floor_to_primary_mount(self):
        janitor = self.janitor(workspace_roots=[])
        janitor.run_once()
        monitor = disk_monitor.DiskMonitor(config={"mounts": [self.root]}, machine_id="m1", janitor=janitor)
        payload, _ = monitor.collect(monitor.load_active_config())
        floor = payload["filesystems"][0]["floor"]
        self.assertEqual(floor["floor_bytes"], 100 * GIB)
        self.assertIsNotNone(floor["cleanup_at"])


if __name__ == "__main__":
    unittest.main()
