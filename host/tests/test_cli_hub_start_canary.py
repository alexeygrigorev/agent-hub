"""Offline tests for scripts/hub-start-canary.sh. Fake hub/a replay real output shapes
(captured read-only from `hub sessions list --json`, `a list --json --running`; ids/paths scrubbed).
Mail renders to a sink; no real msmtp/sendmail is ever called.

Run: scripts/capped-test-run.sh -- scripts/with-temp-home.sh -- python3 -m unittest host.tests.test_cli_hub_start_canary
"""
import json, os, pathlib, shutil, stat, subprocess, sys, tempfile, time, unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "hub-start-canary.sh"
SID, NATIVE = "11111111-2222-4333-8444-555555555555", "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"

FAKE_HUB = r'''
import json, os, sys, pathlib, time
d = pathlib.Path(os.environ["FAKE_DIR"]); sc = os.environ.get("FAKE_SCENARIO", "ok")
args = sys.argv[1:]
log = open(d / "calls.log", "a"); log.write(" ".join(args) + "\n"); log.close()
SID = "%s"; NATIVE = "%s"
def item(state, **kw):
    base = {"id": SID, "state": state, "intake_id": None, "project_id": "owner/repo", "machine_id": "HOST-1", "workspace": os.environ["HUB_CANARY_WORKSPACE"],
            "epoch": 1, "operation_id": "op:0", "native_id": NATIVE, "engine": "shell", "model": None,
            "interactive": False, "evidence": None, "attention": None}
    base.update(kw); return base
if args[:2] == ["sessions", "start"]:
    if sc == "start_fail": sys.stderr.write("Session operation failed\n"); sys.exit(1)
    if sc == "typed_start_fail": sys.stderr.write("Session HTTP request failed with status 503 (no_connected_host)\n"); sys.exit(1)
    if sc == "start_garbage": print("not json"); sys.exit(0)
    if sc == "start_noid": print(json.dumps({"state": "queued"})); sys.exit(0)
    first = not (d / "started").exists()
    (d / "started").write_text("1"); (d / "a_running").write_text("1")
    (d / "prompt").write_text(args[args.index("--prompt") + 1])
    run_id = args[args.index("--name") + 1].rsplit("-", 1)[-1]
    (d / "marker").write_text("CANARY-OK" + (":" + run_id if "--engine" not in args else ""))
    if sc == "start_lost_response" and first: print("lost accepted response"); sys.exit(0)
    if sc == "start_slow" and first: time.sleep(1)
    print(json.dumps({"id": SID, "state": "queued", "operation_id": "op:0"})); sys.exit(0)
if args[:2] == ["sessions", "list"]:
    if sc == "late_running": time.sleep(0.5)
    items = [item("failed", id="other", native_id=None, attention={"reason": "engine_not_allowed", "observed_at": 1})]
    if (d / "started").exists():
        if sc == "never_running": items.append(item("claimed", native_id=None, attention={"reason": "native_launch_unverified", "observed_at": 1}))
        elif sc == "failed_state": items.append(item("failed", native_id=None, attention={"reason": "engine_not_allowed", "observed_at": 1}))
        elif sc == "no_workspace": items.append(item("running", workspace=None))  # real server shape today: no workspace
        elif sc == "no_host": items.append(item("running", machine_id=None, workspace="/w/hub-canary"))
        elif sc == "wrong_host": items.append(item("running", machine_id="desktop"))
        elif sc == "wrong_workspace": items.append(item("running", workspace="/wrong/workspace"))
        else: items.append(item("running"))
    print(json.dumps({"items": items[:1] if sc == "past_first_page" else items, "next": None, "complete": True, "sessions": items, "next_cursor": None})); sys.exit(0)
if args[:2] == ["sessions", "stop"]:
    if sc == "stop_fail" or (sc == "cleanup_fail" and "cleanup" in " ".join(args)): sys.exit(1)
    if sc != "gone_fail": (d / "a_running").unlink(missing_ok=True)
    (d / "stopped").write_text(" ".join(args)); sys.exit(0)
sys.exit(2)
''' % (SID, NATIVE)

FAKE_A = r'''
import json, os, sys, pathlib, time
d = pathlib.Path(os.environ["FAKE_DIR"]); sc = os.environ.get("FAKE_SCENARIO", "ok")
args = sys.argv[1:]
log = open(d / "calls.log", "a"); log.write("a " + " ".join(args) + "\n"); log.close()
if args[:1] == ["list"]:
    if sc == "late_gone": time.sleep(0.5)
    rows = [{"agent": "codex", "command": "list", "cwd": "/w/other", "engine": "codex", "id": "zzzzzzzz-0000-4000-8000-000000000000",
             "phase": "running", "state": "running", "tag": "other", "workspace": "/w/other", "worker_alive": True}]
    if (d / "a_running").exists():
        rows.append({"agent": "shell", "cwd": "/w/hub-canary", "engine": "shell", "id": "%s", "phase": "running",
                     "state": "running", "tag": "hub-canary", "workspace": "/w/hub-canary", "worker_alive": True})
    print(json.dumps(rows)); sys.exit(0)
if args[:1] == ["capture"]:
    if sc == "capture_fail": sys.exit(1)
    if sc == "late_capture": time.sleep(0.5)
    prompt = (d / "prompt").read_text()
    marker = (d / "marker").read_text()
    if sc == "capture_prompt_only": print("$ " + prompt if "printf" not in prompt else prompt)
    elif sc == "wrapped_prompt_only": print("Reply CANARY-OK\nand exit")
    elif sc == "capture_empty": pass
    else: print("$ " + prompt + "\n" + marker)
    sys.exit(0)
sys.exit(2)
''' % NATIVE

SINK = '#!/usr/bin/env bash\n{ printf "ARGS\\t%s\\n" "$*"; cat; printf "\\n---END---\\n"; } >> "$MAIL_SINK"\n[[ "${MAIL_FAIL:-0}" != 1 ]]\n'


class CanaryTest(unittest.TestCase):
    def setUp(self):
        base = ROOT / ".local" / "tmp"
        base.mkdir(parents=True, exist_ok=True)
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="hub-canary-test.", dir=base))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        (self.tmp / "fake_hub.py").write_text(FAKE_HUB)
        (self.tmp / "fake_a.py").write_text(FAKE_A)
        self.mailer = self.tmp / "mailer.sh"
        self.mailer.write_text(SINK)
        self.mailer.chmod(0o755)
        (self.tmp / "bin").mkdir()
        trap = self.tmp / "bin" / "msmtp"  # must never be reached
        trap.write_text('#!/usr/bin/env bash\ntouch "%s/REAL_MSMTP_CALLED"\n' % self.tmp)
        trap.chmod(0o755)
        self.sink = self.tmp / "mail.sink"
        self.state = self.tmp / "state"

    def run_canary(self, scenario="ok", *args, mode_prompt=None, **extra):
        env = dict(os.environ)
        env.update(
            PATH=f"{self.tmp / 'bin'}:{env['PATH']}",
            FAKE_DIR=str(self.tmp), FAKE_SCENARIO=scenario,
            HUB_BIN=f"{sys.executable} {self.tmp / 'fake_hub.py'}", A_BIN=f"{sys.executable} {self.tmp / 'fake_a.py'}",
            HUB_CANARY_MAILER=str(self.mailer), MAIL_SINK=str(self.sink),
            HUB_CANARY_STATE_DIR=str(self.state), HUB_CANARY_WORKSPACE=str(self.tmp / "ws"),
            HUB_CANARY_MACHINE="HOST-1", HUB_CANARY_REPO="owner/repo",
            HUB_CANARY_POLL_S="0.02", HUB_CANARY_RUNNING_TIMEOUT_S="0.3", HUB_CANARY_CAPTURE_TIMEOUT_S="0.2",
            HUB_CANARY_STOP_TIMEOUT_S="0.3", HUB_CANARY_CMD_TIMEOUT_S="10",
        )
        env["FAKE_PROMPT"] = mode_prompt or ("Reply CANARY-OK and exit" if ("full" in args or "--deploy-gate" in args) else "printf CANARY-OK; sleep 30")
        env.update(extra)
        for f in ("started", "a_running", "stopped"):
            (self.tmp / f).unlink(missing_ok=True)
        (self.tmp / "calls.log").write_text("")
        r = subprocess.run([str(SCRIPT), *args], env=env, capture_output=True, text=True, timeout=60)
        verdict = json.loads(r.stdout.strip().splitlines()[-1])
        return r, verdict

    def calls(self):
        return (self.tmp / "calls.log").read_text()

    def mails(self):
        return self.sink.read_text().split("---END---")[:-1] if self.sink.exists() else []

    # ---- success and modes
    def test_success_shell(self):
        r, v = self.run_canary("ok", "--mode", "shell")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(v["ok"])
        self.assertEqual(v["mode"], "shell")
        c = self.calls()
        self.assertIn("--engine shell", c)
        self.assertNotIn("--reason", c)
        self.assertIn("sleep 30", c)
        self.assertIn("--prompt printf CANARY-OK; sleep 30", c)
        self.assertIn(str(self.tmp / "ws"), c)
        self.assertIn("a capture " + NATIVE, c)
        self.assertEqual(r.stderr, "")

    def test_success_full_is_modelless(self):
        r, v = self.run_canary("ok", "--mode", "full")
        self.assertEqual(r.returncode, 0, r.stderr)
        c = self.calls()
        self.assertNotIn("--engine", c)
        self.assertNotIn("--model", c)
        self.assertIn("--prompt Run one shell command", c)
        self.assertIn("--repo owner/repo", c)
        self.assertIn("--machine HOST-1", c)
        self.assertIn("--no-wait", c)
        self.assertIn("--name hub-canary-full-", c)

    def test_workspace_created_private(self):
        self.run_canary("ok", "--mode", "shell")
        self.assertTrue((self.tmp / "ws").is_dir())

    # ---- failing stages
    def assert_stage(self, scenario, stage, reason_part, mode="shell"):
        r, v = self.run_canary(scenario, "--mode", mode)
        self.assertEqual(r.returncode, 1)
        self.assertFalse(v["ok"])
        self.assertEqual(v["stage"], stage, v)
        self.assertIn(reason_part, v["reason"])
        self.assertIn(stage, r.stderr)
        return v

    def test_stage_start_cli_failure(self):
        self.assert_stage("start_fail", "start", "hub_cli_exit_1")

    def test_typed_start_failure_is_preserved(self):
        self.assert_stage("typed_start_fail", "start", "no_connected_host")

    def test_stage_start_garbage_and_no_id(self):
        self.assert_stage("start_garbage", "start", "unparseable_json")
        self.assert_stage("start_noid", "start", "no_session_id")

    def test_stage_running_never(self):
        v = self.assert_stage("never_running", "running", "native_launch_unverified")
        self.assertEqual(v["cleanup"], "stopped")

    def test_stage_running_failed_state(self):
        self.assert_stage("failed_state", "running", "state_failed:engine_not_allowed")

    def test_stage_list_host_workspace(self):
        self.assert_stage("no_workspace", "list_host_workspace", "missing_workspace")
        self.assert_stage("no_host", "list_host_workspace", "missing_host")

    def test_stage_capture(self):
        self.assert_stage("capture_prompt_only", "capture", "no_output_beyond_prompt")
        self.assert_stage("capture_empty", "capture", "empty_capture")
        self.assert_stage("capture_fail", "capture", "a_capture_exit_1")

    def test_stage_stop(self):
        v = self.assert_stage("stop_fail", "stop", "hub_cli_exit_1")
        self.assertEqual(v["cleanup"], "FAILED_needs_reconciliation")

    def test_stage_gone(self):
        self.assert_stage("gone_fail", "gone", "still_listed_in_a_running")

    # ---- leak cleanup
    def test_capture_failure_stops_session_with_cleanup_key(self):
        r, v = self.run_canary("capture_empty", "--mode", "shell")
        self.assertEqual(v["cleanup"], "stopped")
        self.assertIn("-cleanup", (self.tmp / "stopped").read_text())
        self.assertFalse((self.tmp / "a_running").exists())

    def test_start_failure_no_stop_attempt(self):
        self.run_canary("start_fail", "--mode", "shell")
        self.assertNotIn("sessions stop", self.calls())

    def test_lost_accepted_response_recovers_same_key_and_stops(self):
        r, verdict = self.run_canary("start_lost_response", "--mode", "shell")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(verdict["session_id"], SID)
        self.assertEqual(verdict["cleanup"], "stopped")
        starts = [line for line in self.calls().splitlines() if line.startswith("sessions start")]
        self.assertEqual(len(starts), 2)
        self.assertEqual(starts[0], starts[1])
        self.assertFalse((self.tmp / "a_running").exists())

    def test_repeated_malformed_receipt_reports_uncertain_cleanup(self):
        _, verdict = self.run_canary("start_garbage", "--mode", "shell")
        self.assertEqual(verdict["cleanup"], "FAILED_needs_reconciliation")

    def test_wrapped_prompt_is_not_model_output(self):
        self.assert_stage("wrapped_prompt_only", "capture", "no_output_beyond_prompt", mode="full")

    def test_aggregated_cli_list_finds_session_beyond_first_page(self):
        result, verdict = self.run_canary("past_first_page", "--mode", "shell")
        self.assertEqual(result.returncode, 0, verdict)
        self.assertIn("sessions list --all --json", self.calls())

    def test_mismatched_host_or_workspace_never_uses_local_capture(self):
        for scenario in ("wrong_host", "wrong_workspace"):
            with self.subTest(scenario=scenario):
                self.assert_stage(scenario, "list_host_workspace", "host_workspace_mismatch")
                self.assertNotIn("a capture", self.calls())

    def test_launch_and_late_reads_obey_stage_deadlines(self):
        for scenario, stage in (("start_slow", "start"), ("late_running", "running"),
                                ("late_capture", "capture"), ("late_gone", "gone")):
            with self.subTest(scenario=scenario):
                started = time.monotonic()
                result, verdict = self.run_canary(scenario, "--mode", "shell")
                self.assertEqual(result.returncode, 1, verdict)
                self.assertEqual(verdict["stage"], stage)
                self.assertLess(time.monotonic() - started, 1.5)

    # ---- alerting
    def test_two_failures_alert_once_then_dedupe(self):
        self.run_canary("capture_empty", "--mode", "shell")
        self.assertEqual(self.mails(), [])
        self.run_canary("capture_empty", "--mode", "shell")
        self.assertEqual(len(self.mails()), 1)
        for _ in range(3):
            self.run_canary("gone_fail", "--mode", "shell")
        self.assertEqual(len(self.mails()), 1)

    def test_mail_content_and_recipient(self):
        self.run_canary("no_workspace", "--mode", "shell")
        r, v = self.run_canary("no_workspace", "--mode", "shell")
        (mail,) = self.mails()
        first = mail.strip().splitlines()[0]
        self.assertTrue(first.endswith("-- dev@dtcdev.click"), first)
        self.assertNotIn("orin", mail)
        self.assertEqual(mail.count("@dtcdev.click"), mail.count("dev@dtcdev.click") + mail.count("alerts@dtcdev.click"))
        self.assertIn("To: dev@dtcdev.click\n", mail)
        self.assertNotIn("Cc:", mail)
        self.assertIn("Failing stage: list_host_workspace", mail)
        self.assertIn("Reason: missing_workspace", mail)
        self.assertIn("Run id: " + v["run_id"], mail)
        self.assertRegex(mail, r"Timestamp: \d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ")
        self.assertFalse((self.tmp / "REAL_MSMTP_CALLED").exists())

    def test_recipient_not_overridable(self):
        for _ in range(2):
            self.run_canary("gone_fail", "--mode", "shell", ALERT_TO="orin@dtcdev.click", ALERT_CC="orin@dtcdev.click")
        (mail,) = self.mails()
        self.assertNotIn("orin", mail)

    def test_recovery_after_two_successes_once(self):
        for _ in range(2):
            self.run_canary("capture_empty", "--mode", "shell")
        self.run_canary("ok", "--mode", "shell")
        self.assertEqual(len(self.mails()), 1)  # one success is not enough
        self.run_canary("ok", "--mode", "shell")
        mails = self.mails()
        self.assertEqual(len(mails), 2)
        self.assertIn("[RECOVERED]", mails[1])
        self.assertIn("To: dev@dtcdev.click", mails[1])
        self.run_canary("ok", "--mode", "shell")
        self.assertEqual(len(self.mails()), 2)

    def test_failure_between_successes_keeps_incident_open_without_new_mail(self):
        for _ in range(2):
            self.run_canary("capture_empty", "--mode", "shell")
        self.run_canary("ok", "--mode", "shell")
        self.run_canary("capture_empty", "--mode", "shell")
        self.run_canary("capture_empty", "--mode", "shell")
        self.assertEqual(len(self.mails()), 1)

    def test_new_incident_after_recovery_mails_again(self):
        for _ in range(2):
            self.run_canary("capture_empty", "--mode", "shell")
        for _ in range(2):
            self.run_canary("ok", "--mode", "shell")
        for _ in range(2):
            self.run_canary("capture_empty", "--mode", "shell")
        self.assertEqual(len(self.mails()), 3)

    def test_single_failure_then_success_no_mail(self):
        self.run_canary("capture_empty", "--mode", "shell")
        self.run_canary("ok", "--mode", "shell")
        self.run_canary("capture_empty", "--mode", "shell")
        self.assertEqual(self.mails(), [])

    def test_mail_send_failure_retries_next_run(self):
        for _ in range(2):
            self.run_canary("capture_empty", "--mode", "shell", MAIL_FAIL="1")
        self.run_canary("capture_empty", "--mode", "shell")
        self.assertEqual(len(self.mails()), 2)  # one failed attempt + the delivered retry
        self.run_canary("capture_empty", "--mode", "shell")
        self.assertEqual(len(self.mails()), 2)

    def test_modes_have_separate_incident_state(self):
        self.run_canary("capture_empty", "--mode", "shell")
        self.run_canary("capture_empty", "--mode", "full")
        self.assertEqual(self.mails(), [])

    # ---- state perms
    def test_state_permissions(self):
        self.run_canary("ok", "--mode", "shell")
        self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o700)
        for f in self.state.glob("state-*.json"):
            self.assertEqual(stat.S_IMODE(f.stat().st_mode), 0o600)
        self.assertTrue((self.state / "state-shell.json").exists())

    def test_default_state_dir_uses_xdg(self):
        r, v = self.run_canary("ok", "--mode", "shell", XDG_STATE_HOME=str(self.tmp / "xdg"), HUB_CANARY_STATE_DIR="")
        self.assertTrue((self.tmp / "xdg" / "agent-hub-canary" / "state-shell.json").exists())
        self.assertEqual(stat.S_IMODE((self.tmp / "xdg" / "agent-hub-canary").stat().st_mode), 0o700)

    # ---- deploy gate
    def test_deploy_gate_pass(self):
        r, v = self.run_canary("ok", "--deploy-gate")
        self.assertEqual(r.returncode, 0)
        self.assertEqual((v["ok"], v["mode"], v["deploy_gate"]), (True, "full", True))
        self.assertNotIn("--engine", self.calls())

    def test_deploy_gate_fail_json_on_stderr_and_no_mail_no_state(self):
        for _ in range(3):
            r, v = self.run_canary("never_running", "--deploy-gate")
            self.assertEqual(r.returncode, 1)
            self.assertEqual(json.loads(r.stderr.strip().splitlines()[-1])["stage"], "running")
        self.assertEqual(self.mails(), [])
        self.assertFalse(self.state.exists())

    def test_deploy_gate_shell_mode(self):
        r, v = self.run_canary("ok", "--deploy-gate", "--mode", "shell")
        self.assertEqual(r.returncode, 0)
        self.assertEqual((v["mode"], v["deploy_gate"]), ("shell", True))
        self.assertIn("--engine shell", self.calls())
        r, v = self.run_canary("capture_prompt_only", "--deploy-gate", "--mode", "shell")
        self.assertEqual((r.returncode, v["stage"]), (1, "capture"))
        self.assertEqual(self.mails(), [])
        self.assertFalse(self.state.exists())

    def test_shell_capture_requires_output_line_not_command_text(self):
        # command line only, even with the marker inside it, must fail
        self.assert_stage("capture_prompt_only", "capture", "no_output_beyond_prompt")


if __name__ == "__main__":
    unittest.main()
