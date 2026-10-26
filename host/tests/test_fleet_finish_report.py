"""Host finish-report forwarding (intake 88b3260d AC1) + worker CLI auth (AC2/AC3)."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_hub_host.fleet import Dispatcher


def _mk_results(tmpname: str):
    """Create ws/.local/hub-results with 0700 on every level (private_fs)."""
    ws = Path(tmpname) / "ws"
    ws.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(ws, 0o700)
    local = ws / ".local"
    local.mkdir(mode=0o700, exist_ok=True)
    os.chmod(local, 0o700)
    res = local / "hub-results"
    res.mkdir(mode=0o700, exist_ok=True)
    os.chmod(res, 0o700)
    return ws, res


def _dispatcher():
    tmp = tempfile.TemporaryDirectory()
    os.chmod(tmp.name, 0o700)
    settings = {
        "version": 1,
        "workspaces": {"main": tmp.name},
        "subscriptions": [],
        "aplexer_bin": "aplexer",
    }
    d = Dispatcher(settings)
    d._active_launches = {}
    return d, tmp


class TestReadHubFinishReport(unittest.TestCase):
    def test_forwards_valid_report(self):
        d, tmp = _dispatcher()
        self.addCleanup(tmp.cleanup)
        ws, _res = _mk_results(tmp.name)
        offer = "11111111-2222-4333-8444-555555555555"
        rp = ws / ".local" / "hub-results" / (offer + ".json")
        rp.write_text(json.dumps({"status": "done", "report": "All green.", "artifacts": [{"label": "sha", "text": "abc"}]}))
        record = {"offer_id": offer, "workspace": str(ws), "result_path": str(rp)}
        out = d._read_hub_finish_report(record)
        self.assertEqual(out["report_status"], "done")
        self.assertEqual(out["report"], "All green.")
        self.assertEqual(out["artifacts"], [{"label": "sha", "text": "abc"}])

    def test_missing_file_returns_empty(self):
        d, tmp = _dispatcher()
        self.addCleanup(tmp.cleanup)
        record = {"offer_id": "11111111-2222-4333-8444-555555555555",
                  "workspace": tmp.name, "result_path": tmp.name + "/nope.json"}
        self.assertEqual(d._read_hub_finish_report(record), {})

    def test_refuses_symlink_and_escape(self):
        d, tmp = _dispatcher()
        self.addCleanup(tmp.cleanup)
        ws, _res = _mk_results(tmp.name)
        offer = "11111111-2222-4333-8444-555555555555"
        outside = Path(tmp.name) / "outside.json"
        outside.write_text(json.dumps({"status": "done", "report": "x"}))
        link = ws / ".local" / "hub-results" / (offer + ".json")
        try:
            link.symlink_to(outside)
        except OSError:
            self.skipTest("symlinks unavailable")
        record = {"offer_id": offer, "workspace": str(ws), "result_path": str(link)}
        self.assertEqual(d._read_hub_finish_report(record), {})
        record2 = {"offer_id": offer, "workspace": str(ws), "result_path": str(outside)}
        self.assertEqual(d._read_hub_finish_report(record2), {})

    def test_rejects_bad_status_and_truncates(self):
        d, tmp = _dispatcher()
        self.addCleanup(tmp.cleanup)
        ws, _res = _mk_results(tmp.name)
        offer = "11111111-2222-4333-8444-555555555555"
        rp = ws / ".local" / "hub-results" / (offer + ".json")
        rp.write_text(json.dumps({"status": "bogus", "report": "x"}))
        record = {"offer_id": offer, "workspace": str(ws), "result_path": str(rp)}
        self.assertEqual(d._read_hub_finish_report(record), {})
        rp.write_text(json.dumps({"status": "stuck", "report": "y" * 9000,
                                  "artifacts": [{"label": "l", "text": "t"}] * 30}))
        out = d._read_hub_finish_report(record)
        self.assertEqual(out["report_status"], "stuck")
        self.assertLessEqual(len(out["report"]), 4096)
        self.assertLessEqual(len(out["artifacts"]), 8)

    def test_strips_control_chars_and_rejects_bad_shapes(self):
        d, tmp = _dispatcher()
        self.addCleanup(tmp.cleanup)
        ws, _res = _mk_results(tmp.name)
        offer = "11111111-2222-4333-8444-555555555555"
        rp = ws / ".local" / "hub-results" / (offer + ".json")
        rp.write_text(json.dumps({"status": "done", "report": "ok\x00fine\x1f!"}))
        record = {"offer_id": offer, "workspace": str(ws), "result_path": str(rp)}
        out = d._read_hub_finish_report(record)
        self.assertEqual(out["report"], "okfine!")
        # non-string report, oversized file, invalid JSON, non-dict: all {}
        rp.write_text(json.dumps({"status": "done", "report": {"x": 1}}))
        self.assertEqual(d._read_hub_finish_report(record), {})
        rp.write_text("x" * 70000)
        self.assertEqual(d._read_hub_finish_report(record), {})
        rp.write_text("{not json")
        self.assertEqual(d._read_hub_finish_report(record), {})
        rp.write_text(json.dumps([1, 2, 3]))
        self.assertEqual(d._read_hub_finish_report(record), {})

    def test_refuses_non_exact_paths(self):
        d, tmp = _dispatcher()
        self.addCleanup(tmp.cleanup)
        ws, _res = _mk_results(tmp.name)
        offer = "11111111-2222-4333-8444-555555555555"
        other = ws / ".local" / "hub-results" / "other.json"
        other.write_text(json.dumps({"status": "done", "report": "x"}))
        # Basename must be <offer_id>.json, even inside the right directory.
        record = {"offer_id": offer, "workspace": str(ws), "result_path": str(other)}
        self.assertEqual(d._read_hub_finish_report(record), {})
        dotdot = ws / ".local" / "hub-results" / ("sub/../" + offer + ".json")
        record2 = {"offer_id": offer, "workspace": str(ws), "result_path": str(dotdot)}
        self.assertEqual(d._read_hub_finish_report(record2), {})

    def test_reconcile_attaches_report_to_terminal_payload(self):
        d, tmp = _dispatcher()
        self.addCleanup(tmp.cleanup)
        ws, _res = _mk_results(tmp.name)
        offer = "11111111-2222-4333-8444-555555555555"
        rp = ws / ".local" / "hub-results" / (offer + ".json")
        rp.write_text(json.dumps({"status": "waiting-on-human", "report": "Need input on X."}))
        record = {"offer_id": offer, "task_id": "hub-" + offer, "mode": "headless",
                  "workspace": str(ws), "target_cwd": str(ws), "engine": "opencode",
                  "native_id": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", "tag": "task-hub-" + offer,
                  "incarnation": 1700000000000, "state": "running", "settled": False,
                  "result_path": str(rp)}
        d._active_launches[offer] = record
        seen = {}

        def fake_request(method, path, payload=None):
            seen["payload"] = payload
            return {"id": offer, "state": "exited"}

        with patch.object(Dispatcher, "_persist_launch_record", lambda self, r: None), \
             patch.object(Dispatcher, "_maybe_cleanup_worktree", lambda self, w, n=None: None), \
             patch("agent_hub_host.fleet.verify_native_retirement", return_value=(True, {}, {})), \
             patch.object(d, "request", side_effect=fake_request), \
             patch.object(d, "runner", return_value={"state": "completed-awaiting-review",
                     "native_id": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", "resources_reserved": False}):
            d.settings["launcher"] = {"argv": ["x"], "root": tmp.name, "config_dir": tmp.name}
            out = d.reconcile_launches()
        self.assertTrue(any(r.get("id") == offer for r in out))
        self.assertEqual(seen["payload"].get("report_status"), "waiting-on-human")
        self.assertIn("Need input", seen["payload"].get("report", ""))


class TestWorkerCliAuthProvisioning(unittest.TestCase):
    def test_provisions_gh_copy_and_hub_dir_without_secrets_in_logs(self):
        from agent_hub_host import provisioning
        with tempfile.TemporaryDirectory() as real_home, tempfile.TemporaryDirectory() as managed:
            os.chmod(real_home, 0o700)
            os.chmod(managed, 0o700)
            src = Path(real_home) / ".config" / "gh"
            src.mkdir(mode=0o700, parents=True)
            (src / "hosts.yml").write_text("github.com:\n  oauth_token: ghp_faketoken1234567890\n")
            os.chmod(src / "hosts.yml", 0o600)
            dest_home = Path(managed) / "home"
            dest_home.mkdir(mode=0o700)
            with patch.object(Path, "home", return_value=Path(real_home)):
                provisioning._provision_worker_cli_auth(dest_home)
            copied = dest_home / ".config" / "gh" / "hosts.yml"
            self.assertTrue(copied.is_file())
            self.assertIn("github.com", copied.read_text())
            hub_dir = dest_home / ".config" / "agent-hub"
            self.assertTrue(hub_dir.is_dir())
            # The Hub host credential must never be copied into workers.
            self.assertFalse((hub_dir / "host.json").exists())

    def test_missing_host_gh_auth_is_not_a_failure(self):
        from agent_hub_host import provisioning
        with tempfile.TemporaryDirectory() as real_home, tempfile.TemporaryDirectory() as managed:
            os.chmod(real_home, 0o700)
            os.chmod(managed, 0o700)
            dest_home = Path(managed) / "home"
            dest_home.mkdir(mode=0o700)
            with patch.object(Path, "home", return_value=Path(real_home)):
                provisioning._provision_worker_cli_auth(dest_home)
            self.assertTrue((dest_home / ".config" / "agent-hub").is_dir())

    def test_stale_worker_gh_copy_removed_when_host_deauths(self):
        from agent_hub_host import provisioning
        with tempfile.TemporaryDirectory() as real_home, tempfile.TemporaryDirectory() as managed:
            os.chmod(real_home, 0o700)
            os.chmod(managed, 0o700)
            dest_home = Path(managed) / "home"
            stale = dest_home / ".config" / "gh"
            stale.mkdir(mode=0o700, parents=True)
            os.chmod(dest_home, 0o700)
            os.chmod(dest_home / ".config", 0o700)
            os.chmod(stale, 0o700)
            (stale / "hosts.yml").write_text("github.com:\n  oauth_token: stale\n")
            os.chmod(stale / "hosts.yml", 0o600)
            with patch.object(Path, "home", return_value=Path(real_home)):
                provisioning._provision_worker_cli_auth(dest_home)
            self.assertFalse((stale / "hosts.yml").exists())


if __name__ == "__main__":
    unittest.main()
