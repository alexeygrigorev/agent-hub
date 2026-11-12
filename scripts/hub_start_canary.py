#!/usr/bin/env python3
"""Live start canary for Hub: start, observe, capture, stop, confirm gone.

Drives the public CLIs only (`hub sessions start|list|capture|stop`,
`a list --json --running`). See docs/HUB-START-CANARY.md.
"""
import argparse, datetime, errno, fcntl, hashlib, json, os, pathlib, re, shlex, signal, stat, subprocess, sys, time, uuid

ALERT_TO = "dev@dtcdev.click"  # fixed on purpose: never configurable, never orin@
ALERT_FROM = "alerts@dtcdev.click"
MSMTP_ACCOUNT = "alerts"
MARKER = "CANARY-OK"
PROMPTS = {"full": "Reply CANARY-OK and exit", "shell": "printf CANARY-OK; sleep 30"}
STAGES = ("helper_version", "lock", "host_release", "start", "running", "list_host_workspace", "capture", "stop", "gone", "cleanup")
FAIL_THRESHOLD = 2
RECOVER_THRESHOLD = 2
HELPER_FILES = ("hub-deploy-gate-forced.sh", "hub-start-canary.sh", "hub_start_canary.py")

# Fixed trusted ceilings for pre-commit deploy-gate mode (240s active + 60s cleanup < 360s outer wrapper).
TRUSTED_CONVERGE_TIMEOUT_S = 90.0
TRUSTED_RUNNING_TIMEOUT_S = 90.0
TRUSTED_CAPTURE_TIMEOUT_S = 30.0
TRUSTED_STOP_TIMEOUT_S = 45.0
TRUSTED_CMD_TIMEOUT_S = 45.0
TRUSTED_ACTIVE_BUDGET_S = 240.0
TRUSTED_CLEANUP_BUDGET_S = 60.0
TRUSTED_LOCK_WAIT_S = 5.0
PERIODIC_DEFAULT_LOCK_WAIT_S = 120.0
HOST_UPDATER_TRIGGER_ARGV = (
    "systemctl",
    "--user",
    "start",
    "--no-block",
    "agent-hub2-host-update.service",
)


class StageFailure(Exception):
    def __init__(self, stage, reason):
        super().__init__(f"{stage}: {reason}")
        self.stage, self.reason = stage, reason


class CanaryTerminated(BaseException):
    def __init__(self, signum, sig_name):
        super().__init__(f"terminated by {sig_name}")
        self.signum = signum
        self.sig_name = sig_name


def read_regular_nofollow(path, max_bytes=64 * 1024 * 1024):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise OSError(errno.EINVAL, "not a regular file")
        if st.st_size < 0 or st.st_size > max_bytes:
            raise OSError(errno.EFBIG, "regular file size out of bounds")
        chunks = []
        total = 0
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise OSError(errno.EFBIG, "regular file grew beyond limit")
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def create_sealed_pyz_fd(data):
    if hasattr(os, "memfd_create"):
        mfd_allow_sealing = getattr(os, "MFD_ALLOW_SEALING", 0x0002)
        f_add_seals = getattr(fcntl, "F_ADD_SEALS", 1033)
        f_get_seals = getattr(fcntl, "F_GET_SEALS", 1034)
        seals = (
            getattr(fcntl, "F_SEAL_SEAL", 0x0001)
            | getattr(fcntl, "F_SEAL_SHRINK", 0x0002)
            | getattr(fcntl, "F_SEAL_GROW", 0x0004)
            | getattr(fcntl, "F_SEAL_WRITE", 0x0008)
        )
        fd = os.memfd_create("hub_candidate_pyz", mfd_allow_sealing)
        try:
            view = memoryview(data)
            written = 0
            while written < len(view):
                n = os.write(fd, view[written:])
                if n <= 0:
                    raise OSError(errno.EIO, "short memfd write")
                written += n
            os.lseek(fd, 0, os.SEEK_SET)
            fcntl.fcntl(fd, f_add_seals, seals)
            if (fcntl.fcntl(fd, f_get_seals) & seals) != seals:
                raise OSError(errno.EPERM, "memfd seal verification failed")
            os.set_inheritable(fd, True)
            return fd
        except BaseException:
            os.close(fd)
            raise
    raise OSError(errno.ENOSYS, "sealed memfd unsupported")


def compute_helper_sha256(directory=None):
    base = pathlib.Path(directory) if directory else pathlib.Path(__file__).resolve().parent
    digest = hashlib.sha256()
    for name in HELPER_FILES:
        data = read_regular_nofollow(base / name, max_bytes=4 * 1024 * 1024)
        digest.update(f"{name}\0{len(data)}\0".encode("utf-8"))
        digest.update(data)
        digest.update(b"\0")
    return digest.hexdigest()


def raw_state_dir_path():
    base = os.environ.get("HUB_CANARY_STATE_DIR")
    if not base:
        xdg = os.environ.get("XDG_STATE_HOME") or str(pathlib.Path.home() / ".local" / "state")
        base = str(pathlib.Path(xdg) / "agent-hub-canary")
    return pathlib.Path(base)


def fsync_dir(path):
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def ensure_private_dir(path, *, stage="lock", prefix="state_dir"):
    target = pathlib.Path(os.path.abspath(path))
    missing = []
    seen_existing = False
    cur = target
    while True:
        try:
            cst = os.lstat(cur)
        except FileNotFoundError:
            if seen_existing:
                raise StageFailure(stage, f"{prefix}_stat_failed")
            missing.append(cur)
        except OSError:
            raise StageFailure(stage, f"{prefix}_stat_failed")
        else:
            if stat.S_ISLNK(cst.st_mode):
                raise StageFailure(stage, f"{prefix}_symlink")
            if not stat.S_ISDIR(cst.st_mode):
                raise StageFailure(stage, f"{prefix}_not_directory")
            seen_existing = True
        parent = cur.parent
        if parent == cur:
            break
        cur = parent
    if missing:
        try:
            for directory in reversed(missing):
                mode = 0o700 if directory == target else 0o755
                os.mkdir(directory, mode)
                fsync_dir(directory)
                if directory.parent != directory:
                    fsync_dir(directory.parent)
        except OSError:
            raise StageFailure(stage, f"{prefix}_create_failed")
    try:
        st = os.lstat(target)
    except OSError:
        raise StageFailure(stage, f"{prefix}_stat_failed")
    if stat.S_ISLNK(st.st_mode):
        raise StageFailure(stage, f"{prefix}_symlink")
    if not stat.S_ISDIR(st.st_mode):
        raise StageFailure(stage, f"{prefix}_not_directory")
    if st.st_uid != os.geteuid():
        raise StageFailure(stage, f"{prefix}_wrong_owner")
    if (st.st_mode & 0o077) != 0:
        raise StageFailure(stage, f"{prefix}_not_private")
    try:
        home = pathlib.Path(os.path.abspath(pathlib.Path.home()))
        chain = [target]
        cur = target
        while cur.parent != cur and cur != home:
            parent = cur.parent
            pst = os.lstat(parent)
            if stat.S_ISLNK(pst.st_mode):
                raise StageFailure(stage, f"{prefix}_symlink")
            if not stat.S_ISDIR(pst.st_mode):
                raise StageFailure(stage, f"{prefix}_not_directory")
            chain.append(parent)
            if pst.st_uid != os.geteuid() or parent == home:
                break
            cur = parent
        for directory in reversed(chain):
            fsync_dir(directory)
    except StageFailure:
        raise
    except OSError:
        raise StageFailure(stage, f"{prefix}_create_failed")
    return target


def acquire_lock_fd(lock_path, wait_s, *, stage="lock", busy_reason="deploy_gate_lock_busy"):
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise StageFailure(stage, "lock_file_symlink")
        raise StageFailure(stage, "lock_file_open_failed")
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise StageFailure(stage, "lock_file_not_regular")
        if st.st_uid != os.geteuid():
            raise StageFailure(stage, "lock_file_wrong_owner")
        if (st.st_mode & 0o077) != 0:
            raise StageFailure(stage, "lock_file_not_private")
        deadline = time.monotonic() + max(0.0, float(wait_s))
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise StageFailure(stage, busy_reason)
                time.sleep(min(0.05, remaining))
    except BaseException:
        os.close(fd)
        raise


def atomic_write_private_json(path, payload):
    target = pathlib.Path(path)
    tmp = target.with_name(f".{target.name}.tmp.{os.getpid()}.{uuid.uuid4().hex[:8]}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(tmp, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
        fsync_dir(target.parent)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def durable_unlink(path):
    target = pathlib.Path(path)
    try:
        os.unlink(target)
    except FileNotFoundError:
        return
    fsync_dir(target.parent)


def env_num(name, default):
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return float(default)


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def clean(text, limit=80):
    return re.sub(r"[^A-Za-z0-9_.:-]", "_", str(text))[:limit] or "unknown"


def unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON field")
        value[key] = item
    return value


def valid_native_id(val):
    return Canary.valid_native_id(val)


def paired_stream_output_ok(captured, expected, engine):
    """Complete provider frames from ONE capture only; never repair clipped JSON."""
    if len(captured.encode()) > 65536:
        return False
    events = []
    for index, line in enumerate(captured.splitlines()):
        try:
            event = json.loads(line, object_pairs_hook=unique_object)
        except (ValueError, TypeError):
            if index == 0:  # A bounded tail may start inside an earlier frame.
                continue
            return False
        if not isinstance(event, dict):
            return False
        events.append(event)
    if len(events) > 128:
        return False
    uses, results = {}, set()
    proven = False

    def marker_in(text):
        return isinstance(text, str) and any(line.strip() == expected for line in text.splitlines())

    def provider_id(value):
        return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,100}", value) is not None

    for event in events:
        if engine == "claude":
            message = event.get("message")
            if event.get("type") not in ("assistant", "user") or not isinstance(message, dict):
                continue
            blocks = message.get("content")
            if not isinstance(blocks, list):
                continue
            session = event.get("session_id")
            for block in blocks:
                if not isinstance(block, dict):
                    continue
                if event["type"] == "assistant" and block.get("type") == "tool_use":
                    identity = block.get("id")
                    if not provider_id(identity) or not valid_native_id(session):
                        return False
                    if identity in uses:
                        return False
                    params = block.get("input")
                    if (
                        message.get("role") != "assistant"
                        or block.get("name") != "Bash"
                        or not isinstance(params, dict)
                        or not isinstance(params.get("command"), str)
                    ):
                        continue
                    background = params.get("run_in_background")
                    if background is not None and type(background) is not bool:
                        return False
                    uses[identity] = (session, background is True)
                elif event["type"] == "user" and block.get("type") == "tool_result":
                    identity = block.get("tool_use_id")
                    if not provider_id(identity) or identity in results:
                        return False
                    results.add(identity)
                    if identity not in uses:  # An orphan alone can never prove output.
                        continue
                    use_session, is_background = uses[identity]
                    detail = event.get("tool_use_result")
                    if (
                        session != use_session
                        or message.get("role") != "user"
                        or block.get("is_error") is not False
                        or not isinstance(detail, dict)
                        or detail.get("interrupted") is not False
                        or detail.get("stderr") != ""
                        or not isinstance(block.get("content"), str)
                    ):
                        return False
                    if is_background:
                        bg_task_id = detail.get("backgroundTaskId")
                        if (
                            not provider_id(bg_task_id)
                            or detail.get("stdout") != ""
                            or not block["content"].startswith(f"Command running in background with ID: {bg_task_id}.")
                        ):
                            return False
                        continue
                    if detail.get("backgroundTaskId") is not None or detail.get("stdout") != block["content"]:
                        return False
                    proven = proven or marker_in(block["content"])
        else:
            step = event.get("step_update")
            if event.get("event") != "step_update" or not isinstance(step, dict) or step.get("step_type") != "tool":
                continue
            conversation, index = step.get("conversation_id"), step.get("step_index")
            tool = step.get("tool_name")
            info = step.get("tool_info")
            if (
                not valid_native_id(conversation)
                or type(index) is not int
                or not 0 <= index <= 2**31 - 1
                or tool not in ("run_command", "manage_task", "schedule", "command_status", "wait", "wait_5_seconds")
                or not isinstance(info, dict)
                or info.get("name") != tool
            ):
                return False
            if any(
                part.get(key) not in (None, False, "")
                for part in (event, step, info)
                for key in ("error", "is_error", "interrupted")
            ):
                return False
            params = info.get("parameters")
            if not isinstance(params, dict):
                return False
            if tool == "run_command":
                if (
                    set(params) != {"CommandLine"}
                    or not isinstance(params["CommandLine"], str)
                    or not params["CommandLine"]
                ):
                    return False
            elif tool == "manage_task":
                if (
                    not params
                    or not set(params) <= {"Action", "TaskId", "Input"}
                    or params.get("Action") not in ("list", "status", "kill", "send_input")
                    or not all(isinstance(v, str) and bool(v) for v in params.values())
                ):
                    return False
            elif tool == "schedule":
                if (
                    not params
                    or not set(params)
                    <= {
                        "Prompt",
                        "DurationSeconds",
                        "CronExpression",
                        "TimerCondition",
                        "ExactTiming",
                        "IsDaemon",
                        "MaxIterations",
                    }
                    or not isinstance(params.get("Prompt"), str)
                    or not params["Prompt"]
                    or not all(
                        (
                            k in ("Prompt", "CronExpression", "TimerCondition")
                            and isinstance(v, str)
                            and bool(v)
                        )
                        or (k in ("DurationSeconds", "MaxIterations") and type(v) is int and v > 0)
                        or (k in ("ExactTiming", "IsDaemon") and type(v) is bool)
                        for k, v in params.items()
                    )
                ):
                    return False
            elif (
                bool(params)
                if tool == "wait_5_seconds"
                else (
                    not params
                    or not all(
                        provider_id(k)
                        and (
                            (isinstance(v, str) and bool(v))
                            or (type(v) is int and v >= 0)
                            or type(v) is bool
                        )
                        for k, v in params.items()
                    )
                )
            ):
                return False
            identity = (conversation, index)
            if step.get("state") == "ACTIVE":
                if identity in uses or identity in results or "output" in info:
                    return False
                uses[identity] = (tool, params)
            elif step.get("state") == "DONE":
                if identity in results:
                    return False
                results.add(identity)
                if identity in uses and uses[identity] != (tool, params):
                    return False
                if not isinstance(info.get("output"), str):
                    return False
                if tool != "run_command" or identity not in uses:
                    continue
                # Only the native wrapper's anchored zero status counts. A fake
                # zero status embedded inside command output cannot override it.
                match = re.fullmatch(
                    r"Created At: ([^\n]+)\nCompleted At: ([^\n]+)\n"
                    r"Command exited with code 0\.\n\nCommand output:\n([\s\S]*)",
                    info["output"],
                )
                if not match:
                    continue
                try:
                    created, completed = (datetime.datetime.fromisoformat(match[group]) for group in (1, 2))
                    if created.tzinfo is None or completed.tzinfo is None or completed < created:
                        return False
                except ValueError:
                    return False
                proven = proven or marker_in(match[3])
            else:
                return False
    return proven


class Canary:
    def __init__(
        self,
        mode,
        run_id,
        *,
        deploy_gate=False,
        candidate_sha=None,
        candidate_cli_sha256=None,
        run_token=None,
        expected_helper_sha256=None,
    ):
        self.mode, self.run_id = mode, run_id
        self.deploy_gate = bool(deploy_gate)
        self.candidate_sha = candidate_sha
        self.candidate_cli_sha256 = candidate_cli_sha256
        self.run_token = run_token
        self.expected_helper_sha256 = expected_helper_sha256
        self.durable_pending_enabled = (not self.deploy_gate) or (self.candidate_sha is not None)
        self.helper_sha256 = None
        self.host_version = None
        self.hub_pyz_sha256 = None
        self.intake_archived = None
        self.host_updater_triggered = False
        self.host_updater_trigger = "not_needed"
        self.host_updater_trigger_error = None
        self.pyz_fd = None
        self.hub_pass_fds = ()
        self.lock_fd = None
        self.state_dir_path = None
        self.pending_dir = None
        self.pending_path = None
        self.current_stage = "helper_version"
        self.in_cleanup = False
        self.termination_reason = None
        self.hub = shlex.split(os.environ.get("HUB_BIN", "hub"))
        self.a = shlex.split(os.environ.get("A_BIN", "a"))
        self.poll = max(0.005, env_num("HUB_CANARY_POLL_S", 2))
        if candidate_sha is not None:
            self.converge_timeout = min(env_num("HUB_CANARY_CONVERGE_TIMEOUT_S", TRUSTED_CONVERGE_TIMEOUT_S), TRUSTED_CONVERGE_TIMEOUT_S)
            self.running_timeout = min(env_num("HUB_CANARY_RUNNING_TIMEOUT_S", TRUSTED_RUNNING_TIMEOUT_S), TRUSTED_RUNNING_TIMEOUT_S)
            self.capture_timeout = min(env_num("HUB_CANARY_CAPTURE_TIMEOUT_S", TRUSTED_CAPTURE_TIMEOUT_S), TRUSTED_CAPTURE_TIMEOUT_S)
            self.stop_timeout = min(env_num("HUB_CANARY_STOP_TIMEOUT_S", TRUSTED_STOP_TIMEOUT_S), TRUSTED_STOP_TIMEOUT_S)
            self.cmd_timeout = min(env_num("HUB_CANARY_CMD_TIMEOUT_S", TRUSTED_CMD_TIMEOUT_S), TRUSTED_CMD_TIMEOUT_S)
            self.active_budget = min(env_num("HUB_CANARY_ACTIVE_BUDGET_S", TRUSTED_ACTIVE_BUDGET_S), TRUSTED_ACTIVE_BUDGET_S)
            self.cleanup_budget = min(env_num("HUB_CANARY_CLEANUP_BUDGET_S", self.stop_timeout), TRUSTED_CLEANUP_BUDGET_S)
            self.lock_wait = min(max(0.0, env_num("HUB_CANARY_LOCK_WAIT_S", TRUSTED_LOCK_WAIT_S)), TRUSTED_LOCK_WAIT_S)
        else:
            self.converge_timeout = env_num("HUB_CANARY_CONVERGE_TIMEOUT_S", 120)
            self.running_timeout = env_num("HUB_CANARY_RUNNING_TIMEOUT_S", 180)
            self.capture_timeout = env_num("HUB_CANARY_CAPTURE_TIMEOUT_S", 120 if mode == "full" else 30)
            self.stop_timeout = env_num("HUB_CANARY_STOP_TIMEOUT_S", 60)
            self.cmd_timeout = env_num("HUB_CANARY_CMD_TIMEOUT_S", 90)
            self.active_budget = env_num("HUB_CANARY_ACTIVE_BUDGET_S", 300)
            self.cleanup_budget = env_num("HUB_CANARY_CLEANUP_BUDGET_S", self.stop_timeout)
            self.lock_wait = max(0.0, env_num("HUB_CANARY_LOCK_WAIT_S", PERIODIC_DEFAULT_LOCK_WAIT_S))
        self.active_deadline = None
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
                    "Use two sequential shell tool calls. First run a separate foreground shell command that prints the "
                    f'concatenation of "CANARY-", "OK:", and "{sha12}:{run_token}" as one output line and let it finish. '
                    "Immediately after the marker print finishes, run a separate foreground `sleep 60` command using the "
                    "existing shell tool with a supported timeout sufficient for the 60-second hold so the orchestrator can "
                    "observe and stop this session before any final prose or completion bookkeeping. "
                    "Do not chain the marker print with the hold in one command, do not use a background flag, do not include "
                    "the marker or nonce in the hold command, do not echo proof or emit waiting prose, and do not search for "
                    "or call any monitor, task-output, or status tools or poll in a loop. "
                    "Apart from mandatory Hub first-action/completion evidence files if required by the session wrapper, "
                    "do not create or edit other files, start another agent, or exit early."
                )
            )
        else:
            self.marker = MARKER if mode == "shell" else f"{MARKER}:{run_id}"
            # The complete expected output never occurs in the model prompt. A
            # wrapped echo of the prompt therefore cannot count as execution.
            self.prompt = PROMPTS["shell"] if mode == "shell" else (
                "Use two sequential shell tool calls. First run a separate foreground shell command that prints the "
                f'concatenation of "CANARY-", "OK:", and "{run_id}" as one output line and let it finish. '
                "Immediately after the marker print finishes, run a separate foreground `sleep 60` command using the "
                "existing shell tool with a supported timeout sufficient for the 60-second hold so the orchestrator can "
                "observe and stop this session before any final prose or completion bookkeeping. "
                "Do not chain the marker print with the hold in one command, do not use a background flag, do not include "
                "the marker or nonce in the hold command, do not echo proof or emit waiting prose, and do not search for "
                "or call any monitor, task-output, or status tools or poll in a loop. "
                "Apart from mandatory Hub first-action/completion evidence files if required by the session wrapper, "
                "do not create or edit other files, start another agent, or exit early."
            )
        if mode == "full":
            # The picker chooses the runtime after launch. Keep the existing
            # non-Claude instructions verbatim behind an explicit runtime branch.
            marker_suffix = f"{candidate_sha[:12]}:{run_token}" if candidate_sha and run_token else run_id
            self.prompt = (
                "If this runtime is Claude Code, first use its existing native Bash tool to start a separate bounded "
                "`sleep 60` hold with `run_in_background: true`, and retain the returned background task id. "
                "Then run a separate foreground shell command that prints the "
                f'concatenation of "CANARY-", "OK:", and "{marker_suffix}" as one output line and let it finish. '
                "Immediately after that foreground marker result, enter the provider-native blocking Monitor or TaskOutput "
                "wait on that exact background task id so the orchestrator can observe and stop this session before any "
                "final prose or completion bookkeeping. The foreground marker must finish; background completion is not proof. "
                "Do not put the marker or nonce in the background hold, echo proof, emit waiting prose, poll in a loop, "
                "substitute another sleep mechanism, or wait externally. Apart from mandatory Hub first-action/completion "
                "evidence files if required by the session wrapper, do not create or edit other files, start another agent, "
                "or exit early. For every other runtime, follow this unchanged sequence: " + self.prompt
            )
        self.session = self.native = self.engine = None
        self.started_at = self.running_deadline = None
        self.start_attempted = self.stopped = False

    def stage_deadline(self, timeout_s):
        target = time.monotonic() + timeout_s
        if not self.in_cleanup and self.active_deadline is not None:
            return min(target, self.active_deadline)
        return target

    def verify_helper_digest(self):
        self.current_stage = "helper_version"
        try:
            self.helper_sha256 = compute_helper_sha256()
        except OSError:
            raise StageFailure("helper_version", "helper_bundle_unreadable")
        if self.expected_helper_sha256 is not None and self.helper_sha256 != self.expected_helper_sha256:
            raise StageFailure("helper_version", "helper_sha256_mismatch")

    def acquire_deploy_gate_lock(self):
        if not self.durable_pending_enabled:
            return
        self.current_stage = "lock"
        self.state_dir_path = ensure_private_dir(raw_state_dir_path(), stage="lock", prefix="state_dir")
        wait_s = self.lock_wait
        if self.active_deadline is not None:
            wait_s = min(wait_s, max(0.0, self.active_deadline - time.monotonic()))
        self.lock_fd = acquire_lock_fd(
            self.state_dir_path / "deploy-gate.lock",
            wait_s,
            stage="lock",
            busy_reason="deploy_gate_lock_busy",
        )
        self.pending_dir = ensure_private_dir(self.state_dir_path / "pending", stage="lock", prefix="pending_dir")
        self.pending_path = self.pending_dir / f"{self.run_id}.json"

    def write_pending_state(self):
        if self.pending_path is None:
            return
        payload = {
            "run_id": self.run_id,
            "mode": self.mode,
            "name": self.name,
            "key": self.key,
            "repo": self.repo,
            "machine": self.machine,
            "workspace": str(pathlib.Path(self.workspace).resolve()),
            "prompt": self.prompt,
            "candidate_sha": self.candidate_sha,
            "session_id": self.session,
            "native_id": self.native,
            "updated_at": now_iso(),
        }
        atomic_write_private_json(self.pending_path, payload)

    def clear_pending_state(self):
        if self.pending_path is None:
            return
        durable_unlink(self.pending_path)

    @staticmethod
    def valid_session_id(value):
        return isinstance(value, str) and bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value))

    @staticmethod
    def valid_native_id(value):
        return isinstance(value, str) and bool(
            re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", value)
        )

    @staticmethod
    def row_authoritative_key(item):
        ck = item.get("client_key")
        k = item.get("key")
        has_ck = isinstance(ck, str) and bool(ck)
        has_k = isinstance(k, str) and bool(k)
        if has_ck and has_k and ck != k:
            return None, True
        if has_ck:
            return ck, False
        if has_k:
            return k, False
        return None, False

    @staticmethod
    def row_matches_scope(item, expected_repo, expected_machine, expected_workspace):
        repo = item.get("project_id") or item.get("repo") or item.get("project")
        machine = item.get("host") or item.get("machine_id") or item.get("machine")
        workspace = item.get("workspace")
        if repo != expected_repo or machine != expected_machine:
            return False
        if not isinstance(workspace, str) or not workspace:
            return False
        try:
            return pathlib.Path(workspace).resolve() == pathlib.Path(expected_workspace).resolve()
        except OSError:
            return False

    @staticmethod
    def row_conflicts_scope(item, expected_repo, expected_machine, expected_workspace):
        repo = item.get("project_id") or item.get("repo") or item.get("project")
        machine = item.get("host") or item.get("machine_id") or item.get("machine")
        workspace = item.get("workspace")
        if repo is not None and repo != expected_repo:
            return True
        if machine is not None and machine != expected_machine:
            return True
        if workspace is not None:
            if not isinstance(workspace, str) or not workspace:
                return True
            try:
                if pathlib.Path(workspace).resolve() != pathlib.Path(expected_workspace).resolve():
                    return True
            except OSError:
                return True
        return False

    @classmethod
    def resolve_owned_session(
        cls,
        items,
        *,
        expected_sid,
        expected_key,
        expected_repo,
        expected_machine,
        expected_workspace,
    ):
        if not isinstance(items, list):
            return None, None, True
        valid_sid = expected_sid if cls.valid_session_id(expected_sid) else None
        valid_key = expected_key if isinstance(expected_key, str) and expected_key else None
        if valid_sid is not None:
            sid_rows = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                row_sid = item.get("id")
                row_key, key_conflict = cls.row_authoritative_key(item)
                if key_conflict and (
                    row_sid == valid_sid
                    or (valid_key and (item.get("client_key") == valid_key or item.get("key") == valid_key))
                ):
                    return None, None, True
                if valid_key and row_key == valid_key and row_sid != valid_sid:
                    return None, None, True
                if row_sid == valid_sid:
                    if valid_key and row_key is not None and row_key != valid_key:
                        return None, None, True
                    if cls.row_conflicts_scope(item, expected_repo, expected_machine, expected_workspace):
                        return None, None, True
                    sid_rows.append(item)
            native_ids = set()
            for item in sid_rows:
                row_nat = item.get("native_id")
                if row_nat is not None:
                    if not cls.valid_native_id(row_nat):
                        return None, None, True
                    native_ids.add(row_nat)
            if len(native_ids) > 1:
                return None, None, True
            resolved_native = next(iter(native_ids), None)
            if resolved_native is not None:
                for item in items:
                    if isinstance(item, dict) and item.get("id") != valid_sid and item.get("native_id") == resolved_native:
                        return None, None, True
            return valid_sid, resolved_native, False

        if valid_key is None:
            return None, None, False
        key_rows = []
        for item in items:
            if not isinstance(item, dict):
                continue
            row_key, key_conflict = cls.row_authoritative_key(item)
            if key_conflict and (item.get("client_key") == valid_key or item.get("key") == valid_key):
                return None, None, True
            if row_key == valid_key:
                row_sid = item.get("id")
                if not cls.valid_session_id(row_sid):
                    return None, None, True
                if not cls.row_matches_scope(item, expected_repo, expected_machine, expected_workspace):
                    return None, None, True
                key_rows.append(item)
        if not key_rows:
            return None, None, False
        sids = {item.get("id") for item in key_rows}
        native_ids = set()
        for item in key_rows:
            row_nat = item.get("native_id")
            if row_nat is not None:
                if not cls.valid_native_id(row_nat):
                    return None, None, True
                native_ids.add(row_nat)
        if len(sids) != 1 or len(native_ids) > 1:
            return None, None, True
        resolved_sid = next(iter(sids))
        resolved_native = next(iter(native_ids), None)
        for item in items:
            if not isinstance(item, dict):
                continue
            row_key, _ = cls.row_authoritative_key(item)
            if row_key == valid_key:
                continue
            if item.get("id") == resolved_sid:
                return None, None, True
            if resolved_native is not None and item.get("native_id") == resolved_native:
                return None, None, True
        return resolved_sid, resolved_native, False

    @staticmethod
    def resolve_updater_xdg_runtime_dir():
        raw = os.environ.get("XDG_RUNTIME_DIR", "").strip()
        candidate = pathlib.Path(raw) if raw else pathlib.Path(f"/run/user/{os.geteuid()}")
        if not candidate.is_absolute():
            return None, "host_updater_xdg_runtime_dir_relative"
        try:
            st = os.lstat(candidate)
        except FileNotFoundError:
            return None, "host_updater_xdg_runtime_dir_missing"
        except OSError:
            return None, "host_updater_xdg_runtime_dir_stat_failed"
        if stat.S_ISLNK(st.st_mode):
            return None, "host_updater_xdg_runtime_dir_symlink"
        if not stat.S_ISDIR(st.st_mode):
            return None, "host_updater_xdg_runtime_dir_not_directory"
        if st.st_uid != os.geteuid():
            return None, "host_updater_xdg_runtime_dir_wrong_owner"
        if (st.st_mode & 0o077) != 0:
            return None, "host_updater_xdg_runtime_dir_not_private"
        return str(candidate), None

    def trigger_host_updater(self, deadline):
        if self.host_updater_triggered:
            return
        self.host_updater_triggered = True
        effective_deadline = deadline
        if not self.in_cleanup and self.active_deadline is not None:
            effective_deadline = min(effective_deadline, self.active_deadline)
        if effective_deadline - time.monotonic() <= 0:
            self.host_updater_trigger = "timeout"
            self.host_updater_trigger_error = "host_updater_trigger_timeout"
            return
        xdg_dir, xdg_err = self.resolve_updater_xdg_runtime_dir()
        if xdg_err is not None:
            self.host_updater_trigger = "failed"
            self.host_updater_trigger_error = xdg_err
            return
        updater_env = dict(os.environ)
        updater_env["XDG_RUNTIME_DIR"] = xdg_dir
        rc, _, _ = self.run(list(HOST_UPDATER_TRIGGER_ARGV), deadline=deadline, env=updater_env)
        if rc == 0:
            self.host_updater_trigger = "triggered"
            self.host_updater_trigger_error = None
        elif rc == 124:
            self.host_updater_trigger = "timeout"
            self.host_updater_trigger_error = "host_updater_trigger_timeout"
        else:
            self.host_updater_trigger = "failed"
            self.host_updater_trigger_error = f"host_updater_trigger_exit_{rc}"

    def verify_candidate_host_release(self):
        if not self.candidate_sha:
            return
        self.current_stage = "host_release"
        deadline = self.stage_deadline(self.converge_timeout)
        reason = "candidate_release_not_installed"
        release_dir = self.host_release_root / "releases" / self.candidate_sha
        current_link = self.host_release_root / "current"
        verified_pyz = False
        while time.monotonic() < deadline:
            try:
                rel_st = os.lstat(release_dir)
                is_rel_dir = stat.S_ISDIR(rel_st.st_mode) and not stat.S_ISLNK(rel_st.st_mode)
            except OSError:
                is_rel_dir = False
            if not is_rel_dir:
                reason = "candidate_release_not_installed"
                if not self.host_updater_triggered:
                    self.trigger_host_updater(deadline)
                self.pause(deadline)
                continue
            try:
                is_current = current_link.is_symlink() and current_link.resolve() == release_dir.resolve()
            except OSError:
                is_current = False
            if not is_current:
                reason = "current_release_not_converged"
                if not self.host_updater_triggered:
                    self.trigger_host_updater(deadline)
                self.pause(deadline)
                continue
            if not verified_pyz:
                try:
                    raw_meta = read_regular_nofollow(release_dir / "release.json", max_bytes=1024 * 1024)
                    meta = json.loads(raw_meta.decode("utf-8"))
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
                if self.candidate_cli_sha256 is not None and expected_sha != self.candidate_cli_sha256:
                    raise StageFailure("host_release", "candidate_hub_pyz_cli_sha_mismatch")
                try:
                    data = read_regular_nofollow(release_dir / "hub.pyz")
                except OSError:
                    raise StageFailure("host_release", "candidate_hub_pyz_missing")
                if len(data) != expected_size or hashlib.sha256(data).hexdigest() != expected_sha:
                    raise StageFailure("host_release", "candidate_hub_pyz_digest_mismatch")
                try:
                    self.pyz_fd = create_sealed_pyz_fd(data)
                except OSError:
                    raise StageFailure("host_release", "candidate_hub_pyz_memfd_failed")
                self.hub_pass_fds = (self.pyz_fd,)
                # Execute the verified candidate hub.pyz from a kernel-sealed anonymous
                # memfd with isolated stdlib startup (-I -S), preventing post-hash
                # replacement on disk and user site-packages injection.
                self.hub = [sys.executable, "-I", "-S", f"/proc/self/fd/{self.pyz_fd}"]
                self.hub_pyz_sha256 = expected_sha
                verified_pyz = True
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
            if not self.host_updater_triggered:
                self.trigger_host_updater(deadline)
            self.pause(deadline)
        failure_reason = f"timeout_{reason}"
        if self.host_updater_trigger_error:
            failure_reason = f"{failure_reason}:{self.host_updater_trigger_error}"
        raise StageFailure("host_release", failure_reason)

    def _reconcile_one_pending_file(self, pfile, deadline):
        try:
            rec = json.loads(read_regular_nofollow(pfile, max_bytes=65536).decode("utf-8"))
        except (OSError, ValueError):
            raise StageFailure("cleanup", "prior_pending_corrupt")
        if not isinstance(rec, dict):
            raise StageFailure("cleanup", "prior_pending_corrupt")
        raw_sid = rec.get("session_id")
        if raw_sid is not None and not self.valid_session_id(raw_sid):
            raise StageFailure("cleanup", "prior_pending_corrupt")
        sid = raw_sid if self.valid_session_id(raw_sid) else None
        raw_native = rec.get("native_id")
        if raw_native is not None and not self.valid_native_id(raw_native):
            raise StageFailure("cleanup", "prior_pending_reconciliation_failed")
        native_id = raw_native if self.valid_native_id(raw_native) else None
        key = rec.get("key") if isinstance(rec.get("key"), str) and rec.get("key") else None
        repo = rec.get("repo") if isinstance(rec.get("repo"), str) and rec.get("repo") else None
        machine = rec.get("machine") if isinstance(rec.get("machine"), str) and rec.get("machine") else None
        workspace = rec.get("workspace") if isinstance(rec.get("workspace"), str) and rec.get("workspace") else None
        if not key or not repo or not machine or not workspace:
            raise StageFailure("cleanup", "prior_pending_corrupt")
        if not sid and all(isinstance(rec.get(k), str) and rec.get(k) for k in ("repo", "machine", "workspace", "name", "key", "prompt", "mode")):
            replay_args = [
                *self.hub, "sessions", "start", "--no-wait",
                "--repo", rec["repo"], "--machine", rec["machine"],
                "--workspace", rec["workspace"], "--name", rec["name"],
                "--key", rec["key"], "--prompt", rec["prompt"],
            ]
            if rec["mode"] == "shell":
                replay_args += ["--engine", "shell"]
            _, out, _ = self.run(replay_args, deadline=deadline)
            sid = self.extract_session_id(out)
        items = None
        conflict = False
        if not sid or not native_id:
            try:
                items = self.list_items("cleanup", deadline)
            except StageFailure:
                items = None
            if items is not None:
                resolved_sid, resolved_native, conflict = self.resolve_owned_session(
                    items,
                    expected_sid=sid,
                    expected_key=key,
                    expected_repo=repo,
                    expected_machine=machine,
                    expected_workspace=workspace,
                )
                if conflict:
                    raise StageFailure("cleanup", "prior_pending_reconciliation_failed")
                sid = sid or resolved_sid
                if native_id and resolved_native and native_id != resolved_native:
                    raise StageFailure("cleanup", "prior_pending_reconciliation_failed")
                native_id = native_id or resolved_native
        if not sid:
            raise StageFailure("cleanup", "prior_pending_reconciliation_failed")
        confirmed, resolved_native = self.stop_or_confirm_retired(
            sid,
            f"{key}-reconcile",
            deadline,
            expected_native_id=native_id,
            expected_key=key,
            expected_repo=repo,
            expected_machine=machine,
            expected_workspace=workspace,
        )
        if not confirmed:
            raise StageFailure("cleanup", "prior_pending_reconciliation_failed")
        native_id = native_id or resolved_native
        if native_id is not None and not self.valid_native_id(native_id):
            raise StageFailure("cleanup", "prior_pending_reconciliation_failed")
        if native_id:
            try:
                self.wait_gone_for(native_id, deadline)
            except StageFailure:
                raise StageFailure("cleanup", "prior_pending_reconciliation_failed")
        if not self.archive_intake_for(
            sid,
            deadline,
            candidate_sha=rec.get("candidate_sha"),
            mode=rec.get("mode"),
            expected_repo=repo,
            expected_machine=machine,
            expected_workspace=workspace,
        ):
            raise StageFailure("cleanup", "prior_pending_reconciliation_failed")
        try:
            durable_unlink(pfile)
        except OSError:
            raise StageFailure("cleanup", "prior_pending_reconciliation_failed")

    def reconcile_prior_pending(self):
        if self.pending_dir is None:
            return
        self.current_stage = "cleanup"
        pending_files = sorted(self.pending_dir.glob("*.json"))
        for pfile in pending_files:
            if self.pending_path is not None and pfile == self.pending_path:
                continue
            deadline = self.stage_deadline(self.stop_timeout)
            self._reconcile_one_pending_file(pfile, deadline)

    def run(self, argv, timeout=None, *, deadline=None, env=None):
        limit = self.cmd_timeout if timeout is None else timeout
        effective_deadline = deadline
        if not self.in_cleanup and self.active_deadline is not None:
            effective_deadline = self.active_deadline if effective_deadline is None else min(effective_deadline, self.active_deadline)
        if effective_deadline is not None:
            limit = min(limit, effective_deadline - time.monotonic())
        if limit <= 0:
            return 124, "", ""
        pass_fds = self.hub_pass_fds if (self.pyz_fd is not None and f"/proc/self/fd/{self.pyz_fd}" in argv) else ()
        try:
            proc = subprocess.Popen(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
                pass_fds=pass_fds,
                env=env,
            )
        except OSError as error:
            return 127, "", type(error).__name__
        try:
            out, err = proc.communicate(timeout=limit)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            out, err = proc.communicate()
            return 124, out or "", err or ""
        except BaseException:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=1)
            except Exception:
                pass
            raise
        if effective_deadline is not None and time.monotonic() >= effective_deadline:
            return 124, out, err
        return proc.returncode, out, err

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

    @staticmethod
    def extract_session_id(out):
        try:
            data = json.loads(out)
        except ValueError:
            return None
        node = data.get("session", data) if isinstance(data, dict) else None
        sid = node.get("id") if isinstance(node, dict) else None
        if not isinstance(sid, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", sid):
            return None
        return sid

    def remember_receipt(self, out):
        sid = self.extract_session_id(out)
        if not sid:
            return False
        self.session = sid
        try:
            self.write_pending_state()
        except OSError:
            if not self.in_cleanup:
                raise StageFailure("start", "pending_state_write_failed")
        return True

    def start(self):
        self.current_stage = "start"
        ensure_private_dir(self.workspace, stage="start", prefix="workspace")
        self.started_at = time.monotonic()
        self.running_deadline = self.stage_deadline(self.running_timeout)
        try:
            self.write_pending_state()
        except OSError:
            raise StageFailure("start", "pending_state_write_failed")
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
        self.current_stage = "running"
        deadline = self.running_deadline
        reason = "never_listed"
        stage = "running"
        while time.monotonic() < deadline:
            try:
                items = self.list_items("running", deadline)
            except StageFailure as failure:
                if failure.reason == "native_inventory_changed":
                    stage, reason = "running", "native_inventory_changed"
                    if time.monotonic() >= deadline:
                        break
                    self.pause(deadline)
                    continue
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
                        eng = item.get("engine")
                        self.engine = eng.strip() if isinstance(eng, str) and eng.strip() else None
                        try:
                            self.write_pending_state()
                        except OSError:
                            raise StageFailure("running", "pending_state_write_failed")
                        return
                else:
                    reason = f"state_{clean(state)}" + (f":{clean(att['reason'])}" if att.get("reason") else "")
            self.pause(deadline)
        raise StageFailure(stage, ("timeout_" if stage == "running" else "") + reason)

    def output_has_marker(self, out, engine=None):
        if not isinstance(out, str):
            return False
        if self.mode == "shell":
            for line in out.splitlines():
                try:
                    json.loads(line)
                except ValueError:
                    if line.strip(" \t│┃>●•") == self.marker:
                        return True
            return False
        if self.prompt and self.marker in self.prompt:
            return False
        eng = engine if engine is not None else self.engine
        if eng not in ("codex", "zai", "go", "claude", "antigravity"):
            return False
        if eng in ("claude", "antigravity"):
            return paired_stream_output_ok(out, self.marker, eng)

        def marker_in(text):
            return isinstance(text, str) and any(line.strip() == self.marker for line in text.splitlines())

        for line in out.splitlines():
            try:
                event = json.loads(line, object_pairs_hook=unique_object)
            except (ValueError, TypeError):
                continue
            if not isinstance(event, dict):
                continue
            # Only completed tool output for the running session's engine; never
            # assistant prose, result summaries, arguments, prompts or reasoning.
            item = event.get("item")
            if (
                eng in ("codex", "zai")
                and event.get("type") == "item.completed"
                and isinstance(item, dict)
                and item.get("type") == "command_execution"
                and item.get("status") in (None, "completed")
                and (item.get("exit_code") is None or type(item.get("exit_code")) is int and item["exit_code"] == 0)
                and marker_in(item.get("aggregated_output"))
            ):
                return True
            part = event.get("part")
            if (
                eng == "go"
                and event.get("type") == "tool_use"
                and isinstance(part, dict)
                and part.get("type") == "tool"
                and isinstance(part.get("tool"), str)
                and bool(part.get("tool").strip())
                and isinstance(part.get("state"), dict)
                and part["state"].get("status") == "completed"
                and marker_in(part["state"].get("output"))
            ):
                return True
        return False

    def check_capture(self):
        self.current_stage = "capture"
        deadline = self.stage_deadline(self.capture_timeout)
        reason = "no_output"
        attempt = 0
        while time.monotonic() < deadline:
            attempt += 1
            key = f"canary-capture-{self.run_id}-{attempt}"
            rc, raw, _ = self.run([*self.hub, "sessions", "capture", self.session, "--key", key,
                                   "--max-bytes", "65536"], deadline=deadline)
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

    @staticmethod
    def parse_timestamp_ms(val):
        if isinstance(val, int) and not isinstance(val, bool) and val > 0:
            return val
        if isinstance(val, str) and val.strip():
            raw = val.strip()
            if raw.endswith("Z"):
                raw = raw[:-1] + "+00:00"
            try:
                dt = datetime.datetime.fromisoformat(raw)
            except ValueError:
                return None
            if dt.tzinfo is None:
                return None
            return int(dt.timestamp() * 1000)
        return None

    def verify_terminal_session_retired(
        self,
        session_id,
        deadline,
        *,
        expected_native_id,
        expected_key,
        expected_repo,
        expected_machine,
        expected_workspace,
        require_exit_zero=False,
    ):
        if not self.valid_session_id(session_id):
            return False, None
        rc, out, _ = self.run([*self.hub, "sessions", "show", session_id, "--json"], deadline=deadline)
        if rc != 0:
            return False, None
        try:
            payload = json.loads(out)
        except ValueError:
            return False, None
        node = payload.get("session", payload) if isinstance(payload, dict) else None
        if not isinstance(node, dict) or node.get("id") != session_id:
            return False, None
        if not self.row_matches_scope(node, expected_repo, expected_machine, expected_workspace):
            return False, None
        if self.row_conflicts_scope(node, expected_repo, expected_machine, expected_workspace):
            return False, None
        req_ws = node.get("requested_workspace")
        if req_ws is not None:
            if not isinstance(req_ws, str) or not req_ws:
                return False, None
            try:
                if pathlib.Path(req_ws).resolve() != pathlib.Path(expected_workspace).resolve():
                    return False, None
            except OSError:
                return False, None
        row_key, key_conflict = self.row_authoritative_key(node)
        if key_conflict or (expected_key and row_key is not None and row_key != expected_key):
            return False, None
        if expected_native_id is not None and not self.valid_native_id(expected_native_id):
            return False, None
        session_native = node.get("native_id")
        if not self.valid_native_id(session_native):
            return False, None
        if expected_native_id is not None and session_native != expected_native_id:
            return False, None
        intake_id = node.get("intake_id")
        if not isinstance(intake_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", intake_id):
            return False, None
        created_at_ms = self.parse_timestamp_ms(node.get("created_at"))
        if created_at_ms is None:
            return False, None
        state = node.get("state")
        if state == "stopped" and node.get("stop_confirmed") is True and not require_exit_zero:
            updated_at = node.get("updated_at")
            if updated_at is not None:
                updated_at_ms = self.parse_timestamp_ms(updated_at)
                if updated_at_ms is None or updated_at_ms < created_at_ms:
                    return False, None
            return True, session_native
        if (state not in ("completed-awaiting-review", "completed", "failed", "stopped")
                or require_exit_zero and state == "failed"):
            return False, None
        ev = node.get("evidence")
        if not isinstance(ev, dict) or ev.get("native_exit_confirmed") is not True:
            return False, None
        if (
            ev.get("session_id") != session_id
            or ev.get("machine_id") != expected_machine
            or ev.get("intake_id") != intake_id
            or ev.get("native_id") != session_native
        ):
            return False, None
        epoch = node.get("epoch")
        if not isinstance(epoch, int) or isinstance(epoch, bool) or ev.get("epoch") != epoch:
            return False, None
        sess_op = node.get("operation_id")
        ev_op = ev.get("operation_id")
        ev_attempt_op = ev.get("attempt_operation_id")
        if not all(isinstance(x, str) and x for x in (sess_op, ev_op, ev_attempt_op)):
            return False, None
        if ev_attempt_op != sess_op or not (ev_attempt_op == ev_op or ev_attempt_op.startswith(f"{ev_op}:")):
            return False, None
        digest = ev.get("evidence_digest")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            return False, None
        exit_code = ev.get("exit_code")
        if (not isinstance(exit_code, int) or isinstance(exit_code, bool)
                or require_exit_zero and exit_code != 0):
            return False, None
        inner = ev.get("evidence")
        if (
            not isinstance(inner, dict)
            or inner.get("containment_empty") is not True
            or inner.get("source") != "aplexer_retirement"
        ):
            return False, None
        nid_obj = inner.get("native_identity")
        if (
            not isinstance(nid_obj, dict)
            or nid_obj.get("id") != session_native
            or nid_obj.get("tag") != f"task-hub2-{session_id}"
        ):
            return False, None
        nid_ws = nid_obj.get("workspace")
        if not isinstance(nid_ws, str) or not nid_ws:
            return False, None
        try:
            if pathlib.Path(nid_ws).resolve() != pathlib.Path(expected_workspace).resolve():
                return False, None
        except OSError:
            return False, None
        nexit = inner.get("native_exit")
        if not isinstance(nexit, dict):
            return False, None
        ncode = nexit.get("code")
        exited_at_ms = nexit.get("exited_at_ms")
        if not isinstance(ncode, int) or isinstance(ncode, bool) or ncode != exit_code:
            return False, None
        if not isinstance(exited_at_ms, int) or isinstance(exited_at_ms, bool) or exited_at_ms < created_at_ms:
            return False, None
        return True, session_native

    def stop_session_with_output(self, session_id, key, deadline):
        return self.run(
            [
                *self.hub, "sessions", "stop", session_id, "--key", key,
                "--wait", "--timeout", str(max(1, int(self.stop_timeout))), "--interval", "1",
            ],
            deadline=deadline,
        )

    def stop_session(self, session_id, key, deadline):
        rc, _, _ = self.stop_session_with_output(session_id, key, deadline)
        return rc

    def stop_or_confirm_retired(
        self,
        session_id,
        stop_key,
        deadline,
        *,
        expected_native_id,
        expected_key,
        expected_repo,
        expected_machine,
        expected_workspace,
        require_exit_zero=False,
        precomputed_stop=None,
    ):
        rc, _, err = (self.stop_session_with_output(session_id, stop_key, deadline)
                      if precomputed_stop is None else precomputed_stop)
        if rc == 0:
            if expected_native_id is not None and not self.valid_native_id(expected_native_id):
                return False, None
            return True, expected_native_id
        if self.failure_reason(rc, err) == "session_not_running":
            verified, resolved_native = self.verify_terminal_session_retired(
                session_id,
                deadline,
                expected_native_id=expected_native_id,
                expected_key=expected_key,
                expected_repo=expected_repo,
                expected_machine=expected_machine,
                expected_workspace=expected_workspace,
                require_exit_zero=require_exit_zero,
            )
            if not verified or not self.valid_native_id(resolved_native):
                return False, None
            return True, resolved_native
        return False, expected_native_id

    def stop(self, deadline, suffix="stop"):
        self.current_stage = "stop"
        key = f"{self.key}-{suffix}"
        stopped = self.stop_session_with_output(self.session, key, deadline)
        if stopped[0] == 0:
            return 0  # Keep ordinary confirmed STOP behavior unchanged.
        confirmed, _native = self.stop_or_confirm_retired(
            self.session, key, deadline, expected_native_id=self.native,
            expected_key=self.key, expected_repo=self.repo, expected_machine=self.machine,
            expected_workspace=self.workspace, require_exit_zero=True, precomputed_stop=stopped,
        )
        return 0 if confirmed else stopped[0]

    def wait_gone_for(self, native_id, deadline):
        while time.monotonic() < deadline:
            try:
                gone = native_id not in self.a_running_ids(deadline)
            except StageFailure:
                if time.monotonic() >= deadline:
                    break
                raise
            if gone and time.monotonic() < deadline:
                return
            self.pause(deadline)
        raise StageFailure("gone", "still_listed_in_a_running")

    def wait_gone(self, deadline):
        self.current_stage = "gone"
        self.wait_gone_for(self.native, deadline)

    def archive_intake_for(
        self,
        session_id,
        deadline,
        *,
        candidate_sha=None,
        mode=None,
        expected_repo=None,
        expected_machine=None,
        expected_workspace=None,
    ):
        if not session_id:
            return False
        rc, out, _ = self.run([*self.hub, "sessions", "show", session_id, "--json"], deadline=deadline)
        if rc != 0:
            return False
        try:
            payload = json.loads(out)
        except ValueError:
            return False
        node = payload.get("session", payload) if isinstance(payload, dict) else None
        if not isinstance(node, dict) or node.get("id") != session_id:
            return False
        eff_repo = self.repo if expected_repo is None else expected_repo
        eff_machine = self.machine if expected_machine is None else expected_machine
        eff_workspace = self.workspace if expected_workspace is None else expected_workspace
        if self.row_conflicts_scope(node, eff_repo, eff_machine, eff_workspace):
            return False
        intake_id = node.get("intake_id")
        if not isinstance(intake_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", intake_id):
            return False
        eff_candidate = self.candidate_sha if candidate_sha is None else candidate_sha
        eff_mode = self.mode if mode is None else mode
        if eff_candidate:
            archive_args = [
                *self.hub, "intake", "update", intake_id,
                "--status", "archived", "--reason", "deploy_gate_shell_canary",
            ]
        else:
            archive_args = [
                *self.hub, "intake", "update", intake_id,
                "--status", "archived", "--note", f"periodic_{clean(eff_mode, 16)}_canary",
            ]
        rc, _, _ = self.run(archive_args, deadline=deadline)
        return rc == 0

    def archive_intake(self, deadline=None):
        limit_deadline = deadline if deadline is not None else (time.monotonic() + min(self.cmd_timeout, 30))
        return self.archive_intake_for(
            self.session,
            limit_deadline,
            candidate_sha=self.candidate_sha,
            mode=self.mode,
            expected_repo=self.repo,
            expected_machine=self.machine,
            expected_workspace=self.workspace,
        )

    def cleanup(self, deadline=None):
        limit_deadline = deadline if deadline is not None else (time.monotonic() + self.stop_timeout)
        if not self.session and self.start_attempted:
            # A lost or interrupted response is reconciled first with the identical
            # idempotent key and payload, then by unambiguous client_key/key + scope.
            _, out, _ = self.run(self.start_args(), deadline=limit_deadline)
            self.remember_receipt(out)
            if not self.session:
                try:
                    items = self.list_items("cleanup", limit_deadline)
                except StageFailure:
                    items = None
                if items is not None:
                    resolved_sid, resolved_native, conflict = self.resolve_owned_session(
                        items,
                        expected_sid=None,
                        expected_key=self.key,
                        expected_repo=self.repo,
                        expected_machine=self.machine,
                        expected_workspace=self.workspace,
                    )
                    if conflict or not resolved_sid:
                        return "FAILED_needs_reconciliation"
                    self.session = resolved_sid
                    self.native = self.native or resolved_native
                    try:
                        self.write_pending_state()
                    except OSError:
                        pass
        if not self.session:
            return "FAILED_needs_reconciliation" if self.start_attempted else "not_needed"
        confirmed, resolved_native = self.stop_or_confirm_retired(
            self.session,
            f"{self.key}-cleanup",
            limit_deadline,
            expected_native_id=self.native,
            expected_key=self.key,
            expected_repo=self.repo,
            expected_machine=self.machine,
            expected_workspace=self.workspace,
        )
        if not confirmed:
            return "FAILED_needs_reconciliation"
        self.native = self.native or resolved_native
        if self.native is not None and not self.valid_native_id(self.native):
            return "FAILED_needs_reconciliation"
        if self.native:
            try:
                self.wait_gone(limit_deadline)
            except StageFailure:
                return "FAILED_needs_reconciliation"
        return "stopped"

    def execute(self):
        result = {"ok": False, "mode": self.mode, "run_id": self.run_id, "timestamp": now_iso(),
                  "stage": None, "reason": None, "session_id": None, "cleanup": "not_needed"}
        prev_handlers = {}

        def _handle_signal(signum, _frame):
            sig_name = signal.Signals(signum).name
            self.termination_reason = self.termination_reason or f"terminated_{sig_name.lower()}"
            if not self.in_cleanup:
                raise CanaryTerminated(signum, sig_name)

        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            try:
                prev_handlers[sig] = signal.getsignal(sig)
                signal.signal(sig, _handle_signal)
            except (OSError, ValueError):
                pass

        try:
            self.active_deadline = time.monotonic() + self.active_budget
            self.verify_helper_digest()
            self.acquire_deploy_gate_lock()
            self.verify_candidate_host_release()
            self.reconcile_prior_pending()
            self.start()
            self.wait_running()
            self.check_capture()
            deadline = self.stage_deadline(self.stop_timeout)
            rc = self.stop(deadline)
            if rc != 0:
                raise StageFailure("stop", "hub_cli_timeout" if rc == 124 else f"hub_cli_exit_{rc}")
            self.wait_gone(deadline)
            self.stopped = True
            result.update(ok=True)
        except StageFailure as failure:
            result.update(stage=failure.stage, reason=failure.reason)
        except CanaryTerminated as terminated:
            stage = self.current_stage if self.current_stage in STAGES else ("start" if not self.session else "stop")
            result.update(stage=stage, reason=f"terminated_{terminated.sig_name.lower()}")
        except Exception as error:
            result.update(stage="start" if not self.session else "stop", reason="internal_" + type(error).__name__)
        finally:
            self.in_cleanup = True
            cleanup_deadline = time.monotonic() + self.cleanup_budget
            if self.start_attempted and not self.stopped:
                try:
                    result["cleanup"] = self.cleanup(deadline=cleanup_deadline)
                except BaseException:
                    result["cleanup"] = "FAILED_needs_reconciliation"
            if self.durable_pending_enabled and self.session and (
                self.stopped or result["cleanup"] == "stopped"
            ):
                try:
                    self.intake_archived = self.archive_intake(deadline=cleanup_deadline)
                except BaseException:
                    self.intake_archived = False
                if not self.intake_archived:
                    if not self.stopped:
                        result["cleanup"] = "FAILED_needs_reconciliation"
                    if result["ok"]:
                        result.update(ok=False, stage="cleanup", reason="intake_archive_failed")
            if self.durable_pending_enabled and (self.stopped or result["cleanup"] == "stopped") and (
                self.intake_archived is True
            ):
                try:
                    self.clear_pending_state()
                except OSError:
                    result["cleanup"] = "FAILED_needs_reconciliation"
                    if result["ok"]:
                        result.update(ok=False, stage="cleanup", reason="pending_state_clear_failed")
            if self.pyz_fd is not None:
                try:
                    os.close(self.pyz_fd)
                except OSError:
                    pass
                self.pyz_fd = None
                self.hub_pass_fds = ()
            if self.lock_fd is not None:
                try:
                    os.close(self.lock_fd)
                except OSError:
                    pass
                self.lock_fd = None
            for sig, handler in prev_handlers.items():
                try:
                    signal.signal(sig, handler)
                except (OSError, ValueError):
                    pass
        result["session_id"] = self.session
        if self.helper_sha256 is not None:
            result["helper_sha256"] = self.helper_sha256
        if self.intake_archived is not None:
            result["intake_archived"] = bool(self.intake_archived)
        if self.candidate_sha is not None:
            result["candidate_sha"] = self.candidate_sha
            result["run_token"] = self.run_token
            result["host_version"] = self.host_version
            result["hub_pyz_sha256"] = self.hub_pyz_sha256
            result["intake_archived"] = bool(self.intake_archived)
            result["host_updater_trigger"] = self.host_updater_trigger
            if self.host_updater_trigger_error is not None:
                result["host_updater_trigger_error"] = self.host_updater_trigger_error
        return result


# ---- alert state and mail -------------------------------------------------

def state_dir():
    return ensure_private_dir(raw_state_dir_path(), stage="lock", prefix="state_dir")


def load_state(path):
    try:
        return json.loads(read_regular_nofollow(path, max_bytes=65536).decode("utf-8"))
    except (OSError, ValueError):
        return {"failures": 0, "successes": 0, "incident_open": False, "incident_id": "-"}


def save_state(path, state):
    atomic_write_private_json(path, state)


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


def record_dev_alert_occurrence(sdir, mode, result, state, *, decision):
    """Reserved integration hook for the finite Hub dev@ occurrence counter
    (dev_alert_occurrences_v1; invoked before dedup/SMTP on alert decisions:
    critical/critical_dedup/recovery increment cumulative occurrences, while
    pass/recovering/failure_below_threshold update observation coverage only).
    """
    _ = (sdir, mode, result, state, decision)


def apply_alerting(result):
    sdir = state_dir()
    lock_fd = acquire_lock_fd(sdir / f"{result['mode']}.lock", 30.0, stage="lock", busy_reason="alert_state_lock_busy")
    try:
        path = sdir / f"state-{result['mode']}.json"
        st = load_state(path)
        mode = result["mode"]
        if not result["ok"] and result.get("stage") == "lock" and result.get("reason") == "deploy_gate_lock_busy":
            st["last_lock_busy"] = {k: result[k] for k in ("ok", "stage", "reason", "timestamp", "run_id")}
            save_state(path, st)
            return
        if result["ok"]:
            st["failures"] = 0
            if st.get("incident_open"):
                st["successes"] = st.get("successes", 0) + 1
                if st["successes"] >= RECOVER_THRESHOLD:
                    record_dev_alert_occurrence(sdir, mode, result, st, decision="recovery")
                    body = (f"Hub start canary ({mode}) recovered at {result['timestamp']}.\n"
                            f"Run id: {result['run_id']}\nIncident: {st.get('incident_id')}\n")
                    if send_mail(f"[RECOVERED] Hub start canary ({mode}) is passing again", body):
                        st.update(incident_open=False, successes=0, incident_id="-")
                else:
                    record_dev_alert_occurrence(sdir, mode, result, st, decision="recovering")
            else:
                st["successes"] = 0
                record_dev_alert_occurrence(sdir, mode, result, st, decision="pass")
        else:
            st["successes"] = 0
            st["failures"] = st.get("failures", 0) + 1
            if st["failures"] >= FAIL_THRESHOLD:
                record_dev_alert_occurrence(
                    sdir,
                    mode,
                    result,
                    st,
                    decision="critical_dedup" if st.get("incident_open") else "critical",
                )
                if not st.get("incident_open"):
                    incident = st.get("incident_id") if st.get("incident_id", "-") != "-" else result["run_id"]
                    body = (f"CRITICAL: Hub cannot start sessions (canary mode {mode}).\n"
                            f"Failing stage: {result['stage']}\nReason: {result['reason']}\n"
                            f"Timestamp: {result['timestamp']}\nRun id: {result['run_id']}\n"
                            f"Consecutive failures: {st['failures']}\nIncident: {incident}\n"
                            f"Session cleanup: {result['cleanup']}\n")
                    if send_mail(f"[CRITICAL] Hub start canary ({mode}) failing at stage {result['stage']}: {result['reason']}", body):
                        st.update(incident_open=True, incident_id=incident)
            else:
                record_dev_alert_occurrence(sdir, mode, result, st, decision="failure_below_threshold")
        st["last"] = {k: result[k] for k in ("ok", "stage", "reason", "timestamp", "run_id")}
        save_state(path, st)
    finally:
        os.close(lock_fd)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--mode", choices=("shell", "full"), default=None)
    p.add_argument("--deploy-gate", action="store_true", help="run the canary once (default mode full, or --mode shell), no mail, JSON verdict")
    p.add_argument("--candidate-sha", default=None, help="40-hex candidate commit SHA for pre-commit deploy gate")
    p.add_argument("--candidate-cli-sha256", default=None, help="64-hex expected SHA-256 of the candidate hub.pyz")
    p.add_argument("--run-token", default=None, help="32..64-hex run token binding the pre-commit deploy gate verdict")
    p.add_argument("--helper-sha256", default=None, help="64-hex expected SHA-256 of the installed canary helper bundle")
    p.add_argument("--print-helper-sha256", action="store_true", help="print canonical helper bundle SHA-256 and exit")
    args = p.parse_args()
    if args.print_helper_sha256:
        print(compute_helper_sha256())
        return 0
    bound_args = (args.candidate_sha, args.candidate_cli_sha256, args.run_token, args.helper_sha256)
    if any(v is not None for v in bound_args):
        if not args.deploy_gate or not all(v is not None for v in bound_args):
            p.error("--candidate-sha, --candidate-cli-sha256, --run-token, and --helper-sha256 must be provided together with --deploy-gate")
        if not re.fullmatch(r"[0-9a-f]{40}", args.candidate_sha):
            p.error("--candidate-sha must be a 40-character lowercase hex SHA")
        if not re.fullmatch(r"[0-9a-f]{64}", args.candidate_cli_sha256):
            p.error("--candidate-cli-sha256 must be a 64-character lowercase hex SHA-256")
        if not re.fullmatch(r"[0-9a-f]{32,64}", args.run_token):
            p.error("--run-token must be a 32..64-character lowercase hex token")
        if not re.fullmatch(r"[0-9a-f]{64}", args.helper_sha256):
            p.error("--helper-sha256 must be a 64-character lowercase hex SHA-256")
    mode = args.mode or "full"
    run_id = f"{args.candidate_sha[:12]}-{args.run_token[:12]}" if args.candidate_sha else uuid.uuid4().hex[:12]
    result = Canary(
        mode,
        run_id,
        deploy_gate=args.deploy_gate,
        candidate_sha=args.candidate_sha,
        candidate_cli_sha256=args.candidate_cli_sha256,
        run_token=args.run_token,
        expected_helper_sha256=args.helper_sha256,
    ).execute()
    if args.deploy_gate:
        result["deploy_gate"] = True
    else:
        try:
            apply_alerting(result)
        except StageFailure as failure:
            result["alert_state_error"] = f"{failure.stage}:{failure.reason}"
            if result["ok"]:
                result.update(ok=False, stage=failure.stage, reason=failure.reason)
        except OSError:
            result["alert_state_error"] = "lock:alert_state_write_failed"
            if result["ok"]:
                result.update(ok=False, stage="lock", reason="alert_state_write_failed")
    line = json.dumps(result, sort_keys=True)
    print(line, flush=True)
    if not result["ok"]:
        print(line, file=sys.stderr, flush=True)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
