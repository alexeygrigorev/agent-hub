#!/usr/bin/env python3
"""Live start canary for Hub: start, observe, capture, stop, confirm gone.

Drives the public CLIs only (`hub sessions start|list|stop`, `a capture`,
`a list --json --running`). See docs/HUB-START-CANARY.md.
"""
import argparse, datetime, fcntl, json, os, pathlib, re, shlex, subprocess, sys, time, uuid

ALERT_TO = "dev@dtcdev.click"  # fixed on purpose: never configurable, never orin@
ALERT_FROM = "alerts@dtcdev.click"
MSMTP_ACCOUNT = "alerts"
MARKER = "CANARY-OK"
PROMPTS = {"full": "Reply CANARY-OK and exit", "shell": "echo CANARY-OK; exit"}
STAGES = ("start", "running", "list_host_workspace", "capture", "stop", "gone")
FAIL_THRESHOLD = 2
RECOVER_THRESHOLD = 2


class StageFailure(Exception):
    def __init__(self, stage, reason):
        super().__init__(f"{stage}: {reason}")
        self.stage, self.reason = stage, reason


def env_num(name, default):
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return float(default)


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def clean(text, limit=80):
    return re.sub(r"[^A-Za-z0-9_.:-]", "_", str(text))[:limit] or "unknown"


class Canary:
    def __init__(self, mode, run_id):
        self.mode, self.run_id = mode, run_id
        self.hub = shlex.split(os.environ.get("HUB_BIN", "hub"))
        self.a = shlex.split(os.environ.get("A_BIN", "a"))
        self.poll = env_num("HUB_CANARY_POLL_S", 2)
        self.running_timeout = env_num("HUB_CANARY_RUNNING_TIMEOUT_S", 180)
        self.capture_timeout = env_num("HUB_CANARY_CAPTURE_TIMEOUT_S", 120 if mode == "full" else 30)
        self.stop_timeout = env_num("HUB_CANARY_STOP_TIMEOUT_S", 60)
        self.cmd_timeout = env_num("HUB_CANARY_CMD_TIMEOUT_S", 90)
        self.workspace = os.environ.get("HUB_CANARY_WORKSPACE") or str(pathlib.Path.home() / "work" / "hub-canary")
        self.key = f"canary-{run_id}"
        self.name = f"hub-canary-{mode}-{run_id}"
        self.prompt = PROMPTS[mode]
        self.session = None
        self.native = None
        self.stopped = False

    def run(self, argv, timeout=None):
        try:
            r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout or self.cmd_timeout)
        except subprocess.TimeoutExpired:
            return 124, "", ""
        except OSError as error:
            return 127, "", type(error).__name__
        return r.returncode, r.stdout, r.stderr

    def hub_json(self, stage, *args, timeout=None):
        rc, out, _ = self.run([*self.hub, *args], timeout)
        if rc != 0:
            raise StageFailure(stage, "hub_cli_timeout" if rc == 124 else f"hub_cli_exit_{rc}")
        try:
            return json.loads(out)
        except ValueError:
            raise StageFailure(stage, "unparseable_json")

    def list_items(self, stage):
        data = self.hub_json(stage, "sessions", "list", "--json")
        items = data.get("items", data.get("sessions")) if isinstance(data, dict) else data
        if not isinstance(items, list):
            raise StageFailure(stage, "unparseable_json")
        return items

    def a_running_ids(self):
        rc, out, _ = self.run([*self.a, "list", "--json", "--running"])
        if rc != 0:
            raise StageFailure("gone", "a_list_timeout" if rc == 124 else f"a_list_exit_{rc}")
        try:
            data = json.loads(out)
        except ValueError:
            raise StageFailure("gone", "unparseable_json")
        return {x.get("id") for x in data if isinstance(x, dict)}

    def start(self):
        os.makedirs(self.workspace, mode=0o700, exist_ok=True)
        args = ["sessions", "start", "--workspace", self.workspace, "--name", self.name,
                "--key", self.key, "--prompt", self.prompt]
        if self.mode == "shell":
            args += ["--engine", "shell", "--reason", "hub-start-canary shell probe (no model)"]
        data = self.hub_json("start", *args)
        node = data.get("session") if isinstance(data, dict) and isinstance(data.get("session"), dict) else data
        sid = node.get("id") if isinstance(node, dict) else None
        if not isinstance(sid, str) or not sid:
            raise StageFailure("start", "no_session_id")
        self.session = sid

    def wait_running(self):
        deadline = time.monotonic() + self.running_timeout
        last, reason = None, "never_listed"
        while True:
            item = next((i for i in self.list_items("running") if isinstance(i, dict) and i.get("id") == self.session), None)
            if item is not None:
                state = item.get("state")
                if state in ("failed", "stopped", "cancelled", "closed", "aborted"):
                    att = item.get("attention") if isinstance(item.get("attention"), dict) else {}
                    raise StageFailure("running", f"state_{clean(state)}:{clean(att.get('reason', 'no_reason'))}")
                if state == "running":
                    host = item.get("host") or item.get("machine_id") or item.get("machine")
                    ws = item.get("workspace")
                    native = item.get("native_id")
                    missing = [n for n, v in (("host", host), ("workspace", ws), ("native_id", native)) if not v]
                    last = missing
                    if not missing:
                        self.native = native
                        return
                    reason = "missing_" + "_".join(missing)
                else:
                    att = item.get("attention") if isinstance(item.get("attention"), dict) else {}
                    reason = f"state_{clean(state)}" + (f":{clean(att['reason'])}" if att.get("reason") else "")
                    last = None
            if time.monotonic() >= deadline:
                if last:
                    raise StageFailure("list_host_workspace", reason)
                raise StageFailure("running", "timeout_" + reason)
            time.sleep(self.poll)

    def check_capture(self):
        deadline = time.monotonic() + self.capture_timeout
        reason = "no_output"
        while True:
            rc, out, _ = self.run([*self.a, "capture", self.native, "--bytes", "8192"])
            if rc == 0:
                if MARKER in out.replace(self.prompt, ""):
                    return
                reason = "no_output_beyond_prompt" if out.strip() else "empty_capture"
            else:
                reason = "a_capture_timeout" if rc == 124 else f"a_capture_exit_{rc}"
            if time.monotonic() >= deadline:
                raise StageFailure("capture", reason)
            time.sleep(self.poll)

    def stop(self, suffix="stop"):
        rc, _, _ = self.run([*self.hub, "sessions", "stop", self.session, "--key", f"{self.key}-{suffix}",
                             "--wait", "--timeout", str(int(self.stop_timeout)), "--interval", "1"],
                            self.stop_timeout + 30)
        return rc

    def wait_gone(self):
        deadline = time.monotonic() + self.stop_timeout
        while True:
            if self.native not in self.a_running_ids():
                return
            if time.monotonic() >= deadline:
                raise StageFailure("gone", "still_listed_in_a_running")
            time.sleep(self.poll)

    def execute(self):
        result = {"ok": False, "mode": self.mode, "run_id": self.run_id, "timestamp": now_iso(),
                  "stage": None, "reason": None, "session_id": None, "cleanup": "not_needed"}
        try:
            self.start()
            self.wait_running()
            self.check_capture()
            rc = self.stop()
            if rc != 0:
                raise StageFailure("stop", "hub_cli_timeout" if rc == 124 else f"hub_cli_exit_{rc}")
            self.stopped = True
            self.wait_gone()
            result.update(ok=True)
        except StageFailure as failure:
            result.update(stage=failure.stage, reason=failure.reason)
        except Exception as error:  # never leave a session behind on a bug
            result.update(stage="start" if not self.session else "stop", reason="internal_" + type(error).__name__)
        finally:
            if self.session and not self.stopped:
                rc = self.stop("cleanup")
                result["cleanup"] = "stopped" if rc == 0 else "FAILED_needs_reconciliation"
        result["session_id"] = self.session
        return result


# ---- alert state and mail -------------------------------------------------

def state_dir():
    base = os.environ.get("HUB_CANARY_STATE_DIR")
    if not base:
        xdg = os.environ.get("XDG_STATE_HOME") or str(pathlib.Path.home() / ".local" / "state")
        base = str(pathlib.Path(xdg) / "agent-hub-canary")
    path = pathlib.Path(base)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)
    return path


def load_state(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {"failures": 0, "successes": 0, "incident_open": False, "incident_id": "-"}


def save_state(path, state):
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(state, f)
    os.replace(tmp, path)


def send_mail(subject, body):
    """Render to the injected mailer (HUB_CANARY_MAILER) or the msmtp path used by resource-gate-monitor."""
    mailer = shlex.split(os.environ.get("HUB_CANARY_MAILER", "")) or ["msmtp", "-a", MSMTP_ACCOUNT, "-f", ALERT_FROM]
    message = (f"From: {ALERT_FROM}\nTo: {ALERT_TO}\nSubject: {subject}\n"
               f"Date: {datetime.datetime.now(datetime.timezone.utc).strftime('%a, %d %b %Y %H:%M:%S +0000')}\n\n{body}\n")
    try:
        r = subprocess.run([*mailer, "--", ALERT_TO], input=message, text=True, capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


def apply_alerting(result):
    sdir = state_dir()
    lock = open(sdir / f"{result['mode']}.lock", "w")
    fcntl.flock(lock, fcntl.LOCK_EX)
    path = sdir / f"state-{result['mode']}.json"
    st = load_state(path)
    mode = result["mode"]
    if result["ok"]:
        st["failures"] = 0
        if st.get("incident_open"):
            st["successes"] = st.get("successes", 0) + 1
            if st["successes"] >= RECOVER_THRESHOLD:
                body = (f"Hub start canary ({mode}) recovered at {result['timestamp']}.\n"
                        f"Run id: {result['run_id']}\nIncident: {st.get('incident_id')}\n")
                if send_mail(f"[RECOVERED] Hub start canary ({mode}) is passing again", body):
                    st.update(incident_open=False, successes=0, incident_id="-")
        else:
            st["successes"] = 0
    else:
        st["successes"] = 0
        st["failures"] = st.get("failures", 0) + 1
        if st["failures"] >= FAIL_THRESHOLD and not st.get("incident_open"):
            incident = st.get("incident_id") if st.get("incident_id", "-") != "-" else result["run_id"]
            body = (f"CRITICAL: Hub cannot start sessions (canary mode {mode}).\n"
                    f"Failing stage: {result['stage']}\nReason: {result['reason']}\n"
                    f"Timestamp: {result['timestamp']}\nRun id: {result['run_id']}\n"
                    f"Consecutive failures: {st['failures']}\nIncident: {incident}\n"
                    f"Session cleanup: {result['cleanup']}\n")
            if send_mail(f"[CRITICAL] Hub start canary ({mode}) failing at stage {result['stage']}: {result['reason']}", body):
                st.update(incident_open=True, incident_id=incident)
    st["last"] = {k: result[k] for k in ("ok", "stage", "reason", "timestamp", "run_id")}
    save_state(path, st)
    lock.close()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--mode", choices=("shell", "full"), default="full")
    p.add_argument("--deploy-gate", action="store_true", help="run the full canary once, no mail, JSON verdict")
    args = p.parse_args()
    mode = "full" if args.deploy_gate else args.mode
    result = Canary(mode, uuid.uuid4().hex[:12]).execute()
    if args.deploy_gate:
        result["deploy_gate"] = True
    else:
        apply_alerting(result)
    line = json.dumps(result, sort_keys=True)
    print(line, flush=True)
    if not result["ok"]:
        print(line, file=sys.stderr, flush=True)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
