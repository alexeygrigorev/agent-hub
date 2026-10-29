#!/usr/bin/env python3
"""Hub DB restore drill (intake e27e0f9d): load an export into a throwaway
SQLite database and compare row counts with the export and live Hub.

    python3 scripts/hub-db-restore-drill.py [EXPORT.json.gz] [--out-dir DIR] [--no-live]

Without EXPORT, the newest hub-do-*.json.gz under BACKUP_DIR /
STORAGEBOX_DIR is used. Steps: replay tables, load every row (lossless
bigint/blob markers decoded), then indexes, triggers and views (so the
append-only guard triggers exist but never fire during the load), run
PRAGMA integrity_check, and compare each restored table count with the
export's row_counts (must be equal) and with live GET /api/backup/status
(reported as drift; rows written since the export are expected).

Read-only towards Hub. The throwaway database lives in a fresh 0700
directory under --out-dir (default ~/.local/hub-db-restore-drill), never
/tmp, and is left for inspection. The operator token is read from
HUB_ADMIN_TOKEN_FILE and never printed. Exit 0 when the restore matches the
export exactly and every exported table exists live.
"""

from __future__ import annotations

import argparse
import base64
import glob
import gzip
import json
import os
import sqlite3
import sys
import tempfile
import time
import urllib.request

CORE = ("intake_tasks", "session_control_sessions", "claims", "audit")


def newest_export() -> str:
    roots = [os.environ.get("BACKUP_DIR", "/var/backups/hub-do"),
             os.environ.get("STORAGEBOX_DIR", os.path.expanduser("~/storagebox/backups/hub-do"))]
    files = [f for r in roots for f in glob.glob(os.path.join(r, "*", "*", "*", "hub-do-*.json.gz"))]
    if not files:
        sys.exit("no hub-do-*.json.gz exports found")
    return max(files, key=os.path.getmtime)


def decode(value):
    if isinstance(value, dict):
        if "__hub_backup_bigint" in value:
            return int(value["__hub_backup_bigint"])
        if "__hub_backup_blob_b64" in value:
            return base64.b64decode(value["__hub_backup_blob_b64"])
        return json.dumps(value)
    if isinstance(value, list):
        return json.dumps(value)
    return value


def ddl_kind(ddl: str) -> int:
    head = ddl.lstrip().upper()
    if head.startswith("CREATE TABLE"):
        return 0
    if head.startswith("CREATE INDEX") or head.startswith("CREATE UNIQUE INDEX"):
        return 1
    return 2  # triggers, views


def live_counts() -> dict:
    base = os.environ.get("HUB_BASE_URL", "https://hub.dtcdev.click").rstrip("/")
    token_file = os.environ.get("HUB_ADMIN_TOKEN_FILE",
                                os.path.expanduser("~/.config/agent-hub/admin-token"))
    with open(token_file, encoding="utf-8") as fh:
        token = fh.read().strip()
    req = urllib.request.Request(base + "/api/backup/status", headers={
        "Authorization": "Bearer " + token,
        "User-Agent": "agent-hub-host/1",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("export", nargs="?")
    ap.add_argument("--out-dir", default=os.path.expanduser("~/.local/hub-db-restore-drill"))
    ap.add_argument("--no-live", action="store_true", help="skip the live status comparison")
    args = ap.parse_args()

    path = args.export or newest_export()
    os.makedirs(args.out_dir, mode=0o700, exist_ok=True)
    work = tempfile.mkdtemp(prefix=time.strftime("drill-%Y%m%dT%H%M%S-"), dir=args.out_dir)
    db_path = os.path.join(work, "hub-do-restore.sqlite")
    print(f"export   {path}")
    print(f"restore  {db_path}")

    with gzip.open(path, "rt", encoding="utf-8") as fh:
        body = json.load(fh)
    assert body.get("snapshot") == "hub-do-sqlite", "not a hub-do export"
    schema, tables, counts = body["schema"], body["tables"], body["row_counts"]
    print(f"exported_at {body.get('exported_at')}  tables={len(tables)}  schema_objects={len(schema)}")

    con = sqlite3.connect(db_path)
    objects = sorted(schema.items(), key=lambda kv: (ddl_kind(kv[1]), kv[0]))
    ddl_errors = []
    for name, ddl in objects:
        if ddl_kind(ddl) != 0:
            continue
        try:
            con.execute(ddl)
        except sqlite3.Error as err:
            ddl_errors.append((name, str(err)))
    for table, rows in tables.items():
        if not rows:
            continue
        cols = list(rows[0].keys())
        sql = 'INSERT INTO "{}" ({}) VALUES ({})'.format(
            table, ",".join(f'"{c}"' for c in cols), ",".join("?" * len(cols)))
        con.executemany(sql, ([decode(r.get(c)) for c in cols] for r in rows))
    con.commit()
    for name, ddl in objects:
        if ddl_kind(ddl) == 0:
            continue
        try:
            con.execute(ddl)
        except sqlite3.Error as err:
            ddl_errors.append((name, str(err)))
    con.commit()
    integrity = con.execute("PRAGMA integrity_check").fetchone()[0]

    restored = {t: con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in tables}
    mismatch = {t: (counts.get(t), n) for t, n in restored.items() if counts.get(t) != n}
    live = {} if args.no_live else live_counts().get("row_counts", {})
    missing_live = sorted(t for t in tables if live and t not in live)

    print(f"integrity_check {integrity}  ddl_errors={len(ddl_errors)}")
    for name, err in ddl_errors:
        print(f"  ddl error {name}: {err}")
    print(f"{'table':34s} {'export':>8s} {'restored':>9s} {'live':>8s} {'drift':>6s}")
    for t in CORE + tuple(sorted(set(tables) - set(CORE))):
        lv = live.get(t)
        drift = "" if lv is None else str(lv - restored[t])
        if t in CORE or t in mismatch or (lv is not None and lv != restored[t]):
            print(f"{t:34s} {counts.get(t, '-')!s:>8s} {restored[t]:>9d} {lv if lv is not None else '-'!s:>8s} {drift:>6s}")
    total = sum(restored.values())
    print(f"total rows restored {total} / exported {sum(counts.values())}; "
          f"tables equal to export {len(restored) - len(mismatch)}/{len(restored)}")
    if live:
        print(f"live tables {len(live)}; exported tables missing live: {missing_live or 'none'}")
    ok = not mismatch and not ddl_errors and integrity == "ok" and not missing_live
    print("RESTORE_DRILL " + ("OK" if ok else "FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
