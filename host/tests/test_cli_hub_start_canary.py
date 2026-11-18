"""Offline tests for scripts/hub-start-canary.sh. Fake hub/a replay real output shapes
(captured read-only from `hub sessions list --json`, `a list --json --running`; ids/paths scrubbed).
Mail renders to a sink; no real msmtp/sendmail is ever called.

Run: scripts/capped-test-run.sh -- scripts/with-temp-home.sh -- python3 -m unittest host.tests.test_cli_hub_start_canary
"""
import errno, http.server, json, os, pathlib, shutil, socket, stat, subprocess, sys, tempfile, threading, time, unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "hub-start-canary.sh"
SID, NATIVE = "11111111-2222-4333-8444-555555555555", "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"

FAKE_HUB = r'''
import json, os, re, signal, sys, pathlib, time
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
    ws = (d / "session_workspace").read_text() if (d / "session_workspace").exists() else os.environ["HUB_CANARY_WORKSPACE"]
    base = {"id": SID, "state": state, "intake_id": None, "project_id": "owner/repo", "machine_id": "HOST-1", "workspace": ws,
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
    (d / "session_workspace").write_text(args[args.index("--workspace") + 1])
    session_name = args[args.index("--name") + 1]
    session_key = args[args.index("--key") + 1]
    (d / "session_name").write_text(session_name)
    (d / "session_key").write_text(session_key)
    (d / "session_engine").write_text("shell" if "--engine" in args else os.environ.get("FAKE_ENGINE", "codex"))
    run_id = session_name.rsplit("-", 1)[-1]
    if "printf '%%s:%%s:%%s\\n' CANARY-OK " in prompt_text:
        parts = prompt_text.split(";")[0].split()
        (d / "marker").write_text(f"CANARY-OK:{parts[-2]}:{parts[-1]}")
    elif "--engine" not in args and re.search(r'and "[0-9a-f]{12}:[0-9a-f]{32,64}" as one output line', prompt_text):
        bound = re.search(r'and "([0-9a-f]{12}:[0-9a-f]{32,64})" as one output line', prompt_text).group(1)
        (d / "marker").write_text(f"CANARY-OK:{bound}")
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
    if sc == "natural_exit_stop_409": (d / "a_running").unlink(missing_ok=True)
    print(json.dumps({"command_wait":{"state":"acked","ack":{"capture":output}}})); sys.exit(0)
if args[:2] == ["sessions", "stop"]:
    if sc == "stop_fail" or (sc == "cleanup_fail" and "cleanup" in " ".join(args)): sys.exit(1)
    if sc in ("capture_fail_stop_409_retired", "capture_fail_stop_409_still_running", "natural_exit_stop_409", "natural_exit_stop_409_still_running") or (sc == "reconcile_stop_409" and "reconcile" in " ".join(args)):
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


class FakeHealthServer:
    """Owned loopback /health fixture. Every canary run in this module is pointed here,
    so the health preflight never reaches the production endpoint."""

    def __init__(self):
        self.status = 200
        self.body = json.dumps({"ok": True, "service": "hub2"}).encode()
        self.drip_s = None
        self.requests = 0
        self.stop = threading.Event()
        self.disconnected = threading.Event()
        fixture = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                fixture.requests += 1
                fixture.last_headers = dict(self.headers)
                fixture.last_path = self.path
                self.send_response(fixture.status)
                self.send_header("Content-Type", "application/json")
                if fixture.drip_s is None:
                    self.send_header("Content-Length", str(len(fixture.body)))
                else:
                    self.send_header("Connection", "close")  # HTTP/1.0 close-delimited body
                self.end_headers()
                if fixture.drip_s is None:
                    self.wfile.write(fixture.body)
                    return
                # Valid JSON prefix, one byte at a time: each read is fast, the total is not.
                for ch in b'{"ok": true, "pad": "' + b"x" * 1000:
                    if fixture.stop.wait(fixture.drip_s):
                        return
                    try:
                        self.wfile.write(bytes([ch])); self.wfile.flush()
                    except OSError:
                        fixture.disconnected.set()
                        return

            def log_message(self, *_args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = "http://127.0.0.1:%d/health" % self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)



# Test-only admission collector shim, appended to a COPY of the helper (never to
# the shipped file). The admission arithmetic, workspace-config reader, quota
# evaluator and every start/replay seam stay the real production code; only the
# machine/service reads (quse, resource settings, native census, MemAvailable)
# and the fixed config location are replaced by synthetic data under FAKE_DIR.
ADMISSION_SHIM = r"""
def _install_test_collectors():
    import json as _json, os as _os, pathlib as _pl
    base = _os.environ.get("FAKE_DIR")
    if not base:
        return
    d = _pl.Path(base)
    def cfg():
        p = d / "admission.json"
        return _json.loads(p.read_text()) if p.exists() else {}
    global CANARY_WORKSPACE_CONFIG, WORKSPACE_CONFIG_UID, WORKSPACE_CONFIG_TRUST_ROOT
    CANARY_WORKSPACE_CONFIG = str(d / "etc-agent-hub" / "canary-workspace.json")
    WORKSPACE_CONFIG_UID = _os.geteuid()
    WORKSPACE_CONFIG_TRUST_ROOT = str(d)
    def log(name):
        with open(d / "admission.log", "a") as fh:
            fh.write(name + "\n")
    def fetch_disk_floor_gb(self, deadline):
        log("settings")
        floor = cfg().get("floor_gb", 1)
        if floor == "unavailable":
            raise StageFailure("admission", "resource_settings_unavailable")
        if not _finite_number(floor) or not DISK_FLOOR_GB_RANGE[0] <= floor <= DISK_FLOOR_GB_RANGE[1]:
            raise StageFailure("admission", "resource_settings_invalid")
        time.sleep(cfg().get("slow_s", 0))
        return floor
    def acquire_quota(self, deadline):
        log("quota")
        raw = cfg().get("quota", {"codex": {"status": "ok", "details": {"limit_reached": False}, "windows": {"5h": {"percent_remaining": 80, "present": True}}}})
        reason = evaluate_quota(raw, require_eligible_route=(self.mode == "full"))
        if reason:
            raise StageFailure("admission", reason)
    def sample_mem_available(self):
        log("mem")
        values = cfg().get("mem_available", [64 * 1024 ** 3])
        n = sum(1 for line in (d / "admission.log").read_text().splitlines() if line == "mem")
        return values[min(n, len(values)) - 1]
    def read_census(self, deadline):
        log("census")
        return cfg().get("census", [])
    def read_hub_rows(self, deadline):
        log("hub_rows")
        return cfg().get("hub_rows", [])
    def read_launcher_caps(self, deadline):
        log("launcher_caps")
        caps = cfg().get("launcher_caps", [1500 * 1024 ** 2, 256])
        if not isinstance(caps, list):
            raise StageFailure("admission", caps)
        return caps[0], caps[1]
    Canary.read_hub_rows = read_hub_rows
    Canary.read_launcher_caps = read_launcher_caps
    Canary.fetch_disk_floor_gb = fetch_disk_floor_gb
    Canary.acquire_quota = acquire_quota
    Canary.sample_mem_available = sample_mem_available
    Canary.read_census = read_census
_install_test_collectors()

"""


def build_helper_copy(dest):
    """Copy the three shipped helper files into dest, shimming only the Python copy."""
    dest = pathlib.Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    for name in ("hub-deploy-gate-forced.sh", "hub-start-canary.sh", "hub_start_canary.py"):
        data = (ROOT / "scripts" / name).read_text()
        if name == "hub_start_canary.py":
            marker = '\nif __name__ == "__main__":'
            assert data.count(marker) == 1
            data = data.replace(marker, "\n" + ADMISSION_SHIM + marker)
        (dest / name).write_text(data)
        (dest / name).chmod(0o755)
    return dest


class CanaryTest(unittest.TestCase):
    def setUp(self):
        base = ROOT / ".local" / "tmp"
        base.mkdir(parents=True, exist_ok=True)
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="hub-canary-test.", dir=base))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.health = FakeHealthServer()
        self.addCleanup(self.health.close)
        env_patch = mock.patch.dict(os.environ, {"HUB_CANARY_HEALTH_URL": self.health.url})
        env_patch.start()
        self.addCleanup(env_patch.stop)
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
        self.helper_dir = build_helper_copy(self.tmp / "helper-copy")
        sys.path.insert(0, str(ROOT))
        from scripts import hub_start_canary as _mod
        self.shim_inprocess(_mod)

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
            HUB_CANARY_CMD_TIMEOUT_S="10", HUB_CANARY_HEALTH_URL=self.health.url,
        )
        env["FAKE_PROMPT"] = mode_prompt or ("Reply CANARY-OK and exit" if ("full" in args or "--deploy-gate" in args) else "printf CANARY-OK; sleep 30")
        env.update(extra)
        for f in ("started", "a_running", "stopped", "list_count", "systemctl.jsonl", "session_engine", "capture_idx", "session_workspace"):
            (self.tmp / f).unlink(missing_ok=True)
        (self.tmp / "calls.log").write_text("")
        root_sha, copy_sha = self.helper_sha256(), self.copy_sha256()
        args = tuple(copy_sha if a == root_sha else a for a in args)
        r = subprocess.run([str(self.helper_dir / "hub-start-canary.sh"), *args], env=env, capture_output=True, text=True, timeout=60)
        # The shimmed copy stands in for the shipped helper: report the shipped bundle digest.
        r.stdout, r.stderr = r.stdout.replace(copy_sha, root_sha), r.stderr.replace(copy_sha, root_sha)
        verdict = json.loads(r.stdout.strip().splitlines()[-1])
        return r, verdict

    def shim_inprocess(self, mod):
        """Install the synthetic collectors on an imported helper module for one test."""
        saved = {n: getattr(mod.Canary, n) for n in ("read_hub_rows", "read_launcher_caps", "fetch_disk_floor_gb", "acquire_quota", "sample_mem_available", "read_census")}
        names = ("CANARY_WORKSPACE_CONFIG", "WORKSPACE_CONFIG_UID", "WORKSPACE_CONFIG_TRUST_ROOT")
        saved_globals = {n: getattr(mod, n) for n in names}
        def restore():
            for n, v in saved.items():
                setattr(mod.Canary, n, v)
            for n, v in saved_globals.items():
                setattr(mod, n, v)
        self.addCleanup(restore)
        self._restore_inprocess = restore
        with mock.patch.dict(os.environ, {"FAKE_DIR": str(self.tmp)}):
            exec(ADMISSION_SHIM, mod.__dict__)

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
        starts = [args for args in calls if args[:2] == ['sessions', 'start']]
        self.assertTrue(starts)
        self.assertTrue(all(args[args.index('--purpose') + 1] == 'review' for args in starts))
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

    # ---- public /health body preflight (Fable 0.3)
    def assert_no_health_leak(self, r, *extra):
        for text in (r.stdout, r.stderr, *(self.mails())):
            for secret in ("127.0.0.1", self.health.url, "dispatch_enabled", '"bootstrap"', "service", *extra):
                self.assertNotIn(secret, text)

    def test_bootstrap_health_body_fails_twice_and_renders_exactly_one_critical_alert(self):
        self.health.body = json.dumps({"status": "bootstrap", "dispatch_enabled": False}).encode()
        verdicts = []
        for _ in range(2):
            r, v = self.run_canary("ok", "--mode", "shell")
            self.assertEqual(r.returncode, 1)
            self.assertEqual((v["ok"], v["stage"], v["reason"], v["cleanup"], v["session_id"]),
                             (False, "health", "health_status_bootstrap", "not_needed", None))
            self.assertEqual(json.loads(r.stderr.strip().splitlines()[-1]), v)
            self.assertNotIn("sessions start", self.calls())
            self.assertFalse((self.tmp / "started").exists())
            self.assert_no_health_leak(r)
            verdicts.append(v)
        self.assertEqual(self.health.requests, 2)
        self.assertEqual(self.health.last_path, "/health")
        self.assertNotIn("Authorization", self.health.last_headers)
        self.assertNotIn("Cookie", self.health.last_headers)
        (mail,) = self.mails()
        self.assertTrue(mail.strip().splitlines()[0].endswith("-- dev@dtcdev.click"), mail)
        self.assertIn("To: dev@dtcdev.click\n", mail)
        self.assertNotIn("orin", mail)
        self.assertIn("Subject: [CRITICAL] Hub start canary (shell) failing at stage health: health_status_bootstrap\n", mail)
        self.assertIn("Failing stage: health\nReason: health_status_bootstrap\n", mail)
        self.assertIn("Consecutive failures: 2\n", mail)
        self.assertIn("Run id: " + verdicts[1]["run_id"], mail)
        self.assertIn("Incident: " + verdicts[1]["run_id"], mail)
        self.assertIn("Session cleanup: not_needed\n", mail)
        self.assertFalse((self.tmp / "REAL_MSMTP_CALLED").exists())
        state = json.loads((self.state / "state-shell.json").read_text())
        self.assertEqual((state["failures"], state["incident_open"]), (2, True))
        # Dedup holds, then the existing two-success recovery closes the incident once.
        self.run_canary("ok", "--mode", "shell")
        self.assertEqual(len(self.mails()), 1)
        self.health.body = json.dumps({"ok": True, "service": "hub2"}).encode()
        for _ in range(2):
            r, v = self.run_canary("ok", "--mode", "shell")
            self.assertEqual((r.returncode, v["ok"]), (0, True), r.stderr)
        mails = self.mails()
        self.assertEqual(len(mails), 2)
        self.assertIn("[RECOVERED] Hub start canary (shell)", mails[1])
        self.assertFalse((self.tmp / "REAL_MSMTP_CALLED").exists())

    def test_healthy_body_allows_full_lifecycle_and_deploy_gate_skips_preflight(self):
        r, v = self.run_canary("ok", "--mode", "shell")
        self.assertEqual((r.returncode, v["ok"], v["stage"], v["cleanup"]), (0, True, None, "not_needed"), r.stderr)
        self.assertEqual(self.health.requests, 1)
        c = self.calls()
        for needle in ("sessions start", "sessions capture " + SID, "sessions stop", "intake update"):
            self.assertIn(needle, c)
        self.assertEqual(list((self.state / "pending").glob("*.json")), [])
        self.assertEqual(r.stderr, "")
        self.health.body = json.dumps({"status": "bootstrap", "dispatch_enabled": False}).encode()
        r, v = self.run_canary("ok", "--deploy-gate", "--mode", "shell")
        self.assertEqual((r.returncode, v["ok"], v["deploy_gate"]), (0, True, True), r.stderr)
        self.assertEqual(self.health.requests, 1)
        self.assertEqual(len(self.mails()), 0)

    def test_health_body_that_is_not_literal_ok_true_fails_closed_with_fixed_reason(self):
        big_ok = json.dumps({"ok": True, "pad": "y" * 5000}).encode()
        cases = [
            (200, b'{"ok": false, "service": "hub2"}', "health_not_ok"),
            (200, b'{"ok": "true"}', "health_not_ok"),
            (200, b'{"ok": 1}', "health_not_ok"),
            (200, b'{"status": "ready", "service": "hub2"}', "health_not_ok"),
            (200, b'{}', "health_not_ok"),
            (200, b'[{"ok": true}]', "health_unparseable_json"),
            (200, b'true', "health_unparseable_json"),
            (200, b'<html>ok</html>', "health_unparseable_json"),
            (200, b'{"ok": true', "health_unparseable_json"),
            (200, b'{"ok": false, "ok": true}', "health_unparseable_json"),
            (200, b'\xff\xfe{"ok": true}', "health_unparseable_json"),
            (200, b'', "health_unparseable_json"),
            (200, big_ok, "health_body_too_large"),
            (503, b'{"ok": true}', "health_http_status"),
            (302, b'{"ok": true}', "health_http_status"),
        ]
        for status, body, reason in cases:
            with self.subTest(status=status, body=body[:40]):
                self.health.status, self.health.body = status, body
                r, v = self.run_canary("ok", "--mode", "shell")
                self.assertEqual((r.returncode, v["ok"], v["stage"], v["reason"]), (1, False, "health", reason))
                self.assertNotIn("sessions start", self.calls())
                self.assert_no_health_leak(r, "<html>", "yyyy")
        self.assertEqual(self.health.requests, len(cases))
        self.assertFalse((self.tmp / "REAL_MSMTP_CALLED").exists())

    def test_health_read_is_bounded_by_total_remaining_active_budget(self):
        # Each byte arrives well inside any per-read timeout; only a total deadline stops it.
        self.health.drip_s = 0.05
        started = time.monotonic()
        r, v = self.run_canary("ok", "--mode", "shell", HUB_CANARY_ACTIVE_BUDGET_S="0.8")
        elapsed = time.monotonic() - started
        self.assertEqual((r.returncode, v["stage"], v["reason"]), (1, "health", "health_timeout"))
        self.assertLess(elapsed, 4.0)
        self.assertNotIn("sessions start", self.calls())
        self.assert_no_health_leak(r)

    def test_health_url_override_is_restricted_and_unreachable_is_sanitized(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            closed_port = probe.getsockname()[1]
        closed = "http://127.0.0.1:%d/health" % closed_port
        for url, reason in (
            ("http://hub.dtcdev.click/health", "health_url_invalid"),
            ("http://10.0.0.1/health", "health_url_invalid"),
            ("http://localhost:1/health", "health_url_invalid"),
            ("https://user:pw@127.0.0.1:1/health", "health_url_invalid"),
            ("%s?x=1" % self.health.url, "health_url_invalid"),
            (self.health.url.replace("/health", "/api/health"), "health_url_invalid"),
            ("ftp://127.0.0.1/health", "health_url_invalid"),
            ("http://127.0.0.1:99999/health", "health_url_invalid"),
            (closed, "health_unreachable"),
        ):
            with self.subTest(url=url):
                r, v = self.run_canary("ok", "--mode", "shell", HUB_CANARY_HEALTH_URL=url)
                self.assertEqual((r.returncode, v["stage"], v["reason"]), (1, "health", reason))
                self.assertNotIn("sessions start", self.calls())
                self.assert_no_health_leak(r, url, "user:pw")
        self.assertEqual(self.health.requests, 0)

    def run_health_in_process(self, url, budget_s):
        """Call Canary.check_health in this test process and record the real request child."""
        sys.path.insert(0, str(ROOT))
        from scripts import hub_start_canary as mod
        children = []
        real_popen = mod.subprocess.Popen

        def recording_popen(*args, **kwargs):
            proc = real_popen(*args, **kwargs)
            children.append(proc)
            return proc

        canary = mod.Canary("shell", "health-inproc")
        canary.active_deadline = time.monotonic() + budget_s
        with mock.patch.dict(os.environ, {"HUB_CANARY_HEALTH_URL": url}), \
                mock.patch.object(mod.subprocess, "Popen", side_effect=recording_popen):
            with self.assertRaises(mod.StageFailure) as caught:
                canary.check_health()
        return caught.exception, children

    def assert_child_killed_and_reaped(self, children):
        self.assertEqual(len(children), 1)
        (child,) = children
        self.assertEqual(child.returncode, -9)  # SIGKILLed at the deadline, and waited for
        self.assertFalse(pathlib.Path(f"/proc/{child.pid}").exists())  # no zombie left behind

    def test_health_blocked_connect_child_is_killed_and_reaped_at_deadline(self):
        import socket as socketmod
        listener = socketmod.socket()
        self.addCleanup(listener.close)
        listener.bind(("127.0.0.1", 0))
        listener.listen(0)
        port = listener.getsockname()[1]
        filler = socketmod.create_connection(("127.0.0.1", port), timeout=1)
        self.addCleanup(filler.close)
        with socketmod.socket() as probe:  # precondition: further connects block (full accept queue)
            probe.settimeout(0.3)
            with self.assertRaises(TimeoutError):
                probe.connect(("127.0.0.1", port))
        failure, children = self.run_health_in_process("http://127.0.0.1:%d/health" % port, 0.6)
        self.assertEqual((failure.stage, failure.reason), ("health", "health_timeout"))
        self.assert_child_killed_and_reaped(children)

    def test_health_http10_close_delimited_slow_drip_is_disconnected_at_deadline(self):
        self.health.drip_s = 0.05
        failure, children = self.run_health_in_process(self.health.url, 0.6)
        self.assertEqual((failure.stage, failure.reason), ("health", "health_timeout"))
        self.assertEqual(self.health.requests, 1)
        self.assert_child_killed_and_reaped(children)
        # The test process is still alive: only the killed request child can have closed the socket.
        self.assertTrue(self.health.disconnected.wait(3))

    def test_production_default_health_url_is_public_hub_endpoint(self):
        sys.path.insert(0, str(ROOT))
        from scripts import hub_start_canary as mod
        self.assertEqual(mod.DEFAULT_HEALTH_URL, "https://hub.dtcdev.click/health")
        self.assertEqual(mod.Canary.parse_health_url(mod.DEFAULT_HEALTH_URL), ("https", "hub.dtcdev.click", None))
        self.assertIn("health", mod.STAGES)
        self.assertLess(mod.STAGES.index("health"), mod.STAGES.index("start"))

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
        self.assertIn('In that same foreground command, print the concatenation of "CANARY-", "OK:", and', prompt)
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

    def helper_sha256(self, directory=None):
        out = subprocess.check_output(
            [sys.executable, str(pathlib.Path(directory or ROOT / "scripts") / "hub_start_canary.py"), "--print-helper-sha256"],
            text=True,
        ).strip()
        self.assertRegex(out, r"^[0-9a-f]{64}$")
        return out

    def copy_sha256(self):
        return self.helper_sha256(self.helper_dir)

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

    def full_candidate_args(self, sha, pyz_sha, token, helper_sha):
        return (
            "--deploy-gate", "--mode", "full",
            "--candidate-sha", sha, "--candidate-cli-sha256", pyz_sha,
            "--run-token", token, "--helper-sha256", helper_sha,
        )

    def test_precommit_full_deploy_gate_binds_candidate_modelless_and_archives_intake(self):
        sha = "3f7fb75992856cbc0792c7ee8ea4cb8ac7354ce7"
        token = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
        helper_sha = self.helper_sha256()
        rel_root, pyz_sha = self.prepare_candidate_release(sha)
        r, v = self.run_canary(
            "ok", *self.full_candidate_args(sha, pyz_sha, token, helper_sha),
            HUB_BIN="/nonexistent/global-hub-must-not-be-used",
            HUB_CANARY_HOST_RELEASE_ROOT=str(rel_root),
            HUB_CANARY_CONVERGE_TIMEOUT_S="1.5",
            FAKE_HOST_VERSION=sha,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((v["ok"], v["mode"], v["deploy_gate"]), (True, "full", True))
        self.assertEqual((v["candidate_sha"], v["run_token"], v["host_version"]), (sha, token, sha))
        self.assertEqual((v["helper_sha256"], v["hub_pyz_sha256"]), (helper_sha, pyz_sha))
        self.assertEqual((v["intake_archived"], v["cleanup"]), (True, "not_needed"))
        c = self.calls()
        self.assertNotIn("--engine", c)
        self.assertNotIn("--model", c)
        self.assertIn(f"{sha[:12]}:{token}", (self.tmp / "prompt").read_text())
        self.assertEqual(
            (self.tmp / "archived_intake").read_text(),
            "intake update intake-canary-1 --status archived --reason deploy_gate_full_canary",
        )
        self.assertEqual(list((self.state / "pending").glob("*.json")), [])

    def test_precommit_full_deploy_gate_failure_and_timeout_are_typed_and_cleaned_up(self):
        sha = "3f7fb75992856cbc0792c7ee8ea4cb8ac7354ce7"
        token = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
        helper_sha = self.helper_sha256()
        for scenario, stage in (("capture_prompt_only", "capture"), ("never_running", "running")):
            with self.subTest(scenario=scenario):
                rel_root, pyz_sha = self.prepare_candidate_release(sha)
                r, v = self.run_canary(
                    scenario, *self.full_candidate_args(sha, pyz_sha, token, helper_sha),
                    HUB_CANARY_HOST_RELEASE_ROOT=str(rel_root),
                    HUB_CANARY_CONVERGE_TIMEOUT_S="1.5",
                    HUB_CANARY_RUNNING_TIMEOUT_S="0.3",
                    HUB_CANARY_CAPTURE_TIMEOUT_S="0.3",
                    FAKE_HOST_VERSION=sha,
                )
                self.assertEqual((r.returncode, v["ok"], v["mode"], v["stage"]), (1, False, "full", stage))
                self.assertEqual(json.loads(r.stderr.strip().splitlines()[-1])["stage"], stage)
                self.assertEqual(self.mails(), [])
                self.assertEqual((v["cleanup"], v["intake_archived"]), ("stopped", True))
                self.assertIn("sessions stop " + SID, self.calls())
                self.assertNotIn("--engine", self.calls())
                self.assertEqual(
                    (self.tmp / "archived_intake").read_text(),
                    "intake update intake-canary-1 --status archived --reason deploy_gate_full_canary",
                )
                self.assertEqual(list((self.state / "pending").glob("*.json")), [])

    def test_precommit_full_deploy_gate_rejects_helper_and_archive_digest_mismatch(self):
        sha = "3f7fb75992856cbc0792c7ee8ea4cb8ac7354ce7"
        token = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
        helper_sha = self.helper_sha256()
        rel_root, pyz_sha = self.prepare_candidate_release(sha)
        r, v = self.run_canary(
            "ok", *self.full_candidate_args(sha, pyz_sha, token, "0" * 64),
            HUB_CANARY_HOST_RELEASE_ROOT=str(rel_root), HUB_CANARY_CONVERGE_TIMEOUT_S="0.2",
        )
        self.assertEqual((r.returncode, v["stage"], v["reason"]), (1, "helper_version", "helper_sha256_mismatch"))
        self.assertEqual(self.calls(), "")
        r, v = self.run_canary(
            "ok", *self.full_candidate_args(sha, "f" * 64, token, helper_sha),
            HUB_CANARY_HOST_RELEASE_ROOT=str(rel_root), HUB_CANARY_CONVERGE_TIMEOUT_S="0.1",
        )
        self.assertEqual((r.returncode, v["stage"], v["reason"]), (1, "host_release", "candidate_hub_pyz_cli_sha_mismatch"))
        self.assertEqual(self.calls(), "")

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

    def test_timer_cadence_is_shell_5min_and_full_30min_as_rendered_and_announced(self):
        import configparser
        installer = (ROOT / "scripts" / "install-hub-start-canary.sh").read_text()
        rendered = {}
        for name in ("hub-start-canary.timer", "hub-start-canary-full.timer"):
            r = subprocess.run(
                ["sed", "-e", "s/@CANARY_USER@/u/", "-e", "s/@CANARY_GROUP@/g/", str(ROOT / "deploy" / name)],
                capture_output=True, text=True, timeout=10)
            self.assertEqual(r.returncode, 0, r.stderr)
            cp = configparser.ConfigParser(strict=False, interpolation=None)
            cp.optionxform = str
            cp.read_string(r.stdout)
            rendered[name] = cp
        shell, full = rendered["hub-start-canary.timer"], rendered["hub-start-canary-full.timer"]
        self.assertEqual(shell["Timer"]["OnUnitActiveSec"], "5min")
        self.assertIn("every 5 minutes", shell["Unit"]["Description"])
        self.assertEqual(full["Timer"]["OnUnitActiveSec"], "30min")
        self.assertIn("every 30 minutes", full["Unit"]["Description"])
        self.assertIn("shell every 5 min, full every 30 min", installer)
        self.assertNotIn("15 min", installer)

    def test_trusted_wrapper_waits_for_existing_lock_longer_than_five_seconds_once(self):
        import fcntl
        sha = "3f7fb75992856cbc0792c7ee8ea4cb8ac7354ce7"
        token = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
        helper_sha = self.copy_sha256()
        rel_root, pyz_sha = self.prepare_candidate_release(sha)
        lib = self.tmp / "waiting-canary-lib"; lib.mkdir()
        for name in ("hub-deploy-gate-forced.sh", "hub-start-canary.sh", "hub_start_canary.py"):
            shutil.copy2(self.helper_dir / name, lib / name)
            (lib / name).chmod(0o755)
        # Do NOT override DEPLOY_GATE_LOCK_WAIT_S: exercise the actual fixed wrapper policy.
        (lib / ".test-harness.env").write_text(
            f'USER_HOME="{self.tmp}"\n'
            f'A_BIN="{sys.executable} -I -S {self.tmp / "fake_a.py"}"\n'
            f'HOST_RELEASE_ROOT="{rel_root}"\nWORKSPACE_DIR="{self.tmp / "ws"}"\nSTATE_DIR="{self.state}"\n'
            f'EXTRA_ENV=(FAKE_DIR="{self.tmp}" FAKE_SCENARIO="ok" FAKE_HOST_VERSION="{sha}" '
            f'HUB_CANARY_MACHINE="HOST-1" HUB_CANARY_REPO="owner/repo" '
            f'HUB_CANARY_POLL_S="0.02" HUB_CANARY_CONVERGE_TIMEOUT_S="1.5" '
            f'HUB_CANARY_RUNNING_TIMEOUT_S="1.5" HUB_CANARY_CAPTURE_TIMEOUT_S="1.5" '
            f'HUB_CANARY_STOP_TIMEOUT_S="1.5" HUB_CANARY_CLEANUP_BUDGET_S="1.5")\n'
        )
        self.state.mkdir(mode=0o700)
        lock = self.state / "deploy-gate.lock"
        fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        before = lock.stat()
        child = subprocess.Popen([str(lib / "hub-deploy-gate-forced.sh")],
            env={"SSH_ORIGINAL_COMMAND": f"hub-deploy-gate --candidate-sha {sha} --candidate-cli-sha256 {pyz_sha} --run-token {token} --helper-sha256 {helper_sha}",
                 "HUB_CANARY_LOCK_WAIT_S": "0", "DEPLOY_GATE_LOCK_WAIT_S": "0"},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        started = time.monotonic()
        try:
            time.sleep(5.2)
            self.assertIsNone(child.poll(), "old five-second refusal must not win")
            self.assertFalse((self.tmp / "started").exists())
            time.sleep(max(0, 6 - (time.monotonic() - started)))
            fcntl.flock(fd, fcntl.LOCK_UN)
            out, err = child.communicate(timeout=15)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            if child.poll() is None:
                child.kill(); child.communicate(timeout=5)
        self.assertEqual(child.returncode, 0, err)
        verdict = json.loads(out)
        self.assertEqual((verdict["ok"], verdict["mode"], verdict["intake_archived"]), (True, "full", True))
        self.assertEqual((verdict["candidate_sha"], verdict["run_token"], verdict["helper_sha256"], verdict["hub_pyz_sha256"]),
                         (sha, token, helper_sha, pyz_sha))
        starts = [a for a in map(json.loads, (self.tmp / "argv.jsonl").read_text().splitlines()) if a[:2] == ["sessions", "start"]]
        self.assertEqual(len(starts), 1)
        self.assertNotIn("--engine", starts[0]); self.assertNotIn("--model", starts[0])
        self.assertIn("sessions stop", self.calls()); self.assertIn("intake update", self.calls())
        self.assertEqual((lock.stat().st_dev, lock.stat().st_ino), (before.st_dev, before.st_ino))

    def test_forced_command_wrapper_validates_ssh_original_command_purges_env_and_locks(self):
        sha = "3f7fb75992856cbc0792c7ee8ea4cb8ac7354ce7"
        token = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
        helper_sha = self.copy_sha256()
        rel_root, pyz_sha = self.prepare_candidate_release(sha)
        lib = self.tmp / "canary-lib"
        lib.mkdir()
        for name in ("hub-deploy-gate-forced.sh", "hub-start-canary.sh", "hub_start_canary.py"):
            dst = lib / name
            shutil.copy2(self.helper_dir / name, dst)
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
            f'HUB_CANARY_STOP_TIMEOUT_S="1.5" HUB_CANARY_CLEANUP_BUDGET_S="1.5" '
            f'HUB_CANARY_HEALTH_URL="{self.health.url}")\n'
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
        self.assertEqual((verdict["mode"], verdict["deploy_gate"]), ("full", True))
        wrapper_calls = self.calls()
        self.assertIn("sessions start", wrapper_calls)
        self.assertNotIn("--engine", wrapper_calls)
        self.assertNotIn("--model", wrapper_calls)

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

    def test_successful_capture_then_natural_exit_requires_positive_zero_exit_retirement(self):
        import copy
        ws=str((self.tmp/'ws').resolve());created=int(time.time()*1000)-1000
        base={'session':{'id':SID,'state':'completed-awaiting-review','intake_id':'intake-natural',
              'project_id':'owner/repo','machine_id':'HOST-1','workspace':ws,'requested_workspace':ws,
              'epoch':2,'operation_id':'op-natural:0','native_id':NATIVE,'created_at':created,
              'evidence':{'session_id':SID,'machine_id':'HOST-1','intake_id':'intake-natural',
                  'native_id':NATIVE,'epoch':2,'operation_id':'op-natural','attempt_operation_id':'op-natural:0',
                  'evidence_digest':'a'*64,'exit_code':0,'native_exit_confirmed':True,
                  'evidence':{'source':'aplexer_retirement','containment_empty':True,
                      'native_identity':{'id':NATIVE,'tag':'task-hub2-'+SID,'workspace':ws},
                      'native_exit':{'code':0,'exited_at_ms':created+500}}}}}
        # Maintained show omits request keys. Existing conflict checks still
        # apply when a key is present; the exact own SID/native/capture is pinned.
        self.assertNotIn('key',base['session']);self.assertNotIn('client_key',base['session'])
        capture=[{'type':'assistant','session_id':NATIVE,'message':{'role':'assistant','content':[
                    {'type':'tool_use','id':'toolu_natural_print','name':'Bash','input':{'command':'printf __MARKER__'}}]}},
                 {'type':'user','session_id':NATIVE,'message':{'role':'user','content':[
                    {'type':'tool_result','tool_use_id':'toolu_natural_print','content':'__MARKER__\n','is_error':False}]},
                  'tool_use_result':{'stdout':'__MARKER__\n','stderr':'','interrupted':False}}]
        (self.tmp/'capture_output.txt').write_text('\n'.join(json.dumps(row) for row in capture)+'\n')
        (self.tmp/'show_payload.json').write_text(json.dumps(base))
        result,verdict=self.run_canary('natural_exit_stop_409','--mode','full',FAKE_ENGINE='claude')
        self.assertEqual((result.returncode,verdict['ok'],verdict['stage']),(0,True,None),verdict)
        calls=[json.loads(line) for line in (self.tmp/'argv.jsonl').read_text().splitlines()]
        stops=[args for args in calls if args[:2]==['sessions','stop']]
        self.assertEqual(len(stops),1,'successful fallback must not issue duplicate STOP')
        self.assertIn('sessions show '+SID,self.calls());self.assertIn('a list --json --running',self.calls())
        self.assertLess(self.calls().index('sessions capture '),self.calls().index('sessions stop '))
        self.assertLess(self.calls().index('sessions stop '),self.calls().index('a list --json --running'))

        cases=[]
        def mutate(name,change):
            payload=copy.deepcopy(base);change(payload['session']);cases.append((name,payload))
        mutate('missing',lambda row:row.update(evidence=None))
        mutate('foreign_sid',lambda row:row.update(id='other-session'))
        mutate('foreign_machine',lambda row:row.update(machine_id='OTHER'))
        mutate('foreign_workspace',lambda row:row.update(workspace='/foreign'))
        mutate('foreign_native',lambda row:row.update(native_id='bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'))
        mutate('foreign_key',lambda row:row.update(client_key='different-key'))
        mutate('conflicting_keys',lambda row:row.update(client_key='one',key='two'))
        mutate('foreign_operation',lambda row:row['evidence'].update(attempt_operation_id='other:0'))
        mutate('failed_state',lambda row:row.update(state='failed'))
        mutate('unknown_state',lambda row:row.update(state='lost'))
        mutate('confirmed_stop_without_finish',lambda row:row.update(state='stopped',stop_confirmed=True,evidence=None))
        for code in [7,None,False]:
            def exit_code(row,code=code):
                row['evidence']['exit_code']=code;row['evidence']['evidence']['native_exit']['code']=code
            mutate('exit_'+str(code),exit_code)
        for name,payload in cases:
            with self.subTest(retirement=name):
                (self.tmp/'show_payload.json').write_text(json.dumps(payload))
                result,verdict=self.run_canary('natural_exit_stop_409','--mode','full',FAKE_ENGINE='claude')
                self.assertEqual((result.returncode,verdict['ok'],verdict['stage']),(1,False,'stop'),verdict)
                for owned in (self.state/'pending').glob('*.json'):owned.unlink()
        (self.tmp/'show_payload.json').write_text(json.dumps(base))
        result,verdict=self.run_canary('natural_exit_stop_409_still_running','--mode','full',FAKE_ENGINE='claude')
        self.assertEqual((result.returncode,verdict['ok'],verdict['stage']),(1,False,'gone'),verdict)
        for owned in (self.state/'pending').glob('*.json'):owned.unlink()
        # Cleanup may confirm a nonzero terminal exit, but cannot rescue capture.
        failed=copy.deepcopy(base);failed['session']['state']='failed'
        failed['session']['evidence']['exit_code']=7;failed['session']['evidence']['evidence']['native_exit']['code']=7
        (self.tmp/'show_payload.json').write_text(json.dumps(failed))
        result,verdict=self.run_canary('capture_fail_stop_409_retired','--mode','full',FAKE_ENGINE='claude')
        self.assertEqual((result.returncode,verdict['ok'],verdict['stage'],verdict['cleanup']),
                         (1,False,'capture','stopped'),verdict)

    def test_success_stop_retirement_reuses_single_response_and_original_deadline(self):
        from scripts import hub_start_canary as mod
        from unittest.mock import patch
        canary=mod.Canary('full','deadline')
        canary.session=SID;canary.native=NATIVE
        deadline=time.monotonic()+10
        response=(1,'','status 409 (session_not_running): completed')
        with patch.object(canary,'stop_session_with_output',return_value=response) as stop, \
                patch.object(canary,'verify_terminal_session_retired',return_value=(True,NATIVE)) as verify, \
                patch.object(canary,'a_running_ids',return_value=set()) as running:
            self.assertEqual(canary.stop(deadline),0)
            canary.wait_gone(deadline)
        stop.assert_called_once_with(SID,canary.key+'-stop',deadline)
        verify.assert_called_once_with(SID,deadline,expected_native_id=NATIVE,expected_key=canary.key,
            expected_repo=canary.repo,expected_machine=canary.machine,expected_workspace=canary.workspace,require_exit_zero=True)
        running.assert_called_once_with(deadline)
        for code in [0,124,2]:
            with self.subTest(code=code),patch.object(canary,'stop_session_with_output',return_value=(code,'','other')) as stop, \
                    patch.object(canary,'verify_terminal_session_retired') as verify:
                self.assertEqual(canary.stop(deadline),code)
                stop.assert_called_once();verify.assert_not_called()

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

    def test_argon_read_file_is_validated_nonproof_around_genuine_run_command(self):
        import copy
        from scripts import hub_acceptance_10 as acc
        from scripts import hub_start_canary as mod
        SESSION = '11111111-2222-4333-8444-555555555555'
        NONCE = 'SYNTHETIC-NONCE:0123456789abcdef'
        WRAP = ('Created At: 2026-01-01T00:00:00+00:00\nCompleted At: 2026-01-01T00:00:01+00:00\n'
                'Command exited with code 0.\n\nCommand output:\n')

        def pair(step, tool, params, output):
            def frame(state, info):
                return {'event': 'step_update', 'step_update': {
                    'conversation_id': SESSION, 'step_index': step, 'state': state,
                    'step_type': 'tool', 'tool_name': tool, 'tool_info': info}}
            return [frame('ACTIVE', {'name': tool, 'parameters': dict(params)}),
                    frame('DONE', {'name': tool, 'parameters': dict(params), 'output': output})]

        def read_file(step, params=None, output='synthetic file text'):
            return pair(step, 'read_file', {'Path': 'synthetic/readme.txt'} if params is None else params, output)

        command = pair(7, 'run_command', {'CommandLine': 'synthetic nonce command'}, WRAP + NONCE + '\n')

        def text(frames):
            return '\n'.join(json.dumps(f, separators=(',', ':')) for f in frames) + '\n'

        for verdict in [lambda t, m: mod.paired_stream_output_ok(t, m, 'antigravity'),
                    lambda t, m: acc.paired_stream_output_ok(t, m, 'antigravity')]:
            # Valid read_file before/after/around the genuine command pair is accepted.
            for frames in (read_file(4) + command, command + read_file(9), read_file(4) + command + read_file(9),
                           read_file(4, {'Path': 'a', 'StartLine': 700}) + command,
                           read_file(4, {'Path': 'a', 'StartLine': 600, 'EndLine': 1400}) + command,
                           read_file(4, {'Path': 'a', 'StartLine': 5, 'EndLine': 5}) + command,
                           read_file(4)[:1] + command):
                self.assertTrue(verdict(text(frames), NONCE), frames)
            # read_file alone never proves, whatever its output or parameters embed.
            fake_command = json.dumps(command[1])
            for output in (NONCE, WRAP + NONCE + '\n', fake_command, NONCE + '\n' + fake_command):
                self.assertFalse(verdict(text(read_file(4, output=output)), NONCE), output)
            self.assertFalse(verdict(text(read_file(4, {'Path': NONCE})), NONCE))
            # A read_file frame never substitutes for or alters the command proof.
            self.assertFalse(verdict(text(read_file(4) + command[:1]), NONCE))
            self.assertFalse(verdict(text(read_file(4, output=WRAP + NONCE + '\n') + command[:1]), NONCE))
            self.assertFalse(verdict(text(read_file(4) + pair(7, 'run_command', {'CommandLine': 'x'}, WRAP + 'other\n')), NONCE))
            # Malformed read_file parameters stay rejected even beside a valid proof.
            bad_params = [
                {}, {'StartLine': 1}, {'Path': ''}, {'Path': 5}, {'Path': None}, {'Path': ['a']}, {'Path': 'a\0b'},
                {'Path': 'a', 'Extra': 1}, {'Path': 'a', 'path': 'b'}, {'Path': 'a', 'CommandLine': 'x'},
                {'Path': 'a', 'StartLine': '1'}, {'Path': 'a', 'StartLine': 1.0}, {'Path': 'a', 'StartLine': True},
                {'Path': 'a', 'StartLine': None}, {'Path': 'a', 'StartLine': -1}, {'Path': 'a', 'StartLine': 2 ** 31},
                {'Path': 'a', 'EndLine': '9'}, {'Path': 'a', 'EndLine': False}, {'Path': 'a', 'EndLine': -3},
                {'Path': 'a', 'StartLine': 10, 'EndLine': 9},
            ]
            for params in bad_params:
                self.assertFalse(verdict(text(read_file(4, params) + command), NONCE), params)
            for bad in ({'Path': 'a'}, {'Path': 'a', 'StartLine': 1}):
                for name in ('other_file_tool', 'Read_File', 'read_files'):
                    frames = pair(4, name, bad, 'x') + command
                    self.assertFalse(verdict(text(frames), NONCE), name)
            # Envelope guards still apply to read_file steps.
            def mutated(change):
                frames = read_file(4) + command
                change(frames)
                return text(frames)
            changes = [
                lambda f: f[1]['step_update']['tool_info'].update(output=None),
                lambda f: f[1]['step_update']['tool_info'].update(output=5),
                lambda f: f[1]['step_update']['tool_info'].pop('output'),
                lambda f: f[0]['step_update']['tool_info'].update(output='early'),
                lambda f: f[1]['step_update']['tool_info']['parameters'].update(Path='changed'),
                lambda f: f[1]['step_update']['tool_info'].update(name='run_command'),
                lambda f: f[1]['step_update']['tool_info'].update(parameters='not-an-object'),
                lambda f: f[1]['step_update'].update(state='ERROR'),
                lambda f: f[1]['step_update'].update(state='FAILED'),
                lambda f: f[1]['step_update'].update(error='synthetic error'),
                lambda f: f[1]['step_update']['tool_info'].update(is_error=True),
                lambda f: f[1].update(interrupted=True),
                lambda f: f[0]['step_update'].update(conversation_id='not-a-uuid'),
                lambda f: f[0]['step_update'].update(step_index=True),
                lambda f: f[0]['step_update'].update(step_index=-1),
                lambda f: f.append(copy.deepcopy(f[0])),
                lambda f: f.append(copy.deepcopy(f[1])),
                lambda f: f.insert(2, copy.deepcopy(f[1])),
            ]
            for change in changes:
                self.assertFalse(verdict(mutated(change), NONCE), mutated(change))
            # Reused step identity across read_file and the command is rejected.
            self.assertFalse(verdict(text(read_file(7) + command), NONCE))
            # Malformed JSON and the capture byte bound are unchanged.
            self.assertFalse(verdict(text(read_file(4) + command) + '{"event":"step_update"\n', NONCE))
            self.assertTrue(verdict('{"clipped partial frame\n' + text(read_file(4) + command), NONCE))
            self.assertFalse(verdict(text(read_file(4, output='x' * 70000) + command), NONCE))

    def test_claude_native_empty_bash_placeholder_is_nonproof_only(self):
        import copy
        PLACEHOLDER = "(Bash completed with no output)"
        session = "11111111-2222-4333-8444-555555555555"
        marker = "SYNTHETIC-MARKER-0001"

        def pair(tool_id, command, content, detail, background=None, **result_overrides):
            params = {"command": command}
            if background is not None:
                params["run_in_background"] = background
            block = {"type": "tool_result", "tool_use_id": tool_id, "content": content, "is_error": False}
            block.update(result_overrides)
            return [
                {"type": "assistant", "session_id": session, "message": {"role": "assistant", "content": [
                    {"type": "tool_use", "id": tool_id, "name": "Bash", "input": params}]}},
                {"type": "user", "session_id": session, "message": {"role": "user", "content": [block]},
                 "tool_use_result": detail},
            ]

        def empty_detail(**changes):
            detail = {"stdout": "", "stderr": "", "interrupted": False, "isImage": False, "noOutputExpected": False}
            detail.update(changes)
            return detail

        def empty(tool_id="toolu_synthetic_empty", **kw):
            return pair(tool_id, "true", kw.pop("content", PLACEHOLDER), kw.pop("detail", empty_detail()), **kw)

        genuine = pair("toolu_synthetic_marker", "python3 synthetic_print.py", marker + "\n",
                       {"stdout": marker + "\n", "stderr": "", "interrupted": False})

        def text(frames):
            return "\n".join(json.dumps(f) for f in frames) + "\n"

        from scripts import hub_acceptance_10 as acc
        from scripts import hub_start_canary as mod
        parsers = (mod.paired_stream_output_ok, acc.paired_stream_output_ok)
        for parser in parsers:
            # Exact paired empty placeholder before or after a complete genuine pair is accepted.
            self.assertTrue(parser(text(empty() + genuine), marker, "claude"))
            self.assertTrue(parser(text(genuine + empty()), marker, "claude"))
            # The placeholder alone is NONPROOF: it never proves.
            self.assertFalse(parser(text(empty()), marker, "claude"))
            self.assertFalse(parser(text(empty() + empty("toolu_synthetic_empty_two")), marker, "claude"))
            # The marker appearing only in the command of an empty-result call is not proof.
            self.assertFalse(parser(text(pair("toolu_synthetic_echo", "echo " + marker + " > /dev/null", PLACEHOLDER, empty_detail())), marker, "claude"))

        def mutated(change):
            frames = empty()
            change(frames)
            return text(frames + genuine)

        def result(frames):
            return frames[1]["message"]["content"][0]

        negatives = {
            "trailing newline": lambda f: result(f).update(content=PLACEHOLDER + "\n"),
            "lowercase": lambda f: result(f).update(content=PLACEHOLDER.lower()),
            "trailing space": lambda f: result(f).update(content=PLACEHOLDER + " "),
            "unknown suffix": lambda f: result(f).update(content=PLACEHOLDER + " extra"),
            "marker suffix": lambda f: result(f).update(content=PLACEHOLDER + "\n" + marker),
            "fake json content": lambda f: result(f).update(content='{"type":"result","result":"' + marker + '"}'),
            "footer content": lambda f: result(f).update(content=PLACEHOLDER + "\nCommand exited with code 0."),
            "nonce content": lambda f: result(f).update(content=marker),
            "nonempty stdout": lambda f: f[1]["tool_use_result"].update(stdout="x"),
            "marker stdout": lambda f: f[1]["tool_use_result"].update(stdout=marker),
            "stderr": lambda f: f[1]["tool_use_result"].update(stderr="synthetic error"),
            "error flag": lambda f: result(f).update(is_error=True),
            "missing error flag": lambda f: result(f).pop("is_error"),
            "interrupted": lambda f: f[1]["tool_use_result"].update(interrupted=True),
            "missing interrupted": lambda f: f[1]["tool_use_result"].pop("interrupted"),
            "image": lambda f: f[1]["tool_use_result"].update(isImage=True),
            "missing image flag": lambda f: f[1]["tool_use_result"].pop("isImage"),
            "no output expected": lambda f: f[1]["tool_use_result"].update(noOutputExpected=True),
            "missing no-output flag": lambda f: f[1]["tool_use_result"].pop("noOutputExpected"),
            "background id": lambda f: f[1]["tool_use_result"].update(backgroundTaskId="bg_synth_01"),
            "wrong session": lambda f: f[1].update(session_id="99999999-2222-4333-8444-555555555555"),
            "wrong role": lambda f: f[1]["message"].update(role="assistant"),
            "missing detail": lambda f: f[1].pop("tool_use_result"),
            "non-object detail": lambda f: f[1].update(tool_use_result=PLACEHOLDER),
            "list content": lambda f: result(f).update(content=[{"type": "text", "text": PLACEHOLDER}]),
            "reused id with genuine": lambda f: (result(f).update(tool_use_id="toolu_synthetic_marker"),
                                                 f[0]["message"]["content"][0].update(id="toolu_synthetic_marker")),
            "duplicate result": lambda f: f.append(copy.deepcopy(f[1])),
            "duplicate use": lambda f: f.append(copy.deepcopy(f[0])),
        }
        for name, change in negatives.items():
            for parser in parsers:
                self.assertFalse(parser(mutated(change), marker, "claude"), name)
        # A background tool use never accepts the foreground empty placeholder.
        for parser in parsers:
            self.assertFalse(parser(text(empty(background=True) + genuine), marker, "claude"))
            self.assertFalse(parser(text(empty(background=True, detail=empty_detail(backgroundTaskId="bg_synth_01")) + genuine), marker, "claude"))
        # Contradictory genuine marker result: placeholder content with marker stdout never proves.
        contradictory = pair("toolu_synthetic_marker", "python3 synthetic_print.py", PLACEHOLDER,
                             {"stdout": marker + "\n", "stderr": "", "interrupted": False})
        for parser in parsers:
            self.assertFalse(parser(text(contradictory), marker, "claude"))
            self.assertFalse(parser(text(empty() + contradictory), marker, "claude"))

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
            expected_hash = "05cdac60e695dc731cfafdfe5832d78bcaa3d5c13e0a7f55430bae008513f830" if inst is c_full else "c5da7e6e3bfb74c35a3228742327f7d3912acbf68d1db101a680cf59c700396a"
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


class TrustedCaptureBudgetTests(unittest.TestCase):
    def canary(self, mode="full", candidate=True, **env):
        from scripts import hub_start_canary as mod
        with mock.patch.dict(os.environ):
            for key in list(os.environ):
                if key.startswith("HUB_CANARY_"):
                    os.environ.pop(key)
            os.environ.update(env)
            return mod.Canary(mode, "capture-budget", deploy_gate=True,
                              candidate_sha="a" * 40 if candidate else None,
                              run_token="b" * 32 if candidate else None)

    def test_full_ceiling_shell_and_periodic_defaults_and_lower_override(self):
        from scripts import hub_start_canary as mod
        self.assertEqual(self.canary().capture_timeout, 120)
        self.assertEqual(self.canary("shell").capture_timeout, 30)
        self.assertEqual(self.canary(candidate=False).capture_timeout, 120)
        self.assertEqual(self.canary("shell", candidate=False).capture_timeout, 30)
        self.assertEqual(self.canary(HUB_CANARY_CAPTURE_TIMEOUT_S="999").capture_timeout, 120)
        self.assertEqual(self.canary("shell", HUB_CANARY_CAPTURE_TIMEOUT_S="999").capture_timeout, 30)
        self.assertEqual(self.canary(HUB_CANARY_CAPTURE_TIMEOUT_S="17").capture_timeout, 17)
        c = self.canary(HUB_CANARY_ACTIVE_BUDGET_S="999", HUB_CANARY_CLEANUP_BUDGET_S="999")
        self.assertEqual((c.active_budget, c.stop_timeout, c.cleanup_budget), (240, 45, 60))
        self.assertEqual((mod.TRUSTED_RUNNING_TIMEOUT_S, mod.TRUSTED_CONVERGE_TIMEOUT_S), (90, 90))

    def test_full_real_marker_after_30_seconds_can_qualify_before_original_clip(self):
        from scripts import hub_start_canary as mod
        c = self.canary(); c.engine = "codex"; c.active_deadline = 1240
        clock = [1000.0]; deadlines = []
        def run(argv, deadline=None):
            deadlines.append(deadline); clock[0] += 20
            output = json.dumps({"type": "item.completed", "item": {"type": "command_execution",
                "status": "completed", "exit_code": 0, "aggregated_output": c.marker + "\n"}}) if clock[0] >= 1060 else json.dumps({"type": "turn.started"})
            return 0, json.dumps({"command_wait": {"state": "acked", "ack": {"capture": output}}}), ""
        with mock.patch.object(mod.time, "monotonic", side_effect=lambda: clock[0]), \
             mock.patch.object(c, "run", side_effect=run), mock.patch.object(c, "pause"):
            c.check_capture()
        self.assertEqual(clock[0], 1060)
        self.assertEqual(deadlines, [1120, 1120, 1120])
        self.assertEqual(c.active_deadline, 1240)

    def test_capture_reserves_constant_45_seconds_without_renewing_active_deadline(self):
        from scripts import hub_start_canary as mod
        c = self.canary(HUB_CANARY_STOP_TIMEOUT_S="1"); c.active_deadline = 240
        clock = [180.0]; deadlines = []
        def run(argv, deadline=None):
            deadlines.append(deadline); clock[0] += 5
            return 0, json.dumps({"command_wait": {"state": "acked", "ack": {"capture": ""}}}), ""
        with mock.patch.object(mod.time, "monotonic", side_effect=lambda: clock[0]), \
             mock.patch.object(c, "run", side_effect=run), mock.patch.object(c, "pause"):
            with self.assertRaisesRegex(mod.StageFailure, "empty_capture"):
                c.check_capture()
        self.assertEqual(deadlines, [195, 195, 195])
        self.assertEqual((clock[0], c.active_deadline), (195, 240))
        # No capture command starts when only the reserved STOP interval remains.
        with mock.patch.object(mod.time, "monotonic", return_value=195), mock.patch.object(c, "run") as run:
            with self.assertRaisesRegex(mod.StageFailure, "no_output"):
                c.check_capture()
            run.assert_not_called()

    def test_no_output_still_fails_at_full_ceiling_without_a_fresh_window(self):
        from scripts import hub_start_canary as mod
        c = self.canary(); c.active_deadline = 240
        clock = [0.0]; deadlines = []
        def run(argv, deadline=None):
            deadlines.append(deadline); clock[0] += 30
            return 0, json.dumps({"command_wait": {"state": "acked", "ack": {"capture": ""}}}), ""
        with mock.patch.object(mod.time, "monotonic", side_effect=lambda: clock[0]), \
             mock.patch.object(c, "run", side_effect=run), mock.patch.object(c, "pause"):
            with self.assertRaisesRegex(mod.StageFailure, "empty_capture"):
                c.check_capture()
        self.assertEqual(deadlines, [120] * 4)
        self.assertEqual((clock[0], c.active_deadline), (120, 240))

    def test_marker_returned_at_clipped_deadline_is_not_a_success(self):
        from scripts import hub_start_canary as mod
        c = self.canary(); c.engine = "codex"; c.active_deadline = 240
        clock = [190.0]
        output = json.dumps({"type": "item.completed", "item": {"type": "command_execution", "aggregated_output": c.marker}})
        def run(argv, deadline=None):
            self.assertEqual(deadline, 195); clock[0] = 195
            return 0, json.dumps({"command_wait": {"state": "acked", "ack": {"capture": output}}}), ""
        with mock.patch.object(mod.time, "monotonic", side_effect=lambda: clock[0]), \
             mock.patch.object(c, "run", side_effect=run), mock.patch.object(c, "pause"):
            with self.assertRaisesRegex(mod.StageFailure, "no_output_beyond_prompt"):
                c.check_capture()
        self.assertEqual(c.active_deadline, 240)

    def test_execute_capture_expiry_retains_cleanup_and_archive_under_original_bounds(self):
        from scripts import hub_start_canary as mod
        c = self.canary(HUB_CANARY_CLEANUP_BUDGET_S="60"); clock = [0.0]; cleanup = []
        def converge(): clock[0] = 180
        def start(): c.start_attempted = True; c.session = SID
        def run(argv, deadline=None):
            self.assertEqual(deadline, 195); clock[0] += 5
            return 0, json.dumps({"command_wait": {"state": "acked", "ack": {"capture": ""}}}), ""
        def clean(deadline=None): cleanup.append(deadline); return "stopped"
        with mock.patch.object(mod.time, "monotonic", side_effect=lambda: clock[0]), \
             mock.patch.object(c, "verify_helper_digest"), mock.patch.object(c, "acquire_deploy_gate_lock"), \
             mock.patch.object(c, "verify_candidate_host_release", side_effect=converge), \
             mock.patch.object(c, "reconcile_prior_pending"), mock.patch.object(c, "start", side_effect=start), \
             mock.patch.object(c, "wait_running"), mock.patch.object(c, "run", side_effect=run), \
             mock.patch.object(c, "pause"), mock.patch.object(c, "cleanup", side_effect=clean), \
             mock.patch.object(c, "archive_intake", return_value=True) as archive, \
             mock.patch.object(c, "clear_pending_state") as clear:
            result = c.execute()
        self.assertEqual((result["ok"], result["stage"], result["reason"], result["cleanup"]),
                         (False, "capture", "empty_capture", "stopped"))
        self.assertEqual((c.active_deadline, clock[0]), (240, 195))
        self.assertEqual(cleanup, [255])  # Original <=60s independent cleanup, not renewed active240.
        archive.assert_called_once_with(deadline=255)
        clear.assert_called_once()


class TrustedLockBudgetTests(unittest.TestCase):
    def canary(self, **env):
        return TrustedCaptureBudgetTests().canary(**env)

    def temporary(self):
        base = pathlib.Path(os.environ.get("TMPDIR") or ROOT / ".local" / "tmp")
        base.mkdir(mode=0o700, parents=True, exist_ok=True)
        return tempfile.TemporaryDirectory(prefix="trusted-lock-", dir=base)

    def test_trusted_wait_caps_sixty_and_periodic_default_remains_one_twenty(self):
        from scripts import hub_start_canary as mod
        self.assertEqual(self.canary().lock_wait, 60)
        self.assertEqual(self.canary(HUB_CANARY_LOCK_WAIT_S="999").lock_wait, 60)
        self.assertEqual(self.canary(HUB_CANARY_LOCK_WAIT_S="0.1").lock_wait, 0.1)
        self.assertEqual(TrustedCaptureBudgetTests().canary(candidate=False).lock_wait, 120)
        c = self.canary(HUB_CANARY_CLEANUP_BUDGET_S="999")
        self.assertEqual((c.active_budget, c.stop_timeout, c.cleanup_budget, c.capture_timeout), (240, 45, 60, 120))
        self.assertEqual(mod.TRUSTED_RUNNING_TIMEOUT_S, 90)

    def test_existing_lock_acquisition_can_wait_sixteen_seconds_without_new_lock(self):
        from scripts import hub_start_canary as mod
        import fcntl
        clock = [0.0]; calls = []
        def flock(fd, flags):
            calls.append((fd, flags))
            if clock[0] < 16: raise BlockingIOError()
        def sleep(seconds): clock[0] += seconds
        with self.temporary() as root:
            path = pathlib.Path(root) / "deploy-gate.lock"
            with mock.patch.object(mod.time, "monotonic", side_effect=lambda: clock[0]), \
                 mock.patch.object(mod.time, "sleep", side_effect=sleep), mock.patch.object(mod.fcntl, "flock", side_effect=flock):
                fd = mod.acquire_lock_fd(path, self.canary().lock_wait)
            try:
                self.assertGreaterEqual(clock[0], 16); self.assertLess(clock[0], 16.1)
                self.assertEqual(set(x[0] for x in calls), {fd})
                self.assertEqual(set(x[1] for x in calls), {fcntl.LOCK_EX | fcntl.LOCK_NB})
                self.assertEqual(os.fstat(fd).st_ino, path.stat().st_ino)
            finally: os.close(fd)

    def test_persistent_busy_expires_at_sixty_with_zero_start_effects(self):
        from scripts import hub_start_canary as mod
        c = self.canary(); clock = [0.0]
        def sleep(seconds): clock[0] += seconds
        with self.temporary() as root, \
             mock.patch.object(mod, "raw_state_dir_path", return_value=pathlib.Path(root)), \
             mock.patch.object(mod.time, "monotonic", side_effect=lambda: clock[0]), \
             mock.patch.object(mod.time, "sleep", side_effect=sleep), \
             mock.patch.object(mod.fcntl, "flock", side_effect=BlockingIOError), \
             mock.patch.object(c, "verify_helper_digest"), mock.patch.object(c, "verify_candidate_host_release") as converge, \
             mock.patch.object(c, "start") as start, mock.patch.object(c, "cleanup") as cleanup:
            result = c.execute()
            self.assertTrue((pathlib.Path(root) / "deploy-gate.lock").is_file())
            self.assertEqual((result["ok"], result["stage"], result["reason"], result["session_id"], result["cleanup"]),
                             (False, "lock", "deploy_gate_lock_busy", None, "not_needed"))
            self.assertEqual((clock[0], c.active_deadline, c.start_attempted), (60, 240, False))
            converge.assert_not_called(); start.assert_not_called(); cleanup.assert_not_called()

    def test_wait_is_clipped_to_original_active_remaining_and_never_renews_it(self):
        from scripts import hub_start_canary as mod
        c = self.canary(); c.active_deadline = 240; clock = [225.0]
        def sleep(seconds): clock[0] += seconds
        with self.temporary() as root, \
             mock.patch.object(mod, "raw_state_dir_path", return_value=pathlib.Path(root)), \
             mock.patch.object(mod.time, "monotonic", side_effect=lambda: clock[0]), \
             mock.patch.object(mod.time, "sleep", side_effect=sleep), \
             mock.patch.object(mod.fcntl, "flock", side_effect=BlockingIOError):
            with self.assertRaisesRegex(mod.StageFailure, "deploy_gate_lock_busy"):
                c.acquire_deploy_gate_lock()
            self.assertEqual((clock[0], c.active_deadline, c.stage_deadline(90)), (240, 240, 240))
            self.assertIsNone(c.lock_fd); self.assertFalse(c.start_attempted)


class FirstCallCanaryProofTests(unittest.TestCase):
    def canary(self, candidate=True):
        return TrustedCaptureBudgetTests().canary(candidate=candidate)

    def test_nonclaude_prompt_combines_private_first_action_before_marker_not_hold(self):
        for candidate in (False, True):
            with self.subTest(candidate=candidate):
                c=self.canary(candidate);nonclaude=c.prompt.split('For every other runtime, follow this unchanged sequence: ',1)[1]
                self.assertIn('begin that same command with the required `aplexer whoami --json`',nonclaude)
                self.assertIn('complete stdout redirected only to that exact private evidence path',nonclaude)
                self.assertIn('use `&&` before the marker operation',nonclaude)
                self.assertIn('This foreground command must output only the marker',nonclaude)
                self.assertIn('run a separate foreground `sleep 60` command',nonclaude)
                self.assertIn('Do not chain the marker print with the hold',nonclaude)
                self.assertNotIn(c.marker,c.prompt)
                self.assertIn('run_in_background: true',c.prompt)
                self.assertIn('provider-native blocking Monitor or TaskOutput',c.prompt)

    def packet(self, command, content, *, success=True, code=0):
        observation={'kind':'result','native_event':'tool_call_result','native_session_id':'native-source-session',
                     'native_turn_id':'native-source-turn','call_id':'call-1','tool':'Bash','command':command,'content':content,
                     'native_success':success,'exit_code':code,'status':'completed','timed_out':False,
                     'native_truncated':False,'native_original_bytes':len(content.encode()),'native_returned_bytes':len(content.encode()),
                     'local_truncated':False,'observation_complete':True}
        return [{'type':'thread.started','thread_id':'source-thread'},
                {'type':'item.completed','item':{'id':'result-1','type':'native_tool_result','thread_id':'source-thread',
                                               'turn_id':'source-turn','observation':observation}}]

    def combined_command(self, directory, c, *, fail=False):
        import shlex
        directory=pathlib.Path(directory);binary=directory/'bin';binary.mkdir(mode=0o700)
        fake=binary/'aplexer';identity='{"id":"owned-synthetic-native","phase":"running"}\n'
        fake.write_text('#!/bin/sh\n'+('exit 7\n' if fail else "printf '%s\\n' "+shlex.quote(identity.rstrip('\n'))+'\n'));fake.chmod(0o700)
        evidence=directory/'private first-action.json'
        suffix=c.marker.removeprefix('CANARY-OK:')
        command='aplexer whoami --json > '+shlex.quote(str(evidence))+" && printf '%s%s%s' CANARY- OK: "+shlex.quote(suffix)
        env={'PATH':str(binary)+':/usr/bin:/bin','HOME':str(directory),'TMPDIR':str(directory),'LANG':'C.UTF-8'}
        result=subprocess.run(['/bin/sh','-c',command],cwd=directory,env=env,capture_output=True,text=True,timeout=3,check=False,umask=0o077)
        return command,result,evidence,identity

    def temporary(self):
        parent=pathlib.Path(os.environ.get('TMPDIR') or ROOT/'.local/tmp');parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        return tempfile.TemporaryDirectory(dir=parent,prefix='first-call-proof-')

    def test_one_actual_fake_cli_command_writes_private_identity_then_marker_only_and_parser_accepts(self):
        from scripts import hub_start_canary as mod
        with self.temporary()as directory:
            c=self.canary();command,result,evidence,identity=self.combined_command(directory,c)
            self.assertEqual((result.returncode,result.stderr,result.stdout),(0,'',c.marker))
            self.assertEqual(evidence.read_text(),identity)
            self.assertEqual(evidence.stat().st_mode & 0o077,0)
            packet=self.packet(command,result.stdout)
            proof=mod.zai_native_return_proof('\n'.join(json.dumps(e)for e in packet),c.marker)
            self.assertTrue(proof['proven']);self.assertFalse(proof['invalidated'])
            self.assertTrue(c.output_has_marker('\n'.join(json.dumps(e)for e in packet),engine='zai'))

    def test_first_action_failure_cannot_print_marker_or_qualify(self):
        from scripts import hub_start_canary as mod
        with self.temporary()as directory:
            c=self.canary();command,result,evidence,_=self.combined_command(directory,c,fail=True)
            self.assertEqual(result.returncode,7);self.assertEqual(result.stdout,'');self.assertTrue(evidence.exists())
            packet=self.packet(command,result.stdout,success=False,code=7)
            self.assertFalse(mod.zai_native_return_proof('\n'.join(json.dumps(e)for e in packet),c.marker)['proven'])

    def test_identity_stdout_extra_prose_echo_and_invalid_native_result_remain_nonproof(self):
        from scripts import hub_start_canary as mod
        import copy
        c=self.canary();command='aplexer whoami --json > OWNED_FIRST_ACTION && printf SPLIT_MARKER'
        for content in ('{"id":"synthetic"}\n'+c.marker,c.marker+'\nextra',c.marker+'\n'):
            with self.subTest(extra_stdout=content!=c.marker):
                packet=self.packet(command,content)
                self.assertFalse(mod.zai_native_return_proof('\n'.join(json.dumps(e)for e in packet),c.marker)['proven'])
        for change in ({'exit_code':1},{'timed_out':True},{'native_truncated':True},{'observation_complete':False},{'native_session_id':'foreign'}):
            with self.subTest(change=change):
                packet=self.packet(command,c.marker);packet[-1]['item']['observation'].update(change)
                self.assertFalse(mod.zai_native_return_proof('\n'.join(json.dumps(e)for e in packet),c.marker,('source-thread','source-turn','native-source-session'))['proven'])
        echo=json.dumps({'type':'item.completed','item':{'id':'message','type':'agent_message','text':c.marker}})
        self.assertFalse(self.canary().output_has_marker(echo,engine='zai'))


if __name__ == "__main__":
    unittest.main()


GIB = 1024 ** 3
MIB = 1024 ** 2


def synthetic_proc(root, *, boot_id="boot-1", rows=()):
    """Write a synthetic /proc + cgroup tree; rows: dict(pid, cgroup, start_s_ago, anon, current, rss_anon, members)."""
    proc, cg = root / "proc", root / "cgroup"
    (proc / "sys/kernel/random").mkdir(parents=True, exist_ok=True)
    (proc / "sys/kernel/random/boot_id").write_text(boot_id + "\n")
    (proc / "self/ns").mkdir(parents=True, exist_ok=True)
    (proc / "self/ns/cgroup").write_text("")
    (proc / "self/ns/mnt").write_text("")
    for row in rows:
        pid = row["pid"]
        (proc / str(pid)).mkdir(parents=True, exist_ok=True)
        ticks = int((1000.0 - row["age"]) * os.sysconf("SC_CLK_TCK"))
        (proc / str(pid) / "stat").write_text(f"{pid} (worker x) S " + " ".join(["0"] * 18 + [str(ticks)] + ["0"] * 10) + "\n")
        (proc / str(pid) / "cgroup").write_text(f"0::{row['cgroup']}\n")
        (proc / str(pid) / "status").write_text(f"Name:\tw\nRssAnon:\t{row['rss_anon'] // 1024} kB\n")
        group = cg / row["cgroup"].lstrip("/")
        group.mkdir(parents=True, exist_ok=True)
        (group / "cgroup.procs").write_text("\n".join(str(m) for m in row.get("members", [pid])) + "\n")
        (group / "memory.stat").write_text(f"anon {row['anon']}\nfile 4096\n")
        (group / "memory.current").write_text(f"{row['current']}\n")
    return proc, cg


def census_row(n, pid, cgroup, *, boot_id="boot-1", cap=1500 * MIB, **extra):
    row = {
        "id": f"aaaaaaaa-0000-4000-8000-{n:012d}", "tag": f"task-{n}", "workspace": "/w", "phase": "running", "state": "running",
        "worker_alive": True, "workload_pid": pid, "workload_cgroup": cgroup, "containment_empty": False,
        "containment_cgroup_identity": {"boot_id": boot_id}, "limits": {"memory_bytes": cap, "pids": 256},
    }
    row.update(extra)
    return row


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        CanaryTest.setUp(self)
        self._restore_inprocess()  # these tests exercise the real collectors/arithmetic in-process

    prepare_candidate_release = CanaryTest.prepare_candidate_release
    run_canary = CanaryTest.run_canary
    shim_inprocess = CanaryTest.shim_inprocess
    calls = CanaryTest.calls
    helper_sha256 = CanaryTest.helper_sha256
    copy_sha256 = CanaryTest.copy_sha256

    def mod(self):
        sys.path.insert(0, str(ROOT))
        from scripts import hub_start_canary as mod
        return mod

    def cfg(self, **values):
        (self.tmp / "admission.json").write_text(json.dumps(values))

    def write_config(self, workspace, mode=0o644, raw=None):
        directory = self.tmp / "etc-agent-hub"
        directory.mkdir(exist_ok=True)
        directory.chmod(0o755)
        path = directory / "canary-workspace.json"
        path.unlink(missing_ok=True)
        path.write_text(raw if raw is not None else json.dumps({"workspace": str(workspace)}))
        path.chmod(mode)
        return path

    def starts(self):
        path = self.tmp / "argv.jsonl"
        if not path.exists():
            return []
        return [a for a in map(json.loads, path.read_text().splitlines()) if a[:2] == ["sessions", "start"]]

    def canary(self, mode="shell", **patches):
        """Real Canary with real admission arithmetic; the pure-collector seams are replaced per test."""
        mod = self.mod()
        c = mod.Canary(mode, "adm-test")
        c.active_deadline = time.monotonic() + 30
        c.workspace = str(self.tmp / "ws")
        c.machine = "HOST-1"
        defaults = dict(
            fetch_disk_floor_gb=lambda deadline: 1,
            acquire_quota=lambda deadline: None,
            read_census=lambda deadline: [],
            read_hub_rows=lambda deadline: [],
            read_launcher_caps=lambda deadline: (1500 * MIB, 256),
            sample_mem_available=lambda: 64 * GIB,
        )
        defaults.update(patches)
        for name, fn in defaults.items():
            setattr(c, name, fn)
        return c

    def assertHold(self, canary, reason, **kwargs):
        with self.assertRaises(self.mod().StageFailure) as caught:
            canary.admit(**kwargs)
        self.assertEqual((caught.exception.stage, caught.exception.reason), ("admission", reason))

    # ---- root workspace config -------------------------------------------------

    def test_root_config_overrides_hostile_environment_and_is_used_for_start(self):
        data = self.tmp / "data-ws"
        self.write_config(data)
        hostile = self.tmp / "hostile-ws"
        r, v = self.run_canary("ok", "--mode", "shell", HUB_CANARY_WORKSPACE=str(hostile))
        self.assertEqual(r.returncode, 0, r.stderr)
        starts = self.starts()
        self.assertEqual(len(starts), 1)
        self.assertEqual(starts[0][starts[0].index("--workspace") + 1], str(data))
        self.assertTrue(data.is_dir() and not hostile.exists())

    def test_unchanged_forced_wrapper_reaches_root_configured_workspace(self):
        sha, token = "3f7fb75992856cbc0792c7ee8ea4cb8ac7354ce7", "a1b2c3d4e5f60718293a4b5c6d7e8f90"
        helper_sha = self.copy_sha256()
        rel_root, pyz_sha = self.prepare_candidate_release(sha)
        lib = self.tmp / "forced-lib"
        lib.mkdir()
        for name in ("hub-deploy-gate-forced.sh", "hub-start-canary.sh", "hub_start_canary.py"):
            shutil.copy2(self.helper_dir / name, lib / name)
        self.assertEqual((lib / "hub-deploy-gate-forced.sh").read_bytes(), (ROOT / "scripts" / "hub-deploy-gate-forced.sh").read_bytes())
        self.assertEqual((lib / "hub-start-canary.sh").read_bytes(), (ROOT / "scripts" / "hub-start-canary.sh").read_bytes())
        data, hostile = self.tmp / "data-ws", self.tmp / "hostile-wrapper-ws"
        self.write_config(data)
        (lib / ".test-harness.env").write_text(
            f'USER_HOME="{self.tmp}"\nA_BIN="{sys.executable} -I -S {self.tmp / "fake_a.py"}"\n'
            f'HOST_RELEASE_ROOT="{rel_root}"\nWORKSPACE_DIR="{hostile}"\nSTATE_DIR="{self.state}"\nDEPLOY_GATE_LOCK_WAIT_S="0"\n'
            f'EXTRA_ENV=(FAKE_DIR="{self.tmp}" FAKE_SCENARIO="ok" FAKE_HOST_VERSION="{sha}" HUB_CANARY_MACHINE="HOST-1" '
            f'HUB_CANARY_REPO="owner/repo" HUB_CANARY_POLL_S="0.02" HUB_CANARY_CONVERGE_TIMEOUT_S="1.5" '
            f'HUB_CANARY_RUNNING_TIMEOUT_S="1.5" HUB_CANARY_CAPTURE_TIMEOUT_S="1.5" HUB_CANARY_STOP_TIMEOUT_S="1.5" '
            f'HUB_CANARY_CLEANUP_BUDGET_S="1.5" HUB_CANARY_HEALTH_URL="{self.health.url}")\n'
        )
        command = (f"hub-deploy-gate --candidate-sha {sha} --candidate-cli-sha256 {pyz_sha} "
                   f"--run-token {token} --helper-sha256 {helper_sha}")
        r = subprocess.run([str(lib / "hub-deploy-gate-forced.sh")], env={"SSH_ORIGINAL_COMMAND": command},
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        starts = self.starts()
        self.assertEqual([a[a.index("--workspace") + 1] for a in starts], [str(data)])
        self.assertFalse(hostile.exists())

    def test_invalid_present_config_fails_closed_without_fallback_or_start(self):
        data = self.tmp / "data-ws"
        cases = {
            "workspace_config_json": dict(raw="{not json"),
            "workspace_config_schema": dict(raw=json.dumps({"workspace": str(data), "extra": 1})),
            "workspace_config_json#dup": dict(raw='{"workspace": "/a", "workspace": "/b"}'),
            "workspace_config_schema#rel": dict(raw=json.dumps({"workspace": "relative/ws"})),
            "workspace_config_schema#dots": dict(raw=json.dumps({"workspace": str(data) + "/../x"})),
            "workspace_config_writable": dict(workspace=data, mode=0o664),
            "workspace_config_size": dict(raw=json.dumps({"workspace": "/" + "a" * 5000})),
        }
        for label, kwargs in cases.items():
            with self.subTest(label):
                kwargs = dict(kwargs)
                self.write_config(kwargs.pop("workspace", data), **kwargs)
                (self.tmp / "argv.jsonl").unlink(missing_ok=True)
                r, v = self.run_canary("ok", "--mode", "shell", HUB_CANARY_WORKSPACE=str(self.tmp / "fallback-ws"))
                self.assertEqual((r.returncode, v["stage"], v["reason"]), (1, "admission", label.split("#")[0]), r.stdout)
                self.assertEqual(self.starts(), [])
                self.assertFalse((self.tmp / "fallback-ws").exists() or data.exists())

    def test_config_symlink_wrong_owner_hardlink_and_bad_parent_refuse(self):
        mod = self.mod()
        real = self.tmp / "real.json"
        real.write_text(json.dumps({"workspace": str(self.tmp / "w")}))
        directory = self.tmp / "etc-agent-hub"
        directory.mkdir()
        directory.chmod(0o755)
        link = directory / "canary-workspace.json"
        link.symlink_to(real)
        with mock.patch.dict(os.environ, {"FAKE_DIR": str(self.tmp)}):
            self.assertEqual(self.reason(mod), "workspace_config_symlink")
            link.unlink()
            link.write_text(real.read_text())
            link.chmod(0o644)
            os.link(link, self.tmp / "second-link")
            self.assertEqual(self.reason(mod), "workspace_config_nlink")
            (self.tmp / "second-link").unlink()
            with mock.patch.object(mod, "_nofollow_ancestors", return_value=[]):  # file owner only (ancestors are user-owned here)
                self.assertEqual(self.reason(mod, uid=os.geteuid() + 1), "workspace_config_owner")
            directory.chmod(0o775)
            self.assertEqual(self.reason(mod), "workspace_config_parent")
            directory.chmod(0o755)
            with mock.patch.object(mod, "WORKSPACE_CONFIG_UID", os.geteuid()), \
                    mock.patch.object(mod, "WORKSPACE_CONFIG_TRUST_ROOT", str(self.tmp)):
                self.assertEqual(mod.read_workspace_config(str(link)), str(self.tmp / "w"))

    def reason(self, mod, uid=None):
        link = self.tmp / "etc-agent-hub" / "canary-workspace.json"
        with mock.patch.object(mod, "WORKSPACE_CONFIG_TRUST_ROOT", str(self.tmp)), \
                mock.patch.object(mod, "WORKSPACE_CONFIG_UID", os.geteuid() if uid is None else uid):
            with self.assertRaises(mod.StageFailure) as caught:
                mod.read_workspace_config(str(link))
        return caught.exception.reason

    def test_absent_config_keeps_env_fallback_but_still_requires_admission(self):
        r, v = self.run_canary("ok", "--mode", "shell")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(self.starts()), 1)
        self.cfg(floor_gb="unavailable")
        (self.tmp / "argv.jsonl").unlink()
        r, v = self.run_canary("ok", "--mode", "shell")
        self.assertEqual((r.returncode, v["stage"], v["reason"]), (1, "admission", "resource_settings_unavailable"))
        self.assertEqual(self.starts(), [])
        self.assertEqual(list((self.state / "pending").glob("*.json")) if (self.state / "pending").exists() else [], [])
        self.assertEqual(v["cleanup"], "not_needed")

    # ---- admission controls ----------------------------------------------------

    def test_memory_boundary_after_known_inflight_and_new_estimate(self):
        mod = self.mod()
        inflight = [{"id": "s1", "state": "starting", "machine_id": "HOST-1", "native_id": None, "workspace": str(self.tmp)}]
        need = 10 * GIB + 1500 * MIB + 1500 * MIB
        ok = self.canary(read_hub_rows=lambda d: inflight, sample_mem_available=lambda: need)
        ok.admit()
        self.assertHold(self.canary(read_hub_rows=lambda d: inflight, sample_mem_available=lambda: need - 1), "memory_below_floor")
        self.assertHold(self.canary(sample_mem_available=lambda: 10 * GIB + 1500 * MIB - 1), "memory_below_floor")
        self.canary(sample_mem_available=lambda: 10 * GIB + 1500 * MIB).admit()

    def test_meminfo_missing_nan_bool_and_malformed_refuse(self):
        mod = self.mod()
        for text in ("", "MemAvailable: nan kB", "MemAvailable: 12", "MemAvailable: -5 kB", "MemAvailable: 1.5 kB", "MemTotal: 5 kB"):
            self.assertIsNone(mod.parse_meminfo_available(text), text)
        self.assertEqual(mod.parse_meminfo_available("MemAvailable:  2048 kB\n"), 2048 * 1024)
        root = self.tmp / "emptyproc"
        root.mkdir()
        c = mod.Canary("shell", "x")
        with mock.patch.object(mod, "PROC_ROOT", str(root)):
            with self.assertRaises(mod.StageFailure) as caught:
                c.sample_mem_available()
        self.assertEqual(caught.exception.reason, "mem_available_unknown")

    def test_final_memory_sample_is_taken_after_slow_reads(self):
        samples = []
        order = []
        c = self.canary(
            fetch_disk_floor_gb=lambda d: order.append("settings") or 1,
            acquire_quota=lambda d: order.append("quota"),
            read_census=lambda d: order.append("census") or [],
            sample_mem_available=lambda: order.append("mem") or samples.append(1) or 64 * GIB,
        )
        c.admit()
        self.assertEqual(order[-1], "mem")
        self.assertEqual(samples, [1])
        self.assertLess(max(order.index(x) for x in ("settings", "quota", "census")), order.index("mem"))

    def test_inflight_projection_charges_uncovered_once_and_unknown_state_holds(self):
        mod = self.mod()
        c = self.canary()
        native = "aaaaaaaa-0000-4000-8000-000000000001"
        rows = [
            {"id": "a", "state": "starting", "machine_id": "HOST-1", "native_id": None},
            {"id": "b", "state": "running", "machine_id": "HOST-1", "native_id": native},
            {"id": "c", "state": "stopped", "machine_id": "HOST-1", "native_id": None},
            {"id": "d", "state": "queued", "machine_id": "OTHER", "native_id": None},
            {"id": "e", "state": "lost", "machine_id": "HOST-1", "native_id": None},
        ]
        self.assertEqual(c.uncovered_inflight_reserve(rows, {native}), 1500 * MIB)
        with self.assertRaises(mod.StageFailure) as caught:
            c.uncovered_inflight_reserve([{"id": "z", "state": "mystery", "machine_id": "HOST-1"}], set())
        self.assertEqual(caught.exception.reason, "managed_inflight_unknown_state")

    def residency(self, rows, census, *, mem, boot_id="boot-1", caps=True, hub_rows=()):
        mod = self.mod()
        proc, cg = synthetic_proc(self.tmp, boot_id="boot-1", rows=rows)
        c = self.canary(read_census=lambda d: census, read_hub_rows=lambda d: list(hub_rows), sample_mem_available=lambda: mem)
        c.boottime = lambda: 1000.0
        stack = mock.patch.multiple(mod, PROC_ROOT=str(proc), CGROUP_ROOT=str(cg))
        stack.start()
        self.addCleanup(stack.stop)
        return c

    def test_mature_resident_reserves_nothing_even_with_unbounded_memory_max(self):
        row = dict(pid=4242, cgroup="/user.slice/w1.scope", age=400, anon=300 * MIB, current=400 * MIB, rss_anon=300 * MIB)
        census = [census_row(1, 4242, "/user.slice/w1.scope", cap=2 ** 63 - 1)]
        floor_mem = 10 * GIB + 1500 * MIB
        self.residency([row], census, mem=floor_mem).admit()
        # No cap at all (memory.max=max recorded as no finite limit) is still fine when mature and measured.
        census_nocap = [census_row(1, 4242, "/user.slice/w1.scope", limits={})]
        self.residency([row], census_nocap, mem=floor_mem).admit()
        self.assertHold(self.residency([row], census, mem=floor_mem - 1), "memory_below_floor")

    def test_young_resident_reserves_cap_minus_proven_anon_and_unknown_cap_holds(self):
        row = dict(pid=4243, cgroup="/user.slice/w2.scope", age=10, anon=200 * MIB, current=300 * MIB, rss_anon=200 * MIB)
        census = [census_row(2, 4243, "/user.slice/w2.scope")]
        need = 10 * GIB + 1500 * MIB + 1300 * MIB
        self.residency([row], census, mem=need).admit()
        self.assertHold(self.residency([row], census, mem=need - 1), "memory_below_floor")
        nocap = [census_row(2, 4243, "/user.slice/w2.scope", limits={})]
        self.assertHold(self.residency([row], nocap, mem=64 * GIB), "resident_bound_unknown")

    def test_mature_requires_positive_anon_age_boot_and_stable_membership(self):
        base = dict(pid=4244, cgroup="/user.slice/w3.scope", age=500, anon=100 * MIB, current=150 * MIB, rss_anon=100 * MIB)
        census = [census_row(3, 4244, "/user.slice/w3.scope")]
        # zero anonymous memory: not mature, falls back to the full recorded cap
        zero = dict(base, anon=0, rss_anon=0)
        self.assertHold(self.residency([zero], census, mem=10 * GIB + 1500 * MIB + 1500 * MIB - 1), "memory_below_floor")
        # anon above current is inconsistent: conservative cap
        bad = dict(base, anon=200 * MIB, current=150 * MIB)
        self.assertHold(self.residency([bad], census, mem=10 * GIB + 1500 * MIB + 1400 * MIB - 1), "memory_below_floor")
        # wrong kernel boot id, unknown pid/cgroup membership, missing counters: HOLD, never zero
        self.assertHold(self.residency([base], [census_row(3, 4244, "/user.slice/w3.scope", boot_id="old-boot")], mem=64 * GIB), "resident_identity_unknown")
        self.assertHold(self.residency([dict(base, members=[1])], census, mem=64 * GIB), "resident_measurement_unknown")
        self.assertHold(self.residency([], census, mem=64 * GIB), "resident_measurement_unknown")
        self.assertHold(self.residency([base], [census_row(3, None, "/user.slice/w3.scope", phase="running")], mem=64 * GIB), "resident_identity_unknown")

    def test_shared_cgroup_needs_distinct_pids_and_per_pid_anon(self):
        rows = [
            dict(pid=5001, cgroup="/user.slice/shared.scope", age=600, anon=300 * MIB, current=400 * MIB, rss_anon=100 * MIB, members=[5001, 5002]),
            dict(pid=5002, cgroup="/user.slice/shared.scope", age=600, anon=300 * MIB, current=400 * MIB, rss_anon=100 * MIB, members=[5001, 5002]),
        ]
        census = [census_row(10, 5001, "/user.slice/shared.scope"), census_row(11, 5002, "/user.slice/shared.scope")]
        self.residency(rows, census, mem=10 * GIB + 1500 * MIB).admit()
        # one PID without anonymous memory is not credited just because the group has some
        rows[1] = dict(rows[1], rss_anon=0)
        self.assertHold(self.residency(rows, census, mem=10 * GIB + 1500 * MIB), "memory_below_floor")
        # same PID claimed by two census rows is ambiguous identity
        dup = [census_row(10, 5001, "/user.slice/shared.scope"), census_row(11, 5001, "/user.slice/shared.scope")]
        self.assertHold(self.residency(rows, dup, mem=64 * GIB), "resident_identity_ambiguous")

    def test_exited_and_starting_rows(self):
        gone = census_row(20, None, None, phase="exited", state="exited", worker_alive=False, containment_empty=True)
        starting = census_row(21, None, None, phase="starting", state="starting", worker_alive=None)
        self.residency([], [gone], mem=10 * GIB + 1500 * MIB).admit()
        self.assertHold(self.residency([], [starting], mem=10 * GIB + 1500 * MIB + 1500 * MIB - 1), "memory_below_floor")
        self.residency([], [starting], mem=10 * GIB + 1500 * MIB + 1500 * MIB).admit()
        unbounded = census_row(21, None, None, phase="starting", state="starting", worker_alive=None, limits={})
        self.assertHold(self.residency([], [unbounded], mem=64 * GIB), "resident_bound_unknown")

    def test_census_reads_current_inventory_without_history_flag(self):
        mod = self.mod()
        seen = []
        c = mod.Canary("shell", "x")
        c.a = ["a"]
        c.run = lambda argv, **kw: seen.append(argv) or (0, "[]", "")
        self.assertEqual(c.read_census(time.monotonic() + 5), [])
        self.assertEqual(seen, [["a", "list", "--json"]])
        self.assertNotIn("--all", seen[0])

    def test_null_or_absent_historical_terminal_fields_do_not_poison_admission(self):
        floor_mem = 10 * GIB + 1500 * MIB
        dead = []
        for n, extra in enumerate((
            dict(containment_empty=None, workload_pid=123),
            dict(containment_empty=None, workload_pid=None, workload_cgroup=None),
            dict(workload_pid=123),  # containment_empty absent, same stale pid reused by a second row
            dict(containment_empty=None, workload_pid=None, workload_cgroup="/user.slice/gone.scope"),
        ), start=30):
            row = census_row(n, extra.get("workload_pid"), None, phase="exited", state="exited", worker_alive=False, containment_empty=None)
            row.update(extra)
            if "containment_empty" not in extra:
                del row["containment_empty"]
            row.pop("containment_cgroup_identity")
            dead.append(row)
        self.residency([], dead, mem=floor_mem).admit()
        # A named cgroup that still exists but has no members is also not an obligation.
        row = [dict(pid=999, cgroup="/user.slice/empty.scope", age=500, anon=1, current=2, rss_anon=1024, members=[])]
        empty = census_row(40, None, "/user.slice/empty.scope", phase="failed", state="failed", worker_alive=False, containment_empty=None)
        self.residency(row, [empty], mem=floor_mem).admit()

    def test_current_unknown_or_live_rows_still_hold_and_state_alone_never_releases(self):
        floor_mem = 10 * GIB + 1500 * MIB
        cases = {
            "state only, worker_alive unknown": census_row(50, 123, None, phase="exited", state="exited", worker_alive=None, containment_empty=None),
            "state only, worker_alive absent": {k: v for k, v in census_row(51, 123, None, phase="failed", state="failed", containment_empty=None).items() if k != "worker_alive"},
            "dead worker but non-terminal phase": census_row(52, 123, None, phase="running", state="running", worker_alive=False, containment_empty=None),
            "terminal but containment reports members": census_row(53, 123, None, phase="exited", state="exited", worker_alive=False, containment_empty=False),
        }
        for label, row in cases.items():
            with self.subTest(label):
                # No synthetic process for the pid: still a HOLD (never zero), via the identity-bound proof.
                self.assertHold(self.residency([], [row], mem=64 * GIB), "resident_measurement_unknown")
        # Terminal NULL row whose named cgroup still has members is live, not history.
        live = [dict(pid=777, cgroup="/user.slice/live.scope", age=10, anon=100 * MIB, current=120 * MIB, rss_anon=100 * MIB)]
        row = census_row(54, 777, "/user.slice/live.scope", phase="exited", state="exited", worker_alive=False, containment_empty=None)
        self.assertHold(self.residency(live, [row], mem=floor_mem + 1400 * MIB - 1), "memory_below_floor")
        # Live rows keep identity-bound proof: a current worker without pid/identity holds.
        self.assertHold(self.residency([], [census_row(55, None, None)], mem=64 * GIB), "resident_identity_unknown")

    # ---- current coordinator residency fallback -------------------------------------

    BTIME = 1_800_000_000

    def coordinator_world(self, rows, census, *, mem=64 * GIB, hub_rows=(), mutate=None):
        """Synthetic /proc + cgroup tree for workers/workloads, with fresh namespaces, btime and memory.max.

        rows are synthetic_proc rows; extra keys: max (memory.max text, default "max"), ns ("same" or "other").
        """
        mod = self.mod()
        c = self.residency(rows, census, mem=mem, hub_rows=hub_rows)
        proc, cg = self.tmp / "proc", self.tmp / "cgroup"
        (proc / "stat").write_text(f"cpu  1 2 3\nintr 1 2 3\nbtime {self.BTIME}\nprocesses 5\n")
        for row in rows:
            pdir = proc / str(row["pid"]) / "ns"
            pdir.mkdir(parents=True, exist_ok=True)
            for name in ("cgroup", "mnt"):
                link = pdir / name
                link.unlink(missing_ok=True)
                if row.get("ns") == "other":
                    link.write_text("")
                else:
                    link.symlink_to(proc / "self" / "ns" / name)
            (cg / row["cgroup"].lstrip("/") / "memory.max").write_text(row.get("max", "max") + "\n")
        if mutate:
            mutate(proc, cg)
        return c

    def birth_ms(self, age, skew=0.0):
        return int((self.BTIME + (1000.0 - age) + skew) * 1000)

    def worker_row(self, n, pid, cgroup, *, age=400, rss=300 * MIB, anon=300 * MIB, current=400 * MIB, **extra):
        return dict(pid=pid, cgroup=cgroup, age=age, rss_anon=rss, anon=anon, current=current, **extra)

    def coordinator_census(self, n, worker_pid, cgroup, *, age=400, workload_pid=None, skew=0.0, **extra):
        row = census_row(n, workload_pid, "/user.slice/workload-%d.scope" % n, boot_id="old-boot", cap=1500 * MIB,
                         worker_alive=True, worker_pid=worker_pid, worker_cgroup=cgroup, created_at_ms=self.birth_ms(age, skew))
        row.update(extra)
        return row

    def test_exact_current_gap_live_worker_dead_workload_among_sixteen_rows(self):
        rows, census = [], []
        for n in range(15):
            group = f"/user.slice/ok{n}.scope"
            rows.append(self.worker_row(n, 7000 + n, group))
            census.append(census_row(n, 7000 + n, group))
        group = "/user.slice/coord.scope"
        rows.append(self.worker_row(99, 7100, group))
        census.append(self.coordinator_census(99, 7100, group, workload_pid=6999999))
        self.assertEqual(len(census), 16)
        floor = 10 * GIB + 1500 * MIB
        self.coordinator_world(rows, census, mem=floor).admit()
        self.assertHold(self.coordinator_world(rows, census, mem=floor - 1), "memory_below_floor")

    def test_fallback_for_none_or_enoent_workload_and_worker_never_the_workload_scope(self):
        group = "/user.slice/coord.scope"
        rows = [self.worker_row(1, 7100, group)]
        floor = 10 * GIB + 1500 * MIB
        for label, workload_pid in (("none", None), ("enoent", 6999999)):
            with self.subTest(label):
                census = [self.coordinator_census(1, 7100, group, workload_pid=workload_pid)]
                self.coordinator_world(rows, census, mem=floor).admit()
        # A bare workload PID with no worker data is still a HOLD with the original reasons.
        self.assertHold(self.coordinator_world([], [census_row(2, None, None)]), "resident_identity_unknown")
        self.assertHold(self.coordinator_world([], [census_row(2, 6999999, None)]), "resident_measurement_unknown")

    def test_no_fallback_for_other_errors_or_unproven_worker_liveness(self):
        mod = self.mod()
        group, wgroup = "/user.slice/coord.scope", "/user.slice/wl.scope"
        worker = self.worker_row(1, 7100, group)
        workload = self.worker_row(2, 7200, wgroup)

        def census(**extra):
            return [self.coordinator_census(1, 7100, group, workload_pid=7200, containment_cgroup_identity={"boot_id": "boot-1"},
                                            workload_cgroup=wgroup, **extra)]

        # Healthy existing workload: normal path, coordinator never consulted.
        self.coordinator_world([worker, workload], census(), mem=10 * GIB + 1500 * MIB).admit()
        # EACCES on the workload identity open is a HOLD, not a fallback.
        real_open = os.open
        denied = str(self.tmp / "proc" / "7200" / "stat")

        def deny(path, *args, **kwargs):
            if path == denied:
                raise PermissionError(errno.EACCES, "denied")
            return real_open(path, *args, **kwargs)

        c = self.coordinator_world([worker, workload], census(), mem=64 * GIB)
        with mock.patch.object(os, "open", side_effect=deny):
            self.assertHold(c, "resident_measurement_unknown")
        # Existing PID whose cgroup/membership disagrees holds; the live worker does not rescue it.
        self.assertHold(self.coordinator_world([worker, dict(workload, members=[1])], census(), mem=64 * GIB), "resident_measurement_unknown")

        def other_cgroup(proc, cg):
            (proc / "7200" / "cgroup").write_text("0::/user.slice/elsewhere.scope\n")
        self.assertHold(self.coordinator_world([worker, workload], census(), mem=64 * GIB, mutate=other_cgroup), "resident_measurement_unknown")
        # A missing measurement file of an existing workload is not ENOENT at the identity open.
        def drop_stat(proc, cg):
            (cg / "user.slice/wl.scope/memory.stat").unlink()
        self.assertHold(self.coordinator_world([worker, workload], census(), mem=64 * GIB, mutate=drop_stat), "resident_measurement_unknown")
        # ENOENT workload but worker liveness not True, or malformed worker fields.
        gone = 6999999
        for label, extra in (("alive-unknown", dict(worker_alive=None)), ("dead-flag", dict(worker_alive=False)),
                             ("alive-str", dict(worker_alive="true")), ("no-pid", dict(worker_pid=None)),
                             ("bool-pid", dict(worker_pid=True)), ("neg-pid", dict(worker_pid=-5)),
                             ("str-pid", dict(worker_pid="7100")), ("no-group", dict(worker_cgroup=None)),
                             ("rel-group", dict(worker_cgroup="user.slice/coord.scope")),
                             ("dotdot-group", dict(worker_cgroup="/user.slice/../x"))):
            with self.subTest(label):
                row = self.coordinator_census(1, 7100, group, workload_pid=gone)
                row.update(extra)
                self.assertHold(self.coordinator_world([worker], [row], mem=64 * GIB), "resident_measurement_unknown")
        # Invalid workload PID types are never "absent".
        for bad in (True, -4, 0, "7200", 1.5):
            with self.subTest(bad=bad):
                self.assertHold(self.coordinator_world([worker], [self.coordinator_census(1, 7100, group, workload_pid=bad)], mem=64 * GIB),
                                "resident_identity_unknown")

    def test_worker_birth_namespace_cgroup_membership_and_anonymous_failures(self):
        group = "/user.slice/coord.scope"
        floor = 10 * GIB + 1500 * MIB
        base = self.worker_row(1, 7100, group)

        def run(row=base, census_kwargs=None, mutate=None, **kw):
            census = [self.coordinator_census(1, 7100, group, workload_pid=6999999, **(census_kwargs or {}))]
            return self.coordinator_world([row], census, mem=64 * GIB, mutate=mutate)

        # SDK created_at_ms may precede the kernel start by up to 30 s or follow it by up to 2 s; beyond is another incarnation.
        for skew in (-30.0, 0.0, 2.0):
            with self.subTest(skew=skew):
                self.coordinator_world([base], [self.coordinator_census(1, 7100, group, workload_pid=6999999, skew=skew)], mem=floor).admit()
        for skew in (-31.0, 3.0, 3600.0):
            with self.subTest(skew=skew):
                self.assertHold(run(census_kwargs=dict(skew=skew)), "resident_identity_unknown")
        for bad in (None, 0, -1, True, 1.5e12, "1"):
            with self.subTest(created=bad):
                self.assertHold(run(census_kwargs=dict(created_at_ms=bad)), "resident_identity_unknown")
        self.assertHold(self.coordinator_world([dict(base, ns="other")], [self.coordinator_census(1, 7100, group, workload_pid=6999999)]),
                        "resident_identity_unknown")
        self.assertHold(run(mutate=lambda proc, cg: (proc / "7100" / "cgroup").write_text("0::/user.slice/other.scope\n")), "resident_identity_unknown")
        self.assertHold(run(mutate=lambda proc, cg: (proc / "7100" / "cgroup").write_text("0::/user.slice/coord.scope\n0::/x\n")), "resident_identity_unknown")
        self.assertHold(run(row=dict(base, members=[1, 2])), "resident_identity_unknown")
        self.assertHold(run(row=dict(base, rss_anon=0)), "resident_anonymous_unknown")
        self.assertHold(run(row=dict(base, anon=0)), "resident_anonymous_unknown")
        self.assertHold(run(row=dict(base, anon=500 * MIB, current=400 * MIB)), "resident_anonymous_unknown")
        self.assertHold(run(mutate=lambda proc, cg: (proc / "7100" / "stat").write_text("7100 (w) Z " + " ".join(["0"] * 30) + "\n")),
                        "resident_identity_unknown")
        self.assertHold(run(mutate=lambda proc, cg: (cg / "user.slice/coord.scope/memory.current").unlink()), "resident_measurement_unknown")
        self.assertHold(run(mutate=lambda proc, cg: (proc / "stat").write_text("cpu 1\n")), "resident_identity_unknown")
        # Kernel boot differs from the sampled one.
        c = run()
        original = c.sample_coordinator

        def reboot(target, boot_id, ns_ids):
            (self.tmp / "proc/sys/kernel/random/boot_id").write_text("boot-2\n")
            return original(target, boot_id, ns_ids)

        c.sample_coordinator = reboot
        self.assertHold(c, "resident_identity_unknown")

    def test_worker_pid_or_cgroup_replacement_between_samples_and_at_final_check_hold(self):
        group = "/user.slice/coord.scope"
        floor = 10 * GIB + 1500 * MIB
        census = [self.coordinator_census(1, 7100, group, workload_pid=6999999)]

        def fresh():
            return self.coordinator_world([self.worker_row(1, 7100, group)], census, mem=floor)

        def after_sample(n, change):
            c = fresh()
            original, calls = c.sample_coordinator, []

            def wrapped(*args, **kwargs):
                if len(calls) == n:
                    change()
                calls.append(1)
                return original(*args, **kwargs)

            c.sample_coordinator = wrapped
            return c

        proc, cg = self.tmp / "proc", self.tmp / "cgroup"
        ticks = lambda age: int((1000.0 - age) * os.sysconf("SC_CLK_TCK"))

        def restart():
            (proc / "7100/stat").write_text("7100 (w) S " + " ".join(["0"] * 18 + [str(ticks(5))] + ["0"] * 10) + "\n")

        def replace_dir():
            os.rename(cg / "user.slice/coord.scope", cg / f"user.slice/coord.old{time.monotonic_ns()}")  # keep the old inode alive: no reuse
            for name, text in (("cgroup.procs", "7100\n"), ("memory.stat", f"anon {300 * MIB}\n"), ("memory.current", f"{400 * MIB}\n"), ("memory.max", "max\n")):
                (cg / "user.slice/coord.scope").mkdir(exist_ok=True)
                (cg / "user.slice/coord.scope" / name).write_text(text)

        # second sample vs first, and the final revalidation sample vs the credited identity
        for n, label in ((1, "between samples"), (2, "final")):
            with self.subTest("restart " + label):
                c = after_sample(n, restart)
                self.assertHold(c, "resident_identity_unstable" if n == 1 else "resident_fallback_changed")
        with self.subTest("cgroup replaced at final"):
            self.assertHold(after_sample(2, replace_dir), "resident_fallback_changed")
        with self.subTest("cgroup replaced between samples"):
            self.assertHold(after_sample(1, replace_dir), "resident_identity_unstable")
        with self.subTest("unchanged passes"):
            fresh().admit()

    def test_shared_service_cgroup_needs_per_pid_proof_and_distinct_chosen_pids(self):
        group = "/system.slice/hub.service"
        floor = 10 * GIB + 1500 * MIB
        pair = [self.worker_row(1, 7301, group, members=[7301, 7302, 9999]), self.worker_row(2, 7302, group, members=[7301, 7302, 9999])]
        census = [self.coordinator_census(1, 7301, group, workload_pid=None), self.coordinator_census(2, 7302, group, workload_pid=6999999)]
        self.coordinator_world(pair, census, mem=floor).admit()
        # A unique cgroup map entry still forces per-PID proof: a group that merely has anon memory is not enough.
        solo = [self.worker_row(1, 7301, group, rss=0, members=[7301, 9999])]
        self.assertHold(self.coordinator_world(solo, census[:1], mem=64 * GIB), "resident_anonymous_unknown")
        self.assertHold(self.coordinator_world([pair[0], dict(pair[1], rss_anon=0)], census, mem=64 * GIB), "resident_anonymous_unknown")
        # Duplicate chosen PID, directly or workload-vs-worker, is ambiguous before any proof is read.
        dup = [self.coordinator_census(1, 7301, group), self.coordinator_census(2, 7301, group)]
        self.assertHold(self.coordinator_world(pair, dup, mem=64 * GIB), "resident_identity_ambiguous")
        cross = [census_row(3, 7301, "/system.slice/other.scope"), self.coordinator_census(1, 7301, group)]
        self.assertHold(self.coordinator_world(pair, cross, mem=64 * GIB), "resident_identity_ambiguous")
        # Two workloads sharing a group with a coordinator of another row still need their own PID proof.
        mixed = [census_row(4, 7401, group), self.coordinator_census(1, 7301, group)]
        rows = [self.worker_row(4, 7401, group, members=[7301, 7401]), self.worker_row(1, 7301, group, members=[7301, 7401])]
        self.coordinator_world(rows, mixed, mem=floor).admit()
        self.assertHold(self.coordinator_world([dict(rows[0], rss_anon=0), rows[1]], mixed, mem=floor + 1500 * MIB - 1), "memory_below_floor")

    def test_mature_coordinator_reserves_zero_even_unbounded_but_young_needs_actual_finite_cap(self):
        group = "/user.slice/coord.scope"
        floor = 10 * GIB + 1500 * MIB
        census = lambda age, **extra: [self.coordinator_census(1, 7100, group, age=age, workload_pid=6999999, **extra)]
        mature = lambda **extra: self.worker_row(1, 7100, group, age=181, **extra)
        self.coordinator_world([mature(max="max")], census(181), mem=floor).admit()
        self.assertHold(self.coordinator_world([mature(max="max")], census(181), mem=floor - 1), "memory_below_floor")
        # 179 s is young; memory.max=max is an unknown bound, and the SDK workload 1500 MiB limit is never borrowed.
        young = lambda **extra: self.worker_row(1, 7100, group, age=179, **extra)
        self.assertHold(self.coordinator_world([young(max="max")], census(179), mem=64 * GIB), "resident_bound_unknown")
        # Mature age but not in running state is uncertain.
        self.assertHold(self.coordinator_world([mature(max="max")], census(181, state="starting"), mem=64 * GIB), "resident_bound_unknown")
        self.assertHold(self.coordinator_world([mature(max="max")], census(181, phase="exiting"), mem=64 * GIB), "resident_bound_unknown")
        # Finite kernel cap is charged whole (never divided, never the workload cap), also above 1500 MiB.
        for cap in (800 * MIB, 2000 * MIB):
            with self.subTest(cap=cap):
                need = floor + cap
                self.coordinator_world([young(max=str(cap))], census(179), mem=need).admit()
                self.assertHold(self.coordinator_world([young(max=str(cap))], census(179), mem=need - 1), "memory_below_floor")
        for text in ("0", "-1", "garbage", ""):
            with self.subTest(max=text):
                self.assertHold(self.coordinator_world([young(max=text)], census(179), mem=64 * GIB), "resident_bound_unknown")
        # Shared finite cap is charged conservatively per row.
        shared = "/system.slice/hub.service"
        pair = [self.worker_row(1, 7301, shared, age=10, max=str(500 * MIB), members=[7301, 7302]),
                self.worker_row(2, 7302, shared, age=10, max=str(500 * MIB), members=[7301, 7302])]
        two = [self.coordinator_census(1, 7301, shared, age=10), self.coordinator_census(2, 7302, shared, age=10)]
        self.coordinator_world(pair, two, mem=floor + 1000 * MIB).admit()
        self.assertHold(self.coordinator_world(pair, two, mem=floor + 1000 * MIB - 1), "memory_below_floor")

    def test_fallback_premise_selection_and_census_must_survive_to_final_sampling(self):
        group = "/user.slice/coord.scope"
        rows = [self.worker_row(1, 7100, group)]
        base = self.coordinator_census(1, 7100, group, workload_pid=None)

        def run(final, *, premise_pid=None):
            seen = []
            c = self.coordinator_world(rows, [base], mem=64 * GIB)
            c.read_census = lambda d: seen.append(d) or ([base] if len(seen) == 1 else final)
            return c, seen

        c, seen = run([base])
        c.admit()
        self.assertEqual(len(seen), 2)
        self.assertEqual(seen[0], seen[1])  # same original deadline: not renewed
        changes = {
            "workload appeared": dict(base, workload_pid=7100),
            "workload replaced": dict(base, workload_pid=8888),
            "worker pid changed": dict(base, worker_pid=7101),
            "worker group changed": dict(base, worker_cgroup="/user.slice/other.scope"),
            "created changed": dict(base, created_at_ms=base["created_at_ms"] + 1),
            "worker liveness changed": dict(base, worker_alive=False),
            "limits changed": dict(base, limits={"memory_bytes": 1}),
            "state changed": dict(base, state="stopping"),
        }
        for label, changed in changes.items():
            with self.subTest(label):
                c, _ = run([changed])
                self.assertHold(c, "resident_fallback_changed")
        with self.subTest("row vanished / extra row"):
            c, _ = run([])
            self.assertHold(c, "resident_fallback_changed")
            c, _ = run([base, census_row(5, 7777, "/user.slice/new.scope")])
            self.assertHold(c, "resident_fallback_changed")
        # ENOENT premise: the workload PID reappears in /proc without any census change.
        enoent = self.coordinator_census(1, 7100, group, workload_pid=6999999)
        c = self.coordinator_world(rows, [enoent], mem=64 * GIB)
        count = []

        def appear(deadline):
            count.append(1)
            if len(count) == 2:
                (self.tmp / "proc/6999999").mkdir()
                (self.tmp / "proc/6999999/stat").write_text("6999999 (w) S " + " ".join(["0"] * 30) + "\n")
            return [enoent]

        c.read_census = appear
        self.assertHold(c, "resident_fallback_changed")
        # Non-fallback admissions never take the extra census read.
        plain = [census_row(1, 7200, "/user.slice/wl.scope")]
        seen = []
        c = self.coordinator_world([self.worker_row(1, 7200, "/user.slice/wl.scope")], plain, mem=64 * GIB)
        c.read_census = lambda d: seen.append(d) or plain
        c.admit()
        self.assertEqual(len(seen), 1)

    def test_fallback_keeps_original_deadline_and_replay_seam_hold(self):
        group = "/user.slice/coord.scope"
        rows = [self.worker_row(1, 7100, group)]
        base = self.coordinator_census(1, 7100, group, workload_pid=None)
        c = self.coordinator_world(rows, [base], mem=64 * GIB)
        calls = []
        c.active_deadline = time.monotonic() + 1.0

        def expire(deadline):
            calls.append(1)
            if len(calls) == 2:
                time.sleep(1.1)  # the final slow read consumes the original budget
            return [base]

        c.read_census = expire
        self.assertHold(c, "admission_deadline_exceeded")
        # Replay seam: a changed selection is a refusal that skips the replay and records the typed reason.
        c = self.coordinator_world(rows, [base], mem=64 * GIB)
        c.read_census = lambda d: [base] if not calls.append(2) and calls.count(2) % 2 else [dict(base, workload_pid=7100)]
        calls.clear()
        self.assertFalse(c.admission_allows(time.monotonic() + 5))
        self.assertEqual(c.admission_hold, "resident_fallback_changed")
        # An expired caller deadline stays expired (no renewal) before any proc is read.
        c = self.coordinator_world(rows, [base], mem=64 * GIB)
        self.assertHold(c, "admission_deadline_exceeded", deadline=time.monotonic() - 1)

    def test_birth_window_btime_and_cgroup_helpers(self):
        mod = self.mod()
        hz = 100
        self.assertTrue(mod.birth_matches(10_000, 0, 10, hz))
        self.assertTrue(mod.birth_matches(10_000, 0, 8, hz))
        self.assertTrue(mod.birth_matches(10_000, 0, 40, hz))
        self.assertFalse(mod.birth_matches(10_000, 0, 7, hz))
        self.assertFalse(mod.birth_matches(10_000, 0, 41, hz))
        for bad in (None, 0, -1, True, 1.0, "1"):
            self.assertFalse(mod.birth_matches(bad, 0, 10, hz))
        for good in ("/a/b", "/user.slice/w.scope"):
            self.assertEqual(mod.relative_cgroup(good), good)
        for bad in (None, "", "/", "a/b", "/a//b", "/a/../b", "/a/./b", "/a/", "/a\0b", 5):
            self.assertIsNone(mod.relative_cgroup(bad), bad)
        root = self.tmp / "btime-proc"
        root.mkdir()
        with mock.patch.object(mod, "PROC_ROOT", str(root)):
            (root / "stat").write_text("intr " + "1 " * 100000 + "\nbtime 123456\n")
            self.assertEqual(mod.read_proc_btime(), 123456)
            for text in ("cpu 1\n", "btime 0\n", "btime x\n"):
                (root / "stat").write_text(text)
                with self.assertRaises(ValueError):
                    mod.read_proc_btime()

    # ---- actual selected launcher caps ---------------------------------------------

    def home_with_config(self, raw=None, mode=0o600, **config):
        home = self.tmp / "fakehome"
        shutil.rmtree(home, ignore_errors=True)
        directory = home / ".config" / "agent-hub"
        directory.mkdir(parents=True)
        directory.chmod(0o700)
        body = {"machine_id": "HOST-1", "server": "https://x.invalid", "token": "SECRET-TOKEN-VALUE", "roots": {}, "engines": []}
        body.update(config)
        path = directory / "v2.json"
        path.write_text(raw if raw is not None else json.dumps(body))
        path.chmod(mode)
        return home, path

    def host_process(self, home, *, age=0.0, pid=8801, argv=None, cgroup=None, home_env=None):
        """Synthetic Host daemon (hub2 serve) under the shared fake /proc; started `age` seconds ago."""
        time.sleep(0.05)  # the config was written before the daemon "started"
        proc, _ = synthetic_proc(self.tmp, boot_id="boot-1", rows=[])
        directory = proc / str(pid)
        shutil.rmtree(directory, ignore_errors=True)
        directory.mkdir()
        argv = argv if argv is not None else [str(home / ".local/share/agent-hub2/releases/x/bin/python"),
                                              str(home / ".local/share/agent-hub2/current/bin/hub2"), "serve", "--aplexer", "/bin/a"]
        (directory / "cmdline").write_text("\0".join(argv) + "\0")
        (directory / "cgroup").write_text("0::/user.slice/app.slice/" + (cgroup or "agent-hub2-host.service") + "\n")
        ticks = int((time.clock_gettime(time.CLOCK_BOOTTIME) - age) * os.sysconf("SC_CLK_TCK"))
        (directory / "environ").write_bytes(b"LANG=C\0HOME=" + os.fsencode(home if home_env is None else home_env) + b"\0TOKEN=SECRET-ENV-TOKEN\0")
        (directory / "stat").write_text(f"{pid} (hub2) S " + " ".join(["0"] * 18 + [str(ticks)] + ["0"] * 10) + "\n")
        return proc

    def caps(self, home, machine="HOST-1", host=True):
        mod = self.mod()
        proc = self.host_process(home) if host else (self.tmp / "proc")
        with mock.patch.dict(os.environ, {"HOME": str(home)}), mock.patch.object(mod, "PROC_ROOT", str(proc)):
            return mod.read_launcher_caps(mod.selected_launcher_config_path(), machine)

    def assertCapsHold(self, home, reason, machine="HOST-1"):
        with self.assertRaises(self.mod().StageFailure) as caught:
            self.caps(home, machine)
        self.assertEqual((caught.exception.stage, caught.exception.reason), ("admission", reason))
        self.assertNotIn("SECRET-TOKEN-VALUE", repr(caught.exception.reason))

    def test_launcher_caps_come_from_the_selected_host_config_with_source_defaults(self):
        mod = self.mod()
        home, path = self.home_with_config(launcher={"aplexer": "a", "routes": {}})
        self.assertEqual(self.caps(home), (1500 * MIB, 256))  # keys absent: native_launch.py defaults
        self.assertEqual(self.caps(self.home_with_config(launcher={"memory_mb": 1000, "pids": 128})[0]), (1000 * MIB, 128))
        self.assertEqual(self.caps(self.home_with_config()[0]), (1500 * MIB, 256))  # no launcher key at all
        self.assertEqual(self.caps(self.home_with_config(launcher=None)[0]), (1500 * MIB, 256))  # Host: launcher_config or {}
        self.assertEqual(self.caps(self.home_with_config(launcher={"memory_mb": 1500, "pids": 256})[0]), (1500 * MIB, 256))
        with mock.patch.dict(os.environ, {"HOME": str(home)}):
            self.assertEqual(mod.selected_launcher_config_path(), home / ".config/agent-hub/v2.json")

    def test_launcher_defaults_and_selection_match_the_maintained_host_source(self):
        mod = self.mod()
        launch = (ROOT / "hub2/host/agent_hub_host/native_launch.py").read_text()
        self.assertIn('requested_mb = int(self.config.get("memory_mb", %d))' % mod.LAUNCHER_DEFAULT_MEMORY_MB, launch)
        self.assertIn('"--pids", str(self.config.get("pids", %d))' % mod.LAUNCHER_DEFAULT_PIDS, launch)
        self.assertIn('"--memory", f"{requested_mb}M"', launch)
        cli = (ROOT / "hub2/host/agent_hub_host/cli.py").read_text()
        self.assertIn('default=Path.home() / ".config/agent-hub/v2.json"', cli)
        self.assertEqual(mod.LAUNCHER_CONFIG_RELATIVE, ".config/agent-hub/v2.json")
        self.assertIn("launcher_config=config.get(\"launcher\")", cli)
        self.assertIn("NativeLauncher(launcher_config or {}", (ROOT / "hub2/host/agent_hub_host/agent.py").read_text())
        unit = (ROOT / "deploy/agent-hub2-host.service").read_text()
        self.assertIn("hub2 serve\n", unit)
        self.assertNotIn("--config", unit)

    def test_launcher_caps_fail_closed_on_invalid_unsafe_mismatched_or_above_ceiling(self):
        for label, launcher, reason in (
            ("above memory ceiling", {"memory_mb": 1501}, "launcher_config_memory_cap_too_high"),
            ("above pids ceiling", {"pids": 257}, "launcher_config_pids_cap_too_high"),
            ("null memory", {"memory_mb": None}, "launcher_config_caps_invalid"),
            ("bool memory", {"memory_mb": True}, "launcher_config_caps_invalid"),
            ("string pids", {"pids": "256"}, "launcher_config_caps_invalid"),
            ("float memory", {"memory_mb": 1500.0}, "launcher_config_caps_invalid"),
            ("zero", {"memory_mb": 0}, "launcher_config_caps_invalid"),
            ("negative pids", {"pids": -1}, "launcher_config_caps_invalid"),
            ("non-dict launcher", [], "launcher_config_schema"),
        ):
            with self.subTest(label):
                self.assertCapsHold(self.home_with_config(launcher=launcher)[0], reason)
        self.assertCapsHold(self.home_with_config()[0], "launcher_config_machine_mismatch", machine="OTHER")
        self.assertCapsHold(self.home_with_config(machine_id=7)[0], "launcher_config_machine_mismatch")
        self.assertCapsHold(self.home_with_config(raw="[]")[0], "launcher_config_schema")
        self.assertCapsHold(self.home_with_config(raw="{not json")[0], "launcher_config_json")
        self.assertCapsHold(self.home_with_config(raw='{"machine_id":"HOST-1","machine_id":"HOST-1","token":"SECRET-TOKEN-VALUE"}')[0], "launcher_config_json")
        self.assertCapsHold(self.home_with_config(raw=" " * (256 * 1024 + 1))[0], "launcher_config_size")
        self.assertCapsHold(self.home_with_config(mode=0o644)[0], "launcher_config_unsafe")
        self.assertCapsHold(self.home_with_config(mode=0o660)[0], "launcher_config_unsafe")
        home, path = self.home_with_config()
        extra = home / "link.json"
        os.link(path, extra)
        self.assertCapsHold(home, "launcher_config_unsafe")  # nlink != 1
        extra.unlink()
        real = home / "real.json"
        path.rename(real)
        path.symlink_to(real)
        self.assertCapsHold(home, "launcher_config_symlink")
        path.unlink()
        self.assertCapsHold(home, "launcher_config_absent")
        path.mkdir()
        self.assertCapsHold(home, "launcher_config_not_regular")
        home, _ = self.home_with_config()
        (home / ".config" / "agent-hub").chmod(0o750)
        self.assertCapsHold(home, "launcher_config_parent")
        home, _ = self.home_with_config()
        moved = home / ".config" / "agent-hub-real"
        (home / ".config" / "agent-hub").rename(moved)
        (home / ".config" / "agent-hub").symlink_to(moved)
        self.assertCapsHold(home, "launcher_config_parent")

    def test_launcher_caps_require_a_matching_started_after_config_host_daemon(self):
        mod = self.mod()
        home, path = self.home_with_config(launcher={"memory_mb": 1000})

        def hold(reason, **kw):
            proc = self.host_process(home, **kw)
            with mock.patch.dict(os.environ, {"HOME": str(home)}), mock.patch.object(mod, "PROC_ROOT", str(proc)):
                with self.assertRaises(mod.StageFailure) as caught:
                    mod.read_launcher_caps(mod.selected_launcher_config_path(), "HOST-1")
            self.assertEqual(caught.exception.reason, reason)
        self.assertEqual(self.caps(home), (1000 * MIB, 256))
        proc = self.host_process(home)
        shutil.rmtree(proc / "8801")  # no Host daemon at all: the file's caps are unattributable
        with mock.patch.dict(os.environ, {"HOME": str(home)}), mock.patch.object(mod, "PROC_ROOT", str(proc)):
            with self.assertRaises(mod.StageFailure) as caught:
                mod.read_launcher_caps(mod.selected_launcher_config_path(), "HOST-1")
        self.assertEqual(caught.exception.reason, "launcher_config_host_unproven")
        hold("launcher_config_host_unproven", argv=[str(home / ".local/share/agent-hub2/current/bin/hub2"), "--config", "/other.json", "serve"])
        hold("launcher_config_host_unproven", argv=[str(home / ".local/share/agent-hub2/current/bin/hub2"), "serve", "--config=/other.json"])
        hold("launcher_config_host_unproven", argv=["/elsewhere/bin/hub2", "serve"])  # not run from this home
        hold("launcher_config_host_unproven", argv=[str(home / ".local/share/agent-hub2/current/bin/hub2"), "status"])
        hold("launcher_config_host_unproven", cgroup="some-other.service")
        hold("launcher_config_host_unproven", home_env="/home/someone-else")  # daemon HOME differs from the selected home
        # Two Host daemons are ambiguous.
        proc = self.host_process(home)
        shutil.copytree(proc / "8801", proc / "8802")
        with mock.patch.dict(os.environ, {"HOME": str(home)}), mock.patch.object(mod, "PROC_ROOT", str(proc)):
            with self.assertRaises(mod.StageFailure) as caught:
                mod.read_launcher_caps(mod.selected_launcher_config_path(), "HOST-1")
        self.assertEqual(caught.exception.reason, "launcher_config_host_unproven")
        # Config changed (mtime or ctime) after the daemon started: not what it loaded.
        shutil.rmtree(proc / "8802")
        proc = self.host_process(home)
        time.sleep(0.05)
        path.chmod(0o600)
        with mock.patch.dict(os.environ, {"HOME": str(home)}), mock.patch.object(mod, "PROC_ROOT", str(proc)):
            with self.assertRaises(mod.StageFailure) as caught:
                mod.read_launcher_caps(mod.selected_launcher_config_path(), "HOST-1")
        self.assertEqual(caught.exception.reason, "launcher_config_changed_after_host_start")

    def test_admission_uses_actual_launcher_memory_and_holds_on_unreadable_or_oversized_config(self):
        mod = self.mod()
        home, _ = self.home_with_config(launcher={"memory_mb": 1000})
        c = self.canary(sample_mem_available=lambda: 10 * GIB + 1000 * MIB - 1)
        c.read_launcher_caps = lambda deadline: mod.read_launcher_caps(mod.selected_launcher_config_path(), c.machine)
        proc = self.host_process(home)
        with mock.patch.dict(os.environ, {"HOME": str(home)}), mock.patch.object(mod, "PROC_ROOT", str(proc)):
            self.assertHold(c, "memory_below_floor")
            c.sample_mem_available = lambda: 10 * GIB + 1000 * MIB
            c.admit()
        home, _ = self.home_with_config(launcher={"memory_mb": 1501})
        proc = self.host_process(home)
        with mock.patch.dict(os.environ, {"HOME": str(home)}), mock.patch.object(mod, "PROC_ROOT", str(proc)):
            self.assertHold(c, "launcher_config_memory_cap_too_high")
        shutil.rmtree(home)
        with mock.patch.dict(os.environ, {"HOME": str(home)}), mock.patch.object(mod, "PROC_ROOT", str(proc)):
            self.assertHold(c, "launcher_config_absent")
        # Uncovered in-flight tasks are never charged the (possibly smaller) selected cap: the 1500 MiB ceiling.
        smaller = self.canary(read_launcher_caps=lambda d: (1000 * MIB, 256),
                              read_hub_rows=lambda d: [{"id": "u", "state": "starting", "machine_id": "HOST-1", "native_id": None, "workspace": str(self.tmp)}],
                              sample_mem_available=lambda: 10 * GIB + 1000 * MIB + 1500 * MIB - 1)
        self.assertHold(smaller, "memory_below_floor")
        smaller.sample_mem_available = lambda: 10 * GIB + 1000 * MIB + 1500 * MIB
        smaller.admit()
        # A seam returning out-of-range caps can never bypass the ceilings.
        for caps in ((1501 * MIB, 256), (1500 * MIB, 257), (0, 256), (1500 * MIB, True), (1500 * MIB, 0)):
            self.assertHold(self.canary(read_launcher_caps=lambda d, caps=caps: caps), "launcher_config_caps_invalid")

    def test_production_collector_has_no_caller_selected_config_path(self):
        source = (ROOT / "scripts" / "hub_start_canary.py").read_text()
        body = source[source.index("def selected_launcher_config_path"):source.index("def read_launcher_caps")]
        self.assertNotIn("os.environ", body)
        self.assertNotIn("sys.argv", body)
        self.assertNotIn("print(", source[source.index("def read_launcher_caps"):source.index("def parse_iso_utc")])

    # ---- disk growth of other workers ----------------------------------------------

    def disk_setup(self, young_rows, *, free, hub_rows=(), devices=None, rows_proc=(), mem=64 * GIB):
        """Real admission with both canary paths on one fake device (id 1); worker devices are injected."""
        c = self.residency(list(rows_proc), list(young_rows), mem=mem, hub_rows=hub_rows)
        c.fetch_disk_floor_gb = lambda d: 20
        c.disk_free_after_margin = lambda paths: {1: free}
        c.growth_devices = lambda *paths: {1} if devices is None else devices(paths)
        return c

    def test_young_and_inflight_workers_on_the_same_device_are_charged(self):
        floor, spike, per = 20 * GIB, 1024 * MIB, 512 * MIB
        young = [dict(pid=6001, cgroup="/user.slice/y1.scope", age=10, anon=50 * MIB, current=60 * MIB, rss_anon=50 * MIB)]
        census = [census_row(60, 6001, "/user.slice/y1.scope", workspace=str(self.tmp / "ws"))]
        self.disk_setup(census, free=floor + spike + per, rows_proc=young).admit()
        self.assertHold(self.disk_setup(census, free=floor + spike + per - 1, rows_proc=young), "disk_below_floor")
        inflight = [{"id": "h1", "state": "starting", "machine_id": "HOST-1", "native_id": None, "workspace": str(self.tmp / "ws")}]
        self.disk_setup([], free=floor + spike + per, hub_rows=inflight).admit()
        self.assertHold(self.disk_setup([], free=floor + spike + per - 1, hub_rows=inflight), "disk_below_floor")
        both = self.disk_setup(census, free=floor + spike + 2 * per, hub_rows=inflight, rows_proc=young)
        both.admit()
        self.assertHold(self.disk_setup(census, free=floor + spike + 2 * per - 1, hub_rows=inflight, rows_proc=young), "disk_below_floor")

    def test_different_device_and_terminal_history_add_no_disk_charge_and_mature_same_device_is_charged(self):
        floor, spike = 20 * GIB, 1024 * MIB
        young = [dict(pid=6002, cgroup="/user.slice/y2.scope", age=10, anon=50 * MIB, current=60 * MIB, rss_anon=50 * MIB)]
        census = [census_row(61, 6002, "/user.slice/y2.scope", workspace=str(self.tmp / "ws"))]
        self.disk_setup(census, free=floor + spike, rows_proc=young, devices=lambda paths: {2}).admit()  # other device
        mature = [dict(pid=6003, cgroup="/user.slice/m.scope", age=900, anon=50 * MIB, current=60 * MIB, rss_anon=50 * MIB)]
        mcensus = [census_row(62, 6003, "/user.slice/m.scope", workspace=str(self.tmp / "ws"))]
        # Mature RAM proof is not disk proof: a same-device mature worker is charged too.
        self.disk_setup(mcensus, free=floor + spike + 512 * MIB, rows_proc=mature).admit()
        self.assertHold(self.disk_setup(mcensus, free=floor + spike + 512 * MIB - 1, rows_proc=mature), "disk_below_floor")
        self.disk_setup(mcensus, free=floor + spike, rows_proc=mature, devices=lambda paths: {2}).admit()
        history = [census_row(63, 5, None, phase="exited", state="exited", worker_alive=False, containment_empty=None) for _ in range(3)]
        history = [dict(r, id=f"aaaaaaaa-0000-4000-8000-{70 + i:012d}", workspace=str(self.tmp / "ws")) for i, r in enumerate(history)]
        self.disk_setup(history, free=floor + spike).admit()
        self.assertHold(self.disk_setup(history, free=floor + spike - 1), "disk_below_floor")  # floor boundary itself unchanged
        elsewhere = [{"id": "h2", "state": "starting", "machine_id": "OTHER", "native_id": None, "workspace": str(self.tmp / "ws")}]
        self.disk_setup([], free=floor + spike, hub_rows=elsewhere).admit()

    def test_disk_charge_is_capped_at_the_launcher_active_reserve_cap(self):
        floor, spike = 20 * GIB, 1024 * MIB
        inflight = [{"id": f"h{i}", "state": "queued", "machine_id": "HOST-1", "native_id": None, "workspace": "/w"} for i in range(40)]
        self.disk_setup([], free=floor + spike + 8192 * MIB, hub_rows=inflight, mem=400 * GIB).admit()
        self.assertHold(self.disk_setup([], free=floor + spike + 8192 * MIB - 1, hub_rows=inflight, mem=400 * GIB), "disk_below_floor")

    def test_unknown_or_stale_growth_location_holds(self):
        floor, spike = 20 * GIB, 1024 * MIB
        for hub_row in (
            {"id": "h3", "state": "starting", "machine_id": "HOST-1", "native_id": None},
            {"id": "h4", "state": "starting", "machine_id": "HOST-1", "native_id": None, "workspace": "relative/path"},
        ):
            c = self.disk_setup([], free=64 * GIB, hub_rows=[hub_row], devices=lambda paths: self.mod().Canary.growth_devices(*paths))
            self.assertHold(c, "disk_growth_unknown")
        young = [dict(pid=6004, cgroup="/user.slice/y4.scope", age=10, anon=50 * MIB, current=60 * MIB, rss_anon=50 * MIB)]
        census = [census_row(64, 6004, "/user.slice/y4.scope", workspace=None)]
        c = self.disk_setup(census, free=64 * GIB, rows_proc=young, devices=lambda paths: self.mod().Canary.growth_devices(*paths))
        self.assertHold(c, "disk_growth_unknown")
        # Real device resolution: a path under the test tree resolves to a real st_dev set; missing parents use the nearest ancestor.
        devices = self.mod().Canary.growth_devices(str(self.tmp / "ws"))
        self.assertEqual(devices, {os.stat(self.tmp).st_dev})

    def test_disk_floor_boundary_spike_and_device_accounting(self):
        gib = GIB
        c = self.canary(fetch_disk_floor_gb=lambda d: 27.5)
        floor = int(27.5 * gib)
        spike = 1024 * MIB  # start spike + the new worker's own known 512 MiB reserve
        c.disk_free_after_margin = lambda paths: {1: floor + spike}
        c.admit()
        c.disk_free_after_margin = lambda paths: {1: floor + spike - 1}
        self.assertHold(c, "disk_below_floor")
        c.disk_free_after_margin = lambda paths: {1: floor + spike + 5, 2: floor + spike - 1}  # distinct devices each count
        self.assertHold(c, "disk_below_floor")
        for bad in (True, 0, 0.5, 1048577, float("nan"), "15", None):
            with self.subTest(bad=bad):
                mod = self.mod()
                self.assertFalse(mod._finite_number(bad) and mod.DISK_FLOOR_GB_RANGE[0] <= bad <= mod.DISK_FLOOR_GB_RANGE[1])

    def test_real_disk_sampling_counts_same_device_once_and_uses_nearest_existing_path(self):
        c = self.canary()
        self.assertEqual(len(c.disk_free_after_margin([str(self.tmp), str(self.tmp / "missing" / "deeper")])), 1)
        c.workspace = str(self.tmp / "not" / "created")
        c.fetch_disk_floor_gb = lambda d: 1048576
        self.assertHold(c, "disk_below_floor")
        self.assertFalse((self.tmp / "not").exists())

    def test_quota_acceptance_and_privacy(self):
        mod = self.mod()
        now = datetime_now()
        good = {"status": "ok", "details": {"limit_reached": False}, "windows": {"w": {"percent_remaining": 40, "present": True}}}
        self.assertIsNone(mod.evaluate_quota({"claude": good}, require_eligible_route=True, now=now))
        cases = {
            "limit": dict(good, details={"limit_reached": True}),
            "stale_flag": dict(good, stale=True),
            "error": dict(good, error="boom"),
            "old": dict(good, observed_at="2000-01-01T00:00:00Z"),
            "future": dict(good, observed_at="2999-01-01T00:00:00Z"),
            "bool_pct": dict(good, windows={"w": {"percent_remaining": True}}),
            "nan_pct": dict(good, windows={"w": {"percent_remaining": float("nan")}}),
            "no_windows": dict(good, windows={}),
            "exhausted": dict(good, windows={"w": {"percent_remaining": 0, "present": True}}),
            "status_unknown": dict(good, status="unknown"),
        }
        for label, route in cases.items():
            with self.subTest(label):
                self.assertEqual(mod.evaluate_quota({"claude": route}, require_eligible_route=True, now=now), "quota_no_eligible_route")
        reserve = dict(good, windows={"w": {"percent_remaining": 15, "present": True}})
        self.assertEqual(mod.evaluate_quota({"codex": reserve}, require_eligible_route=True, now=now), "quota_no_eligible_route")
        self.assertIsNone(mod.evaluate_quota({"claude": reserve}, require_eligible_route=True, now=now))
        self.assertEqual(mod.evaluate_quota([], require_eligible_route=False, now=now), "quota_unparseable")
        self.assertIsNone(mod.evaluate_quota({"codex": cases["limit"]}, require_eligible_route=False, now=now))  # shell: acquisition only

    def test_null_optional_plan_window_does_not_poison_usable_route_but_required_unknowns_hold(self):
        mod = self.mod()
        now = datetime_now()
        stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        reset = "2999-01-01T00:00:00Z"
        null = {"percent_remaining": None, "reset_at": None}
        live = lambda pct: {"percent_remaining": pct, "reset_at": reset}

        def route(windows, **extra):
            return dict({"status": "ok", "observed_at": stamp, "details": {"limit_reached": False, "observed_at": stamp}, "windows": windows}, **extra)

        def verdict(name, windows, **extra):
            return mod.evaluate_quota({name: route(windows, **extra)}, require_eligible_route=True, now=now)

        # Real quse shape: healthy Claude with a nullable monthly slot is usable; so is Codex with null 5h/monthly.
        self.assertIsNone(verdict("claude", {"5h": live(80), "7d": live(60), "monthly": dict(null)}))
        self.assertIsNone(verdict("claude", {"5h": live(80), "7d": live(60), "monthly": dict(null, present=False)}))
        self.assertIsNone(verdict("codex", {"5h": dict(null), "7d": live(60), "monthly": dict(null)}))
        # Exactly the optional slot is dropped; every other unknown stays fail-closed.
        bad = "quota_no_eligible_route"
        self.assertEqual(verdict("claude", {"5h": dict(null), "7d": live(60), "monthly": dict(null)}), bad)  # required window unknown
        self.assertEqual(verdict("claude", {"5h": live(80), "7d": dict(null), "monthly": dict(null)}), bad)
        self.assertEqual(verdict("codex", {"5h": live(80), "7d": dict(null)}), bad)
        self.assertEqual(verdict("go", {"5h": live(80), "7d": live(60), "monthly": dict(null)}), bad)  # not optional for this provider
        self.assertEqual(verdict("claude", {"5h": live(80), "weekly_opus": dict(null)}), bad)  # unlisted window
        self.assertEqual(verdict("claude", {"5h": live(80), "monthly": dict(null, present=True)}), bad)
        self.assertEqual(verdict("claude", {"5h": live(80), "monthly": dict(null, error="boom")}), bad)
        self.assertEqual(verdict("claude", {"5h": live(80), "monthly": {"percent_remaining": None, "reset_at": reset}}), bad)
        self.assertEqual(verdict("claude", {"5h": live(80), "monthly": {"percent_remaining": 50, "reset_at": None, "present": "yes"}}), bad)
        self.assertEqual(verdict("claude", {"monthly": dict(null)}), bad)  # nothing usable left
        self.assertEqual(verdict("claude", {"5h": live(80), "monthly": live(0)}), bad)  # present optional window is still checked
        self.assertEqual(verdict("claude", {"5h": live(0), "7d": live(60), "monthly": dict(null)}), bad)  # exhausted
        self.assertEqual(verdict("codex", {"7d": live(15), "5h": dict(null)}), bad)  # Codex reserve
        self.assertIsNone(verdict("codex", {"7d": live(16), "5h": dict(null)}))
        self.assertEqual(verdict("claude", {"5h": live(80), "monthly": dict(null)}, stale=True), bad)
        old = (now - __import__("datetime").timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.assertEqual(verdict("claude", {"5h": live(80), "monthly": dict(null)}, observed_at=old, details={"limit_reached": False, "observed_at": old}), bad)
        self.assertEqual(verdict("claude", {"5h": live(80), "monthly": dict(null)}, details={"limit_reached": True, "observed_at": stamp}), bad)

    def test_quota_failure_is_closed_and_raw_output_never_leaks(self):
        secret = "SECRET-ACCOUNT-xyz"
        self.cfg(quota={"codex": {"status": "error", "error": secret}})
        r, v = self.run_canary("ok", "--mode", "full")
        self.assertEqual((r.returncode, v["stage"], v["reason"]), (1, "admission", "quota_no_eligible_route"), r.stdout)
        self.assertNotIn(secret, r.stdout + r.stderr)
        self.assertEqual(self.starts(), [])

    def test_slow_reads_consume_the_original_deadline_without_reset(self):
        mod = self.mod()
        c = self.canary(fetch_disk_floor_gb=lambda d: time.sleep(0.15) or 1)
        c.active_deadline = time.monotonic() + 0.05
        self.assertHold(c, "admission_deadline_exceeded")
        c = self.canary(fetch_disk_floor_gb=lambda d: time.sleep(0.15) or 1)
        c.active_deadline = time.monotonic() + 30
        self.assertHold(c, "admission_deadline_exceeded", deadline=time.monotonic() + 0.05)
        c = self.canary()
        self.assertGreaterEqual(c.admission_deadline(time.monotonic() + 999), 0)
        self.assertLessEqual(c.admission_deadline(time.monotonic() + 999), c.active_deadline)
        # cleanup deadlines are the caller's original ones: not extended by the admission budget
        c.in_cleanup = True
        limit = time.monotonic() + 1
        self.assertEqual(c.admission_deadline(limit), limit)

    # ---- every possible START seam --------------------------------------------

    def test_main_start_refusal_leaves_no_session_pending_or_effects(self):
        self.cfg(mem_available=[1 * GIB])
        r, v = self.run_canary("ok", "--mode", "shell")
        self.assertEqual((r.returncode, v["stage"], v["reason"]), (1, "admission", "memory_below_floor"))
        self.assertEqual(self.starts(), [])
        self.assertIsNone(v["session_id"])
        self.assertEqual(v["cleanup"], "not_needed")
        self.assertFalse((self.tmp / "ws").exists())
        self.assertEqual(list((self.state / "pending").glob("*.json")) if (self.state / "pending").exists() else [], [])
        self.cfg()
        (self.tmp / "admission.log").unlink()
        r, v = self.run_canary("ok", "--mode", "shell")
        self.assertEqual(r.returncode, 0, r.stdout)
        log = (self.tmp / "admission.log").read_text().split()
        self.assertEqual(len(self.starts()), 1)
        self.assertEqual(log.count("settings"), 1)

    def test_cleanup_missing_receipt_replay_is_gated_and_keeps_original_body(self):
        mod = self.mod()
        calls = []
        c = self.canary(sample_mem_available=lambda: 1 * GIB)
        c.start_attempted = True
        c.in_cleanup = True
        c.run = lambda argv, *a, **kw: calls.append(list(argv)) or (1, "", "")
        c.list_items = lambda stage, deadline: []
        self.assertEqual(c.cleanup(deadline=time.monotonic() + 5), "FAILED_needs_reconciliation")
        self.assertEqual([a for a in calls if a[1:3] == ["sessions", "start"] or a[:2] == ["sessions", "start"]], [])
        self.assertEqual(c.admission_hold, "memory_below_floor")
        # admitted: the replay is the identical original start body (same key/workspace/prompt)
        c2 = self.canary()
        c2.start_attempted = True
        c2.in_cleanup = True
        expected = c2.start_args()
        seen = []
        c2.run = lambda argv, *a, **kw: seen.append(list(argv)) or (0, "", "")
        c2.list_items = lambda stage, deadline: []
        c2.cleanup(deadline=time.monotonic() + 5)
        self.assertIn(expected, seen)

    def write_pending(self, c, **overrides):
        pending = self.state / "pending"
        pending.mkdir(parents=True, exist_ok=True, mode=0o700)
        rec = {
            "session_id": None, "native_id": None, "repo": "owner/repo", "machine": "HOST-1",
            "workspace": str(self.tmp / "orig-ws"), "name": "hub-canary-shell-old", "key": "canary-old",
            "prompt": "printf CANARY-OK; sleep 30", "mode": "shell", "purpose": "review",
        }
        rec.update(overrides)
        path = pending / "old.json"
        path.write_text(json.dumps(rec))
        return path

    def test_pending_missing_sid_replay_is_gated_and_never_retargeted(self):
        mod = self.mod()
        calls = []
        c = self.canary(sample_mem_available=lambda: 1 * GIB)
        c.pending_dir = self.state / "pending"
        path = self.write_pending(c)
        c.run = lambda argv, *a, **kw: calls.append(list(argv)) or (1, "", "")
        c.list_items = lambda stage, deadline: []
        with self.assertRaises(mod.StageFailure) as caught:
            c.reconcile_prior_pending()
        self.assertEqual(caught.exception.reason, "prior_pending_reconciliation_failed")
        self.assertTrue(path.exists())
        self.assertEqual([a for a in calls if "start" in a and "sessions" in a], [])
        # When admitted, the replay carries the ORIGINAL recorded workspace/key, not the current one.
        calls.clear()
        c2 = self.canary()
        c2.pending_dir = self.state / "pending"
        c2.workspace = str(self.tmp / "a-different-current-ws")
        c2.run = lambda argv, *a, **kw: calls.append(list(argv)) or (1, "", "")
        c2.list_items = lambda stage, deadline: []
        with self.assertRaises(mod.StageFailure):
            c2.reconcile_prior_pending()
        replay = next(a for a in calls if "start" in a)
        self.assertEqual(replay[replay.index("--workspace") + 1], str(self.tmp / "orig-ws"))
        self.assertEqual(replay[replay.index("--key") + 1], "canary-old")
        self.assertTrue(path.exists())

    def test_known_sid_stop_and_cleanup_are_not_gated_by_admission(self):
        mod = self.mod()
        admitted = []
        c = self.canary()
        c.admit = lambda *a, **kw: admitted.append(1) or (_ for _ in ()).throw(mod.StageFailure("admission", "memory_below_floor"))
        c.pending_dir = self.state / "pending"
        sid = "11111111-2222-4333-8444-555555555555"
        self.write_pending(c, session_id=sid, native_id=NATIVE)
        stopped = []
        c.stop_or_confirm_retired = lambda *a, **kw: stopped.append(a[0]) or (True, NATIVE)
        c.wait_gone_for = lambda *a, **kw: None
        c.archive_intake_for = lambda *a, **kw: True
        c.reconcile_prior_pending()
        self.assertEqual(stopped, [sid])
        self.assertEqual(admitted, [])
        c.session, c.native, c.start_attempted, c.in_cleanup = sid, NATIVE, True, True
        c.wait_gone = lambda deadline: None
        self.assertEqual(c.cleanup(deadline=time.monotonic() + 5), "stopped")
        self.assertEqual(admitted, [])

    # ---- bridge through the whole-archive-verified CLI --------------------------

    def bridge_zip(self, *, worker=False, human=True, floor=27.5, fail=False, refresh_marker=None):
        import io, zipfile
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("agent_hub_host/__init__.py", "")
            z.writestr("agent_hub_host/auth.py", "def refresh_token(*a, **k):\n    return 'refreshed'\n")
            z.writestr("agent_hub_host/worker_auth.py", f"def is_v2_worker():\n    return {worker!r}\n")
            z.writestr("agent_hub_host/config.py", f"def load_human_config():\n    return {({'access_token': 'x'} if human else {})!r}\n")
            body = "from . import auth\n"
            if refresh_marker:
                body += f"def _do_request(method, path):\n    auth.refresh_token()\n    open({str(refresh_marker)!r}, 'w').write('refreshed')\n    return {{}}\n"
            elif fail:
                body += "def _do_request(method, path):\n    raise RuntimeError('503')\n"
            else:
                body += f"def _do_request(method, path):\n    assert (method, path) == ('GET', '/api/resource-settings')\n    return {{'resource_settings': {{'disk_floor_gb': {floor!r}}}}}\n"
            z.writestr("agent_hub_host/bus.py", body)
        return buf.getvalue()

    @unittest.skipUnless(hasattr(os, "memfd_create"), "sealed memfd unsupported by this Python (same limit as the existing sealed-CLI tests)")
    def test_bridge_reads_disk_floor_through_sealed_cli_with_human_only_and_no_refresh(self):
        mod = self.mod()
        marker = self.tmp / "refreshed.flag"
        with mock.patch.dict(os.environ, {"HUB_TOKEN": "", "HUB_CONFIG_FILE": ""}):
            for label, kwargs, expected in (
                ("ok", {}, 27.5),
                ("worker", dict(worker=True), "resource_settings_unavailable"),
                ("not human", dict(human=False), "resource_settings_unavailable"),
                ("http failure", dict(fail=True), "resource_settings_unavailable"),
                ("floor zero", dict(floor=0), "resource_settings_invalid"),
                ("floor huge", dict(floor=2000000), "resource_settings_invalid"),
                ("floor bool", dict(floor=True), "resource_settings_invalid"),
                ("no refresh", dict(refresh_marker=marker), "resource_settings_unavailable"),
            ):
                with self.subTest(label):
                    c = mod.Canary("shell", "bridge")
                    c.pyz_fd = mod.create_sealed_pyz_fd(self.bridge_zip(**kwargs))
                    self.addCleanup(os.close, c.pyz_fd)
                    c.hub_pass_fds = (c.pyz_fd,)
                    if isinstance(expected, float):
                        self.assertEqual(c.fetch_disk_floor_gb(time.monotonic() + 20), expected)
                    else:
                        with self.assertRaises(mod.StageFailure) as caught:
                            c.fetch_disk_floor_gb(time.monotonic() + 20)
                        self.assertEqual(caught.exception.reason, expected)
        self.assertFalse(marker.exists())
        env_token = {"HUB_JOB_TOKEN": "t"}
        with mock.patch.dict(os.environ, env_token):
            c = mod.Canary("shell", "bridge")
            c.pyz_fd = mod.create_sealed_pyz_fd(self.bridge_zip())
            self.addCleanup(os.close, c.pyz_fd)
            with self.assertRaises(mod.StageFailure):
                c.fetch_disk_floor_gb(time.monotonic() + 20)

    @unittest.skipUnless(hasattr(os, "memfd_create"), "sealed memfd unsupported by this Python (same limit as the existing sealed-CLI tests)")
    def test_periodic_cli_must_be_a_bindable_owned_zipapp(self):
        mod = self.mod()
        c = mod.Canary("shell", "periodic")
        c.hub = ["definitely-not-a-hub-binary-xyz"]
        with self.assertRaises(mod.StageFailure) as caught:
            c.bridge_artifact_fd()
        self.assertEqual(caught.exception.reason, "cli_artifact_unbindable")
        fake = self.tmp / "bin" / "hub"
        fake.write_text("#!/bin/sh\nexit 0\n")
        fake.chmod(0o755)
        c.hub = [str(fake)]
        with self.assertRaises(mod.StageFailure):
            c.bridge_artifact_fd()
        fake.write_bytes(self.bridge_zip())
        fake.chmod(0o755)
        fd = c.bridge_artifact_fd()
        self.addCleanup(os.close, fd)
        self.assertEqual(c.bridge_artifact_fd(), fd)
        fake.chmod(0o777)
        c2 = mod.Canary("shell", "periodic2")
        c2.hub = [str(fake)]
        with self.assertRaises(mod.StageFailure):
            c2.bridge_artifact_fd()

    def test_scope_preserved_stages_wrappers_and_limits(self):
        mod = self.mod()
        self.assertLess(mod.STAGES.index("health"), mod.STAGES.index("admission"))
        self.assertLess(mod.STAGES.index("admission"), mod.STAGES.index("start"))
        self.assertEqual((mod.NEW_WORKER_MEMORY_BYTES, mod.NEW_WORKER_PIDS, mod.ADMISSION_RAM_FLOOR_BYTES, mod.ADMISSION_DISK_SPIKE_BYTES),
                         (1500 * MIB, 256, 10 * GIB, 512 * MIB))
        source = (ROOT / "scripts" / "hub_start_canary.py").read_text()
        self.assertIn('CANARY_WORKSPACE_CONFIG = "/etc/agent-hub/canary-workspace.json"', source)
        self.assertNotIn("os.environ.get(\"HUB_CANARY_WORKSPACE_CONFIG", source)


def datetime_now():
    import datetime
    return datetime.datetime.now(datetime.timezone.utc)
