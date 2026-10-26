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

    def test_redacts_before_utf16_clipping_and_rejects_blank_reports(self):
        d,tmp=_dispatcher();self.addCleanup(tmp.cleanup)
        ws,res=_mk_results(tmp.name);offer='11111111-2222-4333-8444-555555555555'
        rp=res/(offer+'.json');record={'offer_id':offer,'workspace':str(ws),'result_path':str(rp)}
        rp.write_text(json.dumps({'status':'done','report':' ', 'artifacts':[]}))
        self.assertEqual(d._read_hub_finish_report(record),{})
        rp.write_text(json.dumps({'status':'done','report':'\x1b[31m\x1b[0m'}))
        self.assertEqual(d._read_hub_finish_report(record),{})
        rp.write_text(json.dumps({'status':'done','report':('work '+chr(0x1f600)+' ')*1000+' hubt_private-value',
                                 'artifacts':[{'label':chr(0x1f600)*120,'text':'hubr_private-value'}]}))
        out=d._read_hub_finish_report(record)
        self.assertLessEqual(len(out['report'].encode('utf-16-le'))//2,4096)
        self.assertLessEqual(len(out['artifacts'][0]['label'].encode('utf-16-le'))//2,120)
        self.assertNotIn('private-value',json.dumps(out))

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
        rp.write_text(json.dumps({"status": "stuck", "report": "ordinary worker prose " * 500,
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
    def test_normal_gh_config_directory_survives_managed_home_without_auth_file_copy(self):
        from agent_hub_host import provisioning
        with tempfile.TemporaryDirectory() as tmp:
            host = Path(tmp) / 'host'
            managed = Path(tmp) / 'managed'
            for inherited, expected in [({'XDG_CONFIG_HOME':str(host / '.config')}, host / '.config/gh'),
                                         ({'GH_CONFIG_DIR':str(host / 'custom-gh')}, host / 'custom-gh')]:
                with patch.dict(os.environ, inherited, clear=True), \
                     patch.object(provisioning, 'load_git_identity', return_value={}):
                    env = provisioning.engine_environment({'provider':'codex'}, managed)
                self.assertEqual(env['HOME'], str(managed))
                self.assertEqual(env['GH_CONFIG_DIR'], str(expected))
                self.assertFalse(managed.exists())

    def test_worker_env_never_inherits_host_hub_credentials(self):
        from agent_hub_host import provisioning
        with tempfile.TemporaryDirectory() as tmp, \
             patch.dict(os.environ, {'HUB_TOKEN':'test-only','HUB_JOB_TOKEN':'test-only',
                                     'HUB_CONFIG_FILE':'/test-only/host.json'}), \
             patch.object(provisioning, 'load_git_identity', return_value={}):
            env = provisioning.engine_environment({'provider':'codex'}, Path(tmp) / 'managed')
            for key in ['HUB_TOKEN','HUB_JOB_TOKEN','HUB_CONFIG_FILE']:
                self.assertNotIn(key, env)


if __name__ == "__main__":
    unittest.main()
