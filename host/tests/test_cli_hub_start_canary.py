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
    eng = (d / "session_engine").read_text() if (d / "session_engine").exists() else os.environ.get("FAKE_ENGINE", "shell")
    base = {"id": SID, "state": state, "intake_id": None, "project_id": "owner/repo", "machine_id": "HOST-1", "workspace": os.environ["HUB_CANARY_WORKSPACE"],
            "epoch": 1, "operation_id": "op:0", "native_id": NATIVE, "engine": eng, "model": None,
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
    (d / "session_engine").write_text("shell" if "--engine" in args else os.environ.get("FAKE_ENGINE", "codex"))
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
    if (d / "show_payload.json").exists():
        print((d / "show_payload.json").read_text()); sys.exit(0)
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
    if sc in ("capture_fail", "capture_fail_stop_409_retired", "capture_fail_stop_409_still_running"):
        if sc == "capture_fail_stop_409_retired":
            (d / "a_running").unlink(missing_ok=True)
        sys.exit(1)
    if sc == "late_capture": time.sleep(0.5)
    prompt, marker = (d / "prompt").read_text(), (d / "marker").read_text()
    if (d / "capture_seq.json").exists():
        seq = json.loads((d / "capture_seq.json").read_text())
        idx = int((d / "capture_idx").read_text()) if (d / "capture_idx").exists() else 0
        (d / "capture_idx").write_text(str(idx + 1))
        raw = seq[min(idx, len(seq) - 1)]
        output = raw.replace("__MARKER__", marker)
    elif (d / "capture_output.txt").exists():
        output = (d / "capture_output.txt").read_text().replace("__MARKER__", marker)
    elif sc == "capture_64k_window":
        output = json.dumps({"type":"item.completed","item":{"type":"command_execution","status":"completed","exit_code":0,"aggregated_output":marker+"\n","command":"printf"}})
        output += "\n" + json.dumps({"type":"item.completed","item":{"type":"agent_message","text":"x"*8000}})
        output = output.encode()[-int(args[args.index("--max-bytes") + 1]):].decode()
    elif sc == "capture_prompt_only": output = "$ " + prompt
    elif sc == "wrapped_prompt_only": output = "Reply CANARY-OK\nand exit"
    elif sc == "capture_empty": output = ""
    elif sc == "json_command_output": output = json.dumps({"type":"item.completed","item":{"type":"command_execution","aggregated_output":marker+"\n","command":"echo"}}) + "\n" + json.dumps({"type":"item.started","item":{"type":"command_execution","command":"sleep 60"}})
    elif sc == "json_prompt_only": output = json.dumps({"type":"item.started","item":{"type":"command_execution","command":"echo "+marker}})
    elif sc == "json_message_output": output = json.dumps({"type":"item.completed","item":{"type":"agent_message","text":marker}})
    elif sc == "json_result_output": output = json.dumps({"type":"result","result":marker})
    elif sc == "json_arbitrary_marker": output = json.dumps({"command":marker,"prompt":marker,"output":marker})
    elif ((d / "session_engine").read_text() if (d / "session_engine").exists() else "shell") != "shell":
        output = json.dumps({"type":"item.completed","item":{"type":"command_execution","status":"completed","exit_code":0,"aggregated_output":marker+"\n","command":"echo"}})
    else: output = "$ " + prompt + "\n" + marker
    print(json.dumps({"command_wait":{"state":"acked","ack":{"capture":output}}})); sys.exit(0)
if args[:2] == ["sessions", "stop"]:
    if sc == "stop_fail" or (sc == "cleanup_fail" and "cleanup" in " ".join(args)): sys.exit(1)
    if sc in ("capture_fail_stop_409_retired", "capture_fail_stop_409_still_running") or (sc == "reconcile_stop_409" and "reconcile" in " ".join(args)):
        sys.stderr.write("hub error: HTTP 409 at http://127.0.0.1:8787/api/sessions/" + args[2] + "/stop: status 409 (session_not_running): session is completed-awaiting-review\n")
        sys.exit(1)
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
        running_timeout = (
            "0.3" if scenario == "start_slow"
            else "0.5" if scenario in ("never_running", "no_workspace", "no_host", "late_running")
            else "1.5"
        )
        capture_timeout = (
            "0.35" if scenario in (
                "capture_prompt_only", "wrapped_prompt_only", "capture_empty",
                "capture_fail", "json_prompt_only", "json_message_output",
                "json_result_output", "json_arbitrary_marker", "late_capture",
            )
            else "1.5"
        )
        stop_timeout = "0.35" if scenario in ("gone_fail", "late_gone") else "1.5"
        cleanup_budget = "0.2" if scenario in ("gone_fail", "late_gone") else "1.5"
        env = dict(os.environ)
        env.update(
            PATH=f"{self.tmp / 'bin'}:{env['PATH']}",
            XDG_RUNTIME_DIR=str(self.xdg_runtime),
            FAKE_DIR=str(self.tmp), FAKE_SCENARIO=scenario,
            HUB_BIN=f"{sys.executable} {self.tmp / 'fake_hub.py'}", A_BIN=f"{sys.executable} {self.tmp / 'fake_a.py'}",
            HUB_CANARY_MAILER=str(self.mailer), MAIL_SINK=str(self.sink),
            HUB_CANARY_STATE_DIR=str(self.state), HUB_CANARY_WORKSPACE=str(self.tmp / "ws"),
            HUB_CANARY_MACHINE="HOST-1", HUB_CANARY_REPO="owner/repo",
            HUB_CANARY_POLL_S="0.02", HUB_CANARY_CONVERGE_TIMEOUT_S="1.5",
            HUB_CANARY_RUNNING_TIMEOUT_S=running_timeout, HUB_CANARY_CAPTURE_TIMEOUT_S=capture_timeout,
            HUB_CANARY_STOP_TIMEOUT_S=stop_timeout, HUB_CANARY_CLEANUP_BUDGET_S=cleanup_budget,
            HUB_CANARY_CMD_TIMEOUT_S="10",
        )
        env["FAKE_PROMPT"] = mode_prompt or ("Reply CANARY-OK and exit" if ("full" in args or "--deploy-gate" in args) else "printf CANARY-OK; sleep 30")
        env.update(extra)
        for f in ("started", "a_running", "stopped", "list_count", "systemctl.jsonl", "session_engine", "capture_idx"):
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
        self.assertIn("--prompt If this runtime is Claude Code,", c)
        self.assertIn("For every other runtime, follow this unchanged sequence: Use two sequential shell tool calls.", c)
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

    def test_capture_requests_64k_and_preserves_proof_older_than_4k_tail(self):
        r, verdict = self.run_canary("capture_64k_window")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(verdict['ok'], verdict)
        calls = [json.loads(line) for line in (self.tmp / 'argv.jsonl').read_text().splitlines()]
        captures = [args for args in calls if args[:2] == ['sessions', 'capture']]
        self.assertTrue(captures)
        self.assertTrue(all(args[args.index('--max-bytes') + 1] == '65536' for args in captures))

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
                for p in (self.state / "pending").glob("*.json"):
                    p.unlink()
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
        r, value = self.run_canary('json_command_output', '--mode', 'full')
        self.assertEqual((r.returncode, value['ok'], value['stage']), (0, True, None))
        prompt = (self.tmp / 'prompt').read_text()
        claude_prompt, prompt = prompt.split('For every other runtime, follow this unchanged sequence: ', 1)
        self.assertIn('`run_in_background: true`', claude_prompt)
        self.assertIn('wait on that exact background task id', claude_prompt)
        self.assertIn('two sequential shell tool calls', prompt)
        self.assertIn('First run a separate foreground shell command that prints the concatenation of "CANARY-", "OK:", and', prompt)
        self.assertLess(
            prompt.index('First run a separate foreground shell command'),
            prompt.index('Immediately after the marker print finishes, run a separate foreground `sleep 60` command'),
        )
        self.assertIn('using the existing shell tool with a supported timeout sufficient for the 60-second hold so the orchestrator can observe and stop this session before any final prose or completion bookkeeping', prompt)
        self.assertIn('Do not chain the marker print with the hold in one command, do not use a background flag, do not include the marker or nonce in the hold command, do not echo proof or emit waiting prose, and do not search for or call any monitor, task-output, or status tools or poll in a loop', prompt)
        self.assertIn('Apart from mandatory Hub first-action/completion evidence files if required by the session wrapper', prompt)
        self.assertNotIn('background tool facility', prompt)
        self.assertNotIn('TaskOutput', prompt)
        self.assertNotIn('Monitor', prompt)
        for scenario in ('json_prompt_only', 'json_message_output', 'json_result_output', 'json_arbitrary_marker'):
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
            HUB_CANARY_CONVERGE_TIMEOUT_S="1.5",
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
            HUB_CANARY_CONVERGE_TIMEOUT_S="0.5",
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
            HUB_CANARY_CONVERGE_TIMEOUT_S="1.5",
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
            HUB_CANARY_CONVERGE_TIMEOUT_S="1.5",
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
            HUB_CANARY_CONVERGE_TIMEOUT_S="1.5",
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
            HUB_CANARY_CONVERGE_TIMEOUT_S="1.5",
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
            HUB_CANARY_CONVERGE_TIMEOUT_S="1.5",
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
            f'HUB_CANARY_POLL_S="0.02" HUB_CANARY_CONVERGE_TIMEOUT_S="1.5" '
            f'HUB_CANARY_RUNNING_TIMEOUT_S="1.5" HUB_CANARY_CAPTURE_TIMEOUT_S="1.5" '
            f'HUB_CANARY_STOP_TIMEOUT_S="1.5" HUB_CANARY_CLEANUP_BUDGET_S="1.5")\n'
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
            HUB_CANARY_CONVERGE_TIMEOUT_S="1.5",
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
            HUB_CANARY_CONVERGE_TIMEOUT_S="1.5",
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
            HUB_CANARY_CONVERGE_TIMEOUT_S="1.5",
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
            "HUB_CANARY_CONVERGE_TIMEOUT_S": "1.5",
            "HUB_CANARY_RUNNING_TIMEOUT_S": "1.5",
            "HUB_CANARY_CAPTURE_TIMEOUT_S": "1.5",
            "HUB_CANARY_STOP_TIMEOUT_S": "1.5",
            "HUB_CANARY_CLEANUP_BUDGET_S": "1.5",
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
            HUB_CANARY_RUNNING_TIMEOUT_S="0.6",
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
            HUB_CANARY_CONVERGE_TIMEOUT_S="1.5",
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
            HUB_CANARY_CONVERGE_TIMEOUT_S="1.5",
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
            HUB_CANARY_CONVERGE_TIMEOUT_S="0.4",
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

        # Observe the fake command's non-zero exit, rather than timing out its startup.
        r_fail, v_fail = self.run_canary(
            "ok", "--deploy-gate", "--mode", "shell",
            "--candidate-sha", sha, "--candidate-cli-sha256", pyz_sha,
            "--run-token", token, "--helper-sha256", helper_sha,
            HUB_CANARY_HOST_RELEASE_ROOT=str(unconv_root),
            HUB_CANARY_CONVERGE_TIMEOUT_S="0.3",
            FAKE_SYSTEMCTL_ACTION="fail_exit_1",
        )
        self.assertEqual(r_fail.returncode, 1)
        self.assertEqual(v_fail["host_updater_trigger"], "failed")
        self.assertEqual(v_fail["host_updater_trigger_error"], "host_updater_trigger_exit_1")
        self.assertIn("host_updater_trigger_exit_1", v_fail["reason"])
        self.assertEqual(len(self.systemctl_calls()), 1)

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
            HUB_CANARY_STOP_TIMEOUT_S="0.8",
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

    def test_session_not_running_409_requires_positive_retirement_or_stop_evidence(self):
        ws = str((self.tmp / "ws").resolve())

        def make_retired_show(
            *,
            state="completed-awaiting-review",
            native_exit_confirmed=True,
            containment_empty=True,
            source="aplexer_retirement",
            tag=f"task-hub2-{SID}",
            nid_ws=ws,
            exited_at_ms=1794410797372,
            created_at="2026-11-11T15:25:12.727Z",
            epoch=167,
            ev_epoch=167,
            operation_id="1ae2c72b-edf1-52dd-a22b-c8964afd4a8f:0",
            ev_operation_id="1ae2c72b-edf1-52dd-a22b-c8964afd4a8f",
            ev_attempt_operation_id="1ae2c72b-edf1-52dd-a22b-c8964afd4a8f:0",
            stop_confirmed=None,
            include_evidence=True,
            native_id=NATIVE,
        ):
            sess = {
                "id": SID,
                "state": state,
                "intake_id": "09717ebe-e742-4403-80a9-2c6d9cb2d845",
                "project_id": "owner/repo",
                "machine_id": "HOST-1",
                "host": "HOST-1",
                "machine": "HOST-1",
                "workspace": ws,
                "requested_workspace": ws,
                "epoch": epoch,
                "operation_id": operation_id,
                "native_id": native_id,
                "created_at": created_at,
                "updated_at": "2026-11-11T15:26:37.604Z",
            }
            if stop_confirmed is not None:
                sess["stop_confirmed"] = stop_confirmed
            if include_evidence:
                sess["evidence"] = {
                    "attempt_operation_id": ev_attempt_operation_id,
                    "epoch": ev_epoch,
                    "evidence": {
                        "containment_empty": containment_empty,
                        "launcher_state": "retired-finished",
                        "native_deadline": {"deadline_ms": 1794412512811, "exceeded": False},
                        "native_exit": {
                            "code": 0,
                            "exited_at_ms": exited_at_ms,
                            "oom_killed": False,
                            "signal": None,
                        },
                        "native_identity": {
                            "id": native_id,
                            "tag": tag,
                            "workspace": nid_ws,
                        },
                        "source": source,
                    },
                    "evidence_digest": "8ecec98d6d55929b09bd7a1557e7b2c9e689bf8dec9339a4cb107c8f2b6d71c3",
                    "exit_code": 0,
                    "intake_id": "09717ebe-e742-4403-80a9-2c6d9cb2d845",
                    "machine_id": "HOST-1",
                    "native_exit_confirmed": native_exit_confirmed,
                    "native_id": native_id,
                    "operation_id": ev_operation_id,
                    "session_id": SID,
                }
            else:
                sess["evidence"] = None
            return {"session": sess}

        # 1. Active run fails at capture after native session already exited 0 and retired:
        # cleanup stop receives 409 (session_not_running), verifies positive aplexer_retirement
        # + fresh gone check, archives intake, unlinks pending file, and keeps ok=False at stage=capture.
        (self.tmp / "show_payload.json").write_text(json.dumps(make_retired_show()))
        r_cap, v_cap = self.run_canary("capture_fail_stop_409_retired", "--mode", "full")
        self.assertEqual(r_cap.returncode, 1)
        self.assertEqual(
            (v_cap["ok"], v_cap["stage"], v_cap["reason"], v_cap["cleanup"], v_cap["intake_archived"]),
            (False, "capture", "hub_capture_exit_1", "stopped", True),
        )
        self.assertEqual(list((self.state / "pending").glob("*.json")), [])
        self.assertEqual(
            (self.tmp / "archived_intake").read_text(),
            "intake update 09717ebe-e742-4403-80a9-2c6d9cb2d845 --status archived --note periodic_full_canary",
        )

        # 2. If native is still listed in `a list --json --running`, even valid retirement evidence
        # must fail closed (FAILED_needs_reconciliation) and retain the pending file.
        r_still, v_still = self.run_canary("capture_fail_stop_409_still_running", "--mode", "full")
        self.assertEqual(r_still.returncode, 1)
        self.assertEqual(
            (v_still["ok"], v_still["stage"], v_still["reason"], v_still["cleanup"]),
            (False, "capture", "hub_capture_exit_1", "FAILED_needs_reconciliation"),
        )
        pending_files = list((self.state / "pending").glob("*.json"))
        self.assertEqual(len(pending_files), 1)

        # 3. Next run reconciles that prior pending file when stop returns 409 (session_not_running)
        # and positive aplexer_retirement + fresh gone check pass.
        (self.tmp / "a_running").unlink(missing_ok=True)
        r_rec, v_rec = self.run_canary("reconcile_stop_409", "--mode", "shell")
        self.assertEqual(r_rec.returncode, 0, r_rec.stderr)
        self.assertTrue(v_rec["ok"])
        self.assertFalse(pending_files[0].exists())
        self.assertEqual(list((self.state / "pending").glob("*.json")), [])

        # 4. Prior pending reconciliation also accepts state="stopped" with stop_confirmed=True + fresh gone check.
        prior_stopped = (self.state / "pending") / "prior-stopped.json"
        prior_stopped.write_text(
            json.dumps(
                {
                    "run_id": "prior-stopped",
                    "mode": "shell",
                    "name": "hub-canary-shell-prior-stopped",
                    "key": "canary-prior-stopped",
                    "repo": "owner/repo",
                    "machine": "HOST-1",
                    "workspace": ws,
                    "prompt": "printf CANARY-OK; sleep 30",
                    "candidate_sha": None,
                    "session_id": SID,
                    "native_id": NATIVE,
                }
            )
        )
        os.chmod(prior_stopped, 0o600)
        (self.tmp / "show_payload.json").write_text(
            json.dumps(make_retired_show(state="stopped", stop_confirmed=True, include_evidence=False))
        )
        r_sc, v_sc = self.run_canary("reconcile_stop_409", "--mode", "shell")
        self.assertEqual(r_sc.returncode, 0, r_sc.stderr)
        self.assertTrue(v_sc["ok"])
        self.assertFalse(prior_stopped.exists())

        # 5. Terminal states without positive confirmation (or with tampered bindings/timestamps)
        # MUST fail closed on 409 even when `a list --json --running` is empty.
        invalid_payloads = [
            make_retired_show(state="failed", include_evidence=False),
            make_retired_show(state="lost", include_evidence=False),
            make_retired_show(state="cancelled", include_evidence=False),
            make_retired_show(state="stopped", stop_confirmed=False, include_evidence=False),
            make_retired_show(native_exit_confirmed=False),
            make_retired_show(containment_empty=False),
            make_retired_show(source="unverified_observer"),
            make_retired_show(tag="task-hub2-wrong-session"),
            make_retired_show(nid_ws="/wrong/workspace"),
            make_retired_show(exited_at_ms=1700000000000),  # before created_at
            make_retired_show(ev_epoch=999),
            make_retired_show(ev_attempt_operation_id="different-op:0"),
            make_retired_show(native_id="bogus"),
            make_retired_show(native_id="aaaaaaaa-bbbb"),
            make_retired_show(native_id="AAAAAAAA-BBBB-4CCC-8DDD-EEEEEEEEEEEE"),
            make_retired_show(state="stopped", stop_confirmed=True, include_evidence=False, native_id="bogus"),
            make_retired_show(state="stopped", stop_confirmed=True, include_evidence=False, native_id="aaaaaaaa-bbbb"),
        ]
        for idx, bad_payload in enumerate(invalid_payloads):
            with self.subTest(bad_case=idx):
                pfile = (self.state / "pending") / f"prior-bad-{idx}.json"
                pfile.write_text(
                    json.dumps(
                        {
                            "run_id": f"prior-bad-{idx}",
                            "mode": "full",
                            "name": f"hub-canary-full-prior-bad-{idx}",
                            "key": f"canary-prior-bad-{idx}",
                            "repo": "owner/repo",
                            "machine": "HOST-1",
                            "workspace": ws,
                            "prompt": "Reply CANARY-OK and exit",
                            "candidate_sha": None,
                            "session_id": SID,
                            "native_id": None if bad_payload["session"]["native_id"] != NATIVE else NATIVE,
                        }
                    )
                )
                os.chmod(pfile, 0o600)
                (self.tmp / "show_payload.json").write_text(json.dumps(bad_payload))
                (self.tmp / "archived_intake").unlink(missing_ok=True)
                (self.tmp / "calls.log").write_text("")
                r_bad, v_bad = self.run_canary("reconcile_stop_409", "--mode", "shell")
                self.assertEqual(r_bad.returncode, 1)
                self.assertEqual(
                    (v_bad["ok"], v_bad["stage"], v_bad["reason"], v_bad["session_id"]),
                    (False, "cleanup", "prior_pending_reconciliation_failed", None),
                )
                self.assertTrue(pfile.exists())
                self.assertFalse((self.tmp / "archived_intake").exists())
                self.assertEqual([ln for ln in self.calls().splitlines() if ln.startswith("sessions start ")], [])
                pfile.unlink()
        (self.tmp / "show_payload.json").unlink(missing_ok=True)

        # Direct unit probe for B2: when expected_native_id is None and real UUID is alive in a_running,
        # self-consistent bogus/partial native_id in sessions show must return (False, None).
        from scripts import hub_start_canary as mod
        c_probe = mod.Canary("full", "probe-b2", deploy_gate=True)
        for bad_nat in ("bogus", "aaaaaaaa-bbbb", "AAAAAAAA-BBBB-4CCC-8DDD-EEEEEEEEEEEE", ""):
            for st_mode in ("retired", "stopped"):
                payload = (
                    make_retired_show(native_id=bad_nat)
                    if st_mode == "retired"
                    else make_retired_show(state="stopped", stop_confirmed=True, include_evidence=False, native_id=bad_nat)
                )
                (self.tmp / "show_payload.json").write_text(json.dumps(payload))
                c_probe.hub = [sys.executable, str(self.tmp / "fake_hub.py")]
                os.environ["FAKE_DIR"] = str(self.tmp)
                os.environ["FAKE_SCENARIO"] = "ok"
                self.assertEqual(
                    c_probe.verify_terminal_session_retired(
                        SID,
                        time.monotonic() + 5.0,
                        expected_native_id=None,
                        expected_key="canary-probe-b2",
                        expected_repo="owner/repo",
                        expected_machine="HOST-1",
                        expected_workspace=ws,
                    ),
                    (False, None),
                )
        (self.tmp / "show_payload.json").unlink(missing_ok=True)

        # 6. @opencode/cli --format json completed tool output (type="tool_use", part.type="tool",
        # part.state.status="completed", part.state.output with standalone marker) is accepted,
        # while Go assistant-only text events (even with standalone marker), non-completed tool states,
        # input/rawInput/metadata, and inline assistant summaries are rejected.
        (self.tmp / "capture_output.txt").write_text(
            json.dumps(
                {
                    "type": "tool_use",
                    "part": {
                        "type": "tool",
                        "tool": "bash",
                        "state": {
                            "status": "completed",
                            "input": {"command": "printf CANARY-OK"},
                            "output": "__MARKER__\n",
                        },
                    },
                }
            )
            + "\n"
        )
        r_tool_ok, v_tool_ok = self.run_canary("ok", "--mode", "full", FAKE_ENGINE="go")
        self.assertEqual(r_tool_ok.returncode, 0, r_tool_ok.stderr)
        self.assertTrue(v_tool_ok["ok"])

        rejected_captures = [
            # Go @opencode/cli --format json assistant-only text event with standalone marker (zero tool_use) must NOT count
            json.dumps(
                {
                    "type": "text",
                    "part": {
                        "type": "text",
                        "text": "__MARKER__",
                    },
                }
            ),
            json.dumps(
                {
                    "type": "text",
                    "part": {
                        "type": "text",
                        "text": "Intro line\n__MARKER__\nOutro line",
                    },
                }
            ),
            # Non-completed tool status must not count even if output has marker
            json.dumps(
                {
                    "type": "tool_use",
                    "part": {
                        "type": "tool",
                        "tool": "bash",
                        "state": {"status": "running", "output": "__MARKER__\n"},
                    },
                }
            ),
            # Marker only in input / rawInput / metadata must not count
            json.dumps(
                {
                    "type": "tool_use",
                    "part": {
                        "type": "tool",
                        "tool": "bash",
                        "state": {
                            "status": "completed",
                            "input": {"command": "__MARKER__"},
                            "rawInput": "__MARKER__",
                            "metadata": {"stdout": "__MARKER__"},
                            "output": "done\n",
                        },
                    },
                }
            ),
            # Inline assistant summary in plain text (03c67f8d failure mode) must not count
            "First tool call printed `__MARKER__` as requested.\nSecond tool call (`sleep 60`) completed.",
            # Inline assistant summary inside tool output must not count
            json.dumps(
                {
                    "type": "tool_use",
                    "part": {
                        "type": "tool",
                        "tool": "bash",
                        "state": {"status": "completed", "output": "Printed __MARKER__ inline\n"},
                    },
                }
            ),
        ]
        for idx, cap_text in enumerate(rejected_captures):
            with self.subTest(rejected_capture=idx):
                (self.tmp / "capture_output.txt").write_text(cap_text + "\n")
                r_rej, v_rej = self.run_canary("ok", "--mode", "full", FAKE_ENGINE="go", HUB_CANARY_CAPTURE_TIMEOUT_S="0.35")
                self.assertEqual(r_rej.returncode, 1)
                self.assertEqual(
                    (v_rej["ok"], v_rej["stage"], v_rej["reason"], v_rej["cleanup"]),
                    (False, "capture", "no_output_beyond_prompt", "stopped"),
                )
        (self.tmp / "capture_output.txt").unlink(missing_ok=True)

    def test_claude_and_argon_paired_streams_and_engine_isolation(self):
        import copy
        from scripts import hub_acceptance_10 as acc
        from scripts import hub_start_canary as mod

        def provider_frames(engine, marker):
            session = "11111111-2222-4333-8444-555555555555"
            if engine == "claude":
                return [
                    {
                        "type": "assistant",
                        "session_id": session,
                        "message": {
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "tool_use",
                                    "id": "toolu_synthetic_print",
                                    "name": "Bash",
                                    "input": {"command": "python3 synthetic_print.py"},
                                }
                            ],
                        },
                    },
                    {
                        "type": "user",
                        "session_id": session,
                        "message": {
                            "role": "user",
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": "toolu_synthetic_print",
                                    "content": marker + "\n",
                                    "is_error": False,
                                }
                            ],
                        },
                        "tool_use_result": {"stdout": marker + "\n", "stderr": "", "interrupted": False},
                    },
                ]
            params = {"CommandLine": "python3 synthetic_print.py"}
            return [
                {
                    "event": "step_update",
                    "step_update": {
                        "conversation_id": session,
                        "step_index": 7,
                        "step_type": "tool",
                        "state": "ACTIVE",
                        "tool_name": "run_command",
                        "tool_info": {"name": "run_command", "parameters": dict(params)},
                    },
                },
                {
                    "event": "step_update",
                    "step_update": {
                        "conversation_id": session,
                        "step_index": 7,
                        "step_type": "tool",
                        "state": "DONE",
                        "tool_name": "run_command",
                        "tool_info": {
                            "name": "run_command",
                            "parameters": dict(params),
                            "output": (
                                "Created At: 2026-01-01T00:00:00+00:00\n"
                                "Completed At: 2026-01-01T00:00:01+00:00\n"
                                "Command exited with code 0.\n\n"
                                "Command output:\n" + marker + "\n"
                            ),
                        },
                    },
                },
            ]

        def jsonl(events):
            return "\n".join(json.dumps(event) for event in events)

        # 1. End-to-end CLI canary passes for Claude and Argon with complete paired frames
        # (including a clipped leading line at index 0), and fails when stitched across two captures.
        for engine in ("claude", "antigravity"):
            frames = provider_frames(engine, "__MARKER__")
            with self.subTest(engine=engine, case="complete_pair_e2e"):
                (self.tmp / "capture_output.txt").write_text("clipped earlier frame\n" + jsonl(frames) + "\n")
                r_ok, v_ok = self.run_canary("ok", "--mode", "full", FAKE_ENGINE=engine)
                self.assertEqual(r_ok.returncode, 0, r_ok.stderr)
                self.assertTrue(v_ok["ok"])
            (self.tmp / "capture_output.txt").unlink(missing_ok=True)
            with self.subTest(engine=engine, case="stitched_across_captures_rejected"):
                (self.tmp / "capture_seq.json").write_text(json.dumps([jsonl(frames[:1]), jsonl(frames[1:])]))
                r_st, v_st = self.run_canary(
                    "ok", "--mode", "full", FAKE_ENGINE=engine, HUB_CANARY_CAPTURE_TIMEOUT_S="0.35"
                )
                self.assertEqual(r_st.returncode, 1)
                self.assertEqual((v_st["ok"], v_st["stage"], v_st["reason"]), (False, "capture", "no_output_beyond_prompt"))
            (self.tmp / "capture_seq.json").unlink(missing_ok=True)

        # 2. Direct parser parity with scripts.hub_acceptance_10.paired_stream_output_ok and strict engine isolation.
        c_full = mod.Canary("full", "pair-check")
        marker = c_full.marker
        codex_ev = json.dumps(
            {
                "type": "item.completed",
                "item": {"type": "command_execution", "status": "completed", "exit_code": 0, "aggregated_output": marker},
            }
        )
        go_ev = json.dumps(
            {"type": "tool_use", "part": {"type": "tool", "tool": "bash", "state": {"status": "completed", "output": marker}}}
        )
        claude_ev = jsonl(provider_frames("claude", marker))
        argon_ev = jsonl(provider_frames("antigravity", marker))
        by_engine = {
            "codex": codex_ev,
            "zai": codex_ev,
            "go": go_ev,
            "claude": claude_ev,
            "antigravity": argon_ev,
        }
        for eng, matching in by_engine.items():
            self.assertTrue(c_full.output_has_marker(matching, engine=eng), eng)
            self.assertFalse(c_full.output_has_marker(marker, engine=eng), eng)
            for other_eng, other_payload in by_engine.items():
                if {eng, other_eng} == {"codex", "zai"} or other_eng == eng:
                    continue
                self.assertFalse(c_full.output_has_marker(other_payload, engine=eng), (eng, other_eng))

        c_shell = mod.Canary("shell", "shell-check")
        self.assertTrue(c_shell.output_has_marker("CANARY-OK\n"))
        self.assertFalse(c_shell.output_has_marker(json.dumps({"type": "result", "result": "CANARY-OK"})))

        # 3. Claude rejection matrix (orphans, reused IDs, errors, interruptions, prose/result, malformed middle).
        claude_changes = [
            lambda f: f[:1],
            lambda f: f[1:],
            lambda f: (f[1].update(session_id="99999999-2222-4333-8444-555555555555") or f),
            lambda f: (f[0].update(session_id="not-provider-session") or f),
            lambda f: (f[0]["message"]["content"][0].update(name="Read") or f),
            lambda f: (f[1]["message"]["content"][0].update(tool_use_id="toolu_other") or f),
            lambda f: (f[1]["message"]["content"][0].update(is_error=True) or f),
            lambda f: (f[1]["message"]["content"][0].pop("is_error", None), f)[1],
            lambda f: (f[1]["tool_use_result"].update(interrupted=True) or f),
            lambda f: (f[1]["tool_use_result"].pop("interrupted", None), f)[1],
            lambda f: (f[1]["tool_use_result"].update(stdout="different output") or f),
            lambda f: (f[1]["tool_use_result"].update(stderr="synthetic error") or f),
            lambda f: (f[1]["message"].update(role="assistant") or f),
            lambda f: (f[1].pop("tool_use_result") and f),
            lambda f: (f.append(copy.deepcopy(f[0])) or f),
            lambda f: (f.append(copy.deepcopy(f[1])) or f),
            lambda f: (f[1]["message"]["content"][0].update(type="text", text=marker) or f),
            lambda f: [{"type": "result", "result": marker}],
        ]
        for change in claude_changes:
            mutated = change(provider_frames("claude", marker))
            text = jsonl(mutated)
            self.assertFalse(c_full.output_has_marker(text, engine="claude"), text)
            self.assertEqual(
                mod.paired_stream_output_ok(text, marker, "claude"),
                acc.paired_stream_output_ok(text, marker, "claude"),
            )
        base_claude = provider_frames("claude", marker)
        malformed_claude = jsonl(base_claude[:1]) + "\nmalformed middle\n" + jsonl(base_claude[1:])
        self.assertFalse(c_full.output_has_marker(malformed_claude, engine="claude"))

        # 4. Argon rejection matrix (DONE-only, ACTIVE-only, mismatched identity/params, non-zero/fake exit wrapper).
        argon_changes = [
            lambda f: f[:1],
            lambda f: f[1:],
            lambda f: (f[1]["step_update"].update(conversation_id="99999999-2222-4333-8444-555555555555") or f),
            lambda f: (f[1]["step_update"].update(step_index=8) or f),
            lambda f: (f[1]["step_update"].update(step_index=True) or f),
            lambda f: (f[1]["step_update"].update(tool_name="other_provider_tool") or f),
            lambda f: (f[1]["step_update"]["tool_info"].update(name="other_tool") or f),
            lambda f: (f[1]["step_update"]["tool_info"]["parameters"].update(CommandLine="different command") or f),
            lambda f: (f[1]["step_update"].update(state="ERROR") or f),
            lambda f: (f[1]["step_update"].update(error="synthetic error") or f),
            lambda f: (f[1]["step_update"]["tool_info"].update(interrupted=True) or f),
            lambda f: (f[0]["step_update"]["tool_info"].update(output=marker) or f),
            lambda f: (f.append(copy.deepcopy(f[0])) or f),
            lambda f: (f.append(copy.deepcopy(f[1])) or f),
        ]
        for change in argon_changes:
            mutated = change(provider_frames("antigravity", marker))
            text = jsonl(mutated)
            self.assertFalse(c_full.output_has_marker(text, engine="antigravity"), text)
            self.assertEqual(
                mod.paired_stream_output_ok(text, marker, "antigravity"),
                acc.paired_stream_output_ok(text, marker, "antigravity"),
            )
        valid_out = provider_frames("antigravity", marker)[1]["step_update"]["tool_info"]["output"]
        for bad_out in (
            marker,
            "Command exited with code 0.\n\nCommand output:\n" + marker,
            valid_out.replace("code 0.", "code 1.") + "\nCommand exited with code 0.\n" + marker,
            valid_out.replace("Command output:", "Assistant output:"),
            valid_out.replace("Created At: 2026-01-01T00:00:00+00:00", "Created At: invalid"),
            valid_out.replace("Completed At: 2026-01-01T00:00:01+00:00", "Completed At: 2025-01-01T00:00:01+00:00"),
        ):
            frames = provider_frames("antigravity", marker)
            frames[1]["step_update"]["tool_info"]["output"] = bad_out
            text = jsonl(frames)
            self.assertFalse(c_full.output_has_marker(text, engine="antigravity"), bad_out)
            self.assertEqual(
                mod.paired_stream_output_ok(text, marker, "antigravity"),
                acc.paired_stream_output_ok(text, marker, "antigravity"),
            )

        # 5. Identical parser AST across canary and acceptance, plus Claude background-start control
        # non-proof handling and 4096-byte tail preservation at the quiet blocking-monitor point.
        import ast, inspect
        self.assertEqual(
            ast.dump(ast.parse(inspect.getsource(mod.paired_stream_output_ok))),
            ast.dump(ast.parse(inspect.getsource(acc.paired_stream_output_ok))),
        )
        session = "11111111-2222-4333-8444-555555555555"

        def padded_frame(obj, target_len):
            raw = json.dumps(obj, separators=(",", ":"))
            self.assertLessEqual(len(raw), target_len)
            if len(raw) < target_len:
                pad_key = "_pad"
                overhead = len(json.dumps({pad_key: ""}, separators=(",", ":"))) - 1
                obj = dict(obj, **{pad_key: "x" * max(0, target_len - len(raw) - overhead)})
                raw = json.dumps(obj, separators=(",", ":"))
            self.assertEqual(len(raw), target_len)
            return raw

        def bg_frames(task_id="bg_synth_01", cmd="sleep 60; echo done", content_override=None, stdout_val="", is_err=False, interrupted_val=False, stderr_val="", bg_flag=True):
            content_str = (
                content_override
                if content_override is not None
                else f"Command running in background with ID: {task_id}. Output is being written to: /tmp/synthetic-claude/tasks/{task_id}.output"
            )
            use_ev = {
                "type": "assistant",
                "session_id": session,
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_synthetic_bg_hold",
                            "name": "Bash",
                            "input": {
                                "command": cmd,
                                "description": "Hold 60 seconds in background",
                                "run_in_background": bg_flag,
                                "timeout": 120000,
                            },
                        }
                    ],
                },
            }
            hook_1 = {"type": "system", "subtype": "hook_started", "session_id": session}
            hook_2 = {"type": "system", "subtype": "hook_response", "session_id": session, "outcome": "success"}
            res_ev = {
                "type": "user",
                "session_id": session,
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_synthetic_bg_hold",
                            "content": content_str,
                            "is_error": is_err,
                        }
                    ],
                },
                "tool_use_result": {
                    "stdout": stdout_val,
                    "stderr": stderr_val,
                    "interrupted": interrupted_val,
                    "isImage": False,
                    "noOutputExpected": False,
                    "backgroundTaskId": task_id,
                },
            }
            return [use_ev, hook_1, hook_2, res_ev]

        valid_fg = provider_frames("claude", marker)
        valid_bg = bg_frames()
        # Both orders (valid foreground pair followed by background control pair as in 23:25Z lines 9..14,
        # and background control pair followed by valid foreground pair) must pass.
        for combined in (valid_fg + valid_bg, valid_bg + valid_fg):
            txt = jsonl(combined)
            self.assertTrue(c_full.output_has_marker(txt, engine="claude"))
            self.assertTrue(mod.paired_stream_output_ok(txt, marker, "claude"))
            self.assertTrue(acc.paired_stream_output_ok(txt, marker, "claude"))

        # Observed byte-size layout (17260B prefix + 1174B bg tool_use + 291B/337B hooks + 950B bg tool_result
        # + 1106B fg marker tool_use + 609B fg marker tool_result + 520B blocking TaskOutput tool_use + 291B/337B hooks):
        # at the quiet blocking-monitor point, the 4096-byte tail slice preserves the complete foreground marker pair.
        prefix_line = padded_frame({"type": "system", "subtype": "init", "session_id": session}, 11000)
        first_action_res = padded_frame({"type": "system", "subtype": "first_action", "session_id": session}, 6259)
        bg_use_line = padded_frame(valid_bg[0], 1174)
        bg_hook1_line = padded_frame(valid_bg[1], 291)
        bg_hook2_line = padded_frame(valid_bg[2], 337)
        bg_res_line = padded_frame(valid_bg[3], 950)
        fg_use_line = padded_frame(valid_fg[0], 1106)
        fg_res_line = padded_frame(valid_fg[1], 609)
        monitor_use_line = padded_frame(
            {
                "type": "assistant",
                "session_id": session,
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_synthetic_monitor",
                            "name": "TaskOutput",
                            "input": {"task_id": "bg_synth_01", "block": True, "timeout": 60000},
                        }
                    ],
                },
            },
            520,
        )
        mon_hook1_line = padded_frame(valid_bg[1], 291)
        mon_hook2_line = padded_frame(valid_bg[2], 337)
        stream_at_blocking_monitor = "\n".join(
            [
                prefix_line,
                first_action_res,
                bg_use_line,
                bg_hook1_line,
                bg_hook2_line,
                bg_res_line,
                fg_use_line,
                fg_res_line,
                monitor_use_line,
                mon_hook1_line,
                mon_hook2_line,
            ]
        ) + "\n"
        tail_4096 = stream_at_blocking_monitor.encode("utf-8")[-4096:].decode("utf-8")
        self.assertEqual(len(tail_4096.encode("utf-8")), 4096)
        self.assertIn(fg_use_line, tail_4096)
        self.assertIn(fg_res_line, tail_4096)
        self.assertTrue(c_full.output_has_marker(tail_4096, engine="claude"))
        self.assertTrue(mod.paired_stream_output_ok(tail_4096, marker, "claude"))
        self.assertTrue(acc.paired_stream_output_ok(tail_4096, marker, "claude"))

        # Conversely, in the old order where fg pair ran before bg hold AND the turn finished with 915B prose + 2524B result,
        # the 4096-byte tail evicted the fg pair; our parser must reject that evicted tail (no prose/result acceptance).
        prose_line = padded_frame(
            {
                "type": "assistant",
                "session_id": session,
                "message": {"role": "assistant", "content": [{"type": "text", "text": marker}]},
            },
            915,
        )
        turn_result_line = padded_frame({"type": "result", "subtype": "success", "result": marker}, 2524)
        old_evicted_stream = "\n".join(
            [
                prefix_line,
                first_action_res,
                fg_use_line,
                fg_res_line,
                bg_use_line,
                bg_hook1_line,
                bg_hook2_line,
                bg_res_line,
                prose_line,
                turn_result_line,
            ]
        ) + "\n"
        evicted_tail_4096 = old_evicted_stream.encode("utf-8")[-4096:].decode("utf-8")
        self.assertFalse(c_full.output_has_marker(evicted_tail_4096, engine="claude"))
        self.assertFalse(mod.paired_stream_output_ok(evicted_tail_4096, marker, "claude"))
        self.assertFalse(acc.paired_stream_output_ok(evicted_tail_4096, marker, "claude"))

        # Background-only and negative background control mutations must all fail closed.
        fg_with_bg_res = provider_frames("claude", marker)
        fg_with_bg_res[1]["tool_use_result"]["backgroundTaskId"] = "bg_synth_01"
        bg_with_fg_res = bg_frames()
        bg_with_fg_res[3]["tool_use_result"].pop("backgroundTaskId", None)
        bg_with_fg_res[3]["tool_use_result"]["stdout"] = marker + "\n"
        bg_with_fg_res[3]["message"]["content"][0]["content"] = marker + "\n"

        negative_bg_streams = [
            valid_bg,
            bg_frames(cmd=marker),
            bg_frames(content_override=f"Command running in background with ID: bg_synth_01.\n{marker}"),
            bg_frames(content_override=marker, stdout_val=marker),
            bg_with_fg_res,
            valid_fg + bg_with_fg_res,
            fg_with_bg_res,
            valid_fg + fg_with_bg_res,
            valid_fg + bg_frames(bg_flag=1),
            valid_fg + bg_frames(bg_flag="true"),
            valid_fg + bg_frames(task_id=""),
            valid_fg + bg_frames(task_id="bad id!"),
            valid_fg + bg_frames(content_override="Command running in background with ID: other_id. Output is being written."),
            valid_fg + bg_frames(stdout_val="unexpected stdout"),
            valid_fg + bg_frames(stderr_val="unexpected stderr"),
            valid_fg + bg_frames(is_err=True),
            valid_fg + bg_frames(interrupted_val=True),
        ]
        for neg_frames in negative_bg_streams:
            neg_text = jsonl(neg_frames)
            self.assertFalse(c_full.output_has_marker(neg_text, engine="claude"), neg_text)
            self.assertFalse(mod.paired_stream_output_ok(neg_text, marker, "claude"), neg_text)
            self.assertFalse(acc.paired_stream_output_ok(neg_text, marker, "claude"), neg_text)

        # 6. Argon non-proof tool control steps (manage_task, schedule, command_status, wait, wait_5_seconds)
        # and structural 4096-byte snapshot (clipped 111B sleep-60 tail + 303B/289B/484B/303B/296B/705B/304B/281B/494B/327B/187B frames).
        def argon_tool_pair(step_idx, tool_name, params, output_str, *, active_name=None, done_name=None, done_params=None, done_state="DONE"):
            act_info = {"name": tool_name if active_name is None else active_name, "parameters": dict(params)}
            done_info = {
                "name": tool_name if done_name is None else done_name,
                "parameters": dict(params if done_params is None else done_params),
                "output": output_str,
            }
            return [
                {
                    "event": "step_update",
                    "step_update": {
                        "conversation_id": session,
                        "step_index": step_idx,
                        "state": "ACTIVE",
                        "step_type": "tool",
                        "tool_name": tool_name,
                        "tool_info": act_info,
                    },
                },
                {
                    "event": "step_update",
                    "step_update": {
                        "conversation_id": session,
                        "step_index": step_idx,
                        "state": done_state,
                        "step_type": "tool",
                        "tool_name": tool_name,
                        "tool_info": done_info,
                    },
                },
            ]

        argon_fg = argon_tool_pair(
            6,
            "run_command",
            {"CommandLine": "printf '%s%s\\n' CANARY- OK:synthetic"},
            f"Created At: 2026-01-01T00:00:00+00:00\nCompleted At: 2026-01-01T00:00:01+00:00\nCommand exited with code 0.\n\nCommand output:\n{marker}\r\n",
        )
        argon_bg_hold = argon_tool_pair(
            4,
            "run_command",
            {"CommandLine": "sleep 60"},
            "Command exited with code 0.\n\nNo output from the command.",
        )
        argon_manage = argon_tool_pair(
            8,
            "manage_task",
            {"Action": "status", "TaskId": f"{session}/task-4"},
            f"Created At: 2026-01-01T00:00:02+00:00\nCompleted At: 2026-01-01T00:00:02+00:00\nTask: {session}/task-4\nStatus: RUNNING\nLog: file:///tmp/synthetic-argon/tasks/task-4.log\n",
        )
        argon_schedule = argon_tool_pair(
            10,
            "schedule",
            {"DurationSeconds": 60, "Prompt": "Background hold task finished."},
            f"Timer cancelled early: its early-termination condition ({session}/task-4) was met by a message from {session}/task-4.",
        )
        argon_cmd_status = argon_tool_pair(
            12,
            "command_status",
            {"CommandId": "cmd-synth-4", "WaitDurationSeconds": 5},
            "Status: RUNNING",
        )
        argon_wait = argon_tool_pair(14, "wait", {"seconds": "5"}, "Waited.")
        argon_wait5 = argon_tool_pair(16, "wait_5_seconds", {}, "Waited 5 seconds.")

        for combo in (
            argon_bg_hold + argon_fg + argon_manage + argon_schedule,
            argon_manage + argon_schedule + argon_fg,
            argon_fg + argon_manage + argon_schedule[:1],
            argon_fg + argon_cmd_status + argon_wait + argon_wait5,
        ):
            txt = jsonl(combo)
            self.assertTrue(c_full.output_has_marker(txt, engine="antigravity"), txt)
            self.assertTrue(mod.paired_stream_output_ok(txt, marker, "antigravity"), txt)
            self.assertTrue(acc.paired_stream_output_ok(txt, marker, "antigravity"), txt)

        # Structural 4096-byte Argon snapshot matching the observed 12-line tail (111B clipped step 4 DONE
        # + 303B step 5 + 289B/484B step 6 run_command pair + 303B step 7 + 296B/705B step 8 manage_task pair
        # + 304B step 9 + 281B/494B step 10 schedule pair + 327B step 11 + 187B step 12).
        ar_init = padded_frame({"event": "init", "conversation_id": session}, 1340)
        ar_s0 = padded_frame({"event": "step_update", "step_update": {"conversation_id": session, "step_index": 0, "state": "DONE", "step_type": "user_input"}}, 151)
        ar_s1 = padded_frame({"event": "step_update", "step_update": {"conversation_id": session, "step_index": 1, "state": "DONE", "step_type": "agent_response"}}, 300)
        ar_s2_act = padded_frame(argon_tool_pair(2, "run_command", {"CommandLine": "aplexer whoami --json"}, "")[0], 422)
        ar_s2_done = padded_frame(argon_tool_pair(2, "run_command", {"CommandLine": "aplexer whoami --json"}, "Command exited with code 0.\n\nNo output from the command.")[1], 601)
        ar_s3 = padded_frame({"event": "step_update", "step_update": {"conversation_id": session, "step_index": 3, "state": "DONE", "step_type": "agent_response"}}, 301)
        ar_s4_act = padded_frame(argon_bg_hold[0], 248)
        ar_s4_done = padded_frame(argon_bg_hold[1], 348)
        ar_s5 = padded_frame({"event": "step_update", "step_update": {"conversation_id": session, "step_index": 5, "state": "DONE", "step_type": "agent_response"}}, 303)
        ar_s6_act = padded_frame(argon_fg[0], 289)
        ar_s6_done = padded_frame(argon_fg[1], 484)
        ar_s7 = padded_frame({"event": "step_update", "step_update": {"conversation_id": session, "step_index": 7, "state": "DONE", "step_type": "agent_response"}}, 303)
        ar_s8_act = padded_frame(argon_manage[0], 296)
        ar_s8_done = padded_frame(argon_manage[1], 705)
        ar_s9 = padded_frame({"event": "step_update", "step_update": {"conversation_id": session, "step_index": 9, "state": "DONE", "step_type": "agent_response"}}, 304)
        ar_s10_act = padded_frame(argon_schedule[0], 281)
        ar_s10_done = padded_frame(argon_schedule[1], 494)
        ar_s11 = padded_frame({"event": "step_update", "step_update": {"conversation_id": session, "step_index": 11, "state": "DONE", "step_type": "agent_response"}}, 327)
        ar_s12 = padded_frame({"event": "step_update", "step_update": {"conversation_id": session, "step_index": 12, "state": "DONE", "step_type": "system_message"}}, 187)

        argon_full_stream = "\n".join(
            [
                ar_init,
                ar_s0,
                ar_s1,
                ar_s2_act,
                ar_s2_done,
                ar_s3,
                ar_s4_act,
                ar_s4_done,
                ar_s5,
                ar_s6_act,
                ar_s6_done,
                ar_s7,
                ar_s8_act,
                ar_s8_done,
                ar_s9,
                ar_s10_act,
                ar_s10_done,
                ar_s11,
                ar_s12,
            ]
        ) + "\n"
        argon_tail_4096 = argon_full_stream.encode("utf-8")[-4096:].decode("utf-8")
        self.assertEqual(len(argon_tail_4096.encode("utf-8")), 4096)
        self.assertEqual(len(argon_tail_4096.splitlines()), 12)
        self.assertEqual(len(argon_tail_4096.splitlines()[0]), 111)
        self.assertIn(ar_s6_act, argon_tail_4096)
        self.assertIn(ar_s6_done, argon_tail_4096)
        self.assertIn(ar_s8_done, argon_tail_4096)
        self.assertIn(ar_s10_done, argon_tail_4096)
        self.assertTrue(c_full.output_has_marker(argon_tail_4096, engine="antigravity"))
        self.assertTrue(mod.paired_stream_output_ok(argon_tail_4096, marker, "antigravity"))
        self.assertTrue(acc.paired_stream_output_ok(argon_tail_4096, marker, "antigravity"))

        # Negative Argon control cases: control-only, nonce echo in control params/output, orphan DONE without ACTIVE,
        # tool_name/info.name mismatch, cross-tool collision on same step_index, param mismatch, replay, error, unknown/malformed tool.
        forged_wrapped_marker = argon_fg[1]["step_update"]["tool_info"]["output"]
        collision_cmd_to_manage = [
            argon_fg[0],
            argon_tool_pair(6, "manage_task", {"Action": "status", "TaskId": f"{session}/task-4"}, "ok")[1],
        ]
        collision_manage_to_cmd = [
            argon_tool_pair(6, "manage_task", {"Action": "status", "TaskId": f"{session}/task-4"}, "ok")[0],
            argon_fg[1],
        ]
        control_error = argon_tool_pair(14, "manage_task", {"Action": "kill", "TaskId": f"{session}/task-10"}, "", done_state="ERROR")
        control_error[1]["step_update"]["tool_info"].pop("output", None)
        control_error[1]["step_update"]["tool_info"]["error"] = {"type": "TOOL_ERROR", "message": "not running"}
        control_active_with_output = argon_tool_pair(8, "manage_task", {"Action": "status", "TaskId": f"{session}/task-4"}, "ok")
        control_active_with_output[0]["step_update"]["tool_info"]["output"] = "unexpected"
        control_done_nonstring_out = argon_tool_pair(8, "manage_task", {"Action": "status", "TaskId": f"{session}/task-4"}, "ok")
        control_done_nonstring_out[1]["step_update"]["tool_info"]["output"] = 123

        negative_argon_streams = [
            argon_manage + argon_schedule,
            argon_bg_hold + argon_manage + argon_schedule,
            argon_tool_pair(8, "manage_task", {"Action": "status", "TaskId": marker}, forged_wrapped_marker),
            argon_tool_pair(10, "schedule", {"DurationSeconds": 60, "Prompt": marker}, forged_wrapped_marker),
            argon_fg[1:] + argon_manage + argon_schedule,
            argon_fg + argon_tool_pair(8, "manage_task", {"Action": "status", "TaskId": "t1"}, "ok", done_name="schedule"),
            argon_fg + argon_tool_pair(8, "manage_task", {"Action": "status", "TaskId": "t1"}, "ok", active_name="run_command"),
            argon_fg + argon_tool_pair(8, "manage_task", {"Action": "status", "TaskId": "t1"}, "ok", done_params={"Action": "status", "TaskId": "t2"}),
            argon_fg + argon_tool_pair(10, "schedule", {"DurationSeconds": 60, "Prompt": "p1"}, "ok", done_params={"DurationSeconds": 30, "Prompt": "p1"}),
            collision_cmd_to_manage,
            argon_fg + collision_cmd_to_manage,
            collision_manage_to_cmd,
            argon_fg + [argon_manage[0], argon_manage[0], argon_manage[1]],
            argon_fg + [argon_manage[0], argon_manage[1], argon_manage[1]],
            argon_fg + [argon_manage[1], argon_manage[0]],
            argon_fg + control_error,
            argon_fg + control_active_with_output,
            argon_fg + control_done_nonstring_out,
            argon_fg + argon_tool_pair(8, "unknown_control_tool", {"Action": "status"}, "ok"),
            argon_fg + argon_tool_pair(8, "run_command", {"CommandLine": "true", "Extra": "bad"}, forged_wrapped_marker),
            argon_fg + argon_tool_pair(8, "run_command", {"CommandLine": ""}, forged_wrapped_marker),
            argon_fg + argon_tool_pair(8, "manage_task", {"Action": "unknown_action"}, "ok"),
            argon_fg + argon_tool_pair(8, "manage_task", {"Action": "status", "Extra": "bad"}, "ok"),
            argon_fg + argon_tool_pair(8, "manage_task", {"Action": "status", "TaskId": ""}, "ok"),
            argon_fg + argon_tool_pair(10, "schedule", {"DurationSeconds": 0, "Prompt": "wait"}, "ok"),
            argon_fg + argon_tool_pair(10, "schedule", {"DurationSeconds": True, "Prompt": "wait"}, "ok"),
            argon_fg + argon_tool_pair(10, "schedule", {"DurationSeconds": 60, "Prompt": ""}, "ok"),
            argon_fg + argon_tool_pair(10, "schedule", {"DurationSeconds": 60, "Prompt": "wait", "Unknown": "x"}, "ok"),
            argon_fg + argon_tool_pair(16, "wait_5_seconds", {"unexpected": "arg"}, "ok"),
            argon_fg + argon_tool_pair(14, "wait", {}, "ok"),
        ]
        for neg_frames in negative_argon_streams:
            neg_text = jsonl(neg_frames)
            self.assertFalse(c_full.output_has_marker(neg_text, engine="antigravity"), neg_text)
            self.assertFalse(mod.paired_stream_output_ok(neg_text, marker, "antigravity"), neg_text)
            self.assertFalse(acc.paired_stream_output_ok(neg_text, marker, "antigravity"), neg_text)

        # 7. Two-call quiet-hold prompt contract (periodic + deploy-gate full modes) and 02:55Z ToolSearch burst regression.
        c_gate_full = mod.Canary(
            "full",
            "pair-check",
            deploy_gate=True,
            candidate_sha="a47ed73aa1eb3f5bbe597fa0016ba69cc8bad3af",
            run_token="tok987",
        )
        for inst, expected_piece in (
            (c_full, '"pair-check"'),
            (c_gate_full, '"a47ed73aa1eb:tok987"'),
        ):
            self.assertNotIn(inst.marker, inst.prompt)
            claude_prompt, other_prompt = inst.prompt.split("For every other runtime, follow this unchanged sequence: ", 1)
            self.assertTrue(claude_prompt.startswith("If this runtime is Claude Code, first use its existing native Bash tool"))
            self.assertLess(claude_prompt.index("`run_in_background: true`"), claude_prompt.index("Then run a separate foreground shell command"))
            self.assertLess(claude_prompt.index("Then run a separate foreground shell command"), claude_prompt.index("provider-native blocking Monitor or TaskOutput"))
            self.assertIn("wait on that exact background task id", claude_prompt)
            self.assertIn("background completion is not proof", claude_prompt)
            self.assertIn("substitute another sleep mechanism", claude_prompt)
            self.assertIn("do not create or edit other files", claude_prompt)
            self.assertNotIn("foreground `sleep", claude_prompt)
            expected_hash = "162c127213a58a56922031e4f7bcb6ed635cf931d6ba5bf2ee6d41b64f14c4bf" if inst is c_full else "ea03a441552fab36088e0dba4ca0d238811c6210e1f0825985d02fa356cc7d44"
            self.assertEqual(__import__("hashlib").sha256(other_prompt.encode()).hexdigest(), expected_hash)
            self.assertTrue(other_prompt.startswith("Use two sequential shell tool calls. First run a separate foreground shell command"))
            self.assertIn(f'concatenation of "CANARY-", "OK:", and {expected_piece} as one output line and let it finish.', inst.prompt)
            self.assertIn(
                "Immediately after the marker print finishes, run a separate foreground `sleep 60` command using the existing shell tool with a supported timeout sufficient for the 60-second hold",
                inst.prompt,
            )
            self.assertIn("Do not chain the marker print with the hold in one command", inst.prompt)
            self.assertIn("do not use a background flag", inst.prompt)
            self.assertIn("do not include the marker or nonce in the hold command", inst.prompt)
            self.assertIn("do not echo proof or emit waiting prose", inst.prompt)
            self.assertIn("do not search for or call any monitor, task-output, or status tools or poll in a loop", inst.prompt)
            self.assertIn("mandatory Hub first-action/completion evidence files", inst.prompt)
            self.assertNotIn("background tool facility", other_prompt)
            self.assertNotIn("TaskOutput", other_prompt)
            self.assertNotIn("Monitor", other_prompt)
            self.assertEqual(inst.poll, 2.0)

        # Two-call active foreground sleep 60 hold: after 1145B fg marker tool_use + 610B fg marker tool_result,
        # an in-progress 1102B foreground Bash `sleep 60` tool_use (with no tool_result while blocked for 60s)
        # keeps both marker frames unclipped inside the 4096B tail and proves the marker.
        fg_use_1145 = padded_frame(valid_fg[0], 1145)
        fg_res_610 = padded_frame(valid_fg[1], 610)
        fg_hold_use_1102 = padded_frame(
            {
                "type": "assistant",
                "session_id": session,
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_synthetic_fg_sleep60",
                            "name": "Bash",
                            "input": {
                                "command": "sleep 60",
                                "description": "Foreground 60-second hold",
                                "timeout": 90000,
                            },
                        }
                    ],
                },
            },
            1102,
        )
        two_call_active_hold_stream = "\n".join(
            [
                prefix_line,
                first_action_res,
                fg_use_1145,
                fg_res_610,
                fg_hold_use_1102,
            ]
        ) + "\n"
        two_call_tail_4096 = two_call_active_hold_stream.encode("utf-8")[-4096:].decode("utf-8")
        self.assertEqual(len(two_call_tail_4096.encode("utf-8")), 4096)
        self.assertIn(fg_use_1145, two_call_tail_4096)
        self.assertIn(fg_res_610, two_call_tail_4096)
        self.assertIn(fg_hold_use_1102, two_call_tail_4096)
        self.assertTrue(c_full.output_has_marker(two_call_tail_4096, engine="claude"))

        # Conversely, the 02:55Z (780f25d061e1) post-marker 4035B burst (193B+195B thinking_tokens + 2013B thinking
        # + 1035B ToolSearch tool_use + 594B ToolSearch tool_result + 5 newlines) evicts the 1755B marker pair from the
        # 4096B tail (leaving only the clipped last 60B of fg_res_610) and must fail closed.
        tok_193 = padded_frame({"type": "system", "subtype": "thinking_tokens", "session_id": session}, 193)
        tok_195 = padded_frame({"type": "system", "subtype": "thinking_tokens", "session_id": session}, 195)
        thinking_2013 = padded_frame(
            {
                "type": "assistant",
                "session_id": session,
                "message": {"role": "assistant", "content": [{"type": "thinking", "thinking": "", "signature": "sig"}]},
            },
            2013,
        )
        tool_search_use_1035 = padded_frame(
            {
                "type": "assistant",
                "session_id": session,
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_synthetic_toolsearch",
                            "name": "ToolSearch",
                            "input": {"query": "select:Monitor,TaskOutput", "max_results": 5},
                        }
                    ],
                },
            },
            1035,
        )
        tool_search_res_594 = padded_frame(
            {
                "type": "user",
                "session_id": session,
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_synthetic_toolsearch",
                            "content": "[{'type': 'tool_reference', 'tool_name': 'Monitor'}]",
                        }
                    ],
                },
            },
            594,
        )
        burst_evicted_stream = "\n".join(
            [
                prefix_line,
                first_action_res,
                fg_use_1145,
                fg_res_610,
                tok_193,
                tok_195,
                thinking_2013,
                tool_search_use_1035,
                tool_search_res_594,
            ]
        ) + "\n"
        burst_tail_4096 = burst_evicted_stream.encode("utf-8")[-4096:].decode("utf-8")
        self.assertEqual(len(burst_tail_4096.encode("utf-8")), 4096)
        self.assertEqual(len(burst_tail_4096.splitlines()[0]), 60)
        self.assertFalse(c_full.output_has_marker(burst_tail_4096, engine="claude"))


if __name__ == "__main__":
    unittest.main()
