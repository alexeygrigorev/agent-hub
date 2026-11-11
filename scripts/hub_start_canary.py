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


def ensure_private_dir(path, *, stage="lock", prefix="state_dir"):
    target = pathlib.Path(path)
    try:
        st = os.lstat(target)
    except FileNotFoundError:
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            os.mkdir(target, 0o700)
            st = os.lstat(target)
        except OSError:
            raise StageFailure(stage, f"{prefix}_create_failed")
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
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


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
    def __init__(
        self,
        mode,
        run_id,
        *,
        candidate_sha=None,
        candidate_cli_sha256=None,
        run_token=None,
        expected_helper_sha256=None,
    ):
        self.mode, self.run_id = mode, run_id
        self.candidate_sha = candidate_sha
        self.candidate_cli_sha256 = candidate_cli_sha256
        self.run_token = run_token
        self.expected_helper_sha256 = expected_helper_sha256
        self.helper_sha256 = None
        self.host_version = None
        self.hub_pyz_sha256 = None
        self.intake_archived = None
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
            self.lock_wait = max(0.0, env_num("HUB_CANARY_LOCK_WAIT_S", TRUSTED_LOCK_WAIT_S))
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
        if self.candidate_sha is None:
            return
        self.current_stage = "lock"
        self.state_dir_path = ensure_private_dir(raw_state_dir_path(), stage="lock", prefix="state_dir")
        self.lock_fd = acquire_lock_fd(
            self.state_dir_path / "deploy-gate.lock",
            self.lock_wait,
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
        try:
            self.pending_path.unlink()
        except FileNotFoundError:
            pass

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
            self.pause(deadline)
        raise StageFailure("host_release", f"timeout_{reason}")

    def reconcile_prior_pending(self):
        if self.pending_dir is None:
            return
        self.current_stage = "cleanup"
        pending_files = sorted(self.pending_dir.glob("*.json"))
        for pfile in pending_files:
            deadline = self.stage_deadline(self.stop_timeout)
            try:
                rec = json.loads(read_regular_nofollow(pfile, max_bytes=65536).decode("utf-8"))
            except (OSError, ValueError):
                raise StageFailure("cleanup", "prior_pending_corrupt")
            if not isinstance(rec, dict):
                raise StageFailure("cleanup", "prior_pending_corrupt")
            sid = rec.get("session_id") if isinstance(rec.get("session_id"), str) else None
            native_id = rec.get("native_id") if isinstance(rec.get("native_id"), str) else None
            key = rec.get("key") if isinstance(rec.get("key"), str) else None
            name = rec.get("name") if isinstance(rec.get("name"), str) else None
            if not sid or not native_id:
                try:
                    items = self.list_items("cleanup", deadline)
                except StageFailure:
                    items = []
                matched = next(
                    (
                        item for item in items
                        if isinstance(item, dict)
                        and (
                            (sid and item.get("id") == sid)
                            or (name and item.get("name") == name)
                            or (key and item.get("key") == key)
                        )
                    ),
                    None,
                )
                if matched is not None:
                    sid = sid or matched.get("id")
                    native_id = native_id or matched.get("native_id")
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
            if not sid or not key:
                raise StageFailure("cleanup", "prior_pending_reconciliation_failed")
            if self.stop_session(sid, f"{key}-reconcile", deadline) != 0:
                raise StageFailure("cleanup", "prior_pending_reconciliation_failed")
            if native_id:
                try:
                    self.wait_gone_for(native_id, deadline)
                except StageFailure:
                    raise StageFailure("cleanup", "prior_pending_reconciliation_failed")
            if rec.get("candidate_sha") and not self.archive_intake_for(sid, deadline):
                raise StageFailure("cleanup", "prior_pending_reconciliation_failed")
            try:
                pfile.unlink()
            except OSError:
                raise StageFailure("cleanup", "prior_pending_reconciliation_failed")

    def run(self, argv, timeout=None, *, deadline=None):
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
        self.write_pending_state()
        return True

    def start(self):
        self.current_stage = "start"
        ensure_private_dir(self.workspace, stage="start", prefix="workspace")
        self.started_at = time.monotonic()
        self.running_deadline = self.stage_deadline(self.running_timeout)
        self.start_attempted = True
        self.write_pending_state()
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
                        self.write_pending_state()
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
        self.current_stage = "capture"
        deadline = self.stage_deadline(self.capture_timeout)
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

    def stop_session(self, session_id, key, deadline):
        rc, _, _ = self.run([*self.hub, "sessions", "stop", session_id, "--key", key,
                             "--wait", "--timeout", str(max(1, int(self.stop_timeout))), "--interval", "1"],
                            deadline=deadline)
        return rc

    def stop(self, deadline, suffix="stop"):
        self.current_stage = "stop"
        return self.stop_session(self.session, f"{self.key}-{suffix}", deadline)

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

    def archive_intake_for(self, session_id, deadline):
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
        intake_id = node.get("intake_id") if isinstance(node, dict) else None
        if not isinstance(intake_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", intake_id):
            return False
        rc, _, _ = self.run(
            [*self.hub, "intake", "update", intake_id, "--status", "archived", "--reason", "deploy_gate_shell_canary"],
            deadline=deadline,
        )
        return rc == 0

    def archive_intake(self, deadline=None):
        limit_deadline = deadline if deadline is not None else (time.monotonic() + min(self.cmd_timeout, 30))
        return self.archive_intake_for(self.session, limit_deadline)

    def cleanup(self, deadline=None):
        limit_deadline = deadline if deadline is not None else (time.monotonic() + self.stop_timeout)
        if not self.session and self.start_attempted:
            # A lost or interrupted response is reconciled first with the identical
            # idempotent key and payload, then by listing sessions for our name/key.
            _, out, _ = self.run(self.start_args(), deadline=limit_deadline)
            self.remember_receipt(out)
            if not self.session:
                try:
                    items = self.list_items("cleanup", limit_deadline)
                except StageFailure:
                    items = []
                matched = next(
                    (
                        item for item in items
                        if isinstance(item, dict)
                        and (item.get("name") == self.name or item.get("key") == self.key)
                    ),
                    None,
                )
                if matched is not None and isinstance(matched.get("id"), str):
                    self.session = matched["id"]
                    self.native = self.native or matched.get("native_id")
                    self.write_pending_state()
        if not self.session:
            return "FAILED_needs_reconciliation" if self.start_attempted else "not_needed"
        if self.stop(limit_deadline, "cleanup") != 0:
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
            if self.candidate_sha is not None and self.session:
                try:
                    self.intake_archived = self.archive_intake(deadline=cleanup_deadline)
                except BaseException:
                    self.intake_archived = False
                if result["ok"] and not self.intake_archived:
                    result.update(ok=False, stage="cleanup", reason="intake_archive_failed")
            if (self.stopped or result["cleanup"] == "stopped") and (
                self.candidate_sha is None or self.intake_archived is True
            ):
                self.clear_pending_state()
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
        if self.candidate_sha is not None:
            result["candidate_sha"] = self.candidate_sha
            result["run_token"] = self.run_token
            result["host_version"] = self.host_version
            result["hub_pyz_sha256"] = self.hub_pyz_sha256
            result["intake_archived"] = bool(self.intake_archived)
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


def apply_alerting(result):
    sdir = state_dir()
    lock_fd = acquire_lock_fd(sdir / f"{result['mode']}.lock", 30.0, stage="lock", busy_reason="alert_state_lock_busy")
    try:
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
        candidate_sha=args.candidate_sha,
        candidate_cli_sha256=args.candidate_cli_sha256,
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
