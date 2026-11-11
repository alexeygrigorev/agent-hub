#!/usr/bin/env python3
"""Live start canary for Hub: start, observe, capture, stop, confirm gone.

Drives the public CLIs only (`hub sessions start|list|capture|stop`,
`a list --json --running`). See docs/HUB-START-CANARY.md.
"""
import argparse, datetime, fcntl, hashlib, json, os, pathlib, re, shlex, subprocess, sys, time, uuid

ALERT_TO = "dev@dtcdev.click"  # fixed on purpose: never configurable, never orin@
ALERT_FROM = "alerts@dtcdev.click"
MSMTP_ACCOUNT = "alerts"
MARKER = "CANARY-OK"
PROMPTS = {"full": "Reply CANARY-OK and exit", "shell": "printf CANARY-OK; sleep 30"}
STAGES = ("helper_version", "host_release", "start", "running", "list_host_workspace", "capture", "stop", "gone", "cleanup")
FAIL_THRESHOLD = 2
RECOVER_THRESHOLD = 2
HELPER_FILES = ("hub-deploy-gate-forced.sh", "hub-start-canary.sh", "hub_start_canary.py")


def compute_helper_sha256(directory=None):
    base = pathlib.Path(directory) if directory else pathlib.Path(__file__).resolve().parent
    digest = hashlib.sha256()
    for name in HELPER_FILES:
        data = (base / name).read_bytes()
        digest.update(f"{name}\0{len(data)}\0".encode("utf-8"))
        digest.update(data)
        digest.update(b"\0")
    return digest.hexdigest()


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
    def __init__(self, mode, run_id, *, candidate_sha=None, run_token=None, expected_helper_sha256=None):
        self.mode, self.run_id = mode, run_id
        self.candidate_sha = candidate_sha
        self.run_token = run_token
        self.expected_helper_sha256 = expected_helper_sha256
        self.helper_sha256 = None
        self.host_version = None
        self.hub_pyz_sha256 = None
        self.intake_archived = None
        self.hub = shlex.split(os.environ.get("HUB_BIN", "hub"))
        self.a = shlex.split(os.environ.get("A_BIN", "a"))
        self.poll = env_num("HUB_CANARY_POLL_S", 2)
        self.converge_timeout = env_num("HUB_CANARY_CONVERGE_TIMEOUT_S", 120)
        self.running_timeout = env_num("HUB_CANARY_RUNNING_TIMEOUT_S", 180)
        self.capture_timeout = env_num("HUB_CANARY_CAPTURE_TIMEOUT_S", 120 if mode == "full" else 30)
        self.stop_timeout = env_num("HUB_CANARY_STOP_TIMEOUT_S", 60)
        self.cmd_timeout = env_num("HUB_CANARY_CMD_TIMEOUT_S", 90)
        self.workspace = os.environ.get("HUB_CANARY_WORKSPACE") or str(pathlib.Path.home() / "work" / "hub-canary")
        self.host_release_root = pathlib.Path(
            os.environ.get("HUB_CANARY_HOST_RELEASE_ROOT") or (pathlib.Path.home() / ".local" / "share" / "agent-hub2")
        )
        self.machine = os.environ.get("HUB_CANARY_MACHINE", "hetzner-aplexer")
        self.repo = os.environ.get("HUB_CANARY_REPO", "alexeygrigorev/agent-hub")
        self.key = f"canary-{run_id}"
        self.name = f"hub-canary-{mode}-{run_id}"
        if candidate_sha and run_token:
            sha12 = candidate_sha[:12]
            self.marker = f"{MARKER}:{sha12}:{run_token}"
            # The complete expected output never occurs in the command or prompt text.
            self.prompt = (
                f"printf '%s:%s:%s\\n' CANARY-OK {sha12} {run_token}; sleep 30"
                if mode == "shell"
                else (
                    f'Use two sequential tool calls. First run a shell command that prints the concatenation of "CANARY-", "OK:", and "{sha12}:{run_token}" '
                    "as one output line and exits immediately. After that tool call completes, use a separate tool call to run sleep 60 "
                    "so the orchestrator can stop this session. Do not combine printing and sleeping in one tool call. "
                    "Do not edit files or start another agent."
                )
            )
        else:
            self.marker = MARKER if mode == "shell" else f"{MARKER}:{run_id}"
            # The complete expected output never occurs in the model prompt. A
            # wrapped echo of the prompt therefore cannot count as execution.
            self.prompt = PROMPTS["shell"] if mode == "shell" else (
                f'Use two sequential tool calls. First run a shell command that prints the concatenation of "CANARY-", "OK:", and "{run_id}" '
                'as one output line and exits immediately. After that tool call completes, use a separate tool call to run sleep 60 '
                'so the orchestrator can stop this session. Do not combine printing and sleeping in one tool call. '
                'Do not edit files or start another agent.')
        self.session = self.native = None
        self.started_at = self.running_deadline = None
        self.start_attempted = self.stopped = False

    def verify_helper_digest(self):
        try:
            self.helper_sha256 = compute_helper_sha256()
        except OSError:
            raise StageFailure("helper_version", "helper_bundle_unreadable")
        if self.expected_helper_sha256 is not None and self.helper_sha256 != self.expected_helper_sha256:
            raise StageFailure("helper_version", "helper_sha256_mismatch")

    def verify_candidate_host_release(self):
        if not self.candidate_sha:
            return
        deadline = time.monotonic() + self.converge_timeout
        reason = "candidate_release_not_installed"
        release_dir = self.host_release_root / "releases" / self.candidate_sha
        current_link = self.host_release_root / "current"
        verified_pyz = None
        verified_pyz_sha = None
        while time.monotonic() < deadline:
            if not release_dir.is_dir():
                reason = "candidate_release_not_installed"
                self.pause(deadline)
                continue
            try:
                is_current = current_link.is_symlink() and current_link.resolve() == release_dir.resolve()
            except OSError:
                is_current = False
            if not is_current:
                reason = "current_release_not_converged"
                self.pause(deadline)
                continue
            if verified_pyz is None:
                try:
                    meta = json.loads((release_dir / "release.json").read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    raise StageFailure("host_release", "candidate_release_json_invalid")
                if not isinstance(meta, dict) or meta.get("host_version") != self.candidate_sha:
                    raise StageFailure("host_release", "candidate_release_version_mismatch")
                artifacts = meta.get("artifacts") if isinstance(meta.get("artifacts"), dict) else {}
                pyz_meta = artifacts.get("hub.pyz") if isinstance(artifacts.get("hub.pyz"), dict) else {}
                expected_sha = pyz_meta.get("sha256")
                expected_size = pyz_meta.get("size")
                if not isinstance(expected_sha, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_sha):
                    raise StageFailure("host_release", "candidate_hub_pyz_metadata_invalid")
                if not isinstance(expected_size, int) or expected_size <= 0:
                    raise StageFailure("host_release", "candidate_hub_pyz_metadata_invalid")
                hub_pyz = release_dir / "hub.pyz"
                try:
                    if hub_pyz.is_symlink() or not hub_pyz.is_file():
                        raise StageFailure("host_release", "candidate_hub_pyz_missing")
                    data = hub_pyz.read_bytes()
                except OSError:
                    raise StageFailure("host_release", "candidate_hub_pyz_missing")
                if len(data) != expected_size or hashlib.sha256(data).hexdigest() != expected_sha:
                    raise StageFailure("host_release", "candidate_hub_pyz_digest_mismatch")
                verified_pyz = hub_pyz
                verified_pyz_sha = expected_sha
                # Execute the verified immutable candidate hub.pyz directly via Python
                # without mutating the global ~/.local/bin/hub operator CLI before commit.
                self.hub = [sys.executable, str(verified_pyz)]
                self.hub_pyz_sha256 = verified_pyz_sha
            rc, out, err = self.run([*self.hub, "routing-machines"], deadline=deadline)
            if rc != 0:
                if rc != 124 or reason in ("candidate_release_not_installed", "current_release_not_converged"):
                    reason = self.failure_reason(rc, err)
                self.pause(deadline)
                continue
            try:
                payload = json.loads(out)
            except ValueError:
                reason = "routing_machines_unparseable_json"
                self.pause(deadline)
                continue
            entries = payload.get("hosts", payload.get("machines")) if isinstance(payload, dict) else None
            if not isinstance(entries, list):
                reason = "routing_machines_unparseable_json"
                self.pause(deadline)
                continue
            host_entry = next(
                (
                    entry for entry in entries
                    if isinstance(entry, dict)
                    and (entry.get("machine_id") == self.machine or entry.get("id") == self.machine)
                ),
                None,
            )
            if host_entry is None:
                reason = "host_not_listed"
            elif host_entry.get("connected") is not True:
                reason = "host_disconnected"
            elif host_entry.get("host_version") != self.candidate_sha:
                reason = f"host_version_mismatch:{clean(host_entry.get('host_version'))}"
            elif host_entry.get("version_mismatch") is not False:
                reason = "host_version_mismatch_flag"
            elif time.monotonic() < deadline:
                self.host_version = self.candidate_sha
                return
            self.pause(deadline)
        raise StageFailure("host_release", f"timeout_{reason}")

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
                                   "--max-bytes", "4096"], deadline=deadline)
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

    def archive_intake(self):
        if not self.session:
            return False
        deadline = time.monotonic() + min(self.cmd_timeout, 30)
        rc, out, _ = self.run([*self.hub, "sessions", "show", self.session, "--json"], deadline=deadline)
        if rc != 0:
            return False
        try:
            payload = json.loads(out)
        except ValueError:
            return False
        node = payload.get("session", payload) if isinstance(payload, dict) else None
        intake_id = node.get("intake_id") if isinstance(node, dict) else None
        if not isinstance(intake_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", intake_id):
            return False
        rc, _, _ = self.run(
            [*self.hub, "intake", "update", intake_id, "--status", "archived", "--reason", "deploy_gate_shell_canary"],
            deadline=deadline,
        )
        return rc == 0

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
            self.verify_helper_digest()
            self.verify_candidate_host_release()
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
            if self.candidate_sha is not None and self.session:
                try:
                    self.intake_archived = self.archive_intake()
                except Exception:
                    self.intake_archived = False
                if result["ok"] and not self.intake_archived:
                    result.update(ok=False, stage="cleanup", reason="intake_archive_failed")
        result["session_id"] = self.session
        if self.helper_sha256 is not None:
            result["helper_sha256"] = self.helper_sha256
        if self.candidate_sha is not None:
            result["candidate_sha"] = self.candidate_sha
            result["run_token"] = self.run_token
            result["host_version"] = self.host_version
            result["hub_pyz_sha256"] = self.hub_pyz_sha256
            result["intake_archived"] = bool(self.intake_archived)
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
    p.add_argument("--candidate-sha", default=None, help="40-hex candidate commit SHA for pre-commit deploy gate")
    p.add_argument("--run-token", default=None, help="32..64-hex run token binding the pre-commit deploy gate verdict")
    p.add_argument("--helper-sha256", default=None, help="64-hex expected SHA-256 of the installed canary helper bundle")
    p.add_argument("--print-helper-sha256", action="store_true", help="print canonical helper bundle SHA-256 and exit")
    args = p.parse_args()
    if args.print_helper_sha256:
        print(compute_helper_sha256())
        return 0
    bound_args = (args.candidate_sha, args.run_token, args.helper_sha256)
    if any(v is not None for v in bound_args):
        if not args.deploy_gate or not all(v is not None for v in bound_args):
            p.error("--candidate-sha, --run-token, and --helper-sha256 must be provided together with --deploy-gate")
        if not re.fullmatch(r"[0-9a-f]{40}", args.candidate_sha):
            p.error("--candidate-sha must be a 40-character lowercase hex SHA")
        if not re.fullmatch(r"[0-9a-f]{32,64}", args.run_token):
            p.error("--run-token must be a 32..64-character lowercase hex token")
        if not re.fullmatch(r"[0-9a-f]{64}", args.helper_sha256):
            p.error("--helper-sha256 must be a 64-character lowercase hex SHA-256")
    mode = args.mode or "full"
    run_id = f"{args.candidate_sha[:12]}-{args.run_token[:12]}" if args.candidate_sha else uuid.uuid4().hex[:12]
    result = Canary(
        mode,
        run_id,
        candidate_sha=args.candidate_sha,
        run_token=args.run_token,
        expected_helper_sha256=args.helper_sha256,
    ).execute()
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
