#!/usr/bin/env python3
"""Live start canary for Hub: start, observe, capture, stop, confirm gone.

Drives the public CLIs only (`hub sessions start|list|capture|stop`,
`a list --json --running`). See docs/HUB-START-CANARY.md.
"""
import argparse, datetime, fcntl, json, os, pathlib, re, shlex, subprocess, sys, time, uuid

ALERT_TO = "dev@dtcdev.click"  # fixed on purpose: never configurable, never orin@
ALERT_FROM = "alerts@dtcdev.click"
MSMTP_ACCOUNT = "alerts"
MARKER = "CANARY-OK"
PROMPTS = {"full": "Reply CANARY-OK and exit", "shell": "printf CANARY-OK; sleep 30"}
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
        self.machine = os.environ.get("HUB_CANARY_MACHINE", "hetzner-aplexer")
        self.repo = os.environ.get("HUB_CANARY_REPO", "alexeygrigorev/agent-hub")
        self.key = f"canary-{run_id}"
        self.name = f"hub-canary-{mode}-{run_id}"
        self.marker = MARKER if mode == "shell" else f"{MARKER}:{run_id}"
        # The complete expected output never occurs in the model prompt. A
        # wrapped echo of the prompt therefore cannot count as execution.
        self.prompt = PROMPTS["shell"] if mode == "shell" else (
            f'Run one shell command that prints the concatenation of "CANARY-", "OK:", and "{run_id}" '
            'as one output line, then sleeps for 60 seconds so the orchestrator can stop this session. '
            'Do not edit files or start another agent.')
        self.session = self.native = None
        self.started_at = self.running_deadline = None
        self.start_attempted = self.stopped = False

    def run(self, argv, timeout=None, *, deadline=None):
        limit = self.cmd_timeout if timeout is None else timeout
        if deadline is not None:
            limit = min(limit, deadline - time.monotonic())
        if limit <= 0:
            return 124, "", ""
        try:
            r = subprocess.run(argv, capture_output=True, text=True, timeout=limit)
        except subprocess.TimeoutExpired as error:
            def decoded(value):
                return value.decode(errors="replace") if isinstance(value, bytes) else value or ""
            return 124, decoded(error.stdout), decoded(error.stderr)
        except OSError as error:
            return 127, "", type(error).__name__
        if deadline is not None and time.monotonic() >= deadline:
            return 124, r.stdout, r.stderr
        return r.returncode, r.stdout, r.stderr

    @staticmethod
    def failure_reason(rc, err):
        if rc == 124:
            return "hub_cli_timeout"
        typed = re.search(r"\bstatus\s+\d{3}\s+\(([a-z][a-z0-9_]{0,79})\)", err)
        return typed.group(1) if typed else f"hub_cli_exit_{rc}"

    def hub_json(self, stage, *args, deadline=None):
        rc, out, err = self.run([*self.hub, *args], deadline=deadline)
        if rc != 0:
            raise StageFailure(stage, self.failure_reason(rc, err))
        try:
            return json.loads(out)
        except ValueError:
            raise StageFailure(stage, "unparseable_json")

    def list_items(self, stage, deadline):
        # Current hub CLI exhausts the indexed cursor. Prefer its aggregated
        # sessions[] over an inherited items[] from the first API page.
        data = self.hub_json(stage, "sessions", "list", "--all", "--json", deadline=deadline)
        items = data.get("sessions", data.get("items")) if isinstance(data, dict) else data
        if not isinstance(items, list):
            raise StageFailure(stage, "unparseable_json")
        if isinstance(data, dict) and data.get("next_cursor", data.get("next")):
            raise StageFailure(stage, "incomplete_listing")
        return items

    def a_running_ids(self, deadline):
        rc, out, _ = self.run([*self.a, "list", "--json", "--running"], deadline=deadline)
        if rc != 0:
            raise StageFailure("gone", "a_list_timeout" if rc == 124 else f"a_list_exit_{rc}")
        try:
            data = json.loads(out)
        except ValueError:
            raise StageFailure("gone", "unparseable_json")
        if not isinstance(data, list):
            raise StageFailure("gone", "unparseable_json")
        return {x.get("id") for x in data if isinstance(x, dict)}

    def start_args(self):
        args = [*self.hub, "sessions", "start", "--no-wait", "--repo", self.repo,
                "--machine", self.machine, "--workspace", self.workspace, "--name", self.name,
                "--key", self.key, "--prompt", self.prompt]
        if self.mode == "shell":
            args += ["--engine", "shell"]
        return args

    def remember_receipt(self, out):
        try:
            data = json.loads(out)
        except ValueError:
            return False
        node = data.get("session", data) if isinstance(data, dict) else None
        sid = node.get("id") if isinstance(node, dict) else None
        if not isinstance(sid, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", sid):
            return False
        self.session = sid
        return True

    def start(self):
        os.makedirs(self.workspace, mode=0o700, exist_ok=True)
        self.started_at = time.monotonic()
        self.running_deadline = self.started_at + self.running_timeout
        self.start_attempted = True
        rc, out, err = self.run(self.start_args(), deadline=self.running_deadline)
        identified = self.remember_receipt(out)
        if rc != 0:
            raise StageFailure("start", self.failure_reason(rc, err))
        if not identified:
            try:
                json.loads(out)
            except ValueError:
                raise StageFailure("start", "unparseable_json")
            raise StageFailure("start", "no_session_id")

    def pause(self, deadline):
        time.sleep(min(self.poll, max(0, deadline - time.monotonic())))

    def wait_running(self):
        deadline = self.running_deadline
        reason = "never_listed"
        stage = "running"
        while time.monotonic() < deadline:
            try:
                items = self.list_items("running", deadline)
            except StageFailure:
                if time.monotonic() >= deadline:
                    break
                raise
            item = next((i for i in items if isinstance(i, dict) and i.get("id") == self.session), None)
            if item is not None:
                state = item.get("state")
                att = item.get("attention") if isinstance(item.get("attention"), dict) else {}
                if state in ("failed", "stopped", "cancelled", "closed", "aborted", "lost", "completed-awaiting-review"):
                    raise StageFailure("running", f"state_{clean(state)}:{clean(att.get('reason', 'no_reason'))}")
                if state == "running":
                    host = item.get("host") or item.get("machine_id") or item.get("machine")
                    missing = [name for name, value in (("host", host), ("workspace", item.get("workspace")), ("native_id", item.get("native_id"))) if not value]
                    if missing:
                        stage, reason = "list_host_workspace", "missing_" + "_".join(missing)
                    elif host != self.machine or pathlib.Path(item["workspace"]).resolve() != pathlib.Path(self.workspace).resolve():
                        raise StageFailure("list_host_workspace", "host_workspace_mismatch")
                    elif time.monotonic() < deadline:
                        self.native = item["native_id"]
                        return
                else:
                    reason = f"state_{clean(state)}" + (f":{clean(att['reason'])}" if att.get("reason") else "")
            self.pause(deadline)
        raise StageFailure(stage, ("timeout_" if stage == "running" else "") + reason)

    def output_has_marker(self, out):
        def marker_in(text):
            return isinstance(text, str) and any(line.strip(" \t│┃>●•") == self.marker for line in text.splitlines())
        for line in out.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                if marker_in(line):
                    return True
                continue
            if not isinstance(event, dict):
                continue
            # Only completed tool output and assistant output; never arguments,
            # prompts, reasoning or arbitrary nested JSON containing the nonce.
            item = event.get('item')
            if event.get('type') == 'item.completed' and isinstance(item, dict):
                if item.get('type') == 'command_execution' and marker_in(item.get('aggregated_output')):
                    return True
                if item.get('type') == 'agent_message' and marker_in(item.get('text')):
                    return True
            if event.get('type') == 'result' and marker_in(event.get('result')):
                return True
            part = event.get('part')
            if event.get('type') == 'text' and isinstance(part, dict) and marker_in(part.get('text')):
                return True
        return False

    def check_capture(self):
        deadline = time.monotonic() + self.capture_timeout
        reason = "no_output"
        attempt = 0
        while time.monotonic() < deadline:
            attempt += 1
            key = f"canary-capture-{self.run_id}-{attempt}"
            rc, raw, _ = self.run([*self.hub, "sessions", "capture", self.session, "--key", key,
                                   "--max-bytes", "4096", "--json"], deadline=deadline)
            out = ''
            if rc == 0:
                try:
                    entry = json.loads(raw).get('command_wait', {})
                    out = entry.get('ack', {}).get('capture', '') if entry.get('state') == 'acked' else ''
                    if not isinstance(out, str):
                        out = ''
                except (ValueError, AttributeError):
                    out = ''
            if rc == 0:
                if self.output_has_marker(out) and time.monotonic() < deadline:
                    return
                reason = "no_output_beyond_prompt" if out.strip() else "empty_capture"
            elif rc != 124 or reason == "no_output":
                reason = "hub_capture_timeout" if rc == 124 else f"hub_capture_exit_{rc}"
            self.pause(deadline)
        raise StageFailure("capture", reason)

    def stop(self, deadline, suffix="stop"):
        rc, _, _ = self.run([*self.hub, "sessions", "stop", self.session, "--key", f"{self.key}-{suffix}",
                             "--wait", "--timeout", str(max(1, int(self.stop_timeout))), "--interval", "1"],
                            deadline=deadline)
        return rc

    def wait_gone(self, deadline):
        while time.monotonic() < deadline:
            try:
                gone = self.native not in self.a_running_ids(deadline)
            except StageFailure:
                if time.monotonic() >= deadline:
                    break
                raise
            if gone and time.monotonic() < deadline:
                return
            self.pause(deadline)
        raise StageFailure("gone", "still_listed_in_a_running")

    def cleanup(self):
        deadline = time.monotonic() + self.stop_timeout
        if not self.session and self.start_attempted:
            # A lost response is reconciled with the identical key and payload.
            # If both receipts are unavailable, explicitly retain uncertainty.
            _, out, _ = self.run(self.start_args(), deadline=deadline)
            self.remember_receipt(out)
        if not self.session:
            return "FAILED_needs_reconciliation" if self.start_attempted else "not_needed"
        if self.stop(deadline, "cleanup") != 0:
            return "FAILED_needs_reconciliation"
        if self.native:
            try:
                self.wait_gone(deadline)
            except StageFailure:
                return "FAILED_needs_reconciliation"
        return "stopped"

    def execute(self):
        result = {"ok": False, "mode": self.mode, "run_id": self.run_id, "timestamp": now_iso(),
                  "stage": None, "reason": None, "session_id": None, "cleanup": "not_needed"}
        try:
            self.start()
            self.wait_running()
            self.check_capture()
            deadline = time.monotonic() + self.stop_timeout
            rc = self.stop(deadline)
            if rc != 0:
                raise StageFailure("stop", "hub_cli_timeout" if rc == 124 else f"hub_cli_exit_{rc}")
            self.wait_gone(deadline)
            self.stopped = True
            result.update(ok=True)
        except StageFailure as failure:
            result.update(stage=failure.stage, reason=failure.reason)
        except Exception as error:
            result.update(stage="start" if not self.session else "stop", reason="internal_" + type(error).__name__)
        finally:
            if self.start_attempted and not self.stopped:
                try:
                    result["cleanup"] = self.cleanup()
                except Exception:
                    result["cleanup"] = "FAILED_needs_reconciliation"
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
    p.add_argument("--mode", choices=("shell", "full"), default=None)
    p.add_argument("--deploy-gate", action="store_true", help="run the canary once (default mode full, or --mode shell), no mail, JSON verdict")
    args = p.parse_args()
    mode = args.mode or "full"
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
