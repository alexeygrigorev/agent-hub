"""Synthetic-only recovery regression; no network, credentials or real identifiers."""
import gzip
from contextlib import closing
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("transform", Path(__file__).resolve().parents[1] / "scripts/hub2-backup-transform.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class TransformTests(unittest.TestCase):
    def fixture(self, root):
        rows = {
            "intake_tasks": [{"id": "synthetic-task", "data": json.dumps({"id": "synthetic-task", "project": "synthetic/project", "title": "verbatim original", "status": "open", "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z", "history": [
                {"kind": "intake", "text": "Original ask", "captured_at": "2026-01-01T00:00:00Z"},
                {"kind": "link", "hub_session_id": "synthetic-hub", "native_id": "synthetic-native"},
                {"kind": "note", "native_id": "ignore-non-link"}, {"kind": "link", "native_id": 123}]})}],
            "intake_task_projection": [{"id": "synthetic-task", "links": json.dumps([
                {"kind": "link", "native_id": "synthetic-projection"}, {"kind": "link", "native_id": "synthetic-native"}])}],
            "intake_link_refs": [{"ref": "stale-derived", "task_id": "synthetic-task"}] * 2,
            "intake_events": [{"seq": 2, "body": "second"}, {"seq": 1, "body": "first"}],
            "binary_data": [{"id": 1, "value": {"__hub_backup_blob_b64": "AAH/"}}],
            "real_data": [{"value": 1}],
            "intake_note_archive": [],
        }
        schema = {
            "intake_tasks": "CREATE TABLE intake_tasks(id TEXT PRIMARY KEY,data TEXT)",
            "intake_task_projection": "CREATE TABLE intake_task_projection(id TEXT PRIMARY KEY,links TEXT)",
            "intake_link_refs": "CREATE TABLE intake_link_refs(ref TEXT,task_id TEXT,PRIMARY KEY(ref,task_id)) WITHOUT ROWID",
            "intake_events": "CREATE TABLE intake_events(seq INTEGER PRIMARY KEY,body TEXT)",
            "binary_data": "CREATE TABLE binary_data(id INTEGER PRIMARY KEY,value BLOB)",
            "real_data": "CREATE TABLE real_data(value REAL)",
            "intake_note_archive": "CREATE TABLE intake_note_archive(id TEXT PRIMARY KEY,task_id TEXT,seq INTEGER,data TEXT)",
            "event_index": "CREATE INDEX event_index ON intake_events(body)",
        }
        path = root / "source.json.gz"
        with gzip.open(path, "wt") as stream:
            json.dump({"snapshot": "hub-do-sqlite", "exported_at": "synthetic-time", "schema": schema,
                       "row_counts": {k: len(v) for k, v in rows.items()}, "tables": rows}, stream)
        return path

    def test_rebuild_only_derived_and_repeatable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.fixture(root)
            before = module.file_hash(source)
            first = module.restore(source, root / "first", before)
            second = module.restore(source, root / "second", before)
            self.assertEqual(first, second)
            self.assertEqual(module.file_hash(source), before)
            self.assertEqual(first["tables"]["intake_link_refs"]["source"]["rows"], 2)
            self.assertEqual(first["tables"]["intake_link_refs"]["restored"]["rows"], 3)
            self.assertEqual(first["canonical_tables_unchanged"], 6)
            with closing(sqlite3.connect(root / "first/restored.sqlite")) as db:
                self.assertEqual(db.execute("SELECT body FROM intake_events ORDER BY seq").fetchall(), [("first",), ("second",)])
                self.assertEqual(db.execute("SELECT value FROM binary_data").fetchone()[0], b"\x00\x01\xff")
            with self.assertRaises(FileExistsError):
                module.restore(source, root / "first", before)

    def test_prepared_plan_is_ndjson_with_verified_merkle_membership(self):
        import hashlib
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.fixture(root)
            module.restore(source, root / "restore")
            manifest = module.prepare_plan(root / "restore", root / "plan")
            lines = (root / "plan/tasks.ndjson").read_text().splitlines()
            self.assertEqual(len(lines), 1)
            task = json.loads(lines[0])
            self.assertEqual(task["id"], "synthetic-task")
            self.assertEqual(task["status"], "needs_attention")
            self.assertEqual(task["text"], "Original ask")
            value = hashlib.sha256(lines[0].encode()).hexdigest()
            self.assertEqual(value, manifest["payload_digests"][0])
            node = hashlib.sha256(("leaf:" + value).encode()).hexdigest()
            for sibling in manifest["proofs"][0]:
                pair = sibling["hash"] + node if sibling["side"] == "left" else node + sibling["hash"]
                node = hashlib.sha256(("node:" + pair).encode()).hexdigest()
            self.assertEqual(node, manifest["merkle_root"])
            self.assertFalse(manifest["live_state_verified"])

    def test_source_mutation_cannot_publish_success_proof(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.fixture(root)
            actual_hash = module.file_hash
            calls = 0
            def changing_hash(path):
                nonlocal calls
                if Path(path) == source:
                    calls += 1
                    if calls > 1:
                        return "0" * 64
                return actual_hash(path)
            with patch.object(module, "file_hash", side_effect=changing_hash):
                with self.assertRaises(RuntimeError):
                    module.restore(source, root / "refused")
            self.assertFalse((root / "refused/proof.json").exists())
            self.assertTrue((root / "refused/failed.json").exists())

    def test_bad_checksum_refuses_before_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.fixture(root)
            with self.assertRaises(ValueError):
                module.restore(source, root / "refused", "0" * 64)
            self.assertFalse((root / "refused").exists())

    def test_canonical_duplicate_is_not_silently_repaired(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.fixture(root)
            with gzip.open(source, "rt") as stream:
                body = json.load(stream)
            body["tables"]["intake_events"].append(body["tables"]["intake_events"][0])
            body["row_counts"]["intake_events"] += 1
            with gzip.open(source, "wt") as stream:
                json.dump(body, stream)
            with self.assertRaises(RuntimeError):
                module.restore(source, root / "failed")
            self.assertFalse((root / "failed/proof.json").exists())
            self.assertTrue((root / "failed/failed.json").exists())


if __name__ == "__main__":
    unittest.main()
