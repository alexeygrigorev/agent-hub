#!/usr/bin/env python3
"""Live start canary for Hub: start, observe, capture, stop, confirm gone.

Drives the public CLIs only (`hub sessions start|list|capture|stop`,
`a list --json --running`). See docs/HUB-START-CANARY.md.
"""
import argparse, datetime, errno, fcntl, hashlib, ipaddress, json, os, pathlib, re, shlex, signal, stat, subprocess, sys, time, urllib.parse, uuid

ALERT_TO = "dev@dtcdev.click"  # fixed on purpose: never configurable, never orin@
ALERT_FROM = "alerts@dtcdev.click"
MSMTP_ACCOUNT = "alerts"
MARKER = "CANARY-OK"
PROMPTS = {"full": "Reply CANARY-OK and exit", "shell": "printf CANARY-OK; sleep 30"}
STAGES = ("helper_version", "lock", "host_release", "health", "admission", "start", "running", "list_host_workspace", "capture", "stop", "gone", "cleanup")
FAIL_THRESHOLD = 2
RECOVER_THRESHOLD = 2
HELPER_FILES = ("hub-deploy-gate-forced.sh", "hub-start-canary.sh", "hub_start_canary.py")

# Fixed trusted ceilings for pre-commit deploy-gate mode (240s active + 60s cleanup < 360s outer wrapper).
TRUSTED_CONVERGE_TIMEOUT_S = 90.0
TRUSTED_RUNNING_TIMEOUT_S = 90.0
TRUSTED_CAPTURE_TIMEOUT_S = 30.0
TRUSTED_FULL_CAPTURE_TIMEOUT_S = 120.0
TRUSTED_STOP_TIMEOUT_S = 45.0
TRUSTED_CMD_TIMEOUT_S = 45.0
TRUSTED_ACTIVE_BUDGET_S = 240.0
TRUSTED_CLEANUP_BUDGET_S = 60.0
TRUSTED_LOCK_WAIT_S = 60.0
PERIODIC_DEFAULT_LOCK_WAIT_S = 120.0
# Public, unauthenticated Hub health body checked before each periodic start.
# HUB_CANARY_HEALTH_URL exists for offline tests: https anywhere, plain http
# only on a loopback literal; path must be /health; no userinfo/query/fragment.
DEFAULT_HEALTH_URL = "https://hub.dtcdev.click/health"
HEALTH_TIMEOUT_S = 10.0
HEALTH_MAX_BYTES = 4096
HEALTH_REASONS = frozenset((
    "health_status_bootstrap", "health_not_ok", "health_unparseable_json", "health_body_too_large",
    "health_http_status", "health_timeout", "health_unreachable",
))
# Runs as `python3 -I -S -c HEALTH_CHILD scheme host port budget_s max_bytes`; prints one fixed token.
HEALTH_CHILD = r"""
import http.client, json, ssl, sys, time
scheme, host, port, budget, max_bytes = sys.argv[1], sys.argv[2], sys.argv[3], float(sys.argv[4]), int(sys.argv[5])
def unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate")
        value[key] = item
    return value
def verdict():
    port_num = int(port) if port else None
    if scheme == "https":
        conn = http.client.HTTPSConnection(host, port_num, timeout=budget, context=ssl.create_default_context())
    else:
        conn = http.client.HTTPConnection(host, port_num, timeout=budget)
    try:
        conn.request("GET", "/health", headers={"Accept": "application/json", "User-Agent": "hub-start-canary"})
        resp = conn.getresponse()
        if resp.status != 200:
            return "health_http_status"
        body = b""
        while len(body) <= max_bytes:
            chunk = resp.read(max_bytes + 1 - len(body))
            if not chunk:
                break
            body += chunk
        if len(body) > max_bytes:
            return "health_body_too_large"
    finally:
        conn.close()
    try:
        data = json.loads(body.decode("utf-8"), object_pairs_hook=unique)
    except (UnicodeDecodeError, ValueError, RecursionError):
        return "health_unparseable_json"
    if not isinstance(data, dict):
        return "health_unparseable_json"
    if data.get("ok") is True:
        return "ok"
    return "health_status_bootstrap" if data.get("status") == "bootstrap" else "health_not_ok"
try:
    token = verdict()
except TimeoutError:
    token = "health_timeout"
except Exception:
    token = "health_unreachable"
sys.stdout.write(token + "\n")
"""
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


# Claude Code 2.1.322 native content for a Bash call with empty stdout/stderr.
CLAUDE_EMPTY_BASH_PLACEHOLDER = "(Bash completed with no output)"


def zai_native_return_proof(captured, expected, binding=None):
    """One owned capture, exact native returned text; never reconstruct stdout.

    The producer has paired the scheduled Bash input with the native return.
    Provider thread/session IDs are not the aplexer UUID: learn their tuple
    from this owned stream and refuse drift. No proof frames cross captures.
    """
    answer = {"seen": False, "proven": False, "invalidated": False, "binding": binding}

    def identity(value):
        return (isinstance(value, str) and bool(value) and len(value.encode("utf-8")) <= 256
                and value == value.strip() and not any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in value))

    def optional_bool(value):
        return value is None or type(value) is bool

    def optional_uint(value):
        return value is None or type(value) is int and 0 <= value <= 2**64 - 1

    try:
        if not isinstance(captured, str) or len(captured.encode("utf-8")) > 65536:
            answer.update(seen=True, invalidated=True)
            return answer
        events, malformed, native_parse_loss = [], False, False
        for line in captured.split("\n"):
            if not line.strip():
                continue
            try:
                event = json.loads(line, object_pairs_hook=unique_object)
            except (ValueError, TypeError):
                # An undecodable retained line may hide a native invalidation.
                # Initial clipping is UNKNOWN, never an ignorable/repaired tail.
                # Complete assistant strings remain opaque to the strict decoder.
                native_parse_loss = True
                malformed = True
                continue
            if not isinstance(event, dict):
                malformed = True
                continue
            events.append(event)
        native = [event for event in events if isinstance(event.get("item"), dict)
                  and event["item"].get("type") == "native_tool_result"]
        # Report parse loss even when this poll contains no complete native
        # item: an already-established native attempt must not heal later.
        answer["seen"] = bool(native) or native_parse_loss
        if malformed or len(events) > 128:
            answer["invalidated"] = True
            return answer
        if not native:
            return answer
        if binding is not None and (type(binding) is not tuple or len(binding) != 3
                                    or not all(identity(value) for value in binding)):
            answer["invalidated"] = True
            return answer
        item_ids, call_ids = set(), set()
        headers = [event.get("thread_id") for event in events if event.get("type") == "thread.started"]
        fields = {"kind", "native_event", "native_session_id", "native_turn_id", "call_id", "tool",
                  "command", "content", "native_success", "exit_code", "status", "timed_out",
                  "native_truncated", "native_original_bytes", "native_returned_bytes",
                  "local_truncated", "observation_complete"}
        for event in native:
            item = event["item"]
            if (set(event) != {"type", "item"} or event["type"] != "item.completed"
                    or set(item) != {"id", "type", "thread_id", "turn_id", "observation"}
                    or not all(identity(item.get(k)) for k in ("id", "thread_id", "turn_id"))
                    or item["id"] in item_ids):
                answer["invalidated"] = True
                return answer
            item_ids.add(item["id"])
            observation = item["observation"]
            if not isinstance(observation, dict):
                answer["invalidated"] = True
                return answer
            if observation.get("kind") == "invalidated":
                answer["invalidated"] = True
                continue
            if (set(observation) != fields or observation.get("kind") != "result"
                    or observation["native_event"] not in ("tool_call_result", "tool_call_error")
                    or not identity(observation["native_session_id"])
                    or not identity(observation["call_id"])
                    or observation["native_turn_id"] is not None and not identity(observation["native_turn_id"])
                    or observation["call_id"] in call_ids
                    or observation["tool"] != "Bash"
                    or not isinstance(observation["command"], str) or not observation["command"]
                    or len(observation["command"].encode("utf-8")) > 8192 or "\0" in observation["command"]
                    or not isinstance(observation["content"], str)
                    or len(observation["content"].encode("utf-8")) > 65536
                    or not all(optional_bool(observation[k]) for k in ("native_success", "timed_out", "native_truncated"))
                    or type(observation["local_truncated"]) is not bool
                    or type(observation["observation_complete"]) is not bool
                    or not all(optional_uint(observation[k]) for k in ("native_original_bytes", "native_returned_bytes"))
                    or observation["exit_code"] is not None and not (type(observation["exit_code"]) is int
                                                                 and -(2**31) <= observation["exit_code"] < 2**31)
                    or observation["status"] not in (None, "completed", "failed", "timed_out", "cancelled", "running", "backgrounded")):
                answer["invalidated"] = True
                return answer
            call_ids.add(observation["call_id"])
            current = (item["thread_id"], item["turn_id"], observation["native_session_id"])
            if answer["binding"] is None:
                answer["binding"] = current
            if current != answer["binding"] or any(header != current[0] for header in headers):
                answer["invalidated"] = True
                return answer
            if observation["local_truncated"]:
                answer["invalidated"] = True
            complete = (observation["observation_complete"] is True
                        and identity(observation["native_turn_id"])
                        and observation["native_truncated"] is False
                        and observation["local_truncated"] is False
                        and type(observation["native_original_bytes"]) is int
                        and type(observation["native_returned_bytes"]) is int
                        and observation["native_returned_bytes"] == len(observation["content"].encode("utf-8"))
                        and observation["native_original_bytes"] <= observation["native_returned_bytes"])
            if observation["observation_complete"] is True and not complete:
                answer["invalidated"] = True
            if (complete and observation["native_event"] == "tool_call_result"
                    and observation["native_success"] is True
                    and type(observation["exit_code"]) is int and observation["exit_code"] == 0
                    and observation["status"] == "completed" and observation["timed_out"] is False
                    and observation["content"] == expected):
                answer["proven"] = True
        answer["proven"] &= not answer["invalidated"]
        return answer
    except (ValueError, TypeError, UnicodeError, RecursionError):
        answer.update(seen=True, proven=False, invalidated=True)
        return answer

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
                    if detail.get("backgroundTaskId") is not None:
                        return False
                    if block["content"] == CLAUDE_EMPTY_BASH_PLACEHOLDER and detail.get("stdout") == "":
                        # NONPROOF: the exact native empty-result pair is classified only.
                        if detail.get("isImage") is not False or detail.get("noOutputExpected") is not False:
                            return False
                        continue
                    if detail.get("stdout") != block["content"]:
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
                or tool not in ("run_command", "read_file", "manage_task", "schedule", "command_status", "wait", "wait_5_seconds")
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
            elif tool == "read_file":
                # NONPROOF class: classified only; its output never sets proven.
                if (
                    not set(params) <= {"Path", "StartLine", "EndLine"}
                    or not isinstance(params.get("Path"), str)
                    or not params["Path"]
                    or "\0" in params["Path"]
                    or not all(
                        type(params[k]) is int and 0 <= params[k] <= 2**31 - 1
                        for k in ("StartLine", "EndLine")
                        if k in params
                    )
                    or ("StartLine" in params and "EndLine" in params and params["EndLine"] < params["StartLine"])
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


# ---- fixed workspace authority and finite pre-start admission --------------

# Root-owned, non-secret, fixed path. Deliberately not overridable through the
# environment or an argument: callers (including the forced wrapper, which runs
# under env -i) cannot select a different production config.
CANARY_WORKSPACE_CONFIG = "/etc/agent-hub/canary-workspace.json"
WORKSPACE_CONFIG_MAX_BYTES = 4096
WORKSPACE_CONFIG_UID = 0
WORKSPACE_CONFIG_TRUST_ROOT = "/"
ADMISSION_BUDGET_S = 60.0
ADMISSION_RAM_FLOOR_BYTES = 10 * 1024 ** 3
# Policy CEILINGS for the new worker. The caps that count are the ones the
# selected Host launcher will actually apply (read_launcher_caps); anything above
# these ceilings, or unknowable, holds admission.
NEW_WORKER_MEMORY_BYTES = 1500 * 1024 ** 2
NEW_WORKER_PIDS = 256
# Host launcher defaults when the selected config omits them: hub2/host/agent_hub_host/
# native_launch.py reads config["launcher"].get("memory_mb", 1500) / .get("pids", 256)
# and passes them as `--memory <N>M --pids <N>` to the native `a start`.
LAUNCHER_DEFAULT_MEMORY_MB = 1500
LAUNCHER_DEFAULT_PIDS = 256
LAUNCHER_CONFIG_RELATIVE = ".config/agent-hub/v2.json"  # hub2 CLI --config default; the Host service passes no --config
LAUNCHER_CONFIG_MAX_BYTES = 256 * 1024
HOST_UNIT_CGROUP_SUFFIX = "/agent-hub2-host.service"  # deploy/agent-hub2-host.service
# Disk, all on the actual cwd/TMP devices after the validated floor:
#  - SPIKE: one transient write margin shared by the start itself.
#  - WORKER_RESERVE: a planning estimate per worker that may still write on the
#    same device, charged for the NEW worker (this task's own known growth) and for
#    every live current worker or uncovered in-flight task there (mature RAM proof
#    says nothing about disk). The figure and the cap borrow the maintained
#    launcher disk gate constants (host launcher_recovery.py: next_disk_mb=512,
#    active reserve cap 8192 MiB). That gate belongs to the quota launcher; its
#    linkage to NativeLauncher starts is NOT proven here, so this is a bounded
#    conservative estimate, not a measured or guaranteed growth bound.
ADMISSION_DISK_SPIKE_BYTES = 512 * 1024 ** 2
ADMISSION_DISK_WORKER_RESERVE_BYTES = 512 * 1024 ** 2
ADMISSION_DISK_RESERVE_CAP_BYTES = 8192 * 1024 ** 2
DISK_FLOOR_GB_RANGE = (1, 1048576)
MATURE_RESIDENT_AGE_S = 180.0
QUOTA_STALE_S = 1200.0
CODEX_QUOTA_RESERVE_PERCENT = 15.0
# Supported quse routes with their required window groups (any alias satisfies a group) and the
# source-proven optional null plan slots. Data semantics only. Window shapes come from quse
# usage.py `windows()` (5h/7d/monthly canonical slots; null slots are plan windows the provider
# does not have): Claude and Gemini (underlying buckets in gemini_quota.py; a missing 5h/7d
# bucket is an unknown REQUIRED window, not an unsupported provider) and ZAI carry 5h+7d with a
# null monthly; Go has a real monthly window; Codex requires weekly. Provider support follows
# the Hub's actual native routes (hub2 native_launch.py: claude, codex, go/opencode, zai via
# zcodex, antigravity via agy; model-policy.ts: engine zai only on account zai-central, Gemini
# engine antigravity on account gemini), NOT the presence of agent-quota-launcher
# provider_admission.py REQUIRED_WINDOWS, which only supplies Claude/Codex/Go data semantics.
# Aliases: quse key `gemini` (canonical) with probe aliases `agy`/`antigravity`; `codex-orin` is
# the second Codex ACCOUNT reported under its own wire key and keeps the Codex requirements
# and strict >15 reserve. Not eligible: copilot (no native route), Grok (quota-only label in
# worker.ts; no native engine in PROVIDER_CAPABILITIES/LOCAL_ROUTES/CENTRAL_ROUTES, so a
# Grok-only reading must not qualify a native start; data schema 7d+monthly/optional 5h and the
# entitlement/5% controls are intentionally not wired until a Hub route exists) and unknown names.
# This is data validation only: the launcher's executor lifecycle and its independent Codex
# execution prohibition are not imported; the protected Codex wrapper stays authoritative.
_FIVE_H_7D = (("5h", "five_hour"), ("7d", "weekly", "seven_day"))
_CODEX_POLICY = {"required": (("7d", "weekly"),), "optional": frozenset(("5h", "monthly"))}
_GEMINI_POLICY = {"required": _FIVE_H_7D, "optional": frozenset(("monthly",))}
QUOTA_ROUTE_POLICY = {
    "claude": {"required": _FIVE_H_7D, "optional": frozenset(("monthly",))},
    "go": {"required": (("5h", "rolling"), ("7d", "weekly"), ("monthly",)), "optional": frozenset()},
    "codex": _CODEX_POLICY,
    "codex-orin": _CODEX_POLICY,
    "zai": {"required": _FIVE_H_7D, "optional": frozenset(("monthly",))},
    "gemini": _GEMINI_POLICY,
    "agy": _GEMINI_POLICY,
    "antigravity": _GEMINI_POLICY,
}
CODEX_RESERVE_ROUTES = frozenset(("codex", "codex-orin"))
ADMISSION_JSON_MAX_BYTES = 16 * 1024 * 1024
ADMISSION_MAX_ROWS = 5000
PROC_ROOT = "/proc"
CGROUP_ROOT = "/sys/fs/cgroup"
# Hub session states with no live physical obligation vs. states that reserve capacity.
HUB_TERMINAL_STATES = frozenset((
    "failed", "stopped", "cancelled", "closed", "aborted", "lost", "completed", "completed-awaiting-review", "exited", "archived",
))
HUB_INFLIGHT_STATES = frozenset(("queued", "pending", "dispatching", "starting", "running", "exiting", "stopping", "needs_attention"))
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)

# Fixed bridge executed through the already whole-archive-verified CLI (sealed
# memfd, isolated interpreter). It issues exactly one authenticated GET through
# the existing client with the effective human profile, no worker/env-token
# fallback and no 401 refresh; the parent enforces the request deadline.
ADMISSION_BRIDGE = r"""
import json, os, sys
sys.path.insert(0, sys.argv[1])
from agent_hub_host import auth, bus, config, worker_auth
def _no_refresh(*args, **kwargs):
    raise RuntimeError("refresh_disabled")
auth.refresh_token = _no_refresh
try:
    if worker_auth.is_v2_worker() or any(os.environ.get(k) for k in ("HUB_CONFIG_FILE", "HUB_JOB_TOKEN", "HUB_TOKEN")):
        raise RuntimeError("not_human")
    human = config.load_human_config()
    if not isinstance(human, dict) or not isinstance(human.get("access_token"), str) or not human.get("access_token"):
        raise RuntimeError("not_human")
    data = bus._do_request("GET", "/api/resource-settings")
    settings = data.get("resource_settings") if isinstance(data, dict) else None
    floor = settings.get("disk_floor_gb") if isinstance(settings, dict) else None
    print(json.dumps({"disk_floor_gb": floor}))
except BaseException:
    sys.exit(3)
"""


def _closed_json(raw, limit):
    if not isinstance(raw, str) or len(raw.encode("utf-8", "replace")) > limit:
        raise ValueError("json too large")
    return json.loads(raw, object_pairs_hook=unique_object)


def _finite_number(value):
    return type(value) in (int, float) and value == value and value not in (float("inf"), float("-inf"))


def _nofollow_ancestors(path, trust_root):
    """Ancestor directories of path from trust_root downward (trust_root first)."""
    parts = []
    cur = pathlib.Path(path).parent
    root = pathlib.Path(trust_root)
    while True:
        parts.append(cur)
        if cur == root or cur.parent == cur:
            break
        cur = cur.parent
    if parts[-1] != root:
        raise OSError(errno.EINVAL, "config outside trust root")
    return list(reversed(parts))


def read_workspace_config(path=None):
    """Return the configured DATA workspace, or None when the file is absent.

    Present but unsafe or malformed config raises StageFailure and never falls
    back. The file is non-secret and read by UID 1000, so root:root 0644 is
    the expected mode; only group/world write is refused.
    """
    path = CANARY_WORKSPACE_CONFIG if path is None else path
    def fail(reason):
        raise StageFailure("admission", "workspace_config_" + reason)
    try:
        ancestors = _nofollow_ancestors(path, WORKSPACE_CONFIG_TRUST_ROOT)
    except OSError:
        fail("path")
    absent = False
    for directory in ancestors:
        try:
            st = os.lstat(directory)
        except FileNotFoundError:
            absent = True
            break
        except OSError:
            fail("parent")
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            fail("parent")
        if st.st_uid not in (0, WORKSPACE_CONFIG_UID) or (st.st_mode & 0o022):
            fail("parent")
    if absent:
        return None
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
    except FileNotFoundError:
        return None
    except OSError as error:
        fail("symlink" if error.errno == errno.ELOOP else "unreadable")
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            fail("not_regular")
        if before.st_uid != WORKSPACE_CONFIG_UID:
            fail("owner")
        if before.st_nlink != 1:
            fail("nlink")
        if before.st_mode & 0o022:
            fail("writable")
        if before.st_size > WORKSPACE_CONFIG_MAX_BYTES:
            fail("size")
        raw = os.read(fd, WORKSPACE_CONFIG_MAX_BYTES + 1)
        after = os.fstat(fd)
        if len(raw) > WORKSPACE_CONFIG_MAX_BYTES:
            fail("size")
        if (before.st_ino, before.st_dev, before.st_size, before.st_mtime_ns, before.st_uid, before.st_mode) != (
            after.st_ino, after.st_dev, after.st_size, after.st_mtime_ns, after.st_uid, after.st_mode
        ) or len(raw) != after.st_size:
            fail("unstable")
    except OSError:
        fail("unreadable")
    finally:
        os.close(fd)
    try:
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object)
    except (UnicodeDecodeError, ValueError):
        fail("json")
    if not isinstance(data, dict) or set(data) != {"workspace"}:
        fail("schema")
    workspace = data["workspace"]
    if (not isinstance(workspace, str) or not workspace.startswith("/") or "\0" in workspace
            or len(workspace) > 1024 or os.path.normpath(workspace) != workspace or workspace == "/"):
        fail("schema")
    return workspace


def selected_launcher_config_path():
    """The Host's selected enrollment/launcher config: the hub2 CLI default, no --config."""
    try:
        home = pathlib.Path.home()
    except (RuntimeError, KeyError, OSError):
        raise StageFailure("admission", "launcher_config_path")
    if not home.is_absolute() or "\0" in str(home):
        raise StageFailure("admission", "launcher_config_path")
    return home / LAUNCHER_CONFIG_RELATIVE


def prove_loaded_host_config(config_stat, home):
    """Exactly one Host daemon of this user, from this home, started with no --config, after the config last changed.

    Source alone cannot prove the daemon loaded the file; this narrows it: the
    process in the Host unit's cgroup is `hub2 serve` run from this home's
    agent-hub2 release tree without a --config override, and neither mtime nor
    ctime of the selected config is later than that process's start. It cannot
    see systemd drop-ins or environment, so it is evidence, not an attestation.
    """
    def fail(reason):
        raise StageFailure("admission", "launcher_config_" + reason)
    euid = os.geteuid()
    release_tree = f"{home}/.local/share/agent-hub2/"
    found = []
    try:
        names = [n for n in os.listdir(PROC_ROOT) if n.isdigit()]
    except OSError:
        fail("host_unproven")
    for name in names[:200000]:
        try:
            if os.stat(f"{PROC_ROOT}/{name}").st_uid != euid:
                continue
            if not any(line.startswith("0::") and line.endswith(HOST_UNIT_CGROUP_SUFFIX)
                       for line in read_text_limited(f"{PROC_ROOT}/{name}/cgroup").splitlines()):
                continue
            argv = read_text_limited(f"{PROC_ROOT}/{name}/cmdline").split("\0")
        except (OSError, ValueError):
            continue
        hub2 = [i for i, a in enumerate(argv) if os.path.basename(a) == "hub2"]
        if not hub2 or "serve" not in argv[hub2[0] + 1:]:
            continue
        found.append((int(name), argv, argv[hub2[0]]))
    if len(found) != 1:
        fail("host_unproven")
    pid, argv, program = found[0]
    if not program.startswith(release_tree) or any(a == "--config" or a.startswith("--config=") for a in argv):
        fail("host_unproven")
    def start_ticks():
        fields = read_text_limited(f"{PROC_ROOT}/{pid}/stat")
        return int(fields[fields.rindex(")") + 2:].split()[19])
    try:
        first = start_ticks()
        # Closed selector: only the daemon's own HOME entry is extracted (RAM only, rest dropped).
        with open(f"{PROC_ROOT}/{pid}/environ", "rb") as handle:
            environ = handle.read(1024 * 1024 + 1)
        if len(environ) > 1024 * 1024:
            fail("host_unproven")
        daemon_home = [e[5:] for e in environ.split(b"\0") if e.startswith(b"HOME=")]
        del environ
        if daemon_home != [os.fsencode(home)]:
            fail("host_unproven")
        started = time.time() - (time.clock_gettime(time.CLOCK_BOOTTIME) - first / os.sysconf("SC_CLK_TCK"))
        if start_ticks() != first:
            fail("host_unproven")
    except (OSError, ValueError, IndexError):
        fail("host_unproven")
    # Strictly before the conservative process birth (one clock tick of slack); ambiguous boundaries refuse.
    if max(config_stat.st_mtime_ns, config_stat.st_ctime_ns) / 1e9 >= started - 1.0 / os.sysconf("SC_CLK_TCK"):
        fail("changed_after_host_start")


def read_launcher_caps(path, expected_machine):
    """(memory_bytes, pids) the selected Host launcher will apply to a new worker.

    Reads the selected Host config in RAM only (it also holds the enrollment
    token): bounded, regular, NOFOLLOW, owner-private, machine identity checked,
    and only machine_id and launcher.memory_mb/pids are extracted; nothing else is
    kept, logged or reported. Unreadable, unsafe, mismatched, invalid,
    changed-since-the-Host-started, unattributable or above-ceiling values raise
    StageFailure and never fall back to a default (the documented source defaults
    apply only when the keys are absent).
    """
    def fail(reason):
        raise StageFailure("admission", "launcher_config_" + reason)
    path = pathlib.Path(path)
    euid = os.geteuid()
    chain = list(reversed([path.parent, *path.parent.parents]))
    try:
        for directory in chain[-3:]:  # ~, ~/.config, ~/.config/agent-hub
            st = os.lstat(directory)
            if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode) or st.st_uid != euid:
                fail("parent")
        if os.lstat(path.parent).st_mode & 0o077:
            fail("parent")
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
    except FileNotFoundError:
        fail("absent")
    except OSError as error:
        fail("symlink" if error.errno == errno.ELOOP else "unreadable")
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            fail("not_regular")
        if before.st_uid != euid or before.st_mode & 0o077 or before.st_nlink != 1:
            fail("unsafe")
        if before.st_size > LAUNCHER_CONFIG_MAX_BYTES:
            fail("size")
        raw = os.read(fd, LAUNCHER_CONFIG_MAX_BYTES + 1)
        after = os.fstat(fd)
        if len(raw) > LAUNCHER_CONFIG_MAX_BYTES or len(raw) != after.st_size or (
            before.st_ino, before.st_dev, before.st_size, before.st_mtime_ns,
        ) != (after.st_ino, after.st_dev, after.st_size, after.st_mtime_ns):
            fail("unstable")
    except OSError:
        fail("unreadable")
    finally:
        os.close(fd)
    try:
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object)
    except (UnicodeDecodeError, ValueError, RecursionError):
        fail("json")
    finally:
        del raw
    if not isinstance(data, dict):
        fail("schema")
    machine = data.get("machine_id")
    launcher = data.get("launcher")
    del data
    if not isinstance(machine, str) or machine != expected_machine:
        fail("machine_mismatch")
    if launcher is None:
        launcher = {}
    if not isinstance(launcher, dict):
        fail("schema")
    memory_mb = launcher.get("memory_mb", LAUNCHER_DEFAULT_MEMORY_MB)
    pids = launcher.get("pids", LAUNCHER_DEFAULT_PIDS)
    del launcher
    if type(memory_mb) is not int or type(pids) is not int or memory_mb <= 0 or pids <= 0:
        fail("caps_invalid")
    prove_loaded_host_config(before, path.parent.parent.parent)
    if memory_mb * 1024 ** 2 > NEW_WORKER_MEMORY_BYTES:
        fail("memory_cap_too_high")
    if pids > NEW_WORKER_PIDS:
        fail("pids_cap_too_high")
    return memory_mb * 1024 ** 2, pids


def parse_iso_utc(value):
    if not isinstance(value, str) or not value or len(value) > 64:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        return None
    return parsed.astimezone(datetime.timezone.utc)


def _quota_record_unhealthy(record):
    return any(record.get(flag) for flag in ("error", "stale", "limit_reached", "infinite"))


def quota_route_usable(name, route, now):
    """Mirror of the maintained quota sanitizer's fail-closed data acceptance of one route.

    Pure and in-memory; raw quota is never retained or printed. Only the finite routes in
    QUOTA_ROUTE_POLICY can be eligible. A route is usable only when it is status ok, fresh,
    not stale/limit-reached, every window (required and extra) is well formed with an ok
    status, every required window group has a validated present member, and the lowest
    present remaining percentage is strictly above the provider reserve (Codex: 15). A fully
    null slot (no percentage, no reset, not explicitly present, no error/status fault) is
    skipped only when it is a source-proven optional plan window of that provider.
    """
    policy = QUOTA_ROUTE_POLICY.get(name)
    if policy is None or not isinstance(route, dict) or route.get("status") != "ok" or _quota_record_unhealthy(route):
        return False
    details = route.get("details") if route.get("details") is not None else {}
    if not isinstance(details, dict) or details.get("limit_reached") is True or _quota_record_unhealthy(details):
        return False
    if details.get("limit_reached") is not None and type(details.get("limit_reached")) is not bool:
        return False
    for holder in (route, details):
        if holder.get("stale") not in (None, False):
            return False
    observed = [holder["observed_at"] for holder in (route, details) if holder.get("observed_at") is not None]
    parsed = [parse_iso_utc(value) for value in observed]
    if any(value is None for value in parsed) or (len(parsed) == 2 and parsed[0] != parsed[1]):
        return False
    if parsed:
        age = (now - parsed[0]).total_seconds()
        if not 0 <= age < QUOTA_STALE_S:
            return False
    windows = route.get("windows")
    if not isinstance(windows, dict) or not 1 <= len(windows) <= 32:
        return False
    lowest = None
    available = set()
    for window_name, window in windows.items():
        if not isinstance(window_name, str) or not window_name or not isinstance(window, dict):
            return False
        # Health and status are checked before any absent-slot handling, so a null slot
        # cannot hide an explicit error or a non-ok status.
        if _quota_record_unhealthy(window) or ("status" in window and window["status"] != "ok"):
            return False
        if "present" in window and type(window["present"]) is not bool:
            return False
        if (window_name in policy["optional"]
                and window.get("percent_remaining") is None and window.get("reset_at") is None
                and window.get("present") is not True):
            continue
        if window.get("present") is False:
            return False
        percent = window.get("percent_remaining")
        if not _finite_number(percent) or not 0 <= percent <= 100:
            return False
        if window.get("reset_at") is not None and parse_iso_utc(window["reset_at"]) is None:
            return False
        available.add(window_name)
        lowest = percent if lowest is None else min(lowest, percent)
    if any(not available.intersection(group) for group in policy["required"]):
        return False
    reserve = CODEX_QUOTA_RESERVE_PERCENT if name in CODEX_RESERVE_ROUTES else 0.0
    return lowest is not None and lowest > reserve


def evaluate_quota(raw, *, require_eligible_route, now=None):
    """Return None when quota acquisition is acceptable, else a closed reason."""
    if not isinstance(raw, dict):
        return "quota_unparseable"
    if not require_eligible_route:
        return None
    now = now or datetime.datetime.now(datetime.timezone.utc)
    if any(quota_route_usable(name, route, now) for name, route in raw.items() if isinstance(name, str)):
        return None
    return "quota_no_eligible_route"


def read_text_limited(path, limit=65536):
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
    try:
        data = os.read(fd, limit + 1)
    finally:
        os.close(fd)
    if len(data) > limit:
        raise OSError(errno.EFBIG, "proc file too large")
    return data.decode("utf-8", "replace")


BIRTH_WINDOW_S = (-2.0, 30.0)  # kernel start (btime + starttime) minus SDK created_at_ms, as qualified for current launches
PROC_STAT_MAX_BYTES = 4 * 1024 * 1024
CENSUS_SIGNATURE_FIELDS = (
    "worker_alive", "worker_pid", "worker_cgroup", "workload_pid", "workload_cgroup", "state", "phase",
    "created_at_ms", "workspace", "cwd", "limits", "containment_empty", "containment_cgroup_identity",
)


def read_proc_btime():
    """Kernel boot wall-clock seconds from /proc/stat, read in bounded chunks (the intr line can be huge)."""
    fd = os.open(f"{PROC_ROOT}/stat", os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
    try:
        data = b""
        while len(data) <= PROC_STAT_MAX_BYTES:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            data += chunk
    finally:
        os.close(fd)
    if len(data) > PROC_STAT_MAX_BYTES:
        raise OSError(errno.EFBIG, "proc stat too large")
    for line in data.decode("utf-8", "replace").splitlines():
        if line.startswith("btime "):
            value = int(line.split()[1])
            if value > 0:
                return value
    raise ValueError("btime")


def birth_matches(created_ms, start_ticks, btime, hz):
    return (type(created_ms) is int and created_ms > 0 and type(start_ticks) is int and btime > 0 and hz > 0
            and BIRTH_WINDOW_S[0] <= btime + start_ticks / hz - created_ms / 1000 <= BIRTH_WINDOW_S[1])


def relative_cgroup(raw):
    """Normalized '/a/b' cgroup path relative to the cgroup root, or None when malformed."""
    if not isinstance(raw, str) or not raw.startswith("/") or "\0" in raw:
        return None
    if raw.startswith(CGROUP_ROOT + "/"):
        raw = raw[len(CGROUP_ROOT):]
    parts = raw.split("/")[1:]
    if not parts or any(part in ("", ".", "..") for part in parts):
        return None
    return raw


def read_at_limited(dir_fd, name, limit=65536):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0), dir_fd=dir_fd)
    try:
        data = os.read(fd, limit + 1)
    finally:
        os.close(fd)
    if len(data) > limit:
        raise OSError(errno.EFBIG, "cgroup file too large")
    return data.decode("utf-8", "replace")


def open_cgroup_dir(group):
    """Directory fd of a cgroup reached component-by-component without following symlinks."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(CGROUP_ROOT, flags)
    try:
        for part in group.split("/")[1:]:
            nxt = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = nxt
    except BaseException:
        os.close(fd)
        raise
    return fd


def census_signature(census):
    """Signature keyed by lowercase UUID ids, the same validation/uniqueness as the initial inventory.

    Raises ValueError on an invalid or duplicate id; the raw id is part of the frozen row.
    """
    def freeze(row):
        # A missing key and an explicit null differ: absent identity becoming provided/malformed is a change.
        return (row["id"], *((field in row, json.dumps(row.get(field), sort_keys=True, default=str)) for field in CENSUS_SIGNATURE_FIELDS))
    signature = {}
    for row in census:
        ident = row.get("id") if isinstance(row, dict) else None
        if not isinstance(ident, str) or UUID_RE.fullmatch(ident) is None or ident.lower() in signature:
            raise ValueError("census id")
        signature[ident.lower()] = freeze(row)
    return signature


def parse_meminfo_available(text):
    for line in text.splitlines():
        if line.startswith("MemAvailable:"):
            parts = line.split()
            if len(parts) == 3 and parts[2] == "kB" and parts[1].isdigit():
                return int(parts[1]) * 1024
            return None
    return None


def nearest_existing(path):
    cur = pathlib.Path(os.path.abspath(path))
    while True:
        try:
            if os.path.lexists(cur):
                return pathlib.Path(os.path.realpath(cur))
        except OSError:
            return None
        if cur.parent == cur:
            return None
        cur = cur.parent



class PhysicalAdmission:
    """Read-only physical admission: current census, residency, disk, launcher caps and final RAM.

    Shared by Canary.admit and the standalone read-only physical check, which constructs
    only this class (no Canary state, lock, key, pending file or provider quota).
    """

    def __init__(self):
        self.hub = shlex.split("hub")
        self.a = shlex.split("a")
        self.cmd_timeout = 90.0
        self.in_cleanup = False
        self.active_deadline = None
        self.pyz_fd = None
        self.bridge_fd = None
        self.hub_pass_fds = ()
        self.current_stage = "admission"
        self.machine = "hetzner-aplexer"
        self.workspace = None

    def admission_deadline(self, deadline):
        if deadline is None:
            deadline = time.monotonic() + ADMISSION_BUDGET_S
        if not self.in_cleanup and self.active_deadline is not None:
            deadline = min(deadline, self.active_deadline)
        return deadline

    @staticmethod
    def admission_time_left(deadline):
        if time.monotonic() >= deadline:
            raise StageFailure("admission", "admission_deadline_exceeded")

    def sample_mem_available(self):
        try:
            value = parse_meminfo_available(read_text_limited(f"{PROC_ROOT}/meminfo"))
        except OSError:
            value = None
        if type(value) is not int or value <= 0:
            raise StageFailure("admission", "mem_available_unknown")
        return value

    def bridge_artifact_fd(self):
        """Sealed fd of the whole-archive-verified CLI (candidate), or of the owned periodic CLI."""
        if self.pyz_fd is not None:
            return self.pyz_fd
        if self.bridge_fd is not None:
            return self.bridge_fd
        if len(self.hub) != 1:
            raise StageFailure("admission", "cli_artifact_unbindable")
        import io, shutil, zipfile
        found = shutil.which(self.hub[0])
        if not found:
            raise StageFailure("admission", "cli_artifact_unbindable")
        try:
            real = os.path.realpath(found)
            st = os.lstat(real)
            if not stat.S_ISREG(st.st_mode) or st.st_uid not in (0, os.geteuid()) or (st.st_mode & 0o022):
                raise StageFailure("admission", "cli_artifact_unbindable")
            data = read_regular_nofollow(real)
            if not zipfile.is_zipfile(io.BytesIO(data)):
                raise StageFailure("admission", "cli_artifact_unbindable")
            self.bridge_fd = create_sealed_pyz_fd(data)
        except OSError:
            raise StageFailure("admission", "cli_artifact_unbindable")
        return self.bridge_fd

    def fetch_disk_floor_gb(self, deadline):
        fd = self.bridge_artifact_fd()
        rc, out, _ = self.run(
            [sys.executable, "-I", "-S", "-c", ADMISSION_BRIDGE, f"/proc/self/fd/{fd}"],
            deadline=deadline, pass_fd=fd,
        )
        if rc != 0:
            raise StageFailure("admission", "resource_settings_timeout" if rc == 124 else "resource_settings_unavailable")
        try:
            data = _closed_json(out, 4096)
        except ValueError:
            raise StageFailure("admission", "resource_settings_invalid")
        floor = data.get("disk_floor_gb") if isinstance(data, dict) and set(data) == {"disk_floor_gb"} else None
        if not _finite_number(floor) or not DISK_FLOOR_GB_RANGE[0] <= floor <= DISK_FLOOR_GB_RANGE[1]:
            raise StageFailure("admission", "resource_settings_invalid")
        return floor

    def read_census(self, deadline):
        rc, out, _ = self.run([*self.a, "list", "--json"], deadline=deadline)  # current inventory; no --all history
        if rc != 0:
            raise StageFailure("admission", "native_inventory_unavailable")
        try:
            rows = _closed_json(out, ADMISSION_JSON_MAX_BYTES)
        except ValueError:
            raise StageFailure("admission", "native_inventory_invalid")
        if not isinstance(rows, list) or len(rows) > ADMISSION_MAX_ROWS or not all(isinstance(r, dict) for r in rows):
            raise StageFailure("admission", "native_inventory_invalid")
        return rows

    def read_hub_rows(self, deadline):
        try:
            return self.list_items("admission", deadline)
        except StageFailure:
            raise StageFailure("admission", "managed_inflight_unavailable")

    def boottime(self):
        return time.clock_gettime(time.CLOCK_BOOTTIME)

    def namespace_inode(self, name):
        return os.stat(f"{PROC_ROOT}/self/ns/{name}").st_ino

    def measure_pid(self, row, pid):
        """One identity+memory sample for a census row's workload; raises OSError/ValueError if unknowable."""
        cgroup = row["workload_cgroup"]
        if (not isinstance(cgroup, str) or not cgroup.startswith("/") or "\0" in cgroup
                or ".." in cgroup.split("/")):
            raise ValueError("cgroup")
        group = f"{CGROUP_ROOT}{cgroup}"
        stat_text = read_text_limited(f"{PROC_ROOT}/{pid}/stat")
        fields = stat_text[stat_text.rindex(")") + 2:].split()
        start_ticks = int(fields[19])
        if f"0::{cgroup}" not in read_text_limited(f"{PROC_ROOT}/{pid}/cgroup").splitlines():
            raise ValueError("pid not in cgroup")
        members = {int(x) for x in read_text_limited(f"{group}/cgroup.procs").split()}
        if pid not in members:
            raise ValueError("pid not a member")
        status = {}
        for line in read_text_limited(f"{PROC_ROOT}/{pid}/status").splitlines():
            if ":" in line:
                key, _, value = line.partition(":")
                status[key] = value.split()
        rss_anon = int(status["RssAnon"][0]) * 1024
        mem_stat = {}
        for line in read_text_limited(f"{group}/memory.stat").splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1].isdigit():
                mem_stat[parts[0]] = int(parts[1])
        current = int(read_text_limited(f"{group}/memory.current").strip())
        return {
            "start_ticks": start_ticks, "members": frozenset(members), "rss_anon": rss_anon,
            "group_anon": mem_stat["anon"], "group_current": current,
        }

    def read_launcher_caps(self, deadline):
        """Actual new-worker caps of the selected Host launcher (seam; production reads the Host config)."""
        return read_launcher_caps(selected_launcher_config_path(), self.machine)

    def row_positively_dead(self, row):
        """True only for a current row whose worker is dead and whose containment holds nothing.

        worker_alive must be False. containment_empty True is positive proof.
        When aplexer left it NULL/absent (older terminal rows), the row must be
        terminal and any current cgroup it names must be gone or have no members;
        missing historical pid/cgroup metadata alone is not a live obligation.
        A phase of exited/failed on its own, or a live/unknown worker, never counts.
        """
        if row.get("worker_alive") is not False:
            return False
        empty = row.get("containment_empty")
        if empty is True:
            return True
        if empty is not None:
            return False
        if row.get("phase") not in ("exited", "failed") and row.get("state") not in ("exited", "failed"):
            return False
        for key in ("workload_cgroup", "containment_cgroup"):
            cgroup = row.get(key)
            if cgroup is None:
                continue
            if isinstance(cgroup, str) and cgroup.startswith(CGROUP_ROOT + "/"):
                cgroup = cgroup[len(CGROUP_ROOT):]
            if (not isinstance(cgroup, str) or not cgroup.startswith("/") or "\0" in cgroup
                    or ".." in cgroup.split("/")):
                return False
            try:
                members = read_text_limited(f"{CGROUP_ROOT}{cgroup}/cgroup.procs").split()
            except FileNotFoundError:
                continue
            except OSError:
                return False
            if members:
                return False
        return True

    def row_memory_cap(self, row):
        limits = row.get("limits")
        cap = limits.get("memory_bytes") if isinstance(limits, dict) else None
        if type(cap) is int and cap > 0:
            return cap
        return None

    def workload_pid_absent(self, pid):
        """True only when opening the workload's /proc/PID identity genuinely returns ENOENT."""
        try:
            fd = os.open(f"{PROC_ROOT}/{pid}/stat", os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
        except FileNotFoundError:
            return True
        except OSError:
            raise StageFailure("admission", "resident_measurement_unknown")
        os.close(fd)
        return False

    def select_residency_target(self, row):
        """Chosen live PID/cgroup of a live current row, resolved BEFORE the duplicate/shared maps.

        The workload is chosen unless its PID is None (not starting) or opening its
        /proc/PID identity returns ENOENT. Only then is the SAME row's native
        coordinator (worker_pid/worker_cgroup, worker_alive is True) selected. Any other
        condition (existing PID, EACCES, invalid type) stays on the workload path and holds there.
        """
        pid, phase, state = row.get("workload_pid"), row.get("phase"), row.get("state")
        recorded_group = row.get("workload_cgroup")
        workload = {"kind": "workload", "pid": pid, "group": recorded_group if isinstance(recorded_group, str) else None}
        if pid is None:
            if phase == "starting" or state == "starting":
                return workload  # starting rows keep their recorded-cap reservation
            premise, failure = "none", "resident_identity_unknown"
        elif type(pid) is int and pid > 0:
            if not self.workload_pid_absent(pid):
                if "containment_cgroup_identity" not in row:
                    return self.select_current_workload(row, pid)  # genuinely absent recorded identity only
                return workload
            premise, failure = "enoent", "resident_measurement_unknown"
        else:
            return workload
        worker_pid, worker_group = row.get("worker_pid"), relative_cgroup(row.get("worker_cgroup"))
        if row.get("worker_alive") is not True or type(worker_pid) is not int or worker_pid <= 0 or worker_group is None:
            raise StageFailure("admission", failure)
        return {"kind": "coordinator", "pid": worker_pid, "group": worker_group, "premise": premise}

    def select_current_workload(self, row, pid):
        """Chosen target for an EXISTING workload whose recorded containment identity key is genuinely absent.

        The same workload PID stays chosen (never the coordinator). Its cgroup is derived from the kernel
        (the single `0::` line of /proc/PID/cgroup) here, before the duplicate/shared maps. A recorded
        workload_cgroup, when present, must be valid and equal to it; any contradiction, malformed value,
        unproven worker liveness or unreadable kernel state is a HOLD, never a fallback.
        """
        if row.get("worker_alive") is not True:
            raise StageFailure("admission", "resident_identity_unknown")
        try:
            lines = read_text_limited(f"{PROC_ROOT}/{pid}/cgroup").splitlines()
        except OSError:
            raise StageFailure("admission", "resident_measurement_unknown")
        groups = [line[3:] for line in lines if line.startswith("0::")]
        derived = relative_cgroup(groups[0]) if len(groups) == 1 else None
        recorded = row.get("workload_cgroup")
        if derived is None or (recorded is not None and relative_cgroup(recorded) != derived):
            raise StageFailure("admission", "resident_identity_unknown")
        return {"kind": "workload_current", "pid": pid, "group": derived, "premise": "identity_absent"}

    def sample_coordinator(self, target, boot_id, ns_ids):
        """One fresh kernel sample of a chosen kernel-proven PID (coordinator or current workload): identity plus anonymous/cap accounting."""
        pid, group = target["pid"], target["group"]
        try:
            if read_text_limited(f"{PROC_ROOT}/sys/kernel/random/boot_id").strip() != boot_id:
                raise StageFailure("admission", "resident_identity_unknown")
            raw = read_text_limited(f"{PROC_ROOT}/{pid}/stat")
            fields = raw[raw.rindex(")") + 2:].split()
            if fields[0] in ("Z", "X"):
                raise StageFailure("admission", "resident_identity_unknown")
            start_ticks = int(fields[19])
            groups = [line[3:] for line in read_text_limited(f"{PROC_ROOT}/{pid}/cgroup").splitlines() if line.startswith("0::")]
            namespaces = {name: os.stat(f"{PROC_ROOT}/{pid}/ns/{name}").st_ino for name in ("cgroup", "mnt")}
            if groups != [group] or namespaces != ns_ids:
                raise StageFailure("admission", "resident_identity_unknown")
            fd = open_cgroup_dir(group)
            try:
                opened = os.fstat(fd)
                members = {int(x) for x in read_at_limited(fd, "cgroup.procs").split()}
                mem_stat = {}
                for line in read_at_limited(fd, "memory.stat").splitlines():
                    parts = line.split()
                    if len(parts) == 2 and parts[1].isdigit():
                        mem_stat[parts[0]] = int(parts[1])
                current = int(read_at_limited(fd, "memory.current").strip())
                maximum = read_at_limited(fd, "memory.max").strip()
                status = {}
                for line in read_text_limited(f"{PROC_ROOT}/{pid}/status").splitlines():
                    if ":" in line:
                        key, _, value = line.partition(":")
                        status[key] = value.split()
                if len(status["RssAnon"]) != 2 or status["RssAnon"][1] != "kB" or not status["RssAnon"][0].isdecimal():
                    raise ValueError("RssAnon")
                rss_anon = int(status["RssAnon"][0]) * 1024
                raw_again = read_text_limited(f"{PROC_ROOT}/{pid}/stat")
                after = os.fstat(fd)
                on_path = os.stat(f"{CGROUP_ROOT}{group}")
                members_again = {int(x) for x in read_at_limited(fd, "cgroup.procs").split()}
            finally:
                os.close(fd)
            if (int(raw_again[raw_again.rindex(")") + 2:].split()[19]) != start_ticks
                    or (after.st_dev, after.st_ino) != (opened.st_dev, opened.st_ino)
                    or (on_path.st_dev, on_path.st_ino) != (opened.st_dev, opened.st_ino)):
                raise StageFailure("admission", "resident_identity_unstable")
            if pid not in members or pid not in members_again:
                raise StageFailure("admission", "resident_identity_unknown")
            return {
                "identity": (boot_id, start_ticks, group, opened.st_dev, opened.st_ino, tuple(sorted(namespaces.items()))),
                "start_ticks": start_ticks, "rss_anon": rss_anon, "group_anon": mem_stat["anon"], "group_current": current,
                "cap": int(maximum) if maximum.isdecimal() and int(maximum) > 0 else None,
            }
        except (OSError, ValueError, KeyError, IndexError):
            raise StageFailure("admission", "resident_measurement_unknown")

    def coordinator_reserve(self, row, target, boot_id, ns_ids, boot_now, btime, deadline):
        """Reserve for a chosen native coordinator or current-proof workload, proven only by its own kernel identity.

        Recorded SDK scope, identity and caps prove nothing here (the current-proof workload's recorded
        identity is genuinely absent and is never invented). A mature
        (>= 180 s), positively resident coordinator reserves 0 extra even with memory.max=max
        (already counted in MemAvailable; not a growth bound). A young or uncertain one needs
        a finite kernel cap, charged whole per row, else the admission holds.
        """
        if deadline is not None:
            self.admission_time_left(deadline)
        first = self.sample_coordinator(target, boot_id, ns_ids)
        if deadline is not None:
            self.admission_time_left(deadline)
        second = self.sample_coordinator(target, boot_id, ns_ids)
        if first["identity"] != second["identity"]:
            raise StageFailure("admission", "resident_identity_unstable")
        ticks = os.sysconf("SC_CLK_TCK")
        try:
            if btime is None:
                btime = read_proc_btime()
        except (OSError, ValueError):
            raise StageFailure("admission", "resident_identity_unknown")
        if not birth_matches(row.get("created_at_ms"), first["start_ticks"], btime, ticks):
            raise StageFailure("admission", "resident_identity_unknown")
        age = boot_now - first["start_ticks"] / ticks
        if not _finite_number(age) or age < 0:
            raise StageFailure("admission", "resident_age_unknown")
        for sample in (first, second):
            if sample["rss_anon"] <= 0 or not 0 < sample["group_anon"] <= sample["group_current"]:
                raise StageFailure("admission", "resident_anonymous_unknown")
        target["identity"] = first["identity"]
        target["cap"] = None
        if age >= MATURE_RESIDENT_AGE_S and row.get("state") == "running" and row.get("phase") == "running":
            return 0, "mature"
        if first["cap"] is None or first["cap"] != second["cap"]:
            raise StageFailure("admission", "resident_bound_unknown")
        target["cap"] = first["cap"]
        return first["cap"], "young"

    def revalidate_fallbacks(self, census, targets, boot_id, ns_ids, deadline, *, hub_rows=None, native_ids=None):
        """Final bounded check: every kernel-derived premise, selection and chosen identity is still the sampled one.

        Covers native-coordinator fallbacks and current-proof workloads (absent recorded identity). For the
        latter the managed in-flight rows are re-read once as well and must not gain or lose an uncovered row.
        """
        derived = [(row, targets[row["id"]]) for row in census
                   if targets.get(row.get("id"), {}).get("kind") in ("coordinator", "workload_current")]
        if not derived:
            return
        self.admission_time_left(deadline)
        final = self.read_census(deadline)
        try:
            changed = census_signature(final) != census_signature(census)
        except ValueError:
            raise StageFailure("admission", "resident_fallback_changed")  # duplicate/invalid final id: ambiguous snapshot
        if changed:
            raise StageFailure("admission", "resident_fallback_changed")
        if hub_rows is not None and any(target["kind"] == "workload_current" for _row, target in derived):
            self.admission_time_left(deadline)
            freeze = lambda rows: sorted(json.dumps([r.get(k) for k in ("id", "native_id", "workspace", "cwd", "host", "machine_id", "machine")], default=str)
                                         for r in self.uncovered_inflight_rows(rows, native_ids))
            if freeze(self.read_hub_rows(deadline)) != freeze(hub_rows):
                raise StageFailure("admission", "resident_fallback_changed")
        for row, target in derived:
            self.admission_time_left(deadline)
            if target["kind"] == "coordinator":
                if row.get("workload_pid") is not None and not self.workload_pid_absent(row["workload_pid"]):
                    raise StageFailure("admission", "resident_fallback_changed")
                if self.sample_coordinator(target, boot_id, ns_ids)["identity"] != target["identity"]:
                    raise StageFailure("admission", "resident_fallback_changed")
                continue
            sample = self.sample_coordinator(target, boot_id, ns_ids)
            if sample["identity"] != target["identity"] or (target["cap"] is not None and sample["cap"] != target["cap"]):
                raise StageFailure("admission", "resident_fallback_changed")

    def resident_reserve(self, row, shared, boot_id, ns_ids, boot_now, *, target=None, btime=None, deadline=None):
        """(extra bytes, kind) this current native row may still claim beyond what MemAvailable already reflects.

        kind is "dead" (positively no obligation), "mature" (identity-bound proven
        resident) or "young" (anything else that is live or still ramping).
        """
        if self.row_positively_dead(row):
            return 0, "dead"
        if target is None:
            target = self.select_residency_target(row)
        if target["kind"] in ("coordinator", "workload_current"):
            return self.coordinator_reserve(row, target, boot_id, ns_ids, boot_now, btime, deadline)
        phase, state = row.get("phase"), row.get("state")
        cap = self.row_memory_cap(row)
        pid = row.get("workload_pid")
        if pid is None and (phase == "starting" or state == "starting"):
            if cap is None:
                raise StageFailure("admission", "resident_bound_unknown")
            return cap, "young"
        identity = row.get("containment_cgroup_identity")
        if type(pid) is not int or pid <= 0 or not isinstance(identity, dict) or identity.get("boot_id") != boot_id:
            raise StageFailure("admission", "resident_identity_unknown")
        for key, name in (("cgroup_namespace_inode", "cgroup"), ("mount_namespace_inode", "mnt")):
            if key in identity and type(identity[key]) is int and identity[key] != ns_ids[name]:
                raise StageFailure("admission", "resident_identity_unknown")
        try:
            first = self.measure_pid(row, pid)
            second = self.measure_pid(row, pid)
        except (OSError, ValueError, KeyError, IndexError):
            raise StageFailure("admission", "resident_measurement_unknown")
        if first["start_ticks"] != second["start_ticks"] or first["members"] != second["members"] or pid not in first["members"]:
            raise StageFailure("admission", "resident_identity_unstable")
        ticks = os.sysconf("SC_CLK_TCK")
        age = boot_now - first["start_ticks"] / ticks
        if not _finite_number(age) or age < 0:
            raise StageFailure("admission", "resident_age_unknown")
        group_ok = 0 < first["group_anon"] <= first["group_current"] and 0 < second["group_anon"] <= second["group_current"]
        pid_ok = first["rss_anon"] > 0 and second["rss_anon"] > 0
        if shared:
            # A shared cgroup cannot prove one row's usage: per-PID anonymous memory only.
            mature_ok = pid_ok
            anon = min(first["rss_anon"], second["rss_anon"])
        else:
            mature_ok = pid_ok and group_ok
            anon = min(first["group_anon"], second["group_anon"])
        if age >= MATURE_RESIDENT_AGE_S and mature_ok:
            return 0, "mature"
        if cap is None:
            raise StageFailure("admission", "resident_bound_unknown")
        return (max(0, cap - anon) if mature_ok else cap), "young"

    def uncovered_inflight_rows(self, hub_rows, native_ids):
        """Current Hub in-flight rows on this machine with no native census row yet."""
        rows = []
        for item in hub_rows:
            if not isinstance(item, dict):
                raise StageFailure("admission", "managed_inflight_invalid")
            host = item.get("host") or item.get("machine_id") or item.get("machine")
            if host is not None and host != self.machine:
                continue
            state = item.get("state")
            if state in HUB_TERMINAL_STATES:
                continue
            if state not in HUB_INFLIGHT_STATES:
                raise StageFailure("admission", "managed_inflight_unknown_state")
            native = item.get("native_id")
            if isinstance(native, str) and native.lower() in native_ids:
                continue
            rows.append(item)
        return rows

    def uncovered_inflight_reserve(self, hub_rows, native_ids):
        return len(self.uncovered_inflight_rows(hub_rows, native_ids)) * NEW_WORKER_MEMORY_BYTES

    @staticmethod
    def growth_devices(*paths):
        """Device ids a worker's workspace, cwd and workspace/.local/tmp write to; None if unknowable."""
        real = [os.path.realpath(p) for p in paths if isinstance(p, str) and p.startswith("/") and "\0" not in p]
        if not real:
            return None
        real.append(os.path.join(real[0], ".local", "tmp"))
        devices = set()
        for path in real:
            existing = nearest_existing(path)
            if existing is None:
                return None
            try:
                devices.add(os.stat(existing).st_dev)
            except OSError:
                return None
        return devices

    def disk_free_after_margin(self, paths):
        """{st_dev: free bytes} for the nearest existing ancestors of paths (one entry per device)."""
        seen = {}
        for path in paths:
            existing = nearest_existing(path)
            if existing is None:
                raise StageFailure("admission", "disk_unknown")
            try:
                st = os.stat(existing)
                vfs = os.statvfs(existing)
            except OSError:
                raise StageFailure("admission", "disk_unknown")
            seen.setdefault(st.st_dev, vfs.f_bavail * vfs.f_frsize)
        return seen

    def physical_inputs(self, deadline, workspace=None):
        """Validated workspace, actual new-worker memory cap and disk floor (GB), in the original order."""
        workspace = self.workspace if workspace is None else workspace
        if (not isinstance(workspace, str) or not workspace.startswith("/") or "\0" in workspace):
            raise StageFailure("admission", "workspace_invalid")
        self.admission_time_left(deadline)
        worker_bytes, worker_pids = self.read_launcher_caps(deadline)
        if (type(worker_bytes) is not int or type(worker_pids) is not int or worker_bytes <= 0 or worker_pids <= 0
                or worker_bytes > NEW_WORKER_MEMORY_BYTES or worker_pids > NEW_WORKER_PIDS):
            raise StageFailure("admission", "launcher_config_caps_invalid")
        floor_gb = self.fetch_disk_floor_gb(deadline)
        self.admission_time_left(deadline)
        return workspace, worker_bytes, floor_gb

    def physical_admit(self, deadline, workspace, worker_bytes, floor_gb):
        """Current census, residency, disk growth and final RAM arithmetic (no quota, no spawn, no state).

        Returns closed aggregate counts/headrooms only; raises StageFailure('admission', closed_reason).
        """
        census = self.read_census(deadline)
        hub_rows = self.read_hub_rows(deadline)
        self.admission_time_left(deadline)
        try:
            boot_id = read_text_limited(f"{PROC_ROOT}/sys/kernel/random/boot_id").strip()
            ns_ids = {"cgroup": self.namespace_inode("cgroup"), "mnt": self.namespace_inode("mnt")}
            boot_now = self.boottime()
        except (OSError, ValueError):
            raise StageFailure("admission", "resident_identity_unknown")
        native_ids, by_pid, by_group = set(), {}, {}
        counts = {"census_rows": len(census), "mature": 0, "young": 0, "kernel_current": 0, "coordinator_fallback": 0}
        for row in census:
            native_id = row.get("id")
            if not isinstance(native_id, str) or not UUID_RE.fullmatch(native_id) or native_id.lower() in native_ids:
                raise StageFailure("admission", "native_inventory_invalid")
            native_ids.add(native_id.lower())
        # Positively dead rows carry no live pid/cgroup claim: they join neither the
        # identity-ambiguity maps nor the reserves (their stale pid/cgroup may be reused).
        live = [row for row in census if not self.row_positively_dead(row)]
        # Resolve every row's CHOSEN live PID/group (workload, or coordinator fallback) before the maps.
        targets = {}
        for row in live:
            self.admission_time_left(deadline)
            targets[row["id"]] = self.select_residency_target(row)
        for row in live:
            target = targets[row["id"]]
            by_pid.setdefault(target["pid"], []).append(row["id"])
            by_group.setdefault(target["group"], []).append(row["id"])
        reserve = 0
        growing = []  # (workspace, cwd) of every live current worker / uncovered task that may still write to disk
        for row in live:
            target = targets[row["id"]]
            pid, group = target["pid"], target["group"]
            # (A coordinator's cgroup may hold other processes: coordinator_reserve always proves per PID.)
            shared = len(by_group.get(group, ())) > 1 and group is not None
            if pid is not None and len(by_pid.get(pid, ())) > 1:
                raise StageFailure("admission", "resident_identity_ambiguous")
            extra, kind = self.resident_reserve(row, shared, boot_id, ns_ids, boot_now, target=target, deadline=deadline)
            reserve += extra
            if kind in ("mature", "young"):
                counts[kind] += 1
            if target["kind"] == "workload_current":
                counts["kernel_current"] += 1
            elif target["kind"] == "coordinator":
                counts["coordinator_fallback"] += 1
            growing.append((row.get("workspace"), row.get("cwd")))  # RAM maturity proves nothing about disk
        uncovered = self.uncovered_inflight_rows(hub_rows, native_ids)
        # Not-yet-native tasks may have been (or be) launched under other caps: the ceiling, never the selected value.
        reserve += len(uncovered) * NEW_WORKER_MEMORY_BYTES
        growing.extend((item.get("workspace"), item.get("cwd")) for item in uncovered)
        # Disk: actual resolved cwd and own TMP filesystems, one charge per device.
        resolved = os.path.realpath(workspace)
        tmp_dir = os.path.join(resolved, ".local", "tmp")
        floor_bytes = int(floor_gb * 1024 ** 3)
        free_by_device = self.disk_free_after_margin([resolved, tmp_dir])
        charged = {device: 0 for device in free_by_device}
        for paths in growing:
            devices = self.growth_devices(*paths)
            if devices is None:
                raise StageFailure("admission", "disk_growth_unknown")
            for device in devices & set(free_by_device):
                charged[device] = min(ADMISSION_DISK_RESERVE_CAP_BYTES, charged[device] + ADMISSION_DISK_WORKER_RESERVE_BYTES)
        disk_headroom = None
        for device, free in free_by_device.items():
            # floor + start spike + this new worker's own known growth + same-device others
            room = free - ADMISSION_DISK_SPIKE_BYTES - ADMISSION_DISK_WORKER_RESERVE_BYTES - charged[device] - floor_bytes
            if room < 0:
                raise StageFailure("admission", "disk_below_floor")
            disk_headroom = room if disk_headroom is None else min(disk_headroom, room)
        # Final fresh RAM sample after every slow read, before any spawn.
        self.admission_time_left(deadline)
        self.revalidate_fallbacks(census, targets, boot_id, ns_ids, deadline, hub_rows=hub_rows, native_ids=native_ids)
        self.admission_time_left(deadline)
        available = self.sample_mem_available()
        if available - reserve - worker_bytes < ADMISSION_RAM_FLOOR_BYTES:
            raise StageFailure("admission", "memory_below_floor")
        self.admission_time_left(deadline)
        counts.update(
            live_rows=len(live), uncovered_inflight=len(uncovered), reserve_bytes=reserve, new_worker_bytes=worker_bytes,
            ram_headroom_bytes=available - reserve - worker_bytes - ADMISSION_RAM_FLOOR_BYTES, disk_headroom_bytes=disk_headroom,
        )
        return counts

    def run(self, argv, timeout=None, *, deadline=None, env=None, pass_fd=None):
        limit = self.cmd_timeout if timeout is None else timeout
        effective_deadline = deadline
        if not self.in_cleanup and self.active_deadline is not None:
            effective_deadline = self.active_deadline if effective_deadline is None else min(effective_deadline, self.active_deadline)
        if effective_deadline is not None:
            limit = min(limit, effective_deadline - time.monotonic())
        if limit <= 0:
            return 124, "", ""
        pass_fds = self.hub_pass_fds if (self.pyz_fd is not None and f"/proc/self/fd/{self.pyz_fd}" in argv) else ()
        if pass_fd is not None and f"/proc/self/fd/{pass_fd}" in argv:
            pass_fds = (pass_fd,)
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



PHYSICAL_CHECK_COUNT_KEYS = (
    "census_rows", "live_rows", "mature", "young", "kernel_current", "coordinator_fallback", "uncovered_inflight",
    "reserve_bytes", "new_worker_bytes", "ram_headroom_bytes", "disk_headroom_bytes",
)


def physical_admission_check(admission=None):
    """Read-only physical admission preview: one JSON-able dict of closed counts and reasons.

    Runs BEFORE any Canary construction: no lock, key, pending/once/state file, provider quota, session
    start/capture/stop/send, auth refresh or config mutation. It uses the same PhysicalAdmission algorithm
    as Canary.admit (target selection, residency, disk growth/floor, launcher caps, final revalidation and
    RAM). Workspace authority is the fixed root config (or the default), never an argument or environment
    value. Never authorizes a start; quota_checked is always false.
    """
    result = {"kind": "physical_admission_check", "ok": False, "status": "hold", "reason": None, "quota_checked": False,
              "full_admission": False, "source_sha256": None}
    try:
        result["source_sha256"] = compute_helper_sha256()
    except (OSError, ValueError):
        result["source_sha256"] = "unavailable"
    try:
        admission = PhysicalAdmission() if admission is None else admission
        deadline = time.monotonic() + ADMISSION_BUDGET_S
        configured = read_workspace_config()
        workspace = configured if configured is not None else str(pathlib.Path.home() / "work" / "hub-canary")
        workspace, worker_bytes, floor_gb = admission.physical_inputs(deadline, workspace)
        counts = admission.physical_admit(deadline, workspace, worker_bytes, floor_gb)
        result.update(ok=True, status="pass", reason="physical_ok")
        result.update({key: counts[key] for key in PHYSICAL_CHECK_COUNT_KEYS if type(counts.get(key)) is int})
        result["disk_floor_gb"] = floor_gb
    except StageFailure as failure:
        result["reason"] = failure.reason if re.fullmatch(r"[a-z][a-z0-9_]{0,79}", str(failure.reason)) else "physical_check_failed"
    except Exception:  # closed output only: never a raw exception text
        result["reason"] = "physical_check_internal_error"
    return result


class Canary(PhysicalAdmission):
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
        super().__init__()
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
        self.bridge_fd = None
        self.admission_hold = None
        self.workspace_source = "env_default"
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
            capture_ceiling = TRUSTED_FULL_CAPTURE_TIMEOUT_S if mode == "full" else TRUSTED_CAPTURE_TIMEOUT_S
            self.capture_timeout = min(env_num("HUB_CANARY_CAPTURE_TIMEOUT_S", capture_ceiling), capture_ceiling)
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
                    "Use two sequential shell tool calls. First run a separate foreground shell command. "
                    "If the session wrapper requires a private first-action evidence file, begin that same command with the required `aplexer whoami --json` "
                    "and its complete stdout redirected only to that exact private evidence path; use `&&` before the marker operation so the file-writing step must succeed first. "
                    "This foreground command must output only the marker: never tee or print the whoami JSON or extra prose. "
                    "In that same foreground command, print the "
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
                "Use two sequential shell tool calls. First run a separate foreground shell command. "
                    "If the session wrapper requires a private first-action evidence file, begin that same command with the required `aplexer whoami --json` "
                    "and its complete stdout redirected only to that exact private evidence path; use `&&` before the marker operation so the file-writing step must succeed first. "
                    "This foreground command must output only the marker: never tee or print the whoami JSON or extra prose. "
                    "In that same foreground command, print the "
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
        self._zai_native_seen = self._zai_native_invalidated = False
        self._zai_native_binding = None
        self.started_at = self.running_deadline = None
        self.start_attempted = self.stopped = False

    # ---- workspace authority ------------------------------------------------

    def resolve_workspace(self):
        """Fixed root-owned config wins over env/default; invalid never falls back."""
        self.current_stage = "admission"
        configured = read_workspace_config()
        if configured is not None:
            self.workspace = configured
            self.workspace_source = "root_config"
        else:
            self.workspace_source = "env_default"

    # ---- admission collectors (each is a narrow, replaceable seam) ----------

    def acquire_quota(self, deadline):
        rc, out, _ = self.run(["quse", "--json"], deadline=deadline)
        if rc != 0:
            raise StageFailure("admission", "quota_timeout" if rc == 124 else "quota_acquisition_failed")
        try:
            raw = _closed_json(out, 1024 * 1024)
        except ValueError:
            raise StageFailure("admission", "quota_unparseable")
        reason = evaluate_quota(raw, require_eligible_route=(self.mode == "full"))
        del raw, out
        if reason:
            raise StageFailure("admission", reason)

    def admit(self, deadline=None, workspace=None, *, seam="start"):
        """Finite fresh pre-start admission. Raises StageFailure('admission', closed_reason)."""
        self.current_stage = "admission" if not self.in_cleanup else self.current_stage
        deadline = self.admission_deadline(deadline)
        workspace, worker_bytes, floor_gb = self.physical_inputs(deadline, workspace)
        self.acquire_quota(deadline)
        self.admission_time_left(deadline)
        self.physical_admit(deadline, workspace, worker_bytes, floor_gb)

    def admission_allows(self, deadline, workspace=None):
        """Replay-seam wrapper: True only when a fresh admission passes."""
        stage = self.current_stage
        try:
            self.admit(deadline, workspace, seam="replay")
            return True
        except StageFailure as failure:
            self.admission_hold = failure.reason
            return False
        finally:
            self.current_stage = stage

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

    @staticmethod
    def parse_health_url(raw):
        try:
            parts = urllib.parse.urlsplit(raw)
            host, port = parts.hostname, parts.port
        except ValueError:
            return None
        if not host or parts.username is not None or parts.password is not None:
            return None
        if parts.path != "/health" or parts.query or parts.fragment or "@" in parts.netloc:
            return None
        if parts.scheme == "http":
            try:
                if not ipaddress.ip_address(host).is_loopback:
                    return None
            except ValueError:
                return None
        elif parts.scheme != "https":
            return None
        return parts.scheme, host, port

    def check_health(self):
        """Fail before start unless the public /health body is HTTP 200 JSON with literal ok:true.

        The request runs in an isolated child (fixed code, no shell) through
        self.run, so DNS, connect, headers and body are all ended by killing
        and reaping that child's process group at min(HEALTH_TIMEOUT_S,
        remaining active budget). Sends no credentials, follows no redirects,
        reads at most HEALTH_MAX_BYTES + 1 and never reports URL, headers or body.
        """
        self.current_stage = "health"
        target = self.parse_health_url(os.environ.get("HUB_CANARY_HEALTH_URL") or DEFAULT_HEALTH_URL)
        if target is None:
            raise StageFailure("health", "health_url_invalid")
        scheme, host, port = target
        deadline = self.stage_deadline(HEALTH_TIMEOUT_S)
        budget = deadline - time.monotonic()
        if budget <= 0:
            raise StageFailure("health", "health_timeout")
        argv = [sys.executable, "-I", "-S", "-c", HEALTH_CHILD, scheme, host, "" if port is None else str(port),
                f"{budget:.3f}", str(HEALTH_MAX_BYTES)]
        rc, out, _ = self.run(argv, timeout=budget, deadline=deadline)
        if rc == 124:
            raise StageFailure("health", "health_timeout")
        token = out.strip() if rc == 0 else ""
        if token == "ok":
            return
        raise StageFailure("health", token if token in HEALTH_REASONS else "health_unreachable")

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
            "purpose": "review",
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
        if (not sid and all(isinstance(rec.get(k), str) and rec.get(k) for k in ("repo", "machine", "workspace", "name", "key", "prompt", "mode"))
                and self.admission_allows(deadline, rec["workspace"])):
            # Replays keep the ORIGINAL recorded workspace/key/body; admission only
            # decides whether a possible new START may be sent now. When it is held
            # the pending file is retained and resolution continues by lookup only.
            replay_args = [
                *self.hub, "sessions", "start", "--no-wait",
                "--repo", rec["repo"], "--machine", rec["machine"],
                "--workspace", rec["workspace"], "--name", rec["name"],
                "--key", rec["key"], "--prompt", rec["prompt"], "--purpose", rec.get("purpose", "implement"),
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
                "--key", self.key, "--prompt", self.prompt, "--purpose", "review"]
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
        self.current_stage = "admission"
        # Fresh admission before EVERY possible Hub START; refusal happens before
        # any pending state or session exists, so there is nothing to clean up.
        self.admit(self.stage_deadline(ADMISSION_BUDGET_S))
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
        if eng == "zai":
            native = zai_native_return_proof(out, self.marker, getattr(self, "_zai_native_binding", None))
            if native["invalidated"] or native["seen"] or getattr(self, "_zai_native_seen", False):
                self._zai_native_seen = True
                self._zai_native_invalidated = getattr(self, "_zai_native_invalidated", False) or native["invalidated"]
                self._zai_native_binding = native["binding"]
                return native["proven"] and not self._zai_native_invalidated

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
        if self.candidate_sha is not None and self.mode == "full" and self.active_deadline is not None:
            # Keep the original active deadline and reserve the normal STOP
            # ceiling even when an operator lowers a stage timeout.
            deadline = min(deadline, self.active_deadline - TRUSTED_STOP_TIMEOUT_S)
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
                "--status", "archived", "--reason", f"deploy_gate_{clean(eff_mode, 16)}_canary",
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
            if self.admission_allows(limit_deadline):
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
            self.resolve_workspace()
            self.acquire_deploy_gate_lock()
            self.verify_candidate_host_release()
            self.reconcile_prior_pending()
            if not self.deploy_gate:
                self.check_health()
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
            if self.bridge_fd is not None:
                try:
                    os.close(self.bridge_fd)
                except OSError:
                    pass
                self.bridge_fd = None
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
    p.add_argument("--physical-admission-check", action="store_true",
                   help="read-only preview of the physical admission (no quota, no start, no state); never authorizes a start")
    args = p.parse_args()
    if args.physical_admission_check:
        if (args.mode or args.deploy_gate or args.print_helper_sha256 or args.candidate_sha or args.candidate_cli_sha256
                or args.run_token or args.helper_sha256):
            p.error("--physical-admission-check takes no other option")
        result = physical_admission_check()
        print(json.dumps(result, sort_keys=True), flush=True)
        return 0 if result["ok"] else 1
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
