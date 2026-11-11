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
import json, os, signal, sys, pathlib, time
d = pathlib.Path(os.environ["FAKE_DIR"]); sc = os.environ.get("FAKE_SCENARIO", "ok")
args = sys.argv[1:]
with open(d / "argv.jsonl", "a") as recorded:
    recorded.write(json.dumps(args) + "\n")
log = open(d / "calls.log", "a"); log.write(" ".join(args) + "\n"); log.close()
SID = "%s"; NATIVE = "%s"
def item(state, **kw):
    name = (d / "session_name").read_text() if (d / "session_name").exists() else None
    key = (d / "session_key").read_text() if (d / "session_key").exists() else None
    base = {"id": SID, "state": state, "intake_id": None, "project_id": "owner/repo", "machine_id": "HOST-1", "workspace": os.environ["HUB_CANARY_WORKSPACE"],
            "epoch": 1, "operation_id": "op:0", "native_id": NATIVE, "engine": "shell", "model": None,
            "name": name, "key": key, "interactive": False, "evidence": None, "attention": None}
    base.update(kw); return base
if args[:2] == ["sessions", "start"]:
    if sc == "start_fail": sys.stderr.write("Session operation failed\n"); sys.exit(1)
    if sc == "typed_start_fail": sys.stderr.write("Session HTTP request failed with status 503 (no_connected_host)\n"); sys.exit(1)
    if sc == "start_garbage": print("not json"); sys.exit(0)
    if sc == "start_noid": print(json.dumps({"state": "queued"})); sys.exit(0)
    if sc == "replay_timeout_empty_list": time.sleep(2); sys.exit(0)
    first = not (d / "started").exists()
    (d / "started").write_text("1"); (d / "a_running").write_text("1")
    prompt_text = args[args.index("--prompt") + 1]
    (d / "prompt").write_text(prompt_text)
    session_name = args[args.index("--name") + 1]
    session_key = args[args.index("--key") + 1]
    (d / "session_name").write_text(session_name)
    (d / "session_key").write_text(session_key)
    run_id = session_name.rsplit("-", 1)[-1]
    if "printf '%%s:%%s:%%s\\n' CANARY-OK " in prompt_text:
        parts = prompt_text.split(";")[0].split()
        (d / "marker").write_text(f"CANARY-OK:{parts[-2]}:{parts[-1]}")
    else:
        (d / "marker").write_text("CANARY-OK" + (":" + run_id if "--engine" not in args else ""))
    if sc == "sigterm_on_start" and first:
        parent_pid = int(os.environ["CANARY_TEST_PID"]) if "CANARY_TEST_PID" in os.environ else os.getppid()
        os.kill(parent_pid, signal.SIGTERM)
        time.sleep(2)
        sys.exit(1)
    if sc in ("same_name_unrelated_key", "client_key_only_recovery", "conflicting_key_rows"):
        print("lost accepted response"); sys.exit(0)
    if sc == "start_lost_response" and first: print("lost accepted response"); sys.exit(0)
    if sc == "start_slow" and first: time.sleep(1)
    print(json.dumps({"id": SID, "state": "queued", "operation_id": "op:0"})); sys.exit(0)
if args[:1] == ["routing-machines"]:
    host_ver = os.environ.get("FAKE_HOST_VERSION", "3f7fb75992856cbc0792c7ee8ea4cb8ac7354ce7")
    if sc == "post_hash_swap":
        rel_root = pathlib.Path(os.environ["HUB_CANARY_HOST_RELEASE_ROOT"])
        (rel_root / "releases" / host_ver / "hub.pyz").write_text("import sys; sys.exit(99)\n")
    connected = sc != "host_disconnected"
    mismatch = sc == "host_version_mismatch_flag"
    print(json.dumps({"hosts": [{"machine_id": os.environ.get("HUB_CANARY_MACHINE", "HOST-1"), "connected": connected, "host_version": host_ver, "version_mismatch": mismatch, "protocol_version": 1}]}))
    sys.exit(0)
if args[:2] == ["sessions", "show"]:
    if sc == "archive_show_fail": sys.exit(1)
    print(json.dumps({"session": {"id": SID, "intake_id": "intake-canary-1"}})); sys.exit(0)
if args[:2] == ["intake", "update"]:
    if sc == "archive_update_fail": sys.exit(1)
    (d / "archived_intake").write_text(" ".join(args)); sys.exit(0)
if args[:2] == ["sessions", "list"]:
    if sc == "late_running": time.sleep(0.5)
    list_count = int((d / "list_count").read_text()) if (d / "list_count").exists() else 0
    (d / "list_count").write_text(str(list_count + 1))
    if sc == "transient_inventory_changed_409" and list_count == 0:
        sys.stderr.write("Session HTTP request failed with status 409 (native_inventory_changed)\n")
        sys.exit(1)
    if sc == "persistent_inventory_changed_409":
        sys.stderr.write("Session HTTP request failed with status 409 (native_inventory_changed)\n")
        sys.exit(1)
    if sc == "list_auth_401":
        sys.stderr.write("Session HTTP request failed with status 401 (unauthorized)\n")
        sys.exit(1)
    items = [item("failed", id="other", name="other-session", key="other-key", native_id=None, attention={"reason": "engine_not_allowed", "observed_at": 1})]
    if (d / "started").exists():
        if sc == "never_running": items.append(item("claimed", native_id=None, attention={"reason": "native_launch_unverified", "observed_at": 1}))
        elif sc == "failed_state": items.append(item("failed", native_id=None, attention={"reason": "engine_not_allowed", "observed_at": 1}))
        elif sc == "no_workspace": items.append(item("running", workspace=None))  # real server shape today: no workspace
        elif sc == "no_host": items.append(item("running", machine_id=None, workspace="/w/hub-canary"))
        elif sc == "wrong_host": items.append(item("running", machine_id="desktop"))
        elif sc == "wrong_workspace": items.append(item("running", workspace="/wrong/workspace"))
        elif sc == "same_name_unrelated_key":
            items.append(item("running", id="UNRELATED-SID", key="different-key", client_key="different-key", native_id="unrelated-native"))
        elif sc == "client_key_only_recovery":
            items.append(item("running", id="UNRELATED-SID", key="different-key", client_key="different-key", native_id="unrelated-native"))
            items.append(item("running", id=SID, key=None, client_key=(d / "session_key").read_text(), native_id=NATIVE))
        elif sc == "conflicting_key_rows":
            ck = (d / "session_key").read_text()
            items.append(item("running", id=SID, key=None, client_key=ck, native_id=NATIVE))
            items.append(item("running", id="CONFLICT-SID-2", key=None, client_key=ck, native_id="conflict-native-2"))
        else: items.append(item("running"))
    print(json.dumps({"items": items[:1] if sc == "past_first_page" else items, "next": None, "complete": True, "sessions": items, "next_cursor": None})); sys.exit(0)
if args[:2] == ["sessions", "capture"]:
    if sc == "capture_fail": sys.exit(1)
    if sc == "late_capture": time.sleep(0.5)
    prompt, marker = (d / "prompt").read_text(), (d / "marker").read_text()
    if sc == "capture_prompt_only": output = "$ " + prompt
    elif sc == "wrapped_prompt_only": output = "Reply CANARY-OK\nand exit"
    elif sc == "capture_empty": output = ""
    elif sc == "json_command_output": output = json.dumps({"type":"item.completed","item":{"type":"command_execution","aggregated_output":marker+"\n","command":"echo"}}) + "\n" + json.dumps({"type":"item.started","item":{"type":"command_execution","command":"sleep 60"}})
    elif sc == "json_prompt_only": output = json.dumps({"type":"item.started","item":{"type":"command_execution","command":"echo "+marker}})
    elif sc == "json_message_output": output = json.dumps({"type":"item.completed","item":{"type":"agent_message","text":marker}})
    elif sc == "json_result_output": output = json.dumps({"type":"result","result":marker})
    elif sc == "json_arbitrary_marker": output = json.dumps({"command":marker,"prompt":marker,"output":marker})
    else: output = "$ " + prompt + "\n" + marker
    print(json.dumps({"command_wait":{"state":"acked","ack":{"capture":output}}})); sys.exit(0)
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

FAKE_SYSTEMCTL = r'''#!%s
import json, os, pathlib, shutil, sys, time
d = pathlib.Path(os.environ["FAKE_DIR"])
rec = {"argv": sys.argv[1:], "xdg_runtime_dir": os.environ.get("XDG_RUNTIME_DIR")}
with open(d / "systemctl.jsonl", "a", encoding="utf-8") as fh:
    fh.write(json.dumps(rec) + "\n")
action = os.environ.get("FAKE_SYSTEMCTL_ACTION", "noop")
if action == "fail_exit_1":
    sys.stderr.write("Failed to start agent-hub2-host-update.service\n")
    sys.exit(1)
if action == "hang":
    time.sleep(5)
    sys.exit(0)
if action == "converge_from_staged":
    staged = pathlib.Path(os.environ["FAKE_STAGED_RELEASE_DIR"])
    rel_root = pathlib.Path(os.environ["HUB_CANARY_HOST_RELEASE_ROOT"])
    sha = staged.name
    dst = rel_root / "releases" / sha
    dst.parent.mkdir(parents=True, exist_ok=True)
    if not dst.exists():
        shutil.copytree(staged, dst)
    cur = rel_root / "current"
    if cur.exists() or cur.is_symlink():
        cur.unlink()
    cur.symlink_to(dst)
sys.exit(0)
''' % sys.executable


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
        fake_sctl = self.tmp / "bin" / "systemctl"
        fake_sctl.write_text(FAKE_SYSTEMCTL)
        fake_sctl.chmod(0o755)
        self.xdg_runtime = self.tmp / "xdg-runtime"
        self.xdg_runtime.mkdir(mode=0o700)
        os.chmod(self.xdg_runtime, 0o700)
        self.sink = self.tmp / "mail.sink"
        self.state = self.tmp / "state"

    def run_canary(self, scenario="ok", *args, mode_prompt=None, **extra):
        env = dict(os.environ)
        env.update(
            PATH=f"{self.tmp / 'bin'}:{env['PATH']}",
            XDG_RUNTIME_DIR=str(self.xdg_runtime),
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
        for f in ("started", "a_running", "stopped", "list_count", "systemctl.jsonl"):
            (self.tmp / f).unlink(missing_ok=True)
        (self.tmp / "calls.log").write_text("")
        r = subprocess.run([str(SCRIPT), *args], env=env, capture_output=True, text=True, timeout=60)
        verdict = json.loads(r.stdout.strip().splitlines()[-1])
        return r, verdict

    def calls(self):
        return (self.tmp / "calls.log").read_text()

    def systemctl_calls(self):
        p = self.tmp / "systemctl.jsonl"
        return [json.loads(line) for line in p.read_text().splitlines()] if p.exists() else []

    def test_actual_canary_argv_is_accepted_by_the_maintained_cli(self):
        import argparse
        sys.path.insert(0, str(ROOT / 'host'))
        from agent_hub_host import intake, session_control
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest='command')
        session_control.add_commands(sub)
        intake.add_commands(sub)
        for mode in ('shell', 'full'):
            result, _ = self.run_canary('ok', '--mode', mode)
            self.assertEqual(result.returncode, 0, result.stderr)
        calls = [json.loads(line) for line in (self.tmp / 'argv.jsonl').read_text().splitlines()]
        self.assertTrue(any(args[:2] == ['sessions', 'capture'] for args in calls))
        self.assertTrue(any(args[:2] == ['intake', 'update'] for args in calls))
        for args in calls:
            with self.subTest(argv=args[:3]):
                parser.parse_args(args)

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
        self.assertIn("sessions capture " + SID, c)
        self.assertEqual(r.stderr, "")

    def test_success_full_is_modelless(self):
        r, v = self.run_canary("ok", "--mode", "full")
        self.assertEqual(r.returncode, 0, r.stderr)
        c = self.calls()
        self.assertNotIn("--engine", c)
        self.assertNotIn("--model", c)
        self.assertIn("--prompt Use two sequential tool calls.", c)
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
        self.assertEqual(len(list((self.state / "pending").glob("*.json"))), 1)
        for p in (self.state / "pending").glob("*.json"):
            p.unlink()
        self.assert_stage("start_noid", "start", "no_session_id")
        self.assertEqual(len(list((self.state / "pending").glob("*.json"))), 1)

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
        self.assert_stage("capture_fail", "capture", "hub_capture_exit_1")

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

    def test_full_jsonl_capture_requires_actual_output_fields(self):
        for scenario in ('json_command_output', 'json_message_output', 'json_result_output'):
            with self.subTest(scenario=scenario):
                r, value = self.run_canary(scenario, '--mode', 'full')
                self.assertEqual((r.returncode, value['ok'], value['stage']), (0, True, None))
                prompt=(self.tmp/'prompt').read_text()
                self.assertIn('two sequential tool calls',prompt)
                self.assertIn('separate tool call to run sleep 60',prompt)
        for scenario in ('json_prompt_only', 'json_arbitrary_marker'):
            with self.subTest(scenario=scenario):
                self.assert_stage(scenario, 'capture', 'no_output_beyond_prompt', mode='full')

    def test_shell_capture_requires_output_line_not_command_text(self):
        # command line only, even with the marker inside it, must fail
        self.assert_stage("capture_prompt_only", "capture", "no_output_beyond_prompt")

    def prepare_candidate_release(self, sha, *, converge=True, tamper_pyz=False):
        root = self.tmp / "host-releases"
        rel = root / "releases" / sha
        rel.mkdir(parents=True, exist_ok=True)
        pyz = rel / "hub.pyz"
        pyz_bytes = (self.tmp / "fake_hub.py").read_bytes()
        pyz.write_bytes(pyz_bytes)
        import hashlib
        digest = hashlib.sha256(pyz_bytes).hexdigest()
        if tamper_pyz:
            pyz.write_bytes(pyz_bytes + b"\n# tampered\n")
        meta = {
            "host_version": sha,
            "protocol_version": 1,
            "schema_version": 1,
            "artifacts": {"hub.pyz": {"sha256": digest, "size": len(pyz_bytes)}},
        }
        (rel / "release.json").write_text(json.dumps(meta))
        current = root / "current"
        if current.exists() or current.is_symlink():
            current.unlink()
        if converge:
            current.symlink_to(rel)
        return root, digest

    def helper_sha256(self):
        out = subprocess.check_output(
            [sys.executable, str(ROOT / "scripts" / "hub_start_canary.py"), "--print-helper-sha256"],
            text=True,
        ).strip()
        self.assertRegex(out, r"^[0-9a-f]{64}$")
        return out

    def test_precommit_deploy_gate_binds_candidate_sha_run_token_helper_and_hub_pyz(self):
        sha = "3f7fb75992856cbc0792c7ee8ea4cb8ac7354ce7"
        token = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
        helper_sha = self.helper_sha256()
        rel_root, pyz_sha = self.prepare_candidate_release(sha)
        # Set HUB_BIN to a broken path to prove verify_candidate_host_release
        # switches self.hub to the verified immutable candidate hub.pyz without mutating ~/.local/bin/hub.
        r, v = self.run_canary(
            "ok", "--deploy-gate", "--mode", "shell",
            "--candidate-sha", sha, "--candidate-cli-sha256", pyz_sha,
            "--run-token", token, "--helper-sha256", helper_sha,
            HUB_BIN="/nonexistent/global-hub-must-not-be-used",
            HUB_CANARY_HOST_RELEASE_ROOT=str(rel_root),
            HUB_CANARY_CONVERGE_TIMEOUT_S="0.2",
            FAKE_HOST_VERSION=sha,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(v["ok"])
        self.assertEqual(v["deploy_gate"], True)
        self.assertEqual(v["mode"], "shell")
        self.assertEqual(v["candidate_sha"], sha)
        self.assertEqual(v["run_token"], token)
        self.assertEqual(v["helper_sha256"], helper_sha)
        self.assertEqual(v["host_version"], sha)
        self.assertEqual(v["hub_pyz_sha256"], pyz_sha)
        self.assertEqual(v["intake_archived"], True)
        self.assertEqual(
            (self.tmp / "archived_intake").read_text(),
            "intake update intake-canary-1 --status archived --reason deploy_gate_shell_canary",
        )
        prompt = (self.tmp / "prompt").read_text()
        self.assertNotIn(f"CANARY-OK:{sha[:12]}:{token}", prompt)
        self.assertIn(f"printf '%s:%s:%s\\n' CANARY-OK {sha[:12]} {token}; sleep 30", prompt)
        self.assertEqual(list((self.state / "pending").glob("*.json")), [])

    def test_precommit_deploy_gate_rejects_helper_digest_mismatch(self):
        sha = "3f7fb75992856cbc0792c7ee8ea4cb8ac7354ce7"
        token = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
        rel_root, pyz_sha = self.prepare_candidate_release(sha)
        r, v = self.run_canary(
            "ok", "--deploy-gate", "--mode", "shell",
            "--candidate-sha", sha, "--candidate-cli-sha256", pyz_sha,
            "--run-token", token, "--helper-sha256", "0" * 64,
            HUB_CANARY_HOST_RELEASE_ROOT=str(rel_root),
            HUB_CANARY_CONVERGE_TIMEOUT_S="0.2",
        )
        self.assertEqual(r.returncode, 1)
        self.assertEqual((v["ok"], v["stage"], v["reason"]), (False, "helper_version", "helper_sha256_mismatch"))
        self.assertEqual(self.calls(), "")
        self.assertFalse(self.state.exists())

    def test_precommit_deploy_gate_rejects_unconverged_or_tampered_host_release(self):
        sha = "3f7fb75992856cbc0792c7ee8ea4cb8ac7354ce7"
        token = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
        helper_sha = self.helper_sha256()
        rel_root, pyz_sha = self.prepare_candidate_release(sha, converge=False)
        r, v = self.run_canary(
            "ok", "--deploy-gate", "--mode", "shell",
            "--candidate-sha", sha, "--candidate-cli-sha256", pyz_sha,
            "--run-token", token, "--helper-sha256", helper_sha,
            HUB_CANARY_HOST_RELEASE_ROOT=str(rel_root),
            HUB_CANARY_CONVERGE_TIMEOUT_S="0.1",
        )
        self.assertEqual((r.returncode, v["stage"], v["reason"]), (1, "host_release", "timeout_current_release_not_converged"))

        rel_root, pyz_sha = self.prepare_candidate_release(sha, tamper_pyz=True)
        r, v = self.run_canary(
            "ok", "--deploy-gate", "--mode", "shell",
            "--candidate-sha", sha, "--candidate-cli-sha256", pyz_sha,
            "--run-token", token, "--helper-sha256", helper_sha,
            HUB_CANARY_HOST_RELEASE_ROOT=str(rel_root),
            HUB_CANARY_CONVERGE_TIMEOUT_S="0.1",
        )
        self.assertEqual((r.returncode, v["stage"], v["reason"]), (1, "host_release", "candidate_hub_pyz_digest_mismatch"))

        rel_root, pyz_sha = self.prepare_candidate_release(sha)
        r, v = self.run_canary(
            "ok", "--deploy-gate", "--mode", "shell",
            "--candidate-sha", sha, "--candidate-cli-sha256", "f" * 64,
            "--run-token", token, "--helper-sha256", helper_sha,
            HUB_CANARY_HOST_RELEASE_ROOT=str(rel_root),
            HUB_CANARY_CONVERGE_TIMEOUT_S="0.1",
        )
        self.assertEqual((r.returncode, v["stage"], v["reason"]), (1, "host_release", "candidate_hub_pyz_cli_sha_mismatch"))

        rel_root, pyz_sha = self.prepare_candidate_release(sha)
        r, v = self.run_canary(
            "ok", "--deploy-gate", "--mode", "shell",
            "--candidate-sha", sha, "--candidate-cli-sha256", pyz_sha,
            "--run-token", token, "--helper-sha256", helper_sha,
            HUB_CANARY_HOST_RELEASE_ROOT=str(rel_root),
            HUB_CANARY_CONVERGE_TIMEOUT_S="0.1",
            FAKE_HOST_VERSION="ad0b8a0b2276303c2b13762a614fe49a63514ae7",
        )
        self.assertEqual((r.returncode, v["stage"]), (1, "host_release"))
        self.assertIn("timeout_host_version_mismatch", v["reason"])

    def test_precommit_deploy_gate_executes_sealed_memfd_immune_to_post_hash_disk_swap(self):
        sha = "3f7fb75992856cbc0792c7ee8ea4cb8ac7354ce7"
        token = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
        helper_sha = self.helper_sha256()
        rel_root, pyz_sha = self.prepare_candidate_release(sha)
        r, v = self.run_canary(
            "post_hash_swap", "--deploy-gate", "--mode", "shell",
            "--candidate-sha", sha, "--candidate-cli-sha256", pyz_sha,
            "--run-token", token, "--helper-sha256", helper_sha,
            HUB_BIN="/nonexistent/global-hub-must-not-be-used",
            HUB_CANARY_HOST_RELEASE_ROOT=str(rel_root),
            HUB_CANARY_CONVERGE_TIMEOUT_S="0.2",
            FAKE_HOST_VERSION=sha,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(v["ok"])
        self.assertEqual(v["hub_pyz_sha256"], pyz_sha)
        # Confirm that the on-disk file was indeed overwritten during routing-machines,
        # yet all subsequent CLI invocations still executed the sealed memfd bytes.
        self.assertEqual(
            (rel_root / "releases" / sha / "hub.pyz").read_text(),
            "import sys; sys.exit(99)\n",
        )

    def test_precommit_deploy_gate_fails_when_intake_archival_fails(self):
        sha = "3f7fb75992856cbc0792c7ee8ea4cb8ac7354ce7"
        token = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
        helper_sha = self.helper_sha256()
        rel_root, pyz_sha = self.prepare_candidate_release(sha)
        r, v = self.run_canary(
            "archive_update_fail", "--deploy-gate", "--mode", "shell",
            "--candidate-sha", sha, "--candidate-cli-sha256", pyz_sha,
            "--run-token", token, "--helper-sha256", helper_sha,
            HUB_CANARY_HOST_RELEASE_ROOT=str(rel_root),
            HUB_CANARY_CONVERGE_TIMEOUT_S="0.2",
            FAKE_HOST_VERSION=sha,
        )
        self.assertEqual((r.returncode, v["ok"], v["stage"], v["reason"], v["intake_archived"]),
                         (1, False, "cleanup", "intake_archive_failed", False))

    def test_precommit_deploy_gate_rejects_symlink_or_nonprivate_state_dir_and_symlink_lock_without_truncation(self):
        sha = "3f7fb75992856cbc0792c7ee8ea4cb8ac7354ce7"
        token = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
        helper_sha = self.helper_sha256()
        rel_root, pyz_sha = self.prepare_candidate_release(sha)

        victim_dir = self.tmp / "victim-dir"
        victim_dir.mkdir(mode=0o700)
        symlink_state = self.tmp / "symlink-state"
        symlink_state.symlink_to(victim_dir)
        r_sym, v_sym = self.run_canary(
            "ok", "--deploy-gate", "--mode", "shell",
            "--candidate-sha", sha, "--candidate-cli-sha256", pyz_sha,
            "--run-token", token, "--helper-sha256", helper_sha,
            HUB_CANARY_STATE_DIR=str(symlink_state),
            HUB_CANARY_HOST_RELEASE_ROOT=str(rel_root),
        )
        self.assertEqual((r_sym.returncode, v_sym["stage"], v_sym["reason"]), (1, "lock", "state_dir_symlink"))

        open_state = self.tmp / "open-state"
        open_state.mkdir(mode=0o755)
        os.chmod(open_state, 0o755)
        r_open, v_open = self.run_canary(
            "ok", "--deploy-gate", "--mode", "shell",
            "--candidate-sha", sha, "--candidate-cli-sha256", pyz_sha,
            "--run-token", token, "--helper-sha256", helper_sha,
            HUB_CANARY_STATE_DIR=str(open_state),
            HUB_CANARY_HOST_RELEASE_ROOT=str(rel_root),
        )
        self.assertEqual((r_open.returncode, v_open["stage"], v_open["reason"]), (1, "lock", "state_dir_not_private"))

        priv_state = self.tmp / "priv-state"
        priv_state.mkdir(mode=0o700)
        os.chmod(priv_state, 0o700)
        victim_file = self.tmp / "victim.txt"
        victim_file.write_text("DO-NOT-TRUNCATE")
        (priv_state / "deploy-gate.lock").symlink_to(victim_file)
        r_lock_sym, v_lock_sym = self.run_canary(
            "ok", "--deploy-gate", "--mode", "shell",
            "--candidate-sha", sha, "--candidate-cli-sha256", pyz_sha,
            "--run-token", token, "--helper-sha256", helper_sha,
            HUB_CANARY_STATE_DIR=str(priv_state),
            HUB_CANARY_HOST_RELEASE_ROOT=str(rel_root),
        )
        self.assertEqual((r_lock_sym.returncode, v_lock_sym["stage"], v_lock_sym["reason"]), (1, "lock", "lock_file_symlink"))
        self.assertEqual(victim_file.read_text(), "DO-NOT-TRUNCATE")

        (priv_state / "deploy-gate.lock").unlink()
        (priv_state / "deploy-gate.lock").write_text("KEEP-BYTES")
        os.chmod(priv_state / "deploy-gate.lock", 0o600)
        r_preserve, v_preserve = self.run_canary(
            "ok", "--deploy-gate", "--mode", "shell",
            "--candidate-sha", sha, "--candidate-cli-sha256", pyz_sha,
            "--run-token", token, "--helper-sha256", helper_sha,
            HUB_CANARY_STATE_DIR=str(priv_state),
            HUB_CANARY_HOST_RELEASE_ROOT=str(rel_root),
            HUB_CANARY_CONVERGE_TIMEOUT_S="0.2",
            FAKE_HOST_VERSION=sha,
        )
        self.assertEqual((r_preserve.returncode, v_preserve["ok"]), (0, True))
        self.assertEqual((priv_state / "deploy-gate.lock").read_text(), "KEEP-BYTES")

    def test_sigterm_during_start_triggers_reconciliation_stop_and_intake_archive(self):
        sha = "3f7fb75992856cbc0792c7ee8ea4cb8ac7354ce7"
        token = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
        helper_sha = self.helper_sha256()
        rel_root, pyz_sha = self.prepare_candidate_release(sha)
        r, v = self.run_canary(
            "sigterm_on_start", "--deploy-gate", "--mode", "shell",
            "--candidate-sha", sha, "--candidate-cli-sha256", pyz_sha,
            "--run-token", token, "--helper-sha256", helper_sha,
            HUB_CANARY_HOST_RELEASE_ROOT=str(rel_root),
            HUB_CANARY_CONVERGE_TIMEOUT_S="0.2",
            FAKE_HOST_VERSION=sha,
        )
        self.assertEqual(r.returncode, 1)
        self.assertEqual((v["ok"], v["stage"], v["reason"]), (False, "start", "terminated_sigterm"))
        self.assertEqual(v["session_id"], SID)
        self.assertEqual(v["cleanup"], "stopped")
        self.assertEqual(v["intake_archived"], True)
        self.assertFalse((self.tmp / "a_running").exists())
        self.assertEqual(list((self.state / "pending").glob("*.json")), [])

    def test_prior_unreconciled_pending_start_is_cleaned_up_on_next_run(self):
        sha = "3f7fb75992856cbc0792c7ee8ea4cb8ac7354ce7"
        token = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
        helper_sha = self.helper_sha256()
        rel_root, pyz_sha = self.prepare_candidate_release(sha)
        self.state.mkdir(mode=0o700, exist_ok=True)
        os.chmod(self.state, 0o700)
        pending_dir = self.state / "pending"
        pending_dir.mkdir(mode=0o700, exist_ok=True)
        os.chmod(pending_dir, 0o700)
        prior_file = pending_dir / "prior-sigkilled.json"
        prior_file.write_text(
            json.dumps(
                {
                    "run_id": "prior-sigkilled",
                    "mode": "shell",
                    "name": "hub-canary-shell-prior-sigkilled",
                    "key": "canary-prior-sigkilled",
                    "repo": "owner/repo",
                    "machine": "HOST-1",
                    "workspace": str((self.tmp / "ws").resolve()),
                    "prompt": "printf CANARY-OK; sleep 30",
                    "candidate_sha": sha,
                    "session_id": SID,
                    "native_id": NATIVE,
                }
            )
        )
        os.chmod(prior_file, 0o600)
        r, v = self.run_canary(
            "ok", "--deploy-gate", "--mode", "shell",
            "--candidate-sha", sha, "--candidate-cli-sha256", pyz_sha,
            "--run-token", token, "--helper-sha256", helper_sha,
            HUB_CANARY_HOST_RELEASE_ROOT=str(rel_root),
            HUB_CANARY_CONVERGE_TIMEOUT_S="0.2",
            FAKE_HOST_VERSION=sha,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(v["ok"])
        self.assertFalse(prior_file.exists())
        self.assertIn("sessions stop " + SID + " --key canary-prior-sigkilled-reconcile", self.calls())

    def test_forced_command_wrapper_validates_ssh_original_command_purges_env_and_locks(self):
        sha = "3f7fb75992856cbc0792c7ee8ea4cb8ac7354ce7"
        token = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
        helper_sha = self.helper_sha256()
        rel_root, pyz_sha = self.prepare_candidate_release(sha)
        lib = self.tmp / "canary-lib"
        lib.mkdir()
        for name in ("hub-deploy-gate-forced.sh", "hub-start-canary.sh", "hub_start_canary.py"):
            dst = lib / name
            shutil.copy2(ROOT / "scripts" / name, dst)
            dst.chmod(0o755)
        # B4: Create a poisoned user site-packages directory under USER_HOME
        # to prove python3 -I -S never imports user site-packages or .pth files.
        pyver = f"python{sys.version_info.major}.{sys.version_info.minor}"
        user_site = self.tmp / ".local" / "lib" / pyver / "site-packages"
        user_site.mkdir(parents=True, exist_ok=True)
        poison_marker = self.tmp / "POISONED_USER_SITE_IMPORTED"
        poison_code = f"import pathlib, sys; pathlib.Path({str(poison_marker)!r}).write_text('pwned'); sys.exit(88)\n"
        (user_site / "sitecustomize.py").write_text(poison_code)
        (user_site / "usercustomize.py").write_text(poison_code)
        (user_site / "evil.pth").write_text(f"import pathlib, sys; pathlib.Path({str(poison_marker)!r}).write_text('pth'); sys.exit(88)\n")

        (lib / ".test-harness.env").write_text(
            f'USER_HOME="{self.tmp}"\n'
            f'A_BIN="{sys.executable} -I -S {self.tmp / "fake_a.py"}"\n'
            f'HOST_RELEASE_ROOT="{rel_root}"\n'
            f'WORKSPACE_DIR="{self.tmp / "ws"}"\n'
            f'STATE_DIR="{self.state}"\n'
            f'DEPLOY_GATE_LOCK_WAIT_S="0"\n'
            f'EXTRA_ENV=(FAKE_DIR="{self.tmp}" FAKE_SCENARIO="ok" FAKE_HOST_VERSION="{sha}" '
            f'HUB_CANARY_MACHINE="HOST-1" HUB_CANARY_REPO="owner/repo" '
            f'HUB_CANARY_POLL_S="0.02" HUB_CANARY_CONVERGE_TIMEOUT_S="0.2" '
            f'HUB_CANARY_RUNNING_TIMEOUT_S="0.3" HUB_CANARY_CAPTURE_TIMEOUT_S="0.2" '
            f'HUB_CANARY_STOP_TIMEOUT_S="0.3")\n'
        )
        forced = str(lib / "hub-deploy-gate-forced.sh")
        for bad_cmd in (
            "",
            "hub-deploy-gate",
            f"hub-deploy-gate --candidate-sha {sha} --run-token {token}",
            f"hub-deploy-gate --candidate-sha {sha} --run-token {token} --helper-sha256 {helper_sha}",
            f"hub-deploy-gate --candidate-sha {sha} --candidate-cli-sha256 {pyz_sha} --run-token {token} --helper-sha256 {helper_sha}; id",
            f"hub-deploy-gate --candidate-sha NOT_HEX --candidate-cli-sha256 {pyz_sha} --run-token {token} --helper-sha256 {helper_sha}",
        ):
            r = subprocess.run([forced], env={"SSH_ORIGINAL_COMMAND": bad_cmd}, capture_output=True, text=True, timeout=10)
            self.assertEqual(r.returncode, 2, bad_cmd)
            v = json.loads(r.stdout.strip())
            self.assertEqual((v["ok"], v["stage"]), (False, "forced_command"))

        valid_cmd = (
            f"hub-deploy-gate --candidate-sha {sha} --candidate-cli-sha256 {pyz_sha} "
            f"--run-token {token} --helper-sha256 {helper_sha}"
        )
        r_pos = subprocess.run(
            [forced, "extra"],
            env={"SSH_ORIGINAL_COMMAND": valid_cmd},
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(r_pos.returncode, 2)

        # Pass poisoned caller HUB_BIN, PYTHONPATH, PYTHONHOME, andDEPLOY_GATE_TIMEOUT_S=0;
        # fixed defaults + env -i + python3 -I -S must ignore all of them.
        r_ok = subprocess.run(
            [forced],
            env={
                "SSH_ORIGINAL_COMMAND": valid_cmd,
                "HUB_BIN": "/nonexistent/poisoned-by-caller",
                "PYTHONPATH": str(user_site),
                "DEPLOY_GATE_TIMEOUT_S": "0",
            },
            capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(r_ok.returncode, 0, r_ok.stderr)
        self.assertFalse(poison_marker.exists())
        verdict = json.loads(r_ok.stdout.strip())
        self.assertEqual(verdict["ok"], True)
        self.assertEqual(verdict["candidate_sha"], sha)
        self.assertEqual(verdict["run_token"], token)
        self.assertEqual(verdict["helper_sha256"], helper_sha)
        self.assertEqual(verdict["hub_pyz_sha256"], pyz_sha)
        self.assertEqual(verdict["intake_archived"], True)

        # Hold the deploy-gate lock and verify concurrent forced-command execution fails closed.
        import fcntl
        lock_fd = os.open(self.state / "deploy-gate.lock", os.O_RDWR)
        self.addCleanup(os.close, lock_fd)
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        r_locked = subprocess.run([forced], env={"SSH_ORIGINAL_COMMAND": valid_cmd}, capture_output=True, text=True, timeout=10)
        self.assertEqual(r_locked.returncode, 1)
        v_locked = json.loads(r_locked.stdout.strip())
        self.assertEqual((v_locked["ok"], v_locked["stage"], v_locked["reason"]), (False, "lock", "deploy_gate_lock_busy"))

    def test_r1_cleanup_and_prior_reconcile_never_match_by_name_or_borrow_unrelated_native_id_and_reject_conflicts(self):
        from scripts import hub_start_canary as mod

        sha = "3f7fb75992856cbc0792c7ee8ea4cb8ac7354ce7"
        token = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
        helper_sha = self.helper_sha256()
        rel_root, pyz_sha = self.prepare_candidate_release(sha)
        ws = str((self.tmp / "ws").resolve())

        # 1. Same name, different key + failed key replay -> must NEVER stop or archive UNRELATED-SID.
        r_name, v_name = self.run_canary(
            "same_name_unrelated_key", "--deploy-gate", "--mode", "shell",
            "--candidate-sha", sha, "--candidate-cli-sha256", pyz_sha,
            "--run-token", token, "--helper-sha256", helper_sha,
            HUB_CANARY_HOST_RELEASE_ROOT=str(rel_root),
            HUB_CANARY_CONVERGE_TIMEOUT_S="0.2",
            FAKE_HOST_VERSION=sha,
        )
        self.assertEqual(r_name.returncode, 1)
        self.assertEqual(v_name["session_id"], None)
        self.assertEqual(v_name["cleanup"], "FAILED_needs_reconciliation")
        self.assertEqual(v_name["intake_archived"], False)
        self.assertNotIn("sessions stop", self.calls())
        self.assertNotIn("intake update", self.calls())
        # Pending file remains on disk because cleanup could not reconcile.
        for pfile in (self.state / "pending").glob("*.json"):
            pfile.unlink()

        # 2. Unambiguous client_key alias + matching project/machine/workspace -> recovers SID, stops, archives, clears pending.
        (self.tmp / "archived_intake").unlink(missing_ok=True)
        r_ck, v_ck = self.run_canary(
            "client_key_only_recovery", "--deploy-gate", "--mode", "shell",
            "--candidate-sha", sha, "--candidate-cli-sha256", pyz_sha,
            "--run-token", token, "--helper-sha256", helper_sha,
            HUB_CANARY_HOST_RELEASE_ROOT=str(rel_root),
            HUB_CANARY_CONVERGE_TIMEOUT_S="0.2",
            FAKE_HOST_VERSION=sha,
        )
        self.assertEqual(r_ck.returncode, 1)
        self.assertEqual(v_ck["session_id"], SID)
        self.assertEqual(v_ck["cleanup"], "stopped")
        self.assertEqual(v_ck["intake_archived"], True)
        self.assertIn("sessions stop " + SID, self.calls())
        self.assertNotIn("UNRELATED-SID", self.calls())
        self.assertEqual(list((self.state / "pending").glob("*.json")), [])

        # 3. Conflicting client_key rows (two distinct SIDs for the same key) -> stays FAILED_needs_reconciliation, no stop/archive.
        (self.tmp / "archived_intake").unlink(missing_ok=True)
        r_conf, v_conf = self.run_canary(
            "conflicting_key_rows", "--deploy-gate", "--mode", "shell",
            "--candidate-sha", sha, "--candidate-cli-sha256", pyz_sha,
            "--run-token", token, "--helper-sha256", helper_sha,
            HUB_CANARY_HOST_RELEASE_ROOT=str(rel_root),
            HUB_CANARY_CONVERGE_TIMEOUT_S="0.2",
            FAKE_HOST_VERSION=sha,
        )
        self.assertEqual(r_conf.returncode, 1)
        self.assertEqual(v_conf["session_id"], None)
        self.assertEqual(v_conf["cleanup"], "FAILED_needs_reconciliation")
        self.assertEqual(v_conf["intake_archived"], False)
        self.assertNotIn("sessions stop", self.calls())
        self.assertNotIn("intake update", self.calls())
        for pfile in (self.state / "pending").glob("*.json"):
            pfile.unlink()

        # 4. Direct unit verification of resolve_owned_session:
        # Known SID never borrows native_id from an earlier same-name row and rejects conflicting rows.
        items_borrow_test = [
            {
                "id": "UNRELATED-SID",
                "name": "same-name",
                "key": "other-key",
                "project_id": "owner/repo",
                "machine_id": "HOST-1",
                "workspace": ws,
                "native_id": "unrelated-native-must-not-be-borrowed",
            },
            {
                "id": SID,
                "name": "same-name",
                "client_key": "expected-key",
                "project_id": "owner/repo",
                "machine_id": "HOST-1",
                "workspace": ws,
                "native_id": NATIVE,
            },
        ]
        res_sid, res_nat, conflict = mod.Canary.resolve_owned_session(
            items_borrow_test,
            expected_sid=SID,
            expected_key="expected-key",
            expected_repo="owner/repo",
            expected_machine="HOST-1",
            expected_workspace=ws,
        )
        self.assertEqual((res_sid, res_nat, conflict), (SID, NATIVE, False))

        # Known SID with another row claiming the same key -> conflict.
        items_key_hijack = [
            {
                "id": "HIJACK-SID",
                "client_key": "expected-key",
                "project_id": "owner/repo",
                "machine_id": "HOST-1",
                "workspace": ws,
                "native_id": "hijack-native",
            },
            {
                "id": SID,
                "client_key": "expected-key",
                "project_id": "owner/repo",
                "machine_id": "HOST-1",
                "workspace": ws,
                "native_id": NATIVE,
            },
        ]
        self.assertEqual(
            mod.Canary.resolve_owned_session(
                items_key_hijack,
                expected_sid=SID,
                expected_key="expected-key",
                expected_repo="owner/repo",
                expected_machine="HOST-1",
                expected_workspace=ws,
            ),
            (None, None, True),
        )

        # Unknown SID with mismatched scope (wrong workspace or machine or project) -> conflict.
        for bad_field in (
            {"project_id": "wrong/repo"},
            {"machine_id": "WRONG-HOST"},
            {"workspace": str(self.tmp / "wrong-ws")},
            {"key": "disagreeing-key", "client_key": "expected-key"},
        ):
            row = {
                "id": SID,
                "client_key": "expected-key",
                "project_id": "owner/repo",
                "machine_id": "HOST-1",
                "workspace": ws,
                "native_id": NATIVE,
            }
            row.update(bad_field)
            self.assertEqual(
                mod.Canary.resolve_owned_session(
                    [row],
                    expected_sid=None,
                    expected_key="expected-key",
                    expected_repo="owner/repo",
                    expected_machine="HOST-1",
                    expected_workspace=ws,
                ),
                (None, None, True),
                bad_field,
            )

    def test_r2_directory_fsync_and_durable_pending_publish_before_post_and_durable_unlink(self):
        import errno
        from unittest import mock
        from scripts import hub_start_canary as mod

        sha = "3f7fb75992856cbc0792c7ee8ea4cb8ac7354ce7"
        token = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
        helper_sha = self.helper_sha256()
        rel_root, pyz_sha = self.prepare_candidate_release(sha)

        fsynced_dirs = []
        real_fsync_dir = mod.fsync_dir

        def record_fsync_dir(path):
            fsynced_dirs.append(pathlib.Path(path).resolve())
            return real_fsync_dir(path)

        fresh_state = self.tmp / "r2-state" / "nested"
        fresh_ws = self.tmp / "r2-ws"
        env_patch = {
            "HUB_CANARY_STATE_DIR": str(fresh_state),
            "HUB_CANARY_WORKSPACE": str(fresh_ws),
            "HUB_CANARY_HOST_RELEASE_ROOT": str(rel_root),
            "HUB_CANARY_MACHINE": "HOST-1",
            "HUB_CANARY_REPO": "owner/repo",
            "HUB_CANARY_POLL_S": "0.02",
            "HUB_CANARY_CONVERGE_TIMEOUT_S": "0.2",
            "HUB_CANARY_RUNNING_TIMEOUT_S": "0.3",
            "HUB_CANARY_CAPTURE_TIMEOUT_S": "0.2",
            "HUB_CANARY_STOP_TIMEOUT_S": "0.3",
            "A_BIN": f"{sys.executable} {self.tmp / 'fake_a.py'}",
            "FAKE_DIR": str(self.tmp),
            "FAKE_SCENARIO": "ok",
            "FAKE_HOST_VERSION": sha,
        }

        with mock.patch.dict(os.environ, env_patch), mock.patch.object(mod, "fsync_dir", side_effect=record_fsync_dir):
            canary = mod.Canary(
                "shell",
                f"{sha[:12]}-{token[:12]}",
                candidate_sha=sha,
                candidate_cli_sha256=pyz_sha,
                run_token=token,
                expected_helper_sha256=helper_sha,
            )
            orig_run = canary.run
            pending_observed_before_post = []

            def check_pending_before_post(argv, *a, **kw):
                if argv[4:6] == ["sessions", "start"]:
                    # At the moment POST (sessions start) is invoked, the pending file
                    # must already exist on disk and pending_dir must already be fsynced.
                    pending_observed_before_post.append(
                        (
                            canary.pending_path.is_file(),
                            (fresh_state / "pending").resolve() in fsynced_dirs,
                        )
                    )
                return orig_run(argv, *a, **kw)

            with mock.patch.object(canary, "run", side_effect=check_pending_before_post):
                verdict = canary.execute()

        self.assertTrue(verdict["ok"], verdict)
        self.assertEqual(pending_observed_before_post, [(True, True)])
        self.assertIn(fresh_state.parent.resolve(), fsynced_dirs)
        self.assertIn(fresh_state.resolve(), fsynced_dirs)
        self.assertIn((fresh_state / "pending").resolve(), fsynced_dirs)
        self.assertIn(fresh_ws.resolve(), fsynced_dirs)
        self.assertFalse(canary.pending_path.exists())

        # Pre-POST fsync failure on pending write must abort BEFORE invoking sessions start.
        (self.tmp / "calls.log").write_text("")
        with mock.patch.dict(os.environ, env_patch):
            canary_fail_pre = mod.Canary(
                "shell",
                f"{sha[:12]}-{token[:12]}",
                candidate_sha=sha,
                candidate_cli_sha256=pyz_sha,
                run_token=token,
                expected_helper_sha256=helper_sha,
            )
            with mock.patch.object(
                canary_fail_pre,
                "write_pending_state",
                side_effect=OSError(errno.EIO, "simulated pre-post dir fsync failure"),
            ):
                verdict_pre = canary_fail_pre.execute()
        self.assertEqual(
            (verdict_pre["ok"], verdict_pre["stage"], verdict_pre["reason"], verdict_pre["cleanup"]),
            (False, "start", "pending_state_write_failed", "not_needed"),
        )
        self.assertFalse(canary_fail_pre.start_attempted)
        self.assertNotIn("sessions start", self.calls())

        # Directory fsync failure during durable_unlink at cleanup must fail closed.
        with mock.patch.dict(os.environ, env_patch):
            canary_fail_unlink = mod.Canary(
                "shell",
                f"{sha[:12]}-{token[:12]}",
                candidate_sha=sha,
                candidate_cli_sha256=pyz_sha,
                run_token=token,
                expected_helper_sha256=helper_sha,
            )
            with mock.patch.object(
                mod,
                "durable_unlink",
                side_effect=OSError(errno.EIO, "simulated unlink dir fsync failure"),
            ):
                verdict_unlink = canary_fail_unlink.execute()
        self.assertEqual(
            (verdict_unlink["ok"], verdict_unlink["stage"], verdict_unlink["reason"], verdict_unlink["cleanup"]),
            (False, "cleanup", "pending_state_clear_failed", "FAILED_needs_reconciliation"),
        )

        # R2 retry durability: if Attempt 1 fails during parent/ancestor fsync_dir AFTER
        # os.mkdir has already created the target directory and its ancestors on disk,
        # Attempt 2 (hitting the existing-path branch of ensure_private_dir) must still
        # repair/validate durability across the existing directory and ancestor chain
        # before POST (and fail closed before POST if fsync_dir still fails on retry).
        retry_state = self.tmp / "r2-retry-state" / "nested" / "sub"
        retry_ws = self.tmp / "r2-retry-ws"
        retry_env = dict(env_patch, HUB_CANARY_STATE_DIR=str(retry_state), HUB_CANARY_WORKSPACE=str(retry_ws))
        (self.tmp / "calls.log").write_text("")

        def fail_on_parent_fsync_attempt1(path):
            resolved = pathlib.Path(path).resolve()
            if resolved == retry_state.parent.resolve() and retry_state.exists():
                raise OSError(errno.EIO, "simulated parent dir fsync failure after mkdir")
            return real_fsync_dir(path)

        with mock.patch.dict(os.environ, retry_env), mock.patch.object(
            mod, "fsync_dir", side_effect=fail_on_parent_fsync_attempt1
        ):
            canary_retry_1 = mod.Canary(
                "shell",
                f"{sha[:12]}-{token[:12]}",
                candidate_sha=sha,
                candidate_cli_sha256=pyz_sha,
                run_token=token,
                expected_helper_sha256=helper_sha,
            )
            verdict_retry_1 = canary_retry_1.execute()
        self.assertEqual(
            (verdict_retry_1["ok"], verdict_retry_1["stage"], verdict_retry_1["reason"], verdict_retry_1["cleanup"]),
            (False, "lock", "state_dir_create_failed", "not_needed"),
        )
        # The directory now already exists on disk from Attempt 1's os.mkdir, even though
        # its parent/ancestor fsync failed.
        self.assertTrue(retry_state.is_dir())
        self.assertNotIn("sessions start", self.calls())

        # Attempt 2a: existing path still fails fsync_dir -> must reject before POST.
        with mock.patch.dict(os.environ, retry_env), mock.patch.object(
            mod, "fsync_dir", side_effect=fail_on_parent_fsync_attempt1
        ):
            canary_retry_2a = mod.Canary(
                "shell",
                f"{sha[:12]}-{token[:12]}",
                candidate_sha=sha,
                candidate_cli_sha256=pyz_sha,
                run_token=token,
                expected_helper_sha256=helper_sha,
            )
            verdict_retry_2a = canary_retry_2a.execute()
        self.assertEqual(
            (verdict_retry_2a["ok"], verdict_retry_2a["stage"], verdict_retry_2a["reason"], verdict_retry_2a["cleanup"]),
            (False, "lock", "state_dir_create_failed", "not_needed"),
        )
        self.assertNotIn("sessions start", self.calls())

        # Attempt 2b: existing path retry succeeds -> must fsync the existing directory
        # and its ancestor leftovers BEFORE POST (sessions start).
        retry_fsynced_before_post = []
        retry_fsynced_now = []

        def record_retry_fsync(path):
            retry_fsynced_now.append(pathlib.Path(path).resolve())
            return real_fsync_dir(path)

        with mock.patch.dict(os.environ, retry_env), mock.patch.object(
            mod, "fsync_dir", side_effect=record_retry_fsync
        ):
            canary_retry_2b = mod.Canary(
                "shell",
                f"{sha[:12]}-{token[:12]}",
                candidate_sha=sha,
                candidate_cli_sha256=pyz_sha,
                run_token=token,
                expected_helper_sha256=helper_sha,
            )
            orig_run_2b = canary_retry_2b.run

            def capture_fsyncs_before_post(argv, *a, **kw):
                if argv[4:6] == ["sessions", "start"]:
                    retry_fsynced_before_post.extend(retry_fsynced_now)
                return orig_run_2b(argv, *a, **kw)

            with mock.patch.object(canary_retry_2b, "run", side_effect=capture_fsyncs_before_post):
                verdict_retry_2b = canary_retry_2b.execute()

        self.assertTrue(verdict_retry_2b["ok"], verdict_retry_2b)
        self.assertIn((self.tmp / "r2-retry-state").resolve(), retry_fsynced_before_post)
        self.assertIn(retry_state.parent.resolve(), retry_fsynced_before_post)
        self.assertIn(retry_state.resolve(), retry_fsynced_before_post)
        self.assertIn((retry_state / "pending").resolve(), retry_fsynced_before_post)
        self.assertIn(retry_ws.resolve(), retry_fsynced_before_post)

        # Real ancestor-symlink alias retry regression (no injected fsync_dir mock needed):
        # ensure_private_dir(alias/private) where alias -> real-parent must fail closed
        # before creating real-parent/private on Attempt 1, on retry Attempt 2, and even
        # if real-parent/private already exists on disk (Attempt 3), never invoking POST.
        real_parent = self.tmp / "real-parent"
        real_parent.mkdir(mode=0o700)
        alias_parent = self.tmp / "alias-parent"
        alias_parent.symlink_to(real_parent)
        alias_state = alias_parent / "private"
        alias_env = dict(env_patch, HUB_CANARY_STATE_DIR=str(alias_state), HUB_CANARY_WORKSPACE=str(retry_ws))
        (self.tmp / "calls.log").write_text("")

        with mock.patch.dict(os.environ, alias_env):
            verdict_alias_1 = mod.Canary(
                "shell",
                f"{sha[:12]}-{token[:12]}",
                candidate_sha=sha,
                candidate_cli_sha256=pyz_sha,
                run_token=token,
                expected_helper_sha256=helper_sha,
            ).execute()
            self.assertFalse((real_parent / "private").exists())
            verdict_alias_2 = mod.Canary(
                "shell",
                f"{sha[:12]}-{token[:12]}",
                candidate_sha=sha,
                candidate_cli_sha256=pyz_sha,
                run_token=token,
                expected_helper_sha256=helper_sha,
            ).execute()
            (real_parent / "private").mkdir(mode=0o700)
            verdict_alias_3 = mod.Canary(
                "shell",
                f"{sha[:12]}-{token[:12]}",
                candidate_sha=sha,
                candidate_cli_sha256=pyz_sha,
                run_token=token,
                expected_helper_sha256=helper_sha,
            ).execute()

        for verdict_alias in (verdict_alias_1, verdict_alias_2, verdict_alias_3):
            self.assertEqual(
                (verdict_alias["ok"], verdict_alias["stage"], verdict_alias["reason"], verdict_alias["cleanup"]),
                (False, "lock", "state_dir_symlink", "not_needed"),
            )
        self.assertNotIn("sessions start", self.calls())

    def test_running_retries_transient_native_inventory_changed_409_and_cleans_up_persistent_409(self):
        # 1. Transient first-list 409 (native_inventory_changed) followed by a valid
        # complete snapshot must succeed within the SAME running deadline, retaining
        # the exact session/key and issuing only ONE sessions start (POST).
        r_ok, v_ok = self.run_canary("transient_inventory_changed_409", "--mode", "shell", "--deploy-gate")
        self.assertEqual(r_ok.returncode, 0, r_ok.stderr)
        self.assertEqual(
            (v_ok["ok"], v_ok["stage"], v_ok["reason"], v_ok["cleanup"], v_ok["session_id"]),
            (True, None, None, "not_needed", SID),
        )
        calls_ok = self.calls().splitlines()
        self.assertEqual(sum(1 for line in calls_ok if line.startswith("sessions start ")), 1)
        self.assertEqual(sum(1 for line in calls_ok if line == "sessions list --all --json"), 2)

        # 2. Persistent 409 (native_inventory_changed) churn until running deadline
        # must time out at running/timeout_native_inventory_changed after retrying
        # fresh complete listings within the same deadline, never issuing a second POST,
        # and perform owned cleanup (stop + wait_gone).
        r_churn, v_churn = self.run_canary(
            "persistent_inventory_changed_409",
            "--mode",
            "shell",
            "--deploy-gate",
            HUB_CANARY_RUNNING_TIMEOUT_S="0.25",
            HUB_CANARY_POLL_S="0.01",
        )
        self.assertEqual(r_churn.returncode, 1)
        self.assertEqual(
            (v_churn["ok"], v_churn["stage"], v_churn["reason"], v_churn["cleanup"], v_churn["session_id"]),
            (False, "running", "timeout_native_inventory_changed", "stopped", SID),
        )
        calls_churn = self.calls().splitlines()
        self.assertEqual(sum(1 for line in calls_churn if line.startswith("sessions start ")), 1)
        self.assertGreaterEqual(sum(1 for line in calls_churn if line == "sessions list --all --json"), 2)
        self.assertTrue(
            any(line.startswith(f"sessions stop {SID} --key canary-{v_churn['run_id']}-cleanup") for line in calls_churn)
        )

        # 3. Non-409 failures (e.g. 401 unauthorized) must still fail immediately and
        # truthfully without retrying.
        r_auth, v_auth = self.run_canary("list_auth_401", "--mode", "shell", "--deploy-gate")
        self.assertEqual(r_auth.returncode, 1)
        self.assertEqual(
            (v_auth["ok"], v_auth["stage"], v_auth["reason"], v_auth["cleanup"]),
            (False, "running", "unauthorized", "stopped"),
        )
        calls_auth = self.calls().splitlines()
        self.assertEqual(sum(1 for line in calls_auth if line.startswith("sessions start ")), 1)
        self.assertEqual(sum(1 for line in calls_auth if line == "sessions list --all --json"), 1)

    def test_precommit_deploy_gate_triggers_maintained_updater_once_and_validates_xdg_runtime_dir(self):
        sha = "3f7fb75992856cbc0792c7ee8ea4cb8ac7354ce7"
        token = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
        helper_sha = self.helper_sha256()
        staged_root, pyz_sha = self.prepare_candidate_release(sha, converge=True)
        staged_rel = self.tmp / "staged-release" / sha
        shutil.copytree(staged_root / "releases" / sha, staged_rel)

        # Start with empty host-releases root so candidate release is not yet installed.
        empty_root = self.tmp / "empty-host-releases"
        empty_root.mkdir()
        r_conv, v_conv = self.run_canary(
            "ok", "--deploy-gate", "--mode", "shell",
            "--candidate-sha", sha, "--candidate-cli-sha256", pyz_sha,
            "--run-token", token, "--helper-sha256", helper_sha,
            HUB_BIN="/nonexistent/global-hub-must-not-be-used",
            HUB_CANARY_HOST_RELEASE_ROOT=str(empty_root),
            HUB_CANARY_CONVERGE_TIMEOUT_S="0.4",
            FAKE_HOST_VERSION=sha,
            FAKE_SYSTEMCTL_ACTION="converge_from_staged",
            FAKE_STAGED_RELEASE_DIR=str(staged_rel),
        )
        self.assertEqual(r_conv.returncode, 0, r_conv.stderr)
        self.assertTrue(v_conv["ok"])
        self.assertEqual(v_conv["host_updater_trigger"], "triggered")
        self.assertNotIn("host_updater_trigger_error", v_conv)
        sctl = self.systemctl_calls()
        self.assertEqual(len(sctl), 1)
        self.assertEqual(
            sctl[0]["argv"],
            ["--user", "start", "--no-block", "agent-hub2-host-update.service"],
        )
        self.assertEqual(sctl[0]["xdg_runtime_dir"], str(self.xdg_runtime))

        # Already converged on first check -> updater not triggered.
        r_already, v_already = self.run_canary(
            "ok", "--deploy-gate", "--mode", "shell",
            "--candidate-sha", sha, "--candidate-cli-sha256", pyz_sha,
            "--run-token", token, "--helper-sha256", helper_sha,
            HUB_CANARY_HOST_RELEASE_ROOT=str(empty_root),
            HUB_CANARY_CONVERGE_TIMEOUT_S="0.2",
            FAKE_HOST_VERSION=sha,
        )
        self.assertEqual(r_already.returncode, 0, r_already.stderr)
        self.assertEqual(v_already["host_updater_trigger"], "not_needed")
        self.assertEqual(self.systemctl_calls(), [])

        # Unconverged release + multiple poll loops -> updater still triggered AT MOST ONCE.
        unconv_root, _ = self.prepare_candidate_release(sha, converge=False)
        r_once, v_once = self.run_canary(
            "ok", "--deploy-gate", "--mode", "shell",
            "--candidate-sha", sha, "--candidate-cli-sha256", pyz_sha,
            "--run-token", token, "--helper-sha256", helper_sha,
            HUB_CANARY_HOST_RELEASE_ROOT=str(unconv_root),
            HUB_CANARY_CONVERGE_TIMEOUT_S="0.1",
            HUB_CANARY_POLL_S="0.01",
            FAKE_SYSTEMCTL_ACTION="noop",
        )
        self.assertEqual(r_once.returncode, 1)
        self.assertEqual(v_once["host_updater_trigger"], "triggered")
        self.assertEqual(len(self.systemctl_calls()), 1)

        # Reject symlink, non-private, or missing XDG_RUNTIME_DIR with typed trigger error and never call systemctl.
        sym_xdg = self.tmp / "sym-xdg"
        sym_xdg.symlink_to(self.xdg_runtime)
        r_sym, v_sym = self.run_canary(
            "ok", "--deploy-gate", "--mode", "shell",
            "--candidate-sha", sha, "--candidate-cli-sha256", pyz_sha,
            "--run-token", token, "--helper-sha256", helper_sha,
            HUB_CANARY_HOST_RELEASE_ROOT=str(unconv_root),
            HUB_CANARY_CONVERGE_TIMEOUT_S="0.05",
            XDG_RUNTIME_DIR=str(sym_xdg),
        )
        self.assertEqual(r_sym.returncode, 1)
        self.assertEqual(v_sym["host_updater_trigger"], "failed")
        self.assertEqual(v_sym["host_updater_trigger_error"], "host_updater_xdg_runtime_dir_symlink")
        self.assertIn("host_updater_xdg_runtime_dir_symlink", v_sym["reason"])
        self.assertEqual(self.systemctl_calls(), [])

        open_xdg = self.tmp / "open-xdg"
        open_xdg.mkdir(mode=0o755)
        os.chmod(open_xdg, 0o755)
        r_open, v_open = self.run_canary(
            "ok", "--deploy-gate", "--mode", "shell",
            "--candidate-sha", sha, "--candidate-cli-sha256", pyz_sha,
            "--run-token", token, "--helper-sha256", helper_sha,
            HUB_CANARY_HOST_RELEASE_ROOT=str(unconv_root),
            HUB_CANARY_CONVERGE_TIMEOUT_S="0.05",
            XDG_RUNTIME_DIR=str(open_xdg),
        )
        self.assertEqual(r_open.returncode, 1)
        self.assertEqual(v_open["host_updater_trigger"], "failed")
        self.assertEqual(v_open["host_updater_trigger_error"], "host_updater_xdg_runtime_dir_not_private")
        self.assertEqual(self.systemctl_calls(), [])

        # Non-zero systemctl exit records typed trigger error.
        r_fail, v_fail = self.run_canary(
            "ok", "--deploy-gate", "--mode", "shell",
            "--candidate-sha", sha, "--candidate-cli-sha256", pyz_sha,
            "--run-token", token, "--helper-sha256", helper_sha,
            HUB_CANARY_HOST_RELEASE_ROOT=str(unconv_root),
            HUB_CANARY_CONVERGE_TIMEOUT_S="0.05",
            FAKE_SYSTEMCTL_ACTION="fail_exit_1",
        )
        self.assertEqual(r_fail.returncode, 1)
        self.assertEqual(v_fail["host_updater_trigger"], "failed")
        self.assertEqual(v_fail["host_updater_trigger_error"], "host_updater_trigger_exit_1")
        self.assertIn("host_updater_trigger_exit_1", v_fail["reason"])

    def test_periodic_mode_serializes_pending_state_archives_intake_and_ignores_lock_busy_for_alerts(self):
        import fcntl

        r_ok, v_ok = self.run_canary("ok", "--mode", "shell")
        self.assertEqual(r_ok.returncode, 0, r_ok.stderr)
        self.assertTrue(v_ok["ok"])
        self.assertEqual(v_ok["intake_archived"], True)
        self.assertEqual(
            (self.tmp / "archived_intake").read_text(),
            "intake update intake-canary-1 --status archived --note periodic_shell_canary",
        )
        self.assertEqual(list((self.state / "pending").glob("*.json")), [])

        # Prior crashed periodic pending record with session_id is reconciled and archived on next periodic run.
        pending_dir = self.state / "pending"
        prior_periodic = pending_dir / "prior-periodic.json"
        prior_periodic.write_text(
            json.dumps(
                {
                    "run_id": "prior-periodic",
                    "mode": "full",
                    "name": "hub-canary-full-prior-periodic",
                    "key": "canary-prior-periodic",
                    "repo": "owner/repo",
                    "machine": "HOST-1",
                    "workspace": str((self.tmp / "ws").resolve()),
                    "prompt": "Reply CANARY-OK and exit",
                    "candidate_sha": None,
                    "session_id": SID,
                    "native_id": NATIVE,
                }
            )
        )
        os.chmod(prior_periodic, 0o600)
        r_rec, v_rec = self.run_canary("ok", "--mode", "shell")
        self.assertEqual(r_rec.returncode, 0, r_rec.stderr)
        self.assertTrue(v_rec["ok"])
        self.assertFalse(prior_periodic.exists())
        self.assertIn("sessions stop " + SID + " --key canary-prior-periodic-reconcile", self.calls())

        # Ambiguous prior periodic pending record (session_id=None, candidate_sha=None) must be RETAINED
        # and fail closed at stage=cleanup/prior_pending_reconciliation_failed BEFORE minting any new start
        # when same-key replay times out (rc=124) or returns unparseable output and list[] is empty.
        prior_ambiguous = pending_dir / "prior-ambiguous.json"
        prior_ambiguous.write_text(
            json.dumps(
                {
                    "run_id": "prior-ambiguous",
                    "mode": "shell",
                    "name": "hub-canary-shell-prior-ambiguous",
                    "key": "canary-prior-ambiguous",
                    "repo": "owner/repo",
                    "machine": "HOST-1",
                    "workspace": str((self.tmp / "ws").resolve()),
                    "prompt": "printf CANARY-OK; sleep 30",
                    "candidate_sha": None,
                    "session_id": None,
                    "native_id": None,
                }
            )
        )
        os.chmod(prior_ambiguous, 0o600)

        (self.tmp / "calls.log").write_text("")
        r_to, v_to = self.run_canary(
            "replay_timeout_empty_list", "--mode", "shell",
            HUB_CANARY_CMD_TIMEOUT_S="0.05",
            HUB_CANARY_STOP_TIMEOUT_S="0.2",
        )
        self.assertEqual(r_to.returncode, 1)
        self.assertEqual(
            (v_to["ok"], v_to["stage"], v_to["reason"], v_to["session_id"]),
            (False, "cleanup", "prior_pending_reconciliation_failed", None),
        )
        self.assertTrue(prior_ambiguous.exists())
        to_starts = [ln for ln in self.calls().splitlines() if ln.startswith("sessions start ")]
        self.assertEqual(len(to_starts), 1)
        self.assertIn("--key canary-prior-ambiguous", to_starts[0])

        (self.tmp / "calls.log").write_text("")
        r_garb, v_garb = self.run_canary("start_garbage", "--mode", "shell")
        self.assertEqual(r_garb.returncode, 1)
        self.assertEqual(
            (v_garb["ok"], v_garb["stage"], v_garb["reason"], v_garb["session_id"]),
            (False, "cleanup", "prior_pending_reconciliation_failed", None),
        )
        self.assertTrue(prior_ambiguous.exists())
        garb_starts = [ln for ln in self.calls().splitlines() if ln.startswith("sessions start ")]
        self.assertEqual(len(garb_starts), 1)
        self.assertIn("--key canary-prior-ambiguous", garb_starts[0])
        prior_ambiguous.unlink()

        # Prior periodic pending record with exact session_id whose stop fails must abort BEFORE any fresh start.
        prior_stop_fail = pending_dir / "prior-stop-fail.json"
        prior_stop_fail.write_text(
            json.dumps(
                {
                    "run_id": "prior-stop-fail",
                    "mode": "shell",
                    "name": "hub-canary-shell-prior-stop-fail",
                    "key": "canary-prior-stop-fail",
                    "repo": "owner/repo",
                    "machine": "HOST-1",
                    "workspace": str((self.tmp / "ws").resolve()),
                    "prompt": "printf CANARY-OK; sleep 30",
                    "candidate_sha": None,
                    "session_id": SID,
                    "native_id": NATIVE,
                }
            )
        )
        os.chmod(prior_stop_fail, 0o600)
        (self.tmp / "calls.log").write_text("")
        r_sf, v_sf = self.run_canary("stop_fail", "--mode", "shell")
        self.assertEqual(r_sf.returncode, 1)
        self.assertEqual(
            (v_sf["ok"], v_sf["stage"], v_sf["reason"], v_sf["session_id"]),
            (False, "cleanup", "prior_pending_reconciliation_failed", None),
        )
        self.assertTrue(prior_stop_fail.exists())
        sf_starts = [ln for ln in self.calls().splitlines() if ln.startswith("sessions start ")]
        self.assertEqual(sf_starts, [])
        prior_stop_fail.unlink()

        (self.state / "state-shell.json").unlink(missing_ok=True)
        if self.sink.exists():
            self.sink.unlink()

        # Lock contention on deploy-gate.lock in periodic mode must NOT increment failures or send CRITICAL mail.
        lock_fd = os.open(self.state / "deploy-gate.lock", os.O_RDWR)
        self.addCleanup(os.close, lock_fd)
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for _ in range(3):
            r_busy, v_busy = self.run_canary("ok", "--mode", "shell", HUB_CANARY_LOCK_WAIT_S="0.01")
            self.assertEqual(r_busy.returncode, 1)
            self.assertEqual((v_busy["ok"], v_busy["stage"], v_busy["reason"]), (False, "lock", "deploy_gate_lock_busy"))
        self.assertEqual(self.mails(), [])
        st = json.loads((self.state / "state-shell.json").read_text())
        self.assertEqual(st["failures"], 0)
        self.assertEqual(st["last_lock_busy"]["reason"], "deploy_gate_lock_busy")

    def test_periodic_alert_state_failures_emit_structured_json_without_traceback(self):
        self.state.mkdir(mode=0o700, exist_ok=True)
        os.chmod(self.state, 0o700)
        victim = self.tmp / "victim-lock"
        victim.write_text("keep")
        (self.state / "shell.lock").symlink_to(victim)

        r, v = self.run_canary("ok", "--mode", "shell")
        self.assertEqual(r.returncode, 1)
        self.assertEqual((v["ok"], v["stage"], v["reason"]), (False, "lock", "lock_file_symlink"))
        self.assertEqual(v["alert_state_error"], "lock:lock_file_symlink")
        self.assertNotIn("Traceback", r.stderr)
        err_verdict = json.loads(r.stderr.strip().splitlines()[-1])
        self.assertEqual(err_verdict["alert_state_error"], "lock:lock_file_symlink")


if __name__ == "__main__":
    unittest.main()
