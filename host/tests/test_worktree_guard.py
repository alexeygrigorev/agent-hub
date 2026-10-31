"""Worktree-guard regression tests (intake a0f01448, RCA 95c1862c/24638f4e).

AC1: Hub lock reason format + git lock refusal while live.
AC2: shared skip predicate (locked / live / open intake).
AC4: first-detection alert is immediate (tested in sweeper behavior below).
AC5: stale " (deleted)" label vs recreated worktree.
"""

import os
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from agent_hub_host import worktree_guard as guard
from agent_hub_host import orphan_sweeper as sweeper


def _run(argv, cwd):
    return subprocess.run(argv, capture_output=True, text=True, timeout=60, check=False, cwd=cwd)


def _make_repo(tmp):
    origin = os.path.join(tmp, "origin")
    os.mkdir(origin)
    assert _run(["git", "init", "-q", origin], tmp).returncode == 0
    assert _run(["git", "-C", origin, "config", "user.email", "t@t"], tmp).returncode == 0
    assert _run(["git", "-C", origin, "config", "user.name", "t"], tmp).returncode == 0
    with open(os.path.join(origin, "f.txt"), "w") as fh:
        fh.write("x")
    assert _run(["git", "-C", origin, "add", "f.txt"], tmp).returncode == 0
    assert _run(["git", "-C", origin, "commit", "-qm", "init"], tmp).returncode == 0
    return origin


def _add_worktree(origin, tmp, name="wt", branch="feature-x"):
    path = os.path.join(tmp, name)
    proc = _run(["git", "-C", origin, "worktree", "add", path, "-b", branch], tmp)
    assert proc.returncode == 0, proc.stderr
    return path


class TestLockReason(unittest.TestCase):
    def test_format_and_roundtrip(self):
        sid = "a" * 32
        reason = guard.lock_reason(sid, "intake-1")
        self.assertEqual(reason, "hub-session %s intake intake-1" % sid)
        self.assertEqual(guard.parse_lock_reason(reason), (sid, "intake-1"))
        self.assertEqual(guard.lock_reason(sid, None), "hub-session %s intake none" % sid)
        self.assertEqual(guard.lock_reason(sid, "none"), "hub-session %s intake none" % sid)
        self.assertEqual(guard.parse_lock_reason("hub-session %s intake none" % sid), (sid, None))

    def test_malformed_ids_raise_or_none(self):
        with self.assertRaises(ValueError):
            guard.lock_reason("not-a-uuid!!", "x")
        with self.assertRaises(ValueError):
            guard.lock_reason("a" * 32, "../evil")
        self.assertIsNone(guard.parse_lock_reason("random reason"))
        self.assertIsNone(guard.parse_lock_reason(None))
        self.assertIsNone(guard.parse_lock_reason("hub-session aaaa intake none extra"))


class TestGates(unittest.TestCase):
    def test_can_unlock_matrix(self):
        self.assertTrue(guard.can_unlock(True, "none"))
        self.assertTrue(guard.can_unlock(True, "done"))
        self.assertTrue(guard.can_unlock(True, "archived"))
        self.assertFalse(guard.can_unlock(True, "open"))
        self.assertFalse(guard.can_unlock(True, "bogus"))
        self.assertFalse(guard.can_unlock(True, None))
        self.assertFalse(guard.can_unlock(False, "done"))
        self.assertFalse(guard.can_unlock(None, "none"))

    def test_should_skip_cleanup_matrix(self):
        self.assertFalse(guard.should_skip_cleanup(False, False, False))
        self.assertTrue(guard.should_skip_cleanup(True, False, False))
        self.assertTrue(guard.should_skip_cleanup(False, True, False))
        self.assertTrue(guard.should_skip_cleanup(False, False, True))
        self.assertTrue(guard.should_skip_cleanup(True, True, True))


class TestGitLock(unittest.TestCase):
    def test_lock_refuses_plain_remove_and_unlock_releases(self):
        with tempfile.TemporaryDirectory() as tmp:
            origin = _make_repo(tmp)
            path = _add_worktree(origin, tmp)
            sid = "b" * 32
            reason = guard.lock_reason(sid, "intake-9")
            self.assertTrue(guard.lock_worktree("git", path, reason))
            self.assertTrue(guard.is_locked("git", path))
            # AC1 live proof analog: plain `git worktree remove` refuses.
            refused = _run(["git", "-C", origin, "worktree", "remove", path], tmp)
            self.assertNotEqual(refused.returncode, 0)
            self.assertTrue(os.path.isdir(path))
            # ensure_locked keeps an existing lock (reason preserved).
            self.assertTrue(guard.ensure_locked("git", path, sid, "intake-9"))
            self.assertTrue(guard.unlock_worktree("git", path))
            self.assertFalse(guard.is_locked("git", path))

    def test_prune_preserves_locked_registration_when_path_disappears(self):
        with tempfile.TemporaryDirectory() as tmp:
            origin = _make_repo(tmp)
            path = _add_worktree(origin, tmp)
            self.assertTrue(guard.ensure_locked("git", path, "b" * 32, "intake-9"))
            moved = path + "-moved"
            os.rename(path, moved)
            self.assertEqual(_run(["git", "worktree", "prune", "--expire", "now"], origin).returncode, 0)
            entry = guard.find_entry(guard.list_entries("git", origin), path)
            self.assertIsNotNone(entry)
            self.assertTrue(entry["locked"])
            os.rename(moved, path)
            self.assertTrue(guard.release_session_lock("git", path, "b" * 32,
                                                      terminal=True, intake_state="done"))

    def test_release_preserves_manual_other_session_and_open_intake_locks(self):
        with tempfile.TemporaryDirectory() as tmp:
            origin = _make_repo(tmp)
            path = _add_worktree(origin, tmp)
            sid = "c" * 32
            for reason in ("manual preservation", guard.lock_reason("d" * 32, None)):
                self.assertTrue(guard.lock_worktree("git", path, reason))
                self.assertFalse(guard.release_session_lock("git", path, sid, terminal=True, intake_state="none"))
                self.assertTrue(guard.is_locked("git", path))
                self.assertTrue(guard.unlock_worktree("git", path))
            self.assertTrue(guard.ensure_locked("git", path, sid, "intake-9"))
            self.assertFalse(guard.release_session_lock("git", path, sid, terminal=True, intake_state="open"))
            self.assertFalse(guard.release_session_lock("git", path, sid, terminal=False, intake_state="done"))
            self.assertTrue(guard.release_session_lock("git", path, sid, terminal=True, intake_state="done"))
            self.assertFalse(guard.is_locked("git", path))

    def test_ensure_locked_malformed_skips(self):
        with tempfile.TemporaryDirectory() as tmp:
            origin = _make_repo(tmp)
            path = _add_worktree(origin, tmp)
            self.assertFalse(guard.ensure_locked("git", path, "bad-id", "x"))
            self.assertFalse(guard.is_locked("git", path))

    def test_bound_linked_tree_requires_exact_lock_before_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            origin = _make_repo(tmp)
            path = _add_worktree(origin, tmp)
            sub = os.path.join(path, "sub")
            os.mkdir(sub)
            sid = "e" * 32
            self.assertTrue(guard.protect_bound_worktree("git", origin, None, None))
            self.assertFalse(guard.protect_bound_worktree("git", sub, None, None))
            self.assertTrue(guard.protect_bound_worktree("git", sub, sid, "intake-1"))
            self.assertFalse(guard.protect_bound_worktree("git", sub, "f" * 32, None))
            self.assertTrue(guard.is_locked("git", path))
            self.assertFalse(guard.release_session_lock("git", sub, sid, terminal=True, intake_state="open"))
            self.assertTrue(guard.release_session_lock("git", sub, sid, terminal=True, intake_state="done"))
            self.assertFalse(guard.is_locked("git", path))

    def test_unregistered_path_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(guard.is_locked("git", os.path.join(tmp, "nope")))
            self.assertIsNone(guard.is_registered_worktree("git", os.path.join(tmp, "nope")))


class TestRecreated(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        override = patch.dict(os.environ, {"HUB_WORKTREE_IDENTITY_DIR": directory.name + "/identities"})
        override.start()
        self.addCleanup(override.stop)

    def test_deleted_label_with_recreated_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            origin = _make_repo(tmp)
            path = _add_worktree(origin, tmp)
            labeled = path + " (deleted)"
            # Kernel-style label while the tree is alive at the stripped path.
            sid = "a" * 32
            self.assertTrue(guard.remember_repository(sid, path))
            expected = guard.expected_repository({"tag": "hub-" + sid})
            self.assertTrue(guard.detect_recreated(labeled, expected=expected))
            self.assertEqual(guard.classify_stale_path(labeled, expected=expected), "cwd_stale_path_recreated")

    def test_recreated_tree_must_match_original_repository(self):
        with tempfile.TemporaryDirectory() as tmp:
            origin = _make_repo(tmp)
            path = _add_worktree(origin, tmp)
            sid = "a" * 32
            self.assertTrue(guard.remember_repository(sid, path))
            expected = guard.expected_repository({"tag": "task-hub-" + sid})
            self.assertTrue(guard.detect_recreated(path + " (deleted)", expected=expected))
            self.assertEqual(_run(["git", "worktree", "remove", path], origin).returncode, 0)
            other_dir = os.path.join(tmp, "other")
            os.mkdir(other_dir)
            other = _make_repo(other_dir)
            self.assertEqual(_run(["git", "worktree", "add", path, "-b", "other"], other).returncode, 0)
            self.assertFalse(guard.detect_recreated(path + " (deleted)", expected=expected))
            self.assertFalse(guard.remember_repository(sid, path))
            self.assertTrue(sweeper.native_missing({"id": "b" * 32, "tag": "hub-" + sid,
                                                  "workspace": path, "cwd": path + " (deleted)"}))

    def test_forged_git_file_is_not_recreation_proof(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, ".git"), "w") as fh:
                fh.write("gitdir: /nonexistent/fake")
            self.assertFalse(guard.detect_recreated(tmp + " (deleted)"))

    def test_truly_missing_is_missing(self):
        self.assertFalse(guard.detect_recreated("/nonexistent-wt-xyz (deleted)"))
        self.assertEqual(guard.classify_stale_path("/nonexistent-wt-xyz (deleted)"), "missing")
        self.assertEqual(guard.classify_stale_path(None), "missing")
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(guard.classify_stale_path(tmp), "present")

    def test_sweeper_does_not_treat_recreated_as_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            origin = _make_repo(tmp)
            path = _add_worktree(origin, tmp)
            row = {"id": "c" * 32, "tag": "task-hub-" + "a" * 32, "workspace": path,
                   "cwd": path + " (deleted)"}
            self.assertTrue(guard.remember_repository("a" * 32, path))
            self.assertFalse(sweeper.native_missing(row))

    def test_sweeper_skip_sets_report_only(self):
        row = {"id": "d" * 32, "tag": "task-hub-1", "workspace": "/nonexistent-xyz",
               "cwd": "/nonexistent-xyz"}
        self.assertTrue(sweeper.native_missing(row))
        self.assertEqual(
            sweeper.skip_reason(row, None, live_ids={"d" * 32}), "live session")
        self.assertEqual(
            sweeper.skip_reason(row, None, open_intake_sessions={"d" * 32}), "linked open intake")
        self.assertEqual(
            sweeper.skip_reason(row, None, locked_paths={"/nonexistent-xyz"}), "worktree locked")
        self.assertIsNone(sweeper.skip_reason(row, None))
        # Live phase in the row itself also skips.
        self.assertEqual(
            sweeper.skip_reason({**row, "phase": "running"}, None), "live session")

    def test_sweep_alerts_on_first_detection_and_skips_guarded(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = os.path.join(tmp, "gone")
            headless = {"id": "e" * 32, "tag": "task-hub-1", "workspace": missing,
                        "cwd": missing, "phase": "running"}
            alerts = []
            observed = []
            out = []
            res = sweeper.sweep_once(
                sessions=[headless],
                hub_lookup=lambda sid: {"human_owned": False},
                now=2000000.0, grace_hours=2.0,
                state_path=os.path.join(tmp, "state.json"),
                stop_fn=lambda row: True,
                observe_fn=lambda row, info: observed.append(row["id"]) or True,
                out=out.append,
            )
            # Live session: guard-skipped to report-only + alerted immediately.
            self.assertEqual(res["report"], 1)
            self.assertEqual(res["alerted"], 1)
            self.assertEqual(res["stopped"], 0)
            self.assertIn(headless["id"], observed)
            # Second run: already alerted, still report-only, no repeat alert.
            out2 = []
            res2 = sweeper.sweep_once(
                sessions=[headless],
                hub_lookup=lambda sid: {"human_owned": False},
                now=2000000.0 + 7200.0 + 5, grace_hours=2.0,
                state_path=os.path.join(tmp, "state.json"),
                stop_fn=lambda row: alerts.append(row["id"]) or True,
                observe_fn=lambda row, info: observed.append(row["id"]) or True,
                out=out2.append,
            )
            self.assertEqual(res2.get("alerted", 0), 0)
            self.assertEqual(alerts, [])

    def test_proc_only_stale_inode_uses_original_repo_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            origin = _make_repo(tmp)
            path = _add_worktree(origin, tmp)
            self.assertTrue(guard.remember_repository("a" * 32, path))
            row = {"id": "f" * 32, "tag": "task-hub-" + "a" * 32,
                   "workspace": path, "cwd": path, "workload_pid": 123}
            with patch.object(sweeper, 'proc_cwd_target', side_effect=lambda pid: path + " (deleted)" if pid == 123 else None):
                self.assertFalse(sweeper.native_missing(row))
                result = sweeper.sweep_once(sessions=[row], out=lambda _: None)
            self.assertEqual(result['recreated'], 1)

    def test_recreated_recovery_resets_episode_only_after_ack(self):
        with tempfile.TemporaryDirectory() as tmp:
            row = {"id": "c" * 32, "tag": "notes", "phase": "running", "workspace": tmp + '/gone'}
            state = tmp + '/state.json'
            alerts = []
            common = dict(sessions=[row], state_path=state, out=lambda _: None,
                          alert_fn=lambda *args: alerts.append(True) or True)
            sweeper.sweep_once(**common, now=1)
            row['cwd'] = tmp + '/gone (deleted)'
            for recovery in (False, RuntimeError('lost')):
                def recover(*args):
                    if isinstance(recovery, Exception): raise recovery
                    return recovery
                sweeper.sweep_once(**common, now=2, recreated_checker=lambda _: True, observe_recreated_fn=recover)
                self.assertIn(row['id'], sweeper.load_alerted(state))
            sweeper.sweep_once(**common, now=3, recreated_checker=lambda _: True, observe_recreated_fn=lambda *args: True)
            self.assertNotIn(row['id'], sweeper.load_alerted(state))
            row.pop('cwd')
            sweeper.sweep_once(**common, now=4)
            self.assertEqual(len(alerts), 2)

    def test_sweep_counts_recreated(self):
        with tempfile.TemporaryDirectory() as tmp:
            origin = _make_repo(tmp)
            path = _add_worktree(origin, tmp)
            row = {"id": "f" * 32, "tag": "task-hub-" + "a" * 32, "workspace": path,
                   "cwd": path + " (deleted)"}
            self.assertTrue(guard.remember_repository("a" * 32, path))
            res = sweeper.sweep_once(
                sessions=[row], hub_lookup=lambda sid: None, now=1.0,
                state_path=os.path.join(tmp, "state.json"),
                out=lambda m: None,
            )
            self.assertEqual(res["recreated"], 1)
            self.assertEqual(res.get("waiting", 0), 0)

    def test_host_observer_uses_current_native_catalog_and_host_http(self):
        from types import SimpleNamespace
        from agent_hub_host.session_host import worktree_step_once
        with tempfile.TemporaryDirectory() as tmp:
            native_id = "d" * 32
            row = {"id": native_id, "tag": "human", "phase": "running", "workspace": tmp + "/gone"}
            calls = []
            dispatcher = SimpleNamespace(settings={"aplexer_bin": "native-cli"},
                runner=lambda argv, **kwargs: [row],
                request=lambda method, path, body: calls.append((method, path, body)))
            worktree_step_once(dispatcher, "own-host", tmp)
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][0:2], ("POST", "/api/sessions/observations"))
            self.assertEqual(calls[0][2]["machine_id"], "own-host")
            self.assertEqual(calls[0][2]["native_id"], native_id)
            self.assertTrue(calls[0][2]["payload"]["worktree_missing"])

    def test_installed_observer_reports_once_retries_failure_and_reports_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "gone")
            row = {"id": "c" * 32, "tag": "manual-interactive", "phase": "running", "workspace": path}
            calls = []
            def request(method, endpoint, body):
                calls.append(body["payload"])
                if len(calls) == 1:
                    raise ConnectionError("synthetic transport loss")
                return {"worktree_followup_complete": True}
            sweeper.observe_catalog([row], request, "host-1", tmp)
            sweeper.observe_catalog([row], request, "host-1", tmp)
            sweeper.observe_catalog([row], request, "host-1", tmp)
            self.assertEqual(len(calls), 3)  # Replayed episode; server deduplicates effects.
            self.assertTrue(calls[-1]["worktree_missing"])
            os.mkdir(path)
            sweeper.observe_catalog([row], request, "host-1", tmp)
            self.assertFalse(calls[-1]["worktree_missing"])
            os.rmdir(path)
            sweeper.observe_catalog([row], request, "host-1", tmp)
            self.assertTrue(calls[-1]["worktree_missing"])
            self.assertEqual(len(calls), 5)


class TestBriefFooter(unittest.TestCase):
    def test_footer_names_incident_and_done_rule(self):
        from agent_hub_host import session_rules as rules
        prompt = rules.compose_prompt("Do the thing.")
        self.assertIn("95c1862c", prompt)
        self.assertIn("DONE note", prompt)
        self.assertIn("while blocked or waiting, keep it", prompt)
        self.assertTrue(prompt.startswith(rules.SESSION_PREAMBLE))
        self.assertIn("Do the thing.", prompt)


class TestNoPruneCallers(unittest.TestCase):
    """AC2: no automated path may prune worktrees or force-remove them.

    Fails closed if any host module (or timer script) ever gains a
    `git worktree prune` / `git worktree remove --force|-f` argv call.
    Doc prose mentioning prune is fine; argv code is not.
    """

    def test_no_prune_or_force_remove_argv(self):
        import re
        from pathlib import Path
        roots = [
            Path(__file__).resolve().parents[1] / "agent_hub_host",
            Path(__file__).resolve().parents[2] / "scripts",
        ]
        prune_re = re.compile(r'''"worktree"\s*,\s*"prune"|'worktree'\s*,\s*'prune'|worktree\s+prune''')
        force_re = re.compile(r'''"remove"\s*,\s*"--force"|'remove'\s*,\s*'--force'|"remove"\s*,\s*"-f"|'remove'\s*,\s*'-f'|worktree\s+remove\s+--force''')
        # intake_worktree.py's operator-invoked tool (hub worktree
        # status/remove, intake 4b161918) prunes only registry metadata next
        # to its own non-force removal: `git worktree remove` (never --force)
        # refuses locked trees, live processes are refused before it runs, and
        # `git worktree prune` never drops locked entries. Hub session trees
        # are locked on start/adopt (AC1), so neither call can delete a
        # protected tree. Every other host path stays prune-free.
        exempt = {"intake_worktree.py"}
        offenders = []
        for root in roots:
            if not root.is_dir():
                continue
            for path in sorted(root.glob("*.py" if root.name != "scripts" else "*.py")):
                try:
                    text = path.read_text(encoding="utf-8")
                except OSError:
                    continue
                in_doc = False
                delim = ""
                for i, line in enumerate(text.splitlines(), 1):
                    stripped = line.strip()
                    if in_doc:
                        if delim and delim in stripped:
                            in_doc = False
                        continue
                    if stripped.startswith("#"):
                        continue
                    if stripped.startswith('"""') or stripped.startswith("'''"):
                        delim = stripped[:3]
                        if stripped.count(delim) < 2:
                            in_doc = True
                        continue
                    if prune_re.search(stripped) or force_re.search(stripped):
                        if path.name in exempt:
                            continue
                        offenders.append("%s:%d: %s" % (path.name, i, stripped[:160]))
        self.assertEqual(offenders, [])


class TestFleetLockWiring(unittest.TestCase):
    def test_lock_wiring_uses_offer_ids(self):
        from agent_hub_host import fleet as fleet_mod
        with tempfile.TemporaryDirectory() as tmp:
            origin = _make_repo(tmp)
            path = _add_worktree(origin, tmp)
            cfg = {"version": 1, "workspaces": {"demo": tmp}, "subscriptions": []}
            dispatcher = fleet_mod.Dispatcher(cfg, lambda *a: {}, lambda *a: {})
            sid = "a" * 32
            with self.assertRaisesRegex(ValueError, "worktree_lock_failed"):
                dispatcher._lock_session_worktree({"id": "x"}, path)
            self.assertFalse(guard.is_locked("git", path))
            with patch.dict(os.environ, {"HUB_WORKTREE_IDENTITY_DIR": tmp + "/identities"}):
                self.assertTrue(dispatcher._lock_session_worktree(
                    {"id": "b" * 32, "hub_session_id": sid, "intake_task_id": "t1"}, path))
            self.assertTrue(guard.is_locked("git", path))


if __name__ == "__main__":
    unittest.main()


class CleanupAuthorityTests(unittest.TestCase):
    def test_exact_fresh_terminal_closed_unlocked_facts_required(self):
        row = dict(id='native', created_at_ms=123, tag='tag', workspace='/tree', phase='exited')
        fact = dict(native_id='native', native_created_at_ms=123, tag='tag', workspace='/tree',
                    terminal=True, native_terminal=True, linked_intake_open=False, protected=False)
        request = lambda *args: {'sessions': [fact]}
        with patch.object(guard, 'is_locked', return_value=False):
            self.assertTrue(guard.cleanup_permitted(row, request))
            for field, value in [('terminal', False), ('native_terminal', False), ('linked_intake_open', None),
                                 ('protected', True), ('native_created_at_ms', 124), ('workspace', '/other')]:
                changed = dict(fact, **{field: value})
                self.assertFalse(guard.cleanup_permitted(row, lambda *args: {'sessions': [changed]}))
            self.assertFalse(guard.cleanup_permitted(dict(row, phase='running'), request))
            self.assertFalse(guard.cleanup_permitted(row, lambda *args: {'sessions': [fact, fact]}))
        for lock in (True, None):
            with patch.object(guard, 'is_locked', return_value=lock):
                self.assertFalse(guard.cleanup_permitted(row, request))
        self.assertFalse(guard.cleanup_permitted(row, lambda *args: (_ for _ in ()).throw(ValueError())))


class LifecycleReconciliationTests(unittest.TestCase):
    def test_late_done_and_failed_unlock_retry_preserve_unknown_and_foreign(self):
        fact = dict(session_id='11111111', terminal=True, native_terminal=True, linked_intake_open=True,
                    protected=False, tag='hub-22222222')
        expected = dict(workspace='/tree', repository={'common_dir': '/repo/.git'})
        with patch.object(guard, 'expected_repository', return_value=expected), \
             patch.object(guard, 'repository_identity', return_value=expected['repository']), \
             patch.object(guard, 'release_session_lock', side_effect=[False, True]) as release:
            request = lambda *args: {'sessions': [fact]}
            self.assertEqual(guard.reconcile_terminal_locks(request), 0)
            release.assert_not_called()
            fact['linked_intake_open'] = False
            self.assertEqual(guard.reconcile_terminal_locks(request), 0)
            self.assertEqual(guard.reconcile_terminal_locks(request), 1)
            fact['protected'] = True
            self.assertEqual(guard.reconcile_terminal_locks(request), 0)
            self.assertEqual(release.call_count, 2)

    def test_bounded_worker_pages_terminal_sessions_with_a_cursor(self):
        guard._reconcile_cursor = None
        fact = dict(session_id='11111111', terminal=True, native_terminal=True, linked_intake_open=False,
                    protected=False, tag='hub-22222222')
        expected = dict(workspace='/tree', repository={'common_dir': '/repo/.git'})
        paths = []
        pages = {None: ('c1', [fact]), 'c1': (None, [fact])}
        def request(method, path):
            paths.append(path)
            query = dict(p.split('=', 1) for p in path.split('?', 1)[1].split('&'))
            nxt, sessions = pages[query.get('cursor')]
            return {'sessions': sessions, 'next_cursor': nxt}
        with patch.object(guard, 'expected_repository', return_value=expected), \
             patch.object(guard, 'repository_identity', return_value=expected['repository']), \
             patch.object(guard, 'release_session_lock', return_value=True):
            self.assertEqual(guard.reconcile_terminal_locks(request), 1)
            self.assertEqual(guard.reconcile_terminal_locks(request), 1)
            self.assertEqual(guard.reconcile_terminal_locks(request), 1)
        self.assertEqual([p.split('?', 1)[1] for p in paths], ['limit=100', 'limit=100&cursor=c1', 'limit=100'])
        self.assertNotIn('cursor', paths[0])
        self.assertIn('cursor=c1', paths[1])
        self.assertNotIn('cursor', paths[2])  # wrapped after the last page
        guard._reconcile_cursor = None

    def test_cursor_does_not_advance_when_the_deadline_cuts_a_page(self):
        guard._reconcile_cursor = None
        fact = dict(session_id='11111111', terminal=True, native_terminal=True, linked_intake_open=False, protected=False)
        request = lambda method, path: {'sessions': [fact, fact], 'next_cursor': 'c9'}
        with patch.object(guard.time, 'monotonic', side_effect=[0, 100, 100, 100]):
            guard.reconcile_terminal_locks(request)
        self.assertIsNone(guard._reconcile_cursor)
        guard.reconcile_terminal_locks(request)
        self.assertEqual(guard._reconcile_cursor, 'c9')
        guard._reconcile_cursor = None

    def test_adoption_does_not_invent_original_repository(self):
        row = dict(workspace='/tree', tag='hub-22222222')
        session = dict(id='11111111', intake_task_id='intake')
        with patch('agent_hub_host.worktree_guard.os.path.isdir', return_value=True), \
             patch.object(guard, 'protect_bound_worktree', return_value=True) as lock, \
             patch.object(guard, 'bound_worktree_root', return_value='/tree'), \
             patch.object(guard, 'expected_repository', return_value=None), \
             patch.object(guard, 'remember_repository') as remember:
            self.assertFalse(guard.protect_adoption(row, session))
            lock.assert_called_once_with('git', '/tree', '11111111', 'intake')
            remember.assert_not_called()

    def test_missing_durable_observation_ack_retries_even_for_unknown_phase(self):
        with tempfile.TemporaryDirectory() as tmp:
            row = dict(id='d' * 32, workspace=tmp + '/missing', tag='manual', phase='unknown')
            calls = []
            def request(*args):
                calls.append(args)
                return {} if len(calls) < 3 else {'worktree_followup_complete': True}
            for _ in range(4):
                sweeper.observe_catalog([row], request, 'host', tmp)
            self.assertEqual(len(calls), 4)


class RealTerminalLockReconciliationTests(unittest.TestCase):
    def test_natural_exit_and_late_done_release_only_recorded_owner(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'HUB_WORKTREE_IDENTITY_DIR': ''}):
            os.environ['HUB_WORKTREE_IDENTITY_DIR'] = tmp + '/identity'
            repo = _make_repo(tmp)
            tree = _add_worktree(repo, tmp)
            launch, sid = 'a' * 32, 'b' * 32
            self.assertTrue(guard.remember_repository(launch, tree))
            self.assertTrue(guard.protect_bound_worktree('git', tree, sid, 'proof-intake'))
            fact = dict(session_id=sid, tag='task-hub-' + launch, terminal=True,
                        native_terminal=True, linked_intake_open=True, protected=False)
            request = lambda *args: {'sessions': [fact]}
            self.assertEqual(guard.reconcile_terminal_locks(request), 0)
            self.assertTrue(guard.is_locked('git', tree))
            fact['linked_intake_open'] = False
            self.assertEqual(guard.reconcile_terminal_locks(request), 1)
            self.assertFalse(guard.is_locked('git', tree))
            self.assertTrue(guard.lock_worktree('git', tree, 'manual protection'))
            self.assertEqual(guard.reconcile_terminal_locks(request), 0)
            self.assertTrue(guard.is_locked('git', tree))


class MissingAdoptionTests(unittest.TestCase):
    def test_missing_path_or_replacement_repo_never_publishes_adoption(self):
        with tempfile.TemporaryDirectory() as tmp:
            row = dict(workspace=tmp + '/missing', tag='hub-' + 'a' * 32)
            with patch.object(guard, 'protect_bound_worktree') as protect:
                self.assertFalse(guard.protect_adoption(row, dict(id='b' * 32)))
                protect.assert_not_called()
            row['workspace'] = tmp
            with patch.object(guard, 'expected_repository', return_value={'repository': {'common_dir': '/original'}}), \
                 patch.object(guard, 'repository_identity', return_value={'common_dir': '/replacement'}), \
                 patch.object(guard, 'protect_bound_worktree') as protect:
                self.assertFalse(guard.protect_adoption(row, dict(id='b' * 32)))
                protect.assert_not_called()


class ExplicitOperatorAdoptionTests(unittest.TestCase):
    def test_exact_operator_intent_locks_and_releases_after_terminal_closed_intake(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'HUB_WORKTREE_IDENTITY_DIR': tmp + '/identity'}):
            repo = _make_repo(tmp)
            tree = _add_worktree(repo, tmp)
            sid = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
            row = dict(id=sid, tag='external-worker', workspace=tree, cwd=tree,
                       engine='codex', created_at_ms=1234, state='running')
            native = dict(native_id=sid, tag=row['tag'], workspace=tree,
                          engine='codex', created_at_ms=1234)
            intent = dict(id='bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb', session_id=sid,
                          intake_task_id='proof-intake', native=native)
            receipt = guard.protect_operator_adoption(row, intent)
            self.assertTrue(receipt['lock_verified'])
            self.assertTrue(receipt['worktree'])
            self.assertTrue(guard.is_locked('git', tree))
            self.assertNotEqual(_run(['git', '-C', repo, 'worktree', 'remove', tree], tmp).returncode, 0)
            self.assertEqual(guard.protect_operator_adoption(row, intent), receipt)
            fact = dict(native, session_id=sid, native_created_at_ms=1234, terminal=True,
                        native_terminal=True, linked_intake_open=True, protected=True,
                        lock_release_permitted=True)
            request = lambda *args: {'sessions': [fact]}
            self.assertEqual(guard.reconcile_terminal_locks(request), 0)
            fact['linked_intake_open'] = False
            self.assertEqual(guard.reconcile_terminal_locks(request), 1)
            self.assertFalse(guard.is_locked('git', tree))

    def test_mismatched_or_terminal_native_and_foreign_locks_are_untouched(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'HUB_WORKTREE_IDENTITY_DIR': tmp + '/identity'}):
            repo = _make_repo(tmp)
            tree = _add_worktree(repo, tmp)
            sid = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
            row = dict(id=sid, tag='external-worker', workspace=tree, engine='codex',
                       created_at_ms=1234, state='running')
            native = dict(native_id=sid, tag=row['tag'], workspace=tree, engine='codex', created_at_ms=1234)
            intent = dict(id='bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb', session_id=sid, native=native)
            self.assertIsNone(guard.protect_operator_adoption(dict(row, created_at_ms=999), intent))
            self.assertIsNone(guard.protect_operator_adoption(dict(row, state='exited'), intent))
            self.assertFalse(guard.is_locked('git', tree))
            self.assertTrue(guard.lock_worktree('git', tree, 'operator protection'))
            self.assertIsNone(guard.protect_operator_adoption(row, intent))
            entries = guard.list_entries('git', tree)
            self.assertTrue(any(e.get('reason') == 'operator protection' for e in entries))

    def test_adopted_repository_witness_rejects_new_incarnation(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'HUB_WORKTREE_IDENTITY_DIR': tmp + '/identity'}):
            repo = _make_repo(tmp)
            tree = _add_worktree(repo, tmp)
            sid = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
            row = dict(id=sid, tag='manual-worker', workspace=tree, engine='codex', created_at_ms=1234, state='running')
            intent = dict(id='bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb', session_id=sid,
                          native=dict(native_id=sid, tag=row['tag'], workspace=tree, engine='codex', created_at_ms=1234))
            self.assertIsNotNone(guard.protect_operator_adoption(row, intent))
            self.assertIsNotNone(guard.expected_repository(row))
            self.assertIsNone(guard.expected_repository(dict(row, created_at_ms=999)))

    def test_installed_dispatcher_reconciles_only_explicit_pending_intent(self):
        from types import SimpleNamespace
        from agent_hub_host.fleet import Dispatcher
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'HUB_WORKTREE_IDENTITY_DIR': tmp + '/identity'}):
            repo = _make_repo(tmp)
            tree = _add_worktree(repo, tmp)
            sid = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
            row = dict(id=sid, tag='manual-worker', workspace=tree, engine='codex', created_at_ms=1234, state='running')
            intent = dict(id='bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb', session_id=sid,
                          native=dict(native_id=sid, tag=row['tag'], workspace=tree, engine='codex', created_at_ms=1234))
            calls = []
            def request(method, path, payload=None):
                calls.append((method, path, payload))
                if method == 'GET':
                    return {'sessions': [intent]}
                self.assertTrue(guard.is_locked('git', tree))
                self.assertEqual(payload['native']['native_id'], sid)
                return {'ok': True}
            dispatcher = SimpleNamespace(settings={}, request=request, runner=lambda argv: [row])
            self.assertEqual(Dispatcher.reconcile_operator_adoptions(dispatcher), 1)
            self.assertEqual([c[:2] for c in calls], [('GET', '/api/sessions/host-adoption'), ('POST', '/api/sessions/host-adoption/receipt')])


class TestStoppedIntakeRestart(unittest.TestCase):
    def test_reuses_confirmed_predecessor_protection_without_unlocking(self):
        with tempfile.TemporaryDirectory() as tmp:
            origin = _make_repo(tmp)
            path = _add_worktree(origin, tmp)
            previous, successor = "a" * 32, "b" * 32
            reason = guard.lock_reason(previous, "intake-restart")
            self.assertTrue(guard.lock_worktree("git", path, reason))
            evidence = {"workspace": path, "repository": guard.repository_identity(path)}
            old = {"session_id": previous, "intake_task_id": "intake-restart",
                   "terminal": True, "native_terminal": True}
            new = {"session_id": successor, "intake_task_id": "intake-restart", "terminal": False}
            with patch.object(guard, "expected_repository", return_value=evidence):
                for change in ({"terminal": False}, {"native_terminal": False},
                               {"intake_task_id": "another"}):
                    self.assertFalse(guard.reuse_stopped_intake_lock("git", path, successor, "intake-restart",
                                     lambda *args: {"sessions": [{**old, **change}, new]}))
                self.assertFalse(guard.reuse_stopped_intake_lock("git", path, successor, "intake-restart",
                                 lambda *args: {"sessions": [old]}))
                self.assertTrue(guard.reuse_stopped_intake_lock("git", path, successor, "intake-restart",
                                lambda *args: {"sessions": [old, new]}))
                row = {"workspace": path, "tag": "task-hub-" + "c" * 32}
                session = {"id": successor, "intake_task_id": "intake-restart"}
                self.assertFalse(guard.protect_adoption(row, session))
                self.assertFalse(guard.protect_adoption(row, session,
                    request=lambda *args: {"sessions": [{**old, "native_terminal": False}, new]}))
                self.assertTrue(guard.protect_adoption(row, session,
                    request=lambda *args: {"sessions": [old, new]}))
            entry = guard.find_entry(guard.list_entries("git", path), path)
            self.assertEqual(entry["reason"], reason)
            self.assertTrue(entry["locked"])
            self.assertNotEqual(_run(["git", "-C", origin, "worktree", "remove", path], tmp).returncode, 0)
            self.assertTrue(os.path.isdir(path))
            self.assertTrue(guard.unlock_worktree("git", path))

    def test_live_successor_release_fence_overrides_unprotected_predecessor(self):
        facts = [{"session_id": "a" * 32, "terminal": True, "native_terminal": True,
                  "linked_intake_open": False, "protected": False, "lock_release_permitted": False}]
        with patch.object(guard, "release_session_lock") as release:
            self.assertEqual(guard.reconcile_terminal_locks(lambda *args: {"sessions": facts}), 0)
        release.assert_not_called()
