"""Regression tests for scripts/hub-db-backup.sh (intake e27e0f9d).

The 2026-10-29 nightly export failed with "row drift too large
table=session_control_commands assembled=1710 post=1748 bound=34": a capture
burst appended 38 rows after the table had been read, and the script
compared each table with a run-wide post status taken later. These tests run
the real script against a local fake Hub that replays that pattern, plus
real row loss, transient drift (bounded retry), an API v4 server, the
same-run failure alert and its drill. The alert command is always a stub and
the staleness mail tests use stub MTAs with *.invalid.test recipients, so
nothing here can mail a real person.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "hub-db-backup.sh"
STALENESS = REPO / "scripts" / "hub-db-backup-staleness.sh"
TOKEN = "synthetic_backup_test_token_0123456789"


class FakeHub:
    """Minimal /api/backup/* server: rowid tables keyed by rowid, plus one
    WITHOUT ROWID table paged by offset only."""

    def __init__(self, api_version: int = 5) -> None:
        self.api_version = api_version
        self.tables: dict[str, dict[int, dict]] = {}
        self.without_rowid: set[str] = set()
        self.lock = threading.Lock()
        self.requests: list[str] = []
        # Hooks: called with (hub, table, chunk_body) after a chunk is built.
        self.after_chunk = None
        # Per-table count of rows to hide from chunk reads (simulated loss).
        self.hide_rows: dict[str, int] = {}
        for name in ("intake_tasks", "session_control_sessions", "claims", "audit"):
            self.add_table(name, 3)
        self.add_table("token_hours_posted", 4, without_rowid=True)

    def add_table(self, name: str, n: int, without_rowid: bool = False) -> None:
        self.tables[name] = {i + 1: {"id": f"{name}-{i}", "v": i} for i in range(n)}
        if without_rowid:
            self.without_rowid.add(name)

    def append(self, name: str, n: int) -> None:
        rows = self.tables[name]
        start = max(rows) if rows else 0
        for i in range(n):
            rows[start + i + 1] = {"id": f"{name}-new-{start + i}", "v": -1}

    def ddl(self, name: str) -> str:
        suffix = " WITHOUT ROWID" if name in self.without_rowid else ""
        return f"CREATE TABLE {name} (id TEXT PRIMARY KEY, v INTEGER){suffix}"

    def status(self) -> dict:
        body = {
            "exported_at": "2026-10-29T03:21:02.000Z",
            "snapshot": "hub-do-sqlite",
            "tables": sorted(self.tables),
            "row_counts": {t: len(r) for t, r in self.tables.items()},
            "api_version": self.api_version,
        }
        if self.api_version >= 5:
            body["without_rowid"] = sorted(self.without_rowid)
        return body

    def chunk(self, q: dict) -> tuple[int, dict]:
        table = q["table"][0]
        limit = int(q.get("limit", ["500"])[0])
        if table not in self.tables:
            return 404, {"error": "unknown_table"}
        rows = self.tables[table]
        ordered = sorted(rows.items())
        hide = self.hide_rows.get(table, 0)
        if hide:
            ordered = ordered[hide:]
        body = {
            "exported_at": "2026-10-29T03:20:47.000Z",
            "snapshot": "hub-do-sqlite",
            "table": table,
            "limit": limit,
            "total": len(rows),
            "ddl": self.ddl(table),
            "value_markers": {"bigint": 0, "blob": 0, "unreadable": 0},
        }
        if "after" in q:
            if self.api_version < 5 or table in self.without_rowid:
                return 400, {"error": "keyset_unsupported"}
            after = int(q["after"][0])
            page = [(k, r) for k, r in ordered if k > after][:limit]
            body.update(cursor="rowid", after=after, next_after=page[-1][0] if page else None)
        else:
            offset = int(q.get("offset", ["0"])[0])
            page = ordered[offset:offset + limit]
            body.update(cursor="offset", offset=offset)
        body["rows"] = [dict(r) for _, r in page]
        if self.after_chunk:
            self.after_chunk(self, table, body)
        return 200, body

    def handle(self, path: str) -> tuple[int, dict]:
        url = urlparse(path)
        q = parse_qs(url.query)
        with self.lock:
            self.requests.append(path)
            if url.path == "/api/backup/status":
                return 200, self.status()
            if url.path == "/api/backup/export" and q.get("schema") == ["1"]:
                return 200, {"exported_at": "x", "snapshot": "hub-do-sqlite",
                             "schema": {t: self.ddl(t) for t in self.tables}}
            if url.path == "/api/backup/export" and "table" in q:
                return self.chunk(q)
        return 404, {"error": "not_found"}


def serve(hub: FakeHub) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                code, body = 401, {"error": "unauthorized"}
            else:
                code, body = hub.handle(self.path)
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@unittest.skipUnless(shutil.which("curl") and shutil.which("flock"), "curl/flock required")
class HubDbBackupScriptTest(unittest.TestCase):
    def setUp(self) -> None:
        # The script refuses TMPDIR under /tmp; keep every artifact in the repo
        # scratch area (gitignored .local/tmp) and remove it afterwards.
        base = REPO / ".local" / "tmp"
        base.mkdir(parents=True, exist_ok=True)
        self.work = Path(tempfile.mkdtemp(prefix="hub-db-backup-test-", dir=base))
        self.addCleanup(shutil.rmtree, self.work, True)
        self.token = self.work / "token"
        self.token.write_text(TOKEN)
        self.token.chmod(0o600)
        self.alert_log = self.work / "alerts.jsonl"
        self.alert_cmd = self.work / "alert-stub.sh"
        self.alert_cmd.write_text(
            "#!/usr/bin/env python3\nimport json, sys\n"
            f"open({str(self.alert_log)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n")
        self.alert_cmd.chmod(0o700)

    def run_backup(self, hub: FakeHub | None, **extra: str) -> subprocess.CompletedProcess:
        server = serve(hub) if hub else None
        if server:
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
        port = server.server_address[1] if server else 9
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(self.work),
            "HUB_DB_BACKUP_ENV_FILE": str(self.work / "no-such.env"),
            "HUB_BASE_URL": f"http://127.0.0.1:{port}",
            "HUB_ALLOW_HTTP_LOCAL": "1",
            "HUB_ADMIN_TOKEN_FILE": str(self.token),
            "BACKUP_DIR": str(self.work / "local"),
            "STORAGEBOX_DIR": str(self.work / "box"),
            "LOCK_FILE": str(self.work / "lock" / "backup.lock"),
            "TMPDIR": str(self.work / "tmp"),
            "CHUNK_ROWS": "500",
            "CURL_TIMEOUT": "20",
            "ALERT_CMD": str(self.alert_cmd),
            **extra,
        }
        return subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True,
                              text=True, timeout=180)

    def alerts(self) -> list[list[str]]:
        if not self.alert_log.exists():
            return []
        return [json.loads(line) for line in self.alert_log.read_text().splitlines()]

    def exports(self, root: str) -> list[Path]:
        return sorted((self.work / root).rglob("hub-do-*.json.gz"))

    def load_export(self) -> dict:
        files = self.exports("local")
        self.assertEqual(len(files), 1, files)
        out = subprocess.run(["gzip", "-dc", str(files[0])], capture_output=True, check=True)
        return json.loads(out.stdout)

    def test_hot_table_appends_after_read_succeed_without_loss(self) -> None:
        """Replay of 2026-10-29: 1710 rows read, then a capture burst appends
        38+ rows before the post status. Must succeed, export every row read
        exactly once, and not page."""
        hub = FakeHub()
        hub.add_table("session_control_commands", 1710)
        seen = {"done": False}

        def burst(h: FakeHub, table: str, body: dict) -> None:
            # Final chunk served (its COUNT(*) is 1710); 38 rows land right
            # after, like the 03:20:57 capture burst, and more keep coming.
            if table == "session_control_commands" and len(body["rows"]) < body["limit"]:
                seen["done"] = True
                h.append("session_control_commands", 38)
            elif seen["done"]:
                h.append("session_control_commands", 10)

        hub.after_chunk = burst
        res = self.run_backup(hub)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("ALL_DONE", res.stdout)
        doc = self.load_export()
        rows = doc["tables"]["session_control_commands"]
        self.assertEqual(len(rows), 1710)
        self.assertEqual(len({r["id"] for r in rows}), 1710)
        # The live table moved far past the old run-wide bound (max(10, 2%)).
        live = len(hub.tables["session_control_commands"])
        self.assertGreater(live - 1710, max(10, int(live * 0.02)))
        self.assertIn("table ok session_control_commands rows=1710 cursor=rowid", res.stdout)
        self.assertIn("table ok token_hours_posted rows=4 cursor=offset", res.stdout)
        self.assertEqual(self.exports("box")[0].read_bytes(), self.exports("local")[0].read_bytes())
        self.assertEqual(stat.S_IMODE(self.exports("local")[0].stat().st_mode), 0o600)
        self.assertEqual(self.alerts(), [])

    def test_inserts_during_keyset_read_are_captured_once(self) -> None:
        hub = FakeHub()
        hub.add_table("session_control_commands", 1200)

        def grow(h: FakeHub, table: str, body: dict) -> None:
            if table == "session_control_commands":
                h.append(table, 7)

        hub.after_chunk = grow
        res = self.run_backup(hub)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        rows = self.load_export()["tables"]["session_control_commands"]
        self.assertEqual(len(rows), len({r["id"] for r in rows}))
        self.assertGreaterEqual(len(rows), 1200 + 7 * 2)

    def test_persistent_row_loss_fails_after_bounded_retries_and_pages(self) -> None:
        hub = FakeHub()
        hub.add_table("session_control_commands", 1000)
        hub.hide_rows["session_control_commands"] = 100  # chunks omit 100 live rows
        res = self.run_backup(hub, TABLE_ATTEMPTS="3")
        self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
        self.assertIn("attempt=1/3", res.stdout)
        self.assertIn("attempt=2/3", res.stdout)
        self.assertIn("row drift too large table=session_control_commands assembled=900 "
                      "final_total=1000 bound=20 after 3 attempts", res.stdout)
        self.assertEqual(self.exports("local"), [])
        self.assertEqual(self.exports("box"), [])
        alerts = self.alerts()
        self.assertEqual(len(alerts), 1, alerts)
        self.assertEqual(alerts[0][0], "--backup-failed")
        self.assertIn("session_control_commands", alerts[0][1])
        self.assertNotIn("--drill", alerts[0])

    def test_transient_drift_recovers_on_retry(self) -> None:
        hub = FakeHub()
        hub.add_table("session_control_commands", 1000)
        hub.hide_rows["session_control_commands"] = 100
        reads = {"n": 0}

        def heal(h: FakeHub, table: str, body: dict) -> None:
            if table == "session_control_commands" and len(body["rows"]) < body["limit"]:
                reads["n"] += 1
                h.hide_rows.pop(table, None)

        hub.after_chunk = heal
        res = self.run_backup(hub)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("WARN: drift table=session_control_commands assembled=900", res.stdout)
        self.assertIn("table ok session_control_commands rows=1000 cursor=rowid attempts=2", res.stdout)
        self.assertEqual(len(self.load_export()["tables"]["session_control_commands"]), 1000)
        self.assertEqual(self.alerts(), [])

    def test_api_v4_server_falls_back_to_offset_paging(self) -> None:
        hub = FakeHub(api_version=4)
        hub.add_table("session_control_commands", 1100)
        res = self.run_backup(hub)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("table ok session_control_commands rows=1100 cursor=offset", res.stdout)
        self.assertFalse(any("after=" in p for p in hub.requests))

    def test_http_failure_pages_with_reason(self) -> None:
        hub = FakeHub()
        hub.add_table("session_control_commands", 10)
        hub.tables.pop("claims")  # status lists it; chunk read 404s
        orig = hub.status

        def status_with_claims() -> dict:
            body = orig()
            body["tables"] = sorted(body["tables"] + ["claims"])
            body["row_counts"]["claims"] = 1
            return body

        hub.status = status_with_claims
        res = self.run_backup(hub)
        self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
        alerts = self.alerts()
        self.assertEqual(len(alerts), 1, alerts)
        self.assertIn("chunk fetch failed table=claims", alerts[0][1])
        self.assertIn("HTTP 404", alerts[0][1])

    def test_fail_drill_pages_ops_only_without_touching_hub(self) -> None:
        hub = FakeHub()
        res = self.run_backup(hub, HUB_DB_BACKUP_FAIL_DRILL="1")
        self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
        self.assertEqual(hub.requests, [])
        alerts = self.alerts()
        self.assertEqual(len(alerts), 1, alerts)
        self.assertEqual(alerts[0][0], "--backup-failed")
        self.assertIn("DRILL", alerts[0][1])
        self.assertEqual(alerts[0][-1], "--drill")

    def test_config_failure_pages(self) -> None:
        self.token.chmod(0o644)
        res = self.run_backup(None)
        self.assertEqual(res.returncode, 1)
        alerts = self.alerts()
        self.assertEqual(len(alerts), 1, alerts)
        self.assertIn("token file must be mode 600", alerts[0][1])


class StalenessBackupFailedModeTest(unittest.TestCase):
    """--backup-failed mail routing with stub MTAs (never real recipients)."""

    def setUp(self) -> None:
        base = REPO / ".local" / "tmp"
        base.mkdir(parents=True, exist_ok=True)
        self.work = Path(tempfile.mkdtemp(prefix="hub-db-stale-fail-test-", dir=base))
        self.addCleanup(shutil.rmtree, self.work, True)
        self.capture = self.work / "capture"
        self.capture.mkdir()
        stubbin = self.work / "stubbin"
        stubbin.mkdir()
        for mta in ("sendmail", "msmtp"):
            p = stubbin / mta
            p.write_text(f"#!/usr/bin/env bash\ncat > \"{self.capture}/{mta}.$$.$RANDOM\"\n")
            p.chmod(0o700)
        (self.work / "local" / "2026" / "10" / "28").mkdir(parents=True)
        (self.work / "local" / "2026" / "10" / "28" / "hub-do-20261028T0420+0100.json.gz").write_bytes(b"x")
        self.env = {
            "PATH": f"{stubbin}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            "HOME": str(self.work),
            "HUB_DB_BACKUP_ENV_FILE": str(self.work / "no-such.env"),
            "BACKUP_DIR": str(self.work / "local"),
            "STORAGEBOX_DIR": str(self.work / "box"),
            "STATE_DIR": str(self.work / "state"),
            "ALERT_TO": "stub-alarm@invalid.test",
            "ALERT_OPS_TO": "stub-ops@invalid.test",
        }

    def run_stale(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["bash", str(STALENESS), *args], env=self.env,
                              capture_output=True, text=True, timeout=60)

    def mails(self) -> list[str]:
        return [p.read_text() for p in sorted(self.capture.iterdir())]

    def test_backup_failed_pages_alert_to_immediately(self) -> None:
        res = self.run_stale("--backup-failed", "row drift too large table=x\nsecond line")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        mails = self.mails()
        self.assertEqual(len(mails), 1, mails)
        self.assertIn("To: stub-alarm@invalid.test", mails[0])
        self.assertIn("Subject: [FAIL] Hub DB backup run failed", mails[0])
        self.assertIn("Reason: row drift too large table=x second line", mails[0])
        self.assertIn("hub-do-20261028T0420+0100.json.gz", mails[0])
        # Never touches the staleness dedupe state.
        self.assertFalse((self.work / "state" / "staleness.state").exists())

    def test_backup_failed_drill_goes_to_ops_only(self) -> None:
        res = self.run_stale("--backup-failed", "DRILL", "--drill")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        mails = self.mails()
        self.assertEqual(len(mails), 1, mails)
        self.assertIn("To: stub-ops@invalid.test", mails[0])
        self.assertNotIn("stub-alarm@invalid.test", mails[0])
        self.assertIn("[FAIL][DRILL]", mails[0])

    def test_backup_failed_dry_run_sends_nothing(self) -> None:
        res = self.run_stale("--backup-failed", "x", "--dry-run")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("would-alert backup-failed to=stub-alarm@invalid.test", res.stdout)
        self.assertEqual(self.mails(), [])


if __name__ == "__main__":
    unittest.main()
