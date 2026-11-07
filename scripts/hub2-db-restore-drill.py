#!/usr/bin/env python3
"""Hub2 DB restore drill (GCP VM deployment, port-gcloud 2026-11-07).

Loads a hub2-do-*.tar.gz artifact produced by scripts/hub2-db-backup.sh
into a throwaway 0700 directory and verifies it without touching the
running deployment:

    python3 scripts/hub2-db-restore-drill.py [ARTIFACT.tar.gz] [--out-dir DIR]

Without ARTIFACT, the newest hub2-do-*.tar.gz under BACKUP_DIR / MIRROR_DIR
is used. Steps: extract every snapshot SQLite database, re-run
PRAGMA integrity_check on each, and compare every per-table row count with
the manifest recorded inside the artifact (must be exact — the snapshot is
internally consistent, unlike the live hub1 REST export). Reports each
database path, user_version and table counts.

Read-only towards the deployment. The throwaway directory lives under
--out-dir (default ~/.local/hub2-db-restore-drill), never /tmp, and is left
for inspection. Exit 0 when every database passes integrity_check and every
table count matches the manifest exactly.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sqlite3
import sys
import tarfile
import tempfile

CORE_HINT = "hub2-do-*.tar.gz"


def newest_artifact() -> str:
    roots = [os.environ.get("BACKUP_DIR", "/var/backups/hub-do"),
             os.environ.get("MIRROR_DIR", "")]
    files: list[str] = []
    for root in roots:
        if root:
            files.extend(glob.glob(os.path.join(root, "*", "*", "*", CORE_HINT)))
    if not files:
        sys.exit(f"no {CORE_HINT} artifacts found under the configured trees")
    return max(files, key=os.path.getmtime)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifact", nargs="?", default=None)
    parser.add_argument("--out-dir", default=os.path.expanduser("~/.local/hub2-db-restore-drill"))
    args = parser.parse_args()

    artifact = args.artifact or newest_artifact()
    if not os.path.isfile(artifact):
        sys.exit(f"artifact not found: {artifact}")

    out_dir = os.path.abspath(args.out_dir)
    os.makedirs(out_dir, mode=0o700, exist_ok=True)
    run_dir = tempfile.mkdtemp(prefix="drill-", dir=out_dir)
    os.chmod(run_dir, 0o700)

    with tarfile.open(artifact, "r:gz") as tar:
        tar.extractall(run_dir, filter="data")

    manifest_path = os.path.join(run_dir, "manifest.json")
    if not os.path.isfile(manifest_path):
        sys.exit("manifest.json missing from artifact")
    with open(manifest_path, "rb") as fh:
        manifest = json.load(fh)
    if manifest.get("snapshot") != "hub2-do-sqlite":
        sys.exit(f"unexpected snapshot marker: {manifest.get('snapshot')!r}")

    failures: list[str] = []
    total_tables = 0
    total_rows = 0
    for entry in manifest.get("databases", []):
        rel = entry["path"]
        db_path = os.path.join(run_dir, rel)
        if not os.path.isfile(db_path):
            failures.append(f"{rel}: database missing from artifact")
            continue
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            failures.append(f"{rel}: integrity_check={integrity}")
            continue
        expected = entry.get("tables", {})
        actual = {
            row[0]: con.execute(f'SELECT COUNT(*) FROM "{row[0]}"').fetchone()[0]
            for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        }
        if actual != expected:
            diffs = {k: (expected.get(k), actual.get(k)) for k in set(expected) | set(actual)
                     if expected.get(k) != actual.get(k)}
            failures.append(f"{rel}: count mismatch {diffs}")
            continue
        total_tables += len(actual)
        total_rows += sum(actual.values())
        print(f"ok: {rel} user_version={entry.get('user_version')} tables={len(actual)} rows={total_rows}")
        con.close()

    print(f"summary: databases={len(manifest.get('databases', []))} tables={total_tables} rows={total_rows}")
    if failures:
        for line in failures:
            print(f"FAIL: {line}", file=sys.stderr)
        return 1
    print(f"PASS: {artifact}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
