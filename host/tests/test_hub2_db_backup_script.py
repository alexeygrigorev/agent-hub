"""Regression tests for scripts/hub2-db-backup.sh (GCP VM port, 2026-11-07).

Hub2 on the GCP VM stores its Durable Object SQLite databases under the
Node runtime data dir (one SQLite file per object instance,
<Class>-<instance>.sqlite). The daily backup is therefore a file
snapshot (VACUUM INTO per database + counts manifest), not the hub1 REST
export. These tests run the real script against a fake DO state tree:

  - full round trip: snapshot -> two trees -> restore drill PASS
  - the drill detects tampered row counts
  - retention prunes only artifacts older than the enforced 30-day floor
  - the HUB2_DB_BACKUP_FAIL_DRILL=1 drill fails, touches no tree and never
    invokes the alert command
  - a real failure dispatches exactly one --backup-failed alert

The alert command is always a stub writing to a scratch file, so nothing
here can mail a real person.
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "hub2-db-backup.sh"
DRILL = REPO / "scripts" / "hub2-db-restore-drill.py"


class Hub2BackupTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="hub2-backup-test-"))
        self.db_dir = self.root / "hub2-db"
        self.backup = self.root / "backups"
        self.mirror = self.root / "mirror"
        self.state = self.root / "state"
        self.lock = self.state / "hub2-db-backup.lock"
        for path in (self.db_dir, self.backup, self.mirror, self.state):
            path.mkdir(parents=True)
        self.stub_out = self.root / "alert-calls.log"
        self.alert_stub = self.root / "alert-stub.sh"
        self.alert_stub.write_text(
            f"#!/bin/sh\nprintf '%s\\n' \"$@\" >> {self.stub_out}\n"
        )
        self.alert_stub.chmod(0o755)
        self.env = {
            **os.environ,
            "HUB2_DB_BACKUP_ENV_FILE": str(self.root / "missing-env"),
            "HUB2_DB_DIR": str(self.db_dir),
            "BACKUP_DIR": str(self.backup),
            "MIRROR_DIR": str(self.mirror),
            "RETENTION_DAYS": "30",
            "LOCK_FILE": str(self.lock),
            "STATE_DIR": str(self.state),
            "ALERT_CMD": str(self.alert_stub),
        }

    def tearDown(self) -> None:
        subprocess.run(["chmod", "-R", "u+rwX", str(self.root)], check=False)
        subprocess.run(["rm", "-rf", str(self.root)], check=False)

    # ---- fixtures -------------------------------------------------------

    def write_db(self, rel: str, tables: dict[str, list[tuple]]) -> Path:
        db_path = self.db_dir / rel
        con = sqlite3.connect(db_path)
        for name, rows in tables.items():
            con.execute(f"CREATE TABLE {name} (id INTEGER PRIMARY KEY, value TEXT)")
            con.executemany(f"INSERT INTO {name} (value) VALUES (?)", rows)
        con.commit()
        con.close()
        return db_path

    def fake_do_state(self) -> None:
        # Real miniflare shape: instance DBs named <hash>.sqlite under
        # <Class>-<instance>.sqlite flat files, and a
        # WAL sibling that must not be picked up as a database.
        self.write_db(
            "ControlDO-abc123.sqlite",
            {"identities": [("a",), ("b",)], "routes": [("r1",)]},
        )
        self.write_db(
            "metadata.sqlite", {"ns": [("only",)]}
        )
        self.write_db("ProjectDO-def456.sqlite", {"intakes": [("i1",)]})
        (self.db_dir / "CONTROL-global-abc123.sqlite-wal").write_bytes(b"")
        unrelated = self.db_dir / "v3/unrelated/keep.sqlite"
        unrelated.parent.mkdir(parents=True, exist_ok=True)
        unrelated.write_bytes(b"other state")

    def run_backup(self, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
        env = dict(self.env)
        env.update(extra_env or {})
        return subprocess.run(
            ["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=120
        )

    def artifacts(self, tree: Path) -> list[Path]:
        return sorted(tree.glob("*/*/*/hub2-db-*.tar.gz"))

    def newest_artifact(self) -> Path:
        files = self.artifacts(self.backup)
        assert files, "no artifact written"
        return max(files, key=os.path.getmtime)

    # ---- tests ----------------------------------------------------------

    def test_roundtrip_backup_mirror_and_drill(self) -> None:
        self.fake_do_state()
        proc = self.run_backup()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("databases=3", proc.stdout)

        for tree in (self.backup, self.mirror):
            files = self.artifacts(tree)
            self.assertEqual(len(files), 1, f"{tree}: expected exactly one artifact")
            mode = stat.S_IMODE(files[0].stat().st_mode)
            self.assertEqual(mode, 0o600, f"{tree}: artifact must be mode 600")
            rel = files[0].relative_to(tree).parts
            expected_date = subprocess.run(
                ["date", "+%Y/%m/%d"], env={**os.environ, "TZ": "Europe/Berlin"},
                capture_output=True, text=True, check=True,
            ).stdout.strip().split("/")
            self.assertEqual(rel[:3], tuple(expected_date), "YYYY/MM/DD tree layout")

        drill = subprocess.run(
            ["python3", str(DRILL), str(self.newest_artifact()),
             "--out-dir", str(self.root / "drill")],
            capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(drill.returncode, 0, drill.stdout + drill.stderr)
        self.assertIn("PASS", drill.stdout)

    def test_drill_detects_tampered_counts(self) -> None:
        self.fake_do_state()
        self.assertEqual(self.run_backup().returncode, 0)
        artifact = self.newest_artifact()
        unpack = self.root / "tamper"
        unpack.mkdir()
        subprocess.run(["tar", "-C", str(unpack), "-xzf", str(artifact)], check=True)
        db = unpack / "ControlDO-abc123.sqlite"
        con = sqlite3.connect(db)
        con.execute("DELETE FROM routes")
        con.commit()
        con.close()
        tampered = self.root / "tampered.tar.gz"
        subprocess.run(
            ["tar", "-C", str(unpack), "-czf", str(tampered), "."], check=True
        )
        drill = subprocess.run(
            ["python3", str(DRILL), str(tampered), "--out-dir", str(self.root / "drill2")],
            capture_output=True, text=True, timeout=120,
        )
        self.assertNotEqual(drill.returncode, 0)
        self.assertIn("count mismatch", drill.stderr)

    def test_retention_prunes_old_keeps_new(self) -> None:
        self.fake_do_state()
        old_dir = self.backup / "2026/09/01"
        old_dir.mkdir(parents=True)
        old = old_dir / "hub2-db-20260901T0000+0200.tar.gz"
        old.write_bytes(b"old artifact")
        old_epoch = time.time() - 40 * 86400
        os.utime(old, (old_epoch, old_epoch))
        proc = self.run_backup()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertFalse(old.exists(), "artifact older than 31 days must be pruned")
        self.assertEqual(len(self.artifacts(self.backup)), 1)

        young_dir = self.backup / "2026/10/20"
        young_dir.mkdir(parents=True)
        young = young_dir / "hub2-db-20261020T0000+0200.tar.gz"
        young.write_bytes(b"young artifact")
        young_epoch = time.time() - 10 * 86400
        os.utime(young, (young_epoch, young_epoch))
        proc = self.run_backup()
        self.assertEqual(proc.returncode, 0)
        self.assertTrue(young.exists(), "artifact within retention must survive")

    def test_retention_floor_fails_closed(self) -> None:
        self.fake_do_state()
        proc = self.run_backup({"RETENTION_DAYS": "7"})
        self.assertEqual(proc.returncode, 1)
        self.assertIn("RETENTION_DAYS", proc.stdout)
        self.assertFalse(self.artifacts(self.backup))

    def test_drill_flag_touches_nothing_and_never_alerts(self) -> None:
        self.fake_do_state()
        proc = self.run_backup({"HUB2_DB_BACKUP_FAIL_DRILL": "1"})
        self.assertEqual(proc.returncode, 1)
        self.assertIn("[DRILL]", proc.stdout)
        self.assertIn("mail=none", proc.stdout)
        self.assertFalse(self.artifacts(self.backup))
        self.assertFalse(self.artifacts(self.mirror))
        self.assertFalse(self.stub_out.exists(), "drill must never invoke the alert command")

    def test_real_failure_dispatches_one_alert(self) -> None:
        # Missing DO state dir: config failure, alert fires exactly once.
        proc = self.run_backup({"HUB2_DB_DIR": str(self.root / "nowhere")})
        self.assertEqual(proc.returncode, 1)
        calls = self.stub_out.read_text().splitlines()
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], "--backup-failed")
        self.assertIn("HUB2_DB_DIR missing", calls[1])

    def test_manifest_counts_match_fixture(self) -> None:
        self.fake_do_state()
        self.run_backup()
        unpack = self.root / "manifest-check"
        unpack.mkdir()
        subprocess.run(["tar", "-C", str(unpack), "-xzf", str(self.newest_artifact())], check=True)
        manifest = json.loads((unpack / "manifest.json").read_text())
        self.assertEqual(manifest["snapshot"], "hub2-node-sqlite")
        counts = {entry["path"]: entry["tables"] for entry in manifest["databases"]}
        self.assertEqual(
            counts["ControlDO-abc123.sqlite"],
            {"identities": 2, "routes": 1},
        )


if __name__ == "__main__":
    unittest.main()
