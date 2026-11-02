"""Windows headless Claude false-fail (intake 17e774f8, session b99fab81).

On Tabo the maintained launcher ran Claude to completion (~110 s) but no
first action validated, so `run --backend aplexer` exited 1 with empty
stdout/stderr. The host recorded launcher_command_failed with output
withheld and settled the retired native failed, never forwarding the
worker's valid result JSON.

Now the retired native is bound and settled only through the ordinary
reconciliation proof: launcher release confirmed, exited only for a clean
native exit (code 0, no signal) with a done report, failed with the report
forwarded otherwise, uncertain while release or identity is unproven.
"""
import datetime as dt
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_hub_host.fleet import Dispatcher, _clean_native_success
from agent_hub_host.launch_diagnostics import LocalCommandError, launcher_record_cause

NATIVE_ID = "cc4714fa-330b-4e22-9899-d5116a937d12"
OFFER_ID = "598e9345-69c0-4a9d-a15b-6353936e88c1"
TASK_ID = "hub-" + OFFER_ID
RESULT = "/api/fleet/launches/" + OFFER_ID + "/result"
CLEAN = {"id": NATIVE_ID, "exit": {"code": 0, "signal": None}}
KILLED = {"id": NATIVE_ID, "exit": {"code": 137, "signal": 9}}
RELEASED = {"task_id": TASK_ID, "state": "failed", "native_status": "dead",
            "resources_reserved": False}


def empty_exit(raw=""):
    """Exactly what run_json raised for b99fab81: exit 1, no output."""
    return LocalCommandError(raw, command="python.exe", exit_code=1, stdout="", stderr="")


@unittest.skipUnless(os.name == "posix", "POSIX-only dispatch path")
class WindowsHeadlessFalseFailTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for target in (patch.dict(os.environ, GIT_CEILING_DIRECTORIES=tmp.name),
                       # Never depend on the real host's load (Hetzner runs >100).
                       patch("agent_hub_host.host_load.check_live_load", return_value={})):
            target.start()
            self.addCleanup(target.stop)
        os.chmod(tmp.name, 0o700)
        root = Path(tmp.name)
        self.workspace = root / "ws"
        (self.workspace / ".local" / "hub-results").mkdir(mode=0o700, parents=True)
        self.launcher_root = root / "launcher"
        self.launcher_cfg = root / "launcher_cfg"
        self.journal = root / "journal"
        self.state_dir = root / "aplexer-state"
        for d in (self.launcher_root, self.launcher_cfg, self.journal, self.state_dir):
            d.mkdir(mode=0o700)
        # The launcher's store exists; settle_unstarted refuses a bound task.
        (self.launcher_cfg / "state.db").write_bytes(b"")
        self.write_start()
        # do_run's terminal record: no first action before native death.
        self.write_launch_record(started_at_ts=time.time() + 1)
        self.reconcile = dict(RELEASED)
        self.lose_terminal_ack = False
        self.run_error = empty_exit

    def write_start(self, **change):
        doc = {"id": NATIVE_ID, "tag": "task-" + TASK_ID, "workspace": str(self.workspace.resolve()),
               "engine": "claude", "created_at_ms": int(time.time() * 1000), "phase": "starting"}
        doc.update(change)
        (self.workspace / ".local" / f"launch-{TASK_ID}-start.json").write_text(json.dumps(doc))

    def write_launch_record(self, **change):
        doc = {"task_id": TASK_ID, "outcome": "failed", "reason": None,
               "native_status_at_deadline": "phase=finished containment_empty=True"}
        doc.update(change)
        (self.workspace / ".local" / f"launch-{TASK_ID}.json").write_text(json.dumps(doc))

    def write_result(self, status="done", report="Posted the note on 52f0e7fd."):
        path = self.workspace / ".local" / "hub-results" / (OFFER_ID + ".json")
        path.write_text(json.dumps({"status": status, "report": report,
                                    "artifacts": [{"label": "note", "text": "ok"}]}))

    def make_dispatcher(self):
        self.requests = []
        lost = []

        def fake_request(method, path, payload=None):
            self.requests.append((method, path, payload))
            if (self.lose_terminal_ack and not lost
                    and (payload or {}).get("state") in ("exited", "failed")):
                lost.append(True)
                raise TimeoutError("lost ACK")
            return {"id": OFFER_ID, "state": (payload or {}).get("state")}

        def fake_runner(argv, cwd=None, timeout=None, payload=None):
            cmd = " ".join(argv)
            if "/bin/true" in cmd:
                return {"launch_allowed": True, "profile_supported": True,
                        "account_label": "claude-max",
                        "observed_at": dt.datetime.now(dt.timezone.utc).isoformat()}
            if " submit " in cmd:
                return {"id": TASK_ID}
            if " plan " in cmd:
                return {"eligible": [{"provider": "claude", "model": "sonnet", "task_fit": 1.0}]}
            if " run " in cmd:
                raise self.run_error()
            if " reconcile " in cmd:
                return dict(self.reconcile)
            return {}

        settings = {
            "version": 1,
            "workspaces": {"main": str(self.workspace)},
            "aplexer_state_dir": str(self.state_dir),
            "subscriptions": [{"service": "claude", "account_label": "claude-max",
                               "engine": "claude", "launcher_provider": "claude",
                               "launcher_model": "sonnet", "profile": "hub-claude-max",
                               "admission_argv": ["/bin/true"]}],
            "launcher": {"argv": ["python.exe", "-m", "launcher"],
                         "root": str(self.launcher_root), "config_dir": str(self.launcher_cfg)},
        }
        self.dispatcher = Dispatcher(settings, request=fake_request, runner=fake_runner,
                                     journal_dir=str(self.journal))
        return self.dispatcher

    def retirement(self, tomb):
        verdict = (True, tomb, "retired_tombstone_verified") if tomb is not None else (
            False, None, "live_catalog_query_failed: boom")
        return (patch("agent_hub_host.headless_health.inspect", return_value=None),
                patch("agent_hub_host.retired_native.verify_native_retirement", return_value=verdict),
                patch("agent_hub_host.fleet.verify_native_retirement", return_value=verdict))

    def dispatch(self, tomb=CLEAN):
        offer = {"id": OFFER_ID, "engine": "claude", "account": "claude-max",
                 "profile": "hub-claude-max", "mode": "headless", "workspace": "main",
                 "prompt": "post a note on the intake"}
        dispatcher = self.make_dispatcher()
        a, b, c = self.retirement(tomb)
        with a, b, c:
            return dispatcher.dispatch(offer)

    def reconcile_later(self, tomb=CLEAN):
        a, b, c = self.retirement(tomb)
        with a, b, c:
            return self.dispatcher.reconcile_launches(terminal_only=True)

    def posted(self):
        return [p for (_m, path, p) in self.requests if path == RESULT and isinstance(p, dict)]

    def terminal(self):
        return [p for p in self.posted() if p.get("state") in ("exited", "failed")]

    def journal_record(self):
        return json.loads((self.journal / "fleet-launches" / (OFFER_ID + ".json")).read_text())

    # --- b99fab81 positive path -------------------------------------------

    def test_b99fab81_clean_exit_with_done_result_settles_exited_with_report(self):
        self.write_result()
        result = self.dispatch()
        self.assertEqual(result["state"], "exited")
        final = self.posted()[-1]
        self.assertEqual(final["state"], "exited")
        self.assertTrue(final["confirmed_exit"])
        self.assertEqual(final["native_id"], NATIVE_ID)
        self.assertEqual(final["launcher_task_id"], TASK_ID)
        self.assertEqual(final["report_status"], "done")
        self.assertEqual(final["report"], "Posted the note on 52f0e7fd.")
        self.assertEqual(final["runtime"]["outcome"], "reported")
        self.assertEqual(final["runtime"]["exit_code"], 0)
        self.assertNotIn("diagnostic", final)
        self.assertEqual(len(self.terminal()), 1)
        record = self.journal_record()
        self.assertTrue(record["settled"])
        self.assertEqual(record["state"], "exited")

    # --- negative probes --------------------------------------------------

    def test_signal_killed_native_with_done_result_is_failed_with_report(self):
        self.write_result()
        self.assertEqual(self.dispatch(tomb=KILLED)["state"], "failed")
        final = self.posted()[-1]
        self.assertEqual(final["state"], "failed")
        self.assertEqual(final["report_status"], "done")
        self.assertEqual(final["report"], "Posted the note on 52f0e7fd.")
        self.assertEqual(final["runtime"]["exit_code"], 137)
        self.assertEqual(final["runtime"]["exit_signal"], 9)
        self.assertEqual(final["diagnostic"]["reason"], "launcher_command_failed")
        self.assertFalse(any(p["state"] == "exited" for p in self.posted()))

    def test_missing_exit_evidence_never_settles_exited(self):
        self.write_result()
        self.dispatch(tomb={"id": NATIVE_ID})
        self.assertEqual(self.posted()[-1]["state"], "failed")

    def test_worker_failed_report_is_forwarded_on_failed(self):
        self.write_result(status="failed", report="Could not reach the intake.")
        self.dispatch()
        final = self.posted()[-1]
        self.assertEqual(final["state"], "failed")
        self.assertEqual(final["report_status"], "failed")
        self.assertEqual(final["report"], "Could not reach the intake.")

    def test_no_result_fails_with_launcher_cause_not_withheld(self):
        self.dispatch()
        final = self.posted()[-1]
        self.assertEqual(final["state"], "failed")
        self.assertEqual(final["native_id"], NATIVE_ID)
        diag = final["diagnostic"]
        self.assertEqual(diag["reason"], "launcher_command_failed")
        self.assertEqual(diag["exit_code"], 1)
        self.assertEqual(diag["stage"], "dispatch_headless")
        self.assertNotIn("withheld", diag["message"])
        self.assertIn("exit code 1", diag["message"])
        self.assertIn("No first action was validated", diag["tail"])
        self.assertLessEqual(len(diag["message"]), 300)

    def test_other_diagnosed_failures_keep_existing_policy(self):
        # model_mismatch is a diagnosed refusal, not the b99fab81 empty exit.
        self.write_result()
        self.run_error = lambda: empty_exit("model_mismatch: native command does not prove model")
        result = self.dispatch()
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["reason"], "launcher_model_mismatch")
        self.assertFalse(any(p["state"] == "exited" for p in self.posted()))
        self.assertNotIn(" reconcile ", " ".join(" ".join(map(str, r)) for r in self.requests))

    def test_unconfirmed_launcher_release_keeps_reservation_until_reconciled(self):
        self.write_result()
        self.reconcile = dict(RELEASED, resources_reserved=True)
        result = self.dispatch()
        self.assertEqual(result["state"], "uncertain")
        self.assertTrue(result["pending_release"])
        self.assertEqual([p["state"] for p in self.posted()], ["uncertain"])
        self.assertEqual(self.posted()[-1]["native_id"], NATIVE_ID)
        self.assertNotEqual(self.journal_record().get("settled"), True)
        # Release later: ordinary reconciliation settles it exactly once.
        self.reconcile = dict(RELEASED)
        self.assertEqual([r["state"] for r in self.reconcile_later()], ["exited"])
        self.assertEqual(self.posted()[-1]["state"], "exited")
        self.assertEqual(self.posted()[-1]["report_status"], "done")
        self.assertEqual(len(self.terminal()), 1)

    def test_wrong_launcher_identity_never_settles(self):
        self.write_result()
        for reconcile in (dict(RELEASED, native_id="11111111-1111-4111-8111-111111111111"),
                          dict(RELEASED, task_id="hub-other"), {"resources_reserved": False,
                                                                "state": "failed"}):
            with self.subTest(reconcile=reconcile):
                self.reconcile = reconcile
                self.assertEqual(self.dispatch()["state"], "uncertain")
                self.assertEqual(self.terminal(), [])

    def test_lost_terminal_ack_returns_uncertain_and_is_replayed(self):
        self.write_result()
        self.lose_terminal_ack = True
        self.assertEqual(self.dispatch()["state"], "uncertain")
        record = self.journal_record()
        self.assertNotEqual(record.get("settled"), True)
        self.assertEqual(record["terminal_payload"]["state"], "exited")
        # The pending terminal payload is replayed, never shadowed by uncertain.
        self.assertFalse(any(p["state"] == "uncertain" for p in self.posted()))
        self.assertEqual([r["state"] for r in self.reconcile_later()], ["exited"])
        self.assertEqual(self.posted()[-1]["state"], "exited")
        self.assertTrue(self.journal_record()["settled"])

    def test_foreign_malformed_or_stale_start_identity_never_settles(self):
        self.write_result()
        stale = int(time.time() * 1000) - 3_600_000
        for change in ({"tag": "task-hub-other"}, {"workspace": "/elsewhere"},
                       {"engine": "codex"}, {"created_at_ms": stale}, {"created_at_ms": "x"}):
            with self.subTest(change=change):
                self.write_start(**change)
                self.assertEqual(self.dispatch()["state"], "uncertain")
                self.assertEqual(self.terminal(), [])

    def test_unknown_native_state_stays_uncertain_with_cause(self):
        self.write_result()
        self.assertEqual(self.dispatch(tomb=None)["state"], "uncertain")
        self.assertEqual(self.posted()[-1]["state"], "uncertain")
        self.assertIn("No first action was validated", self.posted()[-1]["diagnostic"]["tail"])


class CleanNativeSuccessTests(unittest.TestCase):
    def test_only_integer_zero_without_signal_and_done(self):
        done = {"report_status": "done"}
        self.assertTrue(_clean_native_success(done, {"exit_code": 0, "exit_signal": None}))
        self.assertTrue(_clean_native_success(done, {"exit_code": 0, "exit_signal": 0}))
        for evidence in ({"exit_code": False}, {"exit_code": 0, "exit_signal": False},
                         {"exit_code": 0, "exit_signal": 9}, {"exit_code": 137},
                         {"exit_code": None}, {"exit_code": 0.0}, {}, None):
            self.assertFalse(_clean_native_success(done, evidence), evidence)
        self.assertFalse(_clean_native_success({"report_status": "failed"}, {"exit_code": 0}))
        self.assertFalse(_clean_native_success({}, {"exit_code": 0}))


class LauncherRecordCauseTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / ".local").mkdir()
        self.at = 1000.0
        self.failure = {"reason": "launcher_command_failed", "error_type": "RuntimeError",
                        "stage": "dispatch_headless", "exit_code": 1, "command": "python.exe",
                        "stdout_tail": "", "stderr_tail": "",
                        "message": "The launcher command failed with exit code 1; unrecognized output withheld."}

    def write(self, **doc):
        base = {"task_id": TASK_ID, "started_at_ts": self.at + 2}
        base.update(doc)
        path = self.root / ".local" / f"launch-{TASK_ID}.json"
        path.write_text(json.dumps(base))
        return path

    def cause(self, root=None):
        return launcher_record_cause(self.failure, root or self.root, TASK_ID, self.at)

    def test_reason_and_stderr_are_redacted_and_path_free(self):
        self.write(reason="aplexer start rc=2: token=sk-abcdefghijklmnopqrstuvwxyz0123 at "
                          "C:\\Users\\John Smith\\proj\\x.json; and /home/alexey/.secret",
                   aplexer_stderr="API_KEY=secret-fixture invalid option \\\\tabo\\Private Dir\\y")
        value = self.cause()
        self.assertIn("aplexer start rc=2", value["tail"])
        self.assertIn("invalid option", value["stderr_tail"])
        self.assertIn("exit code 1", value["message"])
        self.assertNotIn("withheld", value["message"])
        text = json.dumps(value)
        for leaked in ("sk-abc", "secret-fixture", "John", "Smith", "alexey", "tabo", "Private"):
            self.assertNotIn(leaked, text)
        self.assertLessEqual(len(value["message"]), 300)
        self.assertLessEqual(len(value["stderr_tail"]), 512)
        self.assertEqual(self.failure["stderr_tail"], "")

    def test_failed_outcome_without_reason_uses_fixed_text(self):
        self.write(outcome="failed")
        self.assertEqual(self.cause()["tail"], "No first action was validated before native exit.")

    def test_foreign_stale_or_unbound_records_are_ignored(self):
        for doc in ({"task_id": "hub-other"}, {"task_id": None}, {"started_at_ts": self.at - 3600},
                    {"started_at_ts": self.at + 3600}, {"started_at_ts": None},
                    {"started_at_ts": True}):
            self.write(reason="wrong cause", **doc)
            self.assertIs(self.cause(), self.failure, doc)
        self.assertIs(launcher_record_cause(self.failure, self.root, "../evil", self.at), self.failure)
        self.assertIs(launcher_record_cause(self.failure, self.root, TASK_ID, None), self.failure)

    def test_linked_record_linked_local_and_oversize_are_refused(self):
        real = self.root / "real.json"
        real.write_text(json.dumps({"task_id": TASK_ID, "started_at_ts": self.at, "reason": "x"}))
        (self.root / ".local" / f"launch-{TASK_ID}.json").symlink_to(real)
        self.assertIs(self.cause(), self.failure)
        other = self.root / "other"
        (other / "real").mkdir(parents=True)
        (other / "real" / f"launch-{TASK_ID}.json").write_text(real.read_text())
        (other / ".local").symlink_to(other / "real")
        self.assertIs(self.cause(other), self.failure)
        (self.root / ".local" / f"launch-{TASK_ID}.json").unlink()
        self.write(reason="x" * 300000)
        self.assertIs(self.cause(), self.failure)


if __name__ == "__main__":
    unittest.main()
