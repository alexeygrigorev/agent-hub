"""Workspace roots: advertise existing worktrees and resolve identity paths."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_hub_host import fleet


class WorkspaceRootsTests(unittest.TestCase):
    def test_advertised_includes_worktree_children(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "worktrees"
            root.mkdir()
            child = root / "coolify-website-relaypath-2026-10-22"
            child.mkdir()
            mapped = Path(tmp) / "agent-hub"
            mapped.mkdir()
            settings = {
                "version": 1,
                "workspaces": {str(mapped): str(mapped)},
                "workspace_roots": [str(root)],
                "subscriptions": [],
            }
            ads = fleet.advertised_workspaces(settings)
            self.assertIn(str(mapped), ads)
            self.assertIn(str(root), ads)
            self.assertIn(str(child), ads)

    def test_resolve_identity_under_root_without_map_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "worktrees"
            root.mkdir()
            child = root / "coolify-cmp-relaypath-2026-10-22"
            child.mkdir()
            settings = {
                "workspaces": {},
                "workspace_roots": [str(root)],
                "subscriptions": [
                    {"service": "gemini", "account_label": "shared-gemini", "engine": "antigravity",
                     "admission_argv": ["true"]},
                ],
            }
            resolved = fleet.resolve_workspace_path(settings, str(child))
            self.assertEqual(resolved, child.resolve())
            # _route accepts the absolute worktree path
            sub = fleet.Dispatcher(settings)._route({
                "account": "shared-gemini", "engine": "antigravity", "profile": None,
                "workspace": str(child),
            })
            self.assertEqual(sub["account_label"], "shared-gemini")

    def test_resolve_refuses_outside_roots(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "worktrees"
            root.mkdir()
            outside = Path(tmp) / "escape"
            outside.mkdir()
            settings = {"workspaces": {}, "workspace_roots": [str(root)], "subscriptions": []}
            with self.assertRaises(ValueError):
                fleet.resolve_workspace_path(settings, str(outside))

    def test_auto_detect_worktrees_parent_from_mapped_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "worktrees"
            root.mkdir()
            mapped_child = root / "already-mapped"
            mapped_child.mkdir()
            sibling = root / "new-sibling"
            sibling.mkdir()
            settings = {
                "workspaces": {str(mapped_child): str(mapped_child)},
                "subscriptions": [],
            }
            ads = fleet.advertised_workspaces(settings)
            self.assertIn(str(sibling), ads)

    def test_drop_queued_launcher_task_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "state.db"
            import sqlite3
            conn = sqlite3.connect(str(db))
            conn.executescript("""
                CREATE TABLE tasks (id TEXT PRIMARY KEY, idempotency_key TEXT, payload TEXT, state TEXT);
                CREATE TABLE task_paths (task_id TEXT, path TEXT);
                CREATE TABLE task_resources (task_id TEXT, memory_mb INT, disk_mb INT);
                INSERT INTO tasks VALUES ('hub-offer-1', 'hub-offer-1', '{}', 'queued');
                INSERT INTO task_paths VALUES ('hub-offer-1', '/tmp/x');
                INSERT INTO task_resources VALUES ('hub-offer-1', 1500, 512);
                INSERT INTO tasks VALUES ('hub-offer-2', 'hub-offer-2', '{}', 'running');
            """)
            conn.commit()
            conn.close()
            d = fleet.Dispatcher({"workspaces": {}, "subscriptions": []})
            d._drop_queued_launcher_task({"config_dir": tmp}, "hub-offer-1")
            d._drop_queued_launcher_task({"config_dir": tmp}, "hub-offer-2")
            conn = sqlite3.connect(str(db))
            states = dict(conn.execute("SELECT id, state FROM tasks").fetchall())
            conn.close()
            self.assertNotIn("hub-offer-1", states)
            self.assertEqual(states.get("hub-offer-2"), "running")

    def test_load_settings_accepts_workspace_roots(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "worktrees"
            # absolute_workspace_ok requires [A-Za-z0-9._-] segments — tempfile
            # paths may include other chars; use a sanitized path under /tmp
            safe = Path("/tmp/hub-ws-root-test-roots")
            safe.mkdir(exist_ok=True)
            cfg = {
                "version": 1,
                "subscriptions": [],
                "workspaces": {"home": str(Path.home())},
                "workspace_roots": [str(safe)],
            }
            p = Path(tmp) / "subscriptions.json"
            p.write_text(json.dumps(cfg))
            loaded = fleet.load_settings(p)
            self.assertEqual(loaded["workspace_roots"], [str(safe)])


if __name__ == "__main__":
    unittest.main()
